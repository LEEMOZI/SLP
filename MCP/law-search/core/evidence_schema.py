"""
Evidence Schema V6.2.1

统一法律 Agent 证据数据结构

Pipeline:

Chroma
↓
retrieval_adapter
↓
normalize_evidence()
↓
Evidence Pack
↓
Citation
↓
Reasoning

核心原则：

1. 字段语义彼此独立
2. normalize 不负责猜测法律身份
3. 不允许不同字段互相回填
4. 数值字段统一为 float
5. citation_type 使用统一枚举
6. professional_reference 作为独立 citation_type
"""

# ============================================================
# citation_type 统一枚举
# ============================================================

VALID_CITATION_TYPES = {
    "law",
    "judicial",
    "court_guidance",
    "academic",
    "professional_reference",
    "court_case",
    "unknown",
}


CITATION_TYPE_MAP = {

    # --------------------------------------------------------
    # 法律
    # --------------------------------------------------------

    "law":
        "law",

    "statute":
        "law",

    "法律":
        "law",

    "正式法律":
        "law",

    "法律规定":
        "law",


    # --------------------------------------------------------
    # 司法解释
    # --------------------------------------------------------

    "judicial":
        "judicial",

    "judicial_rule":
        "judicial",

    "judicial_interpretation":
        "judicial",

    "司法解释":
        "judicial",


    # --------------------------------------------------------
    # 最高院观点 / 指导
    # --------------------------------------------------------

    "court_guidance":
        "court_guidance",

    "court_opinion":
        "court_guidance",

    "supreme_court":
        "court_guidance",

    "最高院观点":
        "court_guidance",

    "最高人民法院观点":
        "court_guidance",


    # --------------------------------------------------------
    # 学理观点
    # --------------------------------------------------------

    "academic":
        "academic",

    "professional":
        "academic",

    "professional_view":
        "academic",

    "commentary":
        "academic",

    "学理观点":
        "academic",


    # --------------------------------------------------------
    # 专业参考
    # --------------------------------------------------------

    "professional_reference":
        "professional_reference",

    "专业参考":
        "professional_reference",

    "专业参考资料":
        "professional_reference",


    # --------------------------------------------------------
    # 司法案例
    # --------------------------------------------------------

    "case":
        "court_case",

    "judicial_case":
        "court_case",

    "court_case":
        "court_case",

    "司法案例":
        "court_case",
}


# ============================================================
# Evidence Role
# ============================================================

ROLE_MAP = {

    # --------------------------------------------------------
    # 法律
    # --------------------------------------------------------

    "statute":
        "法律规定",

    "law":
        "法律规定",


    # --------------------------------------------------------
    # 司法解释
    # --------------------------------------------------------

    "judicial_rule":
        "司法解释",

    "judicial_interpretation":
        "司法解释",

    "judicial":
        "司法解释",


    # --------------------------------------------------------
    # 案例
    # --------------------------------------------------------

    "case":
        "司法案例",

    "judicial_case":
        "司法案例",

    "court_case":
        "司法案例",


    # --------------------------------------------------------
    # 最高院观点
    # --------------------------------------------------------

    "court_opinion":
        "最高院观点",

    "supreme_court":
        "最高院观点",

    "court_guidance":
        "最高院观点",


    # --------------------------------------------------------
    # 学理观点
    # --------------------------------------------------------

    "academic":
        "学理观点",

    "commentary":
        "学理观点",

    "professional_view":
        "学理观点",


    # --------------------------------------------------------
    # 专业参考
    # --------------------------------------------------------

    "professional_reference":
        "专业参考",
}


# ============================================================
# Shared utility（统一真相源：core/common.py）
#
# · safe_float / safe_string 保持 evidence_schema.safe_* 公开签名，
#   兼容 legacy / 原调用方；
# · first_non_empty 使用别名 _first_non_empty，与 infer_citation_type
#   中引用 citation_builder 原有私有实现时的命名保持一致。
# ============================================================

from core.common import (
    safe_float,
    safe_string,
    first_non_empty as _first_non_empty,
)


# ============================================================
# Normalize citation type（单项枚举映射）
# ============================================================

