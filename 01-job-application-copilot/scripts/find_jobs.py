#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_jobs.py — 找岗编排层：猎聘搜索 → 双层去重 → 轮次过滤 → 15岗短名单
========================================================================
对应流程：猎聘CLI搜指定方向岗位 → 去重（①本轮内不重复 ②以前展示过的不再推）
→ 按 strategy.md 轮次过滤 → 凑满 15 个出短名单 → 停下等用户确认
（投递由 apply_shortlist.py 执行，本脚本不投递。）

用法：
    python find_jobs.py                          # 按 strategy.md 当前轮次跑一轮找岗
    python find_jobs.py --round 占坑,练手        # 手动指定轮次（跳过 §一 解析）
    python find_jobs.py --job-name Agent产品经理  # 指定搜索词（默认为 profile.md 岗位词矩阵）
    python find_jobs.py --dry-run                # 只搜+报告去重结果，不入库不写名单
    python find_jobs.py --seed-file <旧短名单.md> # 一次性迁移：旧名单行导入 shown_log

纪律：
  · 轮次解析不出来 → 停下来问用户，**不臆测**（与 find_strategy_file 降级口径一致）
  · 凑不满 15 → 自动扩搜（翻页→换词组合→纳入池中旧的未展示🟢）；矩阵耗尽仍不足
    则带 shortfall 出名单，**绝不用🟡/已展示/未标轮次的岗凑数**
  · 「展示过」的判定源 = jd-pool/shown_log.csv，**只有进短名单才算展示**
    （RANKED_LIST 是全池板，按它记会把全池标死）
  · jobId/jobKind 只存短名单 csv，**不进 MATCHMETA**（2026-09-14 字段变更事故纪律）
"""
import argparse
import csv
import datetime
import pathlib
import re
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import match_jd as M          # noqa: E402
import rank_pool as R         # noqa: E402
import cleanup_pool as C      # noqa: E402
import liepin_cli as L        # noqa: E402
import patch_jd as PJ         # noqa: E402

POOL = R.POOL
SKILL = R.SKILL
SHOWN_LOG = POOL / "shown_log.csv"
SHOWN_HEADER = ["展示日", "公司", "岗位", "URL", "jobId", "轮次", "短名单文件"]
TARGET_DEFAULT = 15
MAX_QUERIES_DEFAULT = 12
MIN_BODY_CHARS = 300          # 低于此长度回源抓详情（match_jd 完整性闸门仍会兜底）

# 短名单 csv 列（机读真源，apply_shortlist.py 消费）
SL_CSV_COLS = ["jobId", "jobKind", "公司", "岗位", "URL", "匹配分", "档位",
               "轮次", "薪资", "来源查询", "采集日", "详情文件"]


# ── 轮次解析（strategy.md §一「当前所处轮次」）────────────────────────────────
MACHINE_PAT = re.compile(r"<!--\s*CURRENT_ROUND\s*[:：]\s*([^\n>]+?)\s*-->")


def _section_slice(txt: str, heading_kw: str) -> str:
    """截取含 heading_kw 的 ##/### 小节（到下一个 ## 标题为止）。找不到返回空串。"""
    lines = txt.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith("#") and heading_kw in ln:
            start = i
            break
        # §一 也可能是表格行/加粗行而非标题：命中「当前所处轮次」即从该行起算
        if heading_kw in ln:
            start = i
            break
    if start is None:
        return ""
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].lstrip().startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def parse_current_round(path=None):
    """返回 (rounds, evidence)。rounds=[] 表示解析不可信 → 调用方必须问用户，不臆测。

    三级解析：
      ① 机器标记行 `<!-- CURRENT_ROUND: 占坑,练手 -->`（最优先；技能不写 strategy.md，
         用户愿意加此行则确定性最高）
      ② §一「当前所处轮次」prose 提取：「占坑 + 练手（并行）」→ ["占坑","练手"]
      ③ 提取为空 / 命中≥3个轮次名（说明抓到的是 §二 全表而非 §一）→ 不可信
    """
    path = path or M.find_strategy_file()
    if path is None or not pathlib.Path(path).exists():
        return [], "策略文件 strategy.md 不存在"
    txt = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")

    m = MACHINE_PAT.search(txt)
    if m:
        rounds = [p for p in R.PURPOSE_ORDER if p in m.group(1)]
        if rounds:
            return rounds, f"机器标记行 CURRENT_ROUND → {rounds}"
        return [], f"CURRENT_ROUND 标记里的轮次名无法识别：{m.group(1)!r}"

    sec = _section_slice(txt, "当前所处轮次")
    if not sec:
        return [], "策略文件里找不到「当前所处轮次」段"
    hits = [p for p in R.PURPOSE_ORDER if p in sec]
    if len(hits) >= 3:
        return [], f"该段命中 {len(hits)} 个轮次名，疑似抓到的是全表而非当前轮声明，不可信"
    if not hits:
        return [], "「当前所处轮次」段里没提到任何已知轮次名"
    first_line = next((ln.strip() for ln in sec.splitlines() if any(p in ln for p in hits)), "")
    return hits, f"§一 prose 提取：{first_line[:60]} → {hits}"


