# -*- coding: utf-8 -*-
"""
patch_jd.py — JD 档案补录通道（残缺 JD 的「回源重抓 / 手工补录」入口）
===========================================================================
用法：
    # ① 回源重抓（推荐）：按档案里存的 url 重新抓完整页面并解析 JD 正文
    python scripts/patch_jd.py <档案路径> --refetch
    python scripts/patch_jd.py --refetch-all            # 批量修全池残缺档案
    python scripts/patch_jd.py --refetch-all --dry-run  # 只报告，不写文件

    # ② 手工补录（用户截图 / 粘贴文本）
    python scripts/patch_jd.py <档案路径> --from-file <文本文件> --source "用户截图"
    python scripts/patch_jd.py <档案路径> --text "任职要求：……" --source "用户粘贴"

    # ③ 查看已有补录
    python scripts/patch_jd.py <档案路径> --list

为什么需要它：
    JD 抓取会因站点结构、反爬、分页等原因残缺。陆家嘴金融AI 那份丢了整个
    「任职要求 + 加分项」段，导致 21 个真实技术缺口（RLHF/DPO/ReAct/LangChain/
    CrewAI/CFA…）被系统报成「真缺口 0 ｜ 无」——**把「没查到」伪装成了「查过了，没有」**。
    残缺本身不可怕，可怕的是残缺被当成完整来解读。本脚本只干一件事：
    **把内容安全地补进档案**；补完用 rescore_pool.py 重算，缺口就回来了。

三个必须遵守的设计点（都是踩过的坑）：
  1. **补录内容追加进「JD 原文摘录」的同一个代码块内**，不能新开一节——
     rescore_pool.py 靠 `## JD 原文摘录 \n ```…``` ` 这个锚点取正文，
     新开一节它根本读不到，补了等于没补。
  2. **原始快照原样保留**，补录段另加分隔标记与来源，事后能分清「当初抓到什么、
     补了什么、从哪补的」。
  3. **幂等**：同内容 sha 已在档案里 → 跳过。重复跑不会把档案灌成一锅粥。
"""
import re
import sys
import argparse
import datetime
import hashlib
import pathlib
import urllib.request
import urllib.error
from html import unescape

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import match_jd as M  # noqa: E402

SKILL = pathlib.Path(__file__).resolve().parent.parent
POOL = SKILL / "jd-pool"

META_PAT = re.compile(r"<!--\s*MATCHMETA\s+(.*?)\s*-->", re.S)
# 三组：前缀（含 ``` 行）/ 正文 / 收尾 ```
EXCERPT_PAT = re.compile(r"(##\s*JD\s*原文[^\n]*\n\s*```[^\n]*\n)(.*?)(\n\s*```)", re.S)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

PATCH_HEAD = "（补录段 · {date} · 来源：{source}）"
PATCH_TAIL = "（补录段结束 · sha={sha}）"

# 正文定位锚点：从职责段起，到下列任一段落止
JUNK_PAT = re.compile(r"\{\{|\}\}|-->|@click|@change|v-if|v-for|v-model|:class=|javascript:")

DUTY_HEADS = ("岗位职责", "工作职责", "职位描述", "岗位描述", "工作内容")
TAIL_HEADS = ("工作地址", "公司信息", "公司介绍", "工商信息", "相似职位",
              "热门职位", "公司地址", "企业信息", "其他信息")


def sha8(s: str) -> str:
    return hashlib.sha1(s.strip().encode("utf-8")).hexdigest()[:8]


def parse_meta(raw: str) -> dict:
    m = META_PAT.search(raw)
    if not m:
        return {}
    return {kv.group(1): kv.group(2).strip()
            for kv in re.finditer(r"(\w+)=(.*?)(?=\s+\w+=|$)", m.group(1))}


def render_meta(d: dict) -> str:
    r"""重排 MATCHMETA。新字段必须排在 url 之后——rank_pool.META_PAT 靠
    尾部通配 `(?:\s+\w+=\S+)*` 兜扩展字段，插在中间会解析失败。"""
    order = ["tier", "score", "coverage", "company", "title", "date",
             "source", "url", "purpose", "mode", "hit", "fill", "gap",
             "truncated", "patched", "manual", "status"]
    parts = [f"{k}={d[k]}" for k in order if k in d]
    parts += [f"{k}={v}" for k, v in d.items() if k not in order]
    return f"<!-- MATCHMETA {' '.join(parts)} -->"


