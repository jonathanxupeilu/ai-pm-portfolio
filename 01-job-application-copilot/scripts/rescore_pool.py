# -*- coding: utf-8 -*-
"""
rescore_pool.py — 用当前规则批量重跑全池打分（打分规则升级后的对账工具）
==========================================================================
用法：
    python rescore_pool.py                    # dry-run：只出对账报告，不写文件
    python rescore_pool.py --apply            # 写回（仅更新元数据与匹配段落）
    python rescore_pool.py --resume <路径>    # 指定双向匹配的简历（默认 auto=baseline_v4.md）

为什么需要它：
    打分规则一旦升级（如 2026-09-10 从「单向只看 JD」升级为「双向比对简历+事实库」），
    池子里存量 JD 的分数仍是旧规则算的。**新旧分混排 = 排序无意义**，
    必须一次性全量重跑，让所有岗位回到同一把尺子下。

安全设计（针对「改一个地方不能破坏另一个地方」）：
  1. 默认 dry-run，必须显式 --apply 才写文件
  2. 人工改判保护：META 含 manual=1 的文件**保留其人工判定的档位**（人工判断优先于规则），
     但分数与双向数据照常重算——保护的是档位不是分数，否则它们会留着旧口径分数，
     和全池其他岗位不同尺子，排序照样失真
  3. **不重判硬门槛**（2026-09-10 定的关键约束）：档位沿用旧值，base 分也用旧档位算。
     理由（实测踩出来的）：存量 JD 的「原文摘录」里混进了 AI 批注——某证券机构那条的摘录里
     写着「40-70k 上海 5-10年 本科（列表页标注；任职要求写2年+AI证券产品经理经验）」，
     括号里是批注不是 JD 原文，重跑 hard_gate 会读到批注里的 5-10 年，把人工判定的 yellow
     误打成 red。硬门槛结论已有首次判定 + 人工复核背书，不该被存档污染推翻。
     → 重跑只统一「覆盖率」这把尺子；档位存疑时人工处理（--recheck-gate 可出参考意见）
  4. 写回采用「行级替换」而非整文件重写：只动匹配分/覆盖行与双向匹配段落，
     JD 原文摘录、人工批注、链接一律原样保留

铁律：重跑是为了让尺子统一，不是为了把分数跑高。分数变低必须如实呈现。
"""
import re
import sys
import argparse
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import match_jd as M  # noqa: E402

SKILL = pathlib.Path(__file__).resolve().parent.parent
POOL = SKILL / "jd-pool"

META_PAT = re.compile(r"<!--\s*MATCHMETA\s+(.*?)\s*-->", re.S)
EXCERPT_PAT = re.compile(r"##\s*JD\s*原文[^\n]*\n\s*```[^\n]*\n(.*?)\n\s*```", re.S)


def parse_meta(s: str) -> dict:
    m = META_PAT.search(s)
    if not m:
        return {}
    d = {}
    for kv in re.finditer(r"(\w+)=(.*?)(?=\s+\w+=|$)", m.group(1)):
        d[kv.group(1)] = kv.group(2).strip()
    return d


def render_meta(d: dict) -> str:
    order = ["tier", "score", "coverage", "company", "title", "date",
             "source", "url", "purpose", "mode", "hit", "fill", "gap", "must",
             "truncated", "patched", "manual", "status"]
    parts = [f"{k}={d[k]}" for k in order if k in d]
    parts += [f"{k}={v}" for k, v in d.items() if k not in order]
    return f"<!-- MATCHMETA {' '.join(parts)} -->"