# ── shown 台账（去重层②：以前展示过的不再推）──────────────────────────────────
def load_shown_log(path: pathlib.Path = SHOWN_LOG) -> dict:
    """读台账 → 键索引 {('job',jobId) / ('url',规范URL) / ('ct',公司|岗位): 记录}。
    缺文件 → {}。jobId 优先、URL 次之、公司+岗位兜底（ct 键大小写敏感，防误杀）。"""
    idx = {}
    if not pathlib.Path(path).exists():
        return idx
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            rec = dict(row)
            if row.get("jobId"):
                idx[("job", row["jobId"].strip())] = rec
            url = (row.get("URL") or "").strip()
            if url and url != "none":
                idx[("url", R.normalize_url(url))] = rec
            if row.get("公司") and row.get("岗位"):
                idx[("ct", R._ct_key(row["公司"], row["岗位"]))] = rec
    return idx


def shown_key_of(row: dict):
    """从岗位行提取台账键集合（任一命中即算已展示）。"""
    keys = []
    jid = str(row.get("jobId") or "").strip()
    if jid:
        keys.append(("job", jid))
    url = (row.get("url") or "").strip()
    if url and url != "none":
        keys.append(("url", R.normalize_url(url)))
    co, ti = row.get("company") or row.get("公司"), row.get("title") or row.get("岗位")
    if co and ti:
        keys.append(("ct", R._ct_key(co, ti)))
    return keys


def is_shown(row: dict, shown: dict):
    for k in shown_key_of(row):
        if k in shown:
            return shown[k]
    return None


def append_shown_log(rows, date, shortlist_file, path: pathlib.Path = SHOWN_LOG) -> int:
    """短名单写成后才记 shown；按现有键幂等（重复行不写）。返回新增行数。"""
    path = pathlib.Path(path)
    existing = load_shown_log(path)
    new_rows = []
    for r in rows:
        keys = shown_key_of(r)
        if any(k in existing for k in keys):
            continue
        new_rows.append([
            str(date), r.get("company") or r.get("公司") or "",
            r.get("title") or r.get("岗位") or "",
            r.get("url") or r.get("URL") or "",
            str(r.get("jobId") or ""), r.get("purpose") or r.get("轮次") or "",
            str(shortlist_file),
        ])
        # 本批内部也要幂等：把刚生成的键并入索引
        for k in keys or [("raw", tuple(new_rows[-1][:3]))]:
            existing[k] = True
    if not new_rows:
        return 0
    is_new = not path.exists()
    # BOM 只在新文件写一次：utf-8-sig 追加模式会在续写处再吐一个 BOM，毒化首列
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8-sig" if is_new else "utf-8", newline="") as fh:
        w = csv.writer(fh)
        if is_new:
            w.writerow(SHOWN_HEADER)
        w.writerows(new_rows)
    return len(new_rows)


