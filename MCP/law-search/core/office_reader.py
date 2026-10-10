"""Office 文件文本读取：doc/docx/ppt/pptx/xls/xlsx 统一入口。

格式支持矩阵：
- .docx : python-docx（段落 + 表格）
- .doc  : macOS textutil（系统自带，subprocess 转 txt）
- .pptx : python-pptx（幻灯片文本框 + 表格 + 备注）
- .ppt  : 不支持（旧版二进制格式，本机无 LibreOffice；提示另存为 .pptx）
- .xlsx : openpyxl（工作表名 + 单元格网格）
- .xls  : xlrd（旧版二进制 Excel）

对外接口：
    extract_office_text(path) -> (text, meta)
        meta: {"format": str, "sheet_count"/"slide_count"/"paragraphs": ...}
"""

import os
import subprocess
from typing import Any, Dict, Tuple

logger_o = None


def _log(msg: str) -> None:
    global logger_o
    if logger_o is None:
        import logging

        logger_o = logging.getLogger("law-search.office")
    logger_o.info(msg)


# 大文件防护：单元格/形状读取上限
_MAX_CELLS = 200_000
_MAX_SHAPES = 5_000


# --------------------------------------------------------------------------
# .doc（旧版 Word）→ macOS textutil
# --------------------------------------------------------------------------

