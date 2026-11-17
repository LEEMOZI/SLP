import asyncio
import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from conftest import StubHealthChecker


class FakeGateway:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or ["success"])
        self.calls = []

    async def capabilities(self):
        return {
            "version": "test",
            "parser": {
                "formats": ["pdf", "docx", "txt", "md"],
                "version": "test-parser",
                "ocr_available": True,
                "limits": {
                    "max_file_bytes": 20 * 1024 * 1024,
                    "max_files_per_case": 100,
                    "max_ocr_pages": 20,
                    "max_text_chars": 500000,
                },
            },
            "sources": [],
        }

    async def parse_events(self, payload):
        self.calls.append(payload)
        outcome = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        ids = {
            "material_id": payload["material_id"],
            "parse_version_id": payload["parse_version_id"],
        }
        yield {**ids, "type": "meta", "parser_version": "test-parser", "locator_type": "text_line"}
        if outcome == "error":
            yield {
                **ids,
                "type": "error",
                "status": "failed",
                "error": {"code": "invalid_text", "message": "文本无效"},
            }
            return
        yield {
            **ids,
            "type": "segment",
            "index": 1,
            "text": "第一段",
            "status": "ok",
            "locator": {"line_start": 1, "line_end": 1, "char_start": 0, "char_end": 3},
            "extraction_method": "text_line",
        }
        if outcome == "cancel":
            await asyncio.sleep(0.2)
        if outcome == "stream_error":
            raise RuntimeError("sensitive socket detail")
        partial = outcome == "partial"
        yield {
            **ids,
            "type": "done",
            "status": "partial" if partial else "success",
            "parser_version": "test-parser",
            "segment_count": 1,
            "char_count": 3,
            "coverage": {"total_lines": 2, "parsed_lines": 1, "text_truncated": partial},
        }

    async def close(self):
        pass


def wait_for_task(client, task_id, expected, timeout=4):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = client.get(f"/api/tasks/{task_id}").json()
        if latest["status"] in expected:
            return latest
        time.sleep(0.01)
    pytest.fail(f"任务未进入 {expected}：{latest}")


def create_case(client, name="材料案件"):
    response = client.post("/api/cases", json={"name": name})
    assert response.status_code == 201
    return response.json()


def upload(client, case_id, content="第一行\n第二行", name="notes.txt", **kwargs):
    return client.post(
        f"/api/cases/{case_id}/materials",
        files={"file": (name, content.encode("utf-8"), "text/plain")},
        **kwargs,
    )


@pytest.fixture
def material_client(repository):
    gateway = FakeGateway()
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as test_client:
        yield test_client, gateway


