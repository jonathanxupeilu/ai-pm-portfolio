#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
liepin_cli.py — 猎聘官方 MCP 的纯 stdlib 命令行客户端
=====================================================
找岗链路的主数据源。技能里说的「猎聘 CLI」就是本文件：它不逆向、不伪装指纹，
只走猎聘官方 MCP（streamable-http / JSON-RPC 2.0）的 sanctioned 通道：
  search-jobs / user-search-job（搜岗）  user-apply-job（投递）  my-resume（体检）

令牌唯一真源 = ~/.workbuddy/mcp.json 的 mcpServers["liepin-mcp"]
  （url + headers["x-user-token"]）。令牌 90 天过期，**永不打印、永不写进仓库**。

用法：
    python liepin_cli.py resume                              # 令牌/连通体检
    python liepin_cli.py probe                               # tools/list 原样打印
    python liepin_cli.py search --job-name AI产品经理 --address 上海
    python liepin_cli.py search --job-name AI产品经理 --page 2 --json
    python liepin_cli.py search --job-name X --raw-out capture.json   # 抓原始响应定形状（capture 件勿入库）
    python liepin_cli.py apply --job-id 79448235 --job-kind C0001 [--dry-run]

退出码：0 成功 / 2 参数错 / 3 令牌失效 / 4 限流重试耗尽 / 5 调用失败
"""
import argparse
import json
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

MCP_JSON = pathlib.Path.home() / ".workbuddy" / "mcp.json"
SERVER_KEY = "liepin-mcp"
FALLBACK_URL = "https://open-agent.liepin.com/mcp/user"
QPM_SLEEP = 1.2                 # 60次/分共享配额（搜索+查看+投递共用）→ 进程内自限速
RETRY_WAITS = (3, 10, 30)       # 命中 429001「请求过于频繁」的三级退避
_TIMEOUT = 40


class McpError(Exception):
    pass


class McpAuthError(McpError):
    pass


class McpRateLimit(McpError):
    pass


_last_call_ts = [0.0]


def _pace():
    """进程内全局限速：任意两次 MCP 调用间隔不小于 QPM_SLEEP。"""
    gap = time.time() - _last_call_ts[0]
    if gap < QPM_SLEEP:
        time.sleep(QPM_SLEEP - gap)
    _last_call_ts[0] = time.time()


def load_mcp_config(path: pathlib.Path = MCP_JSON):
    """从 ~/.workbuddy/mcp.json 读 (url, token)。缺文件/缺键 → 中文指引的 McpAuthError。"""
    if not path.exists():
        raise McpAuthError(
            f"未找到配置文件 {path}；猎聘 MCP 未配置。请在猎聘授权页获取令牌并写入该文件。")
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as e:
        raise McpAuthError(f"配置文件 {path} 解析失败：{e}")
    srv = cfg.get("mcpServers", {}).get(SERVER_KEY)
    if not srv:
        raise McpAuthError(f"{path} 里没有 mcpServers['{SERVER_KEY}'] 条目。")
    url = srv.get("url") or FALLBACK_URL
    token = (srv.get("headers") or {}).get("x-user-token")
    if not token:
        raise McpAuthError(f"mcpServers['{SERVER_KEY}'].headers 缺 x-user-token。")
    return url, token


def _parse_streamable(raw: bytes):
    """streamable-http 双格式：整段 JSON 直接 loads；SSE 取最后一个 data: 帧。"""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        raise McpError("空响应")
    if text.startswith("{") or text.startswith("["):
        return json.loads(text)
    frames = [ln[5:].strip() for ln in text.splitlines() if ln.startswith("data:")]
    for f in reversed(frames):
        try:
            return json.loads(f)
        except ValueError:
            continue
    raise McpError(f"无法解析响应：{text[:200]}")


def rpc(url, token, method, params, req_id=1):
    """单次 JSON-RPC 2.0 POST。中文安全的关键：body 用 ensure_ascii=False 的 utf-8 bytes 直发。"""
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
                      ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json, text/event-stream",
        "x-user-token": token,
    })
    _pace()
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return _parse_streamable(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise McpAuthError(
                "令牌失效（HTTP %d）：去猎聘授权页续期（有效期 90 天），再更新 %s 里的 x-user-token。"
                % (e.code, MCP_JSON))
        raise McpError(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise McpError(f"网络错误：{e.reason}")


def unwrap_payload(result):
    """MCP tool result → 内层业务 dict。result.content[0].text 往往是「JSON 字符串」，再 loads 一次。"""
    if not isinstance(result, dict):
        return result
    content = result.get("result", result)
    blocks = content.get("content") if isinstance(content, dict) else None
    text = None
    if blocks:
        for b in blocks:
            if b.get("type") == "text":
                text = b.get("text")
                break
    if text is None:
        return content
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw_text": text}


def _is_rate_limited(payload):
    if isinstance(payload, dict):
        code = payload.get("code")
        msg = str(payload.get("msg", ""))
        if code == 429001 or "过于频繁" in msg:
            return True
    return False


def call_tool(url, token, name, arguments):
    """tools/call → 解包业务 payload；命中限流按 RETRY_WAITS 退避重试。抛 McpAuthError / McpRateLimit / McpError。"""
    last = None
    for attempt in range(len(RETRY_WAITS) + 1):
        resp = rpc(url, token, "tools/call", {"name": name, "arguments": arguments}, req_id=attempt + 1)
        if isinstance(resp, dict) and resp.get("error"):
            raise McpError(f"{name} 报错：{resp['error']}")
        payload = unwrap_payload(resp)
        if _is_rate_limited(payload):
            last = payload
            if attempt < len(RETRY_WAITS):
                time.sleep(RETRY_WAITS[attempt])
                continue
            raise McpRateLimit(f"{name} 持续限流（429001），退避 {RETRY_WAITS} 后仍未放行。")
        return payload
    raise McpRateLimit(str(last))


# ── 搜岗结果解析：唯一耦合真实响应形状的函数 ──────────────────────────────────
# 2026-09-18 已用真实 capture 定稿（user-search-job，AI产品经理，20 条）：
#   形状 {"data": {"list": [ {jobId(int), jobType(str,实测恒为"2"), jobName, company,
#   location, salary, education, workYears, industry, companyTags[], financingStage,
#   companySize, companyLogo, jobDetailUrl} ]}, "code": 0}
#   · jobDetailUrl 形如 https://www.liepin.com/job/19<jobId>.shtml?mscid=soai_pc_001
#     ——查询串是追踪参数，比对/展示前剥掉
#   · **payload 不带 JD 正文** → 打分正文一律回源抓详情页（find_jobs.jd_text_for）
#   · 无 total/页数字段 → 翻页直到返回空列表为止
_ID_KEYS = ("jobId", "job_id", "id", "positionId", "jobNumber")
_TITLE_KEYS = ("jobName", "title", "job_name", "name", "positionName")
_COMP_KEYS = ("company", "companyName", "compName", "corpName", "company_name")
_URL_KEYS = ("jobDetailUrl", "jobUrl", "job_url", "url", "h5Url", "detailUrl", "link", "mUrl")
_KIND_KEYS = ("jobType", "jobKind", "job_kind", "kind", "dataType")
_SAL_KEYS = ("salary", "salaryDesc", "salaryStr", "package")
_DESC_KEYS = ("description", "duty", "jobDesc", "requirement", "content", "responsibility")


def _pick(d, keys, default=""):
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return default


def _find_lists(obj, depth=0):
    """在任意嵌套结构里找出「像岗位数组」的 list[dict]。"""
    out = []
    if depth > 6:
        return out
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj):
            out.append(obj)
        for x in obj:
            out.extend(_find_lists(x, depth + 1))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.extend(_find_lists(v, depth + 1))
    return out


def parse_search_rows(payload):
    """把 search 的业务 payload 归一为 list[dict]：{jobId,title,company,url,jobKind,salary,description,raw}。
    形状未知时尽力探测，绝不崩；定稿以 capture 为准。"""
    # 内层 result 可能又是 JSON 字符串
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        try:
            payload = json.loads(payload["result"])
        except ValueError:
            pass

    lists = _find_lists(payload)
    rows_src = max(lists, key=len) if lists else []

    rows = []
    for d in rows_src:
        url = str(_pick(d, _URL_KEYS))
        url = re.sub(r"([?&])mscid=[^&]*", r"\1", url)          # 剥追踪参数
        url = re.sub(r"[?&]+$", "", url).rstrip("?&")
        rows.append({
            "jobId": str(_pick(d, _ID_KEYS)),
            "title": str(_pick(d, _TITLE_KEYS)),
            "company": str(_pick(d, _COMP_KEYS)),
            "url": url,
            "jobKind": str(_pick(d, _KIND_KEYS)),
            "salary": str(_pick(d, _SAL_KEYS)),
            "description": str(_pick(d, _DESC_KEYS)),
            "raw": d,
        })
    return rows


# ── 高层动作 ────────────────────────────────────────────────────────────
def search_jobs(job_name=None, company=None, address="上海", page=1,
                edu_level=None, work_experience=None, salary_floor=None, salary_cap=None,
                comp_nature=None, tool="search-jobs"):
    url, token = load_mcp_config()
    args = {}
    if job_name:
        args["jobName"] = job_name
    if company:
        args["companyName"] = company
    if address:
        args["address"] = address
    if edu_level:
        args["eduLevel"] = edu_level
    if work_experience:
        args["workExperience"] = work_experience
    if salary_floor is not None:
        args["salaryFloor"] = salary_floor
    if salary_cap is not None:
        args["salaryCap"] = salary_cap
    if comp_nature:
        args["compNature"] = comp_nature
    if tool == "user-search-job":
        # 官方 schema（2026-09-18 tools/list 实拉）：**page 约定 0 = 第 1 页**。
        # 本函数与上层 find_jobs 一律按人类习惯传 1 起的页码，在这里统一减 1。
        args["page"] = max(0, page - 1)
    payload = call_tool(url, token, tool, args)
    return payload, parse_search_rows(payload)


def apply_job(job_id, job_kind):
    # schema：jobId=number（必填）、jobKind=string 职位类型。
    # 短名单 csv 里 jobId 存成文本（csv 无类型），不转 int 会被服务端拒。
    url, token = load_mcp_config()
    return call_tool(url, token, "user-apply-job",
                     {"jobId": int(str(job_id).strip()), "jobKind": str(job_kind).strip()})


def my_resume():
    url, token = load_mcp_config()
    return call_tool(url, token, "my-resume", {})


def tools_list():
    url, token = load_mcp_config()
    return rpc(url, token, "tools/list", {})


def main(argv=None):
    ap = argparse.ArgumentParser(prog="liepin_cli.py")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search")
    s.add_argument("--job-name", default=None)
    s.add_argument("--company", default=None)
    s.add_argument("--address", default="上海")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--tool", default="search-jobs", choices=["search-jobs", "user-search-job"])
    s.add_argument("--json", action="store_true", help="只输出归一后的 rows(JSON)")
    s.add_argument("--raw-out", default=None, help="把原始业务 payload 写到该文件（capture 用，勿入库）")

    a = sub.add_parser("apply")
    a.add_argument("--job-id", required=True)
    a.add_argument("--job-kind", required=True)
    a.add_argument("--dry-run", action="store_true")

    sub.add_parser("resume")
    sub.add_parser("probe")

    args = ap.parse_args(argv)
    try:
        if args.cmd == "resume":
            p = my_resume()
            txt = p.get("_raw_text") if isinstance(p, dict) and "_raw_text" in p else json.dumps(p, ensure_ascii=False)
            print("令牌有效。my-resume 摘要：")
            print((txt or "")[:400])
        elif args.cmd == "probe":
            print(json.dumps(tools_list(), ensure_ascii=False, indent=2))
        elif args.cmd == "search":
            payload, rows = search_jobs(job_name=args.job_name, company=args.company,
                                        address=args.address, page=args.page, tool=args.tool)
            if args.raw_out:
                pathlib.Path(args.raw_out).write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[raw-out] 原始 payload 已写 {args.raw_out}（真实岗位数据，勿提交进仓库）")
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
            else:
                print(f"命中 {len(rows)} 条：")
                for r in rows[:20]:
                    print(f"  · [{r['jobId']}] {r['company']} — {r['title']}  {r['salary']}  {r['url']}")
        elif args.cmd == "apply":
            if args.dry_run:
                print(f"[dry-run] 将调用 user-apply-job jobId={args.job_id} jobKind={args.job_kind}")
                return
            print(json.dumps(apply_job(args.job_id, args.job_kind), ensure_ascii=False, indent=2))
    except McpAuthError as e:
        print(f"❌ 认证失败：{e}", file=sys.stderr)
        sys.exit(3)
    except McpRateLimit as e:
        print(f"⚠️ 限流：{e}", file=sys.stderr)
        sys.exit(4)
    except McpError as e:
        print(f"❌ 调用失败：{e}", file=sys.stderr)
        sys.exit(5)


if __name__ == "__main__":
    main()
