import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

RANK_WINDOW = 20000
_CJK_RE = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+)")
_SENTENCE_SPLIT = re.compile(r"是否|能否|可否|如何|为什么|怎么|怎样|哪些|多少|何种|吗|？|。|，|,|、|；|;")
_DB_NAME_RE = re.compile(r"^judgments_(\d{4})\.db$")


class SourceNotFound(Exception):
    pass


class InvalidSourceReference(Exception):
    pass


class SourceUnavailable(Exception):
    pass


def _seg(value: str) -> str:
    return _CJK_RE.sub(lambda match: " ".join(match.group(1)), value) if value else ""


def _query_terms(query: str) -> List[str]:
    words = [word for word in re.split(r"\s+", query.strip()) if word]
    if len(words) == 1 and len(words[0]) >= 8:
        parts = [part.strip() for part in _SENTENCE_SPLIT.split(words[0]) if len(part.strip()) >= 2]
        if len(parts) > 1:
            return parts[:6]
    return words


def _fts_expression(terms: List[str], operator: str) -> str:
    return (" {} ".format(operator)).join(
        '"{}"'.format(_seg(term).replace('"', '""')) for term in terms
    )


def _decompress(data: bytes) -> str:
    import zstandard

    return zstandard.ZstdDecompressor().decompress(data).decode("utf-8", errors="replace")


def _highlight(text: str, terms: List[str], width: int = 120) -> str:
    positions = [(text.find(term), term) for term in terms]
    positions = [(position, term) for position, term in positions if position >= 0]
    if not positions:
        return text[: width * 2]
    position, term = min(positions, key=lambda pair: pair[0])
    start = max(0, position - width)
    end = min(len(text), position + len(term) + width)
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def _like_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%{}%".format(escaped)


def _readonly_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _year_done(path: Path) -> bool:
    try:
        connection = _readonly_connection(path)
        try:
            row = connection.execute("SELECT v FROM progress WHERE k='done'").fetchone()
            return row is not None
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False


def _metadata(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "case_number": row["ah"] or "",
        "case_name": row["ajmc"] or "",
        "court": row["fy"] or "",
        "region": row["ssdq"] or "",
        "case_type": row["ajlx"] or "",
        "procedure": row["slcx"] or "",
        "judgment_date": row["cprq"] or "",
        "cause": row["ay"] or "",
        "legal_basis": row["fl"] if "fl" in row.keys() else "",
        "full_text_length": row["nr_len"] or 0,
    }


def _result(
    year: int,
    row: sqlite3.Row,
    text: str,
    terms: List[str],
    match_type: str,
    full_text: bool = False,
) -> Dict[str, Any]:
    metadata = _metadata(row)
    partial = match_type == "or"
    return {
        "title": metadata["case_name"] or metadata["case_number"] or "裁判文书",
        "source_label": "{} · {}".format(metadata["court"] or "法院未知", year),
        "excerpt": _highlight(text, terms),
        "content": text if full_text else "",
        "hit_reason": "部分关键词命中（AND 零命中后降级 OR）" if partial else "全部关键词命中",
        "match_type": match_type,
        "partial_match": partial,
        "rank": 0,
        "score": None,
        "locator": {"year": year, "rowid": row["id"]},
        "source_ref": {"year": year, "rowid": row["id"]},
        "metadata": metadata,
    }


