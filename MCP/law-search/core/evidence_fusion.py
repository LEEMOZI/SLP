# core/evidence_fusion.py

"""
Evidence Fusion V6.3

职责：

    Chroma / Retrieval Evidence
            ↓
    Evidence Identity Normalization
            ↓
    Evidence Score
            ↓
    Evidence Pack

核心原则：

1. 法律身份字段彼此独立。
2. 不允许 citation_type=law 强行覆盖明确的法律身份。
3. 不允许 source_type 冒充 source_category。
4. law_identity / source_category / law_level 优先于模糊字段。
5. 不根据标题随意猜测法律身份。
6. 专业参考不得被归类为正式法律。
7. 司法解释不得被归类为正式法律。
8. Evidence Pack 输出统一字段。
"""


# ============================================================
# Category Weight
# ============================================================

CATEGORY_WEIGHT = {

    "法律规定": 100,

    "司法解释": 90,

    "最高院观点": 70,

    "学理观点": 40,

    "专业参考": 30,

    "其他观点": 20,

}


# ============================================================
# Shared utility（唯一真相源：core/common.py）
# —— Thin wrapper：对调用方保持 _xxx 私有命名不变，
# 避免 legacy / 外部 import 出现雪崩式 NameError。
# ============================================================

from core.common import (
    safe_float as _safe_float,
    safe_string as _safe_string,
    first_non_empty as _first_non_empty,
    get_metadata as _get_metadata,
    get_identity as _get_identity,
    normalize_identity as _normalize_identity,
    ROLE_TOKEN_MAP as _ROLE_TOKEN_MAP,
)
from core.evidence_schema import citation_type_from_role as _citation_type_from_role
from core.doc_classifier import (
    STATUS_MULTIPLIER as _STATUS_MULTIPLIER,
    enrich_evidence_item as _enrich_evidence_item,
)


# ============================================================
# Normalize Role
# ============================================================

def normalize_role(
    item,
):
    """
    统一 Evidence Role。

    核心规则：

    1. 明确法律身份优先。
    2. citation_type 不能覆盖明确身份。
    3. 不根据标题猜测法律身份。
    4. 专业参考不得成为 statute。
    """

    if not isinstance(
        item,
        dict,
    ):
        return ""

    metadata = _get_metadata(
        item
    )

    # ========================================================
    # 1. 首先读取明确法律身份
    # ========================================================

    identity = _get_identity(
        item
    )

    normalized_identity = _normalize_identity(
        identity
    )

    # ========================================================
    # 2. 明确身份具有最高优先级
    # ========================================================

    if normalized_identity:
        return normalized_identity

    # ========================================================
    # 3. 如果没有明确身份，再读取已有 evidence_role
    #
    # 注意：
    # 这里只接受已有角色，不根据标题猜。
    # ========================================================

    existing_role = _first_non_empty(

        item.get(
            "evidence_role"
        ),

        metadata.get(
            "evidence_role"
        ),

        item.get(
            "role"
        ),

        metadata.get(
            "role"
        ),

    )

    # ------------------------------------------------------
    # 角色别名词典化（P1-5：唯一真相源 core.common.ROLE_TOKEN_MAP）
    # ------------------------------------------------------

    normalized_role = _ROLE_TOKEN_MAP.get(
        _safe_string(existing_role),
        "",
    )

    if normalized_role:
        return normalized_role

    # ========================================================
    # 4. 最后才读取 citation_type
    #
    # citation_type=law 只能在没有任何明确身份时
    # 作为弱依据。
    # ========================================================

    citation_type = _first_non_empty(

        item.get(
            "citation_type"
        ),

        metadata.get(
            "citation_type"
        ),

    )

    citation_type = _safe_string(
        citation_type
    ).lower()

    if citation_type in {
        "judicial",
        "judicial_rule",
        "judicial_interpretation",
    }:
        return "judicial_rule"

    if citation_type in {
        "court_guidance",
        "court_opinion",
        "supreme_court",
    }:
        return "court_guidance"

    if citation_type in {
        "academic",
        "professional_view",
        "commentary",
    }:
        return "professional_view"

    if citation_type in {
        "law",
        "statute",
    }:
        return "statute"

    # ========================================================
    # 5. 完全无法确定
    # ========================================================

    return ""