def rebuild_body(old: str, meta: dict, new: dict) -> str:
    """行级替换：只更新分数相关行 + 双向匹配段落，其余原样保留。"""
    s = old
    s = re.sub(r"- 匹配分：\*\*[^*]*\*\*（[^\n]*）",
               f"- 匹配分：**{new['score']}**（档位基础分 {new['base']} + 双向覆盖率 × {int(M.COVERAGE_WEIGHT)}）", s, count=1)
    s = re.sub(r"- (?:软匹配|关键词)覆盖：[^\n]*",
               f"- 关键词覆盖：{new['coverage']}（格式：已命中+可补缺口 / JD提及类目·总类目）", s, count=1)

    block = [
        "",
        "## 双向匹配结果（JD ∧ 简历 ∧ 事实库）",
        f"- 打分模式：`bi`（重跑于 {new['today']}；简历 {new['resume_name']} + 事实库 career-facts）",
        f"- ✅ **已命中 {new['n_hit']}**：" + ("、".join(c for c, _ in new['hits']) or "无"),
        f"- 🔧 **可补缺口 {new['n_fill']}**：" + ("、".join(c for c, _ in new['fillable']) or "无"),
        f"- ⬜ **真缺口 {new['n_gap']}**：" + ("、".join(c for c, _ in new['gaps']) or "无"),
    ]
    if new['fillable']:
        block += ["", "**组版动作**：以下项事实库有证据，组版时必须补进简历——"]
        block += [f"- [ ] {c}（补进技能区或项目描述，不得编造新事实）" for c, _ in new['fillable']]
    if new['gaps']:
        block += ["", "**不补项**：以下项事实库无证据，组版时不补，面试用诚实说法兜底——"]
        block += [f"- {c}" for c, _ in new['gaps']]
    # 技能点层级分析：与 match_jd 共用渲染器，简历/规则一变即同步刷新
    block += M.render_atom_section(new['lst'], truncated=(new.get('trunc') == "1"))

    # 替换已有双向匹配段落；没有则插到「JD 原文摘录」之前
    pat = re.compile(r"\n## 双向匹配结果[^\n]*\n(?:.*?)(?=\n## JD 原文|\Z)", re.S)
    if pat.search(s):
        s = pat.sub("\n" + "\n".join(block) + "\n", s, count=1)
    else:
        s = re.sub(r"\n(?=## JD 原文)", "\n" + "\n".join(block) + "\n\n", s, count=1)
    return s


