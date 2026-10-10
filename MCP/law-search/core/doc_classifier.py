# core/doc_classifier.py
"""
文档分类器 — doc_type / authority / status / version / domain 推断
（纯 CPU 正则规则，零模型、零网络）

双消费方（规则唯一真相源，只写一份）：
    1. ingest.py     — 入库时写入 chunk 元数据
                       （doc_type / authority / effective_status /
                        effective_date / version / domain）
    2. evidence_fusion.py — 检索时对「旧库 chunk（无新元数据）」
                       按文件名实时推断，用于权威度回填 + 时效性加权。

设计要点：
    - 按文件名 + 路径 + 库名推断，不依赖模型；
    - classify_document 带 memo（同一文件只分类一次）；
    - 一切输出为 Chroma 可存储的标量（str / int）。
"""

from __future__ import annotations

import re
from typing import Any, Dict

# ============================================================
# 权威度基准（authority：0-50，与既有 source_priority 量纲一致）
# ============================================================

DOC_TYPE_AUTHORITY = {
    "law": 50,                    # 法律（公司法 / 民法典 …）
    "judicial_interpretation": 46,  # 司法解释
    "regulation": 42,             # 行政法规 / 条例
    "departmental_rule": 38,      # 部门规章 / 监管规则 / 交易所指引
    "case": 30,                   # 案例 / 裁判文书
    "commentary": 26,             # 评注 / 理解与适用 / 释义
    "contract_template": 20,      # 合同模板 / 范本
    "practice_article": 15,       # 实务文章 / 公众号整理
    "unknown": 0,
}

# ============================================================
# 时效性乘数（作用于 fusion 的 authority_score）
# ============================================================

STATUS_MULTIPLIER = {
    "current": 1.0,
    "repealed": 0.3,   # 已废止 / 已失效 → 强降权
    "unknown": 1.0,
}

# ============================================================
# 领域关键词（按顺序首中即停）
# ============================================================

_DOMAIN_RULES = [
    (("公司", "股东", "清算", "破产", "出资"), "公司"),
    (("合同", "买卖", "租赁", "借款"), "合同"),
    (("劳动", "工伤", "用工", "劳动合同"), "劳动"),
    (("婚姻", "继承", "家庭", "离婚"), "婚姻家庭"),
    (("刑法", "犯罪", "刑事"), "刑事"),
    (("证券", "上市", "债券", "基金", "资管", "期货", "发行"), "资本市场"),
    (("银行", "信贷"), "银行"),
    (("保险",), "保险"),
    (("税", "纳税"), "税务"),
    (("知识产权", "专利", "商标", "著作权"), "知识产权"),
    (("个人信息", "数据", "网络安全"), "数据"),
    (("仲裁",), "仲裁"),
]

# 文件名扩展名（切掉后缀再匹配，避免 .docx 干扰）
_DOC_EXTS = ".docx.doc.pdf.xlsx.pptx.txt.md.wps"


def _strip_ext(name: str) -> str:
    # 按最后一个点切（仅当末尾是 ≤4 位字母后缀时视为扩展名）
    if "." in name:
        head, _, tail = name.rpartition(".")
        if tail and len(tail) <= 4 and tail.isalpha():
            return head
    return name


# ============================================================
# doc_type 判定（顺序敏感：特定 → 一般）
# ============================================================

def _detect_doc_type(filename: str, path: str) -> str:
    fname = _strip_ext(filename)
    # 1. 合同模板（"模板/范本/协议" 强信号）
    if re.search(r"模板|范本|协议|合同书", fname):
        return "contract_template"
    # 2. 案例
    if re.search(r"案例|裁判|判决|裁定|判例", fname):
        return "case"
    # 3. 学理评注
    if re.search(r"评注|理解与适用|释义|600问|评析|条文说明", fname):
        return "commentary"
    # 4. 实务文章（路径级信号：公众号整理）
    if "公众号" in path or "律动" in path or "新规速览" in path:
        return "practice_article"
    if re.search(r"实务|培训|讲义|问答", fname):
        return "practice_article"
    # 5. 司法解释
    if re.search(r"法释|司法解释", fname) or re.search(
        r"最高人民法院关于.{2,30}的?(规定|解释|答复)", fname
    ):
        return "judicial_interpretation"
    # 6. 行政法规 / 条例
    if re.search(r"条例|实施细则|管理规定", fname):
        return "regulation"
    # 7. 部门规章 / 监管规则
    if re.search(r"管理办法|办法|指引|规则|通知|公告|目录|决定", fname):
        return "departmental_rule"
    # 8. 法律（X法 / 法典）
    if re.search(r"[\u4e00-\u9fa5]{1,8}法$", fname) or re.search(
        r"[\u4e00-\u9fa5]{1,8}法（", fname
    ):
        return "law"
    if re.search(r"法典|中华人民共和国", fname):
        return "law"
    return "unknown"


