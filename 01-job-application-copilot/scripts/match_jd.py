# -*- coding: utf-8 -*-
"""
match_jd.py — JD 三档打分引擎（规则版，非 LLM）
================================================
用法：
    python match_jd.py <jd文本文件路径> [--company 公司名] [--title 岗位名] [--save] [--source 主动搜索|用户投喂]
    python match_jd.py <jd文件> --resume auto        # 双向匹配（推荐）
    python match_jd.py <jd文件> --resume <简历.md>   # 指定简历

匹配分（供 RANKED_LIST.md 排序，rank_pool.py 读取）：
    score = 档位基础分 + 覆盖率 × 80
    档位基础分：green=20，yellow=0（red 一票否决，不进排序 list）
    （2026-09-10 调权：覆盖率从 40% 提到 80%，档位分退为「green 确认加分」。
      实证见 TIER_BASE 上方注释。green 满分 100，yellow 满分 80）

两种覆盖率（重要区别）：
  【单向模式】不带 --resume（旧行为，保留兼容）
      coverage = JD 文本里出现的关键词类数 / len(SOFT_KEYWORDS)
      ⚠️ 这只回答「这份 JD 写得全不全」，**不回答「你配不配」**。
      后果：JD 写得花哨的岗位会排前面，真正匹配你的朴素岗位靠后。
  【双向模式】带 --resume（推荐，2026-09-10 起）
      对每一类关键词做三方比对，分三类：
        ✅ 已命中   = JD 有 ∧ 简历有                     → 计 1.0 分
        🔧 可补缺口 = JD 有 ∧ 简历无 ∧ 事实库有证据       → 计 0.5 分（组版时补进去）
        ⬜ 真缺口   = JD 有 ∧ 简历无 ∧ 事实库也无         → 计 0 分（不补，诚实说法兜底）
      coverage = (已命中 + 可补缺口×0.5) / len(SOFT_KEYWORDS)
      这才回答「你配不配，缺口能不能补」。

规则版的意义：硬门槛判断是确定性规则，先跑规则再谈判断——LLM 只负责规则覆盖不了的语义部分（如"2个以上AI落地案例"的认定）。
三档规则（与 SKILL.md 一致）：
  🔴 red    硬门槛任一不过 → 别投
  🟡 yellow 年限 2 年 PM / 要求"2个以上AI落地案例"等可论证项 → 改造后投
  🟢 green  硬门槛全过 → 投
铁律：打分必须诚实，硬伤就标红；真缺口不许靠编造填平。
"""
import os
import sys
import re
import json
import argparse
import datetime
import pathlib

SKILL = pathlib.Path(__file__).resolve().parent.parent
POOL = SKILL / "jd-pool"
# 简历侧语料（双向匹配用）
DEFAULT_RESUME = SKILL / "resumes" / "baseline_v4.md"
FACTS_DIR = pathlib.Path.home() / ".workbuddy" / "career-facts"

# ── 策略文件 strategy.md 的定位（2026-09-10 迁出 career-facts）──────────────
# 背景：strategy.md 是**用户手工维护的决策文件**，不属于事实库（2026-09-10 用户裁定）。
# 已从 ~/.workbuddy/career-facts/ 迁到项目工作目录，与用户手写的 jd_raw/ 同级。
# 解析顺序（先命中先用）：
#   ① $JOB_SEEKING_DIR/strategy.md（环境变量显式指定，最高优先）
#   ② ~/WorkBuddy/job_seeking/strategy.md（默认项目目录）
#   ③ ~/.workbuddy/career-facts/strategy.md（旧位置，向后兼容，可安全删除）
JOB_SEEKING_DIR = pathlib.Path(
    os.environ.get("JOB_SEEKING_DIR") or (pathlib.Path.home() / "WorkBuddy" / "job_seeking")
)
STRATEGY_CANDIDATES = [
    JOB_SEEKING_DIR / "strategy.md",
    FACTS_DIR / "strategy.md",
]


def find_strategy_file():
    """定位策略文件；均不存在时返回 None（调用方须降级，不得臆测轮次）。"""
    for p in STRATEGY_CANDIDATES:
        if p.exists():
            return p
    return None

# PM 经验年限提取：匹配「N年以上/年-年」且上下文含产品相关词
PM_CTX = r"(产品|PM|项目|策划|产品经理)"
YEARS_PAT = re.compile(r"([1-9１-９一二三四五六七八九十]|10)\s*[-~—至到]?\s*([1-9１-９一二三四五六七八九十]?|10)?\s*年")

CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 投递链接：URL 以空白/中文/常见分隔符为界，避免把后随中文标题一起吞进去
URL_PAT = re.compile(r"https?://[^\s\u4e00-\u9fff｜|、，。）)】\]]+")


def extract_url(text: str):
    """从 JD 文本提取投递链接：优先『链接：』后的那个，否则取第一个 http(s) 链接。"""
    m = re.search(r"链接\s*[:：]\s*(https?://[^\s\u4e00-\u9fff｜|、，。）)】\]]+)", text)
    if m:
        return m.group(1)
    m = URL_PAT.search(text)
    return m.group(0) if m else ""


def to_int(s: str):
    s = s.strip()
    if s.isdigit():
        return int(s)
    return CN_NUM.get(s)


def extract_pm_years(text: str):
    """返回 (max_pm_years, evidence_list)。只在『N年 + 产品语境 + 经验』同时出现时计数。"""
    results = []
    for m in YEARS_PAT.finditer(text):
        start, end = m.span()
        window = text[max(0, start - 30): min(len(text), end + 30)]
        if re.search(PM_CTX, window) and ("经验" in window or "背景" in window or "从业" in window or "工作" in window):
            a = to_int(m.group(1))
            b = to_int(m.group(2)) if m.group(2) else a
            if a is None:
                continue
            results.append((max(a, b), window.replace("\n", " ").strip()))
    if not results:
        return 0, []
    results.sort(key=lambda x: -x[0])
    return results[0][0], [r[1] for r in results]


