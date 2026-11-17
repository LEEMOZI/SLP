import hashlib
import json

import fitz
import pytest
from docx import Document
from fastapi.testclient import TestClient

from apps.capability_adapter.app import create_app
from apps.capability_adapter import parser as parser_module


def _write(root, name, data):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.write_bytes(data)
    return path, hashlib.sha256(data).hexdigest()


def _payload(path, digest):
    return {
        "material_id": "material-1",
        "parse_version_id": "version-1",
        "path": str(path),
        "sha256": digest,
    }


def _parse(client, path, digest):
    response = client.post("/v1/parse", json=_payload(path, digest))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    return [json.loads(line) for line in response.text.splitlines() if line]


@pytest.fixture
def client(tmp_path, monkeypatch):
    root = tmp_path / "materials"
    root.mkdir()
    monkeypatch.setenv("SLP_WORKBENCH_MATERIAL_ROOT", str(root))
    monkeypatch.setenv("SLP_CAPABILITY_MAX_FILE_BYTES", "1048576")
    monkeypatch.setenv("SLP_CAPABILITY_MAX_OCR_PAGES", "5")
    monkeypatch.setenv("SLP_CAPABILITY_MAX_TEXT_CHARS", "10000")
    return TestClient(create_app()), root


@pytest.mark.parametrize("name", ["notes.txt", "notes.md"])
def test_text_formats_preserve_line_and_character_locators(client, name):
    http, root = client
    text = "第一行\n\nthird line\n"
    path, digest = _write(root, name, text)

    events = _parse(http, path, digest)

    assert events[0]["type"] == "meta"
    assert events[0]["format"] == name.rsplit(".", 1)[1]
    segments = [event for event in events if event["type"] == "segment"]
    assert [(item["line_start"], item["line_end"]) for item in segments] == [(1, 1), (3, 3)]
    assert segments[0]["char_start"] == 0
    assert segments[0]["char_end"] == len("第一行")
    assert segments[1]["char_start"] == len("第一行\n\n")
    assert segments[1]["char_end"] == len("第一行\n\nthird line")
    assert all("page" not in item["locator"] for item in segments)
    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "success"
    assert events[-1]["coverage"]["total_lines"] == 3


