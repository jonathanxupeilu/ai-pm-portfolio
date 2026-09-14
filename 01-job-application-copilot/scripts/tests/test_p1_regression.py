#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1/P2 防回归自检（不依赖 pytest，直接 `python scripts/tests/test_p1_regression.py`）。

为什么要有这个文件：2026-09-10 那轮改动里，有 4 个 bug 是靠「肉眼看输出」才发现的，
而它们全都会**静默**给出错误结论——正是这个技能最该防的失效模式：

  1. 批量改 pattern 的脚本把 SKILL_ATOMS 的**键名**也改了
     （键变成 `(?<![A-Za-z0-9])RLHF(?![A-Za-z0-9])`，报告里直接漏出正则语法）
  2. 打分文本误用整份档案而非 JD 正文 → 上一轮自己写进去的「真缺口：多模态」
     被下一轮当成 JD 提及，形成自我循环、分数不可复现
  3. 「优先」藏在括号里时整行被降级 → 「3-5年经验(平台类优先)」的年限要求丢失
  4. HTML 去内联标签时把两侧英文粘成 `PromptEngineering` → 词边界断言漏检

每条断言都对应上面一个已修的 bug，改坏了就会红。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import match_jd as M          # noqa: E402
import patch_jd as P          # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}" + (f"  ← {detail}" if detail else ""))
        FAILURES.append(name)


# ── 1. 词表完整性：键名必须是给人看的名字，不能是正则 ──────────────────
def test_vocab_keys():
    print("\n[1] 词表完整性")
    bad = [k for k in M.SKILL_ATOMS if re.search(r"[()\[\]\\|?*+]|\?-i:|A-Za-z0-9\]\)", k)]
    check("SKILL_ATOMS 键名不含正则语法", not bad, f"被污染：{bad}")
    bad = [k for k in M.SOFT_KEYWORDS
           if re.search(r"[()\[\]\\|?*+]|\?-i:", k)]
    check("SOFT_KEYWORDS 键名不含正则语法", not bad, f"被污染：{bad}")

    broken = []
    for src, table in (("SOFT_KEYWORDS", M.SOFT_KEYWORDS.values()),
                       ("SKILL_ATOMS", (v[0] for v in M.SKILL_ATOMS.values()))):
        for pats in table:
            for p in pats:
                try:
                    re.compile(p)
                except re.error as e:
                    broken.append((src, p, str(e)))
    check("所有 pattern 均可编译", not broken, str(broken[:3]))


# ── 2. ASCII 词边界：中文环境下要能命中，又不能撞子串 ──────────────────
def test_word_boundary():
    print("\n[2] ASCII 词边界（危险区：\\b 在 Unicode 下把中文当词字符）")
    rag = M.SOFT_KEYWORDS["RAG/检索增强"]
    hit = lambda s: any(re.search(p, s, re.I) for p in rag)
    check("「使用RAG架构」能命中（\\b 做不到）", hit("使用RAG架构"))
    check("「storage」不误命中", not hit("对象存储 storage"))
    check("「Ragas」不误命中（曾把评测框架当 RAG）", not hit("基于 Ragas 搭建评测"))

    fin = M.SKILL_ATOMS["RegTech 监管科技"][0]
    check("「Streamlit」不误命中 AML（曾坐实过）", not any(re.search(p, "Streamlit 原型", re.I) for p in fin))
    check("「AML/KYC」能命中", any(re.search(p, "AML/KYC 筛查", re.I) for p in fin))

    cpa = M.SKILL_ATOMS["CFA/FRM/CPA 金融资格"][0]
    check("「CPaaS」不误命中 CPA", not any(re.search(p, "CPaaS 集成", re.I) for p in cpa))
    check("「CFA、FRM、CPA」能命中", any(re.search(p, "（CFA、FRM、CPA）", re.I) for p in cpa))


