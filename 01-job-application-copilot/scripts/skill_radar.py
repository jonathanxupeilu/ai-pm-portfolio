#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""能力雷达：把全池 JD 的技能点聚合成「该学什么 / 该讲清楚什么 / 该用什么覆盖」。

为什么需要它：单份 JD 的缺口是个例（这份要 RLHF，可能只是这家特殊）；
**N 份 JD 重复出现的缺口才是市场信号**。学习优先级应该由市场频次决定，
而不是「我觉得哪个顺眼就学哪个」。

三路分流的依据是「补它的成本」：
  learn  可短周期补的技术栈   → 学（学完能写进简历，可验证）
  drill  面试高频概念         → 讲清楚（不必精通，但要能应对追问）
  cover  硬资质/经验门槛      → 补不了，改用作品集 + 迁移经验覆盖
  ''     通用产品能力         → 与 learn 同路，但优先级看公司类型

用法：
  python scripts/skill_radar.py                     # 生成 jd-pool/SKILL_RADAR.md
  python scripts/skill_radar.py --min-jd 2          # 只看被 ≥2 份 JD 提及的
  python scripts/skill_radar.py --feed-coach        # 额外导出面试薄弱点条目
  python scripts/skill_radar.py --print             # 同时打到终端
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import match_jd as M  # noqa: E402

SKILL = Path(__file__).resolve().parent.parent
POOL = SKILL / "jd-pool"
OUT = POOL / "SKILL_RADAR.md"
COACH_OUT = POOL / "coach_feed.md"

TIER_MARK = {"green": "🟢", "yellow": "🟡", "red": "🔴"}
LEVEL_ORDER = ("must", "pref", "plus")
# 需求强度：必备层提及份数权重最高。选 3/2/1 是因为「必备」对简历的约束力
# 大致是「优先」的 1.5 倍、「加分项」的 3 倍——避免加分项堆量把优先级顶上去。
DEMAND_W = {"must": 3, "pref": 2, "plus": 1}


def scan(pool: Path):
    """扫描全池，返回 (技能点聚合表, 元信息)。"""
    files = []
    for tier in ("green", "yellow", "red"):
        files += [(tier, f) for f in sorted((pool / tier).glob("*.md"))]

    atoms = {}       # atom -> {jd, levels, evidence, tiers, cov}
    meta = {"n_jd": 0, "tiers": {"green": 0, "yellow": 0, "red": 0}, "trunc": []}

    for tier, f in files:
        raw = f.read_text(encoding="utf-8", errors="replace")
        body = M.jd_body(raw)
        if not body.strip():
            continue
        meta["n_jd"] += 1
        meta["tiers"][tier] += 1
        lvl, _ = M.completeness_check(raw)
        if lvl:
            meta["trunc"].append((tier, f.name, lvl))
        company = ""
        mt = re.search(r"company=([^\s]+)", raw)
        if mt:
            company = mt.group(1)

        for atom, rec in M.extract_atoms(raw).items():
            e = atoms.setdefault(atom, {"jd": 0, "levels": {k: 0 for k in LEVEL_ORDER},
                                        "tiers": {k: 0 for k in ("green", "yellow", "red")},
                                        "evidence": [], "route": rec["route"]})
            e["jd"] += 1
            e["tiers"][tier] += 1
            for lv in rec["levels"]:
                e["levels"][lv] += 1
            if len(e["evidence"]) < 3 and rec["evidence"]:
                ev = rec["evidence"][0]
                e["evidence"].append((company or f.stem[:12], rec["top"], ev[:88]))

    resume_text, facts_text = M.load_corpus(M.DEFAULT_RESUME)
    for atom, e in atoms.items():
        e["cov"] = M.atom_coverage(atom, resume_text, facts_text)
        e["demand"] = sum(e["levels"][k] * DEMAND_W[k] for k in LEVEL_ORDER)
        e["top"] = max((k for k in LEVEL_ORDER if e["levels"][k]), key=lambda k: M.ATOM_RANK[k])
    return atoms, meta


def _tier_badge(e):
    return "".join(TIER_MARK[t] + str(e["tiers"][t]) for t in ("green", "yellow", "red") if e["tiers"][t])


def _level_badge(e):
    return "/".join(str(e["levels"][k]) for k in LEVEL_ORDER)


def _rows(items, show_evidence=True):
    """渲染一张技能点表。items 需已按优先级排好序。"""
    out = ["| 优先级 | 技能点 | 必备/优先/加分 | JD 档位分布 | 出处（公司 · 层级） |",
           "|---:|---|---|---|---|"]
    for e, name in items:
        ev = "；".join(f"{c}·{M.LEVEL_CN[lv]}" for c, lv, _ in e["evidence"][:2]) if show_evidence else "—"
        out.append(f"| {e['demand']} | {name} | {_level_badge(e)} | {_tier_badge(e)} | {ev or '—'} |")
    return out if len(out) > 2 else ["（无）"]


