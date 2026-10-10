# core/common.py
"""
跨模块共享 utility（唯一真相源）。

解决问题：
    evidence_fusion.py / citation_builder.py / citation_formatter.py /
    retrieval_adapter.py 等 4 个模块都在定义同名工具函数。
    本次抽离后所有模块统一 import，避免漂移与回归。

迁移策略（thin-wrapper 优先，详见 legacy/ 清理经验）：
    各文件旧私有名（_safe_float/_safe_string/_first_non_empty/_get_metadata/
    _get_identity/_normalize_identity）不删除，统一替换为：
        from core.common import <name> as <old_name>
    这样 external / legacy import 调用旧私有名也正常返回。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


# ============================================================
# Safe Float
# ============================================================

def safe_float(value: Any, default: float = 0.0) -> float:
    """安全转换为 float；bool 被视为非数字（避免 True → 1.0 这类语义错误）。"""

    if value is None:
        return default

    if isinstance(value, bool):
        return default

    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ============================================================
# Safe String
# ============================================================

def safe_string(value: Any, default: str = "") -> str:
    """安全转字符串并 strip；None 返回 default。"""

    if value is None:
        return default

    if isinstance(value, str):
        return value.strip()

    try:
        return str(value).strip()
    except Exception:
        return default


# ============================================================
# First Non Empty
# ============================================================

def first_non_empty(*values: Any) -> Any:
    """
    返回第一个非空值。

    判定为空的情况：
        - None
        - 字符串：去空白后为空
    其他真值（包括 0 / False / [] 作为占位的场景）——
    这些都不是「字段缺失」，因此保留返回调用方自行处理。
    """

    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                continue
        return value

    return ""


# ============================================================
# Get Metadata
# ============================================================

def get_metadata(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    安全读取 evidence item 的 metadata 子字典。

    保证：
        - 返回值永远是 dict
        - 绝不修改 item
    """

    metadata = item.get("metadata", {})
    if not isinstance(metadata, dict):
        return {}
    return metadata


# ============================================================
# Get Explicit Identity
# ============================================================

def get_identity(item: Dict[str, Any]) -> str:
    """
    读取「明确声明的法律身份」。

    优先级（与证据体系 V6.3 核心原则一致）：
        law_identity → source_category → law_level → law_part

    只读取已存在的字段，不根据标题做任何猜测。
    """

    metadata = get_metadata(item)
    return first_non_empty(
        item.get("law_identity"),
        metadata.get("law_identity"),
        item.get("source_category"),
        metadata.get("source_category"),
        item.get("law_level"),
        metadata.get("law_level"),
        item.get("law_part"),
        metadata.get("law_part"),
    )


# ============================================================
# Normalize Identity
# ============================================================

def normalize_identity(identity: Any) -> str:
    """
    把 中文/英文 法律身份名统一为 5 类内部 token：

        statute           正式法律 / 法律规定
        judicial_rule     司法解释
        court_guidance    最高院观点
        professional_view 学理观点
        professional_reference 专业参考（不得升级为正式法律）
        ""                未知
    """

    identity = safe_string(identity)
    if not identity:
        return ""

    # 正式法律
    if identity in {"正式法律", "法律规定", "法律", "statute", "law"}:
        return "statute"

    # 司法解释
    if identity in {
        "司法解释",
        "司法",
        "judicial",
        "judicial_rule",
        "judicial_interpretation",
    }:
        return "judicial_rule"

    # 最高院观点
    if identity in {
        "最高院观点",
        "最高人民法院观点",
        "court_guidance",
        "court_opinion",
        "supreme_court",
    }:
        return "court_guidance"

    # 学理观点
    if identity in {
        "学理观点",
        "academic",
        "professional_view",
        "commentary",
    }:
        return "professional_view"

    # 专业参考（注意：绝不能等同于 statute）
    if identity in {
        "专业参考",
        "专业资料",
        "专业书籍",
        "参考书",
        "参考资料",
    }:
        return "professional_reference"

    return ""


