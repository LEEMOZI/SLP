"""FR-07 诉讼文书起草后端测试。

覆盖：migration 幂等 / Repository CRUD / validate_version /
confirm / generate task / 归档只读 / retry。
"""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from apps.workbench.backend.integrations import CapabilityError
from conftest import StubHealthChecker
from test_materials import create_case, wait_for_task


# ---- Stub sidecar gateway ------------------------------------------------


class LitigationGateway:
    """完整 stub：材料解析 + 诉讼文书生成。"""

    def __init__(self):
        self.parse_calls = []
        self.litigation_calls = []
        self.litigation_outcomes = ["success"]

    async def capabilities(self):
        return {
            "version": "test",
            "parser": {
                "formats": ["txt", "md"],
                "version": "test-parser",
                "ocr_available": False,
                "limits": {"max_file_bytes": 10 * 1024 * 1024},
            },
            "capabilities": ["litigation"],
        }

    async def parse_events(self, payload):
        self.parse_calls.append(payload)
        ids = {
            "material_id": payload["material_id"],
            "parse_version_id": payload["parse_version_id"],
        }
        yield {**ids, "type": "meta", "parser_version": "test-parser", "locator_type": "text_line"}
        yield {
            **ids,
            "type": "segment",
            "index": 1,
            "text": "原告张三诉被告李四民间借贷纠纷...",
            "status": "ok",
            "locator": {"line_start": 1, "line_end": 1},
            "extraction_method": "text_line",
        }
        yield {
            **ids,
            "type": "done",
            "status": "success",
            "parser_version": "test-parser",
            "segment_count": 1,
            "char_count": 20,
            "coverage": {"total_lines": 1, "parsed_lines": 1},
        }

    async def generate_summary(self, payload):
        raise NotImplementedError

    async def search(self, source, payload):
        raise NotImplementedError

    async def judgment_source(self, year, rowid):
        raise NotImplementedError

    async def generate_review(self, payload):
        raise NotImplementedError

    async def generate_litigation(self, payload):
        self.litigation_calls.append(payload)
        outcome = (
            self.litigation_outcomes.pop(0)
            if len(self.litigation_outcomes) > 1
            else self.litigation_outcomes[0]
        )
        if outcome == "failed":
            raise CapabilityError("真实模型暂不可用")
        if outcome == "slow":
            await asyncio.sleep(0.2)
            outcome = "success"
        return {
            "ok": True,
            "document": (
                "民事起诉状\n"
                "原告：张三\n"
                "被告：李四\n"
                "诉讼请求：\n"
                "一、判令被告偿还借款本金 100,000 元；\n"
                "二、判令被告支付逾期利息；\n"
                "事实与理由：\n"
                "2024 年 3 月 1 日，被告向原告借款 100,000 元，约定 2024 年 9 月 1 日归还。\n"
                "依据《中华人民共和国民法典》第六百七十五条 [[L1]]，借款人应当按照约定的期限返还借款。\n"
                "依据《中华人民共和国民法典》第六百七十六条 [[L2]]，借款人未按照约定的期限返还借款的，应当按照约定或者国家有关规定支付逾期利息。\n"
            ),
            "citations": {
                "L1": {
                    "handle": "L1",
                    "title": "民法典",
                    "law_name": "中华人民共和国民法典",
                    "article": 675,
                    "law_type": "法律",
                    "text": "借款人应当按照约定的期限返还借款...",
                    "source_id": "law-civil-code-675",
                    "source_label": "中华人民共和国民法典 第675条",
                    "case_id": "",
                    "kind": "law",
                },
                "L2": {
                    "handle": "L2",
                    "title": "民法典",
                    "law_name": "中华人民共和国民法典",
                    "article": 676,
                    "law_type": "法律",
                    "text": "借款人未按照约定的期限返还借款的，应当按照约定或者国家有关规定支付逾期利息...",
                    "source_id": "law-civil-code-676",
                    "source_label": "中华人民共和国民法典 第676条",
                    "case_id": "",
                    "kind": "law",
                },
            },
            "citation_count": 2,
            "evidence_count": 2,
            "query": "诉讼文书起草",
            "truncated": False,
        }

    async def close(self):
        pass


