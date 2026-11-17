import json
import os
from typing import Any, AsyncGenerator, Dict, Optional

import httpx


class CapabilityError(Exception):
    pass


class CapabilityGateway:
    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.base_url = (
            base_url
            or os.getenv("SLP_CAPABILITY_ADAPTER_URL", "http://127.0.0.1:8790")
        ).rstrip("/")
        resolved_token = os.getenv("SLP_CAPABILITY_ADAPTER_TOKEN", "") if token is None else token
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-SLP-Internal-Token": resolved_token} if resolved_token else {},
            timeout=httpx.Timeout(300.0, connect=5.0),
            transport=transport,
        )

    async def capabilities(self) -> Dict[str, Any]:
        try:
            response = await self.client.get("/v1/capabilities")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise CapabilityError("能力适配服务暂不可用") from error

    async def parse_events(self, payload: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        try:
            async with self.client.stream("POST", "/v1/parse", json=payload) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise CapabilityError("材料解析服务请求失败")
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise CapabilityError("材料解析服务返回了无效事件")
                    yield event
        except CapabilityError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise CapabilityError("材料解析服务连接中断") from error

    async def search(self, source: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        paths = {
            "law": "/v1/search/law",
            "guiding_cases": "/v1/search/guiding-cases",
            "reference": "/v1/search/reference",
            "judgments": "/v1/search/judgments",
        }
        if source not in paths:
            raise ValueError(f"未知检索来源：{source}")
        try:
            response = await self.client.post(paths[source], json=payload)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise CapabilityError("检索服务返回了无效结果")
            return result
        except CapabilityError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise CapabilityError("检索服务暂不可用") from error

    async def generate_summary(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = await self.client.post("/v1/summaries", json=payload)
            if response.status_code != 200:
                message = "摘要生成服务请求失败"
                try:
                    detail = response.json().get("detail")
                    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
                        message = detail["message"]
                except (ValueError, AttributeError):
                    pass
                raise CapabilityError(message)
            result = response.json()
            if not isinstance(result, dict):
                raise CapabilityError("摘要生成服务返回了无效结果")
            return result
        except CapabilityError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise CapabilityError("摘要生成服务暂不可用") from error

    async def judgment_source(self, year: int, rowid: int) -> Dict[str, Any]:
        try:
            response = await self.client.post(
                "/v1/judgments/source", json={"year": year, "rowid": rowid}
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise CapabilityError("裁判文书原文暂不可用") from error

    async def generate_review(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            response = await self.client.post("/v1/reviews", json=payload)
            if response.status_code != 200:
                message = "合同审查服务请求失败"
                try:
                    detail = response.json().get("detail")
                    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
                        message = detail["message"]
                except (ValueError, AttributeError):
                    pass
                raise CapabilityError(message)
            result = response.json()
            if not isinstance(result, dict):
                raise CapabilityError("合同审查服务返回了无效结果")
            return result
        except CapabilityError:
            raise
        except (httpx.HTTPError, ValueError) as error:
            raise CapabilityError("合同审查服务暂不可用") from error

    async def close(self) -> None:
        await self.client.aclose()
