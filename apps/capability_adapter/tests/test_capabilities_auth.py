import json

from fastapi.testclient import TestClient

from apps.capability_adapter.app import create_app


class EmptyRetriever:
    def search_and_rerank(self, query, top_k=8):
        return [], {}


def test_capabilities_exposes_central_limits_without_loading_retriever(tmp_path, monkeypatch):
    monkeypatch.setenv("SLP_WORKBENCH_MATERIAL_ROOT", str(tmp_path))
    monkeypatch.setenv("SLP_CAPABILITY_MAX_FILE_BYTES", "1234")
    monkeypatch.setenv("SLP_CAPABILITY_MAX_FILES_PER_CASE", "7")
    monkeypatch.setenv("SLP_CAPABILITY_MAX_OCR_PAGES", "9")
    monkeypatch.setenv("SLP_CAPABILITY_MAX_TEXT_CHARS", "4567")
    monkeypatch.setenv("SLP_JUDGMENT_DB_DIR", str(tmp_path / "judgments"))
    client = TestClient(create_app(retriever=EmptyRetriever()))

    response = client.get("/v1/capabilities")

    assert response.status_code == 200
    body = response.json()
    assert body["version"]
    assert body["parser"]["formats"] == ["pdf", "docx", "txt", "md"]
    assert body["parser"]["version"]
    assert isinstance(body["parser"]["ocr_available"], bool)
    assert body["parser"]["limits"] == {
        "max_file_bytes": 1234,
        "max_files_per_case": 7,
        "max_ocr_pages": 9,
        "max_text_chars": 4567,
    }
    assert {source["id"] for source in body["sources"]} == {
        "law",
        "guiding_cases",
        "reference",
        "judgments",
    }


def test_internal_token_protects_every_business_route_but_not_capabilities(tmp_path, monkeypatch):
    monkeypatch.setenv("SLP_CAPABILITY_ADAPTER_TOKEN", "internal-secret")
    monkeypatch.setenv("SLP_WORKBENCH_MATERIAL_ROOT", str(tmp_path))
    client = TestClient(create_app(retriever=EmptyRetriever()))

    assert client.get("/v1/capabilities").status_code == 200
    unauthorized = client.post("/v1/search/law", json={"query": "测试"})
    assert unauthorized.status_code == 401
    assert "internal-secret" not in unauthorized.text
    assert client.post(
        "/v1/parse",
        json={"material_id": "m", "parse_version_id": "v", "path": "/secret/a.txt", "sha256": "0" * 64},
    ).status_code == 401
    assert client.post("/v1/search/judgments", json={"query": "测试"}).status_code == 401
    assert client.post("/v1/judgments/source", json={"year": 2020, "rowid": 1}).status_code == 401

    authorized = client.post(
        "/v1/search/law",
        json={"query": "测试"},
        headers={"X-SLP-Internal-Token": "internal-secret"},
    )
    assert authorized.status_code == 200


def test_validation_errors_do_not_echo_sensitive_input(tmp_path, monkeypatch):
    monkeypatch.setenv("SLP_CAPABILITY_ADAPTER_TOKEN", "internal-secret")
    client = TestClient(create_app(retriever=EmptyRetriever()), raise_server_exceptions=False)
    response = client.post(
        "/v1/judgments/source",
        json={"year": "../../private", "rowid": "1 OR 1=1"},
        headers={"X-SLP-Internal-Token": "internal-secret"},
    )
    assert response.status_code == 422
    assert "../../private" not in response.text
    assert "1 OR 1=1" not in response.text
    assert "internal-secret" not in response.text
