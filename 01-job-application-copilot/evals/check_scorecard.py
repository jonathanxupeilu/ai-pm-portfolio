# -*- coding: utf-8 -*-
"""
check_scorecard.py — 找岗-投递链路 L1 九项自动判分（口径见 scorecard.md）
==========================================================================
用法：
    python evals/check_scorecard.py                      # 对仓库当前产物判分（沙箱，零外呼零写入）
    python evals/check_scorecard.py --log 跑岗输出.txt   # 附带 find_jobs 的日志判 #1/#2
    python evals/check_scorecard.py --record             # 判完追加一行进 scorecard_ledger.csv

设计约束：
  · **累加记分**：每项 0/1；缺证据的项记「—」不计分（「没跑到」≠「跑挂了」）
  · 默认只读；只有 --record 写台账，且 BOM 只在新文件写一次（追加用 utf-8）
  · #7 的判法 = 对最新短名单真跑一次 apply_shortlist **dry-run**，前后比对
    pipeline.csv 字节 —— 零副作用必须是实证，不是读代码信誓旦旦
  · 红线扫描是启发式（JWT 样串 + 文件名 + META 扩展字段），命中即标 invalid
"""
import re
import sys
import csv
import argparse
import hashlib
import datetime
import pathlib
import subprocess

HERE = pathlib.Path(__file__).resolve().parent
SKILL = HERE.parent
sys.path.insert(0, str(SKILL / "scripts"))

import rank_pool as R  # noqa: E402  META_PAT/load_applied/find_applied/_ct_key/normalize_url/POOL

JWT_PAT = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}")
J1 = "轮次解析"; J2 = "搜索纪律"; J3 = "双层去重"; J4 = "入库完整性"
J5 = "选15纪律"; J6 = "名单落盘"; J6B = "jobId一致性"; J7 = "投递护栏"; J8 = "闭环刷新"
ITEMS = [J1, J2, J3, J4, J5, J6, J6B, J7, J8]

SL_COLS = ["jobId", "jobKind", "公司", "岗位", "URL", "匹配分", "档位",
           "轮次", "薪资", "来源查询", "采集日", "详情文件"]


def _mark(ok):
    return "1" if ok else "0"


def latest_shortlist(pool: pathlib.Path):
    cs = sorted(pool.glob("短名单_*.csv"))
    return cs[-1] if cs else None


