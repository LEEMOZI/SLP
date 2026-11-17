import pytest
from fastapi.testclient import TestClient

from apps.workbench.backend.app import create_app
from apps.workbench.backend.cases import CaseRepository
from apps.workbench.backend.health import ReadinessReport, ServiceHealth


class StubHealthChecker:
    def __init__(self, status="ready"):
        self.status = status

    async def check(self):
        healthy = self.status == "ready"
        return ReadinessReport(
            status=self.status,
            can_manage_cases=True,
            available_count=1 if healthy else 0,
            total_count=1,
            checked_at="2026-11-16T00:00:00.000Z",
            services=[
                ServiceHealth(
                    name="stub",
                    label="测试服务",
                    status="healthy" if healthy else "unavailable",
                    latency_ms=1,
                    detail="服务正常" if healthy else "连接失败",
                    checked_at="2026-11-16T00:00:00.000Z",
                )
            ],
        )


@pytest.fixture
def repository(tmp_path):
    return CaseRepository(tmp_path / "workbench.db")


@pytest.fixture
def client(repository):
    with TestClient(create_app(repository=repository, health_checker=StubHealthChecker())) as test_client:
        yield test_client
