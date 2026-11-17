import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from apps.workbench.backend.integrations import CapabilityError
from conftest import StubHealthChecker
from test_materials import create_case, upload, wait_for_task
from test_searches import SearchGateway, create_search, finished_attempt


class SummaryGateway(SearchGateway):
    def __init__(self):
        super().__init__()
        self.summary_calls = []
        self.summary_outcomes = ["success"]

    async def generate_summary(self, payload):
        self.summary_calls.append(payload)
        outcome = self.summary_outcomes.pop(0) if len(self.summary_outcomes) > 1 else self.summary_outcomes[0]
        if outcome == "slow":
            await asyncio.sleep(0.2)
            outcome = "success"
        if outcome == "failed":
            raise CapabilityError("真实模型暂不可用，未生成摘要")
        handles = [source["handle"] for source in payload["sources"]]
        provider = payload["provider"]
        return {
            "body": "研究摘要草稿\n" + "\n".join(f"来源摘录 [[{handle}]]" for handle in handles),
            "used_handles": handles,
            "provider": provider,
            "actual_model": "test-model" if provider == "openai-compatible" else None,
            "warnings": ["仅供人工复核"],
        }


@pytest.fixture
def summary_client(repository):
    gateway = SummaryGateway()
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        yield client, gateway


def add_material(client, case_id):
    created = upload(client, case_id).json()
    wait_for_task(client, created["task"]["id"], {"completed"})
    material_id = created["material"]["id"]
    detail = client.get(f"/api/materials/{material_id}").json()
    version_id = detail["material"]["current_version_id"]
    segments = client.get(
        f"/api/materials/{material_id}/versions/{version_id}/segments"
    ).json()["items"]
    return {"kind": "material_segment", "id": segments[0]["id"]}


def add_evidence(client, case_id):
    task_id = create_search(client, case_id, question="法条依据", mode="law").json()["id"]
    _, view = finished_attempt(client, task_id)
    item = view["evidence"]["law"][0]
    return {"kind": "evidence", "id": item["id"]}, view["attempt_id"]


def create_summary(client, case_id, sources, title="合同效力研究"):
    response = client.post(
        f"/api/cases/{case_id}/summaries",
        json={"title": title, "question": "合同是否有效", "sources": sources},
    )
    assert response.status_code == 201, response.text
    return response.json()


def save_version(client, summary_id, body, base_version_id=None):
    return client.post(
        f"/api/summaries/{summary_id}/versions",
        json={"body": body, "base_version_id": base_version_id},
    )


def test_v3_schema_is_upgraded_with_summary_tables(repository):
    from apps.workbench.backend.migrations import run_migrations
    from apps.workbench.backend.tasks import TaskRepository

    repository.initialize()
    TaskRepository(repository).initialize()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("DROP TABLE research_summary_versions")
        connection.execute("DROP TABLE research_summaries")
        connection.execute("DELETE FROM schema_migrations WHERE version = 4")
        connection.execute(
            "INSERT INTO cases(id, name, normalized_name, created_at, updated_at) "
            "VALUES ('existing-case', '既有案件', '既有案件', '2026-01-01', '2026-01-01')"
        )
        run_migrations(connection)
        summary_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(research_summaries)")
        }
        version_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(research_summary_versions)")
        }
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 4"
        ).fetchone()
        existing = connection.execute(
            "SELECT name FROM cases WHERE id = 'existing-case'"
        ).fetchone()
        run_migrations(connection)
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 4"
        ).fetchone()[0]

    assert migration[0] == "research_summaries"
    assert migration_count == 1
    assert existing[0] == "既有案件"
    assert {"current_sources", "next_source_number", "latest_task_id"} <= summary_columns
    assert {"citations", "generator_provider", "actual_model", "coverage"} <= version_columns