def hard_gate(text: str):
    """硬门槛判定。返回 (verdict, findings)，verdict: red/yellow/None"""
    findings = []

    # 规则1：PM 经验年限
    yrs, ev = extract_pm_years(text)
    # 语义例外：若年限要求是「产品/算法/数据」多选（非纯PM年限），候选人 9 年算法背景直接满足
    # 收紧：必须在「产品」与「算法/数据」之间有分隔符（顿号/逗号/斜杠/或/及），否则
    #   「AI 或算法类产品经验」（复星保德信 2026-09-08）会被误判为多选豁免 —— 那是另一回事
    ALT_SEP = r"产品\s*[,、/或及]\s*(算法|数据)|(算法|数据)\s*[,、/或及]\s*产品|(产品|算法|数据)\s*[,、/或及]\s*(产品|算法|数据)"
    alt_ok = any(re.search(ALT_SEP, w) for w in ev) \
        or ("非PM年限硬性" in text) or ("不限于产品" in text)
    if yrs >= 3 and alt_ok:
        findings.append(f"🟢 年限要求 {yrs} 年为『产品/算法/数据』多选或明确豁免PM年限，候选人 9 年算法背景满足 ｜ 证据：{ev[0]}")
    elif yrs >= 3:
        hard_hit = re.search(r"(必须|不低于|至少|优先考虑具有|要求)", text) and any(("必须" in w or "要求" in w or "至少" in w or "不低于" in w) for w in ev)
        if hard_hit:
            findings.append(f"🔴 PM相关经验 {yrs} 年且为硬性要求 → 一票否决 ｜ 证据：{ev[0]}")
            return "red", findings
        findings.append(f"🟡 PM相关经验 {yrs} 年（未明确写'必须'，需人工确认是否软要求）｜ 证据：{ev[0]}")
        return "yellow", findings
    elif yrs == 2:
        findings.append(f"🟡 要求 PM 经验 {yrs} 年，候选人正式 PM 年限不足 → 差距分析 + 曲线路径（内推/作品集）｜ 证据：{ev[0]}")
        return "yellow", findings
    if yrs == 0:
        findings.append("🟢 未检出明确的 PM 经验年限要求（或未与产品语境绑定）")

    # 规则2：学历硬卡（只有「必须/要求/硬性」与 985/211 绑定才算；「优先」是软偏好不算）
    if re.search(r"((必须|要求|硬性|严格)[^。；\n]{0,15}(985|211|双一流))|((985|211|双一流)[^。；\n]{0,15}(必须|硬性|严格))", text):
        findings.append("🔴 硬卡 985/211/双一流 院校（第一学历为天津商业大学）→ 一票否决")
        return "red", findings
    if re.search(r"(985|211|双一流)", text):
        findings.append("🟢 JD 提及 985/211 但仅作「优先」软偏好，非硬卡")
    else:
        findings.append("🟢 无 985/211 硬性院校要求")

    # 规则3：AI 落地案例数量
    if re.search(r"(2|两|二)\s*个以上.{0,8}(AI|人工智能|大模型).{0,6}(落地|应用|案例|产品)", text):
        findings.append("🟡 要求 2 个以上 AI 落地案例 → 可论证项：投研系统 + infoScience 双案例，需作品集支撑")
        return "yellow", findings

    # 规则4：年龄红线（触发示例：某 JD 实录「年龄不超过 35 岁」）
    # 出生年按 career-facts/profile.md 的真实值填写，下方为占位默认值
    birth_year = 1990
    age = datetime.date.today().year - birth_year
    age_m = re.search(r"年龄不超过\s*(\d{2})\s*岁", text) or re.search(r"年龄[^。；\n]{0,6}(\d{2})\s*岁(?:以下|以内)", text) or re.search(r"(\d{2})\s*岁以下", text)
    if age_m:
        limit = int(age_m.group(1))
        if limit < age:
            findings.append(f"🔴 年龄要求不超过 {limit} 岁，候选人年龄 {age} 岁 → 一票否决")
            return "red", findings
        findings.append(f"🟢 年龄要求 {limit} 岁内，候选人 {age} 岁满足")

    # 规则5：城市（仅当 JD 文本明确写出非上海城市且无远程字样）
    city_m = re.search(r"(工作地[点址]?[:：]\s*)?(北京|深圳|杭州|广州|成都|武汉|南京|苏州|西安|厦门|长沙|重庆|天津|青岛|郑州)", text)
    if city_m and not re.search(r"远程|支持远程|可远程|弹性办公", text) and "上海" not in text:
        findings.append(f"🔴 工作城市疑似非上海（检出：{city_m.group(2)}）且无远程字样 → 一票否决（请人工复核）")
        return "red", findings
    findings.append("🟢 城市检查通过（上海 / 未检出冲突城市 / 支持远程）")

    return None, findings


# ── 完整性闸门（2026-09-10 新增）──────────────────────────────────────────
# 起因：陆家嘴金融AI 那份 JD 抓取时残缺——只有「岗位职责」前 3 条，「任职要求 + 加分项」
#   整段丢失。但系统照常打 44.4 分，并报「真缺口 0 ｜ 无」，等于告诉用户「这岗你全都配」。
#   实际截图里的 21 个技术词（RLHF / DPO / ReAct / LangChain / CrewAI / CFA…）
#   因源文本没了，一个都没探到 —— 这是**假阴性**，比漏掉一个岗位危险得多：
#   它把「没查到」伪装成了「查过了，没有」。
#   当时只在【人工复核备注】写了「拿到完整JD后可重评」—— 事后批注拦不住假结论。
#
# 闸门做三件事：
#   ① META 写 truncated=1 → 榜单显示 ⚠️，一眼看出这份 JD 不可信
#   ② gap 不再报 0，改报「未探明」——「查过了没有」和「没查到」必须分开说
#   ③ 输出显著警告，提示走 patch_jd.py 补录后重算
#
# 判据设计纪律：**宁可漏检，不可误杀**（与 rank_pool 的下线词表同一条）。
#   实测否决过一条「末行突兀」判据（正文不以句号结尾 = 疑似截断）：全池误杀 10/35 = 28%，
#   因为末行常是福利标签（「六险一金」「周末双休」）或关键词行，天然不带句号。已废弃。
# ⚠️ 不收英文 "truncated"：实测长宁那份的补录正文混进了 Vue 模板残留
#   （`{{ truncatedJobTitle3 }}`），一个英文变量名就把完整 JD 误判成残缺。
#   收词只认中文截断标记——中文 JD 里写截断提示一定是中文。
TRUNCATE_WORDS = ("快照在此截断", "原文未抓全", "原文截断", "内容被截断",
                  "以下省略", "篇幅所限", "（截断）", "[截断]", "内容不全")
DUTY_HEADS = ("岗位职责", "工作职责", "职位描述", "工作内容", "你将负责",
              "岗位描述", "职责描述", "工作职责描述")
REQ_HEADS = ("任职要求", "岗位要求", "任职资格", "职位要求", "任职条件", "岗位基本需求",
             "岗位基本要求", "我们希望", "加分项", "你需要具备", "任职资格要求")
SHORT_JD_CHARS = 300     # 低于此：正文不足以支撑打分
SUSPECT_JD_CHARS = 1200  # 缺「任职要求」段且低于此：高度疑似抓取截断
TAIL_ZONE = 0.85         # 截断痕迹落在此比例之后才算「真截断」（见 completeness_check 说明）
# 存档摘录上限。2026-09-10 前这里写死 raw[:2000] 且**不带任何标记**——
# 抓到 5000 字符的完整 JD 会被静默砍掉 3000，档案里看不出少了东西，
# 之后的重算、补录全在残文上跑且无人察觉。超限必须显式写明，禁止静默截断。
EXCERPT_LIMIT = 8000


def jd_body(text: str) -> str:
    """从「档案 md」或「原始 JD 文本」里取出真正的 JD 正文本体。

    档案的正文在 `## JD 原文摘录` 的代码块内；原始 JD 文本则整篇即正文。
    两条路径统一走这里，保证「完整性判断」和「打分」看到的是同一份文本。
    """
    m = re.search(r"##\s*JD\s*原文[^\n]*\n\s*```[^\n]*\n(.*?)\n\s*```", text, re.S)
    body = m.group(1) if m else text
    return body.split("【人工复核备注】")[0]


def completeness_check(text: str):
    """JD 完整性闸门。返回 (level, reasons)，level ∈ {'', 'suspect', 'truncated'}。

    truncated = 有硬证据（显式截断痕迹 / 正文过短）
    suspect   = 结构可疑（有职责段却无要求段，且篇幅偏短）——提示但不武断
    """
    body = jd_body(text)
    reasons, level = [], ""
    n = len(body.strip())

    # 判据1：截断痕迹——**只看它是否落在正文尾部**。
    # 补录过的档案里，原始快照那句「快照在此截断」会留在中段，后头接着补录段。
    # 若按「出现即残缺」判，就永远挂着 ⚠️ —— 补了也治不好，实测踩过这个坑。
    # 截断提示的语义是「后面没了」，所以只有出现在尾部才算数。
    hit = next((w for w in TRUNCATE_WORDS if w in body), None)
    if hit:
        pos = body.rfind(hit)
        if pos >= len(body) * TAIL_ZONE:
            reasons.append(f"检出截断痕迹「{hit}」（位于正文尾部 {int(pos * 100 / max(len(body), 1))}% 处）")
            level = "truncated"
        else:
            reasons.append(f"检出历史截断痕迹「{hit}」（在中段 {int(pos * 100 / max(len(body), 1))}% 处，"
                           f"其后仍有内容 → 视为已补录，不判残缺）")
    if n < SHORT_JD_CHARS:
        reasons.append(f"正文仅 {n} 字符（＜{SHORT_JD_CHARS}），不足以支撑打分")
        level = "truncated"
    has_duty = any(h in body for h in DUTY_HEADS)
    has_req = any(h in body for h in REQ_HEADS)
    if has_duty and not has_req and n < SUSPECT_JD_CHARS:
        reasons.append(f"有「岗位职责」段却无「任职要求/加分项」段，正文仅 {n} 字符 → 典型抓取截断")
        if level != "truncated":
            level = "suspect"
    return level, reasons