# ============================================================
# Normalize Category
# ============================================================

def normalize_category(item):

    role = normalize_role(item)

    if role == "statute":
        return "法律规定"

    if role == "judicial_rule":
        return "司法解释"

    if role == "court_guidance":
        return "最高院观点"

    if role == "professional_reference":
        return "专业参考"

    if role == "professional_view":
        return "学理观点"

    if role == "academic":
        return "学理观点"

    return "其他观点"

# ============================================================
# Calculate Score
# ============================================================

def calculate_score(
    item,
):
    """
    Evidence 综合评分。

    Score 组成（2026-09-02 量纲修复）：

        三分量各自归一化到 0-1，按 5:3:2 加权：
            rerank_norm*5.0      相关性主导
          + authority_norm*status_multiplier*3.0   权威度 + 时效乘数
          + category_norm*2.0  类别 tiebreaker

    量纲修复原因：
        旧公式 authority_score = authority*10（最高 1000）×0.3 = 最高 300 分，
        而 rerank*0.5 只有 ±5 分——权威项实际是常量主导
        （老库 source_priority 全部=10 → authority 恒 100），
        0.5/0.3/0.2 权重设计完全失效，排序退化为 category 弱区分。
    """

    # ========================================================
    # Rerank
    # ========================================================

    rerank = _first_non_empty(

        item.get(
            "rerank_score"
        ),

        item.get(
            "score"
        ),

        0,

    )

    rerank = _safe_float(
        rerank,
        0.0,
    )

    # ========================================================
    # Authority
    # ========================================================

    authority = _safe_float(
        item.get(
            "authority",
            0,
        ),
        0.0,
    )

    # ========================================================
    # Category
    # ========================================================

    category = _safe_string(
        item.get(
            "category",
            "其他观点",
        )
    )

    # ========================================================
    # 时效性乘数（doc_status：repealed → 强降权）
    # 旧库 chunk 无 doc_status 时由 enrich_evidence_item
    # 按文件名推断（硬编码 "current" 不可信，以推断为准）
    # ========================================================

    status_multiplier = _STATUS_MULTIPLIER.get(
        _safe_string(
            item.get(
                "doc_status",
                "current",
            )
        )
        or "current",
        1.0,
    )

    # ========================================================
    # 归一化 + 加权
    # ========================================================

    # rerank：bge-reranker logits，>0 相关；截断 [0,10] → 0-1
    rerank_norm = min(max(rerank, 0.0), 10.0) / 10.0

    # authority：新老双量纲（0-50 / 0-100）统一截断 → 0-1
    authority_norm = min(max(authority, 0.0), 100.0) / 100.0

    # category：CATEGORY_WEIGHT 20-100 → 0-1
    category_norm = min(
        max(
            _safe_float(
                CATEGORY_WEIGHT.get(
                    category,
                    0,
                ),
                0.0,
            ),
            0.0,
        ),
        100.0,
    ) / 100.0

    final_score = (
        rerank_norm * 5.0
        +
        authority_norm * status_multiplier * 3.0
        +
        category_norm * 2.0
    )

    return round(
        final_score,
        3,
    )


# ============================================================
# Remove Duplicate
# ============================================================

