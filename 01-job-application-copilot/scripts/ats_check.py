# -*- coding: utf-8 -*-
"""
ats_check.py — 简历 HTML 的 ATS / AI 初筛自检
==============================================
用法：
    python ats_check.py <简历.html>                # 只跑格式层
    python ats_check.py <简历.html> --jd <JD文件>  # 格式层 + 内容层（推荐）

【格式层】(1-6，与 SKILL.md「ATS 自检」一致）
  1. 无图标字体（iconfont/fontawesome/google fonts）——打印后变乱码、文本层丢失
  2. 无 CSS grid / 多栏 / column-count——AI 按阅读顺序解析，多栏会读成串行
  3. 无 svg / canvas / img 承载文字——ATS 解析图片是黑洞
  4. 无「彩块白字」——反色处理后文字消失
  5. 无残留【待补】占位——未确认数据不进投递物
  6. 结构统计：entry 数、总字符

【内容层】(7-10，2026-09-10 增；需 --jd 才能按岗位定制，否则用通用词表)
  7. 关键词堆砌：单个目标词出现 > 5 次 → 堆砌预警（ATS 与人工都会反感）
  8. 首屏 6 秒可读性：摘要首段是否出现该 JD 的核心关键词（HR 扫第一屏决定是否往下看）
  9. 量化成果：每条经历/项目是否带数字（无数字的条目等于没说成果）
 10. 篇幅控制：一页为宜，正文过长会被砍

自检标准：导出的 PDF 文本用记事本能通顺读完 = AI 能读懂。

诚实边界：网上流传的「关键词密度 2-5%」出自英文语境（英文以空格分词，分母清楚）。
中文没有可靠分词，本脚本只给「出现次数 / 词元数」的近似口径供参考，
**判定以堆砌预警为准，不以百分比为准**。
"""
import sys
import re
import argparse
import pathlib

CHECKS = []

# 内容层用：中文按字、英文按词切词元（近似口径，见文件头诚实边界）
TOKEN_PAT = re.compile(r"[\u4e00-\u9fff]|[A-Za-z][A-Za-z0-9+#./-]*")
NUM_PAT = re.compile(r"\d+\.?\d*\s*(%|％|倍|万|亿|条|个|项|人|天|月|年|k|K|w|W|元|张|次|轮|版)+|\d")


def check(name, ok, detail="", warn_only=False):
    """warn_only=True → 显示为 ⚠️ 提示，不参与通过判定。
    用于脚本无法确知、需人工判断的项（如一页容量取决于字号排版）。"""
    CHECKS.append((name, ok, detail, warn_only))


def strip_html(h: str) -> str:
    """HTML → 纯文本（去掉标签/style/script），用于内容层统计。"""
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", h, flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", "\n", s)
    s = re.sub(r"&nbsp;?", " ", s)
    return re.sub(r"[ \t]+", " ", s)