# ── 三层语义切分（必备 / 优先 / 加分项）──────────────────────────────
# 为什么分层：JD 里「任职要求 3. 三年以上经验」和「加分项 5. 有 CFA」对候选人的
# 约束力完全不同，但旧口径把两者一视同仁——都只是「JD 提到了」。结果是一份
# 加分项堆得很满的 JD 会把分数抬上去，而候选人误以为那些是硬门槛。
# 分层后「必备层覆盖率」成为独立指标：必备层没覆盖 = 简历有硬伤（真该慌）；
# 加分项没覆盖 = 正常，本来就只有少数人有。**加分项不该当扣分项用。**
PREF_MARKS = ("优先", "更佳", "更受青睐", "preferred", "prefer")
PLUS_HEADS = ("加分项", "加分条件", "优先条件", "bonus", "额外加分", "锦上添花")
LEVEL_CN = {"must": "必备", "pref": "优先", "plus": "加分项"}
HEAD_MAXLEN = 20         # 段标题长度上限：正文里提到「加分项」三个字不该切换段落


def _split_pref_clause(ln: str):
    """把「优先」的降级范围限定在**它所在的那个括号子句**里。

    返回 (括号外剩余条款, 优先子句)；若「优先」不在括号内、或括号外没剩什么，
    返回 (None, ln) 表示整行按优先处理。

    为什么需要：JD 常把偏好条件塞进括号附在硬要求后面——
      「3-5年产品经验(平台类、SaaS类产品优先)」   ← 经验年限是硬要求
      「本科及以上学历（985 或 QS100 院校优先）」 ← 学历门槛是硬要求
    旧口径「整行含优先即降级」会把这些硬条款一起打成 pref，必备层就漏计了。
    """
    for mk in PREF_MARKS:
        m = re.search(r"[（(][^（()）]*" + re.escape(mk) + r"[^（()）]*[)）]", ln, re.IGNORECASE)
        if not m:
            continue
        rest = (ln[:m.start()] + ln[m.end():]).strip(" \t，,、；;。.")
        # 括号外仍有 ≥MIN 个字符才值得拆，否则说明整行本来就是偏好句
        if len(rest) >= 6:
            return rest, m.group(0)
    return None, ln


def segment_jd(text: str):
    """把 JD 正文按约束力切成三层。返回 (segs, line_level)。

    segs       = {"must": [行...], "pref": [行...], "plus": [行...]}
    line_level = {行号: level}，供技能点定位用

    规则：
      ① 段标题切换所属级别（标题行本身不参与「优先」判定）；
      ② 行内「优先」只降级该行，**不改变段落级别**——否则「有 X 经验者优先」
         后面紧跟着的必备条款会被一起降级（陆家嘴 JD 第 61-62 行就是这种）；
      ③ 「优先」若落在括号内，只降级那个括号子句，括号外的硬条款留在 must
         （见 _split_pref_clause：全池 4 行踩到，如「3-5年产品经验(平台类优先)」）。
    """
    body = jd_body(text)
    segs = {"must": [], "pref": [], "plus": []}
    line_level = {}
    state = "must"
    for i, raw_ln in enumerate(body.split("\n")):
        ln = raw_ln.strip()
        if not ln:
            continue
        low = ln.lower()
        # 段标题判定优先于行内降级：标题行本身不参与「优先」词判定
        if len(ln) <= HEAD_MAXLEN and any(h.lower() in low for h in PLUS_HEADS):
            state = "plus"
        elif len(ln) <= HEAD_MAXLEN and any(h in ln for h in DUTY_HEADS + REQ_HEADS):
            state = "must"
        lvl = state
        if state != "plus" and any(m in low for m in PREF_MARKS):
            # 「优先」只降级它所在的那个括号子句，不连坐整行。
            # 例：「3-5年产品经验(平台类、SaaS类产品优先)」——经验年限是硬要求，
            #      不能因为括号里带个「优先」就被一起降级（全池 4 行踩到这个）。
            rest, pref_clause = _split_pref_clause(ln)
            if rest is not None:
                segs["must"].append(rest)
                segs["pref"].append(pref_clause)
                line_level[i] = "must"
                continue
            lvl = "pref"
        segs[lvl].append(ln)
        line_level[i] = lvl
    return segs, line_level


# 软匹配：JD 高频关键词 → 简历可命中的证据（关联 career-facts 条目）
SOFT_KEYWORDS = {
    "大模型/LLM": [r"大模型", r"(?<![A-Za-z0-9])LLMs?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])GPTs?(?![A-Za-z0-9])", r"语言模型"],
    "RAG/检索增强": [r"(?<![A-Za-z0-9])RAGs?(?![A-Za-z0-9])", r"检索增强", r"向量(检索|库)", r"(?<![A-Za-z0-9])embeddings?(?![A-Za-z0-9])", r"知识库问答", r"知识库"],
    "Agent/编排": [r"(?<![A-Za-z0-9])Agent(?:s|ic)?(?![A-Za-z0-9])", r"智能体", r"(?<![A-Za-z0-9])multi-agent(?![A-Za-z0-9])", r"工作流编排", r"(?<![A-Za-z0-9])MCP(?![A-Za-z0-9])",
                   r"工具调用", r"(?<![A-Za-z0-9])function ?calling(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])tool ?use(?![A-Za-z0-9])", r"工作流"],
    "Prompt工程": [r"(?<![A-Za-z0-9])[Pp]rompt(?:s|ing)?(?![A-Za-z0-9])", r"提示词", r"提示工程", r"指令工程"],
    "效果评测": [r"评测", r"评估体系", r"(?<![A-Za-z0-9])badcase(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])bad case(?![A-Za-z0-9])", r"[Aa][-/\s]?[Bb]\s?测试", r"效果评估"],
    "幻觉治理": [r"幻觉", r"(?<![A-Za-z0-9])hallucination(?![A-Za-z0-9])", r"内容安全", r"可控性", r"可解释"],
    "多模态": [r"多模态", r"文生图", r"语音", r"视频生成"],
    "Vibe Coding/AI编程": [r"(?<![A-Za-z0-9])Vibe ?Coding(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])Cursor(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])Claude ?Code(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])Co[p]ilot(?![A-Za-z0-9])", r"AI编程"],
    "产品设计/PRD": [r"(?<![A-Za-z0-9])PRDs?(?![A-Za-z0-9])", r"需求文档", r"产品方案", r"原型", r"(?<![A-Za-z0-9])MVPs?(?![A-Za-z0-9])", r"产品规划"],
    "数据分析/指标": [r"数据分析", r"指标体系", r"北极星", r"数据驱动", r"埋点"],
    "跨团队协作": [r"跨团队", r"跨职能", r"协作能力", r"推动", r"干系人", r"协同"],
    "商业化/变现": [r"商业化", r"变现", r"营收", r"付费转化", r"定价", r"转化"],
    "金融/风控场景": [r"金融", r"风控", r"信贷", r"银行", r"保险"],
    "合规/隐私": [r"合规", r"隐私", r"数据安全", r"个保法", r"备案", r"监管"],
    "C端产品": [r"C端", r"(?<![A-Za-z0-9])to ?C(?![A-Za-z0-9])", r"用户增长", r"用户体验"],
    "B端/SaaS": [r"B端", r"(?<![A-Za-z0-9])to ?B(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])SaaS(?![A-Za-z0-9])", r"企业客户", r"解决方案"],
    "技术架构": [r"架构", r"系统设计", r"技术方案", r"技术选型"],
    "交付落地/迭代": [r"上线", r"落地", r"交付", r"迭代", r"灰度", r"版本规划"],
}
# 词表来源：16 类为人工初版；2026-09-10 用 mine_keywords.py 对 35 份 JD 做词频挖掘后补 8 个高频
# 且当时未覆盖的词（工具调用/知识库/工作流/架构/产品规划/转化/上线/迭代）。
# 改词表 = 改尺子，改完必须跑 `python scripts/rescore_pool.py --apply` 全池重跑，否则新旧分混排。

