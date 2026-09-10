#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P1/P2 防回归自检（不依赖 pytest，直接 `python scripts/tests/test_p1_regression.py`）。

为什么要有这个文件：2026-09-10 那轮改动里，有 4 个 bug 是靠「肉眼看输出」才发现的，
而它们全都会**静默**给出错误结论——正是这个技能最该防的失效模式：

  1. 批量改 pattern 的脚本把 SKILL_ATOMS 的**键名**也改了
     （键变成 `(?<![A-Za-z0-9])RLHF(?![A-Za-z0-9])`，报告里直接漏出正则语法）
  2. 打分文本误用整份档案而非 JD 正文 → 上一轮自己写进去的「真缺口：多模态」
     被下一轮当成 JD 提及，形成自我循环、分数不可复现
  3. 「优先」藏在括号里时整行被降级 → 「3-5年经验(平台类优先)」的年限要求丢失
  4. HTML 去内联标签时把两侧英文粘成 `PromptEngineering` → 词边界断言漏检

每条断言都对应上面一个已修的 bug，改坏了就会红。
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
sys.path.insert(0, str(SCRIPTS))

import match_jd as M          # noqa: E402
import patch_jd as P          # noqa: E402

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ✅ {name}")
    else:
        print(f"  ❌ {name}" + (f"  ← {detail}" if detail else ""))
        FAILURES.append(name)


# ── 1. 词表完整性：键名必须是给人看的名字，不能是正则 ──────────────────
def test_vocab_keys():
    print("\n[1] 词表完整性")
    bad = [k for k in M.SKILL_ATOMS if re.search(r"[()\[\]\\|?*+]|\?-i:|A-Za-z0-9\]\)", k)]
    check("SKILL_ATOMS 键名不含正则语法", not bad, f"被污染：{bad}")
    bad = [k for k in M.SOFT_KEYWORDS
           if re.search(r"[()\[\]\\|?*+]|\?-i:", k)]
    check("SOFT_KEYWORDS 键名不含正则语法", not bad, f"被污染：{bad}")

    broken = []
    for src, table in (("SOFT_KEYWORDS", M.SOFT_KEYWORDS.values()),
                       ("SKILL_ATOMS", (v[0] for v in M.SKILL_ATOMS.values()))):
        for pats in table:
            for p in pats:
                try:
                    re.compile(p)
                except re.error as e:
                    broken.append((src, p, str(e)))
    check("所有 pattern 均可编译", not broken, str(broken[:3]))


# ── 2. ASCII 词边界：中文环境下要能命中，又不能撞子串 ──────────────────
def test_word_boundary():
    print("\n[2] ASCII 词边界（危险区：\\b 在 Unicode 下把中文当词字符）")
    rag = M.SOFT_KEYWORDS["RAG/检索增强"]
    hit = lambda s: any(re.search(p, s, re.I) for p in rag)
    check("「使用RAG架构」能命中（\\b 做不到）", hit("使用RAG架构"))
    check("「storage」不误命中", not hit("对象存储 storage"))
    check("「Ragas」不误命中（曾把评测框架当 RAG）", not hit("基于 Ragas 搭建评测"))

    fin = M.SKILL_ATOMS["RegTech 监管科技"][0]
    check("「Streamlit」不误命中 AML（曾坐实过）", not any(re.search(p, "Streamlit 原型", re.I) for p in fin))
    check("「AML/KYC」能命中", any(re.search(p, "AML/KYC 筛查", re.I) for p in fin))

    cpa = M.SKILL_ATOMS["CFA/FRM/CPA 金融资格"][0]
    check("「CPaaS」不误命中 CPA", not any(re.search(p, "CPaaS 集成", re.I) for p in cpa))
    check("「CFA、FRM、CPA」能命中", any(re.search(p, "（CFA、FRM、CPA）", re.I) for p in cpa))


# ── 3. A/B 测试四种写法 ────────────────────────────────────────────────
def test_ab_forms():
    print("\n[3] A/B 测试写法（baseline_v4.md 用的是带连字符的 A-B）")
    pats = M.SKILL_ATOMS["模型评估体系"][0]
    for s in ("A/B测试", "AB测试", "A-B 测试", "A B 测试", "效果评测", "评测体系"):
        check(f"命中「{s}」", any(re.search(p, s, re.I) for p in pats))


# ── 4. 「优先」只降级括号子句，不连坐整行 ─────────────────────────────
def test_pref_clause_split():
    print("\n[4] 括号内「优先」的降级范围")
    rest, pref = M._split_pref_clause("本科及以上学历（985 或 QS100 院校优先）")
    check("括号外条款留在 must", rest is not None and "本科及以上学历" in rest, str(rest))
    check("括号子句归 pref", pref is not None and "优先" in pref, str(pref))
    check("整行就是偏好句时不拆", M._split_pref_clause("有 AI 产品落地经验者优先")[0] is None)

    segs, _ = M.segment_jd("岗位职责\n岗位要求\n1. 本科及以上，3-5年产品经验(平台类、SaaS类产品优先)。")
    must_txt = "\n".join(segs["must"])
    pref_txt = "\n".join(segs["pref"])
    check("「3-5年产品经验」未被连带降级", "3-5年产品经验" in must_txt, must_txt)
    check("「平台类优先」归入 pref", "平台类" in pref_txt, pref_txt)


