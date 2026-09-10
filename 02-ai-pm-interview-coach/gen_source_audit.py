# -*- coding: utf-8 -*-
"""生成题库来源核查报告 SOURCE_AUDIT.md"""
import pathlib, re, collections
from datetime import date

BASE = pathlib.Path.home() / ".workbuddy" / "skills" / "ai-pm-interview-coach" / "questions"
OUT = BASE.parent / "SOURCE_AUDIT.md"

rows, cur_q, cur_t = [], None, None
for f in sorted(BASE.glob("0*.md")):
    domain = f.read_text(encoding="utf-8").splitlines()[0].lstrip("# ")
    for ln in f.read_text(encoding="utf-8").split("\n"):
        m = re.match(r"### (Q\d+-\d+)｜(.+)", ln)
        if m:
            cur_q, cur_t = m.group(1), m.group(2)
        if ln.startswith("- 来源："):
            s = ln.replace("- 来源：", "")
            if s.startswith("真题出处"):
                kind = "真题出处"
            elif s.startswith("考点出处"):
                kind = "考点出处"
            elif s.startswith("JD能力"):
                kind = "JD能力出处"
            elif s.startswith("用户提供"):
                kind = "用户喂题"
            elif s.startswith("用户真实项目"):
                kind = "用户真实项目"
            else:
                kind = "模型生成"
            rows.append((cur_q, cur_t, domain, kind, s))

total = len(rows)
cnt = collections.Counter(r[3] for r in rows)
ok = total - cnt["模型生成"]

L = []
L.append("# 题库来源核查报告 SOURCE_AUDIT\n")
L.append(f"> 生成时间：{date.today()} ｜ 题库规模：**{total} 题** ｜ 本报告随每次题库更新重跑\n")
L.append("## 一、总体结果\n")
L.append("| 来源类型 | 题数 | 占比 | 可核查性 |")
L.append("|---|---|---|---|")
L.append(f"| 真题出处（有 URL） | {cnt['真题出处']} | {cnt['真题出处']/total*100:.1f}% | ✅ 可点开核对原题 |")
L.append(f"| 用户喂题（有 URL） | {cnt['用户喂题']} | {cnt['用户喂题']/total*100:.1f}% | ✅ 可点开核对 |")
L.append(f"| 用户真实项目（一手） | {cnt['用户真实项目']} | {cnt['用户真实项目']/total*100:.1f}% | ✅ 一手经历，无需外部出处 |")
L.append(f"| 考点出处（有 URL） | {cnt['考点出处']} | {cnt['考点出处']/total*100:.1f}% | ⚠️ 出处含该考点，题干为本地改写 |")
L.append(f"| JD 能力要求出处（有 URL） | {cnt['JD能力出处']} | {cnt['JD能力出处']/total*100:.1f}% | ⚠️ 该能力在 JD 中明确要求，题干为本地扩展 |")
L.append(f"| **模型生成·无外部出处** | **{cnt['模型生成']}** | **{cnt['模型生成']/total*100:.1f}%** | ❌ 无可查来源 |")
L.append("")
L.append(f"**有可查出处：{ok}/{total} = {ok/total*100:.1f}%**\n")

L.append("## 二、五类来源的定义与可信度\n")
L.append("| 类型 | 定义 | 可信度 |")
L.append("|---|---|---|")
L.append("| 真题出处 | 公开面经/文章里真实问过的题，URL 可点开看到原题 | 最高 |")
L.append("| 用户喂题 | 用户提供的面经文章或亲身被问的题 | 最高 |")
L.append("| 用户真实项目 | 基于用户本人做过的项目定制，一手经历 | 最高 |")
L.append("| 考点出处 | 出处文章讲了该考点但没问这道题，题干由本地改写 | 中——可查考点，不可查原题 |")
L.append("| JD 能力出处 | 该能力在大厂 JD 中被明确要求，题由能力要求转写 | 中——可查要求，不可查原题 |")
L.append("| 模型生成 | 建库时由模型依据岗位通用认知生成，无外部出处 | 待补 |")
L.append("")

L.append("## 三、34 道尚无外部出处的题目（待补或降级）\n")
L.append("> 这些题由模型生成，方向符合行业常识但无可核查来源。后续路径：① 每周自动更新时联网找对应真题替换；② 用户面到同类真题时回喂；③ 长期无出处则降级进存档区。\n")
L.append("| 题号 | 题干 | 所属域 |")
L.append("|---|---|---|")
for q, t, d, k, s in rows:
    if k == "模型生成":
        L.append(f"| {q} | {t} | {d.replace('（','(').split('(')[0]} |")
L.append("")

L.append("## 四、本次核查执行的方法\n")
L.append("1. **三路并行联网调研**（Orchestrator-Workers 模式）：JD 雷达（大厂招聘 JD）／题集猎手（公开面经）／热点追踪（2026 行业动态）")
L.append("2. **来源验证**：掘金面经原文已用 WebFetch 开页核对，确认文章存在、日期与题目属实")
L.append("3. **JD 链接逐条验证**：17 个大厂 JD 链接逐个 WebFetch，判定 A(有效)/B(存疑)/C(失效)，结果 13A / 3B / 1C，B/C 已提供替代源")
L.append("4. **热点 URL 补全**：首轮只给域名的 11 条热点，二轮全部补齐到具体文章页，含 Anthropic 官网、中央网信办等权威源")
L.append("5. **逐题匹配改写**：112 题逐条比对真实来源，能匹配的改写为对应 URL，匹配不上的如实标注为「模型生成·无外部出处」")
L.append("")

L.append("## 五、已知问题\n")
L.append("- **多题共享同一来源**：掘金那篇覆盖面广（8 章），导致 18 题共享同一 URL。可查但不算「一题一源」，属可接受妥协")
L.append("- **字节跳动 JD 无法核验**：字节招聘站为 SPA，WebFetch 只能渲染首页，已用可渲染的镜像站替代（该镜像标注「已结束」）")
L.append("- **34 道模型生成题仍在正式题库**：未删除是因为方向正确、训练价值在，但标注已如实暴露来源状态")
L.append("")

OUT.write_text("\n".join(L), encoding="utf-8")
print("已生成:", OUT)
print(f"总题数 {total}｜有出处 {ok}（{ok/total*100:.1f}%）｜无出处 {cnt['模型生成']}")