# ── 细粒度技能点（SKILL_ATOMS）──────────────────────────────────────
# 为什么需要它：SOFT_KEYWORDS 的 18 个类目衡量的是「面的覆盖」——一个类目里任一
# pattern 命中即算命中。JD 里出现「架构」二字就能让「技术架构」类目过关，看不出
# JD 要的究竟是 ReAct 还是 Plan-and-Execute；也说不出「我会 Prompt，但 Few-shot
# 会不会」。技能点衡量的是「点的覆盖」：可独立判定、可独立补课、可统计市场频次。
#
# route 语义 —— 缺口出现时该走哪个出口：
#   learn  可短周期补的技术栈/工具：看文档 + 动手做 demo 就能覆盖，1-2 周见效
#   drill  面试高频概念：补的是「能讲清楚原理」，不是「会操作」
#   cover  硬资质/经验门槛：短期补不了，只能靠作品集 + 迁移经验覆盖
#   ""     通用产品能力：与 SOFT_KEYWORDS 同源，不单独分流
#
# 收录纪律：只收**池内 JD 真实出现过**的词（依据写在条目第 3 位），不凭想象扩表。
# 改这张表 = 改能力雷达的尺子，改完需重跑 skill_radar.py 复核。
# 大小写敏感的词用 (?-i:...) 局部关闭 IGNORECASE——`ReAct`（Agent 推理范式）
# 与 `React`（前端框架）必须区分，否则前端 JD 会误判成 Agent 岗。
SKILL_ATOMS = {
    # —— Agent / 编排 ——
    "ReAct 推理范式": ([r"(?<![A-Za-z0-9])(?-i:ReAct)(?![A-Za-z0-9])", r"推理[-—+]行动"],
                     "learn", "陆家嘴JD必备层；Agent 推理-行动循环范式"),
    "Plan-and-Execute": ([r"(?<![A-Za-z0-9])[Pp]lan-?and-?[Ee]xecute(?![A-Za-z0-9])", r"先规划后执行"],
                         "learn", "陆家嘴JD必备层；与 ReAct 并列的 Agent 范式"),
    "Multi-Agent 协作": ([r"(?<![A-Za-z0-9])[Mm]ulti-?[Aa]gent(?:s)?(?![A-Za-z0-9])", r"多智能体", r"多 ?Agent"],
                        "learn", "陆家嘴JD必备层；池内 Agent 岗高提及"),
    "Function Calling / Tool Use": ([r"(?<![A-Za-z0-9])[Ff]unction ?[Cc]alling(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Tt]ool ?[Uu]se(?![A-Za-z0-9])", r"工具调用"],
                                    "learn", "陆家嘴JD必备层；Agent 与外部系统交互的接口层"),
    "Agent 工作流编排": ([r"工作流编排", r"任务编排", r"Agent ?工作流", r"编排"],
                      "", "陆家嘴JD必备层；与 SOFT_KEYWORDS 的「Agent/编排」同源"),
    "MCP 协议": ([r"(?<![A-Za-z0-9])MCP(?![A-Za-z0-9])"], "learn", "陆家嘴/其他 JD 出现；模型上下文协议，新兴且易上手"),
    "Human-in-the-loop": ([r"(?<![A-Za-z0-9])in-?the-?loop(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Hh]uman-?in(?![A-Za-z0-9])", r"人工(审核|干预|复核)"],
                          "drill", "陆家嘴JD必备层；高风险场景的兜底设计模式"),

    # —— RAG 检索增强 ——
    "RAG 架构细节（分块/重排序/上下文）": ([r"分块策略", r"重排序", r"(?<![A-Za-z0-9])[Rr]erank(?:s|ing|er)?(?![A-Za-z0-9])", r"检索策略",
                                            r"上下文管理", r"[Cc]ontext ?窗口", r"(?<![A-Za-z0-9])[Cc]hunk(?:s|ing)?(?![A-Za-z0-9])"],
                                          "learn", "陆家嘴JD必备层；区分「用过 RAG」与「设计过 RAG」"),
    "向量数据库": ([r"向量(数据库|库|检索)", r"(?<![A-Za-z0-9])[Vv]ector ?[Dd][Bb](?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Mm]ilvus(?![A-Za-z0-9])",
                  r"(?<![A-Za-z0-9])[Pp]inecone(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Ff]aiss(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Cc]hroma(?:db)?(?![A-Za-z0-9])"], "learn", "池内 RAG 相关岗"),
    "知识库构建": ([r"知识库", r"索引构建", r"文档清洗", r"数据源接入"],
                  "", "陆家嘴JD必备层；与 SOFT_KEYWORDS 同源"),

    # —— 主流框架 / 工具 ——
    "LangChain": ([r"(?<![A-Za-z0-9])[Ll]ang[Cc]hain(?![A-Za-z0-9])"], "learn", "陆家嘴JD加分项；池内 3 份提及"),
    "LlamaIndex": ([r"(?<![A-Za-z0-9])[Ll]lama[Ii]ndex(?![A-Za-z0-9])"], "learn", "陆家嘴JD加分项"),
    "AutoGen": ([r"(?<![A-Za-z0-9])[Aa]uto[Gg]en(?![A-Za-z0-9])"], "learn", "陆家嘴JD加分项；事实库已有证据"),
    "CrewAI": ([r"(?<![A-Za-z0-9])[Cc]rew[Aa][Ii](?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Cc]rew ?AI(?![A-Za-z0-9])"], "learn", "陆家嘴JD加分项"),
    "Coze/扣子": ([r"(?<![A-Za-z0-9])[Cc]oze(?![A-Za-z0-9])", r"扣子"], "learn", "池内 3 份提及；零代码 Agent 平台，上手最快"),
    "Dify": ([r"(?<![A-Za-z0-9])[Dd]ify(?![A-Za-z0-9])"], "learn", "池内 3 份提及；开源 LLMOps 平台"),
    "NL2SQL 自然语言转 SQL": ([r"(?<![A-Za-z0-9])NL2SQL(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Tt]ext-?to-?SQL(?![A-Za-z0-9])"], "learn", "陆家嘴JD必备层；金融报表场景高频"),

    # —— 模型能力 ——
    "Prompt 高级技巧（CoT/Few-shot/Role-playing）": ([r"思维链", r"(?<![A-Za-z0-9])[Cc]o[Tt](?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Ff]ew-?shot(?![A-Za-z0-9])",
                                                     r"(?<![A-Za-z0-9])[Rr]ole-?playing(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])System ?Prompt(?![A-Za-z0-9])",
                                                     r"提示词(工程|优化)"],
                                                    "learn", "陆家嘴JD必备层；区分「会写提示词」与「懂高级技巧」"),
    "Fine-tuning 微调": ([r"(?<![A-Za-z0-9])[Ff]ine-?tuning(?![A-Za-z0-9])", r"微调", r"(?<![A-Za-z0-9])[Ss]FT(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Ll]o[Rr]A(?![A-Za-z0-9])"],
                        "learn", "陆家嘴JD必备层+加分项；事实库已有证据"),
    "训练数据工程": ([r"训练数据", r"数据标注", r"标注标准", r"数据清洗", r"数据工程"],
                    "learn", "陆家嘴JD必备层+加分项"),
    "模型部署/推理优化": ([r"推理优化", r"推理加速", r"模型部署", r"缓存策略", r"量化"],
                        "learn", "陆家嘴JD必备层（Agent 基础设施）"),
    "多模态": ([r"多模态", r"文生图", r"语音交互", r"语音识别", r"(?<![A-Za-z0-9])[Aa][Ss][Rr](?![A-Za-z0-9])"],
              "", "池内多份提及；与 SOFT_KEYWORDS 同源"),
    "Vibe Coding / AI 编程": ([r"(?<![A-Za-z0-9])[Vv]ibe ?[Cc]oding(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Cc]ursor(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Cc]laude ?[Cc]ode(?![A-Za-z0-9])", r"AI ?编程"],
                              "", "用户简历已强化该标签"),

    # —— 评估 / 对齐 / 治理（面试问得最多，动手要求最低）——
    "RLHF 人类反馈强化学习": ([r"(?<![A-Za-z0-9])RLHF(?![A-Za-z0-9])", r"人类反馈"], "drill", "陆家嘴JD必备层；高频面试概念"),
    "DPO 直接偏好优化": ([r"(?<![A-Za-z0-9])DPO(?![A-Za-z0-9])", r"直接偏好优化"], "drill", "陆家嘴JD必备层；与 RLHF 常被连问"),
    "红队测试（Red Teaming）": ([r"红队", r"(?<![A-Za-z0-9])[Rr]ed ?[Tt]eam(?![A-Za-z0-9])", r"对抗测试"],
                              "drill", "陆家嘴JD必备层+需求段；安全评测手段"),
    "幻觉缓解": ([r"幻觉", r"(?<![A-Za-z0-9])[Hh]allucination(?![A-Za-z0-9])"], "drill", "陆家嘴JD必备层；面试必考"),
    "推理延迟": ([r"推理(延迟|耗时)", r"端到端耗时", r"(?<![A-Za-z0-9])[Ll]atency(?![A-Za-z0-9])"],
              "drill", "陆家嘴JD必备层；工程化挑战四件套之一"),
    "成本控制": ([r"成本控制", r"成本-?性能", r"[Tt]oken ?成本", r"降本", r"(?<![A-Za-z0-9])ROI(?![A-Za-z0-9])"],
              "drill", "陆家嘴JD必备层；工程化挑战四件套之一"),
    "安全对齐": ([r"安全对齐", r"内容审核", r"输出审核", r"敏感信息脱敏", r"可控性"],
              "drill", "陆家嘴JD必备层；工程化挑战四件套之一"),
    # 「A/B 测试」的写法在 JD 与简历里有 A/B、AB、A-B、A B 四种形态，
    # 只写 [Aa]/?[Bb] 会漏掉带连字符的「A-B 测试」（baseline_v4.md 就是这种写法，
    # 实测导致「模型评估体系」被误判为真缺口）。
    "模型评估体系": ([r"模型评估", r"评估指标", r"评测体系", r"[Aa][-/\s]?[Bb]\s?测试",
                    r"回归测试", r"准确率", r"召回率", r"自动化评估", r"效果评测", r"效果评估"],
                    "drill", "陆家嘴JD必备层；面试爱问「怎么衡量有没有用」"),
    "badcase 分析": ([r"(?<![A-Za-z0-9])[Bb]ad ?[Cc]ase(?![A-Za-z0-9])", r"错误分析", r"失败案例", r"效果分析"],
                    "drill", "陆家嘴JD必备层；迭代闭环的关键动作"),
    "大模型原理": ([r"大模型原理", r"(?<![A-Za-z0-9])[Tt]ransformers?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Aa]ttention(?:s)?(?![A-Za-z0-9])", r"注意力机制",
                  r"(?<![A-Za-z0-9])decoder-?only(?![A-Za-z0-9])", r"模型原理"], "drill", "陆家嘴JD必备层；面试基础题"),
    "POC/概念验证": ([r"(?<![A-Za-z0-9])POCs?(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Pp]roof ?of ?[Cc]oncept(?![A-Za-z0-9])", r"概念验证", r"可行性验证"],
                    "drill", "池内 4 份提及；面试讲项目常用词"),

    # —— 资质 / 经验门槛（短期补不了）——
    "CFA/FRM/CPA 金融资格": ([r"(?<![A-Za-z0-9])CFA(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])FRM(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])CPA(?![A-Za-z0-9])", r"从业资格"], "cover",
                            "陆家嘴JD加分项；需长周期考试"),
    "名校/学历门槛": ([r"985", r"211", r"(?<![A-Za-z0-9])QS ?\d+(?![A-Za-z0-9])", r"双一流", r"硕士", r"研究生"],
                    "cover", "陆家嘴JD优先层；不可改变项，只能靠其他维度补偿"),
    "英文技术论文阅读": ([r"英文(技术)?论文", r"英语(口语)?(沟通)?流利", r"英文文献"],
                      "cover", "陆家嘴JD必备层；可短期靠精读 2-3 篇缓解"),
    "RegTech 监管科技": ([r"(?<![A-Za-z0-9])[Rr]eg[Tt]ech(?![A-Za-z0-9])", r"监管科技", r"合规科技", r"(?<![A-Za-z0-9])AML(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])KYC(?![A-Za-z0-9])", r"制裁筛查"],
                        "cover", "陆家嘴JD加分项+需求段；金融合规线专属经验"),
    "金融业务知识": ([r"股权", r"基金", r"债券", r"信贷", r"投研", r"资产管理", r"信用评级", r"风控"],
                    "cover", "陆家嘴JD必备层；用户具备金融风控背景，属可迁移资产"),
    "金融行业 AI 交付经验": ([r"金融(行业)?.{0,8}(项目|交付|落地|智能应用)",
                            r"金融科技.{0,8}经验", r"银行、证券、基金、保险"],
                            "cover", "陆家嘴JD优先层；优先项里的硬通货"),

    # —— 通用产品能力 ——
    "PRD/原型/架构图": ([r"(?<![A-Za-z0-9])PRDs?(?![A-Za-z0-9])", r"原型图?", r"架构图", r"(?<![A-Za-z0-9])[Aa]xure(?![A-Za-z0-9])", r"墨刀", r"(?<![A-Za-z0-9])[Ff]igma(?![A-Za-z0-9])"],
                      "", "陆家嘴JD必备层；与 SOFT_KEYWORDS 同源"),
    "数据驱动/指标体系": ([r"数据驱动", r"指标体系", r"埋点", r"北极星", r"(?<![A-Za-z0-9])DAU(?![A-Za-z0-9])",
                        r"留存率", r"任务完成率"], "", "陆家嘴JD必备层"),
    "跨团队协同": ([r"跨团队", r"跨职能", r"协调.{0,6}团队", r"干系人", r"协同"], "",
                "陆家嘴JD必备层；与 SOFT_KEYWORDS 同源"),
    "B端/客户对接": ([r"B端", r"(?<![A-Za-z0-9])to ?B(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])[Bb]2[Bb](?![A-Za-z0-9])", r"客户对接", r"企业客户", r"解决方案"],
                    "", "池内多份 B 端岗"),
    "个人 AI 项目作品": ([r"(?<![A-Za-z0-9])[Gg]it[Hh]ub(?![A-Za-z0-9])", r"个人(项目|作品)", r"开源项目", r"作品集",
                      r"AI ?产品 ?[Dd]emo"], "learn",
                      "陆家嘴JD加分项；**唯一可完全自主创造**的加分项，性价比最高"),
    "合规/隐私/监管": ([r"合规", r"隐私", r"数据安全", r"个保法", r"备案", r"监管政策"],
                    "", "陆家嘴JD必备层；与 SOFT_KEYWORDS 同源"),
    "灰度/上线交付": ([r"灰度", r"上线", r"交付", r"发布流程"], "", "陆家嘴JD必备层"),
}

