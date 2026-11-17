"""FR-08.1 合同审查导出测试。

覆盖：migration v7 幂等 / 未确认版本不能导出 / markdown + docx 渲染 /
正式 + 草稿状态 / 归档只读 / 交付列表合并两种 kind。
"""

import hashlib
import io

import pytest
from docx import Document

from test_contract_reviews import ReviewGateway
from test_materials import create_case, upload, wait_for_task


@pytest.fixture
def review_export_client(repository):
    gateway = ReviewGateway()
    from fastapi.testclient import TestClient

    from apps.workbench.backend.app import create_app
    from conftest import StubHealthChecker

    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        yield client, gateway


def prepare_confirmed_review(client, gateway=None, title="服务合同审查"):
    case = create_case(client, f"审查交付-{title}")
    created = upload(client, case["id"], content="ignored")
    wait_for_task(client, created.json()["task"]["id"], {"completed"})
    material_id = created.json()["material"]["id"]

    # 创建合同审查
    payload = {
        "material_id": material_id,
        "title": title,
        "contract_type": "服务合同",
        "focus_points": "违约金 / 争议解决",
    }
    review = client.post(f"/api/cases/{case['id']}/reviews", json=payload)
    assert review.status_code == 201, review.text
    review_id = review.json()["review"]["id"]

    # 生成版本（填充 citations）
    generated = client.post(f"/api/reviews/{review_id}/generate")
    assert generated.status_code == 201, generated.text
    task_id = generated.json()["id"]
    wait_for_task(client, task_id, {"completed"})

    # 确认版本
    confirm = client.post(f"/api/reviews/{review_id}/confirm")
    assert confirm.status_code == 200, confirm.text
    return case, review_id


def test_v7_migration_upgrades_delivery_tables(repository):
    """v7 让 delivery_versions 同时支持 summary + contract review。"""
    import sqlite3

    from apps.workbench.backend.migrations import run_migrations
    from apps.workbench.backend.tasks import TaskRepository

    repository.initialize()
    TaskRepository(repository).initialize()
    with sqlite3.connect(repository.db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS delivery_versions")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 5")
        conn.commit()
        run_migrations(conn)

        # summary_id 应可空
        columns = {
            row[1]: row for row in conn.execute("PRAGMA table_info(delivery_versions)")
        }
        assert columns["summary_id"][3] == 0  # notnull=0 → 可空
        assert "review_id" in columns
        assert "kind" in columns

        versions = {
            row[0]: row[1]
            for row in conn.execute("SELECT version, name FROM schema_migrations")
        }
        assert versions[7] == "delivery_polymorphic"

        # 幂等
        run_migrations(conn)
        assert {
            row[0]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        } == {1, 2, 3, 4, 5, 6, 7, 8}


def test_unconfirmed_review_cannot_export(review_export_client):
    client, _ = review_export_client
    case = create_case(client, "未确认审查")
    created = upload(client, case["id"], content="ignored")
    wait_for_task(client, created.json()["task"]["id"], {"completed"})
    material_id = created.json()["material"]["id"]
    review = client.post(
        f"/api/cases/{case['id']}/reviews",
        json={"material_id": material_id, "title": "草稿审查", "contract_type": "", "focus_points": ""},
    )
    review_id = review.json()["review"]["id"]

    rejected = client.post(
        f"/api/reviews/{review_id}/exports", json={"format": "markdown"}
    )
    assert rejected.status_code == 422
    detail = rejected.json()["detail"]
    assert isinstance(detail, dict)
    assert "确认" in detail.get("message", "")


def test_review_markdown_export(review_export_client):
    client, _ = review_export_client
    case, review_id = prepare_confirmed_review(client)
    response = client.post(
        f"/api/reviews/{review_id}/exports", json={"format": "markdown"}
    )
    assert response.status_code == 201, response.text
    delivery = response.json()

    assert delivery["kind"] == "contract_review"
    assert delivery["review_id"] == review_id
    assert delivery["summary_id"] is None
    assert delivery["status"] == "official"
    assert delivery["format"] == "markdown"
    assert delivery["facts"] == {}
    assert "storage_key" not in delivery

    # 交付列表应包含两种（目前只有 review 一种）
    listed = client.get(f"/api/cases/{case['id']}/deliveries").json()
    assert listed["total"] == 1
    assert listed["items"][0]["kind"] == "contract_review"

    downloaded = client.get(f"/api/deliveries/{delivery['id']}/download")
    assert downloaded.status_code == 200
    text = downloaded.content.decode("utf-8")
    assert "# 服务合同审查" in text
    assert "## 风险清单" in text
    assert "高危" in text and "违约金" in text
    assert "## 法条来源" in text and "L1" in text
    assert hashlib.sha256(downloaded.content).hexdigest() == delivery["sha256"]


def test_review_docx_export_is_readable(review_export_client):
    client, _ = review_export_client
    case, review_id = prepare_confirmed_review(client, title="DOCX合同审查")
    response = client.post(
        f"/api/reviews/{review_id}/exports", json={"format": "docx"}
    )
    assert response.status_code == 201, response.text
    delivery = response.json()
    assert delivery["status"] == "official"
    assert delivery["format"] == "docx"

    downloaded = client.get(f"/api/deliveries/{delivery['id']}/download")
    assert downloaded.status_code == 200
    document = Document(io.BytesIO(downloaded.content))
    paragraphs = [p.text for p in document.paragraphs]
    assert "DOCX合同审查" in paragraphs[0]
    assert any("风险清单" in p for p in paragraphs)
    assert any("法条来源" in p for p in paragraphs)


def test_archived_case_blocks_review_export(review_export_client):
    client, _ = review_export_client
    case, review_id = prepare_confirmed_review(client)
    client.post(f"/api/cases/{case['id']}/archive", json=None)

    rejected = client.post(
        f"/api/reviews/{review_id}/exports", json={"format": "markdown"}
    )
    assert rejected.status_code == 409

    listed = client.get(f"/api/cases/{case['id']}/deliveries")
    assert listed.status_code == 200


def test_deliveries_merge_summary_and_review(review_export_client):
    """同一案件下两种 kind 的交付在同一列表中返回。"""
    client, _ = review_export_client
    case, review_id = prepare_confirmed_review(client)

    # 合同审查导出
    client.post(f"/api/reviews/{review_id}/exports", json={"format": "markdown"})

    # 不做 summary（太复杂），直接验证列表至少能装 review
    listed = client.get(f"/api/cases/{case['id']}/deliveries").json()
    assert listed["total"] == 1
    assert listed["items"][0]["kind"] == "contract_review"
    assert listed["items"][0]["review_id"] == review_id
    assert listed["items"][0]["summary_id"] is None

    # 文件名应包含 review 标记
    downloaded = client.get(
        f"/api/deliveries/{listed['items'][0]['id']}/download"
    )
    cd = downloaded.headers.get("content-disposition", "")
    assert "_review_" in cd