def render(atoms, meta, min_jd):
    keep = {a: e for a, e in atoms.items() if e["jd"] >= min_jd}
    by_demand = sorted(keep.items(), key=lambda kv: (-kv[1]["demand"], -kv[1]["jd"], kv[0]))

    def take(*preds):
        return [(e, a) for a, e in by_demand if all(p(a, e) for p in preds)]

    is_bucket = lambda route: (lambda a, e: e["route"] == route)
    uncovered = lambda a, e: e["cov"] != "resume"
    facts = lambda a, e: e["cov"] == "facts"
    gap = lambda a, e: e["cov"] == "gap"

    lines = [
        "<!-- 由 scripts/skill_radar.py 生成，重跑会整体覆盖，请勿手改 -->",
        "# 能力雷达 · 市场需求视图",
        "",
        f"> 扫描 **{meta['n_jd']} 份 JD**（🟢{meta['tiers']['green']} / 🟡{meta['tiers']['yellow']} / "
        f"🔴{meta['tiers']['red']}），抽出 **{len(atoms)} 个技能点**，"
        f"生成日期 {date.today()}。",
        f"> 学习优先级 = 必备层份数×3 + 优先层×2 + 加分项×1 —— **由市场频次决定，不拍脑袋选课**。",
    ]
    if meta["trunc"]:
        lines.append(f"> ⚠️ 含 {len(meta['trunc'])} 份残缺 JD，其缺口是「未探明」而非「0」："
                     + "、".join(n for _, n, _ in meta["trunc"]))
    if min_jd > 1:
        lines.append(f"> 当前只显示被 **≥{min_jd} 份** JD 提及的技能点。")
    lines += ["", "---", ""]

    # ① 学习清单
    learn = take(is_bucket("learn"), uncovered)
    learn_facts = [(e, a) for e, a in learn if e["cov"] == "facts"]
    learn_gap = [(e, a) for e, a in learn if e["cov"] == "gap"]
    lines += [f"## ① 该学什么 · 可补技术栈（{len(learn)} 项）", ""]
    lines += [f"### 1.1 事实库有证据 → 补进简历就能拿分（{len(learn_facts)} 项）",
              "",
              "> 你不是不会，是简历没写。**改简历即可**，性价比最高，先做这批。",
              ""]
    lines += _rows(learn_facts)
    lines += ["", f"### 1.2 真缺口 → 需要真去学（{len(learn_gap)} 项）", "",
              "> 事实库里也确实没有。按表中顺序学，先学必备层高频的。", ""]
    lines += _rows(learn_gap)

    # ② 面试重点
    drill = take(is_bucket("drill"), uncovered)
    lines += ["", f"## ② 该讲清楚什么 · 面试高频概念（{len(drill)} 项）", "",
              "> 这些不必精通到能实现，但**必须能说清原理、取舍和你踩过的坑**——"
              "面试官问的是判断力，不是让你现场训模型。", ""]
    lines += _rows(drill)

    # ③ 硬资质
    cover = take(is_bucket("cover"), uncovered)
    lines += ["", f"## ③ 该用什么覆盖 · 硬资质/经验门槛（{len(cover)} 项）", "",
              "> 短期补不了（证书、年限、论文）。**不要在简历里假装有**，"
              "改用作品集 + 可迁移经验正面回应。", ""]
    lines += _rows(cover)

    # ④ 通用产品能力（未分流）
    generic = take(is_bucket(""), uncovered)
    lines += ["", f"## ④ 通用产品能力（{len(generic)} 项）", "",
              "> 产品通用项，按目标公司类型取舍；to B / 金融方向的公司权重更高。", ""]
    lines += _rows(generic)

    # ⑤ 已具备
    have = [(e, a) for a, e in by_demand if e["cov"] == "resume"]
    lines += ["", f"## ⑤ 已具备 · 市场认可你的这些（{len(have)} 项）", "",
              "> 出现在 JD 里、且你简历已覆盖 —— **这些是简历的弹药，按频次往前提**。", ""]
    lines += _rows(have, show_evidence=False)

    # 附录
    lines += ["", "---", "", "## 附录 · 全部技能点明细（含已覆盖）", ""]
    lines += ["| 优先级 | 技能点 | 覆盖面 | 必备/优先/加分 | JD 档位分布 | 分值依据（JD 原文摘录） |",
              "|---:|---|---|---|---|---|"]
    COV_CN = {"resume": "✅ 简历", "facts": "🔧 事实库", "gap": "⬜ 无"}
    for a, e in by_demand:
        src = "；".join(f"「{ev}」" for _, _, ev in e["evidence"][:1]) or "—"
        lines.append(f"| {e['demand']} | {a} | {COV_CN[e['cov']]} | {_level_badge(e)} | "
                     f"{_tier_badge(e)} | {src} |")

    lines += ["", "---", "",
              "重跑：`python scripts/skill_radar.py`　｜　导出面试条目：`python scripts/skill_radar.py --feed-coach`"]
    return "\n".join(lines) + "\n", keep


