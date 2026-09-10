# -*- coding: utf-8 -*-
"""
rank_pool.py — 生成 jd-pool/RANKED_LIST.md 排序推荐 list
==========================================================
用法：
    python rank_pool.py [--no-link-check]

逻辑：
  1. 扫描 jd-pool/{green,yellow}/*.md，解析首行 MATCHMETA
     （match_jd.py --save 写入：tier/score/coverage/company/title/date/source/url）
  2. 按 score 降序 → 推荐 list（用户预期：按简历和能力匹配度排序）
  3. 投递链接：MATCHMETA 的 url 渲染成可点击的「🔗 投递」；默认联网校验有效性
     - 404/410 → ❌ 已失效（岗位下线）；4xx 其他 → ⚠️ 疑似失效；403/429/超时 → ❓ 未确认（反爬，不算失效）
     - 校验结果按「url+日期」缓存到 jd-pool/.link_cache.json，同一天不重复请求
     - --no-link-check 关闭校验（离线/省时用）
  4. 时效：采集日距今 ≥30 天 → 标「待复核」；≥60 天由 cleanup_pool.py 归档
  5. red 档不进 list，只报计数（防重复研究同一个坑）
铁律：分数只是排序依据，投递决策以三档结论 + 人工判断为准；
     链接失效 ≠ 岗位失效，JD 原文摘录才是匹配依据。
"""
import re
import sys
import json
import argparse
import datetime
import pathlib
import urllib.request
import urllib.error

SKILL = pathlib.Path(__file__).resolve().parent.parent
POOL = SKILL / "jd-pool"
OUT = POOL / "RANKED_LIST.md"
CACHE = POOL / ".link_cache.json"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import match_jd as M  # noqa: E402  （权重唯一真源，用于生成表头文案）

STALE_REVIEW_DAYS = 30   # ≥30 天标「待复核」

META_PAT = re.compile(
    r"<!--\s*MATCHMETA\s+tier=(\w+)\s+score=([\d.]+)\s+coverage=(\S+)\s+"
    r"company=(.+?)\s+title=(.+?)\s+date=(\S+)\s+source=(\S+?)(?:\s+url=(\S+?))?(?:\s+\w+=\S+)*\s*-->"
)

TIER_ICON = {"green": "🟢", "yellow": "🟡"}
ACTION = {
    "green": "可进一岗一版组版",
    "yellow": "差距分析 + 曲线路径（内推/作品集）",
}

# ── 分轮投递策略（2026-09-10 接入）───────────────────────────────────────────
# 策略不是技能规则，是用户自维护的个人文件 strategy.md。
# 2026-09-10：该文件已从 career-facts/ 迁到项目目录（与用户手写的 jd_raw/ 同级）。
# 路径解析的唯一真源是 match_jd.py 的 STRATEGY_CANDIDATES + find_strategy_file()，本脚本不自行拼路径。
# 本脚本只读它，把 META 里的 purpose 轮次标注**呈现出来**——不改分数、不改档位。
# 教训：此前 rank_pool 完全没有轮次维度，但 SKILL.md 曾写「榜单可按轮次过滤」，
#   属文档承诺了未实现的功能。现补齐，并让表头说明随策略文件动态生成。
PURPOSE_ORDER = ["占坑", "练手", "熟流程", "主攻"]
# 「不投」不是策略轮次，是**显式排除**标注（2026-09-10 用户裁定后新增）。
# 用途：真目标大厂的**非点名岗**——档位可能很高（🟢），但按分轮纪律「严禁投真目标大厂
# （同岗半年冷却）」，投出去会烧掉第一印象。故必须从推荐排序剔除、单列一节。
# 与 dead 的区别：dead 是岗位失效（客观），不投是策略排除（主观），JD 原文都保留。
NO_SUBMIT = "不投"


