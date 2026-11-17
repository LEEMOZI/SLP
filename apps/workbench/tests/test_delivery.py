import io
import sqlite3

import pytest
from docx import Document

from apps.workbench.backend.delivery import (
    check_traceability,
    extract_facts,
    render_docx,
    render_markdown,
    summarize_facts,
)
from apps.workbench.backend.summaries import (
    CitationIssue,
    SummarySource,
    SummaryVersion,
)
from apps.workbench.backend.tasks import TaskRepository


def make_source(handle, text, title="示例判决书"):
    return SummarySource(
        handle=handle,
        kind="material_segment",
        source_id="seg-1",
        case_id="case-1",
        title=title,
        text=text,
        locator={"line_start": 1, "line_end": 1},
        source_label="测试材料 · 第 1 段",
        origin_status="success",
    )


def test_extract_facts_finds_positions_and_normalizes():
    body = (
        "经审理查明：\n"
        "案号为（2026）京 03 民终 13295 号。\n"
        "判决日期 2026年5月12日，赔偿金额 100万元。"
    )
    facts = extract_facts(body)
    kinds = {(fact["kind"], fact["line"]) for fact in facts}
    assert ("case_number", 2) in kinds
    assert ("date", 3) in kinds
    assert ("amount", 3) in kinds
    case_fact = next(fact for fact in facts if fact["kind"] == "case_number")
    # 全角括号与内部空格在归一化后去除。
    assert case_fact["value"] == "(2026)京03民终13295号"
    assert "案号" in case_fact["excerpt"]
    date_fact = next(fact for fact in facts if fact["kind"] == "date")
    assert date_fact["value"] == "2026年5月12日"


def test_summarize_facts_groups_unique_values():
    facts = extract_facts("（2026）京03民终13295号 \n 再次出现（2026）京03民终13295号 与 50万元")
    summary = summarize_facts(facts)
    assert summary["case_numbers"] == ["(2026)京03民终13295号"]
    assert summary["amounts"] == ["50万元"]
    assert summary["dates"] == []
    assert len(summary["positions"]) == 3


def test_check_traceability_requires_cited_sources():
    source_text = "判决书：（2026）京03民终13295号，2026年5月12日。"
    citations = {"S1": make_source("S1", source_text)}
    body = "根据 [[S1]]，案号为（2026）京03民终13295号，日期 2026年5月12日。"
    assert check_traceability(body, citations) == []

    missing_body = (
        "根据 [[S1]]，案号为（2026）京03民终13295号。\n"
        "但合同签订于 2025年3月1日，标的 80万元。"
    )
    issues = check_traceability(missing_body, citations)
    codes = {issue.code for issue in issues}
    assert codes == {"fact_unverified"}
    assert any("第 2 行" in issue.message and "80万元" in issue.message for issue in issues)

    # 未被正文引用的来源不参与校验。
    orphan = check_traceability("未引用任何来源，案号（2026）京03民终13295号", citations)
    assert [issue.code for issue in orphan] == ["fact_unverified"]


def make_version(body, citations, **overrides):
    values = {
        "id": "v1",
        "summary_id": "s1",
        "version_number": 3,
        "status": "confirmed",
        "body": body,
        "citations": citations,
        "base_version_id": None,
        "task_id": "task-9",
        "attempt_id": None,
        "generator_provider": "builtin-extractive",
        "requested_provider": None,
        "actual_model": None,
        "coverage": {},
        "saved_at": "2026-11-17T00:00:00Z",
        "confirmed_at": None,
    }
    values.update(overrides)
    return SummaryVersion(**values)


def test_render_markdown_keeps_citations_and_lists_sources():
    citations = {"S1": make_source("S1", "判决书：（2026）京03民终13295号。")}
    version = make_version("结论段 [[S1]]：合同有效。", citations)
    text = render_markdown("违约责任研究", "赔偿范围", version, "official", [], "2026-11-17T08:00:00Z")
    assert "# 违约责任研究" in text
    assert "## 正文" in text and "结论段 [[S1]]：合同有效。" in text
    assert "## 来源清单" in text and "**S1** 示例判决书" in text
    assert "校验结论：引用与事实可追溯性校验通过" in text
    assert "生成任务：task-9" in text

    draft_text = render_markdown(
        "违约责任研究",
        "赔偿范围",
        version,
        "draft",
        [CitationIssue(code="fact_unverified", message="第 1 行的案号未在已引用来源中出现")],
        "2026-11-17T08:00:00Z",
    )
    assert "存在 1 个未核实问题，本文件为草稿" in draft_text
    assert "第 1 行的案号未在已引用来源中出现" in draft_text


def test_render_docx_produces_readable_document():
    citations = {"S1": make_source("S1", "判决书：（2026）京03民终13295号。")}
    version = make_version(
        "第一段结论 [[S1]]。\n第二段说明。",
        citations,
        task_id=None,
        generator_provider=None,
        version_number=2,
    )
    content = render_docx("交付测试", "研究问题", version, "official", [], "2026-11-17T08:00:00Z")
    document = Document(io.BytesIO(content))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]
    assert "交付测试" in paragraphs
    assert "版本：v2（正式交付）" in paragraphs
    assert "第一段结论 [[S1]]。" in paragraphs and "第二段说明。" in paragraphs
    assert any("S1 示例判决书" in text for text in paragraphs)


def test_v4_schema_is_upgraded_with_delivery_tables(repository):
    from apps.workbench.backend.migrations import run_migrations

    repository.initialize()
    TaskRepository(repository).initialize()
    with sqlite3.connect(repository.db_path) as connection:
        connection.execute("DROP TABLE delivery_versions")
        connection.execute("DELETE FROM schema_migrations WHERE version = 5")
        connection.execute(
            "INSERT INTO cases(id, name, normalized_name, created_at, updated_at) "
            "VALUES ('existing-case', '既有案件', '既有案件', '2026-01-01', '2026-01-01')"
        )
        run_migrations(connection)
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(delivery_versions)")
        }
        migration = connection.execute(
            "SELECT name FROM schema_migrations WHERE version = 5"
        ).fetchone()
        existing = connection.execute(
            "SELECT name FROM cases WHERE id = 'existing-case'"
        ).fetchone()
        run_migrations(connection)
        migration_count = connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
        ).fetchone()[0]

    assert migration[0] == "delivery_versions"
    assert migration_count == 1
    assert existing[0] == "既有案件"
    assert {
        "summary_id",
        "version_id",
        "format",
        "status",
        "storage_key",
        "sha256",
        "size_bytes",
        "facts",
        "issues",
        "generator_provider",
        "actual_model",
        "task_id",
        "exported_at",
    } <= columns
