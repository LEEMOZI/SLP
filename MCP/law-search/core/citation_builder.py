# core/citation_builder.py
"""
引用构建器（builder）。

共享 utility 统一来自 core/common.py；
citation_type 推断统一走 evidence_schema.infer_citation_type（P0-2）。
本文件保留 _normalize_citation_type 私有名做 thin-wrapper 兼容转发，
避免旧调用（含 legacy/ 中遗留测试）雪崩。
"""

from core.common import (
    first_non_empty as _first_non_empty,
    get_law_name as _get_law_name,
    get_category as _get_category,
    get_role as _get_role,
)
from core.evidence_schema import infer_citation_type as _normalize_citation_type

def build_citations(results):

    citations = []

    for item in results:

        if not isinstance(item, dict):
            continue
        metadata = item.get(
        "metadata",
        {}
    )

        if not isinstance(metadata, dict):
            metadata = {}

        # ==================================================
        # citation_type
        # ==================================================

        citation_type = _normalize_citation_type(
            item,
            metadata
        )

        # ==================================================
        # 来源文献
        #
        # 注意：
        # 司法解释汇编中 source_document 可能为空，
        # 此时使用 document_title / filename。
        # ==================================================

        source_document = _first_non_empty(

            item.get(
                "source_document"
            ),

            metadata.get(
                "source_document"
            ),

            item.get(
                "title"
            ),

            metadata.get(
                "document_title"
            ),

            metadata.get(
                "filename"
            ),

        )

        # ==================================================
        # 法律名称
        #
        # 只有 Evidence 明确提供时才使用。
        # 不根据经验自行猜测司法解释名称。
        # ==================================================

        law_name = _get_law_name(item)

        # ==================================================
        # 条文
        # ==================================================

        article = _first_non_empty(

            item.get(
                "article"
            ),

            metadata.get(
                "citation_article"
            ),

            metadata.get(
                "article_list"
            ),

        )

        # ==================================================
        # 页码
        # ==================================================

        page = _first_non_empty(

            item.get(
                "page"
            ),

            metadata.get(
                "page_number"
            ),

        )

        # ==================================================
        # authority
        # ==================================================

        authority = _first_non_empty(

            item.get(
                "authority"
            ),

            metadata.get(
                "source_priority"
            ),

            0,

        )

        # ==================================================
        # category / role：共享提取（core/common.py P1-5）
        # ==================================================

        category = _get_category(item)

        role = _get_role(item)

        # ==================================================
        # 原始证据内容
        # ==================================================

        content = _first_non_empty(

            item.get(
                "content"
            ),

            item.get(
                "document"
            ),

        )

        citation = {

            # =========================
            # 基础身份
            # =========================

            "evidence_id":
                item.get(
                    "evidence_id",
                    ""
                ),

            "citation_id":
                "C" + str(
                    len(citations) + 1
                ),

            # =========================
            # 标题 / 来源
            # =========================

            "title":
                _first_non_empty(
                    item.get("title"),
                    metadata.get("document_title"),
                    metadata.get("filename"),
                ),

            "source_document":
                source_document,

            # =========================
            # 法律分类
            # =========================

            "category":
                category,

            "citation_level":
                category,

            "role":
                role,

            "citation_type":
                citation_type,

            # =========================
            # 法律名称
            # =========================

            "law_name":
                law_name,

            # =========================
            # 条文
            # =========================

            "article":
                article,

            # =========================
            # 页码
            # =========================

            "page":
                page,

            # =========================
            # 权威等级
            # =========================

            "authority":
                authority,

            # =========================
            # 原始证据内容
            # =========================

            "content":
                content,

        }

        citations.append(
            citation
        )

    return citations
