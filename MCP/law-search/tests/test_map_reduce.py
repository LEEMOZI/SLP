"""P1 map-reduce 回归测试。

运行：
    python tests/test_map_reduce.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 观测库隔离到临时目录，避免污染生产 data/agent_runs.db
_TMP_DIR = tempfile.mkdtemp(prefix="law-search-mapreduce-")
os.environ["LAW_SEARCH_OBS_DB"] = str(Path(_TMP_DIR) / "agent_runs.db")

import core.map_reduce as mr  # noqa: E402
from config import (  # noqa: E402
    AUTO_ROUTE,
    LLM_PROVIDERS,
    PROMPT_CHUNK_THRESHOLD_TOKENS,
)
from core.llm_client import LLMProviderRouter  # noqa: E402


def _sample_text(paragraphs: int = 2400) -> str:
    """构造约 90 万字符 / 46 万 token 的中文材料，模拟超长案卷。"""
    body = (
        "本院认为，上诉人主张的债权人撤销权是否成立，应当结合合同签订时间、"
        "款项支付凭证、当事人陈述以及（2026）京03民终13295号案件的既有认定综合判断。"
        "涉案金额为人民币128万元，付款日期为2024年3月15日。"
    ) * 5
    return "\n".join(
        f"第{i + 1}段：{body}" for i in range(paragraphs)
    )


class SplitTextTest(unittest.TestCase):
    def test_chunks_respect_limit(self):
        text = _sample_text()
        chunks = mr.split_text(text, mr.CHUNK_TOKENS)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(mr.estimate_tokens(chunk), mr.CHUNK_TOKENS)
        self.assertEqual("".join(chunks), text)

    def test_hard_split_single_huge_line(self):
        line = "法" * 200_000  # 单行 20 万汉字，超过单段上限
        chunks = mr.split_text(line, 10_000)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(mr.estimate_tokens(chunk), 10_000)
        self.assertEqual("".join(chunks), line)


class RunMapReduceTest(unittest.TestCase):
    def test_map_reduce_flow(self):
        prompts = []

        def fake_call(prompt, **kwargs):
            prompts.append(prompt)
            if "【材料分段要点" in prompt:
                return "FINAL_ANSWER"
            return "要点：案号（2026）京03民终13295号，金额128万元。"

        result = mr.run_map_reduce(_sample_text(), fake_call, task_type_hint="case_analysis")

        self.assertGreater(result.chunks, 1)
        self.assertFalse(result.degraded)
        self.assertEqual(result.text, "FINAL_ANSWER")
        self.assertEqual(len(prompts), result.chunks + 1)
        self.assertIn("（2026）京03民终13295号", prompts[0])

    def test_degraded_footer(self):
        def fake_call(prompt, **kwargs):
            if "【材料分段要点" in prompt:
                return "FINAL_ANSWER"
            # 第 1 段的两次尝试都失败，触发降级
            if "第 1/" in prompt[:60]:
                raise RuntimeError("boom")
            return "要点"

        result = mr.run_map_reduce(_sample_text(), fake_call)
        self.assertTrue(result.degraded)
        self.assertIn(mr.DEGRADED_FOOTER.strip()[:10], result.text)

    def test_recursive_reduce_capped(self):
        old_limit = mr.REDUCE_INPUT_TOKENS
        mr.REDUCE_INPUT_TOKENS = 50_000
        try:
            def fake_call(prompt, **kwargs):
                if "【材料分段要点" in prompt:
                    return "FINAL_ANSWER"
                # 每段产出约 3 万 token 要点，迫使要点层递归压缩
                return "要点" * 30_000

            result = mr.run_map_reduce("法" * 200_000, fake_call)
            self.assertGreaterEqual(result.levels, 2)
            self.assertLessEqual(result.levels, mr.MAX_LEVELS)
            self.assertEqual(result.text, "FINAL_ANSWER")
        finally:
            mr.REDUCE_INPUT_TOKENS = old_limit


class RouterIntegrationTest(unittest.TestCase):
    def test_threshold_triggers_map_reduce(self):
        router = LLMProviderRouter(
            LLM_PROVIDERS,
            default_provider="auto",
            auto_route=AUTO_ROUTE,
            fallback_chain=["lm_studio_local", "deepseek_api"],
        )
        seen = []

        def fake_call(provider_id, instance, *, prompt, **kwargs):
            seen.append(mr.estimate_tokens(prompt))
            if "【材料分段要点" in prompt:
                return "ROUTER_FINAL"
            return "要点"

        original = router._call_with_soft_timeout
        router._call_with_soft_timeout = fake_call  # type: ignore[assignment]
        try:
            text = router.chat_completion(_sample_text())
        finally:
            router._call_with_soft_timeout = original  # type: ignore[assignment]

        self.assertEqual(text, "ROUTER_FINAL")
        self.assertGreater(len(seen), 1)
        self.assertTrue(all(tokens < PROMPT_CHUNK_THRESHOLD_TOKENS for tokens in seen))

    def test_disable_map_reduce(self):
        router = LLMProviderRouter(
            LLM_PROVIDERS,
            default_provider="auto",
            auto_route=AUTO_ROUTE,
            fallback_chain=["lm_studio_local", "deepseek_api"],
        )

        def fake_call(provider_id, instance, *, prompt, **kwargs):
            return "PLAIN"

        original = router._call_with_soft_timeout
        router._call_with_soft_timeout = fake_call  # type: ignore[assignment]
        try:
            text = router.chat_completion(_sample_text(), enable_map_reduce=False)
        finally:
            router._call_with_soft_timeout = original  # type: ignore[assignment]

        self.assertEqual(text, "PLAIN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