# ── 3. A/B 测试四种写法 ────────────────────────────────────────────────
def test_ab_forms():
    print("\n[3] A/B 测试写法（baseline_v4.md 用的是带连字符的 A-B）")
    pats = M.SKILL_ATOMS["模型评估体系"][0]
    for s in ("A/B测试", "AB测试", "A-B 测试", "A B 测试", "效果评测", "评测体系"):
        check(f"命中「{s}」", any(re.search(p, s, re.I) for p in pats))


# ── 4. 「优先」只降级括号子句，不连坐整行 ─────────────────────────────
def test_pref_clause_split():
    print("\n[4] 括号内「优先」的降级范围")
    rest, pref = M._split_pref_clause("本科及以上学历（985 或 QS100 院校优先）")
    check("括号外条款留在 must", rest is not None and "本科及以上学历" in rest, str(rest))
    check("括号子句归 pref", pref is not None and "优先" in pref, str(pref))
    check("整行就是偏好句时不拆", M._split_pref_clause("有 AI 产品落地经验者优先")[0] is None)

    segs, _ = M.segment_jd("岗位职责\n岗位要求\n1. 本科及以上，3-5年产品经验(平台类、SaaS类产品优先)。")
    must_txt = "\n".join(segs["must"])
    pref_txt = "\n".join(segs["pref"])
    check("「3-5年产品经验」未被连带降级", "3-5年产品经验" in must_txt, must_txt)
    check("「平台类优先」归入 pref", "平台类" in pref_txt, pref_txt)


# ── 5. 打分文本必须是 JD 正文本体（防自我循环） ────────────────────────
ARCHIVE = """<!-- MATCHMETA tier=green score=1.0 company=甲 title=乙 date=2026-01-01 source=测试 url=none -->

## 硬门槛检查
- 🟢 无明显硬伤

## 双向匹配结果（JD ∧ 简历 ∧ 事实库）
- ⬜ **真缺口 1**：多模态
- 🔧 **可补缺口 1**：商业化/变现

## JD 原文摘录（匹配依据，链接只是溯源）
```
岗位职责
负责金融智能体产品设计，熟悉 RAG 与 Agent 编排。
任职要求
本科及以上学历（985 优先）
加分项
有 LangChain 经验者优先
【人工复核备注】
候选人 9 年算法经验，此处为 AI 批注，不是 JD 原文。
```
"""


def test_body_scope():
    print("\n[5] 打分/抽取的文本范围（防自我循环污染）")
    body = M.jd_body(ARCHIVE)
    check("剔除【人工复核备注】", "9 年算法经验" not in body)
    check("剔除「双向匹配结果」段（上一轮的产物）", "真缺口 1" not in body)
    check("剔除「硬门槛检查」段", "无明显硬伤" not in body)
    check("保留 JD 正文", "负责金融智能体产品设计" in body)

    atoms = M.extract_atoms(ARCHIVE)
    check("不吃「多模态」这类上一轮自己写的缺口",
          "多模态" not in atoms, f"误抽出：{sorted(atoms)}")
    check("「商业化/变现」不被当 JD 提及",
          "商业化/变现" not in atoms, f"误抽出：{sorted(atoms)}")
    check("真实提及的 LangChain 抽得到", "LangChain" in atoms, f"实抽：{sorted(atoms)}")

    # match_jd 与 rescore_pool 必须同口径，否则单份跑与全池重算对不上
    exc = re.search(r"##\s*JD\s*原文[^\n]*\n\s*```[^\n]*\n(.*?)\n\s*```", ARCHIVE, re.S)
    rescore_text = exc.group(1).split("【人工复核备注】")[0]
    check("与 rescore_pool 取文口径一致", rescore_text.strip() == body.strip())


