import asyncio
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from conftest import StubHealthChecker


def wait_for_status(client, task_id, expected, timeout=4):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        response = client.get(f"/api/tasks/{task_id}")
        assert response.status_code == 200
        latest = response.json()
        if latest["status"] in expected:
            return latest
        time.sleep(0.01)
    pytest.fail(f"任务未在预期时间内进入 {expected}，最后状态：{latest}")


def create_case(client, name="任务中心测试案件"):
    response = client.post("/api/cases", json={"name": name})
    assert response.status_code == 201
    return response.json()


def create_demo_task(client, case_id, outcome="completed", delay_ms=1):
    return client.post(
        "/api/demo/tasks",
        json={"case_id": case_id, "outcome": outcome, "delay_ms": delay_ms},
    )


@pytest.fixture
def demo_client(repository):
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            enable_task_demo=True,
        )
    ) as test_client:
        yield test_client


def test_task_center_is_empty_and_has_no_general_create_api(client):
    response = client.get("/api/tasks")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "demo_enabled": False}
    assert client.post("/api/tasks", json={}).status_code == 405
    assert client.post("/api/demo/tasks", json={}).status_code == 404


@pytest.mark.parametrize(
    ("outcome", "expected_status", "expected_progress"),
    [
        ("completed", "completed", 100),
        ("partial", "partial", 50),
        ("failed", "failed", None),
    ],
)
def test_demo_task_terminal_states(demo_client, outcome, expected_status, expected_progress):
    case = create_case(demo_client)
    created = create_demo_task(demo_client, case["id"], outcome=outcome)
    assert created.status_code == 201
    task = created.json()
    assert task["case_id"] == case["id"]
    assert task["case_name"] == case["name"]
    assert task["task_type"] == "demo"
    assert task["attempt_number"] == 1

    finished = wait_for_status(demo_client, task["id"], {expected_status})
    assert finished["progress"] == expected_progress
    assert finished["started_at"] is not None
    assert finished["finished_at"] is not None

    detail = demo_client.get(f"/api/tasks/{task['id']}").json()
    assert len(detail["attempts"]) == 1
    assert detail["attempts"][0]["status"] == expected_status
    if outcome == "failed":
        assert "演示" in detail["error"]
    if outcome == "partial":
        assert detail["result"]["items"] == ["已保留的部分结果"]

    assert demo_client.get("/api/tasks").json()["total"] == 1
    assert demo_client.get(f"/api/tasks?case_id={case['id']}").json()["total"] == 1
    assert demo_client.get("/api/tasks?case_id=missing").json()["total"] == 0


def test_cancel_is_cooperative_and_retry_preserves_history(demo_client):
    case = create_case(demo_client)
    created = create_demo_task(demo_client, case["id"], outcome="cancellable", delay_ms=20)
    task_id = created.json()["id"]
    running = wait_for_status(demo_client, task_id, {"running"})
    assert running["progress"] is None

    cancelled_response = demo_client.post(f"/api/tasks/{task_id}/cancel")
    assert cancelled_response.status_code == 200
    assert cancelled_response.json()["cancel_requested"] is True
    wait_for_status(demo_client, task_id, {"cancelled"})

    retried = demo_client.post(
        f"/api/tasks/{task_id}/retry",
        json={"scope": {"outcome": "completed", "delay_ms": 1}},
    )
    assert retried.status_code == 200
    assert retried.json()["attempt_number"] == 2
    completed = wait_for_status(demo_client, task_id, {"completed"})
    assert completed["error"] is None

    detail = demo_client.get(f"/api/tasks/{task_id}").json()
    assert [attempt["status"] for attempt in detail["attempts"]] == ["cancelled", "completed"]
    assert detail["attempts"][0]["input_snapshot"]["outcome"] == "cancellable"
    assert detail["attempts"][1]["input_snapshot"]["scope"] == {
        "outcome": "completed",
        "delay_ms": 1,
    }


def test_retry_defaults_to_original_input_snapshot(demo_client):
    case = create_case(demo_client)
    task_id = create_demo_task(demo_client, case["id"], outcome="failed").json()["id"]
    failed = wait_for_status(demo_client, task_id, {"failed"})
    original_error = failed["error"]

    assert demo_client.post(f"/api/tasks/{task_id}/retry", json={}).status_code == 200
    wait_for_status(demo_client, task_id, {"failed"})
    detail = demo_client.get(f"/api/tasks/{task_id}").json()
    assert len(detail["attempts"]) == 2
    assert detail["attempts"][0]["input_snapshot"] == detail["attempts"][1]["input_snapshot"]
    assert detail["attempts"][0]["error"] == original_error
    assert detail["attempts"][1]["error"] == original_error