def load_strategy_note() -> str:
    """读策略文件生成表头说明；文件不存在则说明未接入。

    2026-09-10：不再自行拼 `FACTS_DIR/strategy.md`，统一走 match_jd.find_strategy_file()
    （项目目录优先、旧位置兜底），避免两处路径定义漂移。
    """
    path = M.find_strategy_file()
    if path is None:
        where = " / ".join(f"`{p}`" for p in M.STRATEGY_CANDIDATES)
        return (f"未找到策略文件 strategy.md（已查找：{where}）——轮次列显示为「—」。"
                "该文件是用户自维护的分轮投递策略，技能只读不写。")
    txt = path.read_text(encoding="utf-8", errors="replace")
    found = [p for p in PURPOSE_ORDER if p in txt]
    try:
        shown = "~/" + path.relative_to(pathlib.Path.home()).as_posix()
    except ValueError:
        shown = str(path)
    return (f"轮次来自 `{shown}`（" + " / ".join(found) + "），"
            "由 `match_jd.py --purpose` 写入；**轮次与分数无关，只影响投递时机与准备深度**。"
            "未标注的岗显示「—」，需人工补。")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# 前端渲染（SPA）站点：任何 /job/* 路径都返回 200，HTTP 状态码无法判断岗位是否还在招
# 实测 2026-09-08：https://www.quanzhi.com/job/THIS_IS_A_FAKE_ID_123 也返回 200
# 注意（2026-09-10 修正）：这不代表「只能人工确认」——域名规范化后多数站点正文可读，
#   见 normalize_url() 与 check_link() 的 GET 正文扫词逻辑。
SPA_HOSTS = ("liepin.com", "zhipin.com", "quanzhi.com", "iguopin.com",
             "careers.tencent.com", "job.tencent.com")

# 其中「桌面端能读到真实岗位详情页」的站点（实测确认，非猜测）
# 2026-09-10 实测：https://www.liepin.com/job/1984901035.shtml 返回 169879B，
#   title 含完整岗位名+公司名（「【上海 AI产品经理（电商/CRM）招聘】-上海凯淳实业…」），
#   正文含「电商/CRM/闵行」等岗位专属词 → 是真详情页而非通用壳。
#   quanzhi 桌面端同理（50KB，含薪资与「已结束」状态）。这类站点应判 ok 而非 spa。
# 未实测的站点（boss/iguopin/腾讯等）保持保守判 spa，不凭猜测放宽。
READABLE_HOSTS = ("liepin.com", "quanzhi.com")

# 正文下线词（用于 GET 正文扫词判定岗位状态）
# 2026-09-10 补「已结束」：实测 quanzhi 页面状态就写作裸的「已结束」
#   （如 `AI产品经理 1-1.5万 已结束`），旧词表只有「招聘已结束」「职位已结束」，因此漏检。
#
# ⚠️ 设计原则：**精确优先，宁可漏检不可误杀**
#   2026-09-10 踩坑：初版按「宁宽勿窄」把「404」也放进词表，结果清缓存重跑后
#   9 个在招岗位被误判 dead（页面里的 `404px`、`statusCode===404` 等 JS/CSS 数字串
#   全都命中）。误杀的代价是把有效岗位从投递清单里删掉，比漏检严重得多。
#   故：**只收「岗位语境下才可能出现的短语」**，纯技术词（404/500/error）一律不收。
OFFLINE_WORDS = ("岗位已下线", "职位已下线", "该职位已关闭", "职位不存在", "岗位不存在",
                 "已停止招聘", "招聘已结束", "职位已结束", "已结束")

# 空壳页阈值：移动端/SPA 壳站常返回 2-5KB 且无正文，无法判断岗位状态
SHELL_MIN_BYTES = 12000


def normalize_url(url: str) -> str:
    """把移动端/空壳域名规范化为可校验的桌面端 URL（同岗位，功能等价）。

    2026-09-10 踩坑：池内 quanzhi 岗位存的是移动端 m.quanzhi.com/job/detail/<id>，
    只返回 3912B 空壳页（无 title、无正文），导致历次校验拿不到任何信息、
    两个岗位下线都漏检。换桌面端 www.quanzhi.com/job/<id> 返回 50467B
    服务端渲染完整页面，正文含「已结束」，可直接判定。
    """
    m = re.match(r"https?://m\.quanzhi\.com/job/detail/([0-9a-zA-Z]{16,})", url)
    if m:
        return f"https://www.quanzhi.com/job/{m.group(1)}"
    return url


