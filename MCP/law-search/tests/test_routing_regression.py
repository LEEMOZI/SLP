"""P1 路由回归：token 估算校准 + 路由/超时/分段阈值。

运行：
    /Users/simon/Documents/1-SLP/venv/bin/python tests/test_routing_regression.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP_DIR = tempfile.mkdtemp(prefix="law-search-routing-")
os.environ["LAW_SEARCH_OBS_DB"] = str(Path(_TMP_DIR) / "agent_runs.db")

from config import (  # noqa: E402
    AUTO_ROUTE,
    LEGAL_SYSTEM_PROMPT,
    LLM_PROVIDERS,
    PROMPT_CHUNK_THRESHOLD_TOKENS,
    TOKEN_ESTIMATE_SAFETY_FACTOR,
)
from core.llm_client import BaseLLMProvider, LLMProviderRouter  # noqa: E402
from core.llm_client import LMStudioLocalProvider  # noqa: E402

PDF = Path(
    "/Users/simon/Documents/2-WorkSpace/2-DeepSeek Harness/4-案件分析梳理/"
    "（2026）京03民终13295号债权人撤销权判决书.pdf"
)

# A/B 基准中同一组消息实测 prompt_tokens=15436（含 system + instruction）
MEASURED_PROMPT_TOKENS = 15_436

INSTRUCTION = (
    "请阅读以下卷宗材料，完成：\n"
    "1. 提取本案案号、当事人、争议焦点；\n"
    "2. 概括裁判结果与核心理由；\n"
    "3. 用 200 字总结裁判要旨。\n\n"
    "【卷宗开始】\n{body}\n【卷宗结束】"
)


def _prompt_with_tokens(target: int) -> str:
    """生成恰好落在 target 估算 token 附近的中文 prompt。"""
    chars = max(1, int(target / (0.5 * TOKEN_ESTIMATE_SAFETY_FACTOR)))
    return "法" * chars


def _router() -> LLMProviderRouter:
    return LLMProviderRouter(
        LLM_PROVIDERS,
        default_provider="auto",
        auto_route=AUTO_ROUTE,
        fallback_chain=["lm_studio_local", "deepseek_api"],
    )


class EstimationCalibrationTest(unittest.TestCase):
    def test_cjk_weight(self):
        # 1000 个汉字 → 1000 * 0.5 * 1.5 = 750 tokens
        self.assertEqual(BaseLLMProvider.estimate_prompt_tokens("法" * 1000), 750)

    @unittest.skipUnless(PDF.exists(), "真实判决 PDF 不存在")
    def test_real_judgment_calibration(self):
        try:
            import fitz
        except ImportError:
            self.skipTest("PyMuPDF 未安装")

        doc = "\n".join(page.get_text() for page in fitz.open(PDF)).strip()
        prompt = INSTRUCTION.format(body=doc)
        estimated = BaseLLMProvider.estimate_prompt_tokens(
            LEGAL_SYSTEM_PROMPT + prompt
        )
        error = (estimated - MEASURED_PROMPT_TOKENS) / MEASURED_PROMPT_TOKENS
        # 校准目标：与实测值偏差在 ±10% 以内（旧估算约 -35%）
        self.assertLess(abs(error), 0.10, f"estimated={estimated} error={error:.1%}")


class RoutingThresholdTest(unittest.TestCase):
    def test_short_prompt_local(self):
        router = _router()
        self.assertEqual(router._auto_select(_prompt_with_tokens(10_000), None),
                         "lm_studio_local")

    def test_soft_limit_local(self):
        router = _router()
        self.assertEqual(router._auto_select(_prompt_with_tokens(45_000), None),
                         "lm_studio_local")

    def test_medium_prompt_local_when_128k(self):
        router = _router()
        self.assertEqual(router._auto_select(_prompt_with_tokens(60_000), None),
                         "lm_studio_local")

    def test_hard_prompt_cloud(self):
        router = _router()
        self.assertEqual(router._auto_select(_prompt_with_tokens(120_000), None),
                         "deepseek_api")

    def test_just_below_chunk_cloud(self):
        router = _router()
        self.assertEqual(router._auto_select(_prompt_with_tokens(380_000), None),
                         "deepseek_api")

    def test_long_task_type_cloud(self):
        router = _router()
        for task in AUTO_ROUTE.get("cloud_required_task_types", []):
            self.assertEqual(
                router._auto_select(_prompt_with_tokens(5_000), task),
                "deepseek_api",
            )

    def test_provider_hint(self):
        router = _router()
        short = _prompt_with_tokens(5_000)
        self.assertEqual(router.select_provider(short, provider_hint="auto")[0],
                         "lm_studio_local")
        self.assertEqual(router.select_provider(short, provider_hint="deepseek_api")[0],
                         "deepseek_api")


class SoftTimeoutTest(unittest.TestCase):
    def test_local_tiers_use_calibrated_tokens(self):
        router = _router()
        self.assertEqual(router._provider_soft_timeout_sec("lm_studio_local", 10_000), 120)
        self.assertEqual(router._provider_soft_timeout_sec("lm_studio_local", 50_000), 300)
        self.assertEqual(router._provider_soft_timeout_sec("lm_studio_local", 100_000), 600)


class LocalModelConfigTest(unittest.TestCase):
    def test_primary_and_quality_models(self):
        cfg = LLM_PROVIDERS["lm_studio_local"]
        self.assertEqual(cfg["default_model"], "qwen/qwen3.6-35b-a3b")
        self.assertEqual(cfg.get("long_context_model"), "qwen/qwen3.8-27b")
        self.assertIn("google/gemma-4-31b-qat", cfg["compatible_models"])
        self.assertNotIn("prism-ml/bonsai-27b", cfg["compatible_models"])
        self.assertNotIn("meta/muse-glimmer", cfg["compatible_models"])

    def test_model_selection_primary_vs_quality(self):
        provider = LMStudioLocalProvider(dict(LLM_PROVIDERS["lm_studio_local"]))
        self.assertEqual(
            provider.choose_model(1_000, None), "qwen/qwen3.6-35b-a3b"
        )
        self.assertEqual(
            provider.choose_model(1_000, "complex_legal_analysis"),
            "qwen/qwen3.8-27b",
        )


class QualityModelTest(unittest.TestCase):
    def test_quality_selects_27b(self):
        provider = LMStudioLocalProvider(dict(LLM_PROVIDERS["lm_studio_local"]))
        provider.ensure_model_ready = lambda model: None  # 不触真实 LM Studio
        model, _ = provider._prepare_request(
            "短问题", temperature=0, max_tokens=64,
            task_type_hint=None, force_model=None, quality=True,
        )
        self.assertEqual(model, "qwen/qwen3.8-27b")

    def test_quality_off_keeps_primary(self):
        provider = LMStudioLocalProvider(dict(LLM_PROVIDERS["lm_studio_local"]))
        provider.ensure_model_ready = lambda model: None
        model, _ = provider._prepare_request(
            "短问题", temperature=0, max_tokens=64,
            task_type_hint=None, force_model=None, quality=False,
        )
        self.assertEqual(model, "qwen/qwen3.6-35b-a3b")

    def test_router_quality_maps_to_local_model(self):
        router = _router()
        self.assertEqual(
            router._resolve_quality_model(None, True), "qwen/qwen3.8-27b"
        )
        self.assertIsNone(router._resolve_quality_model(None, False))

    def test_force_model_not_leaked_to_cloud(self):
        router = _router()
        self.assertIsNone(
            router._local_only_model("deepseek_api", "qwen/qwen3.8-27b")
        )
        self.assertEqual(
            router._local_only_model("lm_studio_local", "qwen/qwen3.8-27b"),
            "qwen/qwen3.8-27b",
        )


class ThinkingSwitchTest(unittest.TestCase):
    def test_local_thinking_off_appends_no_think(self):
        provider = LMStudioLocalProvider(dict(LLM_PROVIDERS["lm_studio_local"]))
        _, kwargs = provider._prepare_request(
            "测试问题", temperature=0, max_tokens=64,
            task_type_hint=None, force_model=None,
        )
        content = kwargs["messages"][1]["content"]
        self.assertTrue(content.endswith("/no_think"))
        self.assertFalse(
            kwargs["extra_body"]["chat_template_kwargs"]["enable_thinking"]
        )

    def test_local_thinking_on_does_not_append(self):
        cfg = dict(LLM_PROVIDERS["lm_studio_local"])
        cfg["enable_thinking"] = True
        provider = LMStudioLocalProvider(cfg)
        _, kwargs = provider._prepare_request(
            "测试问题", temperature=0, max_tokens=64,
            task_type_hint=None, force_model=None,
        )
        self.assertFalse(kwargs["messages"][1]["content"].endswith("/no_think"))


class MapReduceRoutingTest(unittest.TestCase):
    def test_below_chunk_no_map_reduce(self):
        router = _router()
        seen = []

        def fake_call(provider_id, instance, *, prompt, **kwargs):
            seen.append(prompt)
            return "PLAIN"

        original = router._call_with_soft_timeout
        router._call_with_soft_timeout = fake_call  # type: ignore[assignment]
        try:
            text = router.chat_completion(_prompt_with_tokens(380_000))
        finally:
            router._call_with_soft_timeout = original  # type: ignore[assignment]

        self.assertEqual(text, "PLAIN")
        self.assertEqual(len(seen), 1)

    def test_above_chunk_map_reduce(self):
        router = _router()
        seen = []

        def fake_call(provider_id, instance, *, prompt, **kwargs):
            seen.append(prompt)
            if "【材料分段要点" in prompt:
                return "FINAL"
            return "要点：案号（2026）京03民终13295号。"

        original = router._call_with_soft_timeout
        router._call_with_soft_timeout = fake_call  # type: ignore[assignment]
        try:
            text = router.chat_completion(_prompt_with_tokens(450_000))
        finally:
            router._call_with_soft_timeout = original  # type: ignore[assignment]

        self.assertEqual(text, "FINAL")
        self.assertGreater(len(seen), 1)
        # 每个分段（含模板/背景）都应明显小于 128K 本地上下文
        for prompt in seen:
            self.assertLess(
                BaseLLMProvider.estimate_prompt_tokens(prompt),
                PROMPT_CHUNK_THRESHOLD_TOKENS,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