# ============================================================
# status / version / effective_date 判定
# ============================================================

_VERSION_PATTERNS = [
    r"（(\d{4})年?修订）",
    r"（(\d{4})年?修正）",
    r"[（(](\d{4})[版)][）)]",
    r"(\d{4})版",
    r"(\d{4})年修订",
    r"(\d{4})年修正",
]


def _detect_status_version(filename: str) -> Dict[str, str]:
    fname = _strip_ext(filename)

    if re.search(r"废止|失效", fname):
        return {"status": "repealed", "version": "", "effective_date": ""}

    for pat in _VERSION_PATTERNS:
        m = re.search(pat, fname)
        if m:
            year = m.group(1)
            kind = "修订" if ("修订" in m.group(0) or "修订" in fname) else "修正"
            return {
                "status": "current",
                "version": f"{year}{kind}",
                "effective_date": year,
            }

    return {"status": "current", "version": "", "effective_date": ""}


def _detect_domain(filename: str, path: str) -> str:
    text = _strip_ext(filename) + path
    for keywords, domain in _DOMAIN_RULES:
        if any(k in text for k in keywords):
            return domain
    return ""


# ============================================================
# 对外主入口
# ============================================================

_MEMO: Dict[tuple, Dict[str, Any]] = {}


def classify_document(
    filename: str,
    path: str = "",
    library_name: str = "",
) -> Dict[str, Any]:
    """
    按文件名 / 路径 / 库名推断文档画像。

    Returns:
        {
            "doc_type":       str（DOC_TYPE_AUTHORITY 的键）
            "authority":      int（0-50；unknown → 0，由调用方决定是否回填）
            "status":         "current" | "repealed"
            "effective_date": "2023"（尽力推断，可为空）
            "version":        "2023修订"（可为空）
            "domain":         "公司" / "合同" / …（可为空）
        }
    """
    key = (str(filename), str(path), str(library_name))
    if key in _MEMO:
        return _MEMO[key]

    # 库名直接给出强先验：案例库 → case，合同模板库 → contract_template
    if library_name == "case_library":
        doc_type = "case"
    elif library_name == "contract_library":
        doc_type = "contract_template"
    else:
        doc_type = _detect_doc_type(str(filename), str(path))

    result = {
        "doc_type": doc_type,
        "authority": DOC_TYPE_AUTHORITY.get(doc_type, 0),
        **_detect_status_version(str(filename)),
        "domain": _detect_domain(str(filename), str(path)),
    }

    _MEMO[key] = result
    return result


def enrich_evidence_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    检索侧增强：为单个 evidence item 补全文档画像。

    优先用 chunk 元数据（新库已有），缺失时按文件名实时推断
    （兼容 37.7 万条旧库 chunk，无需重建索引）。

    Returns:
        供 calculate_score / pack 使用的字段字典：
        {doc_type, doc_status, doc_version, effective_date, domain, authority}
        （authority 仅在 item 无权威度时给出推断值）
    """
    from core.common import get_metadata

    metadata = get_metadata(item) if isinstance(item, dict) else {}

    # ---- 已有画像：直接信任元数据 ----
    doc_type = str(metadata.get("doc_type") or "").strip()
    status = str(metadata.get("effective_status") or "").strip()
    version = str(metadata.get("version") or "").strip()
    effective_date = str(metadata.get("effective_date") or "").strip()
    domain = str(metadata.get("domain") or "").strip()

    try:
        authority = float(
            item.get("authority")
            or metadata.get("authority")
            or metadata.get("source_priority")
            or 0
        )
    except (TypeError, ValueError):
        authority = 0.0

    # ---- 缺口：按文件名推断（旧库兼容路径）----
    title = str(
        item.get("title")
        or metadata.get("document_title")
        or metadata.get("source_document")
        or metadata.get("filename")
        or item.get("source_document")
        or ""
    )
    path = str(metadata.get("path") or "")

    if not doc_type or not version or status in ("", "current"):
        inferred = classify_document(title, path)
        doc_type = doc_type or inferred["doc_type"]
        # 注意：旧库 effective_status 硬编码 "current" 不可信，
        # 以文件名推断结果覆盖
        status = inferred["status"] if status in ("", "current") else status
        version = version or inferred["version"]
        effective_date = effective_date or inferred["effective_date"]
        domain = domain or inferred["domain"]
        if authority <= 0:
            authority = float(inferred["authority"])

    return {
        "doc_type": doc_type or "unknown",
        "doc_status": status or "current",
        "doc_version": version,
        "effective_date": effective_date,
        "domain": domain,
        "authority": authority,
    }