def load_cache():
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_cache(c):
    CACHE.write_text(json.dumps(c, ensure_ascii=False, indent=0), encoding="utf-8")


def _fetch(url: str, method: str = "HEAD"):
    """GET 时读足量字节。

    2026-09-10 踩坑：原为 read(20000)，实测 quanzhi 完整页约 47-56KB，
    且「已结束」字样在页面的字节位 17810 / 21060 / 24174 / 28227 不等——
    寻序智能那页的**两处都在 20000 字节之外**，只读 20000 会静默漏检。
    靠「字节位置恰好靠后」造成的漏检极其隐蔽：看日志会以为扫过了。
    """
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    req.get_method = lambda: method
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, (r.read(200000) if method == "GET" else b"")


def check_link(url: str, cache: dict, today: str, meta_dead: bool = False):
    """返回 (标记, 说明)。
    标记：
      dead    404/410 ｜ 正文命中下线词 ｜ META 已标 status=dead → 岗位已下线/已结束
      spa     200 但正文读不到（空壳页）或无下线词仍无法证实 → 需人工点开确认
      ok      200 且服务端渲染页面无下线词 → 页面可访问
      suspect 其他 4xx
      unknown 403/405/429/超时/拒绝校验（多为反爬，一律不算失效）
      none    没抓到链接

    诚实边界：HTTP 校验只能证伪（404 是硬证据），不能证实岗位仍在招。
    2026-09-10 修正：旧版对 SPA 站点（liepin/quanzhi 等）只做 HEAD 就直接判 spa，
    导致「已结束」这类正文信号永远扫不到。现改为**所有 200 响应都 GET 正文扫词**，
    只有正文读不到（空壳）或读到了但无下线词，才降级为 spa 人工确认。
    另：URL 先经 normalize_url() 规范化（移动端壳域名 → 桌面端可读域名）。
    """
    if not url or url == "none":
        return "none", "未抓到链接"
    if meta_dead:
        return "dead", "META 标记 status=dead（人工核实岗位已结束）"
    url = normalize_url(url)
    key = f"{url}|{today}"
    if key in cache:
        return cache[key][0], cache[key][1]
    status, body, note = None, b"", ""
    try:
        status, body = _fetch(url, "HEAD")
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 429):  # 反爬/不接受 HEAD，不算失效
            status, note = None, "站点拒绝校验"
        else:
            status = e.code
    except Exception:
        status, note = None, "超时/无法连接"

    if status in (404, 410):
        mark, note = "dead", "岗位已下线"
    elif status is not None and 400 <= status < 500:
        mark, note = "suspect", f"HTTP {status}"
    elif status == 200:
        # 不管是否 SPA，一律 GET 正文扫下线词（旧版对 SPA 直接跳过 = 漏检根因）
        try:
            _, body = _fetch(url, "GET")
        except Exception:
            body = b""
        txt = body.decode("utf-8", errors="ignore")
        hit = next((w for w in OFFLINE_WORDS if w in txt), None)
        if hit:
            # 附上命中处的上下文片段：误判时能一眼看出是不是岗位状态词
            # （2026-09-10 教训：无证据片段的 dead 判定无法复核，9 个误杀查了半天）
            i = txt.find(hit)
            ctx = re.sub(r"<[^>]+>", " ", txt[max(0, i - 45):i + len(hit) + 45])
            ctx = re.sub(r"\s+", " ", ctx).replace("|", "/").strip()
            mark, note = "dead", f"命中「{hit}」｜…{ctx}…"
        elif len(body) < SHELL_MIN_BYTES:
            mark, note = "spa", f"空壳页面（{len(body)}B，无正文），需人工点开确认"
        elif any(h in url for h in SPA_HOSTS) and not any(h in url for h in READABLE_HOSTS):
            mark, note = "spa", "前端渲染站点，正文无下线词但仍不能证实岗位在招，需人工确认"
        elif any(h in url for h in READABLE_HOSTS):
            mark, note = "ok", f"正文完整（{len(body) // 1000}KB）无下线标识，仍建议点开确认"
        else:
            mark, note = "ok", "页面可访问"
    else:
        mark = "unknown"
        note = note or "站点拒绝校验"
    cache[key] = [mark, note]
    return mark, note