# 技能点统计口径：**按 JD 份数去重，不按词频**。
# 原因之一：补录过的档案里原始残段与补录段并存，同一句话会出现两次，按词频统计必然虚高。
# 原因之二：市场需求的真实信号是「多少家公司在要这个」，不是「同一家公司说了几遍」。
ATOM_RANK = {"must": 3, "pref": 2, "plus": 1}
META_LINE_PAT = re.compile(r"^(链接[:：]|技能标签[:：]|（补录段|（补录段结束|https?://|【)")


def _is_meta_line(ln: str) -> bool:
    """过滤档案元信息行与段标题行——它们不是 JD 正文，不该参与技能点抽取。"""
    s = ln.strip()
    if not s:
        return True
    if META_LINE_PAT.match(s):
        return True
    if len(s) <= HEAD_MAXLEN and any(h in s for h in DUTY_HEADS + REQ_HEADS + PLUS_HEADS):
        return True
    return False


def extract_atoms(text: str):
    """从 JD 抽取细粒度技能点。返回 {技能点: {levels, top, evidence, route, note}}。

    top = 该技能点在本 JD 中出现的**最高约束层级**（must > pref > plus）——
    必备层和加分项都提到时按必备算（要求更高的一侧说了算）。
    evidence 保留命中原文，供能力雷达引用 JD 出处（最多 3 条，够溯源即可）。
    """
    segs, _ = segment_jd(text)
    out = {}
    for lvl in ("must", "pref", "plus"):
        for ln in segs[lvl]:
            if _is_meta_line(ln):
                continue
            for atom, (pats, route, note) in SKILL_ATOMS.items():
                hit = None
                for p in pats:
                    hit = re.search(p, ln, re.IGNORECASE)
                    if hit:
                        break
                if not hit:
                    continue
                rec = out.setdefault(atom, {"levels": [], "evidence": [],
                                            "route": route, "note": note})
                if lvl not in rec["levels"]:
                    rec["levels"].append(lvl)
                if len(rec["evidence"]) < 3:
                    rec["evidence"].append((lvl, ln.strip()[:120]))
    for rec in out.values():
        rec["levels"].sort(key=lambda x: -ATOM_RANK[x])
        rec["top"] = rec["levels"][0]
    return out


