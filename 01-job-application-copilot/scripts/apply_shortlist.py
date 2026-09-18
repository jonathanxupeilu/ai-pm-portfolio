# -*- coding: utf-8 -*-
"""
apply_shortlist.py — 短名单批量直投（猎聘官方 MCP user-apply-job，在线简历）
============================================================================
用法：
    python apply_shortlist.py --list jd-pool/短名单_YYYYMMDD.csv            # 默认 dry-run 只看清单
    python apply_shortlist.py --list jd-pool/短名单_YYYYMMDD.csv --confirm  # 用户发话后才真投

为什么与 find_jobs.py 拆成两个脚本：生成名单是本地动作，投递是**对外不可撤回动作**，
风险级不同——拆开后可单独重试投递，且「确认」这个动作在命令行上显式存在。

护栏（全部是硬约束，不是建议）：
  · 默认 dry-run；必须 --confirm 才调 user-apply-job
  · 单次 --max 15 上限（流程图语义「每次凑 15 投 15」）
  · 幂等：投递台账 pipeline.csv 命中（已投/已读/约面/挂/别投）的行自动跳过
  · jobId / jobKind 缺值 → 跳过并报因，**不猜值**（错投的代价远大于漏投）
  · 单行失败不炸全批：逐行 try，汇总「成功/失败/跳过」，重跑即补投失败项
  · 成功一行才 append 一行 pipeline.csv（状态=已投）
  · 只投短名单 csv 里的行——不做任何「顺手多投」

台账口径：批量直投行的「版本文件」写死
    「猎聘在线简历，未生成一岗一版」
与一岗一版人工投递行区分开，复盘时一眼看出这批没做定制简历。

诚实边界：jobKind 的来源已由 schema+capture 确认（= 搜索响应的 jobType，实测恒为 "2"），
jobId 服务端要求 number（liepin_cli.apply_job 已转换）。但 **user-apply-job 的实际返回
形状仍未验证**——脚本按「code∈{0,200}/无 code → 成功」fail-soft 判，**首批实投建议
`--max 1` 试点**，投完去猎聘 App「我的-投递」人工核对后才放全批。
"""
import re
import sys
import csv
import argparse
import datetime
import pathlib

SKILL = pathlib.Path(__file__).resolve().parent.parent
PIPELINE = SKILL / "pipeline.csv"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import rank_pool as R          # noqa: E402  load_applied / find_applied / NOT_RECOMMEND_STATES
import liepin_cli as LC        # noqa: E402  apply_job + 异常类型

VERSION_CELL = "猎聘在线简历，未生成一岗一版"
FOLLOW_UP_DAYS = 7
BATCH_MARK = "批量直投"