# ============================================================
# Role 别名词典化（fusion.normalize_role 步骤 3 用）
# ============================================================

ROLE_TOKEN_MAP = {
    "statute": "statute",
    "law": "statute",
    "法律": "statute",
    "法律规定": "statute",
    "judicial": "judicial_rule",
    "judicial_rule": "judicial_rule",
    "judicial_interpretation": "judicial_rule",
    "司法解释": "judicial_rule",
    "court_guidance": "court_guidance",
    "court_opinion": "court_guidance",
    "supreme_court": "court_guidance",
    "最高院观点": "court_guidance",
    "最高人民法院观点": "court_guidance",
    "professional_view": "professional_view",
    "academic": "professional_view",
    "commentary": "professional_view",
    "学理观点": "professional_view",
}


# ============================================================
# 共享字段提取（builder / formatter / fusion 三处级联去重）
# ============================================================

def get_law_name(item: Any) -> Any:
    """law_name 4 字段优先级：item.law_name > metadata.law_name
    > item.citation_name > metadata.citation_name。"""
    if not isinstance(item, dict):
        return None
    metadata = get_metadata(item)
    return first_non_empty(
        item.get("law_name"),
        metadata.get("law_name"),
        item.get("citation_name"),
        metadata.get("citation_name"),
    )


def wrap_law_name(name: Any) -> Any:
    """给法律/司法解释名称补书名号，统一为《法名》第N条引用格式。

    仅对"干净的法规名"包裹；以下情况保持原样：
    - 空值 / 已含《》（避免《《》或双重处理）；
    - 关联资料占位名（"关联资料N"）；
    - 含冒号的附件式来源（"附件：4．……"）；
    - 带文档扩展名的文件名（xxx.docx / xxx.pdf 等）；
    - 超长名称（>30 字符，大概率是文件标题而非法规名）。
    """
    if not name:
        return name
    name = str(name).strip()
    if (
        not name
        or "《" in name
        or name.startswith("关联资料")
        or "：" in name
        or ":" in name
        or len(name) > 30
    ):
        return name
    lowered = name.lower()
    doc_exts = (".docx", ".doc", ".pdf", ".xlsx", ".pptx", ".txt", ".md")
    if any(lowered.endswith(ext) for ext in doc_exts):
        return name
    return f"《{name}》"


def get_category(item: Any) -> Any:
    """category/citation_level 5 字段优先级：item.category > item.citation_level
    > metadata.source_category > metadata.law_level > metadata.law_identity。"""
    if not isinstance(item, dict):
        return None
    metadata = get_metadata(item)
    return first_non_empty(
        item.get("category"),
        item.get("citation_level"),
        metadata.get("source_category"),
        metadata.get("law_level"),
        metadata.get("law_identity"),
    )


def get_role(item: Any) -> Any:
    """role 4 字段优先级：item.role > item.evidence_role
    > metadata.role > metadata.evidence_role。"""
    if not isinstance(item, dict):
        return None
    metadata = get_metadata(item)
    return first_non_empty(
        item.get("role"),
        item.get("evidence_role"),
        metadata.get("role"),
        metadata.get("evidence_role"),
    )


# ============================================================
# 向后兼容：旧私有名别名（旧调用方不需要改 import）
# ============================================================
# 注意：所有别名应在各自文件内部做 import ... as _xxx 映射，
# 而非从这里暴露以 _ 开头的符号（_ 开头表示模块私有，PEP 8 不建议 import）。

# 仅为 legacy/* 里极少数「from ... import _safe_float」的历史遗留测试
# 提供兜底：这里仍不导出 _ 前缀名（保持风格一致），
# legacy 文件里用 `from core.common import safe_float as _safe_float` 的写法即可。

__all__ = [
    "safe_float",
    "safe_string",
    "first_non_empty",
    "get_metadata",
    "get_identity",
    "normalize_identity",
    "ROLE_TOKEN_MAP",
    "get_law_name",
    "get_category",
    "get_role",
]