def atom_coverage(atom: str, resume_text: str, facts_text: str):
    """单个技能点的三方覆盖判定，返回 'resume' | 'facts' | 'gap'。

    与 bidirectional_match 同一口径，供能力雷达复用——两边各写一套判定
    必然漂移，所以只留这一个真源：
      resume 简历已写        → 已具备，ATS 能抓到
      facts  简历没写但事实库有证据 → 可补（组版时补进简历）
      gap    两处皆无        → 真缺口，按 route 分流出口
    """
    if any(re.search(p, resume_text, re.IGNORECASE) for p in SKILL_ATOMS[atom][0]):
        return "resume"
    if any(re.search(p, facts_text, re.IGNORECASE) for p in SKILL_ATOMS[atom][0]):
        return "facts"
    return "gap"


def layer_stats(text: str, resume_text: str, facts_text: str):
    """必备层覆盖统计（P1 指标）。返回 dict。

    **不参与 match score**——这是刻意的。总分衡量「18 个类目的覆盖率」，刚在
    v3 调平（green 20 + 覆盖率×80），再往里叠一层层级权重就是二次翻桌，而且
    两个问题本就该分开问：
        总分      → 这份 JD 跟我整体的匹配度（横向比岗位用）
        必备覆盖  → 这份 JD 的硬性要求我覆盖到位没有（纵向看简历硬伤用）
    必备层没覆盖 ≠ 不能投，但**投之前必须知道**；加分项没覆盖则属正常。

    must_hit 用严格口径：**只有简历里写了才算**。事实库有证据但简历没写的
    归 must_fill（组版时可补），不能算已覆盖——简历上没写，HR 就看不到。
    """
    atoms = extract_atoms(text)
    detail = {}
    for a, rec in atoms.items():
        detail[a] = {"cov": atom_coverage(a, resume_text, facts_text),
                     "top": rec["top"], "route": rec["route"],
                     "evidence": rec["evidence"]}
    must = {a: d for a, d in detail.items() if d["top"] == "must"}
    return {
        "must_total": len(must),
        "must_hit": sum(1 for d in must.values() if d["cov"] == "resume"),
        "must_fill": sum(1 for d in must.values() if d["cov"] == "facts"),
        "must_gap": sum(1 for d in must.values() if d["cov"] == "gap"),
        "atom_total": len(detail),
        "atom_hit": sum(1 for d in detail.values() if d["cov"] == "resume"),
        "atom_fill": sum(1 for d in detail.values() if d["cov"] == "facts"),
        "atom_gap": sum(1 for d in detail.values() if d["cov"] == "gap"),
        "detail": detail,
    }


# 缺口出口的中文标签：能力雷达与组版提示共用，避免两处各写一套说法
ROUTE_CN = {
    "learn": "🔧 可补技术栈（学习清单）",
    "drill": "🎯 面试重点（补的是讲清楚）",
    "cover": "🧱 硬资质经验（短期补不了，走作品集覆盖）",
    "": "🧩 通用产品能力（可补）",
}


def route_of(atom: str) -> str:
    """技能点的出口路由。未标注 route 的按通用能力处理——**不能让它无处可去**，
    否则缺口就成了死胡同，又回到「查出问题却没有出口」的老毛病。"""
    return SKILL_ATOMS[atom][1] or ""


def fmt_must(st: dict) -> str:
    """META 里的 must 字段：`命中/必备总数`。必备层为空时写 none（覆盖率无意义）。"""
    if not st["must_total"]:
        return "none"
    return f"{st['must_hit']}/{st['must_total']}"


def render_atom_section(lst, truncated: bool = False) -> list:
    """生成「技能点层级分析」的 markdown 行。

    match_jd.py 首次存档与 rescore_pool.py 重跑**共用这一个渲染器**——
    两处各写一套必然格式漂移，且简历更新后技能点段会变陈旧却没人发现。
    """
    if not lst:
        return []
    d = lst["detail"]
    must_items = {a: v for a, v in d.items() if v["top"] == "must"}
    out = ["", "## 技能点层级分析（必备 / 优先 / 加分项）"]
    if lst["must_total"]:
        out.append(f"- **必备层覆盖**：{lst['must_hit']}/{lst['must_total']}（简历已覆盖）"
                   f" ｜ 可补 {lst['must_fill']} ｜ 真缺 {lst['must_gap']}"
                   f"　← 独立指标，不进匹配分")
    else:
        out.append("- **必备层覆盖**：本 JD 未识别出必备层条款（正文过短或结构特殊）")
    out.append("- 口径：必备层 = 岗位职责 + 岗位要求里的硬性条款；「…者优先」为优先层；"
               "独立「加分项」段为加分项层。加分项缺口不算硬伤。")
    if truncated:
        out.append("- ⚠️ **本 JD 存档残缺，下列缺口数属「未探明」而非「无缺口」**，"
                   "补全后需重跑：`python scripts/patch_jd.py <文件> --refetch`")

    ok = [a for a, v in must_items.items() if v["cov"] == "resume"]
    if ok:
        out += ["", f"### ✅ 必备层已具备（{len(ok)}）"] + [f"- {a}" for a in ok]
    fm = [a for a, v in must_items.items() if v["cov"] == "facts"]
    if fm:
        out += ["", f"### 🔧 必备层可补（{len(fm)}）事实库有证据，组版补进简历"]
        out += [f"- [ ] {a}" for a in fm]
    gm = {a: v for a, v in must_items.items() if v["cov"] == "gap"}
    if gm:
        out += ["", f"### ⬜ 必备层缺口（{len(gm)}）按出口分流"]
        by_route = {}
        for a, v in gm.items():
            by_route.setdefault(route_of(a), []).append((a, v))
        for rk in ("learn", "drill", "cover", ""):
            if rk not in by_route:
                continue
            out.append(f"#### {ROUTE_CN[rk]}")
            for a, v in by_route[rk]:
                ev = v["evidence"][0][1][:90] if v["evidence"] else ""
                out.append(f"- **{a}**" + (f"　← JD：「{ev}」" if ev else ""))
    pg = [a for a, v in d.items() if v["top"] == "plus" and v["cov"] == "gap"]
    if pg:
        out += ["", f"### ⭕ 加分项缺口（{len(pg)}）正常，非硬门槛，不影响投递"]
        out += [f"- {a}" for a in pg]
    return out


# ── 匹配分权重（唯一真源，rescore_pool.py / rank_pool.py 从这里取，勿在别处重复定义）──
# 2026-09-10 两次调权，目标是「让真实命中数说话」：
#   v1（原始）：green 60 / yellow 35 + 覆盖率×40 —— 覆盖率满分仅 40，档位几乎主导排序
#   v2（首调）：green 50 / yellow 25 + 覆盖率×50 —— 仍不够：green 命中 0 类也有 50 分，
#              压在 yellow 高命中之上（MiniMax 命中 1 类 52.2 vs 某证券命中 12 类 53.9）
#   v3（现行）：green 20 / yellow 0  + 覆盖率×80 —— 用 35 份真实 JD 模拟四方案后的拐点
# 设计语义：**覆盖率是匹配分本体，档位只是对 green 的「确认加分」**。
#   yellow 得 0 分底，纯靠覆盖率挣分；green 因硬门槛全过额外 +20。
# 实证效果（全池 35 岗）：命中 12+补 2 的某证券从第 11 名升至第 6 名，首次进入主力区；
#   同时 Top3 保持稳定（擎翌/浪潮/长宁），未因调权翻桌。
# 理论上限：green 满分 100，yellow 满分 80（yellow 档位本身就有硬伤，不该能拿满分）。
TIER_BASE = {"green": 20.0, "yellow": 0.0, "red": 0.0}
COVERAGE_WEIGHT = 80.0


def soft_match(text: str):
    """【单向模式】只在 JD 文本里搜关键词。只回答「JD 写得全不全」。"""
    hits, misses = [], []
    for cat, pats in SOFT_KEYWORDS.items():
        matched = [p for p in pats if re.search(p, text, re.IGNORECASE)]
        (hits if matched else misses).append((cat, matched))
    return hits, misses