def render_coach(atoms, min_jd):
    """把 drill 类未覆盖项转成可直接粘进 ai-pm-interview-coach 的弱点条目。

    刻意**不直接写**面试教练的 weaknesses.md —— 跨技能写文件属于外部副作用，
    要在用户明确同意后由人来粘贴。
    """
    drill = sorted([(a, e) for a, e in atoms.items()
                    if e["route"] == "drill" and e["cov"] != "resume" and e["jd"] >= min_jd],
                   key=lambda kv: (-kv[1]["demand"], kv[0]))
    gaps = sorted([(a, e) for a, e in atoms.items()
                   if e["cov"] == "gap" and e["jd"] >= min_jd],
                  key=lambda kv: (-kv[1]["demand"], kv[0]))
    lines = ["<!-- 由 skill_radar.py --feed-coach 生成。请人工确认后粘进 "
             "ai-pm-interview-coach/progress/weaknesses.md -->",
             f"# 面试薄弱点候选（来自 JD 池，{date.today()}）",
             "",
             f"> 依据：{len(drill)} 个面试高频概念 + {len(gaps)} 个跨岗位真缺口，按市场频次排序。",
             "", "## 一、JD 高频概念（面试必被问）", ""]
    for a, e in drill:
        refs = "；".join(f"{c}·{M.LEVEL_CN[lv]}" for c, lv, _ in e["evidence"][:2])
        lines += [f"### {a}", "",
                  f"- 考察域：技能点｜出现 **{e['jd']} 份 JD**（必备 {e['levels']['must']} / "
                  f"优先 {e['levels']['pref']} / 加分 {e['levels']['plus']}）",
                  f"- 当前状态：{'🔧 事实库有证据（讲得出细节即可）' if e['cov'] == 'facts' else '⬜ 简历与事实库均无'}",
                  f"- 典型问法：请结合项目说明你对「{a}」的理解、取舍与踩过的坑。",
                  f"- JD 出处：{refs or '—'}", ""]
    lines += ["## 二、跨岗位真缺口（学习优先级）", ""]
    for a, e in gaps[:20]:
        lines.append(f"- **{a}**（{e['jd']} 份 JD ｜ 必备 {e['levels']['must']}）—— "
                     f"出口：{M.ROUTE_CN.get(e['route'], '通用')}")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description="能力雷达：全池 JD 技能点聚合")
    ap.add_argument("--pool", default=str(POOL), help="JD 池目录")
    ap.add_argument("--min-jd", type=int, default=1, help="只统计被 ≥N 份 JD 提及的技能点")
    ap.add_argument("--feed-coach", action="store_true", help="额外导出面试薄弱点候选条目")
    ap.add_argument("--print", dest="to_stdout", action="store_true", help="同时打印到终端")
    args = ap.parse_args()

    pool = Path(args.pool)
    if not pool.is_dir():
        print(f"❌ 找不到 JD 池：{pool}")
        sys.exit(2)

    atoms, meta = scan(pool)
    if not meta["n_jd"]:
        print("❌ JD 池里没有可解析的档案")
        sys.exit(2)

    md, keep = render(atoms, meta, args.min_jd)
    OUT.write_text(md, encoding="utf-8")

    n_gap = sum(1 for e in keep.values() if e["cov"] == "gap")
    n_facts = sum(1 for e in keep.values() if e["cov"] == "facts")
    n_have = sum(1 for e in keep.values() if e["cov"] == "resume")
    print(f"📡 能力雷达 ｜ 扫描 {meta['n_jd']} 份 JD ｜ 技能点 {len(atoms)} 个"
          + (f"（按 ≥{args.min_jd} 份筛选后 {len(keep)} 个）" if args.min_jd > 1 else ""))
    print(f"   ✅ 已具备 {n_have} ｜ 🔧 事实库有证据 {n_facts} ｜ ⬜ 真缺口 {n_gap}")
    if meta["trunc"]:
        print(f"   ⚠️ 残缺 JD {len(meta['trunc'])} 份，其缺口记为未探明")
    print(f"💾 已生成 {OUT}")

    if args.feed_coach:
        COACH_OUT.write_text(render_coach(atoms, args.min_jd), encoding="utf-8")
        print(f"💾 已生成 {COACH_OUT}（请人工确认后粘贴进面试教练的 weaknesses.md）")

    if args.to_stdout:
        print()
        print(md)


if __name__ == "__main__":
    main()