# ── 去重层①本轮内 + ④red 黑名单 ─────────────────────────────────────────────
def dedupe_within(rows, seen):
    """本轮内按 jobId / 规范URL / 公司+岗位 去重，先到先得（搜索结果序即优先级）。"""
    out = []
    for r in rows:
        keys = shown_key_of(r) or [("ct", R._ct_key(r.get("company", "?"), r.get("title", "?")))]
        if any(k in seen for k in keys):
            continue
        seen.update({k: True for k in keys})
        out.append(r)
    return out


def red_block_keys():
    """层④：red 档 (公司,岗位) 黑名单——别投的坑不重复研究（cleanup --check-block 同逻辑）。"""
    pool = C.scan()
    return {R._ct_key(r["company"], r["title"]) for r in pool["red"]}


# ── 搜索词矩阵 ───────────────────────────────────────────────────────────────
DEFAULT_JOB_WORDS = ["AI产品经理", "大模型产品经理", "Agent产品经理"]


def build_search_matrix(job_name=None, address="上海", seed_date=None):
    """--job-name 指定则单查询；否则岗位词矩阵轮换（按日期取模，跨轮先跑不同组合，天然扩搜面）。"""
    if job_name:
        return [{"jobName": job_name, "address": address}]
    words = list(DEFAULT_JOB_WORDS)
    rot = (seed_date or datetime.date.today()).toordinal() % len(words)
    words = words[rot:] + words[:rot]
    return [{"jobName": w, "address": address} for w in words]


# ── 入库（复用 match_jd --save 链路）─────────────────────────────────────────
def jd_text_for(row: dict) -> str:
    """JD 正文：payload 自带 description 优先；太短则回源抓详情页（网页 GET 不占 MCP 配额）。"""
    body = (row.get("description") or "").strip()
    url = (row.get("url") or "").strip()
    if len(body) < MIN_BODY_CHARS and url and url != "none":
        try:
            fetched, note = PJ.fetch_jd(url)
            if len(fetched) > len(body):
                body = fetched
                print(f"    ↳ 回源补正文：{note}")
        except Exception as e:
            print(f"    ↳ 回源失败（{e}）→ 用 payload 正文走完整性闸门")
    return f"链接：{url}\n\n{body}" if url else body


def ingest_jd(row: dict, purpose: str, tmpdir) -> tuple:
    """写临时 JD → subprocess match_jd --save。返回 (tier|None, 说明)。"""
    txt = jd_text_for(row)
    if len(txt.strip()) < 40:
        return None, "正文过短且回源无果，跳过入库（避免假完整）"
    tmp = pathlib.Path(tmpdir) / f"{row['company']}_{row['title']}.md".replace("/", "_")
    tmp.write_text(txt, encoding="utf-8")
    cmd = [sys.executable, str(HERE / "match_jd.py"), str(tmp),
           "--company", row["company"], "--title", row["title"],
           "--save", "--source", "主动搜索"]
    if row.get("url"):
        cmd += ["--url", row["url"]]
    if purpose:
        cmd += ["--purpose", purpose]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if p.returncode not in (0, 1):   # match_jd：red 档退出码 1，属正常入库
        return None, f"match_jd 失败(exit={p.returncode})：{(p.stderr or p.stdout).strip()[:120]}"
    m = re.search(r"已存入 jd-pool/(\w+)/", p.stdout)
    return (m.group(1) if m else None), "已入库"