def read_sl(path: pathlib.Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        return list(rd), (rd.fieldnames or [])


# ── L1 九项 ──────────────────────────────────────────────────────────────
def j_round(log):
    if log is None:
        return "—", "无 --log，未判"
    if re.search(r"^轮次：\[", log, re.M):
        return "1", "日志有「轮次：[...]」行"
    if "无法确定" in log or "请用户明示" in log:
        return "1", "解析失败但确实停下问用户（不臆测=通过）"
    return "0", "日志里没有轮次声明，也没看到拒跑询问"


def j_search(log):
    if log is None:
        return "—", "无 --log，未判"
    n = len(re.findall(r"猎聘搜索：", log))
    if n == 0:
        return "0", "一次查询都没发（找岗=零搜索是坏轮）"
    if JWT_PAT.search(log):
        return "0", "日志含 JWT 样串（同时触发红线）"
    return "1", f"共 {n} 次查询记录，无令牌泄漏"


def j_dedupe(rows, shown_before, applied, reds):
    if rows is None:
        return "—", "池中无短名单产物"
    keys_seen, dup_in = set(), []
    for r in rows:
        ct, u = R._ct_key(r["公司"], r["岗位"]), R.normalize_url(r["URL"])
        if ct in keys_seen or ("url", u) in keys_seen:
            dup_in.append(ct)
        keys_seen.add(ct); keys_seen.add(("url", u))
    against = [r["公司"] for r in rows
               if any(k in shown_before for k in (
                   ("url", R.normalize_url(r["URL"])), ("ct", R._ct_key(r["公司"], r["岗位"]))))]
    hit_applied = [r["公司"] for r in rows if R.find_applied(
        {"company": r["公司"], "title": r["岗位"], "url": r["URL"]}, applied)]
    hit_red = [r["公司"] for r in rows if R._ct_key(r["公司"], r["岗位"]) in reds]
    bad = dup_in or against or hit_applied or hit_red
    ev = f"内部重复{len(dup_in)} 已展示{len(against)} 已投{len(hit_applied)} red{len(hit_red)}"
    return _mark(not bad), ev


def j_meta_integrity(pool: pathlib.Path):
    dirs = [pool / t for t in ("green", "yellow", "red") if (pool / t).exists()]
    if not dirs:
        return "—", "无档位目录（仓库脱敏态/未入库）"
    bad, n = [], 0
    for d in dirs:
        for f in d.glob("*.md"):
            n += 1
            m = R.META_PAT.search(f.read_text(encoding="utf-8", errors="replace")[:2000])
            if not m or not m.group(8):
                bad.append(f.name)
    return _mark(n > 0 and not bad), f"{n - len(bad)}/{n} 可解析" + (f"，坏件:{bad[:3]}" if bad else "")


def j_select_discipline(rows, md_text, target):
    if rows is None:
        return "—", "无短名单"
    n = len(rows)
    if n == 0 or n > target:
        return "0", f"行数 {n} 越界"
    if any(r["档位"] != "green" for r in rows):
        return "0", "有非🟢混入"
    if n < target and md_text and "⚠️" not in md_text:
        return "0", f"只有 {n} 个却没写 shortfall 说明（凑不满必须留痕）"
    return "1", f"{n}/{target}" + ("（带 shortfall 说明）" if n < target else "")


def j_persist(rows, fieldnames, md_text, shown_rows_stem, csv_name):
    if rows is None:
        return "—", "无短名单"
    if fieldnames[:len(SL_COLS)] != SL_COLS:
        return "0", f"csv 列序漂移：{fieldnames}"
    n_md = len(re.findall(r"^\| \d+ \|", md_text or "", re.M))
    if md_text and n_md != len(rows):
        return "0", f"md {n_md} 行 ≠ csv {len(rows)} 行"
    jr = sum(1 for r in rows if r.get("jobId")) / max(1, len(rows))
    in_log = sum(1 for r in shown_rows_stem
                 if pathlib.Path(r.get("短名单文件", "")).stem == pathlib.Path(csv_name).stem)
    if in_log < len(rows):
        return "0", f"shown_log 只记了 {in_log}/{len(rows)}（展示语义被写坏）"
    return _mark(jr >= 0.9), f"jobId非空率{jr:.0%}，shown_log 记录 {in_log} 行"


def j_jobid_consistency(rows):
    """#6b：jobId 必须等于 URL 推定值。实测 /job/ 页 URL = /job/19<jobId>.shtml，
    "19" 是固定页前缀；/a/ 页 URL 里就是 jobId 本身。2026-09-18 缺陷②的守门项。"""
    if rows is None:
        return "—", "无短名单"
    bad, n_li = [], 0
    for r in rows:
        u = r.get("URL") or ""
        m = re.search(r"liepin\.com/job/(\d{7,})", u)
        if m:
            digits = m.group(1)
            exp = digits[2:] if digits.startswith("19") and len(digits) > 8 else digits
        else:
            m = re.search(r"liepin\.com/a/(\d{5,})", u)
            exp = m.group(1) if m else None
        if exp is None:
            continue
        n_li += 1
        if str(r.get("jobId") or "") != exp:
            bad.append(f"{r.get('公司', '?')}: jobId={r.get('jobId') or '∅'}≠URL推定{exp}")
    if n_li == 0:
        return "—", "无猎聘链接可判"
    return _mark(not bad), f"一致 {n_li - len(bad)}/{n_li}" + (f"；不符:{bad[:3]}" if bad else "")


def j_apply_guard(sl_csv, pipeline: pathlib.Path, max_n):
    if sl_csv is None:
        return "—", "无短名单"
    before = pipeline.read_bytes() if pipeline.exists() else b""
    import io, contextlib
    buf = io.StringIO()
    try:
        sys.path.insert(0, str(SKILL / "scripts"))
        import apply_shortlist as A
        code = 0
        with contextlib.redirect_stdout(buf):
            code = A.main(["--list", str(sl_csv)])
    except SystemExit as e:
        code = e.code or 0
    finally:
        after = pipeline.read_bytes() if pipeline.exists() else b""
    out = buf.getvalue()
    if before != after:
        return "0", "dry-run 动了 pipeline.csv（字节不一致）——这是事故级"
    m = re.search(r"将投 (\d+)", out)
    n = int(m.group(1)) if m else 0
    if n > max_n:
        return "0", f"dry-run 将投 {n} > max {max_n}"
    if JWT_PAT.search(out):
        return "0", "输出含令牌样串"
    return "1", f"dry-run 零副作用（exit={code}，将投 {n}）"


def j_loop(pool: pathlib.Path, applied, pipeline: pathlib.Path):
    lst = pool / "RANKED_LIST.md"
    if not lst.exists():
        return "—", "无 RANKED_LIST"
    if pipeline.exists() and lst.stat().st_mtime < pipeline.stat().st_mtime:
        return "—", "榜单早于台账 mtime（未刷新，不计「跑挂」）"
    txt = lst.read_text(encoding="utf-8", errors="replace")
    sec = txt.split("## 推荐投递排序", 1)[-1].split("\n## ", 1)[0]
    hit = 0
    for ln in sec.splitlines():
        if ln.startswith("|") and "---" not in ln:
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) > 4 and "/" in cells[4]:
                co, _, ti = cells[4].partition("/")
                if R.find_applied({"company": co.strip(), "title": ti.strip(), "url": ""}, applied):
                    hit += 1
    return _mark(hit == 0), f"推荐位命中已投 {hit} 个"


