import hashlib
import importlib.util
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, Optional, Tuple

PARSER_VERSION = "1.0.0"
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
MIN_TEXT_CHARS_PER_PAGE = 8


@dataclass(frozen=True)
class ParserLimits:
    max_file_bytes: int = 20 * 1024 * 1024
    max_files_per_case: int = 100
    max_ocr_pages: int = 20
    max_text_chars: int = 500000

    @classmethod
    def from_env(cls) -> "ParserLimits":
        return cls(
            max_file_bytes=_positive_env("SLP_CAPABILITY_MAX_FILE_BYTES", 20 * 1024 * 1024),
            max_files_per_case=_positive_env("SLP_CAPABILITY_MAX_FILES_PER_CASE", 100),
            max_ocr_pages=_positive_env("SLP_CAPABILITY_MAX_OCR_PAGES", 20),
            max_text_chars=_positive_env("SLP_CAPABILITY_MAX_TEXT_CHARS", 500000),
        )

    def as_dict(self) -> Dict[str, int]:
        return {
            "max_file_bytes": self.max_file_bytes,
            "max_files_per_case": self.max_files_per_case,
            "max_ocr_pages": self.max_ocr_pages,
            "max_text_chars": self.max_text_chars,
        }


def _positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class ParseFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _ocr_available() -> bool:
    try:
        return (
            importlib.util.find_spec("fitz") is not None
            and importlib.util.find_spec("rapidocr_onnxruntime") is not None
        )
    except (ImportError, ValueError):
        return False


def _get_text_layer(page: Any) -> str:
    return (page.get_text("text") or "").strip()