# ── 选岗：过滤 + 排序 ────────────────────────────────────────────────────────
def select_candidates(pool_rows, shown, applied, rounds, fresh_keys, target):
    """🟢 ∧ 轮次 ∧ 未投 ∧ 未展示 ∧ 非不投 ∧ 非dead；本轮新采优先，池中旧未展示回捞补足。
    轮次准入：池里旧岗必须 purpose∈当前轮（未标注=未归类，不进）；**本轮新采的岗例外**——
    它们就是按当前轮的搜索词捞进来的，多轮并行时脚本无法替用户归类（占坑/练手取决于公司
    规模性质，strategy.md 是人话定义），留空标注、榜单显示「—」，下次跑即按未归类排除。
    返回 (picked, excluded_shown_count)。picked 元素 = 池行 + _fresh 标记。"""
    picked, n_shown = [], 0
    for r in pool_rows:
        if r["tier"] != "green" or r["dead"] or r["purpose"] == R.NO_SUBMIT:
            continue
        keys = shown_key_of(r)
        fresh = any(k in fresh_keys for k in keys)
        if rounds and r["purpose"] not in rounds and not fresh:
            continue
        if R.find_applied(r, applied):
            continue
        if is_shown(r, shown) is not None:   # 空 dict 记录也必须算已展示（真值判断会漏）
            n_shown += 1
            continue
        r = dict(r)
        r["_fresh"] = fresh
        picked.append(r)
    picked.sort(key=lambda x: (not x["_fresh"], -x["score"], x["date"]))
    # 同 URL 只留一个：旧池重复档案与本批新采可能是同一岗（2026-09-18 字节VOC双行事故）
    out, seen_urls = [], set()
    for r in picked:
        u = (r.get("url") or "").strip()
        if u and u != "none":
            nu = R.normalize_url(u)
            if nu in seen_urls:
                continue
            seen_urls.add(nu)
        out.append(r)
    return out[:target], n_shown


# ── 短名单写出 ───────────────────────────────────────────────────────────────
def job_id_from_url(url: str) -> str:
    """兜底提取（payload jobId 才是真源）。实测 /job/ 页 URL = /job/19<jobId>.shtml，
    "19" 是固定页前缀须剥掉；/a/ 页 URL 里就是 jobId 本身。"""
    m = re.search(r"/job/(\d{7,})", url or "")
    if m:
        digits = m.group(1)
        return digits[2:] if digits.startswith("19") and len(digits) > 8 else digits
    m = re.search(r"/a/(\d{5,})", url or "")
    return m.group(1) if m else ""


def write_shortlist(picked, meta_by_key, date, pool_dir: pathlib.Path = POOL, shortfall=""):
    """写 短名单_YYYYMMDD.md（人读）+ .csv（机读真源）。返回 (md_path, csv_path)。"""
    stem = f"短名单_{date:%Y%m%d}"
    md_path, csv_path = pool_dir / f"{stem}.md", pool_dir / f"{stem}.csv"
    md = [f"# 投递短名单 ｜ {date} ｜ {len(picked)} 个岗位",
          "",
          "> 生成方式：`python scripts/find_jobs.py`（猎聘搜索 → 双层去重 → 轮次过滤 → 凑15）。",
          "> **确认后**由 `python scripts/apply_shortlist.py --list "
          f"{stem}.csv --confirm` 用猎聘在线简历批量直投；一岗一版留给约到面的重点岗。",
          "> 进本名单即记入 `shown_log.csv`——**下次找岗这些岗不再出现**。",
          ""]
    if shortfall:
        md += [f"> ⚠️ {shortfall}", ""]
    md += ["| # | 匹配分 | 轮次 | 公司 / 岗位 | 薪资 | jobId | 投递链接 | 详情文件 |",
           "|---|---|---|---|---|---|---|---|"]
    csv_rows = []
    for i, r in enumerate(picked, 1):
        meta = meta_by_key.get(R._ct_key(r["company"], r["title"]), {})
        jid = str(r.get("jobId") or "") or meta.get("jobId") or job_id_from_url(r.get("url", ""))
        jkind = meta.get("jobKind", "")
        fresh = "🆕" if r.get("_fresh") else "♻️"
        pur = "" if r["purpose"] == "none" else r["purpose"]   # META 空值约定 purpose=none → 显示 —
        link = f"[投递]({r['url']})" if r.get("url") and r["url"] != "none" else "⚠️ 无链接"
        md.append(f"| {i} | {r['score']} | {pur or '—'} {fresh} | "
                  f"{r['company']} / {r['title']} | {meta.get('salary', '')} | {jid or '—'} | {link} | {r['file']} |")
        csv_rows.append({
            "jobId": jid, "jobKind": jkind, "公司": r["company"], "岗位": r["title"],
            "URL": r.get("url", ""), "匹配分": r["score"], "档位": r["tier"],
            "轮次": pur, "薪资": meta.get("salary", ""),
            "来源查询": meta.get("_query", ""), "采集日": r["date"], "详情文件": r["file"],
        })
    md += ["", "🆕 = 本轮新采 ｜ ♻️ = 池中旧的未展示岗回捞（扩搜不足时）", ""]
    md_path.write_text("\n".join(md), encoding="utf-8")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SL_CSV_COLS)
        w.writeheader()
        w.writerows(csv_rows)
    return md_path, csv_path