def _read_doc(path: str) -> Tuple[str, Dict[str, Any]]:
    """textutil 是 macOS 自带工具，支持 doc/docx/rtf 等互转，stdout 直出。"""
    proc = subprocess.run(
        ["textutil", "-convert", "txt", "-stdout", path],
        capture_output=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise ValueError(f"textutil 转换失败: {proc.stderr.decode('utf-8', 'ignore')[:200]}")
    text = proc.stdout.decode("utf-8", "ignore")
    return text, {"format": "doc(textutil)"}


# --------------------------------------------------------------------------
# .docx
# --------------------------------------------------------------------------

def _read_docx(path: str) -> Tuple[str, Dict[str, Any]]:
    from docx import Document

    doc = Document(path)
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    n_tables = 0
    for table in doc.tables:
        n_tables += 1
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return (
        "\n".join(parts),
        {"format": "docx", "paragraphs": len(parts), "tables": n_tables},
    )


# --------------------------------------------------------------------------
# .pptx
# --------------------------------------------------------------------------

def _read_pptx(path: str) -> Tuple[str, Dict[str, Any]]:
    from pptx import Presentation

    prs = Presentation(path)
    parts = []
    n_shapes = 0
    for idx, slide in enumerate(prs.slides, 1):
        texts = []
        for shape in slide.shapes:
            if n_shapes >= _MAX_SHAPES:
                break
            n_shapes += 1
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    texts.append(t)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        texts.append(" | ".join(cells))
        # 演讲者备注
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                texts.append(f"[备注] {notes}")
        if texts:
            parts.append(f"--- 幻灯片 {idx} ---\n" + "\n".join(texts))
    return (
        "\n\n".join(parts),
        {"format": "pptx", "slides": len(prs.slides), "shapes": n_shapes},
    )


# --------------------------------------------------------------------------
# .xlsx / .xls
# --------------------------------------------------------------------------

def _grid_to_text(rows_iter, sheet_label: str, counter: Dict[str, int]) -> str:
    """把二维单元格序列转成 ' | ' 连接的行文本。"""
    lines = [f"--- 工作表 {sheet_label} ---"]
    n = 0
    for row in rows_iter:
        cells = []
        for cell in row:
            n += 1
            if n > _MAX_CELLS:
                break
            v = ("" if cell is None else str(cell)).strip()
            if v:
                cells.append(v)
        if cells:
            lines.append(" | ".join(cells))
        if n > _MAX_CELLS:
            break
    counter["cells"] = counter.get("cells", 0) + n
    return "\n".join(lines)


def _read_xlsx(path: str, _allow_fallback: bool = True) -> Tuple[str, Dict[str, Any]]:
    import io
    from openpyxl import load_workbook

    wb = None
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        msg = str(exc)
        # openpyxl 按扩展名嗅探拒收错标文件（.xls 实为 xlsx 等）：
        # 换 BytesIO 句柄绕过扩展名检查，按真实内容再试一次
        if "does not support" in msg:
            try:
                with open(path, "rb") as fh:
                    buf = io.BytesIO(fh.read())
                wb = load_workbook(buf, read_only=True, data_only=True)
            except Exception:
                wb = None
        if wb is None:
            if _allow_fallback and ("zip" in msg.lower() or "not a" in msg.lower()):
                # 可能是扩展名错标的真 .xls
                _log("openpyxl rejected file; retrying as xls")
                return _read_xls(path, _allow_fallback=False)
            if "does not support" in msg:
                # 注意：此分支是终局重定向（docx/pptx 不会再回到 xls/xlsx），不受 _allow_fallback 限制。
                # zip 头但非 xlsx：openpyxl 对任何非 Excel 的 zip 都报"old .xls"误导性错误。
                # 按实际内容分发（docx/pptx 错标为 xls/xlsx 的场景在真实盘很常见）
                import zipfile

                try:
                    names = set(zipfile.ZipFile(path).namelist())
                except Exception as zip_exc:
                    # 非 zip 容器：OLE2 复合文档头 = 真旧版 .xls 被错标为 .xlsx
                    try:
                        with open(path, "rb") as fh:
                            magic = fh.read(8)
                    except Exception:
                        magic = b""
                    if magic == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
                        _log("file is actually legacy xls (OLE2); redirecting to xlrd")
                        return _read_xls(path, _allow_fallback=False)
                    raise ValueError(f"文件既非有效 xlsx 也非 zip 结构: {zip_exc}") from exc
                if "word/document.xml" in names:
                    _log("file is actually docx; redirecting")
                    return _read_docx(path)
                if "ppt/presentation.xml" in names:
                    _log("file is actually pptx; redirecting")
                    return _read_pptx(path)
                if "xl/workbook.xml" in names:
                    # xlsx 结构存在但 workbook.xml 解析失败（损坏/异常命名空间）
                    _log("xlsx workbook parse failed; using raw string extraction")
                    return _read_xlsx_raw(path)
                raise ValueError(
                    "文件是 zip 容器但内容不是 xlsx/docx/pptx（可能是压缩包或未知格式）"
                ) from exc
            raise ValueError(f"xlsx 解析失败: {msg}") from exc
    counter: Dict[str, int] = {}
    parts = []
    for ws in wb.worksheets:
        parts.append(_grid_to_text(ws.iter_rows(values_only=True), ws.title, counter))
    wb.close()
    return (
        "\n\n".join(p for p in parts if p.strip()),
        {"format": "xlsx", "sheets": len(parts), **counter},
    )


def _read_xlsx_raw(path: str) -> Tuple[str, Dict[str, Any]]:
    """openpyxl 打不开但 xlsx 结构完整的兜底：直接抽 sharedStrings + sheet 内联文本。

    输出为文本池（顺序保留、可能含重复），对检索场景足够；不做结构化行列还原。
    """
    import html
    import re
    import zipfile

    texts: list = []
    with zipfile.ZipFile(path) as zf:
        # 1. 共享字符串池（xlsx 文本的主要存放处）
        if "xl/sharedStrings.xml" in zf.namelist():
            xml = zf.read("xl/sharedStrings.xml").decode("utf-8", "ignore")
            texts.extend(re.findall(r"<t[^>]*>([^<]+)</t>", xml))
        # 2. 各 sheet 的内联字符串
        for name in zf.namelist():
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                xml = zf.read(name).decode("utf-8", "ignore")
                texts.extend(re.findall(r"<t[^>]*>([^<]+)</t>", xml))
    # openpyxl 等写入方会把非 ASCII 转成 &#NNNN; 数字实体，必须解码还原
    texts = [html.unescape(t).strip() for t in texts if t and t.strip()]
    return "\n".join(texts), {
        "format": "xlsx(raw-strings)", "strings": len(texts),
        "total_chars": sum(len(t) for t in texts),
    }


def _read_xls(path: str, _allow_fallback: bool = True) -> Tuple[str, Dict[str, Any]]:
    import xlrd

    try:
        book = xlrd.open_workbook(path)
    except xlrd.biffh.XLRDError as exc:
        # 常见错标：扩展名 .xls 实为 xlsx（或反之），按实际格式重读
        msg = str(exc)
        if _allow_fallback and "xlsx" in msg:
            _log("xlrd detected xlsx content; retrying as xlsx")
            return _read_xlsx(path, _allow_fallback=False)
        raise ValueError(f"xls 解析失败: {msg}") from exc
    counter: Dict[str, int] = {}
    parts = []
    for si in range(book.nsheets):
        sheet = book.sheet_by_index(si)
        grid = []
        for r in range(sheet.nrows):
            grid.append([sheet.cell_value(r, c) for c in range(sheet.ncols)])
        parts.append(_grid_to_text(grid, sheet.name, counter))
    return (
        "\n\n".join(p for p in parts if p.strip()),
        {"format": "xls", "sheets": book.nsheets, **counter},
    )


# --------------------------------------------------------------------------
# .html / .htm → 纯文本提取（内置 html.parser，无需外部依赖）
# --------------------------------------------------------------------------

def _read_html(path: str) -> Tuple[str, Dict[str, Any]]:
    from html.parser import HTMLParser
    import re

    class _TextExtractor(HTMLParser):
        """提取 HTML 纯文本，忽略 script/style 标签。"""

        _SKIP_TAGS = {"script", "style", "noscript"}

        def __init__(self):
            super().__init__()
            self._parts: list[str] = []
            self._skip_depth = 0

        def handle_starttag(self, tag, attrs):
            if tag.lower() in self._SKIP_TAGS:
                self._skip_depth += 1

        def handle_endtag(self, tag):
            if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
                self._skip_depth -= 1

        def handle_data(self, data):
            if self._skip_depth == 0:
                text = data.strip()
                if text:
                    self._parts.append(text)

    with open(path, encoding="utf-8", errors="replace") as f:
        html_content = f.read()

    extractor = _TextExtractor()
    extractor.feed(html_content)
    text = "\n".join(extractor._parts)

    # 清理多余空行
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, {"format": "html", "chars": len(text)}


# --------------------------------------------------------------------------
# .ppt → 尝试 textutil 提取（macOS 可能部分支持）
# --------------------------------------------------------------------------

def _read_ppt(path: str) -> Tuple[str, Dict[str, Any]]:
    """旧版 .ppt 二进制格式。先试 textutil，失败则提示转换。"""
    try:
        proc = subprocess.run(
            ["textutil", "-convert", "txt", "-stdout", path],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode == 0:
            text = proc.stdout.decode("utf-8", "ignore").strip()
            if len(text) > 20:  # 有实际内容
                return text, {"format": "ppt(textutil)"}
    except Exception:
        pass

    raise ValueError(
        "旧版 .ppt 为二进制格式，textutil 无法解析。"
        "请用 PowerPoint/WPS 另存为 .pptx 后重试。"
    )


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def extract_office_text(path: str) -> Tuple[str, Dict[str, Any]]:
    """按扩展名分发读取 Office/HTML 文件。返回 (text, meta)。

    支持：.doc/.docx/.pptx/.ppt/.xls/.xlsx/.html/.htm
    Raises:
        ValueError: 不支持的格式或读取失败
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    ext = os.path.splitext(path)[1].lower()

    if ext == ".doc":
        return _read_doc(path)
    if ext == ".docx":
        return _read_docx(path)
    if ext == ".pptx":
        return _read_pptx(path)
    if ext == ".ppt":
        return _read_ppt(path)
    if ext == ".xlsx":
        return _read_xlsx(path)
    if ext == ".xls":
        return _read_xls(path)
    if ext in (".html", ".htm"):
        return _read_html(path)
    raise ValueError(f"Office 读取不支持的扩展名: {ext}")
