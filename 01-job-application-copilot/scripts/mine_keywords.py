# -*- coding: utf-8 -*-
"""
mine_keywords.py — 从 JD 池反向沉淀「AI PM 岗高频词」
=====================================================
用法：
    python mine_keywords.py                 # 输出到 stdout
    python mine_keywords.py --df 3 --top 40 # 调文档频次阈值与条数
    python mine_keywords.py --save          # 落盘 jd-pool/KEYWORD_MINING.md

为什么需要它：
    match_jd.py 的 SOFT_KEYWORDS 16 类词表是**拍脑袋定的**（按个人经验列的类目）。
    池子里有 35 份真实 JD，应该让数据说话：哪些词是上海 AI PM 岗真正的高频要求？
    哪些高频词我们的词表根本没覆盖（→ 简历零命中却浑然不觉）？

方法（无第三方分词库，纯规则）：
  1. 候选生成：中文 2-6 字 n-gram + 英文/缩写词（A-Za-z 起头）
  2. 文档频次过滤（DF）：只在 ≥N 份 JD 里出现才算通用要求，避免单家公司的黑话
  3. 子串抑制：「产品」「产品经理」都会高频，保留更长的那个（更具体、更有信息量）
  4. 停用词剔除：经验/能力/相关/优先/熟悉/负责 等无区分度的通用词
  5. 与现有 SOFT_KEYWORDS 对照：标出「高频但未覆盖」→ 建议补进词表或简历

诚实边界：n-gram 是统计方法，会产出「工作经」这类跨词碎片。
          本脚本只负责把候选摆出来，**是否补进词表由人判断**，不自动改 SOFT_KEYWORDS。
"""
import re
import sys
import argparse
import pathlib
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

SKILL = pathlib.Path(__file__).resolve().parent.parent
POOL = SKILL / "jd-pool"
EXCERPT_PAT = re.compile(r"##\s*JD\s*原文[^\n]*\n\s*```[^\n]*\n(.*?)\n\s*```", re.S)

# 无区分度的通用词（招聘 JD 八股 + 采集元数据），不计入高频词
STOP = {
    # 招聘八股
    "工作", "经验", "能力", "相关", "优先", "熟悉", "负责", "以上", "学历", "本科",
    "岗位", "职责", "要求", "任职", "公司", "团队", "业务", "进行", "具备", "良好",
    "以及", "能够", "熟练", "掌握", "使用", "参与", "推动", "协助", "完成", "分析",
    "设计", "开发", "优化", "提升", "支持", "管理", "沟通", "协作", "独立", "较强",
    "专业", "背景", "年龄", "性别", "不限", "薪资", "待遇", "福利", "五险一金",
    "周末", "双休", "职责", "岗位职", "任职要", "招聘", "投递", "入职", "简历",
    # 过于通用、无区分度（几乎每份 AI PM 的 JD 都有，不构成差异化要求）
    "产品", "AI", "智能", "技术", "方案", "流程", "需求", "工具", "用户", "数据",
    "理解", "持续", "定义", "应用", "效果", "迭代", "规划", "工程", "场景", "落地",
    "模型", "系统", "平台", "项目", "功能", "内容", "服务", "研究", "行业", "客户",
    "建设", "输出", "跟踪", "结合", "探索", "尝试", "关注", "学习", "逻辑", "思维",
    # 采集元数据（非 JD 内容）
    "链接", "来源", "采集", "日期", "http", "https", "www", "com", "html", "shtml",
    "job", "detail", "liepin", "quanzhi", "zhipin", "iguopin", "51job", "index",
}

CN_PAT = re.compile(r"[\u4e00-\u9fff]{2,6}")
EN_PAT = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,15}")

