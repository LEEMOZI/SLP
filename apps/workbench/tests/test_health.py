import asyncio

import httpx
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from apps.workbench.backend.health import HealthChecker, ServiceTarget
from conftest import StubHealthChecker


def test_readiness_degradation_does_not_block_case_management(client, repository):
    with TestClient(create_app(repository=repository, health_checker=StubHealthChecker("degraded"))) as degraded_client:
        response = degraded_client.get("/api/readiness")
        assert response.status_code == 200
        assert response.json()["status"] == "degraded"
        assert response.json()["can_manage_cases"] is True
        assert degraded_client.post("/api/cases", json={"name": "降级模式案件"}).status_code == 201


def test_health_checker_reports_partial_failure():
    def handler(request):
        if request.url.host == "healthy":
            return httpx.Response(200, json={"status": "ok"})
        raise httpx.ConnectError("connection refused", request=request)

    checker = HealthChecker(
        targets=[
            ServiceTarget("healthy", "正常服务", "http://healthy/health"),
            ServiceTarget("down", "离线服务", "http://down/health"),
        ],
        transport=httpx.MockTransport(handler),
    )
    report = asyncio.run(checker.check())
    assert report.status == "degraded"
    assert report.available_count == 1
    assert report.total_count == 2
    assert report.can_manage_cases is True
    assert [service.status for service in report.services] == ["healthy", "unavailable"]
    assert "连接失败" in report.services[1].detail


def test_health_checker_reports_loaded_model():
    def handler(_):
        return httpx.Response(
            200,
            json={"data": [{"id": "qwen/qwen3.6-35b-a3b", "loaded_instances": [{"id": "loaded"}]}]},
        )

    checker = HealthChecker(
        targets=[ServiceTarget("lm-studio", "本地模型", "http://model/api/v0/models")],
        transport=httpx.MockTransport(handler),
    )
    report = asyncio.run(checker.check())
    assert report.status == "ready"
    assert report.services[0].detail == "已加载：qwen/qwen3.6-35b-a3b"
