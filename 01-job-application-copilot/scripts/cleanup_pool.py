# -*- coding: utf-8 -*-
"""
cleanup_pool.py — 岗位池清理（过时 / 冗余 / 黑名单去重）
=========================================================
用法：
    python cleanup_pool.py              # dry-run 预览（默认不动文件）
    python cleanup_pool.py --apply      # 真正执行归档/去重
    python cleanup_pool.py --stale-days 45 --apply   # 自定义过期天数

清理规则：
  1. 过时：采集日距今 > 60 天（--stale-days 可调）→ 移入 jd-pool/archive/YYYY/
     —— 不删除，归档可回溯；归档文件不再进 RANKED_LIST
  2. 冗余：同「公司 + 岗位名」重复入库 → 保留匹配分最高（同分保留最新），其余归档
  3. 黑名单去重（新增入库前用）：red 档长期保留，但其 (公司, 岗位) 作为过滤键——
     新搜到同名岗位直接跳过，不重复打分（--check-block 查看当前黑名单键）
  4. 链接失效 ≠ 清理依据：失效只是提示，不触发归档（JD 原文摘录仍是匹配依据）

铁律：默认 dry-run，必须显式 --apply 才动文件；red 档永不因「过时」被清理。
"""
import re
import shutil
import argparse
import datetime
import pathlib

SKILL = pathlib.Path(__file__).resolve().parent.parent
POOL = SKILL / "jd-pool"
ARCHIVE = POOL / "archive"

META_PAT = re.compile(
    r"<!--\s*MATCHMETA\s+tier=(\w+)\s+score=([\d.]+)\s+coverage=(\S+)\s+"
    r"company=(.+?)\s+title=(.+?)\s+date=(\S+)\s+source=(\S+?)(?:\s+url=(\S+?))?\s*-->"
)


def scan():
    """返回 {tier: [fileinfo]}"""
    out = {}
    for tier in ("green", "yellow", "red"):
        d = POOL / tier
        out[tier] = []
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            m = META_PAT.search(f.read_text(encoding="utf-8", errors="replace")[:2000])
            if not m:
                out[tier].append({"file": f, "tier": tier, "score": -1.0, "company": f.stem,
                                  "title": "?", "date": "1970-01-01", "url": "", "no_meta": True})
                continue
            t, score, cov, comp, title, date, source, url = m.groups()
            out[tier].append({
                "file": f, "tier": tier, "score": float(score), "company": comp,
                "title": title, "date": date, "url": (url or "").strip(), "no_meta": False,
            })
    return out


def age_days(date_s, today):
    try:
        return (today - datetime.date.fromisoformat(date_s)).days
    except Exception:
        return 0


def archive(f: pathlib.Path, today, reason, apply=False):
    sub = ARCHIVE / str(today.year)
    rel = f.relative_to(POOL)
    dst = sub / rel.as_posix().replace("/", "_")
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dst))
    return f"{'📦 归档' if apply else '👉 将归档'} {rel.as_posix()} → archive/{today.year}/{dst.name}（{reason}）"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run 只预览）")
    ap.add_argument("--stale-days", type=int, default=60, help="超过该天数归档（默认 60）")
    ap.add_argument("--check-block", action="store_true", help="只打印 red 黑名单过滤键")
    args = ap.parse_args()

    today = datetime.date.today()
    pool = scan()

    if args.check_block:
        keys = sorted({(r["company"], r["title"]) for r in pool["red"]})
        print(f"🔴 黑名单过滤键 {len(keys)} 个（新搜到同名岗位直接跳过，不重复打分）：")
        for c, t in keys:
            print(f"  - {c} / {t}")
        return

    logs, kept = [], []
    # 规则1：过时（red 不参与）
    for tier in ("green", "yellow"):
        for r in pool[tier]:
            a = age_days(r["date"], today)
            if a > args.stale_days:
                logs.append(archive(r["file"], today, f"采集于 {a} 天前，超过 {args.stale_days} 天", args.apply))
            else:
                kept.append(r)

    # 规则2：冗余去重（同公司+岗位，保留最高分/最新）
    seen = {}
    dupes = []
    for r in kept:
        key = (r["company"], r["title"])
        if key in seen:
            prev = seen[key]
            better = prev if (prev["score"], prev["date"]) >= (r["score"], r["date"]) else r
            worse = r if better is prev else prev
            dupes.append(worse)
            seen[key] = better
        else:
            seen[key] = r
    for r in dupes:
        logs.append(archive(r["file"], today, f"与保留项同公司+岗位重复（保留匹配分更高者），本条 {r['score']} 分", args.apply))

    mode = "🔴 实跑（--apply）" if args.apply else "🔵 dry-run 预览（加 --apply 才真正执行）"
    print("=" * 62)
    print(f"岗位池清理 ｜ {today} ｜ {mode}")
    print("=" * 62)
    print(f"\n当前库存：🟢{len(pool['green'])} 🟡{len(pool['yellow'])} 🔴{len(pool['red'])}")
    print(f"过期阈值：> {args.stale_days} 天（red 黑名单不受时效影响，长期保留）")
    if logs:
        print(f"\n【清理项 {len(logs)} 条】")
        for l in logs:
            print("  " + l)
    else:
        print("\n✅ 没有需要清理的岗位（无过时、无重复）")
    remain = [r for r in kept if r not in dupes]
    print(f"\n清理后留存：{len(remain)} 个可投岗位 + {len(pool['red'])} 个黑名单")
    if not args.apply and logs:
        print("\n⚠️ 以上是预览，未改动任何文件。确认无误后重跑：python cleanup_pool.py --apply")
    print("\n清理后记得刷新：`python rank_pool.py`")


if __name__ == "__main__":
    main()