@pytest.fixture
def litigation_client(repository):
    gateway = LitigationGateway()
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        yield client, gateway


# ---- helpers ---------------------------------------------------------------


def create_litigation_doc(client, case_id, **overrides):
    payload = {
        "doc_type": "complaint",
        "title": "民间借贷纠纷起诉状",
        "parties": "原告：张三；被告：李四",
        "claims": "判令被告偿还借款本金 100,000 元及利息",
        "facts": "2024 年 3 月 1 日被告向原告借款 100,000 元",
        "focus_points": "借款事实、还款期限",
    }
    payload.update(overrides)
    response = client.post(f"/api/cases/{case_id}/litigation-docs", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def save_version(client, doc_id, body, base_version_id=None):
    return client.post(
        f"/api/litigation-docs/{doc_id}/versions",
        json={"body": body, "base_version_id": base_version_id},
    )


# ============================================================================
# Migration 幂等
# ============================================================================


def test_v8_schema_is_upgraded_with_litigation_tables(repository):
    from apps.workbench.backend.migrations import run_migrations
    from apps.workbench.backend.tasks import TaskRepository

    repository.initialize()
    TaskRepository(repository).initialize()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("DROP TABLE IF EXISTS litigation_document_versions")
        connection.execute("DROP TABLE IF EXISTS litigation_documents")
        connection.execute("DELETE FROM schema_migrations WHERE version = 8")
        connection.execute(
            "INSERT INTO cases(id, name, normalized_name, created_at, updated_at) "
            "VALUES ('existing-case', '既有案件', '既有案件', '2026-01-01', '2026-01-01')"
        )
        run_migrations(connection)
        doc_cols = {
            row[1]
            for row in connection.execute("PRAGMA table_info(litigation_documents)")
        }
        version_cols = {
            row[1]
            for row in connection.execute("PRAGMA table_info(litigation_document_versions)")
        }
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 8"
        ).fetchone()
        run_migrations(connection)
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 8"
        ).fetchone()[0]

    assert migration[0] == "litigation_documents"
    assert migration_count == 1
    assert {
        "case_id",
        "doc_type",
        "title",
        "parties",
        "claims",
        "facts",
        "focus_points",
        "current_version_id",
        "current_version_number",
        "current_sources",
        "latest_task_id",
    } <= doc_cols
    assert {
        "body",
        "citations",
        "task_id",
        "attempt_id",
        "generator_provider",
        "actual_model",
        "coverage",
        "confirmed_at",
    } <= version_cols


# ============================================================================
# Repository CRUD
# ============================================================================


def test_repository_create_and_list_and_detail(litigation_client):
    client, _ = litigation_client
    case = create_case(client, "诉讼文书案件")

    # 创建
    detail = create_litigation_doc(client, case["id"])
    doc = detail["document"]
    assert doc["doc_type"] == "complaint"
    assert doc["title"] == "民间借贷纠纷起诉状"
    assert doc["parties"] == "原告：张三；被告：李四"
    assert detail["versions"] == []
    assert detail["current_version"] is None

    # list
    listed = client.get(f"/api/cases/{case['id']}/litigation-docs").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == doc["id"]

    # get
    fetched = client.get(f"/api/litigation-docs/{doc['id']}").json()
    assert fetched["document"]["id"] == doc["id"]
    assert fetched["sources"] == []

    # 详情 on non-existent → 404
    missed = client.get("/api/litigation-docs/nope-nope")
    assert missed.status_code == 404