# ── 5. 打分文本必须是 JD 正文本体（防自我循环） ────────────────────────
ARCHIVE = """<!-- MATCHMETA tier=green score=1.0 company=甲 title=乙 date=2026-01-01 source=测试 url=none -->

## 硬门槛检查
- 🟢 无明显硬伤

## 双向匹配结果（JD ∧ 简历 ∧ 事实库）
- ⬜ **真缺口 1**：多模态
- 🔧 **可补缺口 1**：商业化/变现

## JD 原文摘录（匹配依据，链接只是溯源）
```
岗位职责
负责金融智能体产品设计，熟悉 RAG 与 Agent 编排。
任职要求
本科及以上学历（985 优先）
加分项
有 LangChain 经验者优先
【人工复核备注】
候选人 9 年算法经验，此处为 AI 批注，不是 JD 原文。
```
"""


def test_body_scope():
    print("\n[5] 打分/抽取的文本范围（防自我循环污染）")
    body = M.jd_body(ARCHIVE)
    check("剔除【人工复核备注】", "9 年算法经验" not in body)
    check("剔除「双向匹配结果」段（上一轮的产物）", "真缺口 1" not in body)
    check("剔除「硬门槛检查」段", "无明显硬伤" not in body)
    check("保留 JD 正文", "负责金融智能体产品设计" in body)

    atoms = M.extract_atoms(ARCHIVE)
    check("不吃「多模态」这类上一轮自己写的缺口",
          "多模态" not in atoms, f"误抽出：{sorted(atoms)}")
    check("「商业化/变现」不被当 JD 提及",
          "商业化/变现" not in atoms, f"误抽出：{sorted(atoms)}")
    check("真实提及的 LangChain 抽得到", "LangChain" in atoms, f"实抽：{sorted(atoms)}")

    # match_jd 与 rescore_pool 必须同口径，否则单份跑与全池重算对不上
    exc = re.search(r"##\s*JD\s*原文[^\n]*\n\s*```[^\n]*\n(.*?)\n\s*```", ARCHIVE, re.S)
    rescore_text = exc.group(1).split("【人工复核备注】")[0]
    check("与 rescore_pool 取文口径一致", rescore_text.strip() == body.strip())


# ── 6. HTML 去标签不能把英文粘住 ──────────────────────────────────────
def test_html_to_jd():
    print("\n[6] HTML → JD 正文的英文空格")
    html = ("<p>岗位职责</p><p>任职要求</p>"
            "<p>提示工程（<span>Prompt</span><span>Engineering</span>）、<b>RAG</b>等技术，熟悉GitHub</p>")
    out = P.html_to_jd(html)
    check("连续内联标签不粘连英文", "PromptEngineering" not in out, out)
    check("已还原为 Prompt Engineering", "Prompt Engineering" in out, out)
    check("中文夹标签不插空格（不留痕）",
          "学历" in P.html_to_jd("<p>岗位职责</p><p>本科及以上<span>学历</span>要求</p>"))

    # 粘连的英文会漏检 → 用真实 pattern 验证这条链是通的
    pats = M.SOFT_KEYWORDS["Prompt工程"]
    check("还原后 Prompt 能被命中", any(re.search(p, out, re.I) for p in pats))


# ── 7. 完整性闸门只认尾部截断痕迹 ─────────────────────────────────────
def test_completeness():
    print("\n[7] 完整性闸门")
    tail = "岗位职责\n负责智能体产品。\n任职要求\n" + "熟悉大模型应用。" * 30 + "\n（快照在此截断，任职要求原文未抓全）"
    lvl, _ = M.completeness_check(tail)
    check("尾部截断痕迹 → truncated", lvl == "truncated", lvl)

    mid = ("岗位职责\n负责智能体产品。\n（快照在此截断，任职要求原文未抓全）\n"
           + "【补录段｜来源：回源重抓】\n任职要求\n" + "熟悉大模型应用。" * 40)
    lvl, reasons = M.completeness_check(mid)
    check("中段截断痕迹 + 其后有内容 → 视为已补录", lvl == "", str(reasons))

    short = "岗位职责\n负责智能体产品。"
    check("正文过短 → truncated", M.completeness_check(short)[0] == "truncated")


def main():
    print("=" * 68)
    print("P1/P2 防回归自检 ｜ 每条断言对应一个已修 bug")
    print("=" * 68)
    for fn in (test_vocab_keys, test_word_boundary, test_ab_forms, test_pref_clause_split,
               test_body_scope, test_html_to_jd, test_completeness):
        fn()
    print("\n" + "=" * 68)
    if FAILURES:
        print(f"❌ 失败 {len(FAILURES)} 项：")
        for f in FAILURES:
            print(f"   · {f}")
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
