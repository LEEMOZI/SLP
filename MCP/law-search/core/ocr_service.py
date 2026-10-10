"""OCR 服务：扫描版 PDF 与图片的文字提取（RapidOCR + PyMuPDF）。

设计约束（与项目约定一致）：
- 模型懒加载：首次 OCR 才加载 RapidOCR（不拖累 import/冷启动）
- 文本层优先：PDF 先试原生文本层，只有不足时才逐页渲染 + OCR（扫描版检测）
- 页数上限：OCR 逐页耗时 1~5s/页，默认单文件最多 OCR 50 页，防止长任务失控

对外接口：
    extract_text_smart(path, max_ocr_pages=50) -> (text, meta)
        meta: {"text_layer_pages": int, "ocr_pages": int, "skipped_pages": int, "total_pages": int}
"""

import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger("law-search.ocr")

# 单页 OCR 文本少于该字符数视为"文本层缺失/不足"
MIN_TEXT_CHARS_PER_PAGE = 8

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}

_OCR_ENGINE = None


def _ensure_ocr_engine():
    """懒加载 RapidOCR 引擎（首次调用时模型初始化）。"""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR

        started = __import__("time").perf_counter()
        _OCR_ENGINE = RapidOCR()
        logger.info(
            "RapidOCR ready in %.2fs", __import__("time").perf_counter() - started
        )
    return _OCR_ENGINE


def _ocr_image_bytes(img_bytes: bytes) -> str:
    """对单张图片字节流执行 OCR，返回按版面顺序拼接的文本。"""
    engine = _ensure_ocr_engine()
    result, _elapsed = engine(img_bytes)
    if not result:
        return ""
    # result: [[box, text, score], ...] 自上而下返回
    lines = [item[1].strip() for item in result if len(item) >= 2 and item[1]]
    return "\n".join(lines)


def _is_scanned_page(page) -> bool:
    """判断 PDF 页是否为扫描页（原生文本层缺失或极少）。"""
    try:
        text = (page.get_text("text") or "").strip()
    except Exception:
        text = ""
    return len(text) < MIN_TEXT_CHARS_PER_PAGE


def _extract_pdf(path: str, max_ocr_pages: int) -> Tuple[str, Dict[str, Any]]:
    import fitz  # PyMuPDF

    meta = {
        "text_layer_pages": 0,
        "ocr_pages": 0,
        "skipped_pages": 0,
        "total_pages": 0,
    }
    parts = []
    with fitz.open(path) as doc:
        meta["total_pages"] = doc.page_count
        for page in doc:
            if not _is_scanned_page(page):
                text = page.get_text("text").strip()
                meta["text_layer_pages"] += 1
                parts.append(text)
                continue
            # 扫描页 → 渲染 + OCR（受页数上限约束）
            if meta["ocr_pages"] >= max_ocr_pages:
                meta["skipped_pages"] += 1
                continue
            pix = page.get_pixmap(dpi=200)  # 200dpi 兼顾速度与识别率
            text = _ocr_image_bytes(pix.tobytes("png"))
            meta["ocr_pages"] += 1
            if text:
                parts.append(text)
    return "\n\n".join(p for p in parts if p.strip()), meta


def _extract_image(path: str) -> Tuple[str, Dict[str, Any]]:
    with open(path, "rb") as f:
        text = _ocr_image_bytes(f.read())
    meta = {
        "text_layer_pages": 0,
        "ocr_pages": 1 if text else 0,
        "skipped_pages": 0,
        "total_pages": 1,
    }
    return text, meta


def extract_text_smart(path: str, max_ocr_pages: int = 50) -> Tuple[str, Dict[str, Any]]:
    """智能提取文本：PDF 文本层优先，扫描页/图片走 OCR。

    返回 (text, meta)。text 可能为空（空扫描页/图片无文字）。
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)

    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        return _extract_pdf(path, max_ocr_pages)
    if ext in _IMAGE_EXTS:
        return _extract_image(path)
    raise ValueError(f"OCR 不支持的文件类型: {ext}")
