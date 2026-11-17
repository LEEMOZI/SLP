import hashlib
import io

import pytest
from docx import Document

from test_materials import create_case, upload, wait_for_task
from test_searches import SearchGateway
from test_summaries import SummaryGateway


class DeliveryGateway(SummaryGateway):
    """材料段落与证据文本包含可校验的法律事实。"""

    SEGMENT_TEXT = (
        "经审理查明：（2026）京03民终13295号民事判决于2026年5月12日生效，"
        "被告应支付赔偿款100万元。"
    )

    async def parse_events(self, payload):
        async for event in super().parse_events(payload):
            if event["type"] == "segment":
                event["text"] = self.SEGMENT_TEXT
            yield event


@pytest.fixture
def delivery_client(repository):
    gateway = DeliveryGateway()
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


def prepare_confirmed_summary(client, title="违约责任研究", body=None):
    case = create_case(client, f"交付案件-{title}")
    created = upload(client, case["id"], content="ignored", name="judgment.txt")
    wait_for_task(client, created.json()["task"]["id"], {"completed"})
    material_id = created.json()["material"]["id"]
    detail = client.get(f"/api/materials/{material_id}").json()
    segments = client.get(
        f"/api/materials/{material_id}/versions/{detail['material']['current_version_id']}/segments"
    ).json()["items"]
    source = {"kind": "material_segment", "id": segments[0]["id"]}
    summary = client.post(
        f"/api/cases/{case['id']}/summaries",
        json={"title": title, "question": "违约赔偿范围", "sources": [source]},
    ).json()
    if body is None:
        body = "经核对 [[S1]]：案号（2026）京03民终13295号，判决日期2026年5月12日，金额100万元。"
    saved = client.post(
        f"/api/summaries/{summary['summary']['id']}/versions",
        json={"body": body},
    )
    assert saved.status_code == 201, saved.text
    confirmed = client.post(f"/api/summaries/{summary['summary']['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    return case, summary["summary"]["id"]


def test_unconfirmed_version_cannot_export(delivery_client):
    client, _ = delivery_client
    case = create_case(client, "未确认案件")
    created = upload(client, case["id"], content="ignored")
    wait_for_task(client, created.json()["task"]["id"], {"completed"})
    material_id = created.json()["material"]["id"]
    detail = client.get(f"/api/materials/{material_id}").json()
    segments = client.get(
        f"/api/materials/{material_id}/versions/{detail['material']['current_version_id']}/segments"
    ).json()["items"]
    summary = client.post(
        f"/api/cases/{case['id']}/summaries",
        json={"title": "草稿研究", "question": "", "sources": [{"kind": "material_segment", "id": segments[0]["id"]}]},
    ).json()
    saved = client.post(
        f"/api/summaries/{summary['summary']['id']}/versions",
        json={"body": "草稿正文 [[S1]]，尚未确认。"},
    )
    assert saved.status_code == 201
    rejected = client.post(f"/api/summaries/{summary['summary']['id']}/exports", json={"format": "markdown"})
    assert rejected.status_code == 422
    assert "确认" in rejected.json()["detail"]
    assert client.get(f"/api/cases/{case['id']}/deliveries").json()["items"] == []


def test_official_markdown_export_download_and_list(delivery_client):
    client, _ = delivery_client
    case, summary_id = prepare_confirmed_summary(client)
    response = client.post(f"/api/summaries/{summary_id}/exports", json={"format": "markdown"})
    assert response.status_code == 201, response.text
    delivery = response.json()
    assert delivery["status"] == "official"
    assert delivery["issues"] == []
    assert delivery["format"] == "markdown"
    assert delivery["version_number"] == 1
    assert delivery["summary_title"] == "违约责任研究"
    assert delivery["facts"]["case_numbers"] == ["(2026)京03民终13295号"]
    assert delivery["facts"]["dates"] == ["2026年5月12日"]
    assert delivery["facts"]["amounts"] == ["100万元"]

    listed = client.get(f"/api/cases/{case['id']}/deliveries").json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == delivery["id"]
    assert "storage_key" not in listed["items"][0]

    downloaded = client.get(f"/api/deliveries/{delivery['id']}/download")
    assert downloaded.status_code == 200
    assert "markdown" in downloaded.headers.get("content-disposition", "")
    text = downloaded.content.decode("utf-8")
    assert "# 违约责任研究" in text
    assert "正式交付" in text
    assert "经核对 [[S1]]" in text
    assert "## 来源清单" in text and "S1" in text
    assert hashlib.sha256(downloaded.content).hexdigest() == delivery["sha256"]


def test_unverified_fact_blocks_export_and_keep_issues_exports_draft(delivery_client):
    client, _ = delivery_client
    case, summary_id = prepare_confirmed_summary(
        client,
        title="含未核实事实",
        body=(
            "经核对 [[S1]]：案号（2026）京03民终13295号。\n"
            "合同签订于2025年3月1日，标的金额80万元，来源未提及。"
        ),
    )
    rejected = client.post(f"/api/summaries/{summary_id}/exports", json={"format": "markdown"})
    assert rejected.status_code == 422
    payload = rejected.json()["detail"]
    assert payload["code"] == "summary_validation_failed"
    messages = [issue["message"] for issue in payload["issues"]]
    assert any("fact_unverified" == issue["code"] and "第 2 行" in issue["message"] and "80万元" in issue["message"] for issue in payload["issues"])
    assert messages

    draft = client.post(
        f"/api/summaries/{summary_id}/exports",
        json={"format": "markdown", "keep_issues": True},
    )
    assert draft.status_code == 201, draft.text
    delivery = draft.json()
    assert delivery["status"] == "draft"
    assert {issue["code"] for issue in delivery["issues"]} == {"fact_unverified"}

    downloaded = client.get(f"/api/deliveries/{delivery['id']}/download")
    text = downloaded.content.decode("utf-8")
    assert "草稿" in text
    assert "第 2 行" in text


def test_docx_export_is_readable(delivery_client):
    client, _ = delivery_client
    case, summary_id = prepare_confirmed_summary(client, title="DOCX交付")
    response = client.post(f"/api/summaries/{summary_id}/exports", json={"format": "docx"})
    assert response.status_code == 201, response.text
    delivery = response.json()
    assert delivery["status"] == "official"
    assert delivery["format"] == "docx"
    assert delivery["facts"]["case_numbers"] == ["(2026)京03民终13295号"]

    downloaded = client.get(f"/api/deliveries/{delivery['id']}/download")
    assert downloaded.status_code == 200
    assert "docx" in downloaded.headers.get("content-disposition", "")
    document = Document(io.BytesIO(downloaded.content))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    assert "DOCX交付" in paragraphs
    assert any("[[S1]]" in text for text in paragraphs)


def test_archived_case_blocks_export_but_keeps_list_and_download(delivery_client):
    client, _ = delivery_client
    case, summary_id = prepare_confirmed_summary(client, title="归档案件")
    first = client.post(f"/api/summaries/{summary_id}/exports", json={"format": "markdown"})
    assert first.status_code == 201
    delivery = first.json()

    assert client.post(f"/api/cases/{case['id']}/archive").status_code == 200
    rejected = client.post(f"/api/summaries/{summary_id}/exports", json={"format": "markdown"})
    assert rejected.status_code == 409
    listed = client.get(f"/api/cases/{case['id']}/deliveries")
    assert listed.status_code == 200 and listed.json()["total"] == 1
    downloaded = client.get(f"/api/deliveries/{delivery['id']}/download")
    assert downloaded.status_code == 200

    missing = client.get("/api/deliveries/nonexistent/download")
    assert missing.status_code == 404


def test_exports_are_immutable_across_new_versions(delivery_client):
    client, _ = delivery_client
    case, summary_id = prepare_confirmed_summary(client, title="不可变研究")
    first = client.post(f"/api/summaries/{summary_id}/exports", json={"format": "markdown"}).json()

    revised = client.post(
        f"/api/summaries/{summary_id}/versions",
        json={"body": "修订后的正文 [[S1]]，案号（2026）京03民终13295号，日期2026年5月12日，金额100万元。"},
    )
    assert revised.status_code == 201
    assert client.post(f"/api/summaries/{summary_id}/confirm").status_code == 200
    second = client.post(f"/api/summaries/{summary_id}/exports", json={"format": "markdown"}).json()

    listed = client.get(f"/api/cases/{case['id']}/deliveries").json()
    assert listed["total"] == 2
    assert [item["version_number"] for item in listed["items"]] == [2, 1]

    first_download = client.get(f"/api/deliveries/{first['id']}/download").content
    second_download = client.get(f"/api/deliveries/{second['id']}/download").content
    assert hashlib.sha256(first_download).hexdigest() == first["sha256"]
    assert first["sha256"] != second["sha256"]
    assert "修订后的正文" not in first_download.decode("utf-8")