def main():
    import datetime
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写回文件（默认 dry-run 只出报告）")
    ap.add_argument("--resume", default="auto", help="双向匹配用简历：auto 或路径")
    ap.add_argument("--facts-dir", default=str(M.FACTS_DIR))
    ap.add_argument("--recheck-gate", action="store_true",
                    help="附带输出「按当前规则重判档位」的参考意见（仅供人工参考，不改变分数与档位）")
    args = ap.parse_args()

    resume_path = M.DEFAULT_RESUME if args.resume == "auto" else pathlib.Path(args.resume)
    if not resume_path.exists():
        print(f"❌ 简历文件不存在：{resume_path}")
        sys.exit(2)
    resume_text, facts_text = M.load_corpus(resume_path, args.facts_dir)
    today = str(datetime.date.today())

    files = sorted([f for t in ("green", "yellow", "red") for f in (POOL / t).glob("*.md")])
    rows, skipped, nometa, tier_change = [], [], [], []

    for f in files:
        raw = f.read_text(encoding="utf-8", errors="replace")
        meta = parse_meta(raw)
        if not meta:
            nometa.append(f)
            continue
        manual = meta.get("manual") == "1"   # 只保留档位，分数照常重算
        exc = EXCERPT_PAT.search(raw)
        if not exc:
            skipped.append((f, meta))
            continue
        # 打分剔除【人工复核备注】：那是 AI 批注，不是 JD 原文
        text = exc.group(1).split("【人工复核备注】")[0]
        # 完整性闸门**重判**（2026-09-10）：必须基于当前正文重新过闸。
        # 补录完只把 truncated 改回 0 是不够的——那等于补录脚本给自己发合格证，
        # 得让闸门自己宣布「现在完整了」才算真修好。
        comp_level, _ = M.completeness_check(text)
        trunc_flag = ("1" if comp_level == "truncated" else "suspect") if comp_level else "0"

        hits, fillable, gaps = M.bidirectional_match(text, resume_text, facts_text)
        # P1 必备层覆盖 + 技能点分流（与 match_jd 同一套函数，口径不会漂）
        lst = M.layer_stats(text, resume_text, facts_text)
        old_score = float(meta.get("score", 0))
        old_tier = meta.get("tier", "?")
        # 档位沿用旧值：硬门槛不重判（见文件头说明），base 也按旧档位取
        eff_tier = old_tier if old_tier in ("green", "yellow", "red") else "green"
        # 权重唯一真源在 match_jd.TIER_BASE / COVERAGE_WEIGHT，勿在此重复定义
        base = M.TIER_BASE[eff_tier]
        n_hit, n_fill, n_gap = len(hits), len(fillable), len(gaps)
        n_jd = n_hit + n_fill + n_gap
        score = round(base + ((n_hit + n_fill * 0.5) / len(M.SOFT_KEYWORDS)) * M.COVERAGE_WEIGHT, 1)
        coverage = f"{n_hit}+{n_fill}/{n_jd}·{len(M.SOFT_KEYWORDS)}"

        new_tier = eff_tier
        if args.recheck_gate:
            gate, _ = M.hard_gate(text)
            new_tier = gate or "green"
            if new_tier != eff_tier:
                tier_change.append((f, eff_tier, new_tier))

        new = {"score": score, "base": base, "coverage": coverage, "today": today,
               "resume_name": resume_path.name, "n_hit": n_hit, "n_fill": n_fill,
               "n_gap": n_gap, "hits": hits, "fillable": fillable, "gaps": gaps,
               "lst": lst, "trunc": trunc_flag}
        rows.append({"f": f, "meta": meta, "old": old_score, "new": score,
                     "old_tier": old_tier, "new_tier": new_tier, "eff_tier": eff_tier,
                     "n_hit": n_hit, "n_fill": n_fill, "n_gap": n_gap, "coverage": coverage,
                     "manual": manual, "trunc": trunc_flag})

        if args.apply:
            meta.update({"score": score, "coverage": coverage, "tier": eff_tier,
                         "mode": "bi", "hit": n_hit, "fill": n_fill, "gap": n_gap,
                         "must": M.fmt_must(lst),
                         "truncated": trunc_flag})
            out = META_PAT.sub(render_meta(meta), raw, count=1)
            out = rebuild_body(out, meta, new)
            f.write_text(out, encoding="utf-8")

    rows.sort(key=lambda r: r["old"] - r["new"])  # 降幅最大的排前面
    print("=" * 78)
    print(f"全池重跑对账 ｜ {len(files)} 个 JD ｜ 模式：{'APPLY 已写回' if args.apply else 'DRY-RUN 未写入'}")
    print("=" * 78)
    print(f"\n重算 {len(rows)} ｜ 跳过 {len(skipped)}（人工改判/无原文） ｜ 缺 META {len(nometa)}")
    print(f"简历：{resume_path.name} ｜ 事实库：{pathlib.Path(args.facts_dir).name}\n")
    print(f"{'公司/岗位':<34}{'旧分':>7}{'新分':>7}{'变化':>8}  命中/可补/真缺")
    print("-" * 78)
    for r in rows:
        m = r["meta"]
        delta = r["new"] - r["old"]
        sign = f"{delta:+.1f}"
        name = f"{m.get('company','?')}/{m.get('title','?')}"[:30] + (" 🅜" if r["manual"] else "")
        print(f"{name:<34}{r['old']:>7.1f}{r['new']:>7.1f}{sign:>8}  {r['n_hit']}/{r['n_fill']}/{r['n_gap']}")

    if tier_change:
        print(f"\n⚠️ 档位参考意见（--recheck-gate，{len(tier_change)} 个）——**未改分数、未改档位、未移动文件**：")
        print("   注意：重判会读到摘录里混入的 AI 批注（如某证券的「5-10年」实为列表页标注），仅供人工复核参考。")
        for f, o, n in tier_change:
            print(f"  - {f.parent.name}/{f.name}：{o} → {n}")
    n_manual = sum(1 for r in rows if r["manual"])
    if n_manual:
        print(f"\n🅜 人工档位保留（{n_manual} 个）：档位不自动重判，分数与双向数据已按新口径重算。")
    n_trunc = sum(1 for r in rows if r["trunc"] == "1")
    n_susp = sum(1 for r in rows if r["trunc"] == "suspect")
    if n_trunc or n_susp:
        print(f"\n⚠️ JD 完整性：残缺 {n_trunc} 个 ｜ 疑似残缺 {n_susp} 个")
        print("   这些岗位的「可补 / 真缺」缺口数是**未探明**，不是「0 缺口」——")
        print("   源文本缺了大半，探不到缺口不等于没有缺口。")
        for r in rows:
            if r["trunc"] != "0":
                icon = "⚠️" if r["trunc"] == "1" else "❓"
                print(f"     {icon} {r['f'].parent.name}/{r['f'].name}")
        print("   批量补录：python scripts/patch_jd.py --refetch-all")
    if skipped:
        print(f"\n⏭️ 跳过（无 JD 原文摘录，无法重算）：")
        for f, m in skipped:
            print(f"  - {f.parent.name}/{f.name}")
    if nometa:
        print(f"\n❓ 缺 MATCHMETA（需重跑 match_jd.py --save 补分）：")
        for f in nometa:
            print(f"  - {f.parent.name}/{f.name}")

    up = sum(1 for r in rows if r["new"] > r["old"])
    down = sum(1 for r in rows if r["new"] < r["old"])
    print(f"\n统计：升 {up} ｜ 降 {down} ｜ 持平 {len(rows)-up-down}")
    print("说明：双向分普遍低于旧分属正常——旧分统计的是「JD 写得多全」，新分统计的是「你命中多少」。")
    if not args.apply:
        print("\n💡 确认无误后加 --apply 写回，再跑 rank_pool.py 刷新排序。")
    sys.exit(0)


if __name__ == "__main__":
    main()