class JudgmentEngine:
    def __init__(
        self,
        db_dir: Path,
        deadline_s: float = 45.0,
        workers: Optional[int] = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.db_dir = db_dir.expanduser().resolve()
        self.deadline_s = deadline_s
        if workers is None:
            try:
                workers = int(os.environ.get("SLP_JUDGMENT_SEARCH_WORKERS", "6"))
            except ValueError:
                workers = 6
        self.workers = max(1, min(int(workers), 8))
        self.clock = clock

    def _available_dbs(self) -> List[Tuple[int, Path]]:
        try:
            paths = list(self.db_dir.iterdir())
        except OSError:
            return []
        databases = []
        for path in paths:
            match = _DB_NAME_RE.fullmatch(path.name)
            if match and path.is_file() and _year_done(path):
                databases.append((int(match.group(1)), path))
        return sorted(databases, key=lambda pair: pair[0], reverse=True)

    def search(
        self,
        query: str,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
        court: str = "",
        cause: str = "",
        limit: int = 10,
    ) -> Dict[str, Any]:
        query = query.strip()
        terms = _query_terms(query)
        limit = max(1, min(int(limit or 10), 50))
        and_expression = _fts_expression(terms, "AND")
        or_expression = _fts_expression(terms, "OR") if len(terms) > 1 else ""
        deadline = self.clock() + self.deadline_s
        selected = [
            (year, path)
            for year, path in self._available_dbs()
            if (year_from is None or year >= year_from)
            and (year_to is None or year <= year_to)
        ]
        filters = {
            "year_from": year_from,
            "year_to": year_to,
            "court": court,
            "cause": cause,
        }
        if not terms:
            return self._empty_response(query, filters, "empty_query")

        parts = []
        if selected:
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = [
                    pool.submit(
                        self._search_one,
                        year,
                        path,
                        terms,
                        and_expression,
                        or_expression,
                        court,
                        cause,
                        limit,
                        deadline,
                    )
                    for year, path in selected
                ]
                for (year, _path), future in zip(selected, futures):
                    try:
                        parts.append(future.result())
                    except Exception:
                        parts.append(
                            {
                                "year": year,
                                "status": "failed",
                                "match_type": None,
                                "partial_match": False,
                                "results": [],
                                "executed_queries": [],
                                "error": {
                                    "code": "database_failed",
                                    "message": "该年份裁判文书库检索失败",
                                },
                            }
                        )

        database_coverage = []
        executed_queries = []
        results = []
        for part in parts:
            executed_queries.extend(part["executed_queries"])
            results.extend(part["results"])
            coverage = {
                "year": part["year"],
                "status": part["status"],
                "match_type": part["match_type"],
                "partial_match": part["partial_match"],
                "result_count": len(part["results"]),
            }
            if part.get("error"):
                coverage["error"] = part["error"]
            database_coverage.append(coverage)
        results = results[:limit]
        for rank, item in enumerate(results, 1):
            item["rank"] = rank

        counts = {
            "completed": sum(1 for item in database_coverage if item["status"] == "completed"),
            "empty": sum(1 for item in database_coverage if item["status"] == "empty"),
            "timeout": sum(1 for item in database_coverage if item["status"] == "timeout"),
            "failed": sum(1 for item in database_coverage if item["status"] == "failed"),
        }
        unavailable = counts["timeout"] + counts["failed"]
        if not selected:
            status = "success"
            coverage_status = "empty"
            error = None
        elif unavailable == len(selected):
            status = "failed"
            coverage_status = "failed"
            error = {"code": "all_databases_unavailable", "message": "所选年份库均不可用"}
        elif unavailable:
            status = "partial"
            coverage_status = "partial"
            error = {"code": "partial_source_failure", "message": "部分年份库未完成检索"}
        else:
            status = "success"
            coverage_status = "completed"
            error = None
        coverage = {
            "status": coverage_status,
            "selected_databases": len(selected),
            "completed": counts["completed"],
            "empty": counts["empty"],
            "timeout": counts["timeout"],
            "failed": counts["failed"],
            "databases": database_coverage,
        }
        return {
            "source": "judgments",
            "status": status,
            "query": query,
            "filters": filters,
            "executed_queries": executed_queries,
            "coverage": coverage,
            "results": results,
            "error": error,
        }

    def _empty_response(
        self, query: str, filters: Dict[str, Any], code: str
    ) -> Dict[str, Any]:
        return {
            "source": "judgments",
            "status": "failed",
            "query": query,
            "filters": filters,
            "executed_queries": [],
            "coverage": {
                "status": "failed",
                "selected_databases": 0,
                "completed": 0,
                "empty": 0,
                "timeout": 0,
                "failed": 0,
                "databases": [],
            },
            "results": [],
            "error": {"code": code, "message": "检索关键词不能为空"},
        }

    def _search_one(
        self,
        year: int,
        path: Path,
        terms: List[str],
        and_expression: str,
        or_expression: str,
        court: str,
        cause: str,
        limit: int,
        deadline: float,
    ) -> Dict[str, Any]:
        base = {
            "year": year,
            "status": "timeout",
            "match_type": None,
            "partial_match": False,
            "results": [],
            "executed_queries": [],
        }
        if self.clock() >= deadline:
            base["error"] = {"code": "deadline_exceeded", "message": "该年份检索超出时间预算"}
            return base
        connection = None
        executed = []
        try:
            connection = _readonly_connection(path)
            connection.set_progress_handler(
                lambda: 1 if self.clock() >= deadline else 0, 1000
            )
            per_db = max(limit, 20)
            fetch_cap = min(per_db * 10, 20000)
            match_type = "and"
            expression = and_expression
            executed.append(
                {
                    "year": year,
                    "query": " ".join(terms),
                    "expression": expression,
                    "mode": "and",
                    "status": "submitted",
                }
            )
            top_rids = self._rank_rowids(connection, expression, fetch_cap)
            if not top_rids and or_expression:
                match_type = "or"
                expression = or_expression
                executed.append(
                    {
                        "year": year,
                        "query": " ".join(terms),
                        "expression": expression,
                        "mode": "or",
                        "status": "submitted",
                    }
                )
                top_rids = self._rank_rowids(connection, expression, fetch_cap)
            rows = self._filtered_rows(connection, top_rids, court, cause, per_db)
            results = []
            for row in rows:
                blob_row = connection.execute(
                    "SELECT nr_zst FROM docs WHERE id=?", (row["id"],)
                ).fetchone()
                if blob_row is None:
                    continue
                text = _decompress(blob_row["nr_zst"])
                results.append(_result(year, row, text, terms, match_type))
            base.update(
                {
                    "status": "completed" if results else "empty",
                    "match_type": match_type,
                    "partial_match": match_type == "or" and bool(results),
                    "results": results,
                    "executed_queries": executed,
                }
            )
            return base
        except sqlite3.OperationalError as error:
            timed_out = "interrupt" in str(error).lower() or self.clock() >= deadline
            base.update(
                {
                    "status": "timeout" if timed_out else "failed",
                    "executed_queries": executed,
                    "error": {
                        "code": "deadline_exceeded" if timed_out else "database_failed",
                        "message": "该年份检索超出时间预算" if timed_out else "该年份裁判文书库检索失败",
                    },
                }
            )
            return base
        except Exception:
            base.update(
                {
                    "status": "failed",
                    "executed_queries": executed,
                    "error": {"code": "database_failed", "message": "该年份裁判文书库检索失败"},
                }
            )
            return base
        finally:
            if connection is not None:
                connection.close()

    def _rank_rowids(
        self, connection: sqlite3.Connection, expression: str, fetch_cap: int
    ) -> List[int]:
        over_cap = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM docs_fts WHERE docs_fts MATCH ? LIMIT 1 OFFSET ?)",
            (expression, RANK_WINDOW),
        ).fetchone()[0]
        order = "rowid" if over_cap else "bm25(docs_fts)"
        return [
            row[0]
            for row in connection.execute(
                "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? "
                "ORDER BY {} LIMIT ?".format(order),
                (expression, fetch_cap),
            ).fetchall()
        ]

    def _filtered_rows(
        self,
        connection: sqlite3.Connection,
        rowids: List[int],
        court: str,
        cause: str,
        limit: int,
    ) -> List[sqlite3.Row]:
        if not rowids:
            return []
        conditions = []
        params = list(rowids)
        if court.strip():
            conditions.append("d.fy LIKE ? ESCAPE '\\'")
            params.append(_like_value(court.strip()))
        if cause.strip():
            conditions.append("d.ay LIKE ? ESCAPE '\\'")
            params.append(_like_value(cause.strip()))
        placeholders = ",".join("?" for _ in rowids)
        suffix = " AND " + " AND ".join(conditions) if conditions else ""
        columns = (
            "d.id, d.ah, d.ajmc, d.fy, d.ssdq, d.ajlx, d.slcx, d.cprq, "
            "d.ay, d.fl, d.nr_len"
        )
        cursor = connection.execute(
            "SELECT {} FROM docs d WHERE d.id IN ({}){}".format(columns, placeholders, suffix),
            params,
        )
        by_id = {row["id"]: row for row in cursor}
        selected = []
        for rowid in rowids:
            row = by_id.get(rowid)
            if row is not None:
                selected.append(row)
                if len(selected) >= limit:
                    break
        return selected

    def read_source(self, year: int, rowid: int) -> Dict[str, Any]:
        if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999:
            raise InvalidSourceReference("年份无效")
        if isinstance(rowid, bool) or not isinstance(rowid, int) or rowid < 1:
            raise InvalidSourceReference("rowid 无效")
        path = (self.db_dir / "judgments_{:04d}.db".format(year)).resolve()
        if path.parent != self.db_dir or not path.is_file():
            raise SourceNotFound("来源不存在")
        try:
            connection = _readonly_connection(path)
        except (OSError, sqlite3.Error) as error:
            raise SourceUnavailable("来源暂不可用") from error
        try:
            row = connection.execute("SELECT * FROM docs WHERE id=?", (rowid,)).fetchone()
            if row is None:
                raise SourceNotFound("来源不存在")
            text = _decompress(row["nr_zst"])
            terms = _query_terms(row["ajmc"] or "")
            result = _result(year, row, text, terms, "source", full_text=True)
            result["partial_match"] = False
            result.pop("rank", None)
            return {
                "source": "judgments",
                "status": "success",
                "year": year,
                "rowid": rowid,
                "title": result["title"],
                "source_label": result["source_label"],
                "excerpt": result["excerpt"],
                "content": result["content"],
                "hit_reason": "按严格年份和 rowid 读取原文",
                "match_type": "source",
                "partial_match": False,
                "score": None,
                "locator": result["locator"],
                "source_ref": result["source_ref"],
                "metadata": result["metadata"],
                "error": None,
            }
        except SourceNotFound:
            raise
        except Exception as error:
            raise SourceUnavailable("来源暂不可用") from error
        finally:
            connection.close()