def test_docx_emits_paragraph_and_table_row_locators_without_pages(client):
    http, root = client
    path = root / "contract.docx"
    document = Document()
    document.add_paragraph("第一段")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "甲方"
    table.cell(0, 1).text = "乙方"
    document.save(path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    events = _parse(http, path, digest)

    segments = [event for event in events if event["type"] == "segment"]
    assert segments[0]["locator"] == {"paragraph": 1}
    assert segments[1]["locator"] == {"table": 1, "row": 1}
    assert segments[1]["text"] == "甲方 | 乙方"
    assert all("page" not in item["locator"] for item in segments)
    assert events[-1]["status"] == "success"


def test_pdf_uses_text_layer_and_ocr_per_page(client, monkeypatch):
    http, root = client
    path = root / "mixed.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "A sufficiently long native text layer")
    document.new_page()
    document.save(path)
    document.close()
    monkeypatch.setattr(parser_module, "_ocr_available", lambda: True)
    monkeypatch.setattr(parser_module, "_ocr_image_bytes", lambda image: "recognized scanned text")

    events = _parse(http, path, hashlib.sha256(path.read_bytes()).hexdigest())

    segments = [event for event in events if event["type"] == "segment"]
    assert len(segments) == 2
    assert segments[0]["page"] == 1
    assert segments[0]["extraction_method"] == "text_layer"
    assert segments[1]["page"] == 2
    assert segments[1]["extraction_method"] == "ocr"
    assert segments[1]["text"] == "recognized scanned text"
    done = events[-1]
    assert done["status"] == "success"
    assert done["coverage"]["text_layer_pages"] == 1
    assert done["coverage"]["ocr_pages"] == 1


def test_pdf_reports_ocr_unavailable_as_uncovered(client, monkeypatch):
    http, root = client
    path = root / "scan.pdf"
    document = fitz.open()
    document.new_page()
    document.save(path)
    document.close()
    monkeypatch.setattr(parser_module, "_ocr_available", lambda: False)

    events = _parse(http, path, hashlib.sha256(path.read_bytes()).hexdigest())

    segment = next(event for event in events if event["type"] == "segment")
    assert segment["status"] == "uncovered"
    assert segment["error"]["code"] == "ocr_unavailable"
    done = events[-1]
    assert done["status"] == "failed"
    assert done["coverage"]["ocr_uncovered"] == [{"page": 1, "reason": "ocr_unavailable"}]


def test_pdf_preserves_failed_page_and_continues(client, monkeypatch):
    http, root = client
    path = root / "failed-page.pdf"
    document = fitz.open()
    for text in ("First page has enough native text", "Second page has enough native text"):
        page = document.new_page()
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()
    original = parser_module._get_text_layer

    def fail_second_page(page):
        if page.number == 1:
            raise RuntimeError("sensitive native failure")
        return original(page)

    monkeypatch.setattr(parser_module, "_get_text_layer", fail_second_page)

    events = _parse(http, path, hashlib.sha256(path.read_bytes()).hexdigest())

    segments = [event for event in events if event["type"] == "segment"]
    assert segments[0]["status"] == "ok"
    assert segments[1]["status"] == "failed"
    assert segments[1]["page"] == 2
    assert segments[1]["error"]["code"] == "page_extract_failed"
    assert "sensitive" not in json.dumps(events, ensure_ascii=False)
    assert events[-1]["status"] == "partial"
    assert events[-1]["coverage"]["failed_pages"] == [2]


def test_pdf_ocr_page_limit_is_explicit(client, monkeypatch):
    http, root = client
    root.mkdir(exist_ok=True)
    monkeypatch.setenv("SLP_CAPABILITY_MAX_OCR_PAGES", "1")
    configured = TestClient(create_app())
    path = root / "two-scans.pdf"
    document = fitz.open()
    document.new_page()
    document.new_page()
    document.save(path)
    document.close()
    monkeypatch.setattr(parser_module, "_ocr_available", lambda: True)
    monkeypatch.setattr(parser_module, "_ocr_image_bytes", lambda image: "扫描文本")

    events = _parse(configured, path, hashlib.sha256(path.read_bytes()).hexdigest())

    segments = [event for event in events if event["type"] == "segment"]
    assert segments[0]["status"] == "ok"
    assert segments[1]["status"] == "uncovered"
    assert segments[1]["error"]["code"] == "ocr_page_limit"
    assert events[-1]["status"] == "partial"
    assert events[-1]["coverage"]["ocr_uncovered"] == [
        {"page": 2, "reason": "ocr_page_limit"}
    ]


def test_parse_rejects_path_escape_and_hash_mismatch(client, tmp_path):
    http, root = client
    outside, digest = _write(tmp_path, "outside.txt", "outside")
    events = _parse(http, outside, digest)
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] == "path_outside_root"
    assert str(outside) not in json.dumps(events)

    inside, _ = _write(root, "inside.txt", "inside")
    events = _parse(http, inside, "0" * 64)
    assert events[-1]["error"]["code"] == "hash_mismatch"


@pytest.mark.parametrize(
    "name,data",
    [
        ("fake.pdf", b"not a pdf"),
        ("fake.txt", b"%PDF-1.4\nbinary"),
        ("fake.docx", b"plain text"),
    ],
)
def test_parse_rejects_spoofed_extensions(client, name, data):
    http, root = client
    path, digest = _write(root, name, data)
    events = _parse(http, path, digest)
    assert events[-1]["type"] == "error"
    assert events[-1]["error"]["code"] in {"format_mismatch", "invalid_docx"}


def test_parse_rejects_encrypted_and_corrupt_pdf(client):
    http, root = client
    encrypted = root / "encrypted.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "encrypted content")
    document.save(encrypted, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="secret")
    document.close()
    events = _parse(http, encrypted, hashlib.sha256(encrypted.read_bytes()).hexdigest())
    assert events[-1]["error"]["code"] == "encrypted_pdf"

    corrupt, digest = _write(root, "corrupt.pdf", b"%PDF-1.7\ncorrupt")
    events = _parse(http, corrupt, digest)
    assert events[-1]["error"]["code"] == "invalid_pdf"


@pytest.mark.parametrize("name", ["empty.txt", "empty.md"])
def test_empty_text_material_is_failed_not_successful(client, name):
    http, root = client
    path, digest = _write(root, name, "\n\n")

    events = _parse(http, path, digest)

    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "failed"
    assert events[-1]["error"]["code"] == "no_text_extracted"


def test_empty_docx_is_failed_not_successful(client):
    http, root = client
    path = root / "empty.docx"
    Document().save(path)

    events = _parse(http, path, hashlib.sha256(path.read_bytes()).hexdigest())

    assert events[-1]["type"] == "done"
    assert events[-1]["status"] == "failed"
    assert events[-1]["error"]["code"] == "no_text_extracted"
