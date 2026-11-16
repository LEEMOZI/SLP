import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from conftest import StubHealthChecker
from test_materials import FakeGateway, create_case, upload, wait_for_task


class SearchGateway(FakeGateway):
    def __init__(self):
        super().__init__(["success"])
        self.behavior = {}
        self.search_calls = []
        self.source_calls = 0
        self.fail_source = False

    async def parse_events(self, payload):
        async for event in super().parse_events(payload):
            if event["type"] == "segment":
                event["text"] = "《中华人民共和国民法典》 合同效力"
            yield event

    def _response(self, source, payload, status="success", empty=False):
        results = []
        if status != "failed" and not empty:
            is_judgment = source == "judgments"
            results = [
                {
                    "title": "示例判决书" if is_judgment else "中华人民共和国民法典 第十条",
                    "source_label": "测试法院 · 2024" if is_judgment else "法律法规库",
                    "excerpt": "合同效力相关摘录",
                    "content": "" if is_judgment else "处理民事纠纷，应当依照法律。",
                    "hit_reason": "部分关键词命中" if is_judgment else "向量召回并经精排命中",
                    "match_type": "or" if is_judgment else "semantic",
                    "partial_match": is_judgment,
                    "rank": 1,
                    "score": None if is_judgment else 0.8,
                    "locator": {"year": 2024, "rowid": 7} if is_judgment else {"article": "第十条"},
                    "source_ref": {"year": 2024, "rowid": 7} if is_judgment else {"chunk_id": "chunk-1"},
                    "metadata": {"case_number": "（2024）测试1号"} if is_judgment else {},
                }
            ]
        return {
            "source": source,
            "status": status,
            "query": payload["query"],
            "filters": payload.get("filters") or {
                key: payload.get(key)
                for key in ("year_from", "year_to", "court", "cause")
            },
            "executed_queries": [
                {"query": payload["query"], "kind": "primary", "status": "submitted"}
            ],
            "coverage": {"status": "failed" if status == "failed" else "completed"},
            "results": results,
            "error": {"code": "retrieval_failed", "message": "检索源暂不可用"} if status == "failed" else None,
        }

    async def search(self, source, payload):
        self.search_calls.append((source, payload))
        behavior = self.behavior.get(source, "success")
        if behavior == "slow":
            await asyncio.sleep(0.3)
            behavior = "success"
        if behavior == "exception":
            raise RuntimeError("private token and database path")
        return self._response(
            source,
            payload,
            status="failed" if behavior in {"failed", "exception"} else "success",
            empty=behavior == "empty",
        )

    async def judgment_source(self, year, rowid):
        self.source_calls += 1
        if self.fail_source:
            raise RuntimeError("offline")
        return {
            "title": "示例判决书",
            "content": "完整的裁判文书原文",
            "metadata": {"case_number": "（2024）测试1号"},
            "source_ref": {"year": year, "rowid": rowid},
        }


@pytest.fixture
def search_client(repository):
    gateway = SearchGateway()
    with TestClient(
        create_app(
            repository=repository,
            health_checker=StubHealthChecker(),
            capability_gateway=gateway,
        )
    ) as client:
        yield client, gateway


def create_search(client, case_id, question="合同效力与赔偿责任", **kwargs):
    headers = kwargs.pop("headers", None)
    return client.post(
        f"/api/cases/{case_id}/searches",
        json={"question": question, **kwargs},
        headers=headers,
    )


def finished_attempt(client, task_id, expected={"completed"}):
    detail = wait_for_task(client, task_id, expected)
    attempt_id = detail["attempts"][-1]["id"]
    view = client.get(f"/api/searches/{task_id}/attempts/{attempt_id}")
    assert view.status_code == 200
    return detail, view.json()


