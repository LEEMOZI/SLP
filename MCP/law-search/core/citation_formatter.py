# core/citation_formatter.py
"""
律师友好的引用格式化器（formatter）。

共享 utility 统一来自 core/common.py，thin wrapper 保留
 _first_non_empty 私有名以防历史调用。
"""

from core.common import (
    first_non_empty as _first_non_empty,
    get_law_name as _get_law_name,
    get_category as _get_category,
    wrap_law_name as _wrap_law_name,
)


def format_lawyer_citations(citations):
    """
    Citation Formatter

    Evidence Citation
        ↓
    Lawyer Citation Format

    保留检索阶段全部法律身份信息。

    citation_type：

        law
        judicial
        court_guidance
        academic
        unknown
    """

    formatted = []

    for index, c in enumerate(citations):

        if not isinstance(c, dict):
            continue

        metadata = c.get(
            "metadata",
            {}
        )

        if not isinstance(metadata, dict):
            metadata = {}

        # ==================================================
        # 基础字段
        # ==================================================

        citation_id = _first_non_empty(
            c.get("citation_id"),
            f"C{index + 1}"
        )

        evidence_id = _first_non_empty(
            c.get("evidence_id")
        )

        # ==================================================
        # citation_type
        #
        # Builder 已经完成标准化。
        # Formatter 这里只做最后兜底。
        # ==================================================

        citation_type = _first_non_empty(

            c.get(
                "citation_type"
            ),

            metadata.get(
                "citation_type"
            ),

            "unknown"

        )

        citation_type = str(
            citation_type
        ).strip().lower()

        # ==================================================
        # 分类 / 法律名称：共享提取（core/common.py P1-5）
        # ==================================================

        citation_level = _get_category(c)

        law_name = _get_law_name(c)

        # ==================================================
        # 条文
        # ==================================================

        article = _first_non_empty(

            c.get(
                "article"
            ),

            c.get(
                "citation_article"
            ),

            metadata.get(
                "citation_article"
            ),

            metadata.get(
                "article_list"
            ),

        )

        # ==================================================
        # 来源
        # ==================================================

        source = _first_non_empty(

            c.get(
                "source"
            ),

            c.get(
                "source_document"
            ),

            metadata.get(
                "source_document"
            ),

            c.get(
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
        # 页码
        # ==================================================

        page = _first_non_empty(

            c.get(
                "page"
            ),

            c.get(
                "page_number"
            ),

            metadata.get(
                "page_number"
            ),

        )

        # ==================================================
        # 权威等级
        # ==================================================

        authority = _first_non_empty(

            c.get(
                "authority"
            ),

            metadata.get(
                "source_priority"
            ),

            0

        )

        # ==================================================
        # 生成引用文本
        # ==================================================

        citation_text = build_citation_text(

            {
                **c,

                "citation_type":
                    citation_type,

                "law_name":
                    law_name,

                "article":
                    article,

                "source":
                    source,

                "page":
                    page,

            }

        )

        # ==================================================
        # 最终律师引用
        # ==================================================

        item = {

            "citation_id":
                citation_id,

            "evidence_id":
                evidence_id,

            "citation_level":
                citation_level,

            "citation_type":
                citation_type,

            "law_name":
                law_name,

            "article":
                article,

            "source":
                source,

            "page":
                page,

            "authority":
                authority,

            "citation_text":
                citation_text,

        }

        formatted.append(
            item
        )

    return formatted


def build_citation_text(c):

    citation_type = _first_non_empty(

        c.get(
            "citation_type"
        ),

        "unknown"

    )

    citation_type = str(
        citation_type
    ).strip().lower()

    law_name = _first_non_empty(

        c.get(
            "law_name"
        ),

        c.get(
            "citation_name"
        ),

    )

    article = _first_non_empty(

        c.get(
            "article"
        ),

        c.get(
            "citation_article"
        ),

    )

    source = _first_non_empty(

        c.get(
            "source"
        ),

        c.get(
            "source_document"
        ),

    )

    page = _first_non_empty(

        c.get(
            "page"
        ),

        c.get(
            "page_number"
        ),

    )

    # ======================================================
    # 1. 正式法律
    # ======================================================

    if citation_type == "law":

        # 正文引用统一格式：《法名》第N条（书名号 + 规范条文号）
        if (
            article
            and article not in ("—", "-", "–")
            and not (str(article).startswith("第") and "条" in str(article)[:12])
        ):
            article = f"第{article}条"

        main_text = (
            f"{_wrap_law_name(law_name) or ''}"
            f"{article or ''}"
        )

        # 如果法律名称和条文均为空，
        # 避免生成一个完全空的主体。

        if not main_text:

            main_text = source

        if source and page:

            return (
                f"{main_text}"
                f"（来源：{source}"
                f"，第{page}页）"
            )

        if source:

            return (
                f"{main_text}"
                f"（来源：{source}）"
            )

        if page:

            return (
                f"{main_text}"
                f"（第{page}页）"
            )

        return main_text

    # ======================================================
    # 2. 司法解释
    #
    # 当前 Evidence 中通常没有具体司法解释名称，
    # 因此使用 source + article + page。
    # ======================================================

    if citation_type == "judicial":

        main_text = (
            f"{source}"
            f"{article}"
        )

        if not main_text:

            main_text = law_name

        if source and article:

            main_text = (
                f"{source}"
                f"{article}"
            )

        if source and article and page:

            return (
                f"{source}{article}"
                f"（第{page}页）"
            )

        if source and page:

            return (
                f"{source}"
                f"（第{page}页）"
            )

        if source:

            return source

        if article and page:

            return (
                f"{article}"
                f"（第{page}页）"
            )

        return (
            main_text
        )

    # ======================================================
    # 3. 最高院观点
    # ======================================================

    if citation_type == "court_guidance":

        if source and article and page:

            return (
                f"{source}{article}"
                f"（第{page}页）"
            )

        if source and page:

            return (
                f"{source}"
                f"（第{page}页）"
            )

        if source and article:

            return (
                f"{source}{article}"
            )

        if source:

            return source

        return (
            f"{article}"
            f"（第{page}页）"
            if article and page
            else article
        )

    # ======================================================
    # 4. 学理观点
    # ======================================================

    if citation_type == "academic":

        if source and page:

            return (
                f"{source}"
                f"（第{page}页）"
            )

        if source:

            return source

        if page:

            return (
                f"（第{page}页）"
            )

        return ""

    # ======================================================
    # 5. 未知类型
    #
    # 尽量保留已有信息。
    # ======================================================

    if source and article and page:

        return (
            f"{source}{article}"
            f"（第{page}页）"
        )

    if source and page:

        return (
            f"{source}"
            f"（第{page}页）"
        )

    if source and article:

        return (
            f"{source}{article}"
        )

    if source:

        return source

    if article and page:

        return (
            f"{article}"
            f"（第{page}页）"
        )

    return ""
