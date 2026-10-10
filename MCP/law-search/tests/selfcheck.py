# -*- coding: utf-8 -*-
"""
law-search 自检脚本（P2-3 Makefile 后端）。

四档：
    quick   纯函数级：compile + 全链 import + alias 指针 + Guard/QU/链路断言（不加载模型，秒级）
    smoke   检索级：加载 Embedding+Reranker+Chroma，2 题检索断言（~20s）
    e2e     端到端：1 题完整 Pipeline（含 LLM + Guard，~25s，需 LLM Provider 可用）
    ingest  知识库只读体检：chunk 数 / law_library 存在 / 快照存在（不加载模型）

用法：
    python tests/selfcheck.py --level quick   # 默认
    python tests/selfcheck.py --level smoke
    python tests/selfcheck.py --level e2e
    python tests/selfcheck.py --level ingest
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import py_compile
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
os.environ["TOKENIZERS_PARALLELISM"] = "false"

for _n in [
    "chromadb", "huggingface_hub", "httpx", "urllib3",
    "sentence_transformers", "FlagEmbedding", "openai",
]:
    logging.getLogger(_n).setLevel(logging.ERROR)

CORE = PROJECT_ROOT / "core"
COMPILE_TARGETS = [CORE, PROJECT_ROOT / "config",
                   PROJECT_ROOT / "ingest.py", PROJECT_ROOT / "server.py"]


# ============================================================
# quick
# ============================================================

def check_quick() -> None:
    print("== [1] compileall ==")
    n = 0
    for target in COMPILE_TARGETS:
        if target.is_dir():
            for py in sorted(target.rglob("*.py")):
                if "legacy" in py.parts:
                    continue
                py_compile.compile(str(py), doraise=True)
                n += 1
        else:
            py_compile.compile(str(target), doraise=True)
            n += 1
    print(f"   {n} 个文件编译通过")

    print("== [2] 全链 import（server 不触发模型加载）==")
    import core.common as common
    import core.evidence_fusion as fusion
    import core.citation_builder as builder
    import core.citation_formatter as formatter
    import core.evidence_schema as schema
    import core.query_understanding as qu
    import core.answer_guard as guard
    import server  # noqa: F401
    print("   core.* + server 导入通过")

    print("== [3] alias 指针对齐 ==")
    assert fusion._safe_float is common.safe_float
    assert fusion._safe_string is common.safe_string
    assert fusion._first_non_empty is common.first_non_empty
    assert fusion._get_metadata is common.get_metadata
    assert fusion._get_identity is common.get_identity
    assert fusion._normalize_identity is common.normalize_identity
    assert fusion._ROLE_TOKEN_MAP is common.ROLE_TOKEN_MAP
    assert builder._first_non_empty is common.first_non_empty
    assert builder._get_law_name is common.get_law_name
    assert builder._get_category is common.get_category
    assert builder._get_role is common.get_role
    assert builder._normalize_citation_type is schema.infer_citation_type
    assert formatter._first_non_empty is common.first_non_empty
    assert formatter._get_law_name is common.get_law_name
    assert formatter._get_category is common.get_category
    assert fusion._citation_type_from_role is schema.citation_type_from_role
    print("   16 个 thin-wrapper 指针恒等")

    print("== [4] Guard 行为 ==")
    g = guard.check_answer(
        "股东出资不实需承担连带责任，依据公司法第五十条。（必然）",
        evidence_text="公司法第五十条：股东未按期足额出资应当差额填补，其他发起人承担连带责任。",
    )
    assert g["evidence_coverage"] and 0 < g["evidence_coverage"] <= 1
    for w in g["warnings"]:
        assert "发现" not in w and "Evidence" not in w, f"warning 未精简: {w!r}"
    g2 = guard.check_answer("抱歉，本法律知识库未收录该问题。", evidence_text="x")
    assert g2["passed"] is True and g2["honest_refusal"] is True
    print(f"   coverage={g['evidence_coverage']:.0%} + 拒答豁免 + warning 精简 ✓")

    print("== [5] QU 断言（抽 6 题）==")
    cases = [
        ("公司法第3条", "article_lookup", {"公司法"}, {"第3条", "第三条"}),
        ("有限责任公司股东出资不实承担什么责任？", "case_analysis", {"公司法"}, set()),
        ("什么叫法人人格否认？", "definition", {"公司法"}, set()),
        ("民法典 第五百三十八条 债权人撤销权", "article_lookup", {"民法典"}, {"第538条"}),
        ("财产保全申请流程", "procedural", {"民事诉讼法"}, set()),
        ("有限公司虚假出资连带责任", "case_analysis", {"公司法"}, set()),
    ]
    for q, want_intent, want_laws, want_arts in cases:
        u = qu.understand(q)
        assert u["intent"] == want_intent, (q, u["intent"])
        assert want_laws <= set(u["law_names"]), (q, u["law_names"])
        if want_arts:
            assert want_arts <= set(u["where_hints"]["article_numbers"]), q
        assert 1 <= len(u["queries"]) <= 3
    print("   6 题 intent/laws/articles ✓")

    print("== [6] schema→builder→formatter→fusion 链路 ==")
    items = [
        {"document": "第五十条 股东出资不实的责任。", "metadata": {
            "law_name": "公司法", "citation_article": "第五十条", "page_number": 12,
            "law_identity": "正式法律", "source_category": "法律规定",
            "document_title": "中华人民共和国公司法", "filename": "company_law.pdf",
        }, "rerank_score": 0.9, "distance": 0.1},
        {"document": "专业书籍观点：出资不实应补足。", "metadata": {
            "law_identity": "专业参考", "source_category": "专业参考",
            "document_title": "公司法600问", "filename": "qa600.pdf",
        }, "rerank_score": 0.7, "distance": 0.3},
    ]
    normalized = [schema.normalize_evidence(dict(it)) for it in items]
    cits = builder.build_citations(normalized)
    assert cits[0]["citation_type"] == "law" and cits[0]["law_name"] == "公司法"
    assert cits[1]["citation_type"] == "professional_reference"
    assert len(formatter.format_lawyer_citations(cits)) == 2
    assert len(fusion.build_evidence_pack(normalized)) == 2
    print("   分类与输出 ✓")


# ============================================================
# smoke
# ============================================================

def check_smoke() -> None:
    from core.retrieval_service import get_retriever

    ret = get_retriever()
    for q in ["公司法第3条", "股东出资不实连带责任"]:
        items, timing = ret.search_and_rerank(q, top_k=5)
        unknown = sum(1 for it in items if it.get("citation_type") == "unknown")
        arts = sum(1 for it in items if it.get("article"))
        print(f"   {q!r}: hits={len(items)} unknown={unknown} arts>=1={arts} "
              f"total={timing.get('total_s', 0):.1f}s")
        assert len(items) >= 1, f"{q} 零命中"
        assert unknown == 0, f"{q} citation_type=unknown"
        assert arts >= 2, f"{q} article 填充过少"


# ============================================================
# e2e
# ============================================================

def check_e2e() -> None:
    import time as _time

    from core.pipeline import LegalPipeline

    deltas: list = []
    ttft: dict = {"s": None}

    def _on_delta(piece: str) -> None:
        if ttft["s"] is None:
            ttft["s"] = _time.perf_counter() - t0
        deltas.append(piece)

    pipe = LegalPipeline()
    t0 = _time.perf_counter()
    ans = pipe.ask(
        "有限责任公司股东出资不实承担什么责任？", top_k=5, on_delta=_on_delta
    )
    cov = ans["guard_coverage"]
    final = ans["final"] or ""
    lead_nl = len(final) - len(final.lstrip("\n"))
    print(f"   citations={len(ans['citations'])} coverage={cov} "
          f"warnings={ans['guard_warnings']} lead_nl={lead_nl} "
          f"ttft={ttft['s']:.1f}s deltas={len(deltas)} total={ans['total_s']:.1f}s")
    assert len(ans["evidence_items"]) >= 1
    assert len(ans["citations"]) >= 1
    assert cov is None or cov >= 0.3, cov
    assert lead_nl == 0
    assert deltas, "streaming on_delta 未收到任何增量"


# ============================================================
# ingest
# ============================================================

def check_ingest() -> None:
    import chromadb
    from config import CHROMA_PATH, HASH_INDEX_FILE

    print(f"== chroma 只读体检：{CHROMA_PATH} ==")
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    collections = client.list_collections()
    assert collections, "Chroma 中没有任何集合"
    total = 0
    for col in collections:
        cnt = col.count()
        total += cnt
        print(f"   {col.name}: {cnt} chunks")
        if col.name == "law_library":
            assert cnt >= 4000, f"law_library chunks 过少: {cnt}"
    assert total >= 4000, f"总 chunk 数异常: {total}"

    snap_dir = PROJECT_ROOT / "chroma_snapshots"
    snaps = sorted(snap_dir.glob("*.tar.gz"), reverse=True) if snap_dir.is_dir() else []
    print(f"   快照: {len(snaps)} 份（最新 {snaps[0].name if snaps else '无'}）")
    assert snaps, "没有任何快照（P1-3 snapshot_after_write 未生效或被清理）"

    hash_idx = Path(HASH_INDEX_FILE)
    print(f"   hash index: {hash_idx.name} exists={hash_idx.exists()}")


LEVELS = {
    "quick": check_quick,
    "smoke": check_smoke,
    "e2e": check_e2e,
    "ingest": check_ingest,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="law-search selfcheck")
    parser.add_argument("--level", choices=list(LEVELS), default="quick")
    args = parser.parse_args()

    print(f"\n===== selfcheck level={args.level} =====\n")
    try:
        LEVELS[args.level]()
    except AssertionError as exc:
        print(f"\nFAIL: {exc}")
        return 1
    except Exception:
        import traceback

        traceback.print_exc()
        return 2

    print(f"\nselfcheck[{args.level}] ALL PASS ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