def normalize_citation_type(value):
    """
    将历史 citation_type 枚举统一为标准枚举。

    标准值：
        law / judicial / court_guidance / academic /
        professional_reference / court_case / unknown
    """

    if not value:
        return "unknown"

    value = safe_string(value).strip()
    if not value:
        return "unknown"

    return CITATION_TYPE_MAP.get(value, "unknown")


# ============================================================
# Infer citation type（从 10 字段身份信息推断类型）
#
# 迁移来源：core/citation_builder._normalize_citation_type（P0-2 · 统一真相源）
# 调用方：
#   · evidence_schema.normalize_evidence       （作为主要 fallback）
#   · core/citation_builder._normalize_citation_type  （thin wrapper 转发，兼容）
#   · core/evidence_fusion                     （若直接调用则走同一逻辑）
# ============================================================

def infer_citation_type(item, metadata):
    """
    依据明确的法律身份字段推断 citation_type。

    优先级（专业参考永远不能被 citation_type=law 升级为 statute）:
        1. 专业参考（law_identity="专业参考"）
        2. 最高院观点
        3. 司法解释
        4. 学理观点
        5. 正式法律
        6. role / category / source_type / law_identity / law_level
        7. 已显式声明的 citation_type 枚举映射
        8. "unknown" 兜底
    """

    item = item or {}
    metadata = metadata if isinstance(metadata, dict) else {}

    # --------------------------------------------------
    # 1. 读取 10 字段身份（顶层 > metadata 子字典）
    # --------------------------------------------------
    role = _first_non_empty(
        item.get("role"),
        item.get("evidence_role"),
        metadata.get("role"),
        metadata.get("evidence_role"),
    )

    category = _first_non_empty(
        item.get("category"),
        item.get("citation_level"),
        metadata.get("source_category"),
        metadata.get("law_part"),
        metadata.get("law_level"),
    )

    source_type = _first_non_empty(
        item.get("source_type"),
        metadata.get("source_type"),
    )

    law_identity = _first_non_empty(
        item.get("law_identity"),
        metadata.get("law_identity"),
    )

    # --------------------------------------------------
    # 2. 专业参考（优先级最高：避免被错判为 law）
    # --------------------------------------------------
    professional_values = {
        "professional_reference",
        "专业参考",
        "专业参考资料",
    }
    if (
        str(role).strip().lower() in professional_values
        or str(category).strip() in professional_values
        or str(source_type).strip() in professional_values
        or str(law_identity).strip() in professional_values
    ):
        return "professional_reference"

    # --------------------------------------------------
    # 3. 最高院观点
    # --------------------------------------------------
    court_guidance_values = {
        "court_guidance",
        "最高院观点",
        "最高人民法院观点",
    }
    court_guidance_cn = {"最高院观点", "最高人民法院观点"}
    if (
        str(role).strip().lower() in court_guidance_values
        or str(category).strip() in court_guidance_cn
        or str(source_type).strip() in court_guidance_cn
        or str(law_identity).strip() in court_guidance_cn
    ):
        return "court_guidance"

    # --------------------------------------------------
    # 4. 司法解释
    # --------------------------------------------------
    judicial_values = {
        "judicial_rule",
        "judicial",
        "司法解释",
    }
    if (
        str(role).strip().lower() in judicial_values
        or str(category).strip() in judicial_values
        or str(source_type).strip() in judicial_values
        or str(law_identity).strip() in judicial_values
    ):
        return "judicial"

    # --------------------------------------------------
    # 5. 学理观点
    # --------------------------------------------------
    academic_values = {
        "academic",
        "professional_view",
        "学理观点",
    }
    if (
        str(role).strip().lower() in academic_values
        or str(category).strip() in academic_values
        or str(source_type).strip() in academic_values
        or str(law_identity).strip() in academic_values
    ):
        return "academic"

    # --------------------------------------------------
    # 6. 正式法律（law_identity="正式法律"/"法律规定" 等）
    # --------------------------------------------------
    statute_values = {
        "statute",
        "正式法律",
        "法律规定",
        "法律",
    }
    if (
        str(role).strip().lower() in {"statute", "law"}
        or str(category).strip() in statute_values
        or str(source_type).strip() in statute_values
        or str(law_identity).strip() in statute_values
    ):
        return "law"

    # --------------------------------------------------
    # 7. 显式声明的 citation_type（做一次枚举归一化）
    # --------------------------------------------------
    explicit_type = _first_non_empty(
        item.get("citation_type"),
        metadata.get("citation_type"),
    )
    normalized = normalize_citation_type(explicit_type)
    if normalized != "unknown":
        return normalized

    # --------------------------------------------------
    # 8. 兜底：unknown
    # --------------------------------------------------
    return "unknown"


