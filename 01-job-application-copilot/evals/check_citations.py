#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
P-01 投研系统 AI 效果评测 · 维度 2 溯源自动校验 + 维度 1 待核清单生成 + 打分汇总

对应方案：references/评测集补建方案_投研系统.md（v2 校准版）
  - 维度 1 事实忠实度：一票否决，**必须人工核**（金融领域 LLM-as-judge 有 domain blindness）
  - 维度 2 溯源有效性：确定性指标，可脚本自动校验（FutureAGI 2026）
  - 维度 3 / 4 框架完整与决策可用：人工判

三个子命令：
  1) check    维度 2：校验输出里的每个引用能否定位到事实卡
  2) stmt     维度 1 辅助：把输出拆成待核陈述清单（含数字/标的名的优先），人工逐条标 有支撑/无支撑
  3) summary  汇总 scores_v1.csv：合格率 + 各维度失分分布（不记总分，看失分分布）

用法：
  python check_citations.py check   --output 输出.txt --context 事实卡.txt [--cite-regex "..."]
  python check_citations.py stmt    --output 输出.txt --out 待核清单.md
  python check_citations.py summary --scores scores_v1.csv

事实卡文件格式（--context）：每行一条，推荐 `来源ID | 内容`，也支持 `来源ID\\t内容`。
  若为纯文本（无分隔符），则整行作为内容，仅做子串匹配。