def load_shortlist(path: pathlib.Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        # k 过滤：行尾多出的逗号会进 restkey(None) 列，不过滤会在 strip 处崩
        return [ {k: (v or "").strip() for k, v in row.items() if k}
                 for row in csv.DictReader(fh) ]


def plan(rows, applied_idx, max_n=15):
    """拆成 (将投, 跳过)。跳过原因全部显式，绝不静默丢行。"""
    planned, skipped = [], []
    for r in rows:
        rec = R.find_applied({"company": r["公司"], "title": r["岗位"], "url": r["URL"]},
                             applied_idx)
        if rec:
            skipped.append((r, f"台账已记录（{rec['status']}），跳过"))
            continue
        if not r.get("jobId"):
            skipped.append((r, "缺 jobId（不猜值）"))
            continue
        if not r.get("jobKind"):
            skipped.append((r, "缺 jobKind（不猜值；来源=搜索响应 jobType，实测恒为 \"2\"）"))
            continue
        planned.append(r)
    if len(planned) > max_n:
        for r in planned[max_n:]:
            skipped.append((r, f"超出单次上限 --max {max_n}，留待下轮"))
        planned = planned[:max_n]
    return planned, skipped


def _apply_failed(payload):
    """fail-soft 判定：只有明确报错才算失败（形状未验证，宁可报成功让人工核对）。"""
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return True
    code = payload.get("code")
    return code is not None and str(code) not in ("0", "200")


def _err_msg(payload):
    if isinstance(payload, dict):
        return str(payload.get("msg") or payload.get("message") or payload)[:200]
    return str(payload)[:200]


PIPE_KEYS = {
    "公司": ("公司",), "岗位": ("岗位",), "JD链接": ("链接", "JD"),
    "档位": ("档位",), "投递日": ("投递日",), "版本文件": ("版本文件",),
    "本版改了什么": ("本版", "改了"), "状态": ("状态",),
    "下次跟进日": ("下次跟进", "跟进"), "复盘备注": ("复盘", "备注"),
}


# 新建台账时的默认表头：与仓库 pipeline.csv 的真列名逐字一致
# （「状态」列名自带取值说明，rank_pool.load_applied 靠关键字匹配读它）
DEFAULT_HEADER = ["公司", "岗位", "JD链接", "档位(green/yellow/red)", "投递日", "版本文件",
                  "本版改了什么", "状态(待投/已投/已读/约面/挂/别投)", "下次跟进日", "复盘备注"]


def append_pipeline(ok_rows, today):
    """成功后追加台账。BOM 只在新文件写一次（追加模式再写会毒化续写行首列）。"""
    if not ok_rows:
        return 0
    new = not PIPELINE.exists() or PIPELINE.stat().st_size == 0
    if new:
        header = list(DEFAULT_HEADER)
        PIPELINE.parent.mkdir(parents=True, exist_ok=True)
        with PIPELINE.open("w", encoding="utf-8-sig", newline="") as fh:
            csv.writer(fh).writerow(header)
    else:
        with PIPELINE.open(encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh), None) or list(DEFAULT_HEADER)

    def col(*keys):
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return None

    cols = {k: col(*v) for k, v in PIPE_KEYS.items()}
    follow = today + datetime.timedelta(days=FOLLOW_UP_DAYS)
    with PIPELINE.open("a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        for r in ok_rows:
            row = [""] * len(header)

            def put(k, v):
                i = cols.get(k)
                if i is not None:
                    row[i] = v
            put("公司", r["公司"])
            put("岗位", r["岗位"])
            put("JD链接", r.get("URL", ""))
            put("档位", r.get("档位", ""))
            put("投递日", str(today))
            put("版本文件", VERSION_CELL)
            put("本版改了什么", f"{BATCH_MARK}（未做一岗一版改写）")
            put("状态", "已投")
            put("下次跟进日", str(follow))
            put("复盘备注", f"{BATCH_MARK}（短名单 {r.get('_batch', '')}）jobId={r.get('jobId','')}")
            w.writerow(row)
    return len(ok_rows)


def run_confirm(planned, today, apply_fn=LC.apply_job, sleep_between=0.0):
    """逐行实投。返回 (成功行, [(行, 错误)])。单行异常不炸全批。"""
    import time
    ok, failed = [], []
    for i, r in enumerate(planned, 1):
        tag = f"[{i}/{len(planned)}] {r['公司']} ｜ {r['岗位']}"
        try:
            payload = apply_fn(r["jobId"], r["jobKind"])
        except LC.McpAuthError as e:
            # 令牌失效是全局性的，继续跑只会重复报错——中断并把已成功的记账
            print(f"  ⛔ {tag}：{e}\n  中断本批（已成功的行照常记账），续跑前先更新 ~/.workbuddy/mcp.json")
            failed.append((r, f"MCP 认证失效：{e}"))
            for rest in planned[i:]:
                failed.append((rest, "本批因认证中断未执行（可重跑补投）"))
            break
        except Exception as e:
            print(f"  ❌ {tag}：{e}")
            failed.append((r, str(e)))
            continue
        if _apply_failed(payload):
            msg = _err_msg(payload)
            print(f"  ❌ {tag}：接口返回报错 → {msg}")
            failed.append((r, msg))
        else:
            print(f"  ✅ {tag}（jobId={r['jobId']}）已提交")
            ok.append(r)
        if sleep_between and i < len(planned):
            time.sleep(sleep_between)
    return ok, failed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="apply_shortlist.py")
    ap.add_argument("--list", required=True, help="find_jobs.py 生成的短名单 csv")
    ap.add_argument("--confirm", action="store_true", help="真正投递（缺省只打印清单）")
    ap.add_argument("--max", type=int, default=15, help="单次投递硬上限（默认 15）")
    args = ap.parse_args(argv)

    sl = pathlib.Path(args.list)
    if not sl.exists():
        print(f"❌ 短名单不存在：{sl}")
        return 2
    rows = load_shortlist(sl)
    batch = re.sub(r"\D", "", sl.stem)[-8:] or str(datetime.date.today()).replace("-", "")
    for r in rows:
        r["_batch"] = batch

    planned, skipped = plan(rows, R.load_applied(), args.max)

    mode = "🔴 实投（--confirm）" if args.confirm else "🔵 dry-run 预览（加 --confirm 才真投）"
    print("=" * 62)
    print(f"短名单批量直投 ｜ {datetime.date.today()} ｜ {mode}")
    print(f"名单：{sl} ｜ 共 {len(rows)} 行 → 将投 {len(planned)}，跳过 {len(skipped)}")
    print("=" * 62)
    for r in planned:
        print(f"  ⬜ {r['公司']} ｜ {r['岗位']} ｜ jobId={r['jobId']} jobKind={r['jobKind']}"
              f" ｜ {r.get('档位','')}档 {r.get('匹配分','')}分")
    for r, why in skipped:
        print(f"  ⏭️ {r['公司']} ｜ {r['岗位']} → 跳过：{why}")

    if not args.confirm:
        if not planned:
            print("\n没有可投项。")
            return 1
        print(f"\n（dry-run 结束。确认无误后：python scripts/apply_shortlist.py --list {sl.name} --confirm）")
        return 0
    if not planned:
        print("\n没有可投项，本批零动作。")
        return 1

    today = datetime.date.today()
    ok, failed = run_confirm(planned, today)
    n = append_pipeline(ok, today)
    print("-" * 62)
    print(f"结果：✅ 成功 {len(ok)}（已记账 pipeline.csv {n} 行）｜ ❌ 失败/未执行 {len(failed)}")
    for r, why in failed:
        if "因认证中断未执行" not in why:
            print(f"  · {r['公司']} ｜ {r['岗位']}：{why[:80]}")
    if ok:
        print(f"\n⚠️ 人工核对：打开猎聘 App「我的-投递」确认这 {len(ok)} 条真的到了（返回判定是 fail-soft 的）。")
        print("⚠️ 重跑 rank_pool.py 刷新榜单（已投递排除会生效）。")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