def remove_duplicate(
    items,
):
    """
    删除重复 Evidence。

    去重键：

        source_document
        citation_article
        document 前 80 字符
    """

    seen = set()

    result = []

    for item in items:

        if not isinstance(
            item,
            dict,
        ):
            continue

        metadata = _get_metadata(
            item
        )

        source_document = _first_non_empty(

            item.get(
                "source_document"
            ),

            metadata.get(
                "source_document"
            ),

            metadata.get(
                "document_title"
            ),

            metadata.get(
                "filename"
            ),

        )

        article = _first_non_empty(

            item.get(
                "article"
            ),

            item.get(
                "citation_article"
            ),

            metadata.get(
                "citation_article"
            ),

            metadata.get(
                "article_list"
            ),

        )

        document = _first_non_empty(

            item.get(
                "document"
            ),

            item.get(
                "content"
            ),

            "",

        )

        key = (

            _safe_string(
                source_document
            ),

            _safe_string(
                article
            ),

            _safe_string(
                document
            )[:80],

        )

        if key in seen:
            continue

        seen.add(
            key
        )

        result.append(
            item
        )

    return result


# ============================================================
# Build Evidence Pack
# ============================================================

def build_evidence_pack(
    items,
):
    """
    构建统一 Evidence Pack。
    """

    from core.evidence_schema import (
        normalize_evidence,
    )

    if not items:
        return []

    # ========================================================
    # 1. 去重
    # ========================================================

    items = remove_duplicate(
        items
    )

    # ========================================================
    # 2. Evidence Schema Normalize（幂等兜底）
    #
    # 说明：来自「retrieval_service.search_and_rerank」的证据已经在
    # 检索出口端执行过一次 normalize_evidence（P0-2 · 单点归一化）。
    # 这里再次调用是为了兼容「手工构造证据 / 非 Chroma 来源」的场景。
    # normalize_evidence 内部是 item.copy() + 类型收敛 + safe_*，
    # 幂等、性能开销可接受。
    # ========================================================

    normalized_items = []

    for item in items:

        if not isinstance(
            item,
            dict,
        ):
            continue

        normalized_items.append(
            normalize_evidence(
                item
            )
        )

    # ========================================================
    # 3. Identity / Category / Score
    # ========================================================

    pack = []

    for index, item in enumerate(
        normalized_items,
        start=1,
    ):

        item = item.copy()

        # ----------------------------------------------------
        # Role
        # ----------------------------------------------------

        role = normalize_role(
            item
        )

        # ----------------------------------------------------
        # Category
        # ----------------------------------------------------

        category = normalize_category(
            item
        )

        # ----------------------------------------------------
        # Doc 画像增强（新库读 chunk 元数据；
        # 旧库无元数据时按文件名实时推断，纯 CPU）
        # ----------------------------------------------------

        doc_meta = _enrich_evidence_item(item)

        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        score = calculate_score(
            {
                **item,
                "category": category,
                **doc_meta,
            }
        )

        # ----------------------------------------------------
        # Metadata
        # ----------------------------------------------------

        metadata = _get_metadata(
            item
        )

        # ----------------------------------------------------
        # Evidence ID
        # ----------------------------------------------------

        evidence_id = (
            item.get(
                "evidence_id"
            )
            or f"E{index}"
        )

        # ----------------------------------------------------
        # Title
        # ----------------------------------------------------

        title = _first_non_empty(

            item.get(
                "title"
            ),

            metadata.get(
                "document_title"
            ),

            metadata.get(
                "source_document"
            ),

            metadata.get(
                "filename"
            ),

        )

        # ----------------------------------------------------
        # Content
        # ----------------------------------------------------

        content = _first_non_empty(

            item.get(
                "content"
            ),

            item.get(
                "document"
            ),

        )

        # ----------------------------------------------------
        # Article
        # ----------------------------------------------------

        article = _first_non_empty(

            item.get(
                "article"
            ),

            item.get(
                "citation_article"
            ),

            metadata.get(
                "citation_article"
            ),

            metadata.get(
                "article_list"
            ),

        )

        # ----------------------------------------------------
        # Page
        # ----------------------------------------------------

        page = _first_non_empty(

            item.get(
                "page"
            ),

            item.get(
                "page_number"
            ),

            metadata.get(
                "page_number"
            ),

        )

        # ----------------------------------------------------
        # Law Name
        # ----------------------------------------------------

        law_name = _first_non_empty(

            item.get(
                "law_name"
            ),

            metadata.get(
                "law_name"
            ),

        )

        # ----------------------------------------------------
        # Source Document
        # ----------------------------------------------------

        source_document = _first_non_empty(

            item.get(
                "source_document"
            ),

            metadata.get(
                "source_document"
            ),

            metadata.get(
                "document_title"
            ),

            metadata.get(
                "filename"
            ),

        )

        # ----------------------------------------------------
        # Citation Name
        # ----------------------------------------------------

        citation_name = _first_non_empty(

            item.get(
                "citation_name"
            ),

            metadata.get(
                "citation_name"
            ),

        )

        # ----------------------------------------------------
        # Citation Type
        #
        # Role 与 citation_type 是两个独立维度。
        #
        # 前五类可以由明确 Role 决定（P1-5：映射表唯一真相源
        # evidence_schema.CITATION_TYPE_FROM_ROLE）；
        # court_case 没有对应 Role，因此保留
        # normalize_evidence() 已经确定的 citation_type。
        # ----------------------------------------------------

        mapped_type = _citation_type_from_role(role)

        if mapped_type:

            citation_type = mapped_type

        else:

            # ------------------------------------------------
            # 没有 Role 的 Evidence
            #
            # 典型情况：
            #
            # court_case
            # unknown
            #
            # court_case 必须保留为 court_case，
            # 不能因为 role 为空而降级成 unknown。
            # ------------------------------------------------

            raw_citation_type = _safe_string(
                item.get(
                    "citation_type",
                    ""
                )
            ).lower()

            if raw_citation_type in {
                "court_case",
                "case",
                "judicial_case",
                "司法案例",
            }:

                citation_type = "court_case"

            else:

                citation_type = "unknown"


        item["citation_type"] = citation_type


        # ----------------------------------------------------
        # Authority
        # ----------------------------------------------------

        authority = _safe_float(

            _first_non_empty(

                item.get(
                    "authority"
                ),

                metadata.get(
                    "authority"
                ),

                metadata.get(
                    "source_priority"
                ),

                0,

            ),

            0.0,

        )

        # 权威度回填（旧库 chunk 无 authority 元数据时按 doc_type 推断）
        if authority <= 0:
            authority = _safe_float(
                doc_meta.get("authority", 0),
                0.0,
            )

        # ----------------------------------------------------
        # Rerank
        # ----------------------------------------------------

        rerank_score = _safe_float(

            _first_non_empty(

                item.get(
                    "rerank_score"
                ),

                item.get(
                    "score"
                ),

                0,

            ),

            0.0,

        )

        # ====================================================
        # Final Evidence Item
        # ====================================================

        pack.append({

            "evidence_id":
                evidence_id,

            "title":
                title,

            "role":
                role,

            "category":
                category,

            "source_type":
                item.get(
                    "source_type",
                    "",
                ),

            "score":
                score,

            "content":
                content,

            "authority":
                authority,

            "rerank_score":
                rerank_score,

            "article":
                article,

            "page":
                page,

            "law_name":
                law_name,

            "source_document":
                source_document,

            "citation_name":
                citation_name,

            "citation_type":
                citation_type,

            "doc_type":
                doc_meta.get("doc_type", ""),

            "doc_status":
                doc_meta.get("doc_status", ""),

            "doc_version":
                doc_meta.get("doc_version", ""),

            "domain":
                doc_meta.get("domain", ""),

            "metadata":
                metadata,

        })

    # ========================================================
    # 4. Score 排序
    # ========================================================

    pack.sort(

        key=lambda x:
            x.get(
                "score",
                0.0,
            ),

        reverse=True,

    )

    return pack