def test_archived_case_cannot_create_or_retry_and_invalid_actions_return_409(demo_client):
    case = create_case(demo_client)
    task_id = create_demo_task(demo_client, case["id"], outcome="failed").json()["id"]
    wait_for_status(demo_client, task_id, {"failed"})
    assert demo_client.post(f"/api/tasks/{task_id}/cancel").status_code == 409
    assert demo_client.post(f"/api/cases/{case['id']}/archive").status_code == 200
    assert create_demo_task(demo_client, case["id"]).status_code == 409
    assert demo_client.post(f"/api/tasks/{task_id}/retry", json={}).status_code == 409

    missing_id = "00000000-0000-0000-0000-000000000000"
    assert demo_client.get(f"/api/tasks/{missing_id}").status_code == 404
    assert demo_client.post(f"/api/tasks/{missing_id}/cancel").status_code == 404
    assert demo_client.post(f"/api/tasks/{missing_id}/retry", json={}).status_code == 404


def test_restart_marks_unknown_work_recovery_pending_and_allows_new_attempt(repository):
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            enable_task_demo=True,
        )
    ) as first_client:
        case = create_case(first_client)
        task_id = create_demo_task(
            first_client, case["id"], outcome="cancellable", delay_ms=100
        ).json()["id"]
        wait_for_status(first_client, task_id, {"running"})

    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            enable_task_demo=True,
        )
    ) as second_client:
        recovered = second_client.get(f"/api/tasks/{task_id}").json()
        assert recovered["status"] == "recovery_pending"
        assert recovered["progress"] is None
        assert recovered["attempts"][0]["status"] == "recovery_pending"

        retried = second_client.post(
            f"/api/tasks/{task_id}/retry",
            json={"scope": {"outcome": "completed", "delay_ms": 1}},
        )
        assert retried.status_code == 200
        wait_for_status(second_client, task_id, {"completed"})
        detail = second_client.get(f"/api/tasks/{task_id}").json()
        assert [attempt["status"] for attempt in detail["attempts"]] == [
            "recovery_pending",
            "completed",
        ]


def test_demo_task_validation(demo_client):
    assert create_demo_task(demo_client, "missing").status_code == 404
    case = create_case(demo_client)
    assert create_demo_task(demo_client, case["id"], outcome="unknown").status_code == 422
    assert create_demo_task(demo_client, case["id"], delay_ms=-1).status_code == 422


def test_fr05_schema_is_upgraded_with_idempotency_columns(repository):
    from apps.workbench.backend.migrations import run_migrations
    from apps.workbench.backend.tasks import TaskRepository

    with sqlite3.connect(repository.db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY, case_id TEXT NOT NULL, task_type TEXT NOT NULL,
                title TEXT NOT NULL, status TEXT NOT NULL, current_stage TEXT NOT NULL,
                progress INTEGER, progress_detail TEXT NOT NULL, model_role TEXT,
                input_snapshot TEXT NOT NULL, result TEXT, error TEXT,
                active_attempt_id TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, started_at TEXT, finished_at TEXT
            );
            """
        )
        run_migrations(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tasks)")}
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations")]
    assert {"client_request_id", "request_hash"} <= columns
    assert versions == sorted(versions)
    assert TaskRepository(repository).db_path == repository.db_path


def test_task_creation_idempotency_returns_existing_and_rejects_changed_request(repository):
    from apps.workbench.backend.task_service import TaskService
    from apps.workbench.backend.tasks import (
        TaskIdempotencyConflictError,
        TaskOutcome,
        TaskRepository,
    )

    repository.initialize()
    task_repository = TaskRepository(repository)
    task_repository.initialize()
    case = repository.create("幂等测试")

    async def scenario():
        service = TaskService(task_repository)

        async def handler(context, input_snapshot):
            return TaskOutcome(current_stage="已完成", progress_detail="完成")

        service.register_handler("search", handler)
        first = await service.create_task(
            case_id=case.id,
            task_type="search",
            title="统一检索",
            input_snapshot={"question": "合同效力"},
            client_request_id="request-1",
        )
        second = await service.create_task(
            case_id=case.id,
            task_type="search",
            title="统一检索",
            input_snapshot={"question": "合同效力"},
            client_request_id="request-1",
        )
        assert second.id == first.id
        with pytest.raises(TaskIdempotencyConflictError):
            await service.create_task(
                case_id=case.id,
                task_type="search",
                title="统一检索",
                input_snapshot={"question": "不同问题"},
                client_request_id="request-1",
            )
        await service.shutdown()

    asyncio.run(scenario())
    assert len(task_repository.list(case_id=case.id)) == 1


def test_report_partial_persists_before_cancellation_checkpoint(repository):
    from apps.workbench.backend.task_service import TaskCancelled, TaskExecutionContext

    class FakeRepository:
        def __init__(self):
            self.saved = None

        def report(self, task_id, attempt_id, **values):
            self.saved = (task_id, attempt_id, values)

        def is_cancellation_requested(self, task_id, attempt_id):
            return True

    fake = FakeRepository()
    context = TaskExecutionContext(fake, "task-1", "attempt-1")
    with pytest.raises(TaskCancelled):
        asyncio.run(context.report_partial(
            current_stage="已保存部分结果",
            progress_detail="一段已保存",
            result={"segments": 1},
        ))
    assert fake.saved[2]["result"] == {"segments": 1}
