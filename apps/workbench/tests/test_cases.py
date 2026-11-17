from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from conftest import StubHealthChecker


def test_case_lifecycle(client):
    assert client.get("/api/cases").json() == {"items": [], "total": 0}

    created_response = client.post("/api/cases", json={"name": "  某合同纠纷一审  "})
    assert created_response.status_code == 201
    created = created_response.json()
    assert created["name"] == "某合同纠纷一审"
    assert created["last_opened_at"] is None
    assert created["archived_at"] is None

    listed = client.get("/api/cases").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == created["id"]

    opened_response = client.post(f"/api/cases/{created['id']}/open")
    assert opened_response.status_code == 200
    assert opened_response.json()["last_opened_at"] is not None

    archived_response = client.post(f"/api/cases/{created['id']}/archive")
    assert archived_response.status_code == 200
    archived_at = archived_response.json()["archived_at"]
    assert archived_at is not None
    assert client.get("/api/cases").json()["total"] == 0
    assert client.get("/api/cases?include_archived=true").json()["total"] == 1
    assert client.post(f"/api/cases/{created['id']}/open").status_code == 409

    archived_again = client.post(f"/api/cases/{created['id']}/archive")
    assert archived_again.status_code == 200
    assert archived_again.json()["archived_at"] == archived_at


def test_case_name_is_normalized_and_unique(client):
    assert client.post("/api/cases", json={"name": "Alpha Case"}).status_code == 201
    duplicate = client.post("/api/cases", json={"name": "  alpha   case  "})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "同名案件已存在，请使用其他名称"


def test_case_name_validation(client):
    assert client.post("/api/cases", json={"name": "   "}).status_code == 422
    assert client.post("/api/cases", json={"name": "a" * 161}).status_code == 422
    assert client.post("/api/cases", json={}).status_code == 422


def test_missing_case_returns_404(client):
    missing_id = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/api/cases/{missing_id}").status_code == 404
    assert client.post(f"/api/cases/{missing_id}/open").status_code == 404
    assert client.post(f"/api/cases/{missing_id}/archive").status_code == 404


def test_cases_persist_across_app_restarts(repository):
    with TestClient(create_app(repository=repository, health_checker=StubHealthChecker())) as first_client:
        created = first_client.post("/api/cases", json={"name": "持久化验证案件"})
        assert created.status_code == 201

    with TestClient(create_app(repository=repository, health_checker=StubHealthChecker())) as second_client:
        result = second_client.get("/api/cases")
        assert result.status_code == 200
        assert result.json()["total"] == 1
        assert result.json()["items"][0]["name"] == "持久化验证案件"
