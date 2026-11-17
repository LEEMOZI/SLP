"""FR-08 合同审查后端测试。

覆盖：migration 幂等 / 纯函数（parse_risks_from_body、check_citations）/
Repository CRUD / validate_version 三级 issue / confirm / generate task /
归档只读。
"""

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from apps.workbench.backend.integrations import CapabilityError
from apps.workbench.backend.contract_reviews import (
    check_citations,
    parse_risks_from_body,
)
from conftest import StubHealthChecker
from test_materials import create_case, upload, wait_for_task


# ---- Stub sidecar gateway ------------------------------------------------


class ReviewGateway:
    """完整 stub：材料解析（复用 FakeGateway 实现）+ 合同审查。"""

    def __init__(self):
        self.parse_calls = []
        self.review_calls = []
        self.review_outcomes = ["success"]

    async def capabilities(self):
        return {
            "version": "test",
            "parser": {
                "formats": ["txt", "md"],
                "version": "test-parser",
                "ocr_available": False,
                "limits": {"max_file_bytes": 10 * 1024 * 1024},
            },
            "capabilities": ["review"],
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
            "text": "双方约定违约金 30%，仲裁条款...",
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
        self.review_calls.append(payload)
        outcome = (
            self.review_outcomes.pop(0)
            if len(self.review_outcomes) > 1
            else self.review_outcomes[0]
        )
        if outcome == "failed":
            raise CapabilityError("真实模型暂不可用")
        if outcome == "slow":
            await asyncio.sleep(0.2)
            outcome = "success"
        return {
            "ok": True,
            "report": (
                "一、合同效力综述\n"
                "本合同主体资格完备，形式符合法律规定 [[L1]]。\n"
                "二、风险清单\n"
                "**高危风险**\n"
                "- 风险条款：第 12 条违约金比例过高；风险说明：违约金约定为合同总额 30%，显著超过实际损失；"
                "修改建议：建议调整为实际损失的 1.3 倍以内；法律依据：《民法典》第五百八十五条 [[L1]]\n"
                "**中等风险**\n"
                "- 风险条款：第 8 条争议解决仅约定仲裁；风险说明：未约定诉讼管辖；"
                "修改建议：建议补充约定；法律依据：《民事诉讼法》第三十四条 [[L2]]\n"
                "**提示风险**\n"
                "- 风险条款：未约定通知送达地址；风险说明：通知可能无法送达；"
                "修改建议：建议补充各方送达地址；法律依据：《民法典》第一百三十七条 [[L3]]\n"
            ),
            "risks": [
                {
                    "level": "高危",
                    "excerpt": "第 12 条违约金比例过高",
                    "risk_issue": "违约金约定为合同总额 30%，显著超过实际损失",
                    "suggestion": "建议调整为实际损失的 1.3 倍以内",
                    "legal_basis": "《民法典》第五百八十五条 [[L1]]",
                },
                {
                    "level": "中等",
                    "excerpt": "第 8 条争议解决仅约定仲裁",
                    "risk_issue": "未约定诉讼管辖",
                    "suggestion": "建议补充约定",
                    "legal_basis": "《民事诉讼法》第三十四条 [[L2]]",
                },
                {
                    "level": "提示",
                    "excerpt": "未约定通知送达地址",
                    "risk_issue": "通知可能无法送达",
                    "suggestion": "建议补充各方送达地址",
                    "legal_basis": "《民法典》第一百三十七条 [[L3]]",
                },
            ],
            "citations": {
                "L1": {
                    "handle": "L1",
                    "title": "民法典",
                    "law_name": "中华人民共和国民法典",
                    "article": 585,
                    "law_type": "法律",
                    "text": "当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金...",
                    "source_id": "law-civil-code-585",
                    "source_label": "中华人民共和国民法典 第585条",
                    "case_id": "",
                    "kind": "law",
                },
                "L2": {
                    "handle": "L2",
                    "title": "民事诉讼法",
                    "law_name": "中华人民共和国民事诉讼法",
                    "article": 34,
                    "law_type": "法律",
                    "text": "合同或者其他财产权益纠纷的当事人可以书面协议选择被告住所地...",
                    "source_id": "law-civil-proc-34",
                    "source_label": "中华人民共和国民事诉讼法 第34条",
                    "case_id": "",
                    "kind": "law",
                },
                "L3": {
                    "handle": "L3",
                    "title": "民法典",
                    "law_name": "中华人民共和国民法典",
                    "article": 137,
                    "law_type": "法律",
                    "text": "以对话方式作出的意思表示，相对人知道其内容时生效...",
                    "source_id": "law-civil-code-137",
                    "source_label": "中华人民共和国民法典 第137条",
                    "case_id": "",
                    "kind": "law",
                },
            },
            "citation_count": 3,
            "evidence_count": 3,
            "query": "合同文本审查",
            "truncated": False,
        }

    async def close(self):
        pass


@pytest.fixture
def review_client(repository):
    gateway = ReviewGateway()
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        yield client, gateway


# ---- helpers ---------------------------------------------------------------


def add_material(client, case_id):
    """上传 txt 材料并等待解析完成；返回 material_id。"""
    response = upload(client, case_id, content="甲方：张三\n乙方：李四\n合同条款...")
    assert response.status_code == 201, response.text
    created = response.json()
    task_id = created["task"]["id"]
    wait_for_task(client, task_id, {"completed"})
    return created["material"]["id"]


def create_review(client, case_id, material_id, **overrides):
    payload = {
        "material_id": material_id,
        "title": "采购合同审查",
        "contract_type": "采购合同",
        "focus_points": "违约金、争议解决",
    }
    payload.update(overrides)
    response = client.post(f"/api/cases/{case_id}/reviews", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def save_version(client, review_id, body, base_version_id=None):
    return client.post(
        f"/api/reviews/{review_id}/versions",
        json={"body": body, "base_version_id": base_version_id},
    )


# ============================================================================
# Migration 幂等
# ============================================================================


def test_v6_schema_is_upgraded_with_contract_review_tables(repository):
    from apps.workbench.backend.migrations import run_migrations
    from apps.workbench.backend.tasks import TaskRepository

    repository.initialize()
    TaskRepository(repository).initialize()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("DROP TABLE IF EXISTS contract_review_versions")
        connection.execute("DROP TABLE IF EXISTS contract_reviews")
        connection.execute("DELETE FROM schema_migrations WHERE version = 6")
        connection.execute(
            "INSERT INTO cases(id, name, normalized_name, created_at, updated_at) "
            "VALUES ('existing-case', '既有案件', '既有案件', '2026-01-01', '2026-01-01')"
        )
        run_migrations(connection)
        review_cols = {
            row[1]
            for row in connection.execute("PRAGMA table_info(contract_reviews)")
        }
        version_cols = {
            row[1]
            for row in connection.execute("PRAGMA table_info(contract_review_versions)")
        }
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 6"
        ).fetchone()
        run_migrations(connection)
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 6"
        ).fetchone()[0]

    assert migration[0] == "contract_reviews"
    assert migration_count == 1
    assert {
        "material_id",
        "contract_type",
        "focus_points",
        "current_sources",
        "current_version_number",
        "latest_task_id",
    } <= review_cols
    assert {
        "risks",
        "citations",
        "task_id",
        "attempt_id",
        "generator_provider",
        "actual_model",
        "coverage",
        "confirmed_at",
    } <= version_cols


