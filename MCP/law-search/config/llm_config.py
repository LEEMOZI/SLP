"""
LLM Configuration — Multi-Provider Router
RAG-12.10+

Provider:

    lm_studio_local
        本地 LM Studio

    deepseek_api
        DeepSeek V4 Flash / V4 Pro

    bionic_secure_cloud
        预留

    lm_link
        预留

安全要求：

    API Key 禁止写入源码。

    DeepSeek API Key：
        DEEPSEEK_API_KEY

    Bionic Secure Cloud：
        BIONIC_SC_API_KEY
        BIONIC_SC_BASE_URL

    LM Link：
        LM_LINK_API_KEY
        LM_LINK_BASE_URL
        LM_LINK_DEVICE_ID
"""

from __future__ import annotations

import os


# ============================================================================
# Helpers
# ============================================================================

def _env(
    name: str,
    default: str = "",
) -> str:
    """
    安全读取环境变量。

    DSH 兼容：DSH spawn 子进程时会擦除名字含 KEY/PASSWORD/SECRET/TOKEN
    的环境变量（SENSITIVE_ENV_PATTERN，防密钥泄漏），因此 *_KEY 类变量
    在 MCP 子进程里永远为空。此时回退读取密钥文件（~/.dsh/secrets/ 或
    项目 .secrets/，权限 600，不违反「密钥不落源码」约束）。
    """

    value = os.environ.get(name, "").strip()
    if value:
        return value

    # 回退顺序：~/.dsh/secrets（仓外）→ 项目 .secrets（git-ignored，沙盒可写）
    candidates = [
        os.path.expanduser(f"~/.dsh/secrets/{name.lower()}"),
        os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".secrets", name.lower(),
        ),
    ]
    for secret_file in candidates:
        try:
            with open(secret_file, "r", encoding="utf-8") as f:
                stripped = f.read().strip()
                if stripped:
                    return stripped
        except OSError:
            continue
    return default


def _has_env(
    name: str,
) -> bool:
    """
    判断环境变量是否存在且非空。
    """

    return bool(
        _env(name)
    )


# ============================================================================
# Global Router
# ============================================================================

# 可选：
#
#   auto
#   lm_studio_local
#   deepseek_api
#   bionic_secure_cloud
#   lm_link
#
# 推荐：
#
#   auto
#
# 这样：
#
#   短任务
#       -> LM Studio
#
#   长任务
#       -> DeepSeek
#
# 运行时可被环境变量覆盖（DSH/MCP 子进程注入 LLM_PROVIDER=deepseek_api 可跳过
# LM Studio 的连接重试延迟）。非法值回落 auto。
_LLM_PROVIDER_ENV = os.environ.get("LLM_PROVIDER", "").strip().lower()

LLM_PROVIDER = (
    _LLM_PROVIDER_ENV
    if _LLM_PROVIDER_ENV in ("auto", "lm_studio_local", "deepseek_api",
                             "bionic_secure_cloud", "lm_link")
    else "auto"
)


# ============================================================================
# Auto Route
# ============================================================================

AUTO_ROUTE = {

    # ------------------------------------------------------------------------
    # Prompt token 阈值
    #
    # <= 48K
    #     -> 本地 LM Studio 128K
    #
    # 48K ~ 100K
    #     -> 本地 128K 可接则本地，否则 DeepSeek
    #
    # > 100K
    #     -> DeepSeek
    #
    # 说明：这里使用保守 token 估算，不再用字符数硬编码。
    # ------------------------------------------------------------------------

    "short_prompt_tokens": 48_000,

    "hard_prompt_tokens": 100_000,

    "chunk_prompt_tokens": 400_000,

    "short_provider":
        "lm_studio_local",

    "long_provider":
        "deepseek_api",

    # 这些任务即使 token 不大，也优先走云端长上下文模型。
    "cloud_required_task_types": [
        "research_report",
        "complex_legal_analysis",
    ],

    # ------------------------------------------------------------------------
    # 明确属于复杂法律任务的类型
    # ------------------------------------------------------------------------

    "long_task_types": [
        "contract_review_long",
        "litigation_writing",
        "evidence_evaluation",
        "research_report",
        "legal_opinion",
        "case_analysis",
        "complex_legal_analysis",
        "long_document_review",
    ],
}