# ============================================================
# Normalize evidence role
# ============================================================

def normalize_role(role):
    """
    统一 evidence_role。

    注意：

    本函数只做角色枚举映射，
    不根据标题、法律名称、正文内容猜测角色。
    """

    if not role:
        return ""

    role = safe_string(
        role
    ).strip()

    return ROLE_MAP.get(
        role,
        role
    )


# ============================================================
# Role → citation_type 映射（fusion 显式 Role 覆盖路径用）
# ============================================================

CITATION_TYPE_FROM_ROLE = {
    "statute": "law",
    "judicial_rule": "judicial",
    "court_guidance": "court_guidance",
    "professional_reference": "professional_reference",
    "professional_view": "academic",
    "academic": "academic",
}


def citation_type_from_role(role):
    """把规范化 role 映射为 citation_type；无映射返回 ""（调用方保留原值）。

    与 infer_citation_type 的区别：本函数只做「显式 Role 决定 citation_type」
    这一步字典查找，不做 10 字段优先级推断。
    """
    return CITATION_TYPE_FROM_ROLE.get(
        safe_string(role).strip().lower(),
        "",
    )


# ============================================================
# Normalize Evidence
# ============================================================

def normalize_evidence(item):
    """
    将不同来源的 evidence
    转换为统一格式。

    本函数只负责：

    1. 字段标准化
    2. 类型标准化
    3. 历史枚举兼容
    4. 默认值处理

    本函数不负责：

    1. 根据正文猜法律名称
    2. 根据标题猜司法解释
    3. source_category → source_type
    4. citation_name → law_name
    5. source_priority → authority
    """

    item = item.copy()

    metadata = (
        item.get(
            "metadata",
            {}
        )
        or {}
    )

    if not isinstance(metadata, dict):
        metadata = {}


    # ========================================================
    # Content
    # ========================================================

    if "content" not in item:

        item["content"] = item.get(
            "document",
            ""
        )


    # ========================================================
    # 文献基础字段
    # ========================================================

    item["title"] = safe_string(
        item.get(
            "title",
            metadata.get(
                "title",
                ""
            )
        )
    )


    item["law_name"] = safe_string(
        item.get(
            "law_name",
            metadata.get(
                "law_name",
                ""
            )
        )
    )


    item["citation_name"] = safe_string(
        item.get(
            "citation_name",
            metadata.get(
                "citation_name",
                ""
            )
        )
    )


    # ========================================================
    # 法律身份
    # ========================================================

    item["law_identity"] = safe_string(
        item.get(
            "law_identity",
            metadata.get(
                "law_identity",
                ""
            )
        )
    )


    item["law_level"] = safe_string(
        item.get(
            "law_level",
            metadata.get(
                "law_level",
                ""
            )
        )
    )


    # ========================================================
    # 来源属性
    # ========================================================

    item["source_category"] = safe_string(
        item.get(
            "source_category",
            metadata.get(
                "source_category",
                ""
            )
        )
    )


    item["source_type"] = safe_string(
        item.get(
            "source_type",
            metadata.get(
                "source_type",
                ""
            )
        )
    )


    item["source_priority"] = safe_float(
        item.get(
            "source_priority",
            metadata.get(
                "source_priority"
            )
        ),
        0.0
    )


    # ========================================================
    # Evidence 分类
    # ========================================================

    item["category"] = safe_string(
        item.get(
            "category",
            metadata.get(
                "category",
                ""
            )
        )
    )


    raw_role = (
        item.get(
            "evidence_role"
        )
        or metadata.get(
            "evidence_role",
            ""
        )
    )


    item["evidence_role"] = normalize_role(
        raw_role
    )


    # ========================================================
    # Citation
    # ========================================================

    # ========================================================
    # Citation type（基于身份字段推断 + 显式声明兜底）
    #
    # 说明：
    #   · 旧实现仅在 item["citation_type"] / metadata["citation_type"]
    #     有值时做映射，metadata.citation_type 填充率 ~65% → 约 35% 的
    #     evidence 会落到 unknown。
    #   · 现调用 infer_citation_type：按 10 字段身份优先级推断，
    #     law_identity/source_category（填充率 100%）作为最强信号，
    #     最后再用显式声明的 citation_type 做枚举归一化 — 因此现在
    #     非 unknown 的比例会明显提升，更利于引用构建。
    # ========================================================

    item["citation_type"] = infer_citation_type(
        item,
        metadata,
    )


    item["citation_article"] = safe_string(
        item.get(
            "citation_article",
            metadata.get(
                "citation_article",
                ""
            )
        )
    )


    item["article_list"] = safe_string(
        item.get(
            "article_list",
            metadata.get(
                "article_list",
                ""
            )
        )
    )


    # ========================================================
    # 兼容旧 article 字段
    # ========================================================

    item["article"] = safe_string(
        item.get(
            "article",
            ""
        )
    )


    # ========================================================
    # Page
    # ========================================================

    item["page"] = safe_string(
        item.get(
            "page",
            metadata.get(
                "page",
                metadata.get(
                    "page_number",
                    ""
                )
            )
        )
    )


    # ========================================================
    # Authority
    # ========================================================

    item["authority"] = safe_float(
        item.get(
            "authority",
            metadata.get(
                "authority"
            )
        ),
        0.0
    )


    # ========================================================
    # Scores
    # ========================================================

    item["rerank_score"] = safe_float(
        item.get(
            "rerank_score",
            0
        ),
        0.0
    )


    item["evidence_score"] = safe_float(
        item.get(
            "evidence_score",
            0
        ),
        0.0
    )


    item["distance"] = safe_float(
        item.get(
            "distance",
            0
        ),
        0.0
    )


    return item


