"""
Web Search Fallback — Tavily API 封装

作为三级检索架构的第三层（本地 Chroma → 在线法律库 → Web Search），
仅在前两层未命中时才触发，最大限度减少外部 API 调用。

Tavily 专为 AI Agent 设计，返回清洗后的正文内容而非原始 HTML。
免费额度：1,000 次/月。
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("law-search")


def _get_tavily_key() -> str:
    """读取 Tavily API Key（环境变量 → 密钥文件回退）。"""
    # 环境变量
    value = os.environ.get("TAVILY_API_KEY", "").strip()
    if value:
        return value

    # 密钥文件回退（与 llm_config.py 的 _env() 逻辑一致）
    from pathlib import Path
    candidates = [
        os.path.expanduser("~/.dsh/secrets/tavily_api_key"),
        str(Path(__file__).resolve().parent.parent / ".secrets" / "tavily_api_key"),
    ]
    for secret_file in candidates:
        try:
            with open(secret_file, "r", encoding="utf-8") as f:
                stripped = f.read().strip()
                if stripped:
                    return stripped
        except OSError:
            continue
    return ""


def web_search(
    query: str,
    max_results: int = 5,
    search_depth: str = "basic",
    include_answer: bool = True,
    topic: str = "general",
) -> Tuple[str, Dict[str, Any]]:
    """
    调用 Tavily Search API 进行网络搜索。

    Args:
        query: 搜索关键词。
        max_results: 最多返回结果数（默认 5，免费额度下建议 ≤5）。
        search_depth: "basic"（快速）或 "advanced"（深度，消耗更多额度）。
        include_answer: 是否让 Tavily 生成 AI 摘要。
        topic: "general" 或 "news"（新闻类查询用 news）。

    Returns:
        (formatted_text, metadata)
        formatted_text: 适合送入 LLM 的搜索结果文本。
        metadata: 原始 API 响应的元数据。
    """
    api_key = _get_tavily_key()
    if not api_key:
        error_msg = "Tavily API Key 未配置。请在 .secrets/tavily_api_key 或环境变量 TAVILY_API_KEY 中设置。"
        logger.warning("web_search | %s", error_msg)
        return error_msg, {"error": "no_api_key"}

    try:
        from tavily import TavilyClient
    except ImportError:
        error_msg = "tavily-python 未安装。请运行 pip install tavily-python。"
        logger.error("web_search | %s", error_msg)
        return error_msg, {"error": "sdk_not_installed"}

    logger.info("web_search | query=%s | depth=%s | max=%d", query, search_depth, max_results)

    try:
        client = TavilyClient(api_key=api_key)
        response = client.search(
            query=query,
            max_results=max_results,
            search_depth=search_depth,
            include_answer=include_answer,
            topic=topic,
        )

        # 构建适合 LLM 消费的文本
        parts: List[str] = []

        # Tavily AI 生成的摘要（如果有）
        answer = response.get("answer", "")
        if answer:
            parts.append(f"【AI 摘要】\n{answer}\n")

        # 各条搜索结果
        results = response.get("results", [])
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            url = r.get("url", "")
            content = r.get("content", "")
            score = r.get("score", 0)
            parts.append(
                f"【结果 {i}】{title}\n"
                f"来源：{url}\n"
                f"相关度：{score:.2f}\n"
                f"内容：{content}\n"
            )

        formatted = "\n---\n".join(parts) if parts else "未找到相关结果。"

        metadata = {
            "query": query,
            "result_count": len(results),
            "answer_provided": bool(answer),
            "response_id": response.get("response_id", ""),
        }

        logger.info("web_search done | results=%d | answer=%s", len(results), bool(answer))
        return formatted, metadata

    except Exception as exc:
        error_msg = f"Web search 失败：{exc}"
        logger.error("web_search error | %s", exc, exc_info=True)
        return error_msg, {"error": str(exc)}