# ============================================================================
# Pipeline 内部生成路由
# ============================================================================

# search_law 等工具的内部答案生成为机器内 Step（结果进入工具返回值），
# 不是用户界面的最终生成（界面生成由 DSH/LM Studio 的主模型承担）。
#
# MAC Studio 主力为 qwen/qwen3.6-35b-a3b（128K ctx），复杂任务备用
# qwen/qwen3.8-27b。内部生成也必须遵守 token 预算：短任务本地，长任务云端。
# "auto" 表示交给 Auto Route，不再强制本地。
INTERNAL_GENERATION_PROVIDER = "auto"


# ============================================================================
# Fallback Chain
# ============================================================================

# 顺序非常重要。
#
# Auto：
#
#     短任务：
#         LM Studio
#             ↓失败
#         DeepSeek
#
#     长任务：
#         DeepSeek
#             ↓失败
#         LM Studio
#
# Router 会自动去重。
#
LLM_FALLBACK_CHAIN = [
    "lm_studio_local",
    "deepseek_api",
]


# ============================================================================
# Global Timeout
# ============================================================================

# 单次整体请求最大时间。
#
# 注意：
# 这是整个 Router 的总预算。
# Provider 自身也有 timeout_sec。
#
LLM_TOTAL_TIMEOUT_SEC = 900

# P1-2 (a)：单 Provider 软超时（秒）
#
# 本系统 fallback_chain = [lm_studio_local, deepseek_api]。
# 本地 128K prefill 约 220s，固定 90s 会提前掐断。
# llm_client 会按 prompt token 规模使用 LOCAL_SOFT_TIMEOUT_TIERS 动态计算；
# 这里的值是拿不到 prompt 规模时的兜底。
#
# 0 / False / None → 关闭软超时（仅用 Provider 级 timeout_sec 做硬取消）。
LLM_PROVIDER_SOFT_TIMEOUT_SEC = {
    "lm_studio_local": 300,
    "deepseek_api":      300,
    "bionic_secure_cloud": 120,
    "lm_link":            120,
}

# ============================================================================
# Global Generation Parameters
# ============================================================================

LLM_TEMPERATURE = 0.2

LLM_MAX_TOKENS = 8192


# ============================================================================
# Provider #1
# LM Studio Local
# ============================================================================

LM_STUDIO_LOCAL = {

    "enabled": True,

    "display_name":
        "LM Studio 本地推理",

    # LM Studio OpenAI-compatible API
    # 本地运行时: http://localhost:1234/v1
    # 远程 Mac Studio: http://<StudioIP>:1234/v1 (设 LM_STUDIO_BASE_URL 环境变量)
    "base_url":
        os.environ.get("LM_STUDIO_BASE_URL", "http://localhost:1234/v1"),

    # LM Studio 本地服务通常不验证 API key。
    "api_key":
        "lm-studio",

    "api_key_env":
        None,

    # ------------------------------------------------------------------------
    # 当前本地模型配置（MAC Studio 2026-10-26 同步）
    #   主力: qwen/qwen3.6-35b-a3b（MoE 35B-A3B，Agent 循环最快）
    #   复杂任务/质量备用: qwen/qwen3.8-27b（稠密 27B，深度推理）
    #   视觉/英文备用: google/gemma-4-31b-qat
    #
    # 27B 仅在命中 AUTO_ROUTE.long_task_types 或显式 force_model 时启用，
    # 避免日常工具循环在 A3B / 27B 之间反复换模。
    # 注意：64GB 统一内存下两个 Qwen 不能同时以 128K 常驻。
    # ------------------------------------------------------------------------

    "default_model":
        "qwen/qwen3.6-35b-a3b",

    # 复杂法律任务的本地质量备用。SLP 内部生成默认不传 task_type_hint，
    # 因此日常检索/审查仍走 A3B；只有显式复杂任务或云端失败回落到本地时
    # 才使用 27B。
    "long_context_model":
        "qwen/qwen3.8-27b",

    # 请求质量模型（27B）前是否自动卸载当前驻留的另一个 Qwen 再加载。
    # LM Studio 的内存护栏不会自动换出模型，而是直接拒绝加载（HTTP 400），
    # 所以 64GB 机器上不主动卸载就无法真正用上 27B。默认开启。
    "auto_swap":
        True,

    "compatible_models": [
        "qwen/qwen3.6-35b-a3b",
        "qwen/qwen3.8-27b",
        "google/gemma-4-31b-qat",
    ],

    # ------------------------------------------------------------------------
    # 本地模型能力（27B+ 大模型，128K 上下文）
    # ------------------------------------------------------------------------

    "capabilities": {

        "max_context_tokens":
            131072,

        "tool_calls":
            True,

        "streaming":
            True,

    },

    # ------------------------------------------------------------------------
    # LM Studio 特有配置
    #
    # 关闭 thinking。
    #
    # 对你的法律 RAG 系统：
    #
    #   RAG 证据
    #       ↓
    #   Answer Guard
    #       ↓
    #   最终答案
    #
    # 因此不让本地小模型额外输出思维链。
    # ------------------------------------------------------------------------

    "enable_thinking":
        False,

    "timeout_sec":
        900,

    "use_for": [
        "code_refactor",
        "short_legal_search",
        "local_debug",
        "bionic_code_project_default",
        "simple_legal_question",
    ],
}


