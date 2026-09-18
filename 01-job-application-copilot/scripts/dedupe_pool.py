#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
dedupe_pool.py — 岗位池「同岗双站」合并（近似重复归档）
==========================================================
同一个岗位常同时挂在猎聘 / 全职网 / 公司官网，各入库成一份档案，标题只差一个空格
或一个内部编号（`-L0879V` / `(MJ094092)` / `(A243551)`）。RANKED_LIST 里会并排出现
两条 —— 照着投就会把同一个岗位投两遍。
cleanup_pool 的「规则2」按「公司+岗位名**精确**匹配」去重，差一个空格就漏，抓不到这类。

用法：
    python dedupe_pool.py                    # dry-run 预览（默认不动文件）
    python dedupe_pool.py --apply            # 真正归档
    python dedupe_pool.py --threshold 0.85   # 自定义自动合并阈值（默认 0.90）
    python dedupe_pool.py --floor 0.70       # 人工候选下限（默认 0.60）
    python dedupe_pool.py --show-all         # 连低相似度组合也列出来（排查用）

判定（2026-09-14 用池内 113 份档案实测标定）：
  1. 同公司 + **两个条件同时满足**才判同岗，归档「较差」的一份：
       · 全文归一化相似度 ≥ --threshold（默认 0.90）
       · JD 段相似度 ≥ --jd-min（默认 0.85）—— JD 段 = 「JD 原文摘录」块里剥掉
         公司简介与页脚样板后的正文
     实测：4 组铁定同岗的两项分别是 97.8~99.8% / 96.5~98.9%（双达标），
     不存在重复关系的组合最高只有 81.5% / 73.4% —— 两侧都留了 15 个点以上余量。
     **为什么要卡第二道闸**：同公司档案共享的「公司简介＋页脚」样板可达数千字，
     样板占比远高于正文时，只算全文相似度会把两个不同岗位算到 0.90 以上
     （实测构造：3000 字共享样板 + 各自 300 字不同职责 → 全文相似度 0.91）。
     JD 段相似度这道闸专门挡这种灌水。JD 段缺失时**不自动合并**（fail-closed，
     宁可交人工，不可误杀）。
  2. --floor（默认 0.60）~ 阈值之间的组合 → 只进「待人工确认」清单，**绝不自动合并**。
     为什么必须交人工：本机实测「某视频社区公司同一个岗位的两个入口」只有 83%，而
     「某网络科技公司：AI产品经理 vs AI产品负责人」两个**不同层级**的岗位也有 81.5%
     —— 中间区间靠数值分不开。候选会打印三项证据（全文相似度 / JD 段相似度 /
     共享句占比 + 样例），人看一眼即可判定。
  3. 人工判定结果写进 `jd-pool/.dedupe_verdicts.json`，下次跑自动沿用 ——
     同一个疑问只讨论一次，也留下审计痕迹。格式：
         {"same":      {"<路径A> | <路径B>": "判定理由"},
          "different": {"<路径A> | <路径B>": "判定理由"}}
     键 = 两份档案相对 jd-pool 的路径，排序后用 " | " 连接。
  4. 豁免：组内任一条已在投递台账标记「已投递」→ **不合并**。推荐区里只剩未投的那条，
     本来就没有重复投递风险；而合并会把用户准备「下次投」的入口误删
     （2026-09-14 用户明确指示：某咨询公司漏网项留到下次投）。
  5. red 黑名单不参与（档位基础分不同，分数不可比；黑名单本就长期保留）。

保留优先级：匹配分高 > 采集日新 > 文件名短（无内部编号后缀的那份留）

为什么要做文本归一化 / 为什么不能直接比字符串：
    同一 JD 在不同站点抓下来，空行、编号后缀、页面推荐位噪声都有余量，直接比对会把
    「逐字相同」的同一岗位算成 80% 出头而漏判。故比对前统一去掉：
    MATCHMETA 注释 / 标题行 / URL / 元信息行 / 括号内编号 / 短横线编号 / 所有空白。

⚠️ 试过但**不能单独当判据**的两个指标（避免后人重走弯路）：
    · 「最长公共块 ≥ 300 字」：实测某视频社区公司同岗对的公共块 685 字，但内容是从「公司简介」
      到页脚的**平台样板**，同公司任意两个岗位只要抓取时带了页脚就会共享 600+ 字 → 会误合并。
    · 「只比 JD 段相似度 / 句级覆盖率」：某视频社区公司同岗对因空行格式与补录段差异只拿到 57%，
      而某保险科技公司两条只差一整章 AI 要求的岗位却拿到 81% → 单用分不开，不如全文相似度。
      （但 JD 段相似度**作为第二道闸**与全文相似度同时要求，是必要的 —— 见上文第 1 条。）

