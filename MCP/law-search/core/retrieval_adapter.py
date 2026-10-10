# core/retrieval_adapter.py
"""
Chroma → Evidence Items 的 **纯浅层映射适配器**。

本层职责（严格限定）：
    1. 把 Chroma 返回的三段式数组（documents / metadatas / distances）
       解包为逐条 dict；
    2. 把外部传入的 rerank_scores 绑定到每条结果上；
    3. 原封不动保留原始 metadata 子字典。

本层不做（交由下游统一实现）：
    ❌ 不在顶层平铺 metadata 的 26 个子字段 — 由
       core.evidence_schema.normalize_evidence 统一完成；
    ❌ 不猜法律身份、不做类型归一化（safe_float/safe_string
       仅用于输入参数，不碰 metadata）。

设计依据（P0-2）：证据 item 可能来自 Chroma / 外部推送 / 手工构造。
不管来源如何，下游都会过 normalize_evidence 做一次统一格式化。
避免两套平铺逻辑并行造成字段漂移。
"""

from core.common import safe_float as _safe_float


def chroma_to_evidence_items(
    results,
    rerank_scores=None
):
    """
    Args:
        results: Chroma 返回的原生 dict：
            {"documents": [[...]], "metadatas": [[...]], "distances": [[...]]}
        rerank_scores: 与 documents[0] 等长的精排得分列表（可选）。

    Returns:
        List[dict]: 每个 item 只保证 4 个顶层字段：
            {"document": str, "metadata": dict,
             "rerank_score": float, "distance": float}
    """

    items = []

    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    for i, doc in enumerate(documents or []):
        meta = metadatas[i] if i < len(metadatas) else {}
        meta = meta if isinstance(meta, dict) else {}

        rerank_score = 0.0
        if rerank_scores is not None and i < len(rerank_scores):
            rerank_score = _safe_float(rerank_scores[i])

        distance = 0.0
        if i < len(distances):
            distance = _safe_float(distances[i])

        items.append({
            "document": doc,
            "metadata": meta,
            "rerank_score": rerank_score,
            "distance": distance,
        })

    return items