# ── 6. HTML 去标签不能把英文粘住 ──────────────────────────────────────
def test_html_to_jd():
    print("\n[6] HTML → JD 正文的英文空格")
    html = ("<p>岗位职责</p><p>任职要求</p>"
            "<p>提示工程（<span>Prompt</span><span>Engineering</span>）、<b>RAG</b>等技术，熟悉GitHub</p>")
    out = P.html_to_jd(html)
    check("连续内联标签不粘连英文", "PromptEngineering" not in out, out)
    check("已还原为 Prompt Engineering", "Prompt Engineering" in out, out)
    check("中文夹标签不插空格（不留痕）",
          "学历" in P.html_to_jd("<p>岗位职责</p><p>本科及以上<span>学历</span>要求</p>"))

    # 粘连的英文会漏检 → 用真实 pattern 验证这条链是通的
    pats = M.SOFT_KEYWORDS["Prompt工程"]
    check("还原后 Prompt 能被命中", any(re.search(p, out, re.I) for p in pats))


# ── 7. 完整性闸门只认尾部截断痕迹 ─────────────────────────────────────
def test_completeness():
    print("\n[7] 完整性闸门")
    tail = "岗位职责\n负责智能体产品。\n任职要求\n" + "熟悉大模型应用。" * 30 + "\n（快照在此截断，任职要求原文未抓全）"
    lvl, _ = M.completeness_check(tail)
    check("尾部截断痕迹 → truncated", lvl == "truncated", lvl)

    mid = ("岗位职责\n负责智能体产品。\n（快照在此截断，任职要求原文未抓全）\n"
           + "【补录段｜来源：回源重抓】\n任职要求\n" + "熟悉大模型应用。" * 40)
    lvl, reasons = M.completeness_check(mid)
    check("中段截断痕迹 + 其后有内容 → 视为已补录", lvl == "", str(reasons))

    short = "岗位职责\n负责智能体产品。"
    check("正文过短 → truncated", M.completeness_check(short)[0] == "truncated")


# ── 8. 排除条件②：无 AI 含量的 PM 岗（2026-09-11 新增） ────────────────
def test_ai_content_gate():
    print("\n[8] 排除条件②（无 AI 含量 → 排除；截断时不武断封红）")
    # 正文须 >300 字符，否则先被完整性闸门判 truncated → 规则6 会降级黄档（那是保护，不是缺陷）
    no_ai = ("岗位职责\n"
             "1. 负责产品规划、需求文档与原型设计，推动跨团队协作落地，跟进版本规划与上线迭代。\n"
             "2. 梳理业务流程，输出产品方案，参与用户调研与数据分析，沉淀指标体系，支撑运营决策。\n"
             "3. 负责商业化路径设计、定价策略与付费转化提升，推动营收增长。\n"
             "4. 协调研发、设计、运营等干系人，保障交付落地与灰度发布。\n"
             "任职要求\n"
             "1. 本科及以上学历，3 年以上产品经理经验。\n"
             "2. 熟悉 B 端 SaaS 产品，具备解决方案设计与客户沟通能力。\n"
             "3. 具备较强的数据分析能力、跨部门协同与项目推动能力。\n"
             "4. 熟悉产品全生命周期管理，具备从 0 到 1 的产品设计经验。\n"
             "5. 具备良好的沟通表达与文档撰写能力，能独立推进项目。\n"
             "工作地点：上海")
    verdict, findings = M.hard_gate(no_ai)
    joined = "\n".join(findings)
    check("通篇无 AI 词 → 红档排除", verdict == "red", f"verdict={verdict}")
    check("红档说明落在「排除条件②」", "🔴 排除条件②" in joined, joined)

    with_ai = ("岗位职责\n负责大模型应用产品设计，熟悉 RAG 与 Agent 编排。\n"
               "任职要求\n本科及以上学历，3 年以上产品经理经验。\n工作地点：上海")
    _, findings_ai = M.hard_gate(with_ai)
    joined_ai = "\n".join(findings_ai)
    check("检出 AI 含量 → 不因②封红", "🔴 排除条件②" not in joined_ai, joined_ai)
    check("输出 AI 含量证据", "🟢 排除条件②" in joined_ai, joined_ai)

    # 截断 JD：AI 词可能随残文一起丢 → 不得把「没读到」当成「没有」
    trunc = ("岗位职责\n负责产品规划与需求文档。\n任职要求\n熟悉平台类产品设计流程。\n"
             + "熟悉产品设计流程。" * 40 + "\n（快照在此截断，任职要求原文未抓全）")
    verdict_t, findings_t = M.hard_gate(trunc)
    joined_t = "\n".join(findings_t)
    check("截断且无 AI 词 → 降级黄档而非封红",
          verdict_t != "red" and "🟡 排除条件②" in joined_t,
          f"verdict={verdict_t}｜{joined_t}")