📌 本文件与 SKILL.md 里的公司名一律用代号（「某视频社区公司」这类）：技能要同步到公开仓库镜像，
   别把真实投递对象写进去；结论与数字照实保留，只换名字。真实判定记录在 `jd-pool/.dedupe_verdicts.json`
   （该文件在 jd-pool 内，不进公开仓库）。

铁律：默认 dry-run，必须显式 --apply 才动文件；归档不删除，进 jd-pool/archive/YYYY/。
"""
import os
import re
import sys
import json
import argparse
import datetime
import pathlib
import difflib
import itertools
import collections

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import cleanup_pool as C   # 复用 scan() / archive() / POOL
import rank_pool as R      # 复用 load_applied() / find_applied()

AUTO_THRESHOLD = 0.90      # 全文相似度阈值（自动合并的第 1 个条件）
JD_MIN = 0.85              # JD 段相似度阈值（自动合并的第 2 个条件，挡样板灌水）
CAND_FLOOR = 0.60          # ≥ 此值进「待人工确认」清单
VERDICTS = C.POOL / ".dedupe_verdicts.json"
SAMPLE_N = 2               # 候选清单里展示几条共享句样例


# ------------------------------------------------------------------ 文本归一化
def norm_text(p: pathlib.Path) -> str:
    """全文归一化（自动判定与相似度都用它）。"""
    t = p.read_text(encoding="utf-8", errors="replace")
    t = re.sub(r"<!--.*?-->", "", t, flags=re.S)             # MATCHMETA 注释
    t = re.sub(r"^\s*#.*$", "", t, flags=re.M)                # 标题行（含编号后缀）
    t = re.sub(r"https?://\S+", "", t)                        # URL
    t = re.sub(r"^\s*-\s*\*\*.*$", "", t, flags=re.M)         # 元信息行 - **档位**：…
    t = re.sub(r"[（(]\s*[A-Za-z]{1,4}\d{4,}\s*[)）]", "", t)   # (A243551) / (MJ094092)
    t = re.sub(r"-\s*[A-Z]\d{4,}[A-Z]?\b", "", t)             # -L0879V
    return re.sub(r"\s+", "", t)


# ------------------------------------------- JD 段（仅供人工候选参考，不参与自动判定）
_BOILER = re.compile("|".join([
    r"猎聘温馨提示", r"^·", r"如您发现平台内招聘方", r"扣押您的身份证件",
    r"要求您提供担保", r"收取财物", r"强迫您入股", r"牟取不正当利益",
    r"虚假招聘广告", r"工作时长违反劳动法", r"损害您的合法权益",
    r"涉外劳务合作", r"防范招聘欺诈", r"了解更多安全防范知识",
    r"招聘方不向求职者", r"聊一聊", r"猜你喜欢",
    r"【[^】]{0,20}(上海|浦东|徐汇|静安|黄浦|长宁|普陀|闵行|杨浦|虹口|宝山|嘉定)[^】]{0,12}】",
]))
_SENT = re.compile(r"[^；。！？\n]{12,}")


def jd_text(p: pathlib.Path) -> str:
    """只取「JD 原文摘录」围栏块里的正文，剥掉公司简介与页脚样板。"""
    t = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"##\s*JD 原文摘录.*?```(.*?)(?:```|$)", t, re.S)
    if not m:
        return ""
    out, in_boiler = [], False
    for raw in m.group(1).splitlines():
        s = raw.strip()
        if s == "公司简介":
            in_boiler = True
            continue
        if s == "查看全部":
            in_boiler = False
            continue
        if in_boiler or not s or s.startswith("#"):
            continue
        if _BOILER.search(s):
            continue
        out.append(s)
    t = "\n".join(out)
    t = re.sub(r"（补录段[^）]*）", "", t)
    t = re.sub(r"[（(]\s*[A-Za-z]{1,4}\d{4,}\s*[)）]", "", t)
    t = re.sub(r"-\s*[A-Z]\d{4,}[A-Z]?\b", "", t)
    return t


def sentences(txt: str) -> set:
    """切成句子（≥14 字），供「共享句占比」用。"""
    out = set()
    for m in _SENT.finditer(txt):
        s = re.sub(r"\s+", "", m.group(0))
        s = re.sub(r"^[0-9]+[.、]", "", s)
        if len(s) >= 14:
            out.add(s)
    return out


def analyze(pa: pathlib.Path, pb: pathlib.Path) -> dict:
    """算出一对档案的三项证据。"""
    a_full, b_full = norm_text(pa), norm_text(pb)
    ratio = difflib.SequenceMatcher(None, a_full, b_full).ratio()
    a_jd, b_jd = jd_text(pa), jd_text(pb)
    jd_ratio = (difflib.SequenceMatcher(None, re.sub(r"\s+", "", a_jd),
                                        re.sub(r"\s+", "", b_jd)).ratio()
                if a_jd and b_jd else 0.0)
    sa, sb = sentences(a_jd), sentences(b_jd)
    shared = sorted(sa & sb, key=len, reverse=True)
    cov = (len(shared) / min(len(sa), len(sb))) if (sa and sb) else 0.0
    return {"ratio": ratio, "jd_ratio": jd_ratio, "cov": cov,
            "n_shared": len(shared), "n_a": len(sa), "n_b": len(sb),
            "samples": shared[:SAMPLE_N]}


def is_same_job(info: dict, threshold: float = AUTO_THRESHOLD, jd_min: float = JD_MIN) -> bool:
    """自动判定是否同岗 —— 全文与 JD 段**两道闸都过**才算。"""
    return info["ratio"] >= threshold and info["jd_ratio"] >= jd_min


def why_not_auto(info: dict, threshold: float, jd_min: float) -> str:
    """给「待人工确认」的条目一句失败原因，方便人工快速定位。"""
    if info["ratio"] < threshold and info["jd_ratio"] < jd_min:
        return "全文与 JD 段两项都不足"
    if info["ratio"] < threshold:
        return f"全文相似度 {info['ratio']*100:.1f}% 未达 {threshold*100:.0f}%"
    return (f"全文够高但 JD 段只 {info['jd_ratio']*100:.1f}%"
            f"（不足 {jd_min*100:.0f}%，疑似平台样板灌水）")


# ------------------------------------------------------------------ 人工判定
def load_verdicts() -> dict:
    if not VERDICTS.exists():
        return {"same": {}, "different": {}}
    try:
        d = json.loads(VERDICTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"same": {}, "different": {}}
    d.setdefault("same", {})
    d.setdefault("different", {})
    return d


def pair_key(fa: pathlib.Path, fb: pathlib.Path) -> str:
    ra = fa.relative_to(C.POOL).as_posix()
    rb = fb.relative_to(C.POOL).as_posix()
    return " | ".join(sorted([ra, rb]))


def better_of(x: dict, y: dict):
    """保留优先级：匹配分高 > 采集日新 > 文件名短。返回 (better, worse)。"""
    kx = (x["score"], x["date"], -len(x["file"].name))
    ky = (y["score"], y["date"], -len(y["file"].name))
    return (x, y) if kx >= ky else (y, x)


# ------------------------------------------------------------------ 主流程
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行归档（默认 dry-run）")
    ap.add_argument("--threshold", type=float, default=AUTO_THRESHOLD,
                    help=f"全文相似度阈值（默认 {AUTO_THRESHOLD}）")
    ap.add_argument("--jd-min", type=float, default=JD_MIN,
                    help=f"JD 段相似度阈值（默认 {JD_MIN}，挡平台样板灌水）")
    ap.add_argument("--floor", type=float, default=CAND_FLOOR,
                    help=f"人工候选下限（默认 {CAND_FLOOR}）")
    ap.add_argument("--show-all", action="store_true", help="列出全部同公司组合（排查用）")
    args = ap.parse_args()

    today = datetime.date.today()
    pool = C.scan()
    verdicts = load_verdicts()
    applied = R.load_applied()

    items = []
    for tier in ("green", "yellow"):
        for r in pool[tier]:
            r = dict(r)
            r["tier"] = tier
            r["rec"] = R.find_applied(r, applied)     # 已投递记录或 None
            items.append(r)

    groups = collections.defaultdict(list)
    for r in items:
        groups[r["company"]].append(r)

    auto, manual, v_same, v_diff, exempt, low = [], [], [], [], [], []
    for company, lst in sorted(groups.items()):
        if len(lst) < 2:
            continue
        for x, y in itertools.combinations(lst, 2):
            info = analyze(x["file"], y["file"])
            if info["ratio"] < args.floor and not args.show_all:
                continue
            pair = {"company": company, "x": x, "y": y, **info}
            if info["ratio"] < args.floor:
                low.append(pair)
                continue
            key = pair_key(x["file"], y["file"])
            if key in verdicts["same"]:
                pair["note"], pair["src"] = verdicts["same"][key], "人工判定（已落盘）"
                v_same.append(pair)
            elif key in verdicts["different"]:
                pair["note"], pair["src"] = verdicts["different"][key], "人工判定（已落盘）"
                v_diff.append(pair)
            elif x["rec"] or y["rec"]:
                pair["who"] = x if x["rec"] else y
                exempt.append(pair)
            elif is_same_job(info, args.threshold, args.jd_min):
                pair["src"] = "自动判定"
                auto.append(pair)
            else:
                pair["why_not"] = why_not_auto(info, args.threshold, args.jd_min)
                manual.append(pair)

    merge_list = []
    for p in auto + v_same:
        p["keep"], p["drop"] = better_of(p["x"], p["y"])
        merge_list.append(p)

    mode = "🔴 实跑（--apply）" if args.apply else "🔵 dry-run 预览（加 --apply 才真正执行）"
    print("=" * 70)
    print(f"同岗双站合并 ｜ {today} ｜ {mode}")
    print("=" * 70)
    print(f"库存：🟢{len(pool['green'])} 🟡{len(pool['yellow'])} 🔴{len(pool['red'])}"
          f" ｜ 参与比对 {len(items)} 份")
    print(f"自动合并：全文相似度 ≥ {args.threshold:.2f} 且 JD 段 ≥ {args.jd_min:.2f}"
          f" ｜ 人工候选下限：{args.floor:.2f} ｜ 人工判定档案：{VERDICTS.name}")

    if merge_list:
        print(f"\n【将合并 {len(merge_list)} 组】（同公司 + 正文近似重复 → 同一个岗位）")
        for p in merge_list:
            print(f"\n  ▸ {p['src']}｜相似度 {p['ratio']*100:.1f}%"
                  f"（JD 段 {p['jd_ratio']*100:.1f}% / 共享句 {p['cov']*100:.0f}%）"
                  f"｜{p['company']}")
            if p.get("note"):
                print(f"     理由：{p['note']}")
            print(f"     ✅ 保留 {p['keep']['file'].relative_to(C.POOL).as_posix()}"
                  f"（{p['keep']['score']} 分）")
            print(f"     📦 归档 {p['drop']['file'].relative_to(C.POOL).as_posix()}"
                  f"（{p['drop']['score']} 分）")
            print("     " + C.archive(p["drop"]["file"], today,
                                      f"与 {p['keep']['file'].name} 同岗"
                                      f"（相似度 {p['ratio']*100:.1f}%）", args.apply))
    else:
        print("\n✅ 没有需要合并的重复")

    if v_diff:
        print(f"\n【人工判定「不同岗位」，已固定跳过 {len(v_diff)} 组】")
        for p in v_diff:
            print(f"  · {p['company']}（相似度 {p['ratio']*100:.1f}%）—— {p.get('note','')}")

    if exempt:
        print(f"\n【已投递豁免 {len(exempt)} 组】"
              f"（一条已投、一条未投 → 不合并，保留未投那条供下次投）")
        for p in exempt:
            w = p["who"]
            other = p["y"] if w is p["x"] else p["x"]
            print(f"  · {p['company']}（相似度 {p['ratio']*100:.1f}%）"
                  f"｜已投：{w['title']}（{w['rec']['status']} {w['rec']['date']}）")
            print(f"     留在池里供下次投：{other['title']}")

    if manual:
        print(f"\n【待人工确认 {len(manual)} 组】（{args.floor:.2f}~{args.threshold:.2f}，"
              f"证据不足，**不会自动合并**）")
        for p in manual:
            print(f"\n  ▸ {p['company']}｜相似度 {p['ratio']*100:.1f}%"
                  f" ｜ JD 段 {p['jd_ratio']*100:.1f}%"
                  f" ｜ 共享句 {p['cov']*100:.0f}%（{p['n_shared']}/{min(p['n_a'], p['n_b'])}）")
            print(f"     未自动合并原因：{p.get('why_not', '—')}")
            print(f"     A {p['x']['title']}   ← {p['x']['file'].relative_to(C.POOL).as_posix()}")
            print(f"     B {p['y']['title']}   ← {p['y']['file'].relative_to(C.POOL).as_posix()}")
            for s in p["samples"]:
                print(f"     共享句：{s[:68]}{'…' if len(s) > 68 else ''}")
        print(f"\n  💡 判定后写进 {VERDICTS.name} 的 \"same\" / \"different\"，"
              f"下次跑自动沿用，不再重复提示。")

    if args.show_all and low:
        print(f"\n【低相似度组合 {len(low)} 组】（< {args.floor:.2f}，基本是不同岗位，仅备查）")
        for p in low[:12]:
            print(f"  · {p['ratio']*100:5.1f}%｜{p['company']}｜"
                  f"{p['x']['title'][:26]} ⟷ {p['y']['title'][:26]}")
        if len(low) > 12:
            print(f"  … 另有 {len(low) - 12} 组")

    print(f"\n合并后留存：{len(items) - len(merge_list)} 个可投岗位 + {len(pool['red'])} 个黑名单")
    if not args.apply and merge_list:
        print("\n⚠️ 以上是预览，未改动任何文件。确认无误后重跑：python dedupe_pool.py --apply")
    print("\n合并后记得刷新：`python rank_pool.py`")


if __name__ == "__main__":
    main()
