#!/usr/bin/env python3
"""抽查测试：验证本地 KB 中 9 部法律的版本是否为最新。

直接调用 core.pipeline，不经过 MCP 传输。
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault("LLM_PROVIDER", "auto")

from core.pipeline import LegalPipeline

pipeline = LegalPipeline()

# 抽查问题清单：每个问题设计为"只有知道最新版本才能正确回答"
TESTS = [
    {
        "id": "T1_公司法",
        "question": "2024年7月1日施行的新公司法关于注册资本认缴期限的规定是什么？",
        "expect_keywords": ["五年", "5年", "认缴", "出资"],
        "risk": "高",
        "note": "新公司法第47条：有限责任公司全体股东认缴的出资额由股东按照公司章程的规定自公司成立之日起五年内缴足",
    },
    {
        "id": "T2_刑法",
        "question": "刑法修正案十二对行贿罪处罚做了什么修改？",
        "expect_keywords": ["行贿", "加重", "从重", "处罚", "修正"],
        "risk": "中",
        "note": "修正案十二加大了对行贿行为的惩治力度",
    },
    {
        "id": "T3_证券法",
        "question": "证券法关于证券发行注册制的规定",
        "expect_keywords": ["注册制", "注册", "发行"],
        "risk": "低",
        "note": "2019修订版确立了注册制，确认本地版本正确",
    },
    {
        "id": "T4_民法典合同编解释",
        "question": "民法典合同编通则司法解释关于格式条款的规定",
        "expect_keywords": ["格式条款", "提示", "说明", "义务"],
        "risk": "中",
        "note": "法释〔2023〕13号有专门的格式条款规定",
    },
    {
        "id": "T5_公司法司法解释",
        "question": "新公司法施行后，原公司法司法解释是否继续适用？时间效力如何衔接？",
        "expect_keywords": ["时间效力", "法释", "2024", "衔接"],
        "risk": "中",
        "note": "法释〔2024〕7号规定了新旧衔接规则",
    },
]

print("=" * 70)
print("法律版本抽查测试（2026-09-03）")
print("=" * 70)

results = []
for t in TESTS:
    print(f"\n{'─' * 60}")
    print(f"[{t['id']}] 风险: {t['risk']}")
    print(f"问题: {t['question']}")
    print(f"预期: {t['note']}")
    print(f"{'─' * 60}")

    try:
        answer = pipeline.search_law(t["question"], top_k=5)
        # 检查关键词命中
        hits = [kw for kw in t["expect_keywords"] if kw in answer]
        hit_rate = len(hits) / len(t["expect_keywords"]) * 100

        # 截取前 500 字符展示
        preview = answer[:500].replace("\n", "\n  ")
        print(f"\n回答（前500字）:\n  {preview}")
        print(f"\n关键词命中: {len(hits)}/{len(t['expect_keywords'])} ({hit_rate:.0f}%)")
        print(f"命中词: {hits}")

        status = "✅ PASS" if hit_rate >= 50 else "⚠️ PARTIAL" if hit_rate >= 25 else "❌ FAIL"
        results.append((t["id"], status, hit_rate, t["risk"]))
        print(f"结论: {status}")

    except Exception as e:
        print(f"\n❌ 错误: {e}")
        results.append((t["id"], "❌ ERROR", 0, t["risk"]))

# 汇总
print(f"\n\n{'=' * 70}")
print("汇总")
print(f"{'=' * 70}")
print(f"{'ID':<20} {'状态':<12} {'命中率':<10} {'风险'}")
print("-" * 50)
for rid, status, rate, risk in results:
    print(f"{rid:<20} {status:<12} {rate:>6.0f}%     {risk}")

passed = sum(1 for _, s, _, _ in results if "PASS" in s)
total = len(results)
print(f"\n通过: {passed}/{total}")