# A 轨词典：AI PM 岗位的能力/技术/场景术语。
# 说明：词典本身是经验性的，**统计的价值在于告诉我们在真实 JD 里哪些真的高频**
#      ——包括「以为重要但 JD 几乎不提」的词，那同样是有用的信号。
DICT = [
    # —— 大模型技术栈 ——
    "大模型", "LLM", "RAG", "检索增强", "向量库", "向量检索", "embedding", "知识库",
    "Agent", "智能体", "multi-agent", "多智能体", "工作流", "编排", "MCP",
    "Prompt", "提示词", "指令工程", "上下文工程", "微调", "fine-tuning", "SFT",
    "蒸馏", "量化", "推理", "训练", "部署", "模型选型", "开源模型", "闭源模型",
    "多模态", "文生图", "语音识别", "ASR", "TTS", "OCR", "知识图谱", "NL2SQL",
    "function calling", "函数调用", "插件", "工具调用", "幻觉", "hallucination",
    "对齐", "RLHF", "可控性", "可解释", "内容安全", "Token", "成本优化",
    # —— 评测与数据 ——
    "评测", "评估体系", "benchmark", "badcase", "bad case", "A/B", "AB测试",
    "灰度", "埋点", "指标体系", "数据闭环", "北极星", "效果评估", "测试样例",
    "验收标准", "数据分析", "数据驱动", "复盘", "Bad Case",
    # —— 产品方法论 ——
    "PRD", "需求文档", "产品方案", "原型", "原型设计", "Axure", "Figma", "墨刀",
    "MVP", "最小可行", "需求洞察", "用户调研", "竞品分析", "产品规划", "路线图",
    "roadmap", "优先级", "KANO", "JTBD", "用户旅程", "故事地图", "三图",
    "上线", "迭代", "商业化", "变现", "定价", "GTM", "增长", "留存", "转化",
    "DAU", "MAU", "NPS", "OKR", "KPI", "ROI",
    # —— 行业与场景 ——
    "金融", "风控", "信贷", "银行", "保险", "证券", "支付", "投研", "投顾",
    "医疗", "教育", "电商", "零售", "制造", "工业", "物流", "供应链", "政务",
    "法律", "人力", "HR", "客服", "营销", "CRM", "ERP", "SaaS", "B端", "C端",
    "toB", "toC", "企业服务", "解决方案", "私有云", "公有云", "私有化部署",
    "生命科学", "医药", "跨境电商", "招聘",
    # —— 协作与软技能 ——
    "跨团队", "跨部门", "跨职能", "协作", "干系人", "项目管理", "敏捷", "Scrum",
    "驻场", "客户现场", "需求对接", "汇报", "自驱", "抗压", "学习能力",
    # —— 合规 ——
    "合规", "监管", "数据安全", "隐私保护", "个保法", "数据安全法", "备案",
    "算法备案", "等保", "信创", "国产化",
    # —— 工具与工程 ——
    "Cursor", "Claude Code", "Copilot", "Vibe Coding", "AI编程", "AI 编程",
    "低代码", "GitHub", "Git", "Python", "SQL", "API", "SDK", "工程化", "架构",
]


def load_docs():
    docs = []
    for tier in ("green", "yellow", "red"):
        d = POOL / tier
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            raw = f.read_text(encoding="utf-8", errors="replace")
            m = EXCERPT_PAT.search(raw)
            if not m:
                continue
            # 剔除【人工复核备注】：那是 AI 批注，不是 JD 原文（某证券「5-10年」教训）
            t = m.group(1).split("【人工复核备注】")[0]
            # 剔除采集元数据：URL、来源/链接/采集日行——它们是采集产物不是岗位要求
            t = re.sub(r"https?://\S+", " ", t)
            t = re.sub(r"^\s*[-*>]?\s*(来源|链接|采集日期|投递入口|岗位|公司)\s*[:：].*$", " ", t, flags=re.M)
            docs.append((f"{tier}/{f.name}", t))
    return docs


def mine_dict(docs, min_df=2):
    """A 轨：词典匹配。结果 100% 是可用的词（词典已保证），只统计真实频次。"""
    rows = []
    for w in DICT:
        hit_docs = [n for n, t in docs if re.search(re.escape(w), t, re.IGNORECASE)]
        tf = sum(len(re.findall(re.escape(w), t, re.IGNORECASE)) for _, t in docs)
        if len(hit_docs) >= min_df:
            rows.append((w, len(hit_docs), tf))
    rows.sort(key=lambda x: (-x[1], -x[2]))
    return rows