def test_meta_strip():
    """jd_body 必须剥离摘录头部的入库元数据（2026-09-11 实测：抓取管道元数据
    「来源：猎聘 MCP 主动搜索」被当成 JD 正文 → 凭空多出「MCP 协议」技能点）。"""
    print("— test_meta_strip（元数据剥离）")
    meta = ("<!-- MATCHMETA tier=green score=42.2 company=X title=Y -->\n"
            "# X ｜ Y\n\n"
            "- **投递入口**：[点击投递](https://www.liepin.com/job/1.shtml)\n"
            "- **薪资**：50-60k·15薪\n"
            "- **来源**：猎聘 MCP 主动搜索（keyword=AI产品经理）\n"
            "- **URL**：https://www.liepin.com/job/1.shtml\n\n---\n\n"
            "职位介绍 \n 负责AI客服产品的规划与落地。\n")
    body = M.jd_body(meta)
    check("元数据行已剥离（无「投递入口」）", "投递入口" not in body, body[:80])
    check("元数据行已剥离（无「来源」）", "**来源**" not in body, body[:80])
    check("JD 正文保留", "负责AI客服产品" in body, body[:80])
    check("标题行已剥离", body.lstrip().startswith("职位介绍"), body[:40])

    # 真实 JD 里出现「- **薪资**：面议」这类内容不得误伤（只剥引导块）
    real = "岗位职责\n- **薪资**：面议，具体面谈\n负责产品规划。\n任职要求\n3年以上经验。"
    body2 = M.jd_body(real)
    check("正文中段的同类行不受影响", "**薪资**：面议" in body2, body2[:80])


def test_city_rule():
    """规则5城市判定：元数据/正文显式工作地才算，裸城市词与猎聘页面标题不算。"""
    import match_jd as M
    jd_body = ("岗位职责\n负责大模型产品规划，推动跨团队协作落地。\n"
               "任职要求\n本科及以上学历，3 年以上产品经验，熟悉 Agent 编排。"
               "有金融风控场景经验优先。能够承受快节奏工作。")
    # ① 正文显式「工作地点：宁波」→ 红
    v, f = M.hard_gate(jd_body + "工作地点：宁波-高新区", "")
    assert v == "red" and any("工作城市非上海" in x for x in f), (v, f)
    # ② 「工作地点宁波市」无冒号紧邻写法（宁波银行官方简章原文）→ 红
    v, f = M.hard_gate(jd_body + "工作地点宁波市，截止时间2026-12-31", "")
    assert v == "red", (v, f)
    # ③ 元数据干净地点 → 红；④ 猎聘页面标题假地点（搜索词城市）→ 不算
    meta = "- **投递入口**：x\n- **地点**：宁波-福明\n- **薪资**：25-40k"
    v, _ = M.hard_gate(jd_body, meta)
    assert v == "red", v
    meta_fake = "- **地点**：【上海 大模型产品经理招聘】-宁波银行上海招聘信息-猎聘"
    v, _ = M.hard_gate(jd_body, meta_fake)
    assert v != "red", v
    # ⑤ 裸城市词（分支描述「在深圳设有分支机构」）→ 不算，防众安类误报
    v, _ = M.hard_gate(jd_body + "公司在深圳设有分支机构。", "")
    assert v != "red", v
    # ⑥ 「宁波银行」公司名自带城市名 → 不算（正文中无其他工作地证据）
    v, _ = M.hard_gate("宁波银行总行 金融科技部。" + jd_body, "")
    assert v != "red", v
    # ⑦ 正文写明上海 → 通过
    v, _ = M.hard_gate(jd_body + "工作地点：上海市浦东新区", "")
    assert v != "red", v
    # ⑧ 支持远程豁免
    v, _ = M.hard_gate(jd_body + "工作地点：宁波（支持远程）", "")
    assert v != "red", v
    print("  ✅ 城市规则 8 条（显式工作地才算 / 页面标题与裸城市词不算 / 远程豁免）")