# ============================================================================
# 纯函数：风险解析 + 法条引用校验
# ============================================================================


def test_parse_risks_from_body_extracts_three_levels():
    body = (
        "二、风险清单\n"
        "**高危风险**\n"
        "- 风险条款：违约金比例过高；风险说明：30% 过高；"
        "修改建议：降到 1.3 倍；法律依据：《民法典》第 585 条\n"
        "**中等风险**\n"
        "- 风险条款：未约定仲裁机构；风险说明：可能无效；"
        "修改建议：补充仲裁委；法律依据：《仲裁法》第 16 条\n"
        "**提示风险**\n"
        "- 风险条款：未约定送达地址\n"
    )
    risks = parse_risks_from_body(body)
    assert len(risks) >= 1
    # 所有条目都有 level
    assert all(r["level"] in ("高危", "中等", "提示") for r in risks)
    levels_found = {r["level"] for r in risks}
    assert "高危" in levels_found
    # excerpt 不为空
    for risk in risks:
        assert risk["excerpt"]

    # 空 body → 空列表
    assert parse_risks_from_body("") == []
    assert parse_risks_from_body(None) == []
    # 只有标题无条目 → 空列表
    assert parse_risks_from_body("二、风险清单\n**高危风险**") == []


def test_check_citations_warns_unknown_laws_and_articles():
    # 引用了已知法律和条号 → 无 warning
    body = "本合同依据《中华人民共和国民法典》第五百八十五条 [[L1]]"
    evidence = [
        {"law_name": "中华人民共和国民法典", "article": 585},
    ]
    warnings = check_citations(body, evidence)
    assert len(warnings) == 0

    # 引用了已知法律但未知条号
    body2 = "依据《中华人民共和国民法典》第四百条"
    warnings2 = check_citations(body2, evidence)
    assert any("第400条" in w for w in warnings2)

    # 引用了未知法律
    body3 = "依据《中华人民共和国合同法》第五十二条"
    warnings3 = check_citations(body3, evidence)
    assert any("合同法" in w for w in warnings3)

    # 空 body
    assert check_citations("", []) == ["合同审查报告为空，未做法条引用校验。"]


