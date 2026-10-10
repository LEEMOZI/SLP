"""
LLM Client — Multi-Provider Router
RAG-12.10+

Provider:
    lm_studio_local
    deepseek_api
    bionic_secure_cloud      # reserved
    lm_link                  # reserved

设计目标：
1. 保持旧版调用接口完全兼容：
       chat_completion(prompt)
       chat_completion(prompt, temperature=0.2, max_tokens=1200)

2. 支持：
       DEFAULT_ROUTER.chat_completion(
           prompt,
           task_type_hint="litigation_writing",
       )

3. Auto Route：
       短任务 / 普通任务
           -> LM Studio Local

       长任务 / 指定法律复杂任务
           -> DeepSeek V4 Pro

4. Provider 失败自动 fallback。

5. API Key 只从环境变量读取。

6. Provider client 懒加载：
       import core.llm_client
       不会立即连接 LM Studio / DeepSeek。

7. LM Studio：
       enable_thinking=False

8. DeepSeek：
       V4 Flash / V4 Pro
       支持 thinking / reasoning_effort
       thinking 模式下不发送 temperature。
"""

from __future__ import annotations

import concurrent.futures
import logging
import time
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

from openai import OpenAI

from config import (
    AUTO_ROUTE,
    LEGAL_SYSTEM_PROMPT,
    LLM_FALLBACK_CHAIN,
    LLM_MAX_TOKENS,
    LLM_PROVIDER,
    LLM_PROVIDERS,
    LLM_PROVIDER_SOFT_TIMEOUT_SEC,
    LLM_TEMPERATURE,
    LLM_TOTAL_TIMEOUT_SEC,
)

logger = logging.getLogger("law-search.llm")


# ============================================================================
# Provider Base Class
# ============================================================================

