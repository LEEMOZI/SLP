import asyncio
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

import httpx
from pydantic import BaseModel


@dataclass(frozen=True)
class ServiceTarget:
    name: str
    label: str
    url: str


class ServiceHealth(BaseModel):
    name: str
    label: str
    status: str
    latency_ms: int
    detail: str
    checked_at: str


class ReadinessReport(BaseModel):
    status: str
    can_manage_cases: bool
    available_count: int
    total_count: int
    checked_at: str
    services: List[ServiceHealth]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class HealthChecker:
    def __init__(
        self,
        targets: Optional[List[ServiceTarget]] = None,
        timeout: float = 3.0,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.targets = targets or []
        self.timeout = timeout
        self.transport = transport

    @classmethod
    def from_env(cls) -> "HealthChecker":
        targets = [
            ServiceTarget(
                "law-search",
                "法规检索",
                os.getenv("SLP_LAW_SEARCH_URL", "http://127.0.0.1:8765") + "/health",
            ),
            ServiceTarget(
                "judgment-search",
                "类案检索",
                os.getenv("SLP_JUDGMENT_SEARCH_URL", "http://127.0.0.1:8766") + "/health",
            ),
            ServiceTarget(
                "session-storage",
                "会话存储",
                os.getenv("SLP_SESSION_STORAGE_URL", "http://127.0.0.1:8767") + "/health",
            ),
            ServiceTarget(
                "lm-studio",
                "本地模型",
                os.getenv("SLP_LM_STUDIO_URL", "http://127.0.0.1:1234") + "/api/v0/models",
            ),
        ]
        timeout = float(os.getenv("SLP_WORKBENCH_HEALTH_TIMEOUT", "3"))
        return cls(targets=targets, timeout=timeout)

    async def check(self) -> ReadinessReport:
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
            services = await asyncio.gather(
                *(self.check_service(client, target) for target in self.targets)
            )
        available_count = sum(service.status == "healthy" for service in services)
        return ReadinessReport(
            status="ready" if available_count == len(services) else "degraded",
            can_manage_cases=True,
            available_count=available_count,
            total_count=len(services),
            checked_at=utc_now(),
            services=services,
        )

    async def check_service(self, client: httpx.AsyncClient, target: ServiceTarget) -> ServiceHealth:
        started = time.monotonic()
        try:
            response = await client.get(target.url)
            latency_ms = round((time.monotonic() - started) * 1000)
            if response.status_code != 200:
                return self.result(target, "unavailable", latency_ms, f"HTTP {response.status_code}")
            detail = "服务正常"
            if target.name == "lm-studio":
                detail = self.model_detail(response)
            return self.result(target, "healthy", latency_ms, detail)
        except httpx.RequestError as error:
            latency_ms = round((time.monotonic() - started) * 1000)
            return self.result(target, "unavailable", latency_ms, f"连接失败：{error}")

    @staticmethod
    def model_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except ValueError:
            return "服务正常"
        models = payload.get("data") or payload.get("models") or []
        loaded = []
        for model in models:
            if model.get("loaded_instances"):
                loaded.append(str(model.get("id") or model.get("model") or "未知模型"))
        return "已加载：" + "、".join(loaded) if loaded else "服务正常"

    @staticmethod
    def result(target: ServiceTarget, status: str, latency_ms: int, detail: str) -> ServiceHealth:
        return ServiceHealth(
            name=target.name,
            label=target.label,
            status=status,
            latency_ms=latency_ms,
            detail=detail,
            checked_at=utc_now(),
        )