def content_checks(text: str, targets: dict, jd_name: str, blocks=None):
    """内容层 7-10。targets: {关键词类目: [正则...]}，来自 match_jd.SOFT_KEYWORDS 的 JD 侧子集。

    blocks: 经历/项目条目列表（由 main 按 HTML entry 或 md 标题切好）。
    """
    tokens = TOKEN_PAT.findall(text)
    n_tok = len(tokens) or 1
    detail_rows = []

    # 7. 堆砌检测：按「单个词形」计数，不是按类目聚合。
    #    类目聚合会把 PRD/需求文档/产品方案/原型/MVP 五个不同词加总，15 次其实是 5 个词各 3 次——
    #    那不叫堆砌。真堆砌是同一个词反复刷（如「Agent」出现 20 次）。阈值 10。
    # 阈值与篇幅挂钩：1600 词元的简历里 Agent 出现 12 次（0.7%，7 个条目均摊 1.7 次）
    # 是自然分布，不是刷词。真堆砌的密度会明显更高，故取 1% 词元数。
    STUFF_THRESHOLD = max(10, int(n_tok * 0.01))
    stuffing = []
    for cat, pats in targets.items():
        total = 0
        for p in pats:
            cnt = len(re.findall(p, text, re.IGNORECASE))
            total += cnt
            if cnt > STUFF_THRESHOLD:
                stuffing.append(f"「{p}」×{cnt}")
        detail_rows.append((cat, total))
    check(f"关键词无堆砌（单词>{STUFF_THRESHOLD}次）", not stuffing,
          "；".join(stuffing) if stuffing
          else f"最高单词形 {max((len(re.findall(p, text, re.IGNORECASE)) for pats in targets.values() for p in pats), default=0)} 次")

    # 8. 首屏 6 秒：摘要首段（首个 300 字符）是否含 JD 核心关键词
    head = text[:300]
    hit_head = [c for c, pats in targets.items()
                if any(re.search(p, head, re.IGNORECASE) for p in pats)]
    check("首屏含目标岗关键词", len(hit_head) >= 1,
          f"首屏命中 {len(hit_head)}/{len(targets)} 类：{'、'.join(hit_head[:5])}" if hit_head
          else f"首屏 300 字内无目标岗关键词（JD：{jd_name}）")

    # 9. 量化：检查每条经历/项目是否带数字（block 由 main 切好，见下）
    if not blocks:
        blocks = [b.strip() for b in re.split(r"\n(?=#{2,3}\s|\*\*)", text) if len(b.strip()) > 40]
    no_num = [b[:28].replace("\n", " ") for b in blocks if not NUM_PAT.search(b)]
    check("经历/项目条目带量化数字", not no_num or len(no_num) <= max(0, len(blocks) // 4),
          f"{len(blocks)} 条中 {len(no_num)} 条无数字：" + "；".join(no_num[:3]) if no_num
          else f"{len(blocks)} 条全部含数字")

    # 10. 篇幅：只提示不判失败——一页与否取决于字号/行距/边距，脚本的词元口径只能给量级参考
    check("篇幅一页可控", n_tok < 1800,
          f"词元 {n_tok}（中文按字+英文按词）｜参考线 1800；超了不一定超页，以 Ctrl+P 预览为准",
          warn_only=True)

    # 密度：仅作参考，不参与判定
    total_kw = sum(c for _, c in detail_rows)
    return total_kw, n_tok, detail_rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("resume", help="简历 HTML（或 md）文件")
    ap.add_argument("--jd", default=None, help="目标 JD 文件（给出则按该岗位关键词做内容层检查）")
    ap.add_argument("--no-content", action="store_true", help="只跑格式层")
    args = ap.parse_args()

    p = pathlib.Path(args.resume)
    if not p.exists():
        print(f"❌ 文件不存在：{p}")
        sys.exit(2)
    h = p.read_text(encoding="utf-8")

    # 1. 图标字体 / 外链字体
    bad_fonts = [k for k in ("iconfont", "fontawesome", "fonts.googleapis", "@font-face") if k in h]
    check("无图标字体/外链字体", not bad_fonts, "检出: " + ",".join(bad_fonts) if bad_fonts else "")

    # 2. 多栏布局
    bad_layout = []
    if "grid-template" in h:
        bad_layout.append("grid-template")
    if re.search(r"column-count\s*:\s*[2-9]", h):
        bad_layout.append("column-count>=2")
    if re.search(r"flex-direction\s*:\s*row", h) and h.count("display:flex") + h.count("display: flex") > 3:
        bad_layout.append("大量flex行布局(疑似多栏)")
    check("无 CSS grid/多栏", not bad_layout, "检出: " + ",".join(bad_layout) if bad_layout else "")

    # 3. svg/canvas/img
    bad_media = [t for t in ("<svg", "<canvas") if t in h]
    imgs = re.findall(r"<img[^>]*src=\"([^\"]+)\"", h)
    # 头像类小图可容忍，但正文用图承载文字不行——全部列出让人工判断
    check("无 svg/canvas", not bad_media, "检出: " + ",".join(bad_media) if bad_media else "")
    check("img 数量(应<=1 头像位)", len(imgs) <= 1, f"检出 {len(imgs)} 张: {imgs[:3]}" if imgs else "无图片（最安全）")

    # 4. 彩块白字（color: #fff/#ffffff 且伴随 background 非 none 的 style）
    white_on_color = re.findall(r"background(-color)?\s*:\s*(?!transparent|none|inherit)[^;\"']{3,40}[;\"'][^>]*color\s*:\s*#f(?:ff|fff)\b", h, re.IGNORECASE)
    white_on_color += re.findall(r"color\s*:\s*#f(?:ff|fff)\b[^>]*background(-color)?\s*:\s*(?!transparent|none)[^;\"']{3,40}", h, re.IGNORECASE)
    check("无彩块白字", not white_on_color, f"疑似 {len(white_on_color)} 处" if white_on_color else "")

    # 5. 待补占位（baseline 母版豁免：母版本身带占位符供一岗一版时替换，不是投递物）
    todos = re.findall(r"【?待补[^<】\]]*[】\]]?", h)
    todo_cls = h.count('class="todo"')
    is_template = "baseline" in p.name.lower()
    if is_template:
        check("无残留待补项（母版豁免）", True,
              f"母版含待补 {len(todos)} 处 + todo 标签 {todo_cls} 处，属预期；**投递稿必须清零**",
              warn_only=True)
    else:
        check("无残留待补项", not todos and not todo_cls,
              f"文本待补 {len(todos)} 处 + todo标签 {todo_cls} 处" if (todos or todo_cls) else "")

    # 6. 结构统计
    entries = len(re.findall(r'class="entry"', h))
    print("=" * 62)
    print(f"ATS 自检报告 ｜ {p.name}")
    print("=" * 62)

    # —— 内容层 7-10 ——
    content_report = None
    if not args.no_content:
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        try:
            import match_jd as M
        except Exception:
            M = None
        if M is None:
            print("⚠️ 无法导入 match_jd，跳过内容层")
        else:
            if args.jd:
                jd_path = pathlib.Path(args.jd)
                if not jd_path.exists():
                    print(f"⚠️ JD 文件不存在：{jd_path} → 内容层用通用词表")
                    jd_text, jd_name = "", "（无 JD，通用词表）"
                else:
                    jd_text = jd_path.read_text(encoding="utf-8", errors="replace")
                    jd_name = jd_path.name
            else:
                jd_text, jd_name = "", "（未传 --jd，通用词表）"
            # 目标词表：传了 JD 就用 JD 提及的类目，否则全量
            if jd_text:
                targets = {c: pats for c, pats in M.SOFT_KEYWORDS.items()
                           if any(re.search(x, jd_text, re.IGNORECASE) for x in pats)}
            else:
                targets = dict(M.SOFT_KEYWORDS)
            is_html = h.lstrip().startswith("<")
            text = strip_html(h) if is_html else h
            # 按条目切块：HTML 的 <div class="entry"> = 一段经历/项目；md 用 ### 标题
            if is_html:
                parts = re.split(r'<div class="entry"', h)[1:]
                blocks = [strip_html(x)[:500] for x in parts if len(strip_html(x).strip()) > 40]
            else:
                blocks = [b.strip() for b in re.split(r"\n(?=###\s)", h) if len(b.strip()) > 40]
            content_report = content_checks(text, targets, jd_name, blocks)

    all_pass = True
    for name, ok, detail, warn_only in CHECKS:
        if not ok and warn_only:
            mark = "⚠️"
        else:
            mark = "✅" if ok else "❌"
        if not ok and not warn_only:
            all_pass = False
        print(f"{mark} {name}" + (f"  ｜ {detail}" if detail else ""))
    print(f"\n结构统计：entry {entries} 个 ｜ 总字符 {len(h)}")
    if content_report:
        total_kw, n_tok, rows = content_report
        print(f"\n关键词密度参考：{total_kw} 次 / {n_tok} 词元 ≈ {total_kw/n_tok*100:.1f}%"
              f"（英文语境经验值 2-5%，中文无可靠分词，此值仅供参考，**判定以堆砌项为准**）")
        top = sorted(rows, key=lambda x: -x[1])[:5]
        print("词频 Top5：" + "、".join(f"{c}×{n}" for c, n in top))
    print("\n结论：" + ("🟢 通过，可导出 PDF 投递（用户自行 Ctrl+P，边距默认+勾选背景图形）" if all_pass else "🔴 未通过，先修复 ❌ 项"))
    sys.exit(0 if all_pass else 1)


if __name__ == "__main__":
    main()