class BaseLLMProvider:
    """
    所有 Provider 的通用基类。

    所有 Provider 均尽量使用 OpenAI-compatible Chat Completions API。
    """

    provider_id: str = "base"
    display_name: str = "Base Provider"

    # 子类可覆盖。
    # LM Studio 使用 chat_template_kwargs。
    # DeepSeek 使用 thinking。
    enable_thinking: Optional[bool] = None

    def __init__(self, config: Dict[str, Any]):
        self.config = config

        self.provider_id = getattr(
            type(self),
            "provider_id",
            self.provider_id,
        )

        self.display_name = config.get(
            "display_name",
            self.display_name,
        )

        self._client: Optional[OpenAI] = None
        self._last_model_used: Optional[str] = None

    # ------------------------------------------------------------------
    # Lazy OpenAI Client
    # ------------------------------------------------------------------

    @property
    def client(self) -> OpenAI:
        """
        第一次真正调用时才创建 OpenAI client。

        避免：
            import core.llm_client

        就产生网络 / localhost 连接副作用。
        """

        if self._client is None:
            api_key = (
                self.config.get("api_key")
                or "placeholder"
            )

            base_url = self.config["base_url"]

            timeout = self.config.get(
                "timeout_sec",
                LLM_TOTAL_TIMEOUT_SEC,
            )

            self._client = OpenAI(
                base_url=base_url,
                api_key=api_key,
                timeout=timeout,
            )

            logger.info(
                "Initialized OpenAI client | "
                "provider=%s | base_url=%s | api_key_env=%s",
                self.provider_id,
                base_url,
                self.config.get("api_key_env"),
            )

        return self._client

    # ------------------------------------------------------------------
    # Prompt length estimation
    # ------------------------------------------------------------------

    @staticmethod
    def estimate_prompt_tokens(prompt: str) -> int:
        """
        粗略估算 token 数。

        这里故意不引入 tokenizer，避免：
            - 增加启动依赖
            - 不同模型 tokenizer 不一致
            - 本地模型 / DeepSeek tokenizer 不统一

        对中文法律文本而言：
            约 1 token ≈ 1~2 中文字符

        这里使用保守估算：
            max(len(prompt) // 2, len(prompt) // 4)

        实际 Router 主要使用字符阈值，因此这里只作为辅助。
        """

        if not prompt:
            return 0

        # 一个相对保守、简单的估算。
        return max(1, len(prompt) // 2)

    # ------------------------------------------------------------------
    # Model Selection
    # ------------------------------------------------------------------

    def choose_model(
        self,
        prompt_len: int,
        task_type_hint: Optional[str] = None,
        force_model: Optional[str] = None,
    ) -> str:
        """
        Provider 内部模型选择。

        force_model:
            最高优先级。

        long_task_types:
            对法律长任务优先 long_context_model。

        prompt 超长：
            同样优先 long_context_model。
        """

        if force_model:
            self._last_model_used = force_model
            return force_model

        cfg = self.config

        long_tasks = AUTO_ROUTE.get(
            "long_task_types",
            [],
        )

        long_model = cfg.get(
            "long_context_model"
        )

        if (
            task_type_hint
            and task_type_hint in long_tasks
            and long_model
        ):
            self._last_model_used = long_model
            return long_model

        # Router 已经做过一次判断。
        # Provider 内部这里只做第二层保险。
        max_context = int(
            cfg.get(
                "capabilities",
                {},
            ).get(
                "max_context_tokens",
                32768,
            )
        )

        # 粗略估算：中文法律文本约 1 token ≈ 2 字符
        estimated_tokens = prompt_len // 2

        if (
            estimated_tokens > max_context * 0.70
            and long_model
        ):
            self._last_model_used = long_model
            return long_model

        self._last_model_used = cfg.get(
            "default_model",
            "unknown",
        )

        return self._last_model_used

    # ------------------------------------------------------------------
    # Provider-specific request parameters
    # ------------------------------------------------------------------

    def build_extra_body(
        self,
        model: str,
    ) -> Optional[Dict[str, Any]]:
        """
        默认没有 extra_body。

        子类覆盖。
        """

        flag = self.config.get(
            "enable_thinking",
            self.enable_thinking,
        )

        if flag is None:
            return None

        return {
            "chat_template_kwargs": {
                "enable_thinking": bool(flag),
            }
        }

    def should_send_temperature(
        self,
        model: str,
    ) -> bool:
        """
        默认发送 temperature。
        """

        return True

    def build_reasoning_kwargs(
        self,
        model: str,
    ) -> Dict[str, Any]:
        """
        默认没有 reasoning 参数。
        """

        return {}

    # ------------------------------------------------------------------
    # Shared Request Building
    # ------------------------------------------------------------------

    def _prepare_request(
        self,
        prompt: str,
        *,
        temperature: Optional[float],
        max_tokens: Optional[int],
        task_type_hint: Optional[str],
        force_model: Optional[str],
    ) -> Tuple[str, Dict[str, Any]]:
        """构建 (model, kwargs)。chat_completion 与 chat_completion_stream 共用。"""

        prompt_len_chars = len(prompt or "")

        model = self.choose_model(
            prompt_len_chars,
            task_type_hint,
            force_model,
        )

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": LEGAL_SYSTEM_PROMPT,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            "max_tokens": (
                max_tokens
                if max_tokens is not None
                else LLM_MAX_TOKENS
            ),
        }

        # --------------------------------------------------------------
        # temperature 双保险（P1-2(c)）：
        # thinking/reasoning 模式下不发送 temperature / top_p 类参数
        # --------------------------------------------------------------

        reasoning_kwargs_later = self.build_reasoning_kwargs(model)
        thinking_like_active = bool(reasoning_kwargs_later)
        send_temp = bool(self.should_send_temperature(model)) and (
            not thinking_like_active
        )

        if send_temp:
            kwargs["temperature"] = (
                temperature
                if temperature is not None
                else LLM_TEMPERATURE
            )

        # --------------------------------------------------------------
        # Provider-specific extra_body
        # --------------------------------------------------------------

        extra_body = self.build_extra_body(model)

        if extra_body:
            kwargs["extra_body"] = extra_body

        # --------------------------------------------------------------
        # Reasoning parameters
        # --------------------------------------------------------------

        if reasoning_kwargs_later:
            kwargs.update(reasoning_kwargs_later)

        return model, kwargs

    # ------------------------------------------------------------------
    # Main Completion
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        prompt: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        task_type_hint: Optional[str] = None,
        force_model: Optional[str] = None,
    ) -> str:

        started = time.perf_counter()

        prompt_len_chars = len(prompt or "")

        model, kwargs = self._prepare_request(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            task_type_hint=task_type_hint,
            force_model=force_model,
        )

        try:

            response = (
                self.client
                .chat
                .completions
                .create(**kwargs)
            )

            if (
                not response.choices
                or not response.choices[0].message
            ):
                raise RuntimeError(
                    f"Provider {self.provider_id} "
                    "returned an empty response."
                )

            message = response.choices[0].message

            content = (
                message.content
                or ""
            )

            elapsed_ms = (
                time.perf_counter()
                - started
            ) * 1000

            logger.info(
                "LLM success | "
                "provider=%s | "
                "model=%s | "
                "prompt_chars=%s | "
                "output_chars=%s | "
                "%.0fms",
                self.provider_id,
                model,
                prompt_len_chars,
                len(content),
                elapsed_ms,
            )

            return content

        except Exception as exc:

            elapsed_ms = (
                time.perf_counter()
                - started
            ) * 1000

            logger.warning(
                "LLM failed | "
                "provider=%s | "
                "model=%s | "
                "prompt_chars=%s | "
                "err=%s | "
                "%.0fms",
                self.provider_id,
                model,
                prompt_len_chars,
                type(exc).__name__,
                elapsed_ms,
                exc_info=True,
            )

            raise

    # ------------------------------------------------------------------
    # Streaming Completion
    # ------------------------------------------------------------------

    def _stream_read_timeout_sec(self) -> float:
        """流式读超时：相邻 chunk / 首 token 之间的最大静默秒数。

        复用该 provider 的软超时配置（LLM_PROVIDER_SOFT_TIMEOUT_SEC）——
        语义与 P1-2(a) 一致：静默超过 soft_sec 视为挂死。
        健康的长流式生成只要 chunk 间隔正常，就不会被误杀。
        """
        raw = LLM_PROVIDER_SOFT_TIMEOUT_SEC.get(self.provider_id)
        try:
            v = float(raw) if raw else 0.0
        except (TypeError, ValueError):
            v = 0.0
        return v if v > 0 else 120.0

    def chat_completion_stream(
        self,
        prompt: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        task_type_hint: Optional[str] = None,
        force_model: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """流式补全：逐段 yield 文本增量（仅 content，跳过 reasoning 段）。

        - 与 chat_completion 共用模型选择 / temperature 双保险 / extra_body
        - 读超时 = 该 provider 软超时（httpx read timeout 语义），
          首 token 或相邻 chunk 静默超限 → 抛超时 → Router 切 fallback
        """
        model, kwargs = self._prepare_request(
            prompt,
            temperature=temperature,
            max_tokens=max_tokens,
            task_type_hint=task_type_hint,
            force_model=force_model,
        )

        kwargs["stream"] = True
        kwargs["timeout"] = self._stream_read_timeout_sec()

        started = time.perf_counter()
        first_at: Optional[float] = None
        total_chars = 0

        try:

            stream = (
                self.client
                .chat
                .completions
                .create(**kwargs)
            )

            for chunk in stream:

                if not getattr(chunk, "choices", None):
                    continue

                delta = chunk.choices[0].delta

                piece = (
                    (getattr(delta, "content", None) or "")
                    if delta is not None
                    else ""
                )

                if not piece:
                    continue

                if first_at is None:
                    first_at = time.perf_counter() - started
                    logger.info(
                        "LLM stream TTFT | provider=%s | model=%s | %.2fs",
                        self.provider_id, model, first_at,
                    )

                total_chars += len(piece)
                yield piece

        except Exception as exc:

            logger.warning(
                "LLM stream failed | "
                "provider=%s | "
                "model=%s | "
                "ttft=%s | "
                "got_chars=%s | "
                "err=%s | "
                "%.1fs",
                self.provider_id,
                model,
                f"{first_at:.2f}s" if first_at is not None else "N/A",
                total_chars,
                type(exc).__name__,
                time.perf_counter() - started,
                exc_info=True,
            )

            raise

        logger.info(
            "LLM stream success | "
            "provider=%s | "
            "model=%s | "
            "ttft=%s | "
            "output_chars=%s | "
            "%.1fs",
            self.provider_id,
            model,
            f"{first_at:.2f}s" if first_at is not None else "N/A",
            total_chars,
            time.perf_counter() - started,
        )


# ============================================================================
# Provider #1 — LM Studio Local
# ============================================================================

class LMStudioLocalProvider(
    BaseLLMProvider
):
    """
    本地 LM Studio。

    默认：
        http://localhost:1234/v1

    当前生产模型：
        qwen/qwen3.8-27b
    """

    provider_id = "lm_studio_local"

    display_name = "LM Studio 本地推理"

    enable_thinking = False

    def build_extra_body(
        self,
        model: str,
    ) -> Optional[Dict[str, Any]]:

        flag = self.config.get(
            "enable_thinking",
            False,
        )

        return {
            "chat_template_kwargs": {
                "enable_thinking": bool(flag),
            }
        }


# ============================================================================
# Provider #2 — DeepSeek API
# ============================================================================

class DeepSeekAPIProvider(
    BaseLLMProvider
):
    """
    DeepSeek V4 API。

    当前官方模型：
        deepseek-v4-flash
        deepseek-v4-pro

    Flash：
        快速、成本较低。
        默认用于一般云端长任务。

    Pro：
        复杂法律分析、诉讼文书、证据评价等任务。
    """

    provider_id = "deepseek_api"

    display_name = "DeepSeek V4 API 云端推理"

    enable_thinking = None

    def build_extra_body(
        self,
        model: str,
    ) -> Optional[Dict[str, Any]]:

        # DeepSeek V4 当前支持：
        #
        # thinking:
        #     enabled / disabled
        #
        # 通过 OpenAI SDK 时放到 extra_body。
        #
        # 默认配置从 llm_config.py 获取。

        thinking_by_model = self.config.get(
            "thinking_by_model",
            {},
        )

        enabled = thinking_by_model.get(
            model,
            self.config.get(
                "enable_thinking",
                False,
            ),
        )

        return {
            "thinking": {
                "type": (
                    "enabled"
                    if bool(enabled)
                    else "disabled"
                )
            }
        }

    def should_send_temperature(
        self,
        model: str,
    ) -> bool:

        thinking_by_model = self.config.get(
            "thinking_by_model",
            {},
        )

        thinking_enabled = thinking_by_model.get(
            model,
            self.config.get(
                "enable_thinking",
                False,
            ),
        )

        # DeepSeek thinking mode：
        # temperature 不支持。
        return not bool(thinking_enabled)

    def build_reasoning_kwargs(
        self,
        model: str,
    ) -> Dict[str, Any]:

        thinking_by_model = self.config.get(
            "thinking_by_model",
            {},
        )

        thinking_enabled = thinking_by_model.get(
            model,
            self.config.get(
                "enable_thinking",
                False,
            ),
        )

        if not thinking_enabled:
            return {}

        effort_by_model = self.config.get(
            "reasoning_effort_by_model",
            {},
        )

        effort = effort_by_model.get(
            model
        )

        if not effort:
            return {}

        return {
            "reasoning_effort": effort
        }


# ============================================================================
# Provider #3 — Bionic Secure Cloud
# ============================================================================

class BionicSecureCloudProvider(
    BaseLLMProvider
):
    """
    Bionic Secure Cloud 预留接口。

    当前不参与默认路由。
    """

    provider_id = "bionic_secure_cloud"

    display_name = (
        "LM Studio Bionic Secure Cloud"
    )

    enable_thinking = None


# ============================================================================
# Provider #4 — LM Link
# ============================================================================

class LMLinkProvider(
    BaseLLMProvider
):
    """
    LM Link 远程设备预留接口。

    当前不参与默认路由。
    """

    provider_id = "lm_link"

    display_name = (
        "LM Studio LM Link 远程推理"
    )

    enable_thinking = None


# ============================================================================
# Provider Registry
# ============================================================================

PROVIDER_CLASSES: Dict[
    str,
    type,
] = {
    "lm_studio_local":
        LMStudioLocalProvider,

    "deepseek_api":
        DeepSeekAPIProvider,

    "bionic_secure_cloud":
        BionicSecureCloudProvider,

    "lm_link":
        LMLinkProvider,
}


# ============================================================================
# Router
# ============================================================================

class LLMProviderRouter:
    """
    Multi-Provider Router。

    路由优先级：

        provider_hint
            ↓
        LLM_PROVIDER
            ↓
        AUTO_ROUTE
            ↓
        fallback chain
    """

    def __init__(
        self,
        providers_registry: Dict[
            str,
            Dict[str, Any],
        ],
        *,
        default_provider: str = "auto",
        auto_route: Optional[
            Dict[str, Any]
        ] = None,
        fallback_chain: Optional[
            List[str]
        ] = None,
        total_timeout_sec: int = (
            LLM_TOTAL_TIMEOUT_SEC
        ),
    ):

        self._provider_configs = (
            providers_registry or {}
        )

        self._instances: Dict[
            str,
            BaseLLMProvider,
        ] = {}

        self.default_provider = (
            default_provider
            or "auto"
        )

        self.auto_route = (
            auto_route or {}
        )

        self.fallback_chain = list(
            fallback_chain or []
        )

        self.total_timeout_sec = (
            total_timeout_sec
        )

        self._soft_timeout_sec_map: Dict[str, int] = (
            dict(LLM_PROVIDER_SOFT_TIMEOUT_SEC or {})
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _provider_soft_timeout_sec(self, provider_id: str) -> Optional[int]:
        """读取单 Provider 软超时（0/None/False → 关闭）。"""
        raw = self._soft_timeout_sec_map.get(provider_id)
        if not raw:
            return None
        try:
            v = int(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _call_with_soft_timeout(
        self,
        provider_id: str,
        instance: BaseLLMProvider,
        *,
        prompt: str,
        temperature: Optional[float],
        max_tokens: Optional[int],
        task_type_hint: Optional[str],
        force_model: Optional[str],
    ) -> str:
        """单 Provider 调用（支持软超时）。

        实现要点（P1-2(a)）：
          - 若该 provider 配置了 timeout_soft_sec 且 > 0，则把实际 HTTP 请求
            放到后台线程；wall-clock 超过 soft_sec 即对主流程视为失败，
            Router 立刻尝试下一个 fallback。
          - 后台线程会继续跑（不会真的中断 urllib3 连接），但这对用户没影响：
            我们只关心第一个返回的有效结果。
          - 软超时设为 None / 0 → 退化为直接同步调用（完全和原行为一致）。
        """
        soft_sec = self._provider_soft_timeout_sec(provider_id)
        if soft_sec is None:
            return instance.chat_completion(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                task_type_hint=task_type_hint,
                force_model=force_model,
            )

        def _worker() -> str:
            return instance.chat_completion(
                prompt,
                temperature=temperature,
                max_tokens=max_tokens,
                task_type_hint=task_type_hint,
                force_model=force_model,
            )

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"llm-{provider_id}",
        )
        try:
            future = executor.submit(_worker)
            try:
                return future.result(timeout=soft_sec)
            except concurrent.futures.TimeoutError as exc:
                logger.warning(
                    "Provider soft timeout | provider=%s | wall_clock>=%ss -> 切换 fallback",
                    provider_id, soft_sec,
                )
                raise TimeoutError(
                    f"{provider_id} 软超时 {soft_sec}s"
                ) from exc
        finally:
            # 不 wait 已超时的 worker（它会在后台自然结束），
            # 但释放 executor 对象避免泄漏。
            executor.shutdown(wait=False, cancel_futures=False)

    # ------------------------------------------------------------------
    # Enabled Provider
    # ------------------------------------------------------------------

    def _is_enabled(
        self,
        provider_id: str,
    ) -> bool:

        cfg = self._provider_configs.get(
            provider_id
        )

        if not cfg:
            return False

        return bool(
            cfg.get(
                "enabled",
                True,
            )
        )

    # ------------------------------------------------------------------
    # Get / Create Provider
    # ------------------------------------------------------------------

    def _get_or_create(
        self,
        provider_id: str,
    ) -> BaseLLMProvider:

        if provider_id not in self._provider_configs:

            raise KeyError(
                f"Unknown LLM provider_id="
                f"{provider_id!r}. "
                f"Valid: "
                f"{sorted(self._provider_configs.keys())}"
            )

        cfg = self._provider_configs[
            provider_id
        ]

        if not cfg.get(
            "enabled",
            True,
        ):

            raise RuntimeError(
                f"LLM provider "
                f"{provider_id!r} "
                f"is NOT enabled "
                f"(missing env var "
                f"{cfg.get('api_key_env')!r}?)"
            )

        if provider_id not in PROVIDER_CLASSES:

            raise KeyError(
                f"No implementation class "
                f"registered for provider_id="
                f"{provider_id!r}. "
                f"Registered classes: "
                f"{sorted(PROVIDER_CLASSES.keys())}"
            )

        if provider_id not in self._instances:

            cls = PROVIDER_CLASSES[
                provider_id
            ]

            self._instances[
                provider_id
            ] = cls(cfg)

        return self._instances[
            provider_id
        ]

    # ------------------------------------------------------------------
    # Auto Select
    # ------------------------------------------------------------------

    def _auto_select(
        self,
        prompt: str,
        task_type_hint: Optional[str],
    ) -> str:

        long_types = self.auto_route.get(
            "long_task_types",
            [],
        )

        short_id = self.auto_route.get(
            "short_provider",
            "lm_studio_local",
        )

        long_id = self.auto_route.get(
            "long_provider",
            "deepseek_api",
        )

        threshold = int(
            self.auto_route.get(
                "short_prompt_chars",
                8000,
            )
        )

        # --------------------------------------------------------------
        # 1. Explicit long task
        # --------------------------------------------------------------

        if (
            task_type_hint
            and task_type_hint in long_types
        ):

            candidate = long_id

        # --------------------------------------------------------------
        # 2. Prompt length
        # --------------------------------------------------------------

        elif len(prompt or "") >= threshold:

            candidate = long_id

        # --------------------------------------------------------------
        # 3. Normal short task
        # --------------------------------------------------------------

        else:

            candidate = short_id

        # --------------------------------------------------------------
        # Candidate enabled?
        # --------------------------------------------------------------

        if self._is_enabled(candidate):

            return candidate

        # --------------------------------------------------------------
        # Candidate unavailable.
        #
        # Find first enabled provider from fallback chain.
        # --------------------------------------------------------------

        logger.warning(
            "Auto-route candidate "
            "%s is disabled; searching "
            "enabled fallback providers.",
            candidate,
        )

        for pid in self.fallback_chain:

            if self._is_enabled(pid):

                logger.info(
                    "Auto-route switched "
                    "from %s to %s",
                    candidate,
                    pid,
                )

                return pid

        # 最后尝试所有已配置 Provider。
        for pid in self._provider_configs:

            if self._is_enabled(pid):

                logger.info(
                    "Auto-route switched "
                    "from %s to %s",
                    candidate,
                    pid,
                )

                return pid

        raise RuntimeError(
            "没有任何启用的 LLM Provider。"
        )

    # ------------------------------------------------------------------
    # Select Provider
    # ------------------------------------------------------------------

    def select_provider(
        self,
        prompt: str,
        *,
        task_type_hint: Optional[str] = None,
        provider_hint: Optional[str] = None,
    ) -> Tuple[
        str,
        BaseLLMProvider,
    ]:

        # --------------------------------------------------------------
        # 1. 调用方显式指定
        # --------------------------------------------------------------

        if provider_hint:

            return (
                provider_hint,
                self._get_or_create(
                    provider_hint
                ),
            )

        # --------------------------------------------------------------
        # 2. 全局强制 Provider
        # --------------------------------------------------------------

        if (
            self.default_provider
            and self.default_provider != "auto"
        ):

            return (
                self.default_provider,
                self._get_or_create(
                    self.default_provider
                ),
            )

        # --------------------------------------------------------------
        # 3. Auto
        # --------------------------------------------------------------

        chosen_id = self._auto_select(
            prompt,
            task_type_hint,
        )

        return (
            chosen_id,
            self._get_or_create(
                chosen_id
            ),
        )

    # ------------------------------------------------------------------
    # Provider Attempt Order
    # ------------------------------------------------------------------

    def _build_attempt_order(
        self,
        main_id: str,
    ) -> List[str]:

        order: List[str] = []

        seen = set()

        # 主 Provider
        if main_id not in seen:
            order.append(main_id)
            seen.add(main_id)

        # fallback
        for pid in self.fallback_chain:

            if pid in seen:
                continue

            # disabled Provider 不进入实际尝试。
            if not self._is_enabled(pid):
                continue

            order.append(pid)
            seen.add(pid)

        return order

    # ------------------------------------------------------------------
    # Main Completion
    # ------------------------------------------------------------------

    def chat_completion(
        self,
        prompt: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        task_type_hint: Optional[str] = None,
        provider_hint: Optional[str] = None,
        force_model: Optional[str] = None,
    ) -> str:

        overall_started = (
            time.perf_counter()
        )

        main_id, _ = (
            self.select_provider(
                prompt,
                task_type_hint=task_type_hint,
                provider_hint=provider_hint,
            )
        )

        order = (
            self._build_attempt_order(
                main_id
            )
        )

        if not order:

            raise RuntimeError(
                "没有可用的 LLM Provider。"
            )

        attempts: List[str] = []

        last_exc: Optional[
            BaseException
        ] = None

        # --------------------------------------------------------------
        # Attempt providers
        # --------------------------------------------------------------

        for pid in order:

            elapsed = (
                time.perf_counter()
                - overall_started
            )

            if (
                elapsed
                >= self.total_timeout_sec
            ):

                logger.error(
                    "LLM total timeout "
                    "after %.1fs; "
                    "remaining providers aborted.",
                    elapsed,
                )

                break

            attempts.append(pid)

            try:

                instance = (
                    self._get_or_create(pid)
                )

                result = self._call_with_soft_timeout(
                    pid,
                    instance,
                    prompt=prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    task_type_hint=(
                        task_type_hint
                    ),
                    force_model=force_model,
                )

                # ------------------------------------------------------
                # Successful fallback
                # ------------------------------------------------------

                if len(attempts) > 1:

                    logger.info(
                        "LLM fallback SUCCESS | "
                        "attempts=%s | chain=%s | "
                        "final_provider=%s",
                        len(attempts),
                        " -> ".join(attempts),
                        pid,
                    )

                return result

            except Exception as exc:

                last_exc = exc

                logger.warning(
                    "Provider attempt failed | "
                    "provider=%s | "
                    "attempt=%s/%s | "
                    "error=%s",
                    pid,
                    len(attempts),
                    len(order),
                    type(exc).__name__,
                )

                continue

        # ------------------------------------------------------------------
        # All failed
        # ------------------------------------------------------------------

        total_ms = (
            time.perf_counter()
            - overall_started
        ) * 1000

        logger.error(
            "All LLM providers failed | "
            "attempts=%s | "
            "total=%.0fms | "
            "last_err=%s",
            " -> ".join(attempts),
            total_ms,
            (
                type(last_exc).__name__
                if last_exc
                else "N/A"
            ),
            exc_info=last_exc,
        )

        attempted_text = (
            " -> ".join(attempts)
            if attempts
            else "none"
        )

        raise RuntimeError(
            "所有 LLM Provider 均调用失败。"
            f"已尝试：{attempted_text}。"
            "请检查 LM Studio 是否启动，"
            "以及 DEEPSEEK_API_KEY 是否正确配置。"
            f"原始错误：{last_exc!r}"
        ) from last_exc

    # ------------------------------------------------------------------
    # Streaming Completion
    # ------------------------------------------------------------------

    def chat_completion_stream(
        self,
        prompt: str,
        *,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        task_type_hint: Optional[str] = None,
        provider_hint: Optional[str] = None,
        force_model: Optional[str] = None,
        on_delta: Optional[Callable[[str], None]] = None,
    ) -> str:
        """流式补全：复用 fallback 链，返回完整文本；on_delta 逐段回调（预览用途）。

        - 熔断语义：provider 的读超时（首 token / 相邻 chunk 静默超限）→
          立即切换下一个 fallback，无需等墙钟总超时
        - 注意：若某 provider 在输出中途失败，已通过 on_delta 推送的
          「中途增量」与最终返回文本可能不一致（预览语义）。
          最终答案以返回值为准（管线层 Guard 基于完整文本校验）。
        """
        overall_started = (
            time.perf_counter()
        )

        main_id, _ = (
            self.select_provider(
                prompt,
                task_type_hint=task_type_hint,
                provider_hint=provider_hint,
            )
        )

        order = (
            self._build_attempt_order(
                main_id
            )
        )

        if not order:

            raise RuntimeError(
                "没有可用的 LLM Provider。"
            )

        attempts: List[str] = []

        last_exc: Optional[
            BaseException
        ] = None

        for pid in order:

            elapsed = (
                time.perf_counter()
                - overall_started
            )

            if (
                elapsed
                >= self.total_timeout_sec
            ):

                logger.error(
                    "LLM stream total timeout "
                    "after %.1fs; "
                    "remaining providers aborted.",
                    elapsed,
                )

                break

            attempts.append(pid)

            pieces: List[str] = []

            try:

                instance = (
                    self._get_or_create(pid)
                )

                for piece in instance.chat_completion_stream(
                    prompt,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    task_type_hint=(
                        task_type_hint
                    ),
                    force_model=force_model,
                ):
                    pieces.append(piece)

                    if on_delta is not None:

                        try:
                            on_delta(piece)
                        except Exception:
                            logger.exception(
                                "on_delta callback failed (ignored)"
                            )

                text = "".join(pieces)

                if not text.strip():

                    raise RuntimeError(
                        f"Provider {pid} 流式返回为空。"
                    )

                if len(attempts) > 1:

                    logger.info(
                        "LLM stream fallback SUCCESS | "
                        "attempts=%s | "
                        "final_provider=%s",
                        len(attempts),
                        pid,
                    )

                return text

            except Exception as exc:

                last_exc = exc

                got = sum(
                    len(p) for p in pieces
                )

                logger.warning(
                    "Provider stream attempt failed | "
                    "provider=%s | "
                    "attempt=%s/%s | "
                    "got_chars=%s | "
                    "error=%s",
                    pid,
                    len(attempts),
                    len(order),
                    got,
                    type(exc).__name__,
                )

                continue

        total_ms = (
            time.perf_counter()
            - overall_started
        ) * 1000

        logger.error(
            "All LLM providers failed (stream) | "
            "attempts=%s | "
            "total=%.0fms | "
            "last_err=%s",
            " -> ".join(attempts),
            total_ms,
            (
                type(last_exc).__name__
                if last_exc
                else "N/A"
            ),
            exc_info=last_exc,
        )

        attempted_text = (
            " -> ".join(attempts)
            if attempts
            else "none"
        )

        raise RuntimeError(
            "所有 LLM Provider 均调用失败。"
            f"已尝试：{attempted_text}。"
            "请检查 LM Studio 是否启动，"
            "以及 DEEPSEEK_API_KEY 是否正确配置。"
            f"原始错误：{last_exc!r}"
        ) from last_exc


# ============================================================================
# Default Router
# ============================================================================

def _build_default_router() -> LLMProviderRouter:

    return LLMProviderRouter(
        LLM_PROVIDERS,
        default_provider=LLM_PROVIDER,
        auto_route=AUTO_ROUTE,
        fallback_chain=LLM_FALLBACK_CHAIN,
        total_timeout_sec=(
            LLM_TOTAL_TIMEOUT_SEC
        ),
    )


DEFAULT_ROUTER: LLMProviderRouter = (
    _build_default_router()
)


# ============================================================================
# Backward Compatible Top-Level Function
# ============================================================================

def chat_completion(
    prompt,
    temperature: Optional[float] = 0.2,
    max_tokens: Optional[int] = 1200,
):
    """
    与旧版完全兼容。

    原有调用：

        chat_completion(prompt)

    或：

        chat_completion(
            prompt,
            temperature=0.2,
            max_tokens=1200,
        )

    不需要修改 server.py / legal_answer.py。
    """

    return DEFAULT_ROUTER.chat_completion(
        prompt,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def chat_completion_stream(
    prompt,
    on_delta: Optional[Callable[[str], None]] = None,
    temperature: Optional[float] = 0.2,
    max_tokens: Optional[int] = 1200,
):
    """
    与 chat_completion 相同路由语义的流式版本。

        chat_completion_stream(prompt, on_delta=callback) -> str

    逐段回调 on_delta(piece)（预览用途），返回完整文本。
    """

    return DEFAULT_ROUTER.chat_completion_stream(
        prompt,
        temperature=temperature,
        max_tokens=max_tokens,
        on_delta=on_delta,
    )