def test_rule6_title_scope():
    """规则6判据范围：岗位名里的 AI 字样也算 AI 含量（防「AI Infra PM」类误杀）。"""
    import match_jd as M
    jd_body = ("岗位职责\n1.负责推理平台的产品规划与功能设计，制定迭代路线图；\n"
               "2.跟踪性能、稳定性和模型兼容性的持续改进，与研发团队协作推动方案落地；\n"
               "3.深入理解客户在模型服务化场景下的痛点，输出解决方案与产品文档。\n"
               "任职要求\n1. 3 年以上后端平台、基础设施或技术产品经验；\n"
               "2. 理解队列、并发、调度等系统概念，能与算法团队顺畅协作；\n"
               "3. 具备良好的跨团队沟通能力与文档能力，对平台型产品有热情。\n"
               "加分项\n有云平台或开源社区贡献经验者优先，能承受快节奏工作。\n"
               "我们提供有竞争力的薪酬与期权激励，团队氛围开放，重视工程师文化与产品思维，"
               "欢迎对基础软件有长期投入意愿的伙伴加入，一起打磨服务于企业客户的核心平台产品。")
    # ① 岗位名带 AI、正文无 AI 词 → 不封红（2026-09-11 修）
    v, f = M.hard_gate(jd_body, title="AI Infra PM/推理平台产品经理")
    assert v != "red", (v, [x for x in f if "②" in x])
    # ② 岗位名和正文都没有 AI 词 → 仍封红
    v, _ = M.hard_gate(jd_body, title="高级产品经理")
    assert v == "red", v
    # ③ 正文带 AI 词 → 不封红（原有行为不回归）
    v, _ = M.hard_gate(jd_body + "熟悉大模型推理服务与效果评测体系。", title="高级产品经理")
    assert v != "red", v
    print("  ✅ 规则6范围 3 条（岗位名 AI 字样计入判据 / 全无才封红）")


