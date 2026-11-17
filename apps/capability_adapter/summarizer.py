import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator


SUMMARY_PROVIDERS = ("builtin-extractive", "openai-compatible")


class SummarySourceInput(BaseModel):
    handle: str = Field(pattern=r"^S[1-9][0-9]*$")
    title: str = Field(default="", max_length=500)
    text: str = Field(min_length=1, max_length=100000)
    locator: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("text")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("来源文本不能为空")
        return value


class SummaryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    sources: List[SummarySourceInput] = Field(min_length=1, max_length=100)
    provider: Literal["builtin-extractive", "openai-compatible"] = "builtin-extractive"

    @field_validator("question")
    @classmethod
    def non_blank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("研究问题不能为空")
        return value.strip()


class SummaryGenerationError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class SummarySettings:
    provider: str = "builtin-extractive"
    openai_base_url: str = ""
    openai_model: str = ""
    openai_api_key: str = ""
    timeout_s: float = 180.0

    @classmethod
    def from_env(cls) -> "SummarySettings":
        try:
            timeout = float(os.getenv("SLP_SUMMARY_OPENAI_TIMEOUT_S", "180"))
        except ValueError:
            timeout = 180.0
        return cls(
            provider=os.getenv("SLP_SUMMARY_PROVIDER", "builtin-extractive").strip(),
            openai_base_url=os.getenv("SLP_SUMMARY_OPENAI_BASE_URL", "").strip(),
            openai_model=os.getenv("SLP_SUMMARY_OPENAI_MODEL", "").strip(),
            openai_api_key=os.getenv("SLP_SUMMARY_OPENAI_API_KEY", "").strip(),
            timeout_s=timeout if timeout > 0 else 180.0,
        )

    @property
    def openai_configured(self) -> bool:
        return bool(self.openai_base_url and self.openai_model and self.openai_api_key)


class SummaryGenerator:
    def __init__(
        self,
        settings: SummarySettings,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        if settings.provider not in SUMMARY_PROVIDERS:
            raise ValueError("SLP_SUMMARY_PROVIDER 配置无效")
        self.settings = settings
        self.transport = transport

    def capability(self) -> Dict[str, Any]:
        return {
            "default_provider": self.settings.provider,
            "providers": [
                {"id": "builtin-extractive", "status": "available"},
                {
                    "id": "openai-compatible",
                    "status": "available" if self.settings.openai_configured else "unavailable",
                },
            ],
        }

    async def generate(self, payload: SummaryRequest) -> Dict[str, Any]:
        if payload.provider != self.settings.provider:
            raise SummaryGenerationError(
                "summary_provider_mismatch",
                "请求生成器与服务配置不一致",
                status_code=503,
            )
        if payload.provider == "builtin-extractive":
            return self._extractive(payload)
        return await self._openai_compatible(payload)

    @staticmethod
    def _extractive(payload: SummaryRequest) -> Dict[str, Any]:
        sections = [
            "规则摘录草稿（未使用模型，不构成法律结论）",
            f"研究问题：{payload.question}",
        ]
        used_handles = []
        for index, source in enumerate(payload.sources, start=1):
            text = source.text.strip()
            sentences = re.split(r"(?<=[。！？!?])", text)
            excerpt = "".join(sentences[:2]).strip()
            if len(excerpt) > 420:
                excerpt = excerpt[:420]
            title = source.title.strip() or f"来源 {source.handle}"
            sections.append(f"### {index}. {title}\n{excerpt} [[{source.handle}]]")
            used_handles.append(source.handle)
        return {
            "body": "\n\n".join(sections),
            "used_handles": used_handles,
            "provider": "builtin-extractive",
            "actual_model": None,
            "warnings": ["本稿仅摘录真实来源，未进行模型归纳或外部事实补充"],
        }

    async def _openai_compatible(self, payload: SummaryRequest) -> Dict[str, Any]:
        if not self.settings.openai_configured:
            raise SummaryGenerationError(
                "summary_configuration_error",
                "真实模型配置不完整，未生成摘要",
                status_code=503,
            )
        source_description = [
            {
                "handle": source.handle,
                "title": source.title,
                "text": source.text,
                "locator": source.locator,
            }
            for source in payload.sources
        ]
        messages = [
            {
                "role": "system",
                "content": (
                    "你是法律研究摘要助手。只能依据用户提供的来源，不得补充外部事实。"
                    "所有事实性归纳必须使用来源句柄，格式为 [[S1]]。"
                    "仅返回 JSON 对象：body（摘要正文）、used_handles（实际引用句柄数组）、"
                    "warnings（限制或警告字符串数组）。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"question": payload.question, "sources": source_description},
                    ensure_ascii=False,
                ),
            },
        ]
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(self.settings.timeout_s, connect=5.0),
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.settings.openai_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {self.settings.openai_api_key}"},
                    json={
                        "model": self.settings.openai_model,
                        "messages": messages,
                        "temperature": 0.1,
                    },
                )
                response.raise_for_status()
                result = response.json()
            content = result["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("invalid model content")
            stripped = content.strip()
            if stripped.startswith("```"):
                stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped)
            generated = json.loads(stripped)
            body = generated["body"]
            used_handles = generated["used_handles"]
            warnings = generated.get("warnings") or []
            if not isinstance(body, str) or not body.strip():
                raise ValueError("empty model body")
            if not isinstance(used_handles, list) or not all(
                isinstance(handle, str) for handle in used_handles
            ):
                raise ValueError("invalid model handles")
            if not isinstance(warnings, list) or not all(
                isinstance(warning, str) for warning in warnings
            ):
                raise ValueError("invalid model warnings")
            allowed = {source.handle for source in payload.sources}
            cited = set(re.findall(r"\[\[([^\[\]\s]{1,32})\]\]", body))
            if not cited <= allowed or not set(used_handles) <= allowed:
                raise ValueError("unknown model citation")
            actual_model = result.get("model") or self.settings.openai_model
            return {
                "body": body,
                "used_handles": used_handles,
                "provider": "openai-compatible",
                "actual_model": actual_model,
                "warnings": warnings,
            }
        except SummaryGenerationError:
            raise
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise SummaryGenerationError(
                "summary_generation_failed",
                "真实模型请求或响应校验失败，未降级为规则摘要",
            ) from error
