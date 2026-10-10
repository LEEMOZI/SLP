"""
tests/run_benchmark.py — 法律问答真实问题测试集

覆盖范围（步骤 5）：
  - 民法典（合同编、物权编、婚姻家庭、侵权责任、诉讼时效）
  - 公司法（出资、股权、解散、章程）
  - 刑法（常见经济犯罪/责任年龄）
  - 劳动法/劳动合同法（辞退、加班、试用期）
  - 证券法（信息披露、内幕交易）
  - 民事诉讼法（管辖、举证）

两阶段：
  Phase A (快速): LegalRetriever 检索层质量评估（无 LLM，~1 题/6s）
  Phase B (完整): LegalPipeline 端到端（含 LLM + Guard，~1 题/20s）
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# ============================================================
# Test Suite
# ============================================================

@dataclass
class TestCase:
    id: str
    question: str
    category: str                          # 法律领域
    expected_keywords: List[str] = field(default_factory=list)  # 命中的 evidence 内容中若包含任一关键词则算 relevant
    expected_law_hint: str = ""            # 命中的 evidence_item 中 law_name / document_title / filename 应包含的词


# Phase A: 8 道覆盖检索质量（无 LLM）
PHASE_A_CASES: List[TestCase] = [
    # ---------- 民法典 ----------
    TestCase("A1", "民法典关于诉讼时效的规定",          "民法典", ["诉讼时效", "三年", "期间"],                      "民法典"),
    TestCase("A2", "合同解除权的行使期限",               "民法典", ["解除权", "一年", "催告", "期限"],                 "民法典|合同"),
    TestCase("A3", "夫妻共同债务的认定标准",             "民法典", ["共同债务", "夫妻共同", "追认", "家庭日常生活"], "民法典|婚姻"),

    # ---------- 公司法 ----------
    TestCase("A4", "股东出资不实的连带责任",             "公司法", ["出资不实", "补足差额", "连带责任", "出资不足"],   "公司法"),
    TestCase("A5", "股东会决议无效与可撤销的情形",       "公司法", ["决议无效", "可撤销", "股东会决议", "召集程序"],  "公司法"),

    # ---------- 刑法 ----------
    TestCase("A6", "刑事责任年龄的法律规定",             "刑法",   ["刑事责任年龄", "十四周岁", "十六周岁", "故意杀人"], "刑法|刑事"),

    # ---------- 劳动法 ----------
    TestCase("A7", "试用期最长可以约定多久？",            "劳动法", ["试用期", "劳动合同期限", "不得超过"],             "劳动|合同"),
    TestCase("A8", "违法解除劳动合同的赔偿金计算",        "劳动法", ["违法解除", "赔偿金", "经济补偿", "二倍"],         "劳动|合同"),
]

# Phase B: 5 道端到端（含 LLM）— 注意 LLM 调用成本 & 耗时
PHASE_B_CASES: List[TestCase] = [
    TestCase("B1", "有限责任公司股东出资不实应承担什么法律责任？设立时的其他股东是否承担连带责任？", "公司法",
             ["出资不实", "补足差额", "连带责任", "出资义务"],                "公司法"),
    TestCase("B2", "用人单位违法辞退员工，劳动者可以主张什么赔偿？",                        "劳动法",
             ["违法解除", "赔偿金", "二倍", "经济补偿", "解除劳动合同"],     "劳动|合同"),
    TestCase("B3", "夫妻一方在婚姻关系存续期间以个人名义超出家庭日常生活需要所负的债务，是否属于夫妻共同债务？", "民法典婚姻",
             ["共同债务", "追认", "共同意思表示", "家庭日常生活", "债权人"], "民法典|婚姻"),
    TestCase("B4", "内幕交易行为在证券法上应承担什么法律责任？",                          "证券法",
             ["内幕交易", "内幕信息", "没收违法所得", "罚款", "证券"],         "证券"),
    TestCase("B5", "向人民法院请求保护民事权利的诉讼时效期间是多久？有哪些特殊规定？",          "民法典总则",
             ["诉讼时效", "三年", "自知道", "权利受到损害", "二十年"],        "民法典|民法"),
]


# ============================================================
# Phase A: Retrieval-only 评估
# ============================================================

def run_phase_a(retriever, top_k: int = 8) -> dict:
    """纯检索层评估：看 top-k 候选是否命中预期关键词/法律来源。"""
    print("\n" + "=" * 88)
    print(f" PHASE A · 检索层质量评估 ({len(PHASE_A_CASES)} cases · top_k={top_k})")
    print("=" * 88)

    summary = {"phase": "A_retrieval", "top_k": top_k, "cases": [], "total_time_s": 0.0}
    t_all = time.perf_counter()

    for tc in PHASE_A_CASES:
        print(f"\n--- [{tc.id}] {tc.category}: {tc.question}")
        t0 = time.perf_counter()
        items, timing = retriever.search_and_rerank(tc.question, top_k=top_k)
        case_time = time.perf_counter() - t0

        # Relevance: 任一 evidence 内容包含 ≥1 个 expected_keywords 就算命中 (Hit@k)
        relevant_hits = 0
        law_hint_hits = 0
        import re
        law_pat = re.compile(tc.expected_law_hint, re.I) if tc.expected_law_hint else None
        for it in items:
            doc = (it.get("document") or "").lower()
            kw_hit = any(kw.lower() in doc for kw in tc.expected_keywords)
            if kw_hit:
                relevant_hits += 1
            if law_pat:
                blob = (
                    str(it.get("law_name", ""))
                    + " " + str(it.get("document_title", ""))
                    + " " + str(it.get("filename", ""))
                    + " " + str(it.get("title", ""))
                )
                if law_pat.search(blob):
                    law_hint_hits += 1

        recall_top1   = 1 if relevant_hits >= 1 else 0
        recall_top3   = 1 if sum(1 for it in items[:3] if any(kw.lower() in (it.get("document") or "").lower() for kw in tc.expected_keywords)) >= 1 else 0
        law_hit_rate  = law_hint_hits / max(1, len(items))
        avg_authority = sum(float(it.get("authority") or 0) for it in items) / max(1, len(items))
        avg_rerank    = sum(float(it.get("rerank_score") or 0) for it in items) / max(1, len(items))

        status = "✓" if recall_top1 else "✗"
        print(f"     {status} Keyword Hit@1={recall_top1} · Hit@3={recall_top3} · "
              f"Relevant in top-{top_k}={relevant_hits}/{len(items)} · "
              f"LawHintMatch={law_hint_hits}/{len(items)} · "
              f"AvgAuthority={avg_authority:.0f} · "
              f"Time={case_time:.1f}s")
        if not recall_top1:
            # 诊断：打印 top-3 law_name + 内容预览
            print(f"     ⚠ 可能漏召回，Top-3 命中:")
            for idx, it in enumerate(items[:3]):
                content_preview = (it.get("document") or "")[:80].replace("\n", "↵")
                print(f"       [{idx+1}] {it.get('law_name','?')[:30]} | {it.get('article','?')} | {content_preview!r}")

        case_summary = {
            "id": tc.id,
            "category": tc.category,
            "question": tc.question,
            "time_s": round(case_time, 2),
            "retrieved": len(items),
            "candidates": timing.get("candidates"),
            "relevant_count_in_topk": relevant_hits,
            "law_hint_match_count": law_hint_hits,
            "recall_hit_at_1": bool(recall_top1),
            "recall_hit_at_3": bool(recall_top3),
            "avg_authority": round(avg_authority, 1),
            "avg_rerank_score": round(avg_rerank, 4),
            "top_hits": [
                {
                    "rank": idx + 1,
                    "law_name": str(it.get("law_name", ""))[:50],
                    "article": str(it.get("article", ""))[:20],
                    "source": str(it.get("source_document") or it.get("title", ""))[:60],
                    "rerank_score": round(float(it.get("rerank_score") or 0), 4),
                    "authority": float(it.get("authority") or 0),
                }
                for idx, it in enumerate(items[:5])
            ],
        }
        summary["cases"].append(case_summary)

    summary["total_time_s"] = round(time.perf_counter() - t_all, 2)
    # 汇总指标
    n = len(summary["cases"])
    summary["aggregate"] = {
        "Hit@1_rate": round(sum(1 for c in summary["cases"] if c["recall_hit_at_1"]) / max(1, n), 2),
        "Hit@3_rate": round(sum(1 for c in summary["cases"] if c["recall_hit_at_3"]) / max(1, n), 2),
        "avg_relevant_per_query": round(sum(c["relevant_count_in_topk"] for c in summary["cases"]) / max(1, n), 2),
        "avg_lawhint_match_per_query": round(sum(c["law_hint_match_count"] for c in summary["cases"]) / max(1, n), 2),
        "avg_authority": round(sum(c["avg_authority"] for c in summary["cases"]) / max(1, n), 1),
        "avg_time_per_query_s": round(sum(c["time_s"] for c in summary["cases"]) / max(1, n), 2),
    }
    return summary


# ============================================================
# Phase B: 完整端到端 Pipeline 评估
# ============================================================

def run_phase_b(pipeline, top_k: int = 8) -> dict:
    """端到端 Pipeline 评估（含 LLM + Answer Guard）。"""
    print("\n" + "=" * 88)
    print(f" PHASE B · 端到端管线评估 ({len(PHASE_B_CASES)} cases · top_k={top_k})")
    print("=" * 88)

    summary = {"phase": "B_end2end", "top_k": top_k, "cases": [], "total_time_s": 0.0}
    t_all = time.perf_counter()

    for tc in PHASE_B_CASES:
        print(f"\n{'='*60}")
        print(f" [{tc.id}] {tc.category}: {tc.question}")
        print(f"{'='*60}")
        t0 = time.perf_counter()
        r = pipeline.ask(tc.question, top_k=top_k, return_prompt=False)
        case_time = time.perf_counter() - t0

        n_chunks = len(r["evidence_items"])
        ev_text = "\n".join(e.get("document", "") for e in r["evidence_items"]).lower()
        ev_kw_hit = sum(1 for kw in tc.expected_keywords if kw.lower() in ev_text)
        ev_kw_cover = ev_kw_hit / max(1, len(tc.expected_keywords))

        final_lower = r["final"].lower()
        ans_kw_hit = sum(1 for kw in tc.expected_keywords if kw.lower() in final_lower)
        ans_kw_cover = ans_kw_hit / max(1, len(tc.expected_keywords))

        status = "✓" if r["guard_passed"] else "⚠"
        print(f"     {status} Guard={r['guard_passed']} · Warnings={len(r['guard_warnings'])} · "
              f"Coverage={r['guard_coverage']} · "
              f"AnswerKWCover={ans_kw_cover:.0%} · "
              f"EvidenceKWCover={ev_kw_cover:.0%} · "
              f"Chunks={n_chunks} · Time={case_time:.1f}s")

        # Answer 预览（前 400 chars）
        print(f"     Answer preview: {r['final'][:300]!r}...")
        if r["guard_warnings"]:
            print(f"     Guard warnings: {r['guard_warnings']}")

        case_summary = {
            "id": tc.id,
            "category": tc.category,
            "question": tc.question,
            "time_s": round(case_time, 2),
            "evidence_items": n_chunks,
            "evidence_pack": len(r["evidence_pack"]),
            "citations": len(r["citations"]),
            "answer_chars": len(r["answer"]),
            "final_chars": len(r["final"]),
            "guard_passed": r["guard_passed"],
            "guard_warnings_count": len(r["guard_warnings"]),
            "guard_warnings": r["guard_warnings"],
            "guard_coverage": r["guard_coverage"],
            "evidence_kw_coverage": round(ev_kw_cover, 2),
            "answer_kw_coverage": round(ans_kw_cover, 2),
            "citations_sample": [
                {
                    "id": c.get("citation_id"),
                    "type": c.get("citation_type"),
                    "law": c.get("law_name"),
                    "article": c.get("article"),
                    "source": c.get("source"),
                    "page": c.get("page"),
                }
                for c in r["citations"][:5]
            ],
            "timing_detail": {k: round(v, 2) for k, v in r["timing_s"].items() if isinstance(v, float)},
        }
        summary["cases"].append(case_summary)

    summary["total_time_s"] = round(time.perf_counter() - t_all, 2)
    n = len(summary["cases"])
    summary["aggregate"] = {
        "guard_pass_rate":    round(sum(1 for c in summary["cases"] if c["guard_passed"]) / max(1, n), 2),
        "avg_warnings":       round(sum(c["guard_warnings_count"] for c in summary["cases"]) / max(1, n), 2),
        "avg_guard_coverage": round(sum(c["guard_coverage"] or 0 for c in summary["cases"]) / max(1, n), 2),
        "avg_ans_kw_cover":   round(sum(c["answer_kw_coverage"] for c in summary["cases"]) / max(1, n), 2),
        "avg_ev_kw_cover":    round(sum(c["evidence_kw_coverage"] for c in summary["cases"]) / max(1, n), 2),
        "avg_time_s":         round(sum(c["time_s"] for c in summary["cases"]) / max(1, n), 1),
    }
    return summary


# ============================================================
# CLI
# ============================================================

def _print_phase_report(tag: str, summary: dict):
    print(f"\n{'#'*88}")
    print(f"## BENCHMARK REPORT · PHASE {tag}")
    print(f"{'#'*88}")
    agg = summary.get("aggregate", {})
    for k, v in agg.items():
        print(f"   {k:30s} = {v}")
    print(f"   total_time_s                 = {summary.get('total_time_s')}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # 减少噪声
    for nm in ["sentence_transformers", "chromadb", "huggingface_hub", "httpx", "urllib3"]:
        logging.getLogger(nm).setLevel(logging.WARNING)

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")

    phases = set(sys.argv[1:]) or {"A", "B"}

    if "A" in phases:
        from core.retrieval_service import LegalRetriever
        retriever = LegalRetriever()
        report_a = run_phase_a(retriever, top_k=8)
        _print_phase_report("A (Retrieval)", report_a)
        out_a = out_dir / f"benchmark_phaseA_{stamp}.json"
        out_a.write_text(json.dumps(report_a, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n📄 详细结果已保存: {out_a}")

    if "B" in phases:
        from core.pipeline import LegalPipeline
        pipeline = LegalPipeline()
        report_b = run_phase_b(pipeline, top_k=8)
        _print_phase_report("B (End-to-End)", report_b)
        out_b = out_dir / f"benchmark_phaseB_{stamp}.json"
        out_b.write_text(json.dumps(report_b, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n📄 详细结果已保存: {out_b}")


if __name__ == "__main__":
    main()
