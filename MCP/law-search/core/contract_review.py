# -*- coding: utf-8 -*-
"""core/contract_review.py — 合同审查支持模块（review_contract 工具）

职责：
    1. 文件路径校验（允许目录白名单 + 扩展名 + 大小上限）
    2. 合同文本提取（PDF: pypdf 逐页 / Word: python-docx / txt·md 直读）
    3. 检索 query 构建（合同类型 + 关注点 → 检索语句）
    4. 审查 prompt 构建（风险清单结构 + 证据上下文 + 引用约束）

边界：
    - 只做纯函数与文件解析；检索 / LLM / 引用校验由 pipeline 编排
    - 引用一致性校验复用 core.litigation_builder.check_citations
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("law-search.contract")

# --------------------------------------------------------------------------
# 路径与大小限制
# --------------------------------------------------------------------------

# 授权目录从 config.path_config 统一导入（避免多处硬编码）
from config.path_config import (
    DSH_WORKSPACE_ROOT,
    DSH_UPLOADS_ROOT,
    ALLOWED_ROOTS,
)

# 向后兼容：重新导出供外部使用
__all__ = ["DSH_WORKSPACE_ROOT", "DSH_UPLOADS_ROOT", "ALLOWED_ROOTS"]

ALLOWED_EXTENSIONS: Tuple[str, ...] = (
    ".pdf", ".docx", ".doc", ".txt", ".md",
    ".ppt", ".pptx", ".xls", ".xlsx",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp",
)

_IMAGE_EXTS: Tuple[str, ...] = (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp")

MAX_FILE_MB = 20

# 合同文本上限（deepseek 128k 上下文足够容纳；超出截断并提示）
_MAX_CONTRACT_CHARS = 80_000


def validate_path(file_path: str) -> Tuple[bool, str]:
    """路径校验：白名单目录 + 存在性 + 扩展名 + 大小。返回 (ok, 错误信息)。"""
    raw = (file_path or "").strip()
    if not raw:
        return False, "文件路径为空。"
    path = os.path.realpath(os.path.expanduser(raw))

    if not any(path.startswith(root + os.sep) or path == root for root in ALLOWED_ROOTS):
        return False, (
            f"路径不在允许范围内。允许的目录：{', '.join(ALLOWED_ROOTS)}"
        )
    if not os.path.isfile(path):
        return False, f"文件不存在：{path}"
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False, (
            f"不支持的文件类型 {ext}。支持：{', '.join(ALLOWED_EXTENSIONS)}"
        )
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb > MAX_FILE_MB:
        return False, f"文件过大（{size_mb:.1f}MB），上限 {MAX_FILE_MB}MB。"
    return True, ""


# --------------------------------------------------------------------------
# 路径解析（DSH 上传区模糊匹配）
# --------------------------------------------------------------------------

# DSH 上传区文件名格式：<16位十六进制hash>-<原文件名>
_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{16}-(?P<name>.+)$")


def _strip_hash_prefix(name: str) -> str:
    m = _HASH_PREFIX_RE.match(name)
    return m.group("name") if m else name


def _iter_upload_files() -> List[str]:
    """遍历 DSH 上传区所有会话目录下的文件（跳过隐藏文件）。"""
    found: List[str] = []
    if not os.path.isdir(DSH_UPLOADS_ROOT):
        return found
    for entry in os.listdir(DSH_UPLOADS_ROOT):
        session_dir = os.path.join(DSH_UPLOADS_ROOT, entry)
        if not entry.startswith("session-") or not os.path.isdir(session_dir):
            continue
        for name in os.listdir(session_dir):
            if name.startswith("."):
                continue
            found.append(os.path.join(session_dir, name))
    return found


def resolve_path(file_path: str) -> str:
    """把 LLM 传入的文件路径解析为真实可读路径。

    处理三类真实失败模式（2026-09-01 星图合同审查全败的根因）：
        1. DSH 上传区相对路径（.dsh-uploads/...）→ 以 DSH WorkSpace 为根补全
        2. 原始路径已不存在（文件被 DSH 拷贝到上传区并加 hash 前缀）
           → 按文件名（去 hash 前缀）在上传区精确匹配
        3. 同一文件在多个会话重复上传 → 取 mtime 最新一份

    Raises:
        ValueError: 路径为空 / 文件不存在且上传区无同名文件
    """
    raw = (file_path or "").strip()
    if not raw:
        raise ValueError("文件路径为空。")
    if not os.path.isabs(raw) and raw.replace("\\", "/").startswith(".dsh-uploads"):
        raw = os.path.join(DSH_WORKSPACE_ROOT, raw)
    path = os.path.realpath(os.path.expanduser(raw))
    if os.path.isfile(path):
        return path

    # 上传区模糊匹配：按原始文件名（去 hash 前缀）精确比对
    want = os.path.basename(path)
    want_plain = _strip_hash_prefix(want)
    candidates = []
    for f in _iter_upload_files():
        name = os.path.basename(f)
        if want_plain and (name == want or _strip_hash_prefix(name) == want_plain):
            candidates.append(f)
    if not candidates:
        raise ValueError(
            f"文件不存在：{path}\n"
            f"（DSH 上传区 {DSH_UPLOADS_ROOT} 下也未找到同名文件，"
            "请确认文件名，或让用户重新上传/提供文件的真实绝对路径）"
        )
    if len(candidates) > 1:
        candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    resolved = os.path.realpath(candidates[0])
    logger.info("resolve_path: %s -> %s", raw, resolved)
    return resolved


# --------------------------------------------------------------------------
# 文本提取
# --------------------------------------------------------------------------

def extract_text(file_path: str) -> Tuple[str, Dict[str, Any]]:
    """提取合同文本。返回 (text, meta)；meta 含 pages/截断标记等。

    Raises:
        ValueError: 解析失败（损坏文件 / 加密 PDF / 空 docx 等）
    """
    path = os.path.realpath(os.path.expanduser(file_path))
    ext = os.path.splitext(path)[1].lower()
    meta: Dict[str, Any] = {"file": path, "ext": ext, "truncated": False}

    if ext == ".pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(path)
        except Exception as exc:  # 加密/损坏
            raise ValueError(f"PDF 解析失败（可能已加密或损坏）：{exc}") from exc
        pages = []
        for i, page in enumerate(reader.pages):
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append(f"（第{i + 1}页解析失败）")
        meta["pages"] = len(reader.pages)
        text = "\n\n".join(p for p in pages if p.strip())
        # 扫描版 PDF（文本层缺失/极少）→ OCR fallback
        if len(text.strip()) < 50:
            from core.ocr_service import extract_text_smart

            text, ocr_meta = extract_text_smart(path)
            meta["ocr"] = ocr_meta

    elif ext in _IMAGE_EXTS:
        # 拍照/扫描图片合同 → 直接 OCR
        from core.ocr_service import extract_text_smart

        text, ocr_meta = extract_text_smart(path)
        meta["ocr"] = ocr_meta

    elif ext in (".doc", ".ppt", ".pptx", ".xls", ".xlsx", ".html", ".htm"):
        # Office 全家桶 + HTML（doc 走 textutil；pptx/ppt/xls/xlsx/html 走 office_reader）
        from core.office_reader import extract_office_text

        text, office_meta = extract_office_text(path)
        meta["office"] = office_meta

    elif ext == ".docx":
        from core.office_reader import _read_docx

        text, meta["office"] = _read_docx(path)

    else:  # .txt / .md
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()

    if not text.strip():
        raise ValueError(
            "未提取到任何文本内容（文件可能是空白扫描件、纯图片或加密文档）。"
        )

    if len(text) > _MAX_CONTRACT_CHARS:
        meta["truncated"] = True
        meta["orig_chars"] = len(text)
        text = text[:_MAX_CONTRACT_CHARS]

    meta["chars"] = len(text)
    return text, meta


# --------------------------------------------------------------------------
# 检索 query 与 prompt
# --------------------------------------------------------------------------

# 检索 query 不宜过长：类型 + 关注点 + 合同开头（标题/当事人段最有信息量）
_QUERY_HEAD_CHARS = 400

# 合同正文自带的条款编号（第一条/第2条…）会被 Query Understanding 误识别为
# 法条引用，导致 where 过滤跑偏；构建检索 query 前必须清洗
_ARTICLE_NO_RE = re.compile(r"第[0-9一二三四五六七八九十百千]+条")


def build_review_query(
    contract_text: str, contract_type: str = "", focus_points: str = ""
) -> str:
    parts: List[str] = []
    if (contract_type or "").strip():
        parts.append(f"{contract_type.strip()}合同 相关法律规定")
    fp = (focus_points or "").strip()
    if fp:
        parts.append(_ARTICLE_NO_RE.sub(" ", fp[:300]))
    if contract_text:
        parts.append(_ARTICLE_NO_RE.sub(" ", contract_text[:_QUERY_HEAD_CHARS]))
    return " ".join(p.strip() for p in parts if p.strip()).strip()


def build_review_prompt(
    contract_text: str,
    *,
    contract_type: str = "",
    focus_points: str = "",
    truncated: bool = False,
    evidence_context: str = "",
) -> str:
    """构建合同审查 prompt（风险清单结构）。"""
    type_line = contract_type.strip() or "（未指定，请根据文本判断）"
    focus_line = focus_points.strip() or "（未指定，请全面审查）"
    trunc_note = (
        "\n注意：合同原文过长已被截断，仅审查前部分内容，报告中须说明此限制。"
        if truncated else ""
    )
    evidence_block = (
        evidence_context.strip()
        or "（检索未找到可用法条，请在报告中注明：本次审查未经知识库法条校验）"
    )

    return "\n".join([
        "你是一名资深中国执业律师，专长合同审查。请对以下合同进行全面审查，输出风险清单报告。",
        "",
        "【合同类型】",
        type_line,
        "",
        "【重点关注】",
        focus_line,
        "",
        "【合同全文】",
        contract_text,
        f"{trunc_note}",
        "",
        "【可用法律依据】（来自本地法律知识库检索，只可引用其中确实支持的条文）",
        evidence_block,
        "",
        "【报告结构要求】",
        "一、合同概况：合同类型、当事人、核心交易内容（3 行以内）",
        "二、风险清单：按 高危 / 中等 / 提示 三级分级。每条风险包含：",
        "    1. 风险条款（引用原文位置或原文摘要）",
        "    2. 风险说明（对我方的具体不利后果）",
        "    3. 修改建议（给出可直接使用的替代条款文本）",
        "    4. 法律依据（《法名》第X条，仅限【可用法律依据】中的条文）",
        "三、缺失条款提示：常见必要条款缺失清单（如违约责任、争议解决、解除条件等）",
        "四、总体结论：是否建议签署（可签 / 修改后可签 / 建议不签）及核心理由（3 行以内）",
        "",
        "【写作要求】",
        "1. 严格使用简体中文；法条引用必须采用《法名》第X条格式，且仅限【可用法律依据】中出现的条文，不得虚构。",
        "2. 风险判断须基于合同原文，不得臆测未出现的条款内容。",
        "3. 若【可用法律依据】中没有能支持某项风险判断的条文，该条的“法律依据”写“（本次检索证据未覆盖，建议人工核对相关法律规定）”，不得编造。",
        "4. 必须完整输出四个部分，尤其是“四、总体结论”不得省略。",
        "5. 直接输出报告正文，不要输出任何解释、前言或后记。",
    ])