def test_auto_dispute_saves_grouped_evidence_queries_filters_and_idempotency(search_client):
    client, gateway = search_client
    case = create_case(client)
    payload = {
        "question": "合同效力与赔偿责任",
        "filters": {"year_from": 2020, "year_to": 2024, "court": "北京", "cause": "合同纠纷"},
    }
    created = client.post(
        f"/api/cases/{case['id']}/searches",
        json=payload,
        headers={"Idempotency-Key": "search-1"},
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    repeated = client.post(
        f"/api/cases/{case['id']}/searches",
        json=payload,
        headers={"Idempotency-Key": "search-1"},
    )
    assert repeated.json()["id"] == task_id
    conflict = create_search(
        client,
        case["id"],
        question="不同问题",
        headers={"Idempotency-Key": "search-1"},
    )
    assert conflict.status_code == 409

    detail, view = finished_attempt(client, task_id)
    assert {call[0] for call in gateway.search_calls} == {"law", "judgments"}
    assert len(view["evidence"]["law"]) == 1
    assert len(view["evidence"]["cases"]) == 1
    assert view["evidence"]["reference"] == []
    assert view["sources"]["law"]["executed_queries"][0]["query"] == "合同效力与赔偿责任"
    judgment_request = next(call[1] for call in gateway.search_calls if call[0] == "judgments")
    law_request = next(call[1] for call in gateway.search_calls if call[0] == "law")
    assert judgment_request["court"] == "北京"
    assert judgment_request["year_from"] == 2020
    assert law_request["filters"] == {}
    assert view["evidence"]["cases"][0]["partial_match"] is True
    assert detail["result"]["evidence_count"] == 2


def test_rule_routing_modes_and_uncovered_sources(search_client):
    client, gateway = search_client
    case = create_case(client)
    scenarios = [
        ("什么是诉讼时效", {}, {"law"}),
        ("查找类似案件的类案裁判", {}, {"judgments", "guiding_cases"}),
        ("我需要办案指引和学习材料", {}, {"reference"}),
        ("完全无法归类的一句话", {}, {"law"}),
        ("任意问题", {"mode": "cases"}, {"judgments", "guiding_cases"}),
        ("任意问题", {"mode": "law"}, {"law"}),
    ]
    for index, (question, extra, expected_sources) in enumerate(scenarios):
        gateway.search_calls.clear()
        created = create_search(client, case["id"], question=question, **extra)
        assert created.status_code == 201
        _, view = finished_attempt(client, created.json()["id"])
        assert {call[0] for call in gateway.search_calls} == expected_sources
        assert set(view["plan"]["sources"]) == expected_sources
        if index == 3:
            assert set(view["plan"]["uncovered_sources"]) == {
                "judgments",
                "guiding_cases",
                "reference",
            }
    assert client.get(f"/api/cases/{case['id']}/searches").json()["total"] == len(scenarios)


def test_zero_hits_all_failed_and_partial_source_failure_are_distinct(search_client):
    client, gateway = search_client
    case = create_case(client)

    gateway.behavior = {"law": "empty"}
    empty = create_search(client, case["id"], question="法条定义", mode="law").json()
    _, empty_view = finished_attempt(client, empty["id"])
    assert empty_view["status"] == "completed"
    assert empty_view["sources"]["law"]["evidence_count"] == 0

    gateway.behavior = {"law": "exception"}
    failed = create_search(client, case["id"], question="法条定义", mode="law").json()
    failed_detail, failed_view = finished_attempt(client, failed["id"], {"failed"})
    assert failed_view["sources"]["law"]["status"] == "failed"
    assert failed_view["evidence"]["law"] == []
    assert "private" not in failed_detail["error"]

    gateway.behavior = {"law": "success", "judgments": "failed"}
    partial = create_search(client, case["id"]).json()
    _, partial_view = finished_attempt(client, partial["id"], {"partial"})
    assert partial_view["sources"]["law"]["status"] == "success"
    assert partial_view["sources"]["judgments"]["status"] == "failed"
    assert len(partial_view["evidence"]["law"]) == 1


def test_material_enrichment_is_traced_and_cross_case_or_archived_writes_fail(search_client):
    client, gateway = search_client
    case = create_case(client)
    other = create_case(client, "其他案件")
    material = upload(client, case["id"]).json()
    wait_for_task(client, material["task"]["id"], {"completed"})
    version_id = client.get(f"/api/materials/{material['material']['id']}").json()["versions"][0]["id"]

    created = create_search(
        client,
        case["id"],
        question="法条依据",
        mode="law",
        material_version_ids=[version_id],
    ).json()
    _, view = finished_attempt(client, created["id"])
    assert "中华人民共和国民法典" in gateway.search_calls[-1][1]["query"]
    enrichment = view["plan"]["material_enrichment"][0]
    assert enrichment["material_version_id"] == version_id
    assert enrichment["segment_ids"]
    assert "中华人民共和国民法典" in enrichment["terms"]
    assert view["plan"]["filters"]["cause"] == ""

    assert create_search(
        client,
        other["id"],
        mode="law",
        material_version_ids=[version_id],
    ).status_code == 422
    assert client.post(f"/api/cases/{case['id']}/archive").status_code == 200
    assert create_search(client, case["id"], mode="law").status_code == 409
    assert client.get(f"/api/searches/{created['id']}/attempts/{view['attempt_id']}").status_code == 200


def test_retry_new_attempt_preserves_old_evidence_and_query_trace(search_client):
    client, gateway = search_client
    case = create_case(client)
    gateway.behavior = {"judgments": "failed"}
    task_id = create_search(client, case["id"]).json()["id"]
    _, old_view = finished_attempt(client, task_id, {"partial"})
    old_ids = {item["id"] for item in old_view["evidence"]["law"]}

    retried = client.post(
        f"/api/tasks/{task_id}/retry",
        json={"scope": {"query": "民法典 第十条", "mode": "law"}},
    )
    assert retried.status_code == 200
    assert retried.json()["attempt_number"] == 2
    detail, new_view = finished_attempt(client, task_id)
    assert new_view["plan"]["base_query"] == "民法典 第十条"
    new_ids = {item["id"] for item in new_view["evidence"]["law"]}
    assert old_ids.isdisjoint(new_ids)
    old_again = client.get(
        f"/api/searches/{task_id}/attempts/{old_view['attempt_id']}"
    ).json()
    assert {item["id"] for item in old_again["evidence"]["law"]} == old_ids
    assert old_again["plan"]["base_query"] == "合同效力与赔偿责任"
    assert [item["status"] for item in detail["attempts"]] == ["partial", "completed"]


def test_judgment_full_text_is_cached_immutably(search_client):
    client, gateway = search_client
    case = create_case(client)
    gateway.behavior = {"guiding_cases": "empty"}
    task_id = create_search(client, case["id"], question="查找类案", mode="cases").json()["id"]
    _, view = finished_attempt(client, task_id)
    evidence = next(
        item for item in view["evidence"]["cases"] if item["source"] == "judgments"
    )
    url = f"/api/evidence/{evidence['id']}"
    first = client.get(url)
    assert first.status_code == 200
    assert first.json()["document"]["content"] == "完整的裁判文书原文"
    assert gateway.source_calls == 1
    gateway.fail_source = True
    second = client.get(url)
    assert second.status_code == 200
    assert second.json()["document"]["cached"] is True
    assert second.json()["source_error"] is None
    assert gateway.source_calls == 1


def test_cancel_preserves_completed_source_evidence(search_client):
    client, gateway = search_client
    case = create_case(client)
    gateway.behavior = {"judgments": "slow"}
    task_id = create_search(client, case["id"]).json()["id"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        detail = client.get(f"/api/tasks/{task_id}").json()
        if (detail.get("result") or {}).get("evidence_count", 0) >= 1:
            break
        time.sleep(0.01)
    assert detail["result"]["evidence_count"] >= 1
    assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
    detail, view = finished_attempt(client, task_id, {"partial", "cancelled"})
    assert view["evidence"]["law"]
    assert detail["status"] == "partial"


def test_invalid_year_range_and_retry_scope_are_rejected(search_client):
    client, _ = search_client
    case = create_case(client)
    invalid = create_search(
        client,
        case["id"],
        filters={"year_from": 2025, "year_to": 2020},
    )
    assert invalid.status_code == 422
    task_id = create_search(client, case["id"], mode="law").json()["id"]
    wait_for_task(client, task_id, {"completed"})
    assert client.post(
        f"/api/tasks/{task_id}/retry", json={"scope": {"unknown": "value"}}
    ).status_code == 422