"""

import argparse
import csv
import pathlib
import re
import sys

# ---------------------------------------------------------------- 引用抽取
# 默认支持的引用格式；若你的系统用了别的格式，用 --cite-regex 覆盖
# （正则需含一个捕获组，捕获"来源标识"）
CITE_PATTERNS = [
    r"【\s*来源\s*[:：]\s*([^】\n]+?)\s*】",   # 【来源：xxx】
    r"\[\s*来源\s*[:：]\s*([^\]\n]+?)\s*\]",  # [来源: xxx]
    r"[（(]\s*来源\s*[:：]\s*([^）)\n]+?)\s*[）)]",  # （来源：xxx）
    r"来源\s*[:：]\s*([^。；;\n】\]）)]+)",  # 来源：xxx（裸写，排除结束符防止过度捕获）
    r"[Ss]ource\s*[:：]\s*([^。；;\n】\]\)]+)",  # source: xxx
]

# 捕获值尾部需要剥掉的字符（防止把结束符算进来源名）
STRIP_TAIL = "。，、；;：:）)】] \t"

# 无法定位的"伪来源"——出现即 fail（哪怕格式上像引用）
VAGUE_SOURCES = [
    "公开资料", "公开信息", "网络", "网上", "相关资料", "据公开", "一般来说",
    "市场数据", "行业数据", "通常", "众所周知", "研究表明", "有资料显示",
]

# 需要人工核的陈述特征：含数字 / 百分号 / 金额 / 年份 / 标的名
STMT_SPLIT = re.compile(r"(?<=[。！？；\n])")
NUM_PAT = re.compile(r"\d")
CODE_PAT = re.compile(r"\b\d{6}\b")  # A 股六位代码


def load_context(path):
    """返回 (ids:set, contents:list[str])"""
    ids, contents = set(), []
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            head, body = line.split("|", 1)
        elif "\t" in line:
            head, body = line.split("\t", 1)
        else:
            head, body = None, line
        if head and head.strip():
            ids.add(head.strip())
        contents.append(body.strip())
    return ids, contents


def extract_cites(text, extra_regex=None):
    """抽取引用，返回 [(原文, 来源值)]。

    注意：多个正则可能命中同一处（如【来源：X】既被【】规则命中、也被裸写规则命中），
    必须按匹配区间去重——否则同一引用被计两次，后一次因捕获到多余字符而误判 fail。
    """
    pats = [re.compile(p) for p in CITE_PATTERNS]
    if extra_regex:
        pats.insert(0, re.compile(extra_regex))

    hits = []  # (start, end, raw, val)
    for p in pats:
        for m in p.finditer(text):
            hits.append((m.start(), m.end(), m.group(0), m.group(1).strip(STRIP_TAIL)))

    # 短匹配优先（更精确），同起点时取更长？—不，取**更短**的（结束符更少）
    hits.sort(key=lambda h: (h[0], h[1] - h[0]))

    kept, out = [], []
    for start, end, raw, val in hits:
        if not val:
            continue
        # 与已保留区间重叠 → 视为同一引用的重复命中，跳过
        if any(not (end <= ks or start >= ke) for ks, ke in kept):
            continue
        kept.append((start, end))
        out.append((raw, val))

    # 来源值去重保序
    seen, uniq = set(), []
    for raw, val in out:
        if val not in seen:
            seen.add(val)
            uniq.append((raw, val))
    return uniq


def locate(val, ids, contents):
    """判断引用能否定位：ID 精确匹配 / 内容子串匹配"""
    if val in ids:
        return True, "ID 精确匹配"
    for c in contents:
        if val and (val in c or c in val):
            return True, "内容子串匹配"
    return False, "无法定位"


def cmd_check(args):
    text = pathlib.Path(args.output).read_text(encoding="utf-8")
    ids, contents = load_context(args.context)
    cites = extract_cites(text, args.cite_regex)

    print("=" * 62)
    print(f"维度 2 · 溯源有效性校验 ｜ {pathlib.Path(args.output).name}")
    print("=" * 62)
    print(f"事实卡：{len(ids)} 个 ID / {len(contents)} 条内容")
    print(f"抽取到引用：{len(cites)} 条\n")

    if not cites:
        print("⚠️ 未抽取到任何引用标记。两种可能：")
        print("   ① 系统输出确实没标来源 → 维度 2 fail")
        print("   ② 引用格式不在默认 5 种里 → 用 --cite-regex '你的正则' 指定（须含 1 个捕获组）")
        print("\n判定：❌ fail（无引用可校验）")
        return 1

    ok, bad = [], []
    for raw, val in cites:
        is_vague = any(v in val for v in VAGUE_SOURCES)
        found, how = locate(val, ids, contents)
        if is_vague:
            bad.append((raw, val, "伪来源（无法定位到具体数据源）"))
        elif not found:
            bad.append((raw, val, how))
        else:
            ok.append((raw, val, how))

    for raw, val, how in ok:
        print(f"  ✅ {raw}  → {how}")
    for raw, val, why in bad:
        print(f"  ❌ {raw}  → {why}")

    print()
    if bad:
        print(f"判定：❌ fail（{len(bad)}/{len(cites)} 条引用无法定位到具体数据源）")
        print("   → 按方案：出现「据公开资料」这类无法定位的来源即 fail")
        return 1
    print(f"判定：✅ pass（{len(ok)}/{len(cites)} 条引用均可定位）")
    return 0


def cmd_stmt(args):
    text = pathlib.Path(args.output).read_text(encoding="utf-8")
    stmts = [s.strip() for s in STMT_SPLIT.split(text) if s.strip()]
    # 优先排：含数字 / 代码的陈述（这些才是编造高发区）
    scored = []
    for s in stmts:
        w = 0
        if NUM_PAT.search(s):
            w += 2
        if CODE_PAT.search(s):
            w += 1
        if "%" in s or "％" in s:
            w += 1
        scored.append((w, s))
    scored.sort(key=lambda x: -x[0])

    lines = [
        f"# 维度 1 待核陈述清单 ｜ {pathlib.Path(args.output).name}",
        "",
        "> 用法：逐条核对**该陈述能否在事实卡/检索上下文中找到支撑**，在下表「有支撑」列填 Y/N。",
        "> 忠实度 = 有支撑陈述数 / 总陈述数；**出现任何编造数字 → 一票否决，整条不合格**。",
        "> 按编造风险降序排列（含数字/代码的在前），时间不够可只核前若干条——但**一票否决项不可跳过**。",
        "",
        "| # | 陈述 | 有支撑(Y/N) | 支撑来源 | 备注 |",
        "|---|---|---|---|---|",
    ]
    for i, (w, s) in enumerate(scored, 1):
        flag = "🔴 高风险" if w >= 3 else ("🟡 中" if w >= 1 else "⚪ 低")
        lines.append(f"| {i} | {s} |  |  | {flag} |")
    lines += ["", f"共 {len(scored)} 条陈述。", ""]

    out = pathlib.Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"✅ 待核清单已生成：{out}（{len(scored)} 条陈述）")
    high = sum(1 for w, _ in scored if w >= 3)
    print(f"   其中高风险（含数字/代码/百分比）{high} 条——优先核这些")
    return 0


def cmd_summary(args):
    rows = list(csv.DictReader(pathlib.Path(args.scores).open(encoding="utf-8-sig")))
    if not rows:
        print("❌ 打分表为空")
        return 1
    dims = ["dim1_忠实度", "dim2_溯源", "dim3_框架", "dim4_决策"]
    print("=" * 62)
    print(f"评测汇总 ｜ {pathlib.Path(args.scores).name}")
    print("=" * 62)

    total = len(rows)
    passed = 0
    fail_cnt = {d: 0 for d in dims}
    per_cat = {}
    for r in rows:
        vals = {}
        for d in dims:
            v = (r.get(d) or "").strip().upper()
            vals[d] = v
            if v.startswith("F") or v == "0" or v == "N" or "不通过" in v or "✗" in v or "❌" in v:
                fail_cnt[d] += 1
        # 通过 = 4 维全 pass（空值视为未判）
        judged = [d for d in dims if vals[d]]
        ok = judged and all(
            not (vals[d].startswith("F") or vals[d] == "0" or vals[d] == "N"
                 or "不通过" in vals[d] or "✗" in vals[d] or "❌" in vals[d])
            for d in judged)
        if ok and len(judged) == 4:
            passed += 1
        # 分类合格率：分母只算「4 维判完」的行，未判完的不计入（否则会虚低）
        if len(judged) == 4:
            cat = (r.get("类别") or "").strip()[:2]
            per_cat.setdefault(cat, [0, 0])
            per_cat[cat][1] += 1
            if ok:
                per_cat[cat][0] += 1

    judged_all = sum(1 for r in rows if all((r.get(d) or "").strip() for d in dims))
    print(f"总题数 {total} ｜ 已判完 {judged_all} ｜ 通过 {passed}")
    if judged_all:
        print(f"合格率：**{passed / judged_all * 100:.1f}%**（分母=已判完，未判的不计入）")
    print()
    print("各维度失分分布（不是总分，直接指向该改哪里）：")
    for d in dims:
        n = fail_cnt[d]
        bar = "█" * n
        print(f"  {d:<12} 失分 {n:>2}  {bar}")
    print()
    print("分类合格率（分母 = 该类已判完的题数）：")
    for cat in sorted(per_cat):
        p, t = per_cat[cat]
        print(f"  {cat} 类  {p}/{t}" + (f"  ({p / t * 100:.0f}%)" if t else ""))
    print()
    print("⚠️ 规模提醒：30 条 < 50 条建议下限，单条失败移动合格率 ≥2pp。对外只说「30 条起步集」。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="P-01 评测：溯源校验 / 待核清单 / 打分汇总")
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("check", help="维度2：校验引用能否定位到事实卡")
    c.add_argument("--output", required=True, help="系统输出文本文件")
    c.add_argument("--context", required=True, help="事实卡文件（ID | 内容）")
    c.add_argument("--cite-regex", default=None, help="自定义引用正则（须含1个捕获组）")
    c.set_defaults(func=cmd_check)

    s = sub.add_parser("stmt", help="维度1辅助：生成待核陈述清单")
    s.add_argument("--output", required=True)
    s.add_argument("--out", default="待核清单.md")
    s.set_defaults(func=cmd_stmt)

    m = sub.add_parser("summary", help="汇总打分表：合格率 + 失分分布")
    m.add_argument("--scores", default="scores_v1.csv")
    m.set_defaults(func=cmd_summary)

    args = ap.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
