import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_DEFAULT_RETRIEVER = None
_RETRIEVER_LOCK = threading.Lock()


def get_default_retriever() -> Any:
    global _DEFAULT_RETRIEVER
    if _DEFAULT_RETRIEVER is None:
        with _RETRIEVER_LOCK:
            if _DEFAULT_RETRIEVER is None:
                law_root = str(Path(__file__).resolve().parents[2] / "MCP" / "law-search")
                if law_root not in sys.path:
                    sys.path.insert(0, law_root)
                from core.retrieval_service import get_retriever

                _DEFAULT_RETRIEVER = get_retriever()
    return _DEFAULT_RETRIEVER


def _first_text(item: Dict[str, Any], metadata: Dict[str, Any], names: List[str]) -> str:
    for name in names:
        value = item.get(name)
        if value is None:
            value = metadata.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _locator(item: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    existing = item.get("locator")
    if isinstance(existing, dict):
        return dict(existing)
    locator = {}
    article = _first_text(item, metadata, ["article", "citation_article"])
    page = item.get("page")
    if page is None or page == "":
        page = metadata.get("page_number")
    section = _first_text(item, metadata, ["section", "chapter", "heading"])
    if article:
        locator["article"] = article
    if page is not None and page != "":
        locator["page"] = page
    if section:
        locator["section"] = section
    return locator


def _source_ref(item: Dict[str, Any], metadata: Dict[str, Any]) -> Dict[str, Any]:
    existing = item.get("source_ref")
    if isinstance(existing, dict):
        return dict(existing)
    if existing is not None and str(existing).strip():
        return {"id": str(existing)}
    for key in ("chunk_id", "document_id", "id"):
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return {key: str(value)}
    return {}


def _normalize_result(item: Dict[str, Any], rank: int, source: str) -> Dict[str, Any]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    content = _first_text(item, metadata, ["content", "document", "text"])
    excerpt = _first_text(item, metadata, ["excerpt", "summary"]) or content[:500]
    law_name = _first_text(item, metadata, ["law_name", "document_title"])
    article = _first_text(item, metadata, ["article", "citation_article"])
    title = _first_text(item, metadata, ["title", "case_name", "document_title"])
    if law_name and law_name not in title:
        title = (law_name + " " + article).strip()
    if not title:
        title = {"law": "法律法规", "guiding_cases": "指导案例", "reference": "业务参考"}[source]
    source_label = _first_text(
        item, metadata, ["source_label", "source_document", "document_title", "law_name"]
    ) or {"law": "法律法规库", "guiding_cases": "指导案例库", "reference": "业务参考库"}[source]
    score = item.get("rerank_score", item.get("score"))
    try:
        score = float(score) if score is not None else None
    except (TypeError, ValueError):
        score = None
    return {
        "title": title,
        "source_label": source_label,
        "excerpt": excerpt,
        "content": content,
        "hit_reason": _first_text(item, metadata, ["hit_reason"]) or "向量召回并经精排命中",
        "match_type": _first_text(item, metadata, ["match_type"]) or "semantic",
        "rank": rank,
        "score": score,
        "locator": _locator(item, metadata),
        "source_ref": _source_ref(item, metadata),
        "metadata": dict(metadata),
    }


def _response(
    source: str,
    query: str,
    filters: Dict[str, Any],
    executed_queries: List[Dict[str, str]],
    coverage: Dict[str, Any],
    results: List[Dict[str, Any]],
    status: str = "success",
    error: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    return {
        "source": source,
        "status": status,
        "query": query,
        "filters": filters,
        "executed_queries": executed_queries,
        "coverage": coverage,
        "results": results,
        "error": error,
    }


def search(
    source: str,
    query: str,
    limit: int = 8,
    filters: Optional[Dict[str, Any]] = None,
    retriever: Any = None,
) -> Dict[str, Any]:
    filters = dict(filters or {})
    if filters:
        return _response(
            source,
            query,
            filters,
            [],
            {"status": "failed", "applied_filters": []},
            [],
            status="failed",
            error={
                "code": "unsupported_filters",
                "message": "该检索源不支持显式结构化过滤器",
            },
        )
    executed = [{"query": query, "kind": "primary", "status": "submitted"}]
    try:
        engine = retriever if retriever is not None else get_default_retriever()
        if source == "law":
            items, _timing = engine.search_and_rerank(query, top_k=limit)
        elif source == "guiding_cases":
            items, _timing = engine.search_cases(query, top_k=limit)
        elif source == "reference":
            items, _timing = engine.search_reference(query, top_k=limit)
        else:
            raise ValueError("unknown source")
        normalized = [
            _normalize_result(item, rank, source)
            for rank, item in enumerate(items, 1)
            if isinstance(item, dict)
        ]
        coverage = {
            "status": "completed",
            "returned": len(normalized),
            "internal_query_trace": "complete",
            "trace_limitations": [],
        }
        if source == "law":
            coverage.update(
                {
                    "internal_query_trace": "unavailable",
                    "open_query_fallback": "not_observable",
                    "trace_limitations": [
                        "现有 search_and_rerank 不暴露内部归一化、开放查询回退和扩展查询的实际执行 trace；executed_queries 仅记录适配器提交的主查询。"
                    ],
                }
            )
        return _response(source, query, filters, executed, coverage, normalized)
    except Exception:
        return _response(
            source,
            query,
            filters,
            executed,
            {"status": "failed", "returned": 0},
            [],
            status="failed",
            error={"code": "retrieval_failed", "message": "检索源暂不可用"},
        )