def html_to_jd(h: str) -> str:
    """HTML → JD 正文：去脚本样式、按块级标签断行、从职责段截到尾部段。"""
    h = re.sub(r"<script.*?</script>", "", h, flags=re.S | re.I)
    h = re.sub(r"<style.*?</style>", "", h, flags=re.S | re.I)
    h = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    h = re.sub(r"</(p|div|li|h\d|tr|td|section|article)>", "\n", h, flags=re.I)
    # 去内联标签时，若它夹在两个 ASCII 字符之间，补一个空格。
    # 否则 `<span>Prompt</span><span>Engineering</span>` 会被粘成 `PromptEngineering`，
    # 而关键词 pattern 的 ASCII 词边界断言就会漏检（薪合 JD 实测踩到）。
    # 必须先把标签整体换成哨兵、再按「最近的非标签字符」判定——直接逐个删标签时，
    # 相邻两个标签看到的是对方标签的 < 和 >，判定会失效（已踩过一次）。
    h = re.sub(r"<[^>]+>", "\x00", h)

    def _tag_gap(m):
        left, right = m.start() - 1, m.end()
        if (left >= 0 and right < len(h)
                and h[left].isascii() and h[left].isalnum()
                and h[right].isascii() and h[right].isalnum()):
            return " "
        return ""

    h = re.sub(r"\x00+", _tag_gap, h)
    h = re.sub(r"[ ]{2,}", " ", h)
    h = unescape(h)
    lines = [re.sub(r"[ \t\u3000]+", " ", ln).strip() for ln in h.split("\n")]
    lines = [ln for ln in lines if ln and not JUNK_PAT.search(ln)]  # 滤掉页脚模板残留
    txt = "\n".join(lines)
    s = -1
    for head in DUTY_HEADS:
        s = txt.find(head)
        if s >= 0:
            break
    if s < 0:
        return ""
    e = -1
    for tail in TAIL_HEADS:
        e = txt.find(tail, s + 10)
        if e > s:
            break
    return txt[s:e if e > s else len(txt)].strip()