# ============================================================
# Stable Evidence ID
# ============================================================

def generate_evidence_id(item):
    """
    生成稳定 Evidence ID。

    示例：

        790a0f09_414
    """

    metadata = (
        item.get(
            "metadata",
            {}
        )
        or {}
    )

    if not isinstance(metadata, dict):
        metadata = {}


    file_hash = safe_string(
        metadata.get(
            "file_hash",
            ""
        )
    )


    chunk_index = metadata.get(
        "chunk_index",
        ""
    )


    if file_hash and chunk_index != "":

        return (
            f"{file_hash[:8]}"
            f"_"
            f"{chunk_index}"
        )


    return "unknown"


# ============================================================
# Evidence Summary
# ============================================================

def evidence_summary(item):
    """
    用于 debug 输出。
    """

    return {

        "id":
            item.get(
                "evidence_id",
                ""
            ),

        "title":
            item.get(
                "title",
                ""
            ),

        "law_name":
            item.get(
                "law_name",
                ""
            ),

        "law_identity":
            item.get(
                "law_identity",
                ""
            ),

        "law_level":
            item.get(
                "law_level",
                ""
            ),

        "source_category":
            item.get(
                "source_category",
                ""
            ),

        "source_type":
            item.get(
                "source_type",
                ""
            ),

        "citation_type":
            item.get(
                "citation_type",
                ""
            ),

        "article":
            item.get(
                "article",
                ""
            ),

        "citation_article":
            item.get(
                "citation_article",
                ""
            ),

        "article_list":
            item.get(
                "article_list",
                ""
            ),

        "role":
            item.get(
                "role",
                item.get(
                    "evidence_role",
                    ""
                )
            ),

        "category":
            item.get(
                "category",
                ""
            ),

        "authority":
            item.get(
                "authority",
                0.0
            ),

        "rerank_score":
            item.get(
                "rerank_score",
                0.0
            ),

        "evidence_score":
            item.get(
                "evidence_score",
                0.0
            ),

        "distance":
            item.get(
                "distance",
                0.0
            ),
    }