def test_create_with_invalid_doc_type_rejected(litigation_client):
    client, _ = litigation_client
    case = create_case(client)
    response = client.post(
        f"/api/cases/{case['id']}/litigation-docs",
        json={"doc_type": "invalid_type", "title": "测试"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"]["code"] == "litigation_validation_failed"


def test_save_manual_version_and_validate_version_issues(litigation_client):
    client, _ = litigation_client
    case = create_case(client)
    detail = create_litigation_doc(client, case["id"])
    doc_id = detail["document"]["id"]

    # 空 body → empty_litigation
    empty = save_version(client, doc_id, "")
    assert empty.status_code == 201, empty.text
    assert [i["code"] for i in empty.json()["issues"]] == ["empty_document"]

    # 有 body 但无 citation → no_citation
    no_cite = save_version(client, doc_id, "原告请求法院判令被告还款。")
    assert no_cite.status_code == 201
    assert [i["code"] for i in no_cite.json()["issues"]] == ["no_citation"]

    # 伪造引用 → unknown_citation
    fake = save_version(
        client, doc_id, "依据 [[L999]] 原告主张；[[L888]] 被告抗辩"
    )
    assert fake.status_code == 201
    fake_issues = fake.json()["issues"]
    assert len(fake_issues) == 2
    handles = {i["handle"] for i in fake_issues}
    assert handles == {"L999", "L888"}

    # 版本号递增
    assert fake.json()["current_version"]["version_number"] == 3
    # 历史版本不可变
    first_version = next(
        v for v in fake.json()["versions"]
        if v["version_number"] == 1
    )
    assert first_version["body"] == ""
    assert first_version["status"] == "edited"  # 手动保存固定 edited


def test_confirm_rejects_on_unknown_citation_and_accepts_valid(litigation_client):
    client, _ = litigation_client
    case = create_case(client)
    detail = create_litigation_doc(client, case["id"])
    doc_id = detail["document"]["id"]

    # 先 generate 填充 citations（L1/L2）
    gen_task_id = client.post(f"/api/litigation-docs/{doc_id}/generate").json()["id"]
    wait_for_task(client, gen_task_id, {"completed"})

    # 伪造引用 → confirm 被拒绝
    save_version(
        client, doc_id, "依据 [[L99]] 原告主张；[[L1]] 被告抗辩"
    )
    rejected = client.post(f"/api/litigation-docs/{doc_id}/confirm")
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "litigation_validation_failed"
    assert rejected.json()["detail"]["issues"][0]["code"] == "unknown_citation"
    assert rejected.json()["detail"]["issues"][0]["handle"] == "L99"

    # 修正为有效引用 → confirm 成功
    corrected = save_version(
        client, doc_id, "依据 [[L1]] 原告主张；[[L2]] 被告抗辩"
    ).json()
    corrected_id = corrected["current_version"]["id"]
    assert corrected["issues"] == []
    confirmed = client.post(f"/api/litigation-docs/{doc_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["current_version"]["status"] == "confirmed"
    assert confirmed.json()["current_version"]["confirmed_at"]

    # 确认后的新版本 base 指向 confirmed
    edited = save_version(
        client, doc_id, "确认后的补充说明 [[L1]]",
        base_version_id=corrected_id,
    ).json()
    assert edited["current_version"]["status"] == "edited"
    assert edited["current_version"]["base_version_id"] == corrected_id


def test_generate_creates_task_and_saved_version(litigation_client):
    client, gateway = litigation_client
    case = create_case(client)
    detail = create_litigation_doc(client, case["id"])
    doc_id = detail["document"]["id"]

    # generate
    task_response = client.post(f"/api/litigation-docs/{doc_id}/generate")
    assert task_response.status_code == 201, task_response.text
    task_id = task_response.json()["id"]
    wait_for_task(client, task_id, {"completed"})

    # 确认版本已保存
    after = client.get(f"/api/litigation-docs/{doc_id}").json()
    versions = after["versions"]
    assert len(versions) == 1
    generated = versions[0]
    assert generated["status"] == "draft"
    assert generated["generator_provider"] == "openai-compatible"
    assert generated["task_id"] == task_id
    assert generated["attempt_id"] is not None

    # citations 已填充 LawSource（有 law_name / article）
    assert len(generated["citations"]) == 2
    assert "law_name" in generated["citations"]["L1"]
    assert generated["citations"]["L1"]["article"] == 675

    # body 里有 [[L1]] [[L2]] — 所以没有 validate 问题
    assert after["issues"] == []

    # 可以 confirm
    confirmed = client.post(f"/api/litigation-docs/{doc_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["current_version"]["status"] == "confirmed"

    # 再次 generate 追加新版本
    second_task = client.post(f"/api/litigation-docs/{doc_id}/generate")
    wait_for_task(client, second_task.json()["id"], {"completed"})
    final = client.get(f"/api/litigation-docs/{doc_id}").json()
    assert len(final["versions"]) == 2
    assert final["current_version"]["version_number"] == 2
    # 历史 confirmed 版本不可变
    first_confirmed = next(
        v for v in final["versions"] if v["version_number"] == 1
    )
    assert first_confirmed["status"] == "confirmed"

    # gateway 被调用两次
    assert len(gateway.litigation_calls) == 2


def test_generate_failure_preserves_prior_versions(litigation_client):
    client, gateway = litigation_client
    case = create_case(client)
    detail = create_litigation_doc(client, case["id"])
    doc_id = detail["document"]["id"]

    # 一次成功
    first = client.post(f"/api/litigation-docs/{doc_id}/generate").json()["id"]
    wait_for_task(client, first, {"completed"})
    before = client.get(f"/api/litigation-docs/{doc_id}").json()
    before_version_id = before["current_version"]["id"]
    before_count = len(before["versions"])

    # 一次失败
    gateway.litigation_outcomes = ["failed"]
    failed = client.post(f"/api/litigation-docs/{doc_id}/generate").json()
    failed_id = failed["id"]
    wait_for_task(client, failed_id, {"failed"})
    after_failure = client.get(f"/api/litigation-docs/{doc_id}").json()
    assert len(after_failure["versions"]) == before_count
    assert after_failure["current_version"]["id"] == before_version_id


def test_archive_case_makes_litigation_docs_readonly(litigation_client):
    client, gateway = litigation_client
    case = create_case(client)
    detail = create_litigation_doc(client, case["id"])
    doc_id = detail["document"]["id"]

    # generate 一个版本
    task_id = client.post(f"/api/litigation-docs/{doc_id}/generate").json()["id"]
    wait_for_task(client, task_id, {"completed"})

    # 归档案件
    assert client.post(f"/api/cases/{case['id']}/archive").status_code == 200

    # 读取仍可用
    assert client.get(f"/api/litigation-docs/{doc_id}").status_code == 200
    assert client.get(f"/api/cases/{case['id']}/litigation-docs").status_code == 200

    # 写操作全部 409
    assert save_version(client, doc_id, "只读修改").status_code == 409
    assert client.post(f"/api/litigation-docs/{doc_id}/confirm").status_code == 409
    assert client.post(f"/api/litigation-docs/{doc_id}/generate").status_code == 409
    assert client.post(
        f"/api/cases/{case['id']}/litigation-docs",
        json={"doc_type": "defense", "title": "新文书"},
    ).status_code == 409


def test_retry_generation_succeeds_after_failure(litigation_client):
    client, gateway = litigation_client
    case = create_case(client)
    detail = create_litigation_doc(client, case["id"])
    doc_id = detail["document"]["id"]

    # 失败
    gateway.litigation_outcomes = ["failed"]
    failed_task_id = client.post(f"/api/litigation-docs/{doc_id}/generate").json()["id"]
    wait_for_task(client, failed_task_id, {"failed"})

    # 重试成功
    gateway.litigation_outcomes = ["success"]
    retried = client.post(f"/api/tasks/{failed_task_id}/retry", json={})
    assert retried.status_code == 200, retried.text
    wait_for_task(client, failed_task_id, {"completed"})

    task_detail = client.get(f"/api/tasks/{failed_task_id}").json()
    statuses = [a["status"] for a in task_detail["attempts"]]
    assert "failed" in statuses
    assert "completed" in statuses

    after = client.get(f"/api/litigation-docs/{doc_id}").json()
    assert len(after["versions"]) == 1
    assert after["current_version"]["attempt_id"] == task_detail["attempts"][1]["id"]