def test_sources_are_stable_across_attempts_and_invalid_drafts_remain_editable(summary_client):
    client, _ = summary_client
    case = create_case(client)
    other = create_case(client, "其他案件")
    material = add_material(client, case["id"])
    evidence_one, _ = add_evidence(client, case["id"])
    detail = create_summary(client, case["id"], [evidence_one, material])
    summary_id = detail["summary"]["id"]
    assert [source["handle"] for source in detail["sources"]] == ["S1", "S2"]
    assert detail["issues"][0]["code"] == "empty_summary"

    cross_case = client.post(
        f"/api/cases/{other['id']}/summaries",
        json={"title": "跨案件", "sources": [evidence_one]},
    )
    assert cross_case.status_code == 422

    evidence_two, attempt_two = add_evidence(client, case["id"])
    updated = client.put(
        f"/api/summaries/{summary_id}/sources",
        json={"add": [evidence_two], "remove": []},
    )
    assert updated.status_code == 200
    assert [source["handle"] for source in updated.json()["sources"]] == ["S1", "S2", "S3"]
    assert attempt_two in updated.json()["coverage"]["attempt_ids"]

    first = save_version(client, summary_id, "第一版 [[S1]] 与 [[S2]]")
    assert first.status_code == 201, first.text
    first_version = first.json()["current_version"]
    blocked = client.put(
        f"/api/summaries/{summary_id}/sources",
        json={"remove": ["S1"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["affected"][0]["handle"] == "S1"
    assert len(client.get(f"/api/summaries/{summary_id}").json()["sources"]) == 3

    removed = client.put(
        f"/api/summaries/{summary_id}/sources",
        json={"remove": ["S1"], "confirm_remove": True},
    )
    assert removed.status_code == 200
    assert removed.json()["issues"][0]["code"] == "unselected_citation"
    second = save_version(
        client,
        summary_id,
        "移除后仍保留的无效草稿 [[S1]]，以及 [[S2]]",
        first_version["id"],
    )
    assert second.status_code == 201
    assert second.json()["issues"][0]["code"] == "unselected_citation"
    assert second.json()["current_version"]["citations"]["S1"]["text"]
    historical = next(
        version for version in second.json()["versions"] if version["id"] == first_version["id"]
    )
    assert historical["body"] == "第一版 [[S1]] 与 [[S2]]"
    assert historical["citations"]["S1"]["text"]

    unknown = save_version(
        client,
        summary_id,
        "伪造引用 [[S99]]",
        second.json()["current_version"]["id"],
    )
    assert unknown.status_code == 201
    assert unknown.json()["issues"][0]["code"] == "unknown_citation"
    rejected = client.post(f"/api/summaries/{summary_id}/confirm")
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["issues"][0]["code"] == "unknown_citation"

    corrected = save_version(
        client,
        summary_id,
        "已修正并人工核对 [[S2]]",
        unknown.json()["current_version"]["id"],
    )
    confirmed = client.post(f"/api/summaries/{summary_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    confirmed_version = confirmed.json()["current_version"]
    assert confirmed_version["status"] == "confirmed"
    assert confirmed_version["confirmed_at"]
    edited = save_version(
        client,
        summary_id,
        "确认后的补充说明 [[S2]]",
        confirmed_version["id"],
    )
    assert edited.json()["current_version"]["status"] == "edited"
    assert edited.json()["current_version"]["base_version_id"] == confirmed_version["id"]
    old = next(
        version for version in edited.json()["versions"] if version["id"] == confirmed_version["id"]
    )
    assert old["status"] == "confirmed"


def test_blank_summary_and_archived_case_rules(summary_client):
    client, _ = summary_client
    case = create_case(client)
    blank = create_summary(client, case["id"], [], title="空白研究")
    summary_id = blank["summary"]["id"]
    assert client.post(f"/api/summaries/{summary_id}/generate").status_code == 422
    saved = save_version(client, summary_id, "")
    assert saved.status_code == 201
    rejected = client.post(f"/api/summaries/{summary_id}/confirm")
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["issues"][0]["code"] == "empty_summary"

    assert client.post(f"/api/cases/{case['id']}/archive").status_code == 200
    assert client.get(f"/api/summaries/{summary_id}").status_code == 200
    assert save_version(client, summary_id, "只读").status_code == 409
    assert client.put(
        f"/api/summaries/{summary_id}/sources", json={"remove": []}
    ).status_code == 409
    assert client.post(f"/api/summaries/{summary_id}/generate").status_code == 409
    assert client.post(f"/api/summaries/{summary_id}/confirm").status_code == 409
    archived_create = client.post(
        f"/api/cases/{case['id']}/summaries", json={"title": "只读", "sources": []}
    )
    assert archived_create.status_code == 409


def test_generate_idempotency_versions_failure_and_retry_preserve_work(summary_client):
    client, gateway = summary_client
    case = create_case(client)
    material = add_material(client, case["id"])
    evidence_one, attempt_one = add_evidence(client, case["id"])
    evidence_two, attempt_two = add_evidence(client, case["id"])
    detail = create_summary(client, case["id"], [evidence_one, material])
    summary_id = detail["summary"]["id"]
    client.put(
        f"/api/summaries/{summary_id}/sources", json={"add": [evidence_two]}
    )
    manual = save_version(client, summary_id, "人工正文 [[S1]]").json()
    manual_id = manual["current_version"]["id"]

    first = client.post(
        f"/api/summaries/{summary_id}/generate",
        headers={"Idempotency-Key": "generate-1"},
    )
    assert first.status_code == 201, first.text
    task_id = first.json()["id"]
    repeated = client.post(
        f"/api/summaries/{summary_id}/generate",
        headers={"Idempotency-Key": "generate-1"},
    )
    assert repeated.json()["id"] == task_id
    wait_for_task(client, task_id, {"completed"})
    generated = client.get(f"/api/summaries/{summary_id}").json()
    assert len(generated["versions"]) == 2
    assert generated["current_version"]["status"] == "draft"
    assert generated["current_version"]["base_version_id"] == manual_id
    assert generated["current_version"]["generator_provider"] == "builtin-extractive"
    assert generated["current_version"]["actual_model"] is None
    assert set(generated["current_version"]["coverage"]["attempt_ids"]) == {
        attempt_one,
        attempt_two,
    }
    assert len(gateway.summary_calls) == 1

    second = client.post(f"/api/summaries/{summary_id}/generate").json()
    wait_for_task(client, second["id"], {"completed"})
    before_failure = client.get(f"/api/summaries/{summary_id}").json()
    assert len(before_failure["versions"]) == 3

    gateway.summary_outcomes = ["failed"]
    failed = client.post(f"/api/summaries/{summary_id}/generate").json()
    failed_task_id = failed["id"]
    wait_for_task(client, failed_task_id, {"failed"})
    after_failure = client.get(f"/api/summaries/{summary_id}").json()
    assert after_failure["summary"]["current_version_id"] == before_failure["summary"]["current_version_id"]
    assert after_failure["sources"] == before_failure["sources"]
    assert after_failure["versions"] == before_failure["versions"]
    rejected_scope = client.post(
        f"/api/tasks/{failed_task_id}/retry", json={"scope": {"question": "篡改"}}
    )
    assert rejected_scope.status_code == 422

    gateway.summary_outcomes = ["success"]
    retried = client.post(f"/api/tasks/{failed_task_id}/retry", json={})
    assert retried.status_code == 200
    wait_for_task(client, failed_task_id, {"completed"})
    task_detail = client.get(f"/api/tasks/{failed_task_id}").json()
    assert [attempt["status"] for attempt in task_detail["attempts"]] == ["failed", "completed"]
    assert task_detail["attempts"][0]["error"]
    after_retry = client.get(f"/api/summaries/{summary_id}").json()
    assert len(after_retry["versions"]) == 4
    assert after_retry["current_version"]["attempt_id"] == task_detail["attempts"][1]["id"]


def test_cancel_and_restart_do_not_save_unknown_generation(repository):
    gateway = SummaryGateway()
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        case = create_case(client)
        evidence, _ = add_evidence(client, case["id"])
        summary = create_summary(client, case["id"], [evidence])
        summary_id = summary["summary"]["id"]

        gateway.summary_outcomes = ["slow"]
        cancelled_task = client.post(f"/api/summaries/{summary_id}/generate").json()["id"]
        wait_for_task(client, cancelled_task, {"running"})
        assert client.post(f"/api/tasks/{cancelled_task}/cancel").status_code == 200
        wait_for_task(client, cancelled_task, {"cancelled"})
        assert client.get(f"/api/summaries/{summary_id}").json()["versions"] == []

        gateway.summary_outcomes = ["success"]
        client.post(f"/api/tasks/{cancelled_task}/retry", json={})
        wait_for_task(client, cancelled_task, {"completed"})
        assert len(client.get(f"/api/summaries/{summary_id}").json()["versions"]) == 1

        gateway.summary_outcomes = ["slow"]
        interrupted_task = client.post(f"/api/summaries/{summary_id}/generate").json()["id"]
        wait_for_task(client, interrupted_task, {"running"})
        before_restart = client.get(f"/api/summaries/{summary_id}").json()

    gateway.summary_outcomes = ["success"]
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as restarted:
        recovered = restarted.get(f"/api/tasks/{interrupted_task}").json()
        assert recovered["status"] == "recovery_pending"
        current = restarted.get(f"/api/summaries/{summary_id}").json()
        assert current["versions"] == before_restart["versions"]
        assert restarted.post(f"/api/tasks/{interrupted_task}/retry", json={}).status_code == 200
        wait_for_task(restarted, interrupted_task, {"completed"})
        detail = restarted.get(f"/api/tasks/{interrupted_task}").json()
        assert [attempt["status"] for attempt in detail["attempts"]] == [
            "recovery_pending",
            "completed",
        ]
        assert len(restarted.get(f"/api/summaries/{summary_id}").json()["versions"]) == 2