def mine(docs, min_df=3, top=40, min_tf=4):
    df = defaultdict(set)    # 词 → 出现在哪些 JD
    tf = defaultdict(int)    # 词 → 总次数
    right = defaultdict(set)  # 词 → 右邻字集合（用于碎片识别）
    for name, text in docs:
        cands = set()
        for m in CN_PAT.finditer(text):
            s = m.group(0)
            for n in (2, 3, 4, 5, 6):       # 2-6 字 n-gram
                for i in range(len(s) - n + 1):
                    w = s[i:i + n]
                    cands.add(w)
                    if i + n < len(s):
                        right[w].add(s[i + n])
        for m in EN_PAT.finditer(text):
            cands.add(m.group(0).strip(".-"))
        for c in cands:
            if c in STOP or len(c) < 2:
                continue
            df[c].add(name)
            tf[c] += 1
    # 右邻字熵过滤：真实词能接多种字（「大模型」后可接 应用/的/技术/能力…），
    # 而 n-gram 碎片的右邻是固定的——「产品经」后面永远跟着「理」，说明它只是「产品经理」的腰斩。
    # 英文/缩写词不适用此规则（「Agent」右邻几乎总是「的」），只过滤中文。
    def is_cn(w):
        return bool(re.fullmatch(r"[\u4e00-\u9fff]{2,6}", w))
    items = [(w, len(s), tf[w]) for w, s in df.items()
             if len(s) >= min_df and tf[w] >= min_tf
             and (not is_cn(w) or len(right[w]) >= 2)]
    # 子串抑制：若 A 是 B 的子串且 A 的 DF 不显著更高（<1.5倍），丢掉短的 A
    items.sort(key=lambda x: (-x[1], -len(x[0])))
    kept = []
    for w, d, t in items:
        dominated = False
        for w2, d2, _ in kept:
            if w in w2 and d < d2 * 1.5:
                dominated = True
                break
        if not dominated:
            kept.append((w, d, t))
    return kept[:top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--df", type=int, default=3, help="文档频次阈值：至少在几份 JD 中出现（默认 3）")
    ap.add_argument("--top", type=int, default=40, help="输出条数")
    ap.add_argument("--save", action="store_true", help="落盘 jd-pool/KEYWORD_MINING.md")
    args = ap.parse_args()

    docs = load_docs()
    if not docs:
        print("❌ 池子里没有可解析的 JD 原文")
        sys.exit(2)
    kept = mine(docs, args.df, args.top)

    try:
        import match_jd as M
        covered = {p: c for c, pats in M.SOFT_KEYWORDS.items() for p in pats}
    except Exception:
        covered = {}

    def is_covered(w):
        for p, c in covered.items():
            if re.search(p, w, re.IGNORECASE):
                return c
        return ""

    a_rows = mine_dict(docs, min_df=args.df)
    a_covered, a_uncovered = [], []
    for w, d, t in a_rows:
        c = is_covered(w)
        (a_covered if c else a_uncovered).append((w, d, t))

    lines = [
        "# JD 池关键词挖掘（上海 AI PM 岗真实高频要求）",
        "",
        f"> 生成方式：`python scripts/mine_keywords.py --df {args.df} --top {args.top}`",
        f"> 语料：{len(docs)} 份 JD 的原文摘录（green + yellow + red），已剔除 URL / 采集元数据 / 人工批注",
        "> **A 轨（词典匹配）**：用 AI PM 领域术语词典匹配统计，结果可靠，是决策依据。",
        "> **B 轨（n-gram 探索）**：纯统计挖词，能发现词典外的新词，但会产出「产品的」这类碎片，**只作线索，需人工判断**。",
        "",
        "## A 轨：术语词典命中（按出现 JD 数排序）",
        "",
        f"词典 {len(DICT)} 词，命中 ≥{args.df} 份 JD 的共 {len(a_rows)} 个：",
        "",
        "| # | 术语 | 出现JD数 | 总次数 | 现有词表覆盖 |",
        "|---|---|---|---|---|",
    ]
    for i, (w, d, t) in enumerate(a_rows, 1):
        c = is_covered(w)
        lines.append(f"| {i} | **{w}** | {d} | {t} | {c or '⚠️ **未覆盖**'} |")

    # 反向信号：词典里 JD 几乎不提的词
    cold = [w for w in DICT if w not in {r[0] for r in a_rows}]
    lines += ["", f"### 反向信号：词典里有但 JD 几乎不提（{len(cold)} 词）", ""]
    if cold:
        lines.append("这些词我们准备好了，但市场（本池 35 份 JD）基本不要求——")
        lines.append("**不值得为它们花组版篇幅**，除非某个特定 JD 明确提到。")
        lines.append("")
        lines.append("、".join(f"`{w}`" for w in cold))

    lines += ["", f"## 未覆盖高频词（{len(a_uncovered)} 个，来自 A 轨）", ""]
    if a_uncovered:
        lines.append("这些词在多份 JD 中反复出现，但 `match_jd.py` 的 `SOFT_KEYWORDS` 没覆盖——")
        lines.append("意味着**简历即便完全命中也拿不到分**，且我们不知道自己在这项上是盲区。")
        lines.append("")
        lines.append("| 术语 | 出现JD数 | 总次数 |")
        lines.append("|---|---|---|")
        for w, d, t in a_uncovered:
            lines.append(f"| `{w}` | {d} | {t} |")
        lines += ["", "处理方式（**人工判断，脚本不自动改词表**）：",
                  "1. 若是真实能力要求 → 加进 `SOFT_KEYWORDS` 对应类目；",
                  "2. 若简历确实有证据 → 组版时把该词写进去；",
                  "3. 若确实没有 → 承认是真缺口，面试用诚实说法兜底。"]
    else:
        lines.append("_A 轨全部已被现有词表覆盖。_")

    lines += ["", f"## B 轨：n-gram 探索（Top {args.top}，需人工判断）", ""]
    lines.append("| # | 候选词 | 出现JD数 | 总次数 | 现有词表覆盖 |")
    lines.append("|---|---|---|---|---|")
    for i, (w, d, t) in enumerate(kept, 1):
        c = is_covered(w)
        lines.append(f"| {i} | {w} | {d} | {t} | {c or '⚠️ 未覆盖'} |")
    lines.append("")
    lines.append("> B 轨看的是「A 轨词典里没有、但真实 JD 反复出现」的词——**先人工判是否是词，再决定是否进词典**。")

    out = "\n".join(lines)
    if args.save:
        (POOL / "KEYWORD_MINING.md").write_text(out + "\n", encoding="utf-8")
        print(f"💾 已写入 {POOL/'KEYWORD_MINING.md'}")
    print(out)
    print(f"\n统计：{len(docs)} 份 JD ｜ A 轨命中 {len(a_rows)} 词 ｜ 其中未覆盖 {len(a_uncovered)} 个 ｜ 词典冷词 {len(cold)} 个")


if __name__ == "__main__":
    main()