def test_upload_persists_immutable_original_parse_version_and_real_locator(material_client):
    client, gateway = material_client
    case = create_case(client)
    content = "第一行\n第二行"
    response = upload(client, case["id"], content)
    assert response.status_code == 201, response.text
    body = response.json()
    material = body["material"]
    task = body["task"]
    assert material["sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert material["latest_task_id"] == task["id"]
    assert "storage_key" not in material
    assert "path" not in material

    finished = wait_for_task(client, task["id"], {"completed"})
    assert finished["result"]["segment_count"] == 1
    detail = client.get(f"/api/materials/{material['id']}").json()
    assert detail["material"]["status"] == "success"
    assert detail["material"]["current_version_id"] == detail["versions"][0]["id"]
    version = detail["versions"][0]
    assert version["attempt_id"] == finished["attempts"][0]["id"]
    assert version["locator_type"] == "text_line"

    segments = client.get(
        f"/api/materials/{material['id']}/versions/{version['id']}/segments"
    ).json()
    assert segments["items"][0]["locator"]["line_start"] == 1
    assert segments["items"][0]["locator"]["char_start"] == 0
    assert segments["next_cursor"] is None
    original = client.get(f"/api/materials/{material['id']}/original")
    assert original.content == content.encode()
    assert original.headers["x-content-type-options"] == "nosniff"
    assert "attachment" in original.headers["content-disposition"]
    assert gateway.calls[0]["sha256"] == material["sha256"]


def test_duplicate_requires_confirmation_and_idempotency_rejects_changed_file(material_client):
    client, _ = material_client
    case = create_case(client)
    first = upload(client, case["id"], headers={"Idempotency-Key": "upload-1"})
    assert first.status_code == 201
    repeated = upload(client, case["id"], headers={"Idempotency-Key": "upload-1"})
    assert repeated.status_code == 201
    assert repeated.json()["material"]["id"] == first.json()["material"]["id"]
    assert repeated.json()["task"]["id"] == first.json()["task"]["id"]

    conflict = upload(
        client,
        case["id"],
        content="不同文件",
        headers={"Idempotency-Key": "upload-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"

    duplicate = upload(client, case["id"])
    assert duplicate.status_code == 409
    detail = duplicate.json()["detail"]
    assert detail["code"] == "duplicate_material"
    assert detail["duplicates"][0]["id"] == first.json()["material"]["id"]
    confirmed = upload(client, case["id"], data={"confirm_duplicate": "true"})
    assert confirmed.status_code == 201
    assert confirmed.json()["material"]["id"] != first.json()["material"]["id"]
    wait_for_task(client, first.json()["task"]["id"], {"completed"})
    wait_for_task(client, confirmed.json()["task"]["id"], {"completed"})
    assert client.get(f"/api/cases/{case['id']}/materials").json()["total"] == 2


def test_partial_parse_retry_creates_new_version_without_overwriting_old_segments(repository):
    gateway = FakeGateway(["partial", "success"])
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        case = create_case(client)
        created = upload(client, case["id"]).json()
        task_id = created["task"]["id"]
        wait_for_task(client, task_id, {"partial"})
        before = client.get(f"/api/materials/{created['material']['id']}").json()
        assert before["versions"][0]["status"] == "partial"
        old_version = before["versions"][0]["id"]

        retried = client.post(f"/api/tasks/{task_id}/retry", json={})
        assert retried.status_code == 200
        wait_for_task(client, task_id, {"completed"})
        after = client.get(f"/api/materials/{created['material']['id']}").json()
        assert len(after["versions"]) == 2
        assert after["material"]["current_version_id"] != old_version
        old_segments = client.get(
            f"/api/materials/{created['material']['id']}/versions/{old_version}/segments"
        )
        assert old_segments.status_code == 200
        assert old_segments.json()["items"][0]["text"] == "第一段"
        assert len(gateway.calls) == 2


def test_parse_failure_is_explicit_and_does_not_leak_gateway_details(repository):
    gateway = FakeGateway(["stream_error"])
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        case = create_case(client)
        created = upload(client, case["id"]).json()
        failed = wait_for_task(client, created["task"]["id"], {"partial"})
        assert "sensitive" not in failed["error"]
        detail = client.get(f"/api/materials/{created['material']['id']}").json()
        assert detail["versions"][0]["status"] == "failed"
        assert detail["versions"][0]["error"]["code"] == "parse_failed"
        assert detail["versions"][0]["error"]["message"]


def test_cancel_preserves_already_persisted_segments(repository):
    gateway = FakeGateway(["cancel"])
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        case = create_case(client)
        created = upload(client, case["id"]).json()
        material_id = created["material"]["id"]
        task_id = created["task"]["id"]
        deadline = time.monotonic() + 2
        version = None
        while time.monotonic() < deadline:
            detail = client.get(f"/api/materials/{material_id}").json()
            if detail["versions"] and detail["versions"][0]["segment_count"] == 1:
                version = detail["versions"][0]
                break
            time.sleep(0.01)
        assert version is not None
        assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
        wait_for_task(client, task_id, {"partial", "cancelled"})
        segments = client.get(
            f"/api/materials/{material_id}/versions/{version['id']}/segments"
        ).json()
        assert len(segments["items"]) == 1


def test_upload_limits_archived_access_and_cross_case_isolation(repository, monkeypatch):
    monkeypatch.setenv("SLP_CAPABILITY_MAX_FILE_BYTES", "8")
    monkeypatch.setenv("SLP_CAPABILITY_MAX_FILES_PER_CASE", "1")
    gateway = FakeGateway()
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        case = create_case(client)
        other = create_case(client, "另一案件")
        assert upload(client, case["id"], name="notes.doc").status_code == 415
        assert upload(client, case["id"], content="123456789").status_code == 413
        created = upload(client, case["id"], content="1234").json()
        material_id = created["material"]["id"]
        wait_for_task(client, created["task"]["id"], {"completed"})
        assert upload(client, case["id"], content="abc").status_code == 409
        assert client.get(f"/api/cases/{other['id']}/materials").json()["total"] == 0
        assert client.post(f"/api/cases/{case['id']}/archive").status_code == 200
        assert upload(client, case["id"], data={"confirm_duplicate": "true"}).status_code == 409
        assert client.get(f"/api/materials/{material_id}").status_code == 200
        assert client.get(f"/api/materials/{material_id}/original").status_code == 200
        assert client.post(f"/api/tasks/{created['task']['id']}/retry", json={}).status_code == 409


def test_capabilities_are_proxied_without_internal_configuration(material_client):
    client, _ = material_client
    response = client.get("/api/capabilities")
    assert response.status_code == 200
    assert response.json()["parser"]["limits"]["max_file_bytes"] == 20 * 1024 * 1024
    assert "token" not in response.text.lower()
    assert "material_root" not in response.text