def _ocr_image_bytes(image: bytes) -> str:
    """Reuse the existing atomic OCR function without importing the MCP server."""
    module_path = (
        Path(__file__).resolve().parents[2]
        / "MCP"
        / "law-search"
        / "core"
        / "ocr_service.py"
    )
    spec = importlib.util.spec_from_file_location("_slp_capability_ocr_service", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("OCR capability unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module._ocr_image_bytes(image) or "")


def ocr_available() -> bool:
    return _ocr_available()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _validate_docx(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "word/document.xml" not in names or "[Content_Types].xml" not in names:
                raise ParseFailure("format_mismatch", "文件扩展名与实际格式不符")
    except ParseFailure:
        raise
    except (OSError, zipfile.BadZipFile) as error:
        raise ParseFailure("invalid_docx", "DOCX 文件已损坏或格式无效") from error


def _sniff_format(path: Path, extension: str) -> bytes:
    try:
        with path.open("rb") as stream:
            prefix = stream.read(4096)
    except OSError as error:
        raise ParseFailure("file_unreadable", "材料文件无法读取") from error
    if extension == ".pdf" and not prefix.startswith(b"%PDF-"):
        raise ParseFailure("format_mismatch", "文件扩展名与实际格式不符")
    if extension == ".docx":
        _validate_docx(path)
    if extension in {".txt", ".md"}:
        if (
            b"\x00" in prefix
            or prefix.startswith(b"%PDF-")
            or prefix.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0"))
        ):
            raise ParseFailure("format_mismatch", "文本材料包含不支持的二进制内容")
        try:
            path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            raise ParseFailure("invalid_text", "TXT/Markdown 必须是有效的 UTF-8 文本") from error
    return prefix


def _base_event(event_type: str, material_id: str, parse_version_id: str) -> Dict[str, Any]:
    return {
        "type": event_type,
        "material_id": material_id,
        "parse_version_id": parse_version_id,
    }


def _segment(
    material_id: str,
    parse_version_id: str,
    index: int,
    text: str,
    locator: Dict[str, int],
    status: str = "ok",
    extraction_method: Optional[str] = None,
    error: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    event = _base_event("segment", material_id, parse_version_id)
    event.update(
        {
            "index": index,
            "text": text,
            "status": status,
            "locator": locator,
            "extraction_method": extraction_method,
        }
    )
    event.update(locator)
    if error is not None:
        event["error"] = error
    return event


def _fit_text(text: str, remaining: int) -> Tuple[str, bool]:
    if len(text) <= remaining:
        return text, False
    return text[:max(0, remaining)], True


class MaterialParser:
    def __init__(self, material_root: Path, limits: ParserLimits) -> None:
        self.material_root = material_root.expanduser().resolve()
        self.limits = limits

    def parse(
        self, material_id: str, parse_version_id: str, raw_path: str, expected_sha256: str
    ) -> Generator[Dict[str, Any], None, None]:
        try:
            path, extension = self._validate_file(raw_path, expected_sha256)
        except ParseFailure as error:
            event = _base_event("error", material_id, parse_version_id)
            event.update(
                {
                    "status": "failed",
                    "error": {"code": error.code, "message": error.message},
                }
            )
            yield event
            return

        meta = _base_event("meta", material_id, parse_version_id)
        meta.update(
            {
                "parser_version": PARSER_VERSION,
                "format": extension[1:],
                "locator_type": {
                    ".pdf": "page",
                    ".docx": "docx_block",
                    ".txt": "text_line",
                    ".md": "text_line",
                }[extension],
                "limits": self.limits.as_dict(),
            }
        )
        yield meta

        try:
            if extension == ".pdf":
                for event in self._parse_pdf(material_id, parse_version_id, path):
                    yield event
            elif extension == ".docx":
                for event in self._parse_docx(material_id, parse_version_id, path):
                    yield event
            else:
                for event in self._parse_text(material_id, parse_version_id, path, extension):
                    yield event
        except ParseFailure as error:
            event = _base_event("error", material_id, parse_version_id)
            event.update(
                {
                    "status": "failed",
                    "error": {"code": error.code, "message": error.message},
                }
            )
            yield event
        except Exception:
            event = _base_event("error", material_id, parse_version_id)
            event.update(
                {
                    "status": "failed",
                    "error": {"code": "parse_failed", "message": "材料解析失败"},
                }
            )
            yield event

    def _validate_file(self, raw_path: str, expected_sha256: str) -> Tuple[Path, str]:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256 or ""):
            raise ParseFailure("invalid_sha256", "SHA-256 格式无效")
        try:
            path = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ParseFailure("file_not_found", "材料文件不存在") from error
        try:
            path.relative_to(self.material_root)
        except ValueError as error:
            raise ParseFailure("path_outside_root", "材料路径不在允许的根目录内") from error
        if not path.is_file():
            raise ParseFailure("file_not_found", "材料文件不存在")
        extension = path.suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ParseFailure("unsupported_format", "材料格式不受支持")
        try:
            size = path.stat().st_size
        except OSError as error:
            raise ParseFailure("file_unreadable", "材料文件无法读取") from error
        if size > self.limits.max_file_bytes:
            raise ParseFailure("file_too_large", "材料超过单文件大小限制")
        try:
            actual_sha256 = _sha256(path)
        except OSError as error:
            raise ParseFailure("file_unreadable", "材料文件无法读取") from error
        if actual_sha256.lower() != expected_sha256.lower():
            raise ParseFailure("hash_mismatch", "材料 SHA-256 校验不一致")
        _sniff_format(path, extension)
        return path, extension

    def _parse_pdf(
        self, material_id: str, parse_version_id: str, path: Path
    ) -> Generator[Dict[str, Any], None, None]:
        try:
            import fitz

            document = fitz.open(str(path))
        except Exception as error:
            raise ParseFailure("invalid_pdf", "PDF 文件已损坏或格式无效") from error
        try:
            if document.needs_pass:
                raise ParseFailure("encrypted_pdf", "不支持加密 PDF，请先解除密码保护")
            total_pages = document.page_count
            coverage = {
                "total_pages": total_pages,
                "processed_pages": 0,
                "text_layer_pages": 0,
                "ocr_pages": 0,
                "empty_pages": [],
                "failed_pages": [],
                "ocr_uncovered": [],
                "text_truncated": False,
            }
            char_count = 0
            segment_count = 0
            useful_pages = 0
            ocr_attempts = 0
            ocr_usable = None
            for page_index in range(total_pages):
                page_number = page_index + 1
                coverage["processed_pages"] = page_number
                text = ""
                status = "ok"
                method = None
                error = None
                try:
                    native_text = _get_text_layer(document.load_page(page_index))
                except Exception:
                    status = "failed"
                    error = {"code": "page_extract_failed", "message": "PDF 页面文本提取失败"}
                    coverage["failed_pages"].append(page_number)
                else:
                    page = document.load_page(page_index)
                    if len(native_text) >= MIN_TEXT_CHARS_PER_PAGE:
                        text = native_text
                        method = "text_layer"
                        coverage["text_layer_pages"] += 1
                    else:
                        if ocr_attempts >= self.limits.max_ocr_pages:
                            status = "uncovered"
                            error = {"code": "ocr_page_limit", "message": "页面超出 OCR 页数上限"}
                            coverage["ocr_uncovered"].append(
                                {"page": page_number, "reason": "ocr_page_limit"}
                            )
                        else:
                            if ocr_usable is None:
                                ocr_usable = _ocr_available()
                            if not ocr_usable:
                                status = "uncovered"
                                error = {"code": "ocr_unavailable", "message": "OCR 能力不可用"}
                                coverage["ocr_uncovered"].append(
                                    {"page": page_number, "reason": "ocr_unavailable"}
                                )
                            else:
                                ocr_attempts += 1
                                try:
                                    pixmap = page.get_pixmap(dpi=150)
                                    text = (_ocr_image_bytes(pixmap.tobytes("png")) or "").strip()
                                    method = "ocr"
                                    coverage["ocr_pages"] += 1
                                    if not text:
                                        status = "empty"
                                        coverage["empty_pages"].append(page_number)
                                except Exception:
                                    status = "failed"
                                    method = "ocr"
                                    error = {"code": "ocr_failed", "message": "PDF 页面 OCR 失败"}
                                    coverage["failed_pages"].append(page_number)
                                    coverage["ocr_uncovered"].append(
                                        {"page": page_number, "reason": "ocr_failed"}
                                    )
                if text:
                    remaining = self.limits.max_text_chars - char_count
                    text, truncated = _fit_text(text, remaining)
                    if truncated:
                        status = "partial"
                        coverage["text_truncated"] = True
                        coverage["ocr_uncovered"].append(
                            {"page": page_number, "reason": "text_limit"}
                        )
                    char_count += len(text)
                if status in {"ok", "partial"} and text:
                    useful_pages += 1
                segment_count += 1
                yield _segment(
                    material_id,
                    parse_version_id,
                    segment_count,
                    text,
                    {"page": page_number},
                    status=status,
                    extraction_method=method,
                    error=error,
                )
                if coverage["text_truncated"]:
                    for remaining_page in range(page_number + 1, total_pages + 1):
                        segment_count += 1
                        coverage["processed_pages"] = remaining_page
                        coverage["ocr_uncovered"].append(
                            {"page": remaining_page, "reason": "text_limit"}
                        )
                        yield _segment(
                            material_id,
                            parse_version_id,
                            segment_count,
                            "",
                            {"page": remaining_page},
                            status="uncovered",
                            error={"code": "text_limit", "message": "文本总量上限已到达"},
                        )
                    break
            issues = bool(
                coverage["failed_pages"] or coverage["ocr_uncovered"] or coverage["text_truncated"]
            )
            status = (
                "partial" if issues and useful_pages
                else "failed" if issues or not useful_pages
                else "success"
            )
            done = _base_event("done", material_id, parse_version_id)
            done.update(
                {
                    "status": status,
                    "parser_version": PARSER_VERSION,
                    "segment_count": segment_count,
                    "char_count": char_count,
                    "coverage": coverage,
                }
            )
            if not useful_pages:
                done["error"] = {"code": "no_text_extracted", "message": "未提取到可用文本"}
            yield done
        finally:
            document.close()

    def _parse_docx(
        self, material_id: str, parse_version_id: str, path: Path
    ) -> Generator[Dict[str, Any], None, None]:
        try:
            from docx import Document

            document = Document(str(path))
        except Exception as error:
            raise ParseFailure("invalid_docx", "DOCX 文件已损坏、加密或格式无效") from error
        blocks = []
        for index, paragraph in enumerate(document.paragraphs, 1):
            if paragraph.text.strip():
                blocks.append((paragraph.text, {"paragraph": index}))
        for table_index, table in enumerate(document.tables, 1):
            for row_index, row in enumerate(table.rows, 1):
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    blocks.append((" | ".join(cells), {"table": table_index, "row": row_index}))
        char_count = 0
        emitted = 0
        truncated = False
        for text, locator in blocks:
            remaining = self.limits.max_text_chars - char_count
            fitted, truncated = _fit_text(text, remaining)
            char_count += len(fitted)
            emitted += 1
            yield _segment(
                material_id,
                parse_version_id,
                emitted,
                fitted,
                locator,
                status="partial" if truncated else "ok",
                extraction_method="docx_paragraph" if "paragraph" in locator else "docx_table_row",
            )
            if truncated:
                break
        done = _base_event("done", material_id, parse_version_id)
        done.update(
            {
                "status": "failed" if not char_count else "partial" if truncated else "success",
                "parser_version": PARSER_VERSION,
                "segment_count": emitted,
                "char_count": char_count,
                "coverage": {
                    "total_blocks": len(blocks),
                    "parsed_blocks": emitted,
                    "uncovered_blocks": len(blocks) - emitted,
                    "text_truncated": truncated,
                },
            }
        )
        if not char_count:
            done["error"] = {"code": "no_text_extracted", "message": "未提取到可用文本"}
        yield done

    def _parse_text(
        self, material_id: str, parse_version_id: str, path: Path, extension: str
    ) -> Generator[Dict[str, Any], None, None]:
        try:
            content = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as error:
            raise ParseFailure("invalid_text", "TXT/Markdown 必须是有效的 UTF-8 文本") from error
        lines = content.splitlines(keepends=True)
        char_count = 0
        emitted = 0
        offset = 0
        truncated = False
        for line_number, raw_line in enumerate(lines, 1):
            text = raw_line.rstrip("\r\n")
            line_start = offset
            offset += len(raw_line)
            if not text.strip():
                continue
            remaining = self.limits.max_text_chars - char_count
            fitted, truncated = _fit_text(text, remaining)
            char_count += len(fitted)
            emitted += 1
            locator = {
                "line_start": line_number,
                "line_end": line_number,
                "char_start": line_start,
                "char_end": line_start + len(fitted),
            }
            yield _segment(
                material_id,
                parse_version_id,
                emitted,
                fitted,
                locator,
                status="partial" if truncated else "ok",
                extraction_method="markdown_line" if extension == ".md" else "text_line",
            )
            if truncated:
                break
        done = _base_event("done", material_id, parse_version_id)
        done.update(
            {
                "status": "failed" if not char_count else "partial" if truncated else "success",
                "parser_version": PARSER_VERSION,
                "segment_count": emitted,
                "char_count": char_count,
                "coverage": {
                    "total_lines": len(lines),
                    "parsed_lines": emitted,
                    "text_truncated": truncated,
                },
            }
        )
        if not char_count:
            done["error"] = {"code": "no_text_extracted", "message": "未提取到可用文本"}
        yield done