def fetch_jd(url: str):
    """回源抓取 + 解析。返回 (正文, 说明)。异常由调用方处理。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": url,
    })
    with urllib.request.urlopen(req, timeout=25) as r:
        page = r.read(400000).decode("utf-8", errors="replace")
        status = r.status
    body = html_to_jd(page)
    return body, f"HTTP {status} ｜ 页面 {len(page)}B ｜ 解析正文 {len(body)} 字符"


def clean_content(s: str) -> str:
    """补录内容清洗：干掉 ``` 三反引号（会截断档案的代码块结构）。"""
    s = s.replace("```", "'''")
    return s.strip()


def add_patch(path: pathlib.Path, content: str, source: str, dry: bool = False):
    """把内容追加进档案的 JD 代码块内。返回 (是否写入, 说明)。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    m = EXCERPT_PAT.search(raw)
    if not m:
        return False, "档案里找不到「## JD 原文摘录」代码块，无法定位补录位置"
    content = clean_content(content)
    if not content:
        return False, "补录内容为空（解析失败或文本为空）"
    sha = sha8(content)
    if sha in raw:
        return False, f"内容已在档案里（sha={sha}）→ 跳过，重复跑不会灌水"
    today = str(datetime.date.today())
    block = (f"\n\n{PATCH_HEAD.format(date=today, source=source)}\n"
             f"{content}\n{PATCH_TAIL.format(sha=sha)}")
    # ⚠️ 插入点必须落在「【人工复核备注】之前」——这是本脚本最容易踩的坑：
    #   打分（match_jd）与重算（rescore_pool）都按 split("【人工复核备注】")[0] 取正文，
    #   因为备注是 AI 批注、不能参与打分。若把补录段追加到代码块最末尾，
    #   它就落在备注「之后」，会被这刀直接切掉 —— 补了等于没补（实测踩过）。
    seg = raw[m.start(2):m.end(2)]
    note_at = seg.find("【人工复核备注】")
    insert_at = (m.start(2) + note_at) if note_at >= 0 else m.end(2)
    out = raw[:insert_at] + block + raw[insert_at:]

    # 补录后标记「待重算」：truncated 的结论必须由 rescore_pool 在完整文本上重新判定，
    # 不能在这里自己宣布「修好了」——那样等于用同一个残缺逻辑给自己发合格证。
    meta = parse_meta(out)
    if meta:
        meta["patched"] = meta.get("patched", "0")
        meta["truncated"] = "recheck"
        out = META_PAT.sub(render_meta(meta), out, count=1)
    if dry:
        return True, f"[dry-run] 将补录 {len(content)} 字符（sha={sha}）"
    path.write_text(out, encoding="utf-8")
    return True, f"已补录 {len(content)} 字符（sha={sha}）→ META 标记 truncated=recheck"


def list_patches(path: pathlib.Path):
    raw = path.read_text(encoding="utf-8", errors="replace")
    m = EXCERPT_PAT.search(raw)
    if not m:
        return []
    return re.findall(r"（补录段 · ([^·]+) · 来源：([^）]+)）\n(.*?)\n（补录段结束 · sha=(\w+)）",
                      m.group(2), re.S)


def is_truncated(raw: str) -> bool:
    meta = parse_meta(raw)
    return meta.get("truncated", "0") not in ("0", "")


def refetch_one(path: pathlib.Path, dry: bool = False):
    """回源重抓单个档案。返回 (状态, 说明)。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta = parse_meta(raw)
    url = (meta.get("url") or "").strip()
    if not url or url == "none":
        return "skip", "META 里没有 url，无法回源"
    try:
        body, note = fetch_jd(url)
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 429):
            return "blocked", f"站点反爬（HTTP {e.code}），请改用 --from-file 手工补录"
        return "fail", f"HTTP {e.code}"
    except Exception as e:
        return "fail", f"{type(e).__name__}: {e}"
    if not body or len(body) < M.SHORT_JD_CHARS:
        return "empty", f"解析出的正文过短（{len(body)} 字符）｜{note}　可能需手工补录"
    ok, msg = add_patch(path, body, f"回源重抓 {url.split('/')[2]}", dry=dry)
    return ("done" if ok else "dup"), f"{note}　→ {msg}"


def all_targets():
    """全池里需要补录的档案（truncated 非 0）。"""
    out = []
    for t in ("green", "yellow", "red"):
        d = POOL / t
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if is_truncated(f.read_text(encoding="utf-8", errors="replace")):
                out.append(f)
    return out


def main():
    ap = argparse.ArgumentParser(description="JD 档案补录通道（回源重抓 / 手工补录）")
    ap.add_argument("path", nargs="?", help="JD 档案路径（--refetch-all 时省略）")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--refetch", action="store_true", help="按 META 的 url 回源重抓并补录")
    g.add_argument("--from-file", metavar="TXT", help="从文本文件补录（截图 OCR / 手工整理）")
    g.add_argument("--text", metavar="STR", help="直接补录一段文本")
    g.add_argument("--list", action="store_true", help="列出档案已有的补录段")
    ap.add_argument("--refetch-all", action="store_true", help="批量修全池 truncated 档案")
    ap.add_argument("--source", default="回源重抓", help="补录来源标注（默认「回源重抓」）")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写文件")
    args = ap.parse_args()

    # ── 批量回源 ──────────────────────────────────────────────
    if args.refetch_all:
        targets = all_targets()
        print("=" * 74)
        print(f"批量回源补录 ｜ 目标 {len(targets)} 份 ｜ 模式：{'DRY-RUN 未写入' if args.dry_run else 'APPLY'}")
        print("=" * 74)
        if not targets:
            print("\n全池没有 truncated 标记的档案——无需补录。")
            sys.exit(0)
        stat = {}
        for f in targets:
            st, msg = refetch_one(f, dry=args.dry_run)
            stat[st] = stat.get(st, 0) + 1
            icon = {"done": "✅", "dup": "↩️", "skip": "⏭️",
                    "empty": "⚠️", "blocked": "🚧", "fail": "❌"}.get(st, "❔")
            print(f"\n{icon} {f.parent.name}/{f.name}")
            print(f"   {msg}")
        print("\n" + "-" * 74)
        print("统计：" + " ｜ ".join(f"{k}={v}" for k, v in sorted(stat.items())))
        if not args.dry_run and stat.get("done"):
            print("\n💡 下一步：python scripts/rescore_pool.py --apply  → 再跑 rank_pool.py 刷新榜单")
        sys.exit(0)

    if not args.path:
        ap.error("需要给出 JD 档案路径（或使用 --refetch-all）")
    path = pathlib.Path(args.path)
    if not path.exists():
        print(f"❌ 档案不存在：{path}")
        sys.exit(2)

    # ── 查看补录 ──────────────────────────────────────────────
    if args.list:
        items = list_patches(path)
        print(f"📄 {path.name} ｜ 已有补录 {len(items)} 段")
        for i, (date, src, body, sha) in enumerate(items, 1):
            print(f"  {i}. [{date.strip()}] 来源：{src} ｜ {len(body.strip())} 字符 ｜ sha={sha}")
        sys.exit(0)

    # ── 回源重抓 ──────────────────────────────────────────────
    if args.refetch:
        st, msg = refetch_one(path, dry=args.dry_run)
        icon = {"done": "✅", "dup": "↩️", "skip": "⏭️",
                "empty": "⚠️", "blocked": "🚧", "fail": "❌"}.get(st, "❔")
        print(f"{icon} {path.name}\n   {msg}")
        if st == "done" and not args.dry_run:
            print("\n💡 下一步：python scripts/rescore_pool.py --apply（重算缺口）→ rank_pool.py")
        sys.exit(0 if st in ("done", "dup") else 1)

    # ── 手工补录 ──────────────────────────────────────────────
    if args.from_file:
        fp = pathlib.Path(args.from_file)
        if not fp.exists():
            print(f"❌ 补录文件不存在：{fp}")
            sys.exit(2)
        content = fp.read_text(encoding="utf-8", errors="replace")
        src = args.source if args.source != "回源重抓" else f"手工补录 {fp.name}"
    elif args.text:
        content, src = args.text, args.source
    else:
        ap.error("请指定 --refetch / --from-file / --text / --list 之一")

    ok, msg = add_patch(path, content, src, dry=args.dry_run)
    print(f"{'✅' if ok else '↩️'} {path.name}\n   {msg}")
    if ok and not args.dry_run:
        print("\n💡 下一步：python scripts/rescore_pool.py --apply（重算缺口）→ rank_pool.py")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