LINK_MARK = {
    "ok": ("🔗 投递", ""),
    "spa": ("🔗 投递", "❔"),
    "dead": ("❌ 已结束", ""),
    "suspect": ("⚠️ 疑似失效", ""),
    "unknown": ("🔗 投递", "❓"),
    "none": ("🔗 无链接", ""),
    # --no-link-check 时使用：不能显示成 🔗 可投递（旧行为如此，会误导读成「已核实可投」）
    "skip": ("⬜ 未校验", ""),
}


def parse_pool():
    rows, legacy, red_count = [], [], 0
    if not POOL.exists():
        return rows, legacy, red_count
    for tier_dir in ("green", "yellow"):
        d = POOL / tier_dir
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            m = META_PAT.search(f.read_text(encoding="utf-8", errors="replace")[:2000])
            if not m:
                legacy.append(f)
                continue
            t, score, cov, comp, title, date, source, url = m.groups()
            if t != tier_dir:
                t = tier_dir
            # 双向匹配字段（2026-09-10 起由 match_jd.py/rescore_pool.py 写入；老文件缺省）
            head = f.read_text(encoding="utf-8", errors="replace")[:2000]
            mm = re.search(r"<!--\s*MATCHMETA(.*?)-->", head, re.S)
            blob = mm.group(1) if mm else ""

            def _f(k, dflt=""):
                x = re.search(rf"\b{k}=(\S+)", blob)
                return x.group(1) if x else dflt
            rows.append({
                "tier": t, "score": float(score), "coverage": cov,
                "company": comp, "title": title, "date": date,
                "source": source, "url": (url or "").strip(),
                "mode": _f("mode", "jd"), "hit": _f("hit", "?"),
                "fill": _f("fill", "?"), "gap": _f("gap", "?"),
                # must：必备层覆盖 `命中/必备总数`（P1 独立指标，**不进匹配分**）。
                # 与「命中/可补/真缺」的区别：那个按 18 个关键词类目算，
                # 这个只看 JD 里写死的硬性条款，且有细粒度技能点兜底。
                "must": _f("must", ""),
                "purpose": _f("purpose", ""),
                # truncated：JD 完整性标记（match_jd.py 完整性闸门写入）
                #   1=残缺（硬证据）/ suspect=疑似残缺 / recheck=补录后待重算 / 0=通过
                "trunc": _f("truncated", "0"),
                # status=dead：人工核实岗位已结束（不依赖联网校验，优先级最高）
                "dead": _f("status", "").lower() == "dead",
                "file": str(f.relative_to(SKILL)).replace("\\", "/"),
            })
    red_dir = POOL / "red"
    if red_dir.exists():
        red_count = len(list(red_dir.glob("*.md")))
    rows.sort(key=lambda r: (-r["score"], r["company"]))
    return rows, legacy, red_count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-link-check", action="store_true", help="关闭联网链接校验（离线/省时）")
    args = ap.parse_args()

    rows, legacy, red_count = parse_pool()
    today = datetime.date.today()
    cache = load_cache()
    today_s = str(today)

    dead = suspect = unknown = spa = stale = 0
    for r in rows:
        if args.no_link_check:
            # 离线模式也要尊重人工核实的 status=dead——它是事实，与是否联网无关
            # （2026-09-10 踩坑：旧写法在离线模式下把 dead 岗位又放回推荐列表）
            if r["dead"]:
                r["mark"], r["note"] = "dead", "META 标记 status=dead（人工核实岗位已结束）"
            else:
                r["mark"], r["note"] = ("skip" if r["url"] and r["url"] != "none" else "none"), ""
        else:
            r["mark"], r["note"] = check_link(r["url"], cache, today_s, meta_dead=r["dead"])
        dead += r["mark"] == "dead"
        suspect += r["mark"] == "suspect"
        unknown += r["mark"] == "unknown"
        spa += r["mark"] == "spa"
        try:
            age = (today - datetime.date.fromisoformat(r["date"])).days
        except Exception:
            age = 0
        r["age"] = age
        r["stale"] = age >= STALE_REVIEW_DAYS
        stale += r["stale"]
    if not args.no_link_check:
        save_cache(cache)

    # 已结束/已下线的岗位从推荐排序中剔除，单列一节（信息不丢，但不再占投递位）
    # 同时剔除 purpose=不投 的岗（策略排除，非失效）——否则 🟢 档的不投岗会混在推荐位里
    dead_rows = [r for r in rows if r["mark"] == "dead"]
    nosubmit_rows = [r for r in rows if r["mark"] != "dead" and r["purpose"] == NO_SUBMIT]
    active = [r for r in rows
              if r["mark"] != "dead" and r["purpose"] != NO_SUBMIT]

    lines = [
        "# JD 排序推荐 List",
        "",
        f"> 生成时间：{today} ｜ 生成方式：`python scripts/rank_pool.py`（每次 match_jd.py --save 入库后重跑刷新）",
        # 权重文案从唯一真源动态取，避免改权重后表头文案漂移（2026-09-10 踩过）
        f"> 排序依据：**匹配分 = 档位基础分（🟢{M.TIER_BASE['green']:.0f} / 🟡{M.TIER_BASE['yellow']:.0f}）"
        f"+ 双向覆盖率 × {M.COVERAGE_WEIGHT:.0f}**，按简历与能力匹配度降序。"
        f"（覆盖率是分本体，档位只得对 green 的确认加分 → green 满分 100，yellow 满分 "
        f"{M.TIER_BASE['yellow'] + M.COVERAGE_WEIGHT:.0f}）",
        "> 覆盖率口径（2026-09-10 升级）：**命中** = JD 有 ∧ 简历有（计 1.0）｜**可补** = JD 有 ∧ 简历无 ∧ 事实库有证据（计 0.5，组版时补进去）｜**真缺** = 三方都没有（0 分，不补）。**旧口径只统计 JD 侧写了多少词，不比对简历，会高估 JD 写得花哨的岗位**。",
        "> **必备层列（2026-09-10 新增）**：`命中/必备总数（百分比）` = JD 硬性条款里我覆盖了多少，由细粒度技能点按「必备/优先/加分项」三层切分后统计（`必备` 段去掉「…者优先」；`加分项` 不计入）。**这是独立指标，不进匹配分**——「这岗我命中多」和「这岗的硬门槛我够了」是两件事，后者 <50% 标 ⚠️。跨岗位的学习优先级看 `jd-pool/SKILL_RADAR.md`。",
        "> **JD 完整性闸门（2026-09-10 新增）**：残缺 JD 的缺口数是「**未探明**」而不是 0——源文本缺了大半时，探不到缺口 ≠ 没有缺口。标记：⚠️ 检出截断痕迹/正文过短 ｜ ❓ 结构可疑（有职责段却无要求段且篇幅偏短）｜ 🔄 已补录待重算。补录命令：`python scripts/patch_jd.py --refetch-all`。",
        f"> {load_strategy_note()}",
        f"> 轮次列取值：`占坑` / `练手` / `熟流程` / `主攻`（来自 strategy.md）｜"
        f"**`{NO_SUBMIT}`** = 策略性排除（真目标大厂的非点名岗，不进推荐排序）｜`—` = 未标注。",
        "> 分数只是排序依据，投递决策以三档结论 + 人工判断为准；red 档不进本 list。",
        "> 链接列：🔗 页面可访问（正文已读到、无下线标识）｜ ❔ 需人工点开确认（空壳页或未实测站点，正文读不到）｜ ❌ 已结束 ｜ ⚠️ 疑似失效 ｜ ❓ 站点拒绝校验（反爬，不算失效）｜ ⬜ 未校验（跑了 --no-link-check）。",
        "> **岗位状态校验的能力边界（2026-09-10 重写）**：能自动判定 **已结束/下线**——① HTTP 404/410；② GET 正文命中岗位下线词（已结束/已下线/停止招聘等）。为此做了三件事：**URL 域名规范化**（`m.quanzhi.com` 空壳域名 → `www.` 可读域名）、**对所有站点都扫正文**（旧版对 SPA 站直接跳过 = 漏检根因）、**读足量字节**（旧版读 20000 字节，而「已结束」实际出现在 17810~28227 字节不等，会静默漏检）。",
        "> **不能证实「仍在招」**：正文无下线词只说明暂时没读到。**下线判定精确优先**——词表只收岗位语境下才出现的短语（曾因收录「404」误杀 9 个在招岗位），宁可漏检交人工，不可误杀好岗。",
        f"> 时效：采集满 {STALE_REVIEW_DAYS} 天标「待复核」，满 60 天由 `cleanup_pool.py --apply` 归档。",
        "",
        f"## 推荐投递排序（{len(active)} 个岗位）",
        "",
    ]
    if active:
        lines += [
            "| 排名 | 匹配分 | 档位 | 轮次 | 公司 / 岗位 | 投递链接 | 命中 / 可补 / 真缺 | 必备层 | 采集日 | 时效 | 来源 | 详情文件 |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for i, r in enumerate(active, 1):
            label, flag = LINK_MARK[r["mark"]]
            if r["url"] and r["url"] != "none":
                # 显示链接也规范化：否则 {m.quanzhi.com} 的移动端壳域名点开是空白页
                cell = f"[{label}]({normalize_url(r['url'])})"
            else:
                cell = label
            if flag:
                cell += f" {flag}"
            age_cell = f"⚠️ {r['age']}天待复核" if r["stale"] else f"{r['age']}天"
            cov_cell = (f"**{r['hit']}** / {r['fill']} / {r['gap']}"
                        if r["mode"] == "bi" else f"{r['coverage']} ⚠️旧口径")
            # JD 残缺时缺口数是「未探明」——必须标在同一格里，
            # 否则「真缺口 0」会被读成「这岗我全会」，正是闸门要拦的假阴性
            if r["trunc"] == "1":
                cov_cell += " ⚠️JD残缺·未探明"
            elif r["trunc"] == "suspect":
                cov_cell += " ❓疑残"
            elif r["trunc"] == "recheck":
                cov_cell += " 🔄待重算"
            pur_cell = r["purpose"] if r["purpose"] else "—"
            # 必备层覆盖：独立指标，帮读者区分「这岗我命中多」与「这岗的硬门槛我够了」
            if r["must"] and r["must"] != "none":
                try:
                    m_hit, m_tot = (int(x) for x in r["must"].split("/"))
                    pct = int(round(m_hit * 100 / m_tot)) if m_tot else 0
                    must_cell = f"{m_hit}/{m_tot}（{pct}%）" + (" ⚠️" if pct < 50 else "")
                except ValueError:
                    must_cell = r["must"]
            else:
                must_cell = "—" if not r["must"] else r["must"]
            lines.append(
                f"| {i} | **{r['score']}** | {TIER_ICON[r['tier']]} | {pur_cell} | {r['company']} / {r['title']} "
                f"| {cell} | {cov_cell} | {must_cell} | {r['date']} | {age_cell} | {r['source']} | {r['file']} |"
            )
        lines += ["", "### 下一步动作", ""]
        for i, r in enumerate(active, 1):
            extra = ""
            if r["stale"]:
                extra = f"（⚠️ 采集于 {r['age']} 天前，建议复核岗位状态）"
            lines.append(f"{i}. **{r['company']} / {r['title']}**（{TIER_ICON[r['tier']]} {r['score']} 分）→ {ACTION[r['tier']]}{extra}")
    else:
        lines.append("_池子是空的——用 match_jd.py --save 入库第一批 JD 后重跑本脚本。_")

    if dead_rows:
        lines += [
            "", f"## ⛔ 已结束 / 不可投递（{len(dead_rows)} 个）", "",
            "> 链接已确认下线，或页面正文显示「已结束」。**JD 原文保留**"
            "（可作岗位画像、行业调研与组版语料参考），但**不再占投递位**。",
            "> 这类岗位多半是「采集时还在招、投前已下线」——说明**入库不等于有效，投递前必须复核**。",
            "",
            "| 匹配分 | 档位 | 轮次 | 公司 / 岗位 | 状态证据 | 详情文件 |",
            "|---|---|---|---|---|---|",
        ]
        for r in dead_rows:
            pur = r["purpose"] if r["purpose"] else "—"
            lines.append(
                f"| {r['score']} | {TIER_ICON[r['tier']]} | {pur} | {r['company']} / {r['title']} "
                f"| {r['note']} | {r['file']} |"
            )

    if nosubmit_rows:
        lines += [
            "", f"## 🚫 本轮不投（{len(nosubmit_rows)} 个）", "",
            "> **策略性排除，不是岗位失效。** 这些是真目标大厂的**非点名岗**——档位可能不低，"
            "但按分轮纪律「**严禁投真目标大厂**（同岗半年冷却，第一印象烧掉后窗口期内再投难度上升）」，"
            "本轮不投。",
            "> 投递位留给点名岗（占坑）与目标行业（主攻）。**JD 原文保留**，供后续窗口期复用；"
            "若想改判，直接改该文件 MATCHMETA 里的 `purpose=`。",
            "",
            "| 匹配分 | 档位 | 轮次 | 公司 / 岗位 | 详情文件 |",
            "|---|---|---|---|---|",
        ]
        for r in nosubmit_rows:
            lines.append(
                f"| {r['score']} | {TIER_ICON[r['tier']]} | {r['purpose']} | {r['company']} / {r['title']} "
                f"| {r['file']} |"
            )

    if legacy:
        lines += ["", f"## 待补分（{len(legacy)} 个，缺 MATCHMETA 元数据）", ""]
        for f in legacy:
            lines.append(f"- {f.relative_to(SKILL)} → 重跑 `python scripts/match_jd.py <jd文件> --save` 补分")

    lines += ["", "---", f"🔴 red 档黑名单：{red_count} 个（别投的坑，长期保留防重复研究，不进排序）。", ""]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"💾 已生成 {OUT}")
    print(f"   推荐投递 {len(active)} ｜ 已结束剔除 {len(dead_rows)} ｜ 策略排除(不投) {len(nosubmit_rows)} "
          f"｜ 待补分 {len(legacy)} ｜ red 黑名单 {red_count}")
    if not args.no_link_check:
        ok = len(rows) - dead - suspect - unknown - spa
        print(f"   链接校验：可访问 {ok} ｜ 待人工确认(SPA) {spa} ｜ 已结束 {dead} ｜ 疑似 {suspect} ｜ 未确认 {unknown}")
    n_trunc = sum(1 for r in rows if r["trunc"] == "1")
    n_susp = sum(1 for r in rows if r["trunc"] == "suspect")
    n_rc = sum(1 for r in rows if r["trunc"] == "recheck")
    if n_trunc or n_susp or n_rc:
        print(f"   JD 完整性：⚠️残缺 {n_trunc} ｜ ❓疑残 {n_susp} ｜ 🔄待重算 {n_rc}"
              f"（这些岗位的缺口数是「未探明」，不可当 0 读）")
    print(f"   待复核（≥{STALE_REVIEW_DAYS}天）：{stale}")


if __name__ == "__main__":
    main()