# ============================================================================
# Repository CRUD + validate_version
# ============================================================================


def test_repository_create_and_list_and_detail(review_client):
    client, _ = review_client
    case = create_case(client, "合同审查案件")
    material_id = add_material(client, case["id"])

    # 创建
    detail = create_review(client, case["id"], material_id)
    review = detail["review"]
    assert review["material_id"] == material_id
    assert review["title"] == "采购合同审查"
    assert review["contract_type"] == "采购合同"
    assert detail["versions"] == []
    assert detail["current_version"] is None

    # list
    listed = client.get(f"/api/cases/{case['id']}/reviews").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == review["id"]

    # get
    fetched = client.get(f"/api/reviews/{review['id']}").json()
    assert fetched["review"]["id"] == review["id"]
    assert fetched["sources"] == []

    # 详情 on non-existent → 404
    missed = client.get("/api/reviews/nope-nope")
    assert missed.status_code == 404


def test_create_without_parsed_material_rejected(review_client):
    client, _ = review_client
    case = create_case(client)
    # 故意传入不存在的 material_id
    response = client.post(
        f"/api/cases/{case['id']}/reviews",
        json={
            "material_id": "no-such-material",
            "title": "审查",
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body["detail"]["code"] == "contract_review_validation_failed"


def test_save_manual_version_and_validate_version_issues(review_client):
    client, _ = review_client
    case = create_case(client)
    material_id = add_material(client, case["id"])
    detail = create_review(client, case["id"], material_id)
    review_id = detail["review"]["id"]

    # 空 body → empty_review
    empty = save_version(client, review_id, "")
    assert empty.status_code == 201, empty.text
    assert [i["code"] for i in empty.json()["issues"]] == ["empty_review"]

    # 有 body 但无 citation → no_citation
    no_cite = save_version(client, review_id, "本合同主体合格，条款清晰。")
    assert no_cite.status_code == 201
    assert [i["code"] for i in no_cite.json()["issues"]] == ["no_citation"]

    # 伪造引用 → unknown_citation
    fake = save_version(
        client, review_id, "本合同依据 [[L999]] 有效，违约金条款 [[L888]]"
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


def test_confirm_rejects_on_unknown_citation_and_accepts_valid(review_client):
    client, _ = review_client
    case = create_case(client)
    material_id = add_material(client, case["id"])
    detail = create_review(client, case["id"], material_id)
    review_id = detail["review"]["id"]

    # 先 generate 填充 citations（L1/L2/L3）
    gen_task_id = client.post(f"/api/reviews/{review_id}/generate").json()["id"]
    wait_for_task(client, gen_task_id, {"completed"})

    # 伪造引用 → confirm 被拒绝
    save_version(
        client, review_id, "依据 [[L99]] 本合同有效；[[L1]] 违约金"
    )
    rejected = client.post(f"/api/reviews/{review_id}/confirm")
    assert rejected.status_code == 422
    assert rejected.json()["detail"]["code"] == "contract_review_validation_failed"
    assert rejected.json()["detail"]["issues"][0]["code"] == "unknown_citation"
    assert rejected.json()["detail"]["issues"][0]["handle"] == "L99"

    # 修正为有效引用 → confirm 成功
    corrected = save_version(
        client, review_id, "依据 [[L1]] 本合同违约金条款有效；[[L2]] 争议解决"
    ).json()
    corrected_id = corrected["current_version"]["id"]
    assert corrected["issues"] == []
    confirmed = client.post(f"/api/reviews/{review_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["current_version"]["status"] == "confirmed"
    assert confirmed.json()["current_version"]["confirmed_at"]

    # 确认后的新版本 base 指向 confirmed
    edited = save_version(
        client, review_id, "确认后的补充说明 [[L1]]",
        base_version_id=corrected_id,
    ).json()
    assert edited["current_version"]["status"] == "edited"
    assert edited["current_version"]["base_version_id"] == corrected_id


def test_generate_creates_task_and_saved_version(review_client):
    client, gateway = review_client
    case = create_case(client)
    material_id = add_material(client, case["id"])
    detail = create_review(client, case["id"], material_id)
    review_id = detail["review"]["id"]

    # generate
    task_response = client.post(f"/api/reviews/{review_id}/generate")
    assert task_response.status_code == 201, task_response.text
    task_id = task_response.json()["id"]
    wait_for_task(client, task_id, {"completed"})

    # 确认版本已保存
    after = client.get(f"/api/reviews/{review_id}").json()
    versions = after["versions"]
    assert len(versions) == 1
    generated = versions[0]
    assert generated["status"] == "draft"
    assert generated["generator_provider"] == "openai-compatible"
    assert generated["task_id"] == task_id
    assert generated["attempt_id"] is not None

    # 风险解析正确
    assert len(generated["risks"]) == 3
    levels = {r["level"] for r in generated["risks"]}
    assert levels == {"高危", "中等", "提示"}

    # citations 已填充 LawSource（有 law_name / article）
    assert len(generated["citations"]) == 3
    assert "law_name" in generated["citations"]["L1"]
    assert generated["citations"]["L1"]["article"] == 585

    # body 里有 [[L1]] [[L2]] [[L3]] — 所以没有 validate 问题
    assert after["issues"] == []

    # 可以 confirm
    confirmed = client.post(f"/api/reviews/{review_id}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["current_version"]["status"] == "confirmed"

    # 再次 generate 追加新版本
    second_task = client.post(f"/api/reviews/{review_id}/generate")
    wait_for_task(client, second_task.json()["id"], {"completed"})
    final = client.get(f"/api/reviews/{review_id}").json()
    assert len(final["versions"]) == 2
    assert final["current_version"]["version_number"] == 2
    # 历史 confirmed 版本不可变
    first_confirmed = next(
        v for v in final["versions"] if v["version_number"] == 1
    )
    assert first_confirmed["status"] == "confirmed"

    # gateway 被调用两次
    assert len(gateway.review_calls) == 2


def test_generate_failure_preserves_prior_versions(review_client):
    client, gateway = review_client
    case = create_case(client)
    material_id = add_material(client, case["id"])
    detail = create_review(client, case["id"], material_id)
    review_id = detail["review"]["id"]

    # 一次成功
    first = client.post(f"/api/reviews/{review_id}/generate").json()["id"]
    wait_for_task(client, first, {"completed"})
    before = client.get(f"/api/reviews/{review_id}").json()
    before_version_id = before["current_version"]["id"]
    before_count = len(before["versions"])

    # 一次失败
    gateway.review_outcomes = ["failed"]
    failed = client.post(f"/api/reviews/{review_id}/generate").json()
    failed_id = failed["id"]
    wait_for_task(client, failed_id, {"failed"})
    after_failure = client.get(f"/api/reviews/{review_id}").json()
    assert len(after_failure["versions"]) == before_count
    assert after_failure["current_version"]["id"] == before_version_id


def test_archive_case_makes_reviews_readonly(review_client):
    client, gateway = review_client
    case = create_case(client)
    material_id = add_material(client, case["id"])
    detail = create_review(client, case["id"], material_id)
    review_id = detail["review"]["id"]

    # generate 一个版本
    task_id = client.post(f"/api/reviews/{review_id}/generate").json()["id"]
    wait_for_task(client, task_id, {"completed"})

    # 归档案件
    assert client.post(f"/api/cases/{case['id']}/archive").status_code == 200

    # 读取仍可用
    assert client.get(f"/api/reviews/{review_id}").status_code == 200
    assert client.get(f"/api/cases/{case['id']}/reviews").status_code == 200

    # 写操作全部 409
    assert save_version(client, review_id, "只读修改").status_code == 409
    assert client.post(f"/api/reviews/{review_id}/confirm").status_code == 409
    assert client.post(f"/api/reviews/{review_id}/generate").status_code == 409
    assert client.post(
        f"/api/cases/{case['id']}/reviews", json={"material_id": material_id, "title": "新审查"}
    ).status_code == 409


def test_retry_generation_succeeds_after_failure(review_client):
    client, gateway = review_client
    case = create_case(client)
    material_id = add_material(client, case["id"])
    detail = create_review(client, case["id"], material_id)
    review_id = detail["review"]["id"]

    # 失败
    gateway.review_outcomes = ["failed"]
    failed_task_id = client.post(f"/api/reviews/{review_id}/generate").json()["id"]
    wait_for_task(client, failed_task_id, {"failed"})

    # 重试成功
    gateway.review_outcomes = ["success"]
    retried = client.post(f"/api/tasks/{failed_task_id}/retry", json={})
    assert retried.status_code == 200, retried.text
    wait_for_task(client, failed_task_id, {"completed"})

    task_detail = client.get(f"/api/tasks/{failed_task_id}").json()
    statuses = [a["status"] for a in task_detail["attempts"]]
    assert "failed" in statuses
    assert "completed" in statuses

    after = client.get(f"/api/reviews/{review_id}").json()
    assert len(after["versions"]) == 1
    assert after["current_version"]["attempt_id"] == task_detail["attempts"][1]["id"]