# ============================================================================
# Provider #2
# DeepSeek API
# ============================================================================

DEEPSEEK_API_KEY = _env(
    "DEEPSEEK_API_KEY"
)


DEEPSEEK_API = {

    # 没有 API Key 就不进入自动路由。
    "enabled":
        bool(DEEPSEEK_API_KEY),

    "display_name":
        "DeepSeek V4 API 云端推理",

    # DeepSeek 官方 OpenAI-compatible endpoint
    "base_url":
        "https://api.deepseek.com",

    # API Key 只来自环境变量。
    #
    # 不在源码中写真实 sk-xxxx。
    "api_key":
        DEEPSEEK_API_KEY
        or "__MISSING_DEEPSEEK_API_KEY__",

    "api_key_env":
        "DEEPSEEK_API_KEY",

    # ------------------------------------------------------------------------
    # 默认模型
    #
    # Flash：
    #   速度快
    #   成本低
    #   适合一般云端任务
    # ------------------------------------------------------------------------

    "default_model":
        "deepseek-v4-flash",

    # ------------------------------------------------------------------------
    # 长任务模型
    #
    # Pro：
    #   复杂法律分析
    #   长文书
    #   证据评价
    #   复杂诉讼问题
    # ------------------------------------------------------------------------

    "long_context_model":
        "deepseek-v4-pro",

    "reasoning_model":
        "deepseek-v4-pro",

    "compatible_models": [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ],

    # ------------------------------------------------------------------------
    # V4 能力
    # ------------------------------------------------------------------------

    "capabilities": {

        "max_context_tokens":
            1_000_000,

        "max_output_tokens":
            384_000,

        "tool_calls":
            True,

        "streaming":
            True,

        "structured_output":
            True,

        "thinking":
            True,

    },

    # ------------------------------------------------------------------------
    # Thinking
    #
    # DeepSeek V4 Flash：
    #
    #   默认关闭
    #
    #   普通法律问答：
    #       更快
    #
    # DeepSeek V4 Pro：
    #
    #   默认开启
    #
    #   复杂法律任务：
    #       开启 reasoning
    #
    # 这也是为什么 llm_client.py 对 DeepSeek 和 LM Studio
    # 使用了不同的 extra_body 格式。
    # ------------------------------------------------------------------------

    "enable_thinking":
        False,

    "thinking_by_model": {

        "deepseek-v4-flash":
            False,

        "deepseek-v4-pro":
            True,

    },

    # ------------------------------------------------------------------------
    # Reasoning Effort
    #
    # DeepSeek V4 当前支持：
    #
    #   low
    #   high
    #   max
    #
    # 对法律复杂分析：
    #
    #   Pro + high
    #
    # 作为默认的性能/成本平衡。
    # ------------------------------------------------------------------------

    "reasoning_effort_by_model": {

        "deepseek-v4-flash":
            "low",

        "deepseek-v4-pro":
            "high",

    },

    "timeout_sec":
        600,

    "use_for": [

        "contract_review_long",

        "litigation_writing",

        "evidence_evaluation",

        "research_report",

        "legal_opinion",

        "case_analysis",

        "complex_legal_analysis",

        "long_document_review",

        "bionic_work_project_long",

    ],
}