def test_applied_exclusion():
    """已投递排除（2026-09-14 新增）：台账里状态为「已投/已读/约面/挂/别投」的岗位
    必须从推荐排序剔除 —— 用户要求「投递过的就别再给我推荐了」。

    为什么要有这条断言：判定链路上有三个**静默**失效点，全都不会报错，只会悄悄排错。
      ① 台账列名自带括号说明（`状态(待投/已投/…)`），按**位置**读列一旦改了列序就静默读错列；
      ② 公司+岗位比对键若做**大小写归一**，`AI agent产品经理` 与 `AI Agent产品经理` 会撞成
         同一个键 —— 2026-09-14 实测这俩是**两个不同岗位**（`/a/79448235` 要求 2-5 年 AI 产品
         经验；`/a/79611815` 要求 3 年产品经理 + 多模态/内容创作工具），归一会把用户
         **没投过**的那个也误排掉（当时已误排，改成大小写敏感才修好）；
      ③ 仓库自带的「示例科技有限公司」示例行状态列写着「已投」，不挡掉每轮都会误报
         「台账有已投递行在池里找不到档案」。
    """
    import csv as _csv
    import shutil
    import tempfile
    import rank_pool as R
    print("— test_applied_exclusion（已投递不再推荐）")

    # ① 比对键：空白差异要等价（同一岗位挂两个站点，标题会差一个空格）
    check("比对键忽略空白差异",
          R._ct_key("某咨询公司", "金融AI产品经理 / 智能体工程师")
          == R._ct_key("某咨询公司", "金融AI产品经理/智能体工程师"))
    # ② 比对键：大小写必须敏感（否则同名不同岗被误杀）
    check("比对键保持大小写敏感（防误杀同名不同岗）",
          R._ct_key("某知名公司", "AI agent产品经理")
          != R._ct_key("某知名公司", "AI Agent产品经理"))

    # ③ 用真台账的表头结构测状态判定
    header = ["公司", "岗位", "JD链接", "档位(green/yellow/red)", "投递日", "版本文件",
              "本版改了什么", "状态(待投/已投/已读/约面/挂/别投)", "下次跟进日", "复盘备注"]

    def row(co, ti, url, st):
        return [co, ti, url, "green", "2026-09-14", "x", "x", st, "2026-09-21", "t"]

    tmpdir = Path(tempfile.mkdtemp())
    tmp = tmpdir / "pipeline.csv"
    with tmp.open("w", encoding="utf-8-sig", newline="") as fh:
        _csv.writer(fh).writerows([
            header,
            row("甲", "已投岗", "https://www.liepin.com/job/1.shtml", "已投"),
            row("乙", "待投岗", "https://www.liepin.com/job/2.shtml", "待投"),
            row("丙", "挂掉岗", "https://www.liepin.com/job/3.shtml", "挂"),
            row("示例科技有限公司", "示例岗", "https://example.com/jobs/1001", "已投"),
        ])
    orig = R.PIPELINE
    R.PIPELINE = tmp
    try:
        idx = R.load_applied()
    finally:
        R.PIPELINE = orig
    shutil.rmtree(tmpdir, ignore_errors=True)

    check("已投 → 进排除索引", ("url", "https://www.liepin.com/job/1.shtml") in idx)
    check("挂 → 也进排除索引（不再推荐）", ("url", "https://www.liepin.com/job/3.shtml") in idx)
    check("待投 → 不进排除索引（仍要推荐）",
          ("url", "https://www.liepin.com/job/2.shtml") not in idx)
    check("仓库自带的示例行被挡掉（不误报「对不上号」）",
          ("url", "https://example.com/jobs/1001") not in idx)

    # ④ 池内档案：URL 优先；URL 缺失时公司+岗位兜底；没投过的绝不能排除
    check("池内岗位按 URL 命中",
          R.find_applied({"company": "丁", "title": "别的名",
                          "url": "https://www.liepin.com/job/1.shtml"}, idx) is not None)
    check("URL 缺失时靠公司+岗位兜底命中",
          R.find_applied({"company": "甲", "title": "已投岗", "url": ""}, idx) is not None)
    check("没投过的岗不得被排除",
          R.find_applied({"company": "戊", "title": "新岗",
                          "url": "https://www.liepin.com/job/9.shtml"}, idx) is None)

    # ⑤ 本机真台账：必须能读出「状态」列，且已投递行都解析出了键（防列名漂移）
    with R.PIPELINE.open(encoding="utf-8-sig", newline="") as fh:
        raw = list(_csv.reader(fh))
    si = next((i for i, h in enumerate(raw[0]) if "状态" in h), None)
    if si is None:
        check("本机台账有「状态」列", False, "列名漂移：找不到状态列，已投递排除会整体失效")
    else:
        n_applied = sum(1 for r in raw[1:]
                        if len(r) > si and r[si].strip() in R.NOT_RECOMMEND_STATES
                        and "示例" not in r[0])
        real = R.load_applied()
        n_url = len([k for k in real if k[0] == "url"])
        check("本机台账的已投递行都解析出了键（防列名漂移）",
              n_applied == 0 or n_url >= n_applied,
              f"台账已投递 {n_applied} 行 → 只解析出 {n_url} 个 URL 键")


def main():
    print("=" * 68)
    print("P1/P2 防回归自检 ｜ 每条断言对应一个已修 bug")
    print("=" * 68)
    for fn in (test_vocab_keys, test_word_boundary, test_ab_forms, test_pref_clause_split,
               test_body_scope, test_html_to_jd, test_completeness, test_ai_content_gate,
               test_meta_strip, test_city_rule, test_rule6_title_scope,
               test_applied_exclusion):
        fn()
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"   · {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