def seed_from_file(path, date=None):
    """一次性迁移：把旧短名单 md 的表格行导入 shown_log（用户确认后才跑）。"""
    date = date or datetime.date.today()
    txt = pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
    rows = []
    for ln in txt.splitlines():
        if not ln.strip().startswith("|") or "---" in ln:
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        m_url = re.search(r"\]\((https?://[^)]+)\)", ln)
        co_ti = next((c for c in cells if "/" in c and "公司" not in c), "")
        if not (co_ti and m_url):
            continue
        co, _, ti = co_ti.partition("/")
        rows.append({"company": co.strip(), "title": ti.strip(), "url": m_url.group(1)})
    n = append_shown_log(rows, date, pathlib.Path(path).name)
    print(f"迁移完成：从 {pathlib.Path(path).name} 解析 {len(rows)} 行，台账新增 {n} 行。")
    return n


# ── 主流程 ───────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--round", default=None, help="手动指定轮次（逗号分隔，如 占坑,练手）；缺省解析 strategy.md §一")
    ap.add_argument("--job-name", default=None, help="指定搜索词；缺省用岗位词矩阵")
    ap.add_argument("--address", default="上海")
    ap.add_argument("--target", type=int, default=TARGET_DEFAULT)
    ap.add_argument("--max-queries", type=int, default=MAX_QUERIES_DEFAULT, help="扩搜硬上限（防跑飞）")
    ap.add_argument("--max-pages", type=int, default=3, help="单个搜索词翻页上限")
    ap.add_argument("--dry-run", action="store_true", help="只搜+去重报告，不入库不写名单")
    ap.add_argument("--no-expand", action="store_true", help="禁用扩搜（只跑矩阵一轮）")
    ap.add_argument("--seed-file", default=None, help="一次性迁移旧短名单进 shown_log 后退出")
    args = ap.parse_args(argv)
    today = datetime.date.today()

    if args.seed_file:
        seed_from_file(args.seed_file, today)
        return 0

    # 1) 轮次
    if args.round:
        rounds = [p.strip() for p in args.round.split(",") if p.strip() in R.PURPOSE_ORDER]
        evidence = f"--round 手动指定 → {rounds}"
        bad = [p.strip() for p in args.round.split(",") if p.strip() not in R.PURPOSE_ORDER]
        if bad:
            print(f"❌ --round 含未知轮次 {bad}（已知：{R.PURPOSE_ORDER}）")
            return 2
    else:
        rounds, evidence = parse_current_round()
    if not rounds:
        print(f"❓ 当前轮次无法确定（{evidence}）。")
        print("   → 请用户明示本轮投什么（占坑/练手/熟流程/主攻），或重跑时加 --round；不臆测。")
        return 3
    print(f"轮次：{rounds} ｜ 依据：{evidence}")

    # 2) 台账与索引
    shown = load_shown_log()
    applied = R.load_applied()
    blocked = red_block_keys()
    print(f"索引：已展示 {len(shown)} 键 ｜ 已投递 {len(applied)} 键 ｜ red 黑名单 {len(blocked)} 个")

    matrix = build_search_matrix(args.job_name, args.address)
    if args.no_expand:
        matrix = matrix[:1]

    seen_run = {}       # 层①：本轮内去重
    fresh_keys = set()  # 本轮新采的台账键（select 时新岗优先）
    meta_by_key = {}    # ct 键 → 搜索行（jobId/jobKind/salary/_query 带给短名单 csv）
    queries_used = 0
    shortfall = ""

    # 3) 搜索 → 去重 → 入库；不足 target 则扩搜。
    #    每次跑至少发 1 个查询——「找岗」的语义就是捞新的，池里够 15 个也不许零搜索。
    with tempfile.TemporaryDirectory() as tmpdir:
        plans = [dict(q, page=p) for q in matrix for p in range(1, args.max_pages + 1)]
        for plan in plans:
            if queries_used >= args.max_queries:
                shortfall = f"达到 --max-queries {args.max_queries} 上限，提前停止扩搜"
                break
            if queries_used > 0:  # 首个查询无条件跑（至少搜一次）
                pool_rows, _, _ = R.parse_pool()
                picked, n_shown = select_candidates(pool_rows, shown, applied, rounds, fresh_keys, args.target)
                if len(picked) >= args.target:
                    break
            queries_used += 1
            q_desc = f"{plan['jobName']}@{plan['address']} p{plan['page']}"
            print(f"\n🔎 [{queries_used}/{args.max_queries}] 猎聘搜索：{q_desc}")
            try:
                payload, rows = L.search_jobs(job_name=plan["jobName"], address=plan["address"],
                                              page=plan["page"], tool="user-search-job")
            except L.McpAuthError as e:
                print(f"❌ {e}")
                return 3
            except L.McpError as e:
                print(f"⚠️ 查询失败（{e}）→ 换下一个组合继续")
                continue
            cand = [r for r in rows if r.get("company") and r.get("title")]
            cand = [r for r in cand if R._ct_key(r["company"], r["title"]) not in blocked]
            cand = dedupe_within(cand, seen_run)
            print(f"   返回 {len(rows)} ｜ 去重后可入库 {len(cand)}")
            if args.dry_run:
                for r in cand:
                    print(f"   · [dry-run] {r['company']} / {r['title']}  {r['salary']}")
                for r in cand:
                    seen_key = R._ct_key(r["company"], r["title"])
                    meta_by_key.setdefault(seen_key, {**r, "_query": q_desc})
                    fresh_keys.update(shown_key_of(r))
                continue
            for r in cand:
                tier, note = ingest_jd(r, rounds[0] if len(rounds) == 1 else "", tmpdir)
                meta_by_key[R._ct_key(r["company"], r["title"])] = {**r, "_query": q_desc}
                fresh_keys.update(shown_key_of(r))
                print(f"   · {r['company']} / {r['title']} → {tier or '—'}（{note}）")

        pool_rows, _, _ = R.parse_pool()
        picked, n_shown = select_candidates(pool_rows, shown, applied, rounds, fresh_keys, args.target)
        if len(picked) < args.target and not shortfall:
            shortfall = (f"搜索矩阵已穷尽（{queries_used} 次查询 / {len(matrix)} 组词 × ≤{args.max_pages} 页），"
                         f"仍只有 {len(picked)} 个达标岗——**不拿🟡/未标轮次/已展示的岗凑数**")

    if args.dry_run:
        print(f"\n🔵 dry-run 结束：搜了 {queries_used} 次；当前可选 {len(picked)} 个；按台账排除已展示 {n_shown} 个。")
        return 0
    if not picked:
        print(f"\n🛑 没有任何可选岗位（{shortfall or '池为空'}）")
        return 4

    # 4) 出名单 + 记台账（picked 补上 jobId，台账才有最强去重键）
    for r in picked:
        meta = meta_by_key.get(R._ct_key(r["company"], r["title"]), {})
        r["jobId"] = meta.get("jobId") or job_id_from_url(r.get("url", ""))
    md_path, csv_path = write_shortlist(picked, meta_by_key, today, shortfall=shortfall)
    n_log = append_shown_log(picked, today, md_path.name)
    print("\n" + "=" * 62)
    print(f"✅ 短名单已生成：{md_path.name}（{len(picked)} 个，本轮新采 {sum(1 for p in picked if p['_fresh'])}）")
    if shortfall:
        print(f"⚠️ {shortfall}")
    print(f"   台账：shown_log.csv 本轮新增 {n_log} 行；按台账排除已展示 {n_shown} 个（想重看某岗→手删台账该行）")
    print(f"   下一步：核对名单后跑  python scripts/apply_shortlist.py --list {csv_path.name} --confirm")
    print("   （本技能不自动投递——名单确认一次再投。）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