# ── 红线扫描（启发式，命中 → invalid） ────────────────────────────────────
def redlines(pool: pathlib.Path):
    hits = []
    for f in SKILL.rglob("*"):
        if not f.is_file() or ".git" in f.parts or "archive" in f.parts:
            continue
        rel = f.relative_to(SKILL).as_posix()
        if re.search(r"capture.*\.json$", rel):
            hits.append(f"capture 文件疑似入库：{rel}")
            continue
        if rel.startswith(("jd-pool/", "resumes/", "evals/outputs/")) and f.suffix in (".md", ".csv", ".txt"):
            try:
                if JWT_PAT.search(f.read_text(encoding="utf-8", errors="replace")):
                    hits.append(f"数据文件含 JWT 样串：{rel}")
            except Exception:
                pass
    for d in ("green", "yellow", "red"):
        dd = pool / d
        if dd.exists():
            for f in dd.glob("*.md"):
                head = f.read_text(encoding="utf-8", errors="replace")[:2000]
                if re.search(r"MATCHMETA.*\bstatus=applied\b", head):
                    hits.append(f"投递状态写进了 META：{f.name}（违反 2026-09-14 纪律）")
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", default=str(R.POOL))
    ap.add_argument("--pipeline", default=str(R.PIPELINE))
    ap.add_argument("--log", default=None, help="find_jobs 一轮的输出文本文件")
    ap.add_argument("--shortlist", default=None, help="指定短名单 csv（默认取池中最新）")
    ap.add_argument("--target", type=int, default=15)
    ap.add_argument("--record", action="store_true", help="追加一行进 scorecard_ledger.csv")
    ap.add_argument("--ledger", default=str(HERE / "scorecard_ledger.csv"))
    args = ap.parse_args()

    pool = pathlib.Path(args.pool)
    pipeline = pathlib.Path(args.pipeline)
    log = pathlib.Path(args.log).read_text(encoding="utf-8", errors="replace") if args.log else None

    sl_csv = pathlib.Path(args.shortlist) if args.shortlist else latest_shortlist(pool)
    rows, fieldnames, md_text = None, [], ""
    shown_all = []
    if sl_csv and sl_csv.exists():
        rows, fieldnames = read_sl(sl_csv)
        md = sl_csv.with_suffix(".md")
        md_text = md.read_text(encoding="utf-8", errors="replace") if md.exists() else ""
    if pool.exists():
        slg = pool / "shown_log.csv"
        if slg.exists():
            with slg.open(encoding="utf-8-sig", newline="") as fh:
                shown_all = list(csv.DictReader(fh))
    # 「已展示」要排除本名单自己（它就是被展示的那批）；台账存的是 md 名 → 按 stem 对
    stem = sl_csv.stem if sl_csv else ""
    shown_before = {}
    for r in shown_all:
        if stem and pathlib.Path(r.get("短名单文件", "")).stem == stem:
            continue
        if r.get("jobId"):
            shown_before[("job", r["jobId"].strip())] = 1
        if r.get("URL"):
            shown_before[("url", R.normalize_url(r["URL"]))] = 1
        if r.get("公司") and r.get("岗位"):
            shown_before[("ct", R._ct_key(r["公司"], r["岗位"]))] = 1

    applied = R.load_applied() if pipeline.exists() else {}
    reds = set()
    if (pool / "red").exists():
        for f in (pool / "red").glob("*.md"):
            m = R.META_PAT.search(f.read_text(encoding="utf-8", errors="replace")[:2000])
            if m:
                reds.add(R._ct_key(m.group(4), m.group(5)))

    results = {
        J1: j_round(log), J2: j_search(log),
        J3: j_dedupe(rows, shown_before, applied, reds),
        J4: j_meta_integrity(pool),
        J5: j_select_discipline(rows, md_text, args.target),
        J6: j_persist(rows, fieldnames, md_text, shown_all, sl_csv.name if sl_csv else ""),
        J6B: j_jobid_consistency(rows),
        J7: j_apply_guard(sl_csv, pipeline, args.target),
        J8: j_loop(pool, applied, pipeline),
    }
    rds = redlines(pool)

    print("=" * 66)
    mode = "沙箱 E2E（零外呼）"
    print(f"找岗-投递链路打分表 ｜ {datetime.date.today()} ｜ {mode}")
    print(f"证据源：短名单={sl_csv.name if sl_csv else '（无）'} ｜ 日志={'有' if log else '无'}")
    print("=" * 66)
    score, judged = 0, 0
    for k in ITEMS:
        mark, ev = results[k]
        icon = {"1": "✅", "0": "❌", "—": "⬜"}[mark]
        print(f"  {icon} {k:<6} {mark:>1}  {ev}")
        if mark in "01":
            judged += 1
            score += int(mark)
    print(f"\n得分 {score}/{judged}（可判 {judged}/{len(ITEMS)}，「—」为缺证据不计分）")
    if rds:
        print(f"\n🚨 红线命中 {len(rds)} 项 → 本轮记 invalid（不进均分，留档）：")
        for h in rds:
            print(f"   · {h}")
    else:
        print("\n红线扫描：未见异常（③ 未确认实投属操作纪律，机器判不了）")

    if args.record:
        ledger = pathlib.Path(args.ledger)
        new = not ledger.exists() or ledger.stat().st_size == 0
        header = (["跑分日", "模式", "系统版本"] + ITEMS +
                  ["得分", "可判项", "L2_短名单保留率", "L2_备注", "红线invalid", "badcase归因", "证据文件"])
        ver = ""
        try:
            ver = subprocess.run(["git", "-C", str(SKILL), "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True, timeout=10).stdout.strip()
        except Exception:
            pass
        row = {h: "" for h in header}
        row["跑分日"] = str(datetime.date.today())
        row["模式"] = "沙箱"
        row["系统版本"] = ver
        for k in ITEMS:
            row[k] = results[k][0]
        row["得分"], row["可判项"] = str(score), f"{judged}/{len(ITEMS)}"
        row["红线invalid"] = "invalid" if rds else ""
        row["badcase归因"] = "；".join(f"{k}:{results[k][1]}" for k in ITEMS if results[k][0] == "0")[:300]
        row["证据文件"] = sl_csv.name if sl_csv else ""
        with ledger.open("a", encoding="utf-8-sig" if new else "utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=header)
            if new:
                w.writeheader()
            w.writerow(row)
        print(f"\n📄 已记一行进 {ledger.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
