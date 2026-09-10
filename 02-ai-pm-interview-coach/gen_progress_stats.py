#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
训练进度统计脚本 —— AI 产品经理面试陪练技能

用法：
    python gen_progress_stats.py

输出：累计已练题数 / 覆盖率 / 分域覆盖 / 得分分布 / 薄弱域 Top3 / 断点状态 / 下次建议
在每次训练启动和收尾时运行，向用户汇报进度。
"""
import pathlib
import re
import collections
from datetime import datetime

BASE = pathlib.Path(__file__).parent
Q_DIR = BASE / "questions"
PROGRESS = BASE / "progress" / "progress.md"
WEAK = BASE / "progress" / "weaknesses.md"
SESSION = BASE / "progress" / "session.md"

DOMAIN_NAME = {
    "1": "AI/大模型基础",
    "2": "产品方法论",
    "3": "AI产品设计case",
    "4": "真实项目定制",
    "5": "商业化战略",
    "6": "开放思辨",
    "7": "行为面试",
    "8": "项目管理与技术协同",
    "9": "伦理合规与高阶思维",
}

STAR_LEVEL = {
    "★": "初级岗",
    "★★": "初级岗",
    "★★★": "初级-资深岗(主力档)",
    "★★★★": "资深岗",
    "★★★★★": "专家岗",
}


def load_bank():
    """题库：返回 {题号: (域号, 题干, 难度)}"""
    bank = {}
    for f in sorted(Q_DIR.glob("0*.md")):
        text = f.read_text(encoding="utf-8")
        cur, diff = None, ""
        for ln in text.split("\n"):
            m = re.match(r"### (Q(\d+)-(\d+))[｜|](.+)", ln)
            if m:
                cur = m.group(1)
                bank[cur] = {"domain": m.group(2), "title": m.group(4).strip(), "diff": ""}
                continue
            if cur and ln.startswith("- 难度："):
                dm = re.search(r"★+", ln)
                if dm:
                    bank[cur]["diff"] = dm.group(0)
    return bank


def load_progress():
    """训练记录：返回 [(日期, 模式, 题号, 得分, 关键词)]"""
    rows = []
    if not PROGRESS.exists():
        return rows
    for ln in PROGRESS.read_text(encoding="utf-8").split("\n"):
        if not ln.startswith("|"):
            continue
        cells = [c.strip() for c in ln.strip("|").split("|")]
        if len(cells) < 4 or cells[0] in ("日期", "---"):
            continue
        if not re.match(r"\d{4}-\d{2}-\d{2}", cells[0]):
            continue
        score = None
        sm = re.search(r"(\d)", cells[3])
        if sm:
            score = int(sm.group(1))
        rows.append({
            "date": cells[0], "mode": cells[1], "qid": cells[2],
            "score": score, "kw": cells[4] if len(cells) > 4 else "",
        })
    return rows


CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _domain_no(token: str):
    """把「一」/「1」统一转成 int，转不了返回 None"""
    token = token.strip()
    if token.isdigit():
        return int(token)
    return CN_NUM.get(token)


def load_weakness():
    """薄弱点：返回 {域号str: [条目行]}。档案里用中文数字（域一），需兼容。"""
    weak = collections.defaultdict(list)
    if not WEAK.exists():
        return weak
    cur_domain = None
    for ln in WEAK.read_text(encoding="utf-8").split("\n"):
        m = re.match(r"###\s*域\s*([一二三四五六七八九1-9])", ln)
        if m:
            cur_domain = _domain_no(m.group(1))
            continue
        m2 = re.match(r"-\s*域\s*([一二三四五六七八九1-9])\s*>", ln)
        if m2:  # 条目自带域号，优先用条目自己的
            d = _domain_no(m2.group(1))
        elif cur_domain:
            d = cur_domain
        else:
            continue
        if "待改进" in ln or "改善中" in ln:
            weak[str(d)].append(ln)
    return weak


def load_session():
    """断点：返回 (进行中题号列表, 已出题集合, 轮次)"""
    doing, asked, rnd = [], set(), None
    if not SESSION.exists():
        return doing, asked, rnd
    section = None
    for ln in SESSION.read_text(encoding="utf-8").split("\n"):
        if ln.startswith("## 进行中"):
            section = "doing"; continue
        if ln.startswith("## 已完成"):
            section = "done"; continue
        if ln.startswith("## 已出题清单"):
            section = "asked"; continue
        if ln.startswith("## 出题轮次"):
            section = "round"; continue
        if ln.startswith("## "):
            section = None; continue

        if section == "doing" and ln.startswith("| Q"):
            doing.append(ln.strip("|").split("|")[0].strip())
        if section == "asked":
            asked.update(re.findall(r"Q\d+-\d+", ln))
        if section == "round":
            rm = re.search(r"第\s*(\d+)\s*轮", ln)
            if rm and rnd is None:
                rnd = int(rm.group(1))
    return doing, asked, rnd


def bar(done, total, width=18):
    if total == 0:
        return "[" + " " * width + "]"
    filled = int(round(done / total * width))
    return "[" + "█" * filled + "·" * (width - filled) + "]"


def main():
    bank = load_bank()
    prog = load_progress()
    weak = load_weakness()
    doing, asked, rnd = load_session()

    total = len(bank)
    done_ids = {r["qid"] for r in prog}
    coverage = len(done_ids & set(bank)) / total * 100 if total else 0

    print("=" * 62)
    print("  训练进度快照  |  AI 产品经理面试陪练")
    print("=" * 62)

    # 1. 总览
    print(f"\n【总览】")
    print(f"  题库总数     {total} 题")
    print(f"  已练         {len(done_ids & set(bank))} 题")
    print(f"  覆盖率       {coverage:.1f}%  {bar(len(done_ids & set(bank)), total)}")
    print(f"  已出题       {len(asked)} 题（含未答）｜当前第 {rnd or 1} 轮")
    if prog:
        last = max(r["date"] for r in prog)
        d0 = datetime.strptime(last, "%Y-%m-%d").date()
        gap = (datetime.now().date() - d0).days
        print(f"  上次训练     {last}（{gap} 天前）" + ("  ⚠️ 断更太久" if gap >= 3 else ""))
    else:
        print("  上次训练     无记录")

    # 2. 分域覆盖
    print(f"\n【分域覆盖】")
    by_domain = collections.Counter()
    for qid in done_ids:
        if qid in bank:
            by_domain[bank[qid]["domain"]] += 1
    total_by_domain = collections.Counter(v["domain"] for v in bank.values())
    for d in sorted(total_by_domain):
        dn, dt = by_domain.get(d, 0), total_by_domain[d]
        wcnt = len(weak.get(d, []))
        flag = f"  ⚠️ 薄弱 {wcnt} 条" if wcnt else ""
        print(f"  域{d} {DOMAIN_NAME.get(d,''):<16} {dn:>2}/{dt:<3} {bar(dn, dt, 12)} {dn/dt*100:>5.1f}%{flag}")

    # 2.5 难度分布（已练 vs 全库）
    print(f"\n【难度分布】★初级 ★★★初级-资深(主力档) ★★★★★专家")
    star_all = collections.Counter(v["diff"] for v in bank.values())
    star_done = collections.Counter(bank[q]["diff"] for q in done_ids if q in bank)
    for s in sorted(star_all, key=lambda x: len(x)):
        da, dt = star_done.get(s, 0), star_all[s]
        mark = "  ← 主力档" if s == "★★★" else ""
        print(f"  {s:<6} {STAR_LEVEL.get(s,''):<22} {da:>2}/{dt:<3} {bar(da, dt, 10)}{mark}")

    # 3. 得分分布
    scores = [r["score"] for r in prog if r["score"] is not None]
    if scores:
        print(f"\n【得分分布】  平均 {sum(scores)/len(scores):.2f} / 5")
        dist = collections.Counter(scores)
        for s in sorted(dist, reverse=True):
            label = {5: "可直接上考场", 4: "良好", 3: "结构在深度不够", 2: "明显缺口", 1: "答偏/答不上"}.get(s, "")
            print(f"  {s} 分 × {dist[s]}  {'●' * dist[s]:<12} {label}")

    # 4. 薄弱域 Top3
    if weak:
        print(f"\n【薄弱域 Top3】（按待改进条目数）")
        ranked = sorted(weak.items(), key=lambda kv: -len(kv[1]))[:3]
        for d, items in ranked:
            print(f"  域{d} {DOMAIN_NAME.get(d,'')} —— {len(items)} 条待改进")
            for it in items[:2]:
                core = re.sub(r"^-\s*域\s*[一二三四五六七八九1-9]\s*>\s*", "", it)
                core = re.split(r"\s*>\s*次数", core)[0].replace("**", "")
                print(f"      · {core[:44]}")

    # 5. 断点
    print(f"\n【断点】")
    if doing:
        for q in doing:
            title = bank.get(q, {}).get("title", "（题干未知）")
            print(f"  ⏸ 进行中：{q}  {title[:34]}")
            print(f"     → 下次启动优先续上此题，不另出新题")
    else:
        print("  无未完成的题，下次可直接出新题")

    # 6. 建议
    print(f"\n【下次建议】")
    if doing:
        print(f"  1. 续上 {doing[0]}")
    untrained = [d for d in sorted(total_by_domain) if by_domain.get(d, 0) == 0]
    if untrained:
        names = "、".join(f"域{d} {DOMAIN_NAME.get(d,'')}" for d in untrained)
        print(f"  2. 以下域从未练过，优先覆盖：{names}")
    if weak:
        top = sorted(weak.items(), key=lambda kv: -len(kv[1]))[0][0]
        print(f"  3. 薄弱项最集中的是 域{top} {DOMAIN_NAME.get(top,'')}，建议穿插弱项专项")
    low = [r["qid"] for r in prog if r["score"] is not None and r["score"] <= 3]
    if low:
        print(f"  4. 得分 ≤3 的题值得二刷：{'、'.join(sorted(set(low)))}")
    print()


if __name__ == "__main__":
    main()