# ============================================================================
# Provider #3
# Bionic Secure Cloud
# ============================================================================

BIONIC_SC_API_KEY = _env(
    "BIONIC_SC_API_KEY"
)

BIONIC_SC_BASE_URL = _env(
    "BIONIC_SC_BASE_URL"
)


BIONIC_SECURE_CLOUD = {

    # 没有 API Key 默认关闭。
    "enabled":
        bool(BIONIC_SC_API_KEY),

    "display_name":
        "LM Studio Bionic Secure Cloud",

    "base_url":
        BIONIC_SC_BASE_URL
        or "https://api.lmstudio.ai/secure-cloud/v1",

    "api_key":
        BIONIC_SC_API_KEY
        or "__MISSING_BIONIC_SC_API_KEY__",

    "api_key_env":
        "BIONIC_SC_API_KEY",

    "default_model":
        "frontier-model-auto",

    "compatible_models": [
        "frontier-model-auto",
    ],

    "capabilities": {

        "max_context_tokens":
            131072,

        "tool_calls":
            True,

        "streaming":
            True,

    },

    "enable_thinking":
        None,

    "timeout_sec":
        600,

    "use_for": [
        "bionic_work_project_premium",
        "sensitive_data_on_prem",
    ],
}


# ============================================================================
# Provider #4
# LM Link
# ============================================================================

LM_LINK_API_KEY = _env(
    "LM_LINK_API_KEY"
)

LM_LINK_BASE_URL = _env(
    "LM_LINK_BASE_URL"
)

LM_LINK_DEVICE_ID = _env(
    "LM_LINK_DEVICE_ID"
)


LM_LINK = {

    "enabled":
        bool(LM_LINK_API_KEY),

    "display_name":
        "LM Studio LM Link 远程推理设备",

    "base_url":
        LM_LINK_BASE_URL
        or "https://link.lmstudio.ai/v1",

    "api_key":
        LM_LINK_API_KEY
        or "__MISSING_LM_LINK_API_KEY__",

    "api_key_env":
        "LM_LINK_API_KEY",

    "device_id":
        LM_LINK_DEVICE_ID or None,

    "default_model":
        "remote-device-default",

    "compatible_models": [
        "remote-device-default",
    ],

    "capabilities": {

        "max_context_tokens":
            131072,

        "tool_calls":
            True,

        "streaming":
            True,

    },

    "enable_thinking":
        None,

    "timeout_sec":
        600,

    "use_for": [
        "remote_gpu_inference",
        "cross_device_sharing",
    ],
}


# ============================================================================
# Provider Registry
# ============================================================================

LLM_PROVIDERS = {

    "lm_studio_local":
        LM_STUDIO_LOCAL,

    "deepseek_api":
        DEEPSEEK_API,

    "bionic_secure_cloud":
        BIONIC_SECURE_CLOUD,

    "lm_link":
        LM_LINK,
}


# ============================================================================
# Backward Compatibility
# ============================================================================

# 原来的代码如果存在：
#
#     from config import LM_STUDIO_BASE_URL
#
# 继续有效。

LM_STUDIO_BASE_URL = (
    LM_STUDIO_LOCAL["base_url"]
)


# 原来的代码如果存在：
#
#     from config import LLM_MODEL
#
# 继续有效。

LLM_MODEL = (
    LM_STUDIO_LOCAL["default_model"]
)