def load_corpus(resume_path, facts_dir=FACTS_DIR):
    """加载简历侧语料：(简历文本, 事实库文本)。缺文件返回空串，不崩。"""
    rt = ""
    p = pathlib.Path(resume_path)
    if p.exists():
        rt = p.read_text(encoding="utf-8", errors="replace")
    ft = ""
    fd = pathlib.Path(facts_dir)
    if fd.is_dir():
        ft = "\n".join(f.read_text(encoding="utf-8", errors="replace")
                       for f in sorted(fd.glob("*.md")))
    return rt, ft


def _snippet(text: str, pats, width: int = 26):
    """取简历里第一处命中关键词的上下文片段。

    返回 (展示片段, 命中位置索引)。位置必须原样传出——片段做了空白归一化，
    拿归一化后的字符串回原文 find 会失败（尤其跨换行时）。
    """
    for p in pats:
        m = re.search(p, text, re.IGNORECASE)
        if not m:
            continue
        s = max(0, m.start() - width // 2)
        e = min(len(text), m.end() + width)
        return re.sub(r"\s+", " ", text[s:e]).strip(), m.start()
    return "", -1


def _locate(text: str, pos: int):
    """按命中位置判断落在简历哪个章节——区分「技能清单列词」与「项目经历实证」。"""
    if pos < 0:
        return "未知"
    heads = [(m.start(), m.group(0).lstrip("# ").strip())
             for m in re.finditer(r"^#{2,3}\s+.+$", text, re.MULTILINE)]
    cur = "开头（姓名/求职意向行）"
    for idx, h in heads:
        if idx <= pos:
            cur = h
        else:
            break
    return cur


def bidirectional_match(jd_text: str, resume_text: str, facts_text: str):
    """【双向模式】三方比对，返回 (hits, fillable, true_gaps)。

    每项是 (类目, JD中命中的证据词列表)。
      hits      JD 有 ∧ 简历有                → 简历已覆盖，ATS 能抓到
      fillable  JD 有 ∧ 简历无 ∧ 事实库有证据 → 组版时补进简历（有真凭实据）
      true_gaps JD 有 ∧ 简历无 ∧ 事实库也无   → 不补，面试用诚实说法兜底
    """
    hits, fillable, gaps = [], [], []
    for cat, pats in SOFT_KEYWORDS.items():
        ev = [p for p in pats if re.search(p, jd_text, re.IGNORECASE)]
        if not ev:
            continue  # JD 没提这一类，不参与计分
        if any(re.search(p, resume_text, re.IGNORECASE) for p in pats):
            hits.append((cat, ev))
        elif any(re.search(p, facts_text, re.IGNORECASE) for p in pats):
            fillable.append((cat, ev))
        else:
            gaps.append((cat, ev))
    return hits, fillable, gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jd_file", help="JD 文本文件（md/txt 均可）")
    ap.add_argument("--company", default="未知名公司")
    ap.add_argument("--title", default="未知岗位")
    ap.add_argument("--save", action="store_true", help="写入 jd-pool 对应档位目录")
    ap.add_argument("--source", default="用户投喂", choices=["主动搜索", "用户投喂"], help="JD 来源")
    ap.add_argument("--url", default=None, help="投递链接（不传则自动从 JD 文本提取）")
    # 「不投」不是策略轮次，是**显式排除**：用于真目标大厂的非点名岗（2026-09-10 用户裁定）
    ap.add_argument("--purpose", default="",
                    choices=["", "占坑", "练手", "熟流程", "主攻", "不投"],
                    help="投递轮次定位（分轮策略）；「不投」= 显式排除，不进推荐排序")
    ap.add_argument("--resume", default="auto", metavar="PATH|auto|none",
                    help="双向匹配（默认 auto=resumes/baseline_v4.md）；none=退回旧的单向模式（仅诊断用）")
    ap.add_argument("--facts-dir", default=str(FACTS_DIR), help="事实库目录（默认 ~/.workbuddy/career-facts）")
    args = ap.parse_args()

    jd_path = pathlib.Path(args.jd_file)
    if not jd_path.exists():
        print(f"❌ 文件不存在：{jd_path}")
        sys.exit(2)
    raw = jd_path.read_text(encoding="utf-8")
    # 打分/抽取只看 JD 正文本体，必须用 jd_body() 而不是 raw 的一切两半：
    #   ① 【人工复核备注】是 AI 批注（曾含「候选人 9 年算法」被当 JD 年限提取，通联支付/某证券两次误判的根源）
    #   ② 「硬门槛检查 / 双向匹配结果」是本脚本**上一轮自己写进去的产物**。若不清掉会形成自我循环：
    #      上一轮列出的缺口类目名（如「商业化/变现」）会被下一轮当成「JD 提及」而变成命中，
    #      分数越跑越高且不可复现。rescore_pool 一直只用摘录代码块，两边口径必须一致。
    text = jd_body(raw)

    gate_verdict, findings = hard_gate(text)
    # 完整性闸门先跑：JD 若是被抓残的，后面算出来的缺口数一律视为「未探明」
    comp_level, comp_reasons = completeness_check(raw)

    # 双向模式：--resume auto 解析为通用基线母版；none 退回单向（仅诊断用）
    resume_path = None
    if args.resume and args.resume != "none":
        resume_path = DEFAULT_RESUME if args.resume == "auto" else pathlib.Path(args.resume)
        if not pathlib.Path(resume_path).exists():
            print(f"⚠️ 简历文件不存在：{resume_path} → 退回单向模式")
            resume_path = None
    resume_text, facts_text = load_corpus(resume_path, args.facts_dir) if resume_path else ("", "")

    if resume_path:
        hits, fillable, gaps = bidirectional_match(text, resume_text, facts_text)
        misses = [(c, []) for c, _ in fillable] + [(c, []) for c, _ in gaps]
        n_hit, n_fill, n_gap = len(hits), len(fillable), len(gaps)
        n_jd = n_hit + n_fill + n_gap  # JD 实际提及的类目数
        coverage = f"{n_hit}+{n_fill}/{n_jd}·{len(SOFT_KEYWORDS)}"
        ratio = (n_hit + n_fill * 0.5) / len(SOFT_KEYWORDS)
    else:
        hits, misses = soft_match(text)
        fillable, gaps = [], []
        n_hit, n_fill, n_gap = len(hits), 0, 0
        n_jd = len(hits)
        coverage = f"{len(hits)}/{len(SOFT_KEYWORDS)}"
        ratio = len(hits) / len(SOFT_KEYWORDS)

    # P1 必备层覆盖：只在双向模式下有意义（单向模式没有简历可比对）
    lst = layer_stats(text, resume_text, facts_text) if resume_path else None

    tier = gate_verdict or "green"
    icon = {"red": "🔴 别投", "yellow": "🟡 改造后投", "green": "🟢 投"}[tier]

    # 匹配分：档位基础分 + 覆盖率×权重（权重见 TIER_BASE 上方注释；red 一票否决不排序）
    tier_base = TIER_BASE[tier]
    score = round(tier_base + ratio * COVERAGE_WEIGHT, 1)

    jd_url = (args.url or "").strip() or extract_url(raw)
    url_note = jd_url if jd_url else "（未抓到，需人工补）"

    print("=" * 62)
    print(f"JD 三档打分 ｜ {args.company} · {args.title} ｜ 采集 {datetime.date.today()}")
    print("=" * 62)
    print(f"\n判定：{icon} ｜ 匹配分：{score}")
    print(f"投递链接：{url_note}")
    print("\n【硬门槛检查】")
    for f in findings:
        print(f"  {f}")
    if resume_path:
        comp_note = "　← ⚠️ 后两项不可信：JD 残缺，缺口未探明" if comp_level else ""
        print(f"\n【双向匹配】已命中 {n_hit} ｜ 可补缺口 {n_fill} ｜ 真缺口 {n_gap}"
              f"（JD 共提及 {n_jd}/{len(SOFT_KEYWORDS)} 类）{comp_note}")
        print(f"  简历：{pathlib.Path(resume_path).name} ｜ 事实库：{pathlib.Path(args.facts_dir).name}")
        if hits:
            print("  ✅ 已命中（简历已覆盖）：")
            for cat, pats in hits:
                # 简历侧证据片段：区分「技能清单里列了个词」与「项目经历里有实证」——两者分量不同
                # 用全量变体表（非 JD 侧命中子集）回查简历：JD 写 "LLM"、简历写 "大模型" 是常态
                r_ev, r_pos = _snippet(resume_text, SOFT_KEYWORDS[cat])
                where = _locate(resume_text, r_pos)
                print(f"     · {cat} ｜ [{where}] 「{r_ev}」")
        if fillable:
            print("  🔧 可补缺口（事实库有证据，组版时补进简历）：")
            for cat, pats in fillable:
                print(f"     · {cat} ← JD 原文：{'、'.join(sorted(set(re.findall('|'.join(pats), text, re.IGNORECASE))))[:40]}")
        if gaps:
            print("  ⬜ 真缺口（事实库也无，不补，面试诚实说法兜底）：")
            for cat, pats in gaps:
                print(f"     · {cat}")
        if n_fill:
            print(f"\n  → 组版提示：补上 {n_fill} 项可补缺口，覆盖率将从 {round((n_hit)/len(SOFT_KEYWORDS)*100)}% 提到 {round((n_hit+n_fill)/len(SOFT_KEYWORDS)*100)}%")

        # ── P1：必备层覆盖 + 技能点分流 ──
        # 类目级覆盖看的是「面」，这里看的是「点」：JD 到底要哪些具体能力、我还差哪几个。
        # 与 match score 分开呈现——总分是横向比岗位用的，必备层是纵向看简历硬伤用的。
        if lst:
            d = lst["detail"]
            must_items = {a: v for a, v in d.items() if v["top"] == "must"}
            comp_note2 = "　⚠️ JD 残缺，下列缺口数未探明" if comp_level else ""
            if lst["must_total"]:
                print(f"\n【必备层覆盖】{lst['must_hit']}/{lst['must_total']}（简历已覆盖）"
                      f" ｜ 可补 {lst['must_fill']} ｜ 真缺 {lst['must_gap']}"
                      f"　← 独立指标，不进匹配分{comp_note2}")
                print("  必备层 = 岗位职责 + 岗位要求里的硬性条款；加分项不计入。")
                ok = [a for a, v in must_items.items() if v["cov"] == "resume"]
                if ok:
                    print(f"  ✅ 已具备（{len(ok)}）：{'、'.join(ok)}")
                fm = [a for a, v in must_items.items() if v["cov"] == "facts"]
                if fm:
                    print(f"  🔧 可补（{len(fm)}）事实库有证据，组版补进简历：{'、'.join(fm)}")
                gm = {a: v for a, v in must_items.items() if v["cov"] == "gap"}
                if gm:
                    print(f"  ⬜ 必备层缺口 {len(gm)} 项，按出口分流：")
                    by_route = {}
                    for a, v in gm.items():
                        by_route.setdefault(route_of(a), []).append((a, v))
                    for rk in ("learn", "drill", "cover", ""):
                        if rk not in by_route:
                            continue
                        print(f"     {ROUTE_CN[rk]}")
                        for a, v in by_route[rk]:
                            ev = v["evidence"][0][1][:52] if v["evidence"] else ""
                            print(f"        · {a}" + (f"　← JD：「{ev}」" if ev else ""))
            pg = [a for a, v in d.items() if v["top"] == "plus" and v["cov"] == "gap"]
            if pg:
                print(f"\n  ⭕ 加分项缺口 {len(pg)} 项（正常，非硬门槛，不影响投递）：{'、'.join(pg)}")
            print("  → 跨岗位的学习优先级用 `python scripts/skill_radar.py` 按市场频次排序")
    else:
        print(f"\n【软匹配】关键词覆盖 {coverage}（⚠️ 单向模式：只统计 JD 侧，未比对简历。加 --resume auto 得真实匹配度）")
        if hits:
            for cat, pats in hits:
                print(f"  ✅ {cat} ← {'、'.join(sorted(set(re.findall('|'.join(pats), text, re.IGNORECASE))))[:40]}")
        if misses and tier != "red":
            print("  ⬜ 未命中：" + "、".join(c for c, _ in misses))
    if tier == "red":
        print("\n（红档不出简历。理由已在硬门槛中，防重复研究同一个坑。）")
    if tier == "yellow":
        print("\n（黄档：组版时走『差距分析 + 曲线路径』——内推/作品集打动 + 针对性改写方向。）")
    if tier == "green":
        print("\n（绿档：可进一岗一版组版流程——JD关键词→tag映射→templates母版组版→ats_check。）")

    if args.save:
        d = POOL / tier
        d.mkdir(parents=True, exist_ok=True)
        fname = f"{args.company}_{args.title}_{datetime.date.today():%Y%m%d}.md".replace("/", "_")
        mode = "bi" if resume_path else "jd"
        meta = (
            f"<!-- MATCHMETA tier={tier} score={score} coverage={coverage} "
            f"company={args.company} title={args.title} "
            f"date={datetime.date.today()} source={args.source} url={jd_url or 'none'} purpose={args.purpose or 'none'} "
            f"mode={mode} hit={n_hit} fill={n_fill} gap={n_gap} "
            f"must={fmt_must(lst) if lst else 'none'} -->"
        )
        lines = [
            meta,
            f"# {args.company} ｜ {args.title}",
            f"",
            f"- **投递入口**：{('[点击投递 → ' + (args.company + ' · ' + args.title) + '](' + jd_url + ')') if jd_url else '⚠️ 未抓到链接，需人工补'}",
            f"- 档位：**{icon}**",
            f"- 匹配分：**{score}**（档位基础分 {tier_base} + {'双向' if resume_path else '单向'}覆盖率 × {int(COVERAGE_WEIGHT)}）",
            f"- 来源：{args.source}（{args.jd_file}）",
            *( [f"- 轮次定位：**{args.purpose}**"] if args.purpose else [] ),
            f"- 采集日期：{datetime.date.today()}",
            f"- 关键词覆盖：{coverage}" + ("（格式：已命中+可补缺口 / JD提及类目·总类目）" if resume_path else "（⚠️ 单向模式，未比对简历）"),
            f"",
            f"## 硬门槛检查",
            *[f"- {f}" for f in findings],
        ]
        if resume_path:
            lines += [
                f"",
                f"## 双向匹配结果（JD ∧ 简历 ∧ 事实库）",
                f"- 打分模式：`{mode}`（简历 {pathlib.Path(resume_path).name} + 事实库 career-facts）",
                f"- ✅ **已命中 {n_hit}**：" + ("、".join(c for c, _ in hits) or "无"),
                f"- 🔧 **可补缺口 {n_fill}**：" + ("、".join(c for c, _ in fillable) or "无"),
                f"- ⬜ **真缺口 {n_gap}**：" + ("、".join(c for c, _ in gaps) or "无"),
            ]
            if fillable:
                lines += [f"", "**组版动作**：以下项事实库有证据，组版时必须补进简历——"]
                lines += [f"- [ ] {c}（补进技能区或项目描述，不得编造新事实）" for c, _ in fillable]
            if gaps:
                lines += [f"", "**不补项**：以下项事实库无证据，组版时不补，面试用诚实说法兜底——"]
                lines += [f"- {c}" for c, _ in gaps]
            # 技能点层级分析：类目级看「面」，这里看「点」——JD 到底要哪些具体能力、还差哪几个
            lines += render_atom_section(lst, truncated=bool(comp_level))
        # 超限必须显式标记（旧版 raw[:2000] 静默砍，档案里看不出少了东西）
        excerpt = raw if len(raw) <= EXCERPT_LIMIT else (
            raw[:EXCERPT_LIMIT]
            + f"\n\n（⚠️ 存档截断：原文共 {len(raw)} 字符，本摘录仅保留前 {EXCERPT_LIMIT}；"
              f"评分依据以本摘录为准，补全请用 patch_jd.py）"
        )
        lines += [
            f"",
            f"## JD 原文摘录（匹配依据，链接只是溯源）",
            f"```",
            excerpt,
            f"```",
        ]
        out = d / fname
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n💾 已存入 jd-pool/{tier}/{fname}")

    sys.exit(0 if tier != "red" else 1)


if __name__ == "__main__":
    main()
