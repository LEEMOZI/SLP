"""judgment-search MCP 服务：8800 万份裁判文书检索（SQLite FTS5 按年分库）。

工具：
- search_judgment: 全文检索 + 结构化过滤（法院/案由/类型/日期区间），返回高亮摘要
- get_judgment: 按库+rowid 取单份全文
- export_result / deliver_file: 交付链路（与 law-search 同款，写 iCloud 目录 + 返回 URL）

传输：
- stdio（默认）：MCP client 子进程挂载
- http：streamable-http 于 :8766/mcp（Bearer abcd1234ef），/files/ 免鉴权静态路由
"""
import json
import os
import re
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import zstandard
from fastmcp import FastMCP

DB_DIR = Path(__file__).parent / "data" / "judgments"
DELIVERY_DIR = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "SLP交付输出"
EXTERNAL_BASE = os.environ.get("JUDGMENT_EXTERNAL_BASE", "http://100.66.217.64:8766")

mcp = FastMCP("judgment-search")
_zctx = zstandard.ZstdDecompressor()
logger = None  # 由 _setup_logging 赋值

# 工具输出截断 + 落盘：避免单份判决书全文把 prompt 撑爆
_TOOL_OUTPUT_MAX_CHARS = int(os.environ.get("JUDGMENT_TOOL_OUTPUT_MAX_CHARS", "4000"))
# 单次检索墙钟预算：必须小于客户端 MCP 调用超时（DSH/Cherry 默认 60s），
# 否则客户端会取消请求；到点后返回已扫描年份的部分结果，而不是继续跑。
_SEARCH_DEADLINE_S = float(os.environ.get("JUDGMENT_SEARCH_DEADLINE_S", "45"))
_SPILL_DIR = Path(
    os.environ.get(
        "JUDGMENT_SPILL_DIR",
        "/Users/simon/Documents/1-SLP/MCP/law-search/tool_outputs",
    )
)


def _cap_tool_output(text: str, tool: str) -> str:
    """超过上限时只回传前 N 字符，完整内容落盘并给出续读方式。"""
    if not isinstance(text, str) or len(text) <= _TOOL_OUTPUT_MAX_CHARS:
        return text
    try:
        _SPILL_DIR.mkdir(parents=True, exist_ok=True)
        spill = _SPILL_DIR / f"{tool}_{time.strftime('%Y%m%d_%H%M%S')}_{os.getpid()}.txt"
        spill.write_text(text, encoding="utf-8")
    except OSError as exc:
        print(f"spill failed | tool={tool} | error={exc}", flush=True)
        return text[:_TOOL_OUTPUT_MAX_CHARS] + f"\n\n...[输出已截断，共 {len(text):,} 字符]"
    return (
        f"{text[:_TOOL_OUTPUT_MAX_CHARS]}\n\n...[输出已截断] 完整内容共 {len(text):,} 字符，"
        f"已落盘：{spill}\n"
        f'需要剩余内容时调用 read_document(file_path="{spill}", '
        f"offset={_TOOL_OUTPUT_MAX_CHARS}, max_chars=4000)。"
    )

# 高频保护阈值：两档简化（去掉中间档以优化双词 AND 查询的探针速度）
# 双词 AND 查询在大库 FTS 求交集本身就慢，EXISTS OFFSET 20000 比 OFFSET 200000 快 7-8x
RANK_WINDOW = 20000     # 命中 ≤2万：全库 bm25 精确排序
# 命中 >2万：入库序兜底（时间序≈相关性足够，且毫秒级返回）


def _setup_logging():
    global logger
    import logging
    logger = logging.getLogger("judgment-search")
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        _h = logging.StreamHandler()
        _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(_h)


_done_cache: dict[Path, tuple[int, bool]] = {}


def _year_done(db_path: Path) -> bool:
    """判断分库是否已建完（progress 表 done 标记），按文件大小缓存避免重复探测。"""
    try:
        size = db_path.stat().st_size
    except OSError:
        return False
    cached = _done_cache.get(db_path)
    if cached and cached[0] == size:
        return cached[1]
    done = False
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT v FROM progress WHERE k='done'").fetchone()
            done = row is not None
        finally:
            con.close()
    except sqlite3.Error:
        done = False  # 表未建/库仍在写入 → 视为未完成
    _done_cache[db_path] = (size, done)
    return done


def _available_dbs() -> list[Path]:
    """列出已建好的年度库（按年份降序：新案优先）。未建完（无 done 标记）的自动跳过。"""
    return sorted(
        (p for p in DB_DIR.glob("judgments_*.db") if _year_done(p)),
        key=lambda p: p.stem, reverse=True,
    )


def _highlight(nr: str, terms: list[str], width: int = 120) -> str:
    """本地高亮：找第一个命中词的位置，返回前后文窗口。"""
    if not terms:
        return nr[:width * 2]
    pos = -1
    for t in terms:
        p = nr.find(t)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return nr[:width * 2]
    start = max(0, pos - width)
    return ("…" if start > 0 else "") + nr[start:pos + len(terms[0]) + width] + "…"


# 与 build_judgment_db.seg 同款：入库时 CJK 逐字空格化，查询侧必须同步转换，
# 否则短语对不上（「盗窃罪」入库为「盗 窃 罪」）。
_CJK_RE = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+)")


def _seg(s: str) -> str:
    return _CJK_RE.sub(lambda m: " ".join(m.group(1)), s) if s else ""


# 整句提问里的疑问/语气标记；单串长句按这些位置切分成关键词片段
_SENTENCE_SPLIT = re.compile(
    r"是否|能否|可否|如何|为什么|怎么|怎样|哪些|多少|何种|吗|？|。|，|,|、|；|;"
)


def _query_terms(query: str) -> list[str]:
    """把查询拆成检索词。空格分隔优先；整句（单串长中文）按疑问词/标点切段，
    避免"整句当短语"导致 AND 零命中。"""
    words = [w for w in re.split(r"\s+", query.strip()) if w]
    if len(words) == 1 and len(words[0]) >= 8:
        parts = [p.strip() for p in _SENTENCE_SPLIT.split(words[0]) if len(p.strip()) >= 2]
        if len(parts) > 1:
            return parts[:6]
    return words


@mcp.tool()
def search_judgment(
    query: str,
    court: str = "",
    cause: str = "",
    case_type: str = "",
    date_from: str = "",
    date_to: str = "",
    year_from: int = 0,
    year_to: int = 0,
    limit: int = 10,
) -> str:
    """检索 6800 万份裁判文书（1985-2021，SQLite FTS5 全文 + 结构化过滤）。

    案例 / 类案 / 判决理由检索的首选工具（law-search 的 search_case 只有约 1000 条，覆盖面小）。

    Args:
        query: 检索关键词，空格分隔；多词为 AND（全部命中）。**建议 2–5 个实词，不要整句提问**，
            如 "民间借贷 利息"、"差额补足 承诺函 信托"。整句（含"是否""如何"）会因 AND 而零命中，
            此时会自动降级为部分匹配（OR，按相关性排序），并在 rank_notes 标注"部分匹配"。
        court: 按法院名称过滤（模糊包含，如 "北京市朝阳区人民法院"）
        cause: 按案由过滤（模糊包含，如 "民间借贷纠纷"）
        case_type: 按案件类型过滤（民事案件/刑事案件/行政案件/执行案件等）
        date_from: 裁判日期起（YYYY-MM-DD）
        date_to: 裁判日期止（YYYY-MM-DD）
        year_from: 年份区间起（与 date_* 二选一，跨年检索时更快）
        year_to: 年份区间止
        limit: 返回条数（默认 10，最大 50）

    无结果时可换 2–3 组不同关键词重试；收窄年份区间能显著提速。
    """
    _setup_logging()
    t0 = time.perf_counter()
    limit = max(1, min(int(limit or 10), 50))
    if not query or not query.strip():
        return "query 不能为空。请提供检索关键词，例如：民间借贷 利息"
    terms = _query_terms(query)
    expr = " AND ".join(f'"{_seg(w)}"' for w in terms) if terms else '""'
    # AND 零命中时的降级表达式：任一词命中即可（部分匹配）
    or_expr = " OR ".join(f'"{_seg(w)}"' for w in terms) if len(terms) > 1 else ""

    courts = [c.strip() for c in re.split(r"[;,，；]", court) if c.strip()]
    causes = [c.strip() for c in re.split(r"[;,，；]", cause) if c.strip()]
    per_db = max(limit, 20)  # 每库取多一些再合并排序

    deadline = t0 + _SEARCH_DEADLINE_S
    all_dbs = _available_dbs()

    def _search_one(db_path: Path) -> dict:
        """单个年份库的检索。预算耗尽时立即返回空结果，供并行调度快速收尾。"""
        year = int(re.search(r"(\d{4})", db_path.stem).group(1))
        if time.perf_counter() > deadline:
            return {"results": [], "rank_note": None,
                    "timed_out": True, "attempted": False}

        rows, rank_note, hit_deadline = [], None, False
        zctx = zstandard.ZstdDecompressor()  # 每线程独立，避免共享实例的线程安全问题
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        # 单库内部的长查询同样受总预算约束：SQLite 每 1000 个 VM 步回调一次
        con.set_progress_handler(
            lambda: 1 if time.perf_counter() > deadline else 0, 1000
        )
        try:
            # ── Phase 1: 探针 + 排序（纯 FTS 表，无 JOIN，无 BLOB）──────
            # 优化 1: 探针用 EXISTS OFFSET 20000，只扫到第 20001 行就停
            #         双词 AND 查询比物化 200001 行快 7-8x
            # 优化 2: 两档简化（去掉中间档），>2万命中直接入库序兜底
            over_cap = con.execute(
                "SELECT EXISTS(SELECT 1 FROM docs_fts WHERE docs_fts MATCH ? LIMIT 1 OFFSET ?)",
                (expr, RANK_WINDOW)).fetchone()[0]

            # 取比 per_db 多 10 倍的 rowids，防止结构化过滤后数量不够
            fetch_cap = min(per_db * 10, 20000)  # 排序阶段最多取 2 万 rowids

            if not over_cap:
                # 低/中频：全库 bm25 精确排序
                rank_sql = "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT ?"
                rank_params = [expr, fetch_cap]
            else:
                # 高频：入库序≈时间序兜底，毫秒级返回
                rank_sql = "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rowid LIMIT ?"
                rank_params = [expr, fetch_cap]
                rank_note = f"{year}(按入库序)"

            top_rids = [r[0] for r in con.execute(rank_sql, rank_params).fetchall()]

            if not top_rids and or_expr:
                # AND 零命中 → 降级部分匹配（OR），按相关性排序，避免整句提问 0 召回
                over_cap = con.execute(
                    "SELECT EXISTS(SELECT 1 FROM docs_fts WHERE docs_fts MATCH ? LIMIT 1 OFFSET ?)",
                    (or_expr, RANK_WINDOW)).fetchone()[0]
                if over_cap:
                    top_rids = [r[0] for r in con.execute(
                        "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? ORDER BY rowid LIMIT ?",
                        (or_expr, fetch_cap)).fetchall()]
                else:
                    top_rids = [r[0] for r in con.execute(
                        "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? ORDER BY bm25(docs_fts) LIMIT ?",
                        (or_expr, fetch_cap)).fetchall()]
                if top_rids:
                    rank_note = f"{year}(部分匹配)"

            if not top_rids:
                return {"results": [], "rank_note": rank_note,
                        "timed_out": False, "attempted": True}

            # ── Phase 2: 批量取数（JOIN docs 带过滤条件）────────────────
            # 优化：先取排序 rowids，再用 IN 查询 docs，避免排序阶段 JOIN BLOB 大字段
            conds, filter_params = [], []
            if date_from:
                conds.append("d.cprq >= ?")
                filter_params.append(date_from)
            if date_to:
                conds.append("d.cprq <= ?")
                filter_params.append(date_to)
            if courts:
                conds.append("(" + " OR ".join("d.fy LIKE ?" for _ in courts) + ")")
                filter_params += [f"%{c}%" for c in courts]
            if causes:
                conds.append("(" + " OR ".join("d.ay LIKE ?" for _ in causes) + ")")
                filter_params += [f"%{c}%" for c in causes]
            if case_type:
                conds.append("d.ajlx LIKE ?")
                filter_params.append(f"%{case_type}%")

            placeholders = ",".join("?" * len(top_rids))
            cols = ("d.id, d.ah, d.ajmc, d.fy, d.ssdq, d.ajlx, d.slcx, d.cprq,"
                    " d.ay, d.nr_len, d.nr_zst")
            where_filter = (" AND " + " AND ".join(conds)) if conds else ""
            fetch_sql = (f"SELECT {cols} FROM docs d WHERE d.id IN ({placeholders})"
                         f"{where_filter} LIMIT ?")
            fetch_params = top_rids + filter_params + [per_db]

            for r in con.execute(fetch_sql, fetch_params):
                nr = zctx.decompress(r["nr_zst"]).decode("utf-8", errors="replace")
                rows.append({
                    "db": db_path.stem.replace("judgments_", ""),
                    "rowid": r["id"],
                    "案号": r["ah"],
                    "案件名称": r["ajmc"],
                    "法院": r["fy"],
                    "地区": r["ssdq"],
                    "类型": r["ajlx"],
                    "程序": r["slcx"],
                    "裁判日期": r["cprq"],
                    "案由": r["ay"],
                    "全文长度": r["nr_len"],
                    "摘要": _highlight(nr, terms),
                })
        except sqlite3.OperationalError as exc:
            if "interrupt" in str(exc).lower():
                hit_deadline = True
            # 其它 OperationalError：该年 fts 表为空或未建
        finally:
            con.close()
        return {"results": rows, "rank_note": rank_note,
                "timed_out": hit_deadline, "attempted": True}

    selected = []
    for db_path in all_dbs:
        year = int(re.search(r"(\d{4})", db_path.stem).group(1))
        if year_from and year < year_from:
            continue
        if year_to and year > year_to:
            continue
        selected.append(db_path)

    workers = max(1, min(int(os.environ.get("JUDGMENT_SEARCH_WORKERS", "6")), 8))
    results, searched, timed_out = [], 0, False
    rank_notes = []  # 未做全库相关性排序的年份（高频保护触发记录）
    if selected:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_search_one, p) for p in selected]
            for fut in futures:
                try:
                    part = fut.result()
                except Exception as exc:  # 单库异常不影响其它年份
                    logger.warning("search_judgment | db scan failed: %s", exc)
                    continue
                if part["attempted"]:
                    searched += 1
                results.extend(part["results"])
                if part["rank_note"]:
                    rank_notes.append(part["rank_note"])
                timed_out = timed_out or part["timed_out"]

    # bm25 越小越相关；跨库按库内排名合并：取每库前 limit 条再全局截断
    results = results[:limit * 3]
    elapsed = time.perf_counter() - t0
    out = {
        "query": query,
        "filters": {"court": court, "cause": cause, "case_type": case_type,
                    "date_from": date_from, "date_to": date_to,
                    "year_from": year_from, "year_to": year_to},
        "searched_years": searched,
        "available_years": len(all_dbs),
        "total_hits_shown": len(results[:limit]),
        "elapsed_s": round(elapsed, 3),
        "note": "用 get_judgment(db, rowid) 取全文；用 export_judgment(db, rowid) 导出全文并返回 URL",
        "results": results[:limit],
    }
    if timed_out:
        out["timed_out"] = True
        out["note"] = (
            f"检索在 {_SEARCH_DEADLINE_S:.0f}s 预算内扫描了 {searched}/{len(all_dbs)} 个年份库，"
            "以上为已扫描部分的结果。建议缩小年份区间（year_from/year_to），"
            "或增加 court / cause / case_type 过滤后重试。"
        )
    if rank_notes:
        out["rank_note"] = ("高频保护，以下年份未做全库相关性排序 → " + "、".join(rank_notes)
                            + "；建议添加过滤条件或使用更精确关键词")
    logger.info("search_judgment | %r | %s hits | %.3fs", query, len(results[:limit]), elapsed)
    return _cap_tool_output(json.dumps(out, ensure_ascii=False, indent=2), "search_judgment")


@mcp.tool()
def get_judgment(db: str, rowid: int) -> str:
    """获取单份裁判文书全文（zstd 解压，毫秒级）。

    Args:
        db: 年份库标识（search_judgment 结果中的 db 字段，如 "2021"）
        rowid: 文书 ID（search_judgment 结果中的 rowid 字段）
    """
    _setup_logging()
    db_path = DB_DIR / f"judgments_{db}.db"
    if not db_path.exists():
        return f"库不存在：{db_path.name}。可用年份：{[p.stem.replace('judgments_','') for p in _available_dbs()]}"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM docs WHERE id=?", (int(rowid),)).fetchone()
        if not r:
            return f"未找到 rowid={rowid}"
        nr = _zctx.decompress(r["nr_zst"]).decode("utf-8", errors="replace")
        meta = {k: r[k] for k in ("ah", "ajmc", "fy", "ssdq", "ajlx", "slcx", "cprq", "ay", "fl", "nr_len")}
        out = {"db": db, "rowid": rowid, **meta, "全文": nr}
        logger.info("get_judgment | %s/%s | %s 字", db, rowid, r["nr_len"])
        return _cap_tool_output(json.dumps(out, ensure_ascii=False, indent=2), "get_judgment")
    finally:
        con.close()


# ---------- 交付链路（与 law-search 同款）----------

def _ensure_delivery_dir() -> Path:
    target = DELIVERY_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        link = Path.home() / "SLP交付输出"
        if not link.exists() and not link.is_symlink():
            link.symlink_to(target)
    except OSError:
        pass
    return target


@mcp.tool()
def export_judgment(db: str, rowid: int, filename: str = "") -> str:
    """将单份裁判文书全文导出为 txt 文件，写入 iCloud 交付目录并返回 URL。

    iPhone 可在「文件」App 中直接查看，或点聊天中返回的 URL 打开。

    Args:
        db: 年份库标识（search_judgment 结果中的 db 字段）
        rowid: 文书 ID
        filename: 导出文件名（留空则用案号命名）
    """
    _setup_logging()
    raw = get_judgment(db, rowid)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw  # 错误信息透传
    name = filename.strip() or (data.get("ah") or f"judgment_{db}_{rowid}").replace("/", "_")
    name = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", name).strip("_") or "judgment"
    target = _ensure_delivery_dir()
    full = f"{name}.txt"
    dest = target / full
    counter = 1
    while dest.exists():
        full = f"{name}_{counter}.txt"
        dest = target / full
        counter += 1
    body = (f"{data.get('ajmc','')}\n{data.get('ah','')} | {data.get('fy','')} | "
            f"{data.get('cprq','')}\n案由：{data.get('ay','')}\n{'='*40}\n"
            f"{data.get('全文','')}")
    dest.write_text(body, encoding="utf-8")
    url = f"{EXTERNAL_BASE}/files/{full}"
    logger.info("export_judgment | %s/%s -> %s", db, rowid, full)
    return json.dumps({"status": "ok", "filename": full, "url": url,
                       "size_bytes": len(body.encode()),
                       "icloud_sync": "已写入 iCloud，iPhone「文件」App 可见"},
                      ensure_ascii=False, indent=2)


@mcp.tool()
def export_result(content: str, filename: str = "", format: str = "txt") -> str:
    """将检索结果或任意文本导出为文件（txt/md/html），写入 iCloud 交付目录并返回 URL。

    Args:
        content: 要导出的文本内容（必填）
        filename: 文件名（不含扩展名），留空自动生成
        format: txt / md / html
    """
    _setup_logging()
    if not content or not content.strip():
        return "内容为空。"
    fmt = (format or "txt").lower()
    if fmt not in ("txt", "md", "html"):
        fmt = "txt"
    name = filename.strip() or f"export_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    name = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", name).strip("_") or "export"
    target = _ensure_delivery_dir()
    full, dest, counter = f"{name}.{fmt}", target / f"{name}.{fmt}", 1
    while dest.exists():
        full = f"{name}_{counter}.{fmt}"
        dest = target / full
        counter += 1
    dest.write_text(content, encoding="utf-8")
    logger.info("export_result | %s | %s B", full, len(content.encode()))
    return json.dumps({"status": "ok", "filename": full,
                       "url": f"{EXTERNAL_BASE}/files/{full}",
                       "size_bytes": len(content.encode()),
                       "icloud_sync": "已写入 iCloud，iPhone「文件」App 可见"},
                      ensure_ascii=False, indent=2)


@mcp.tool()
def deliver_file(file_path: str, filename: str = "") -> str:
    """将已生成的任意格式文件（docx/pptx/xlsx/pdf 等）原样复制到 iCloud 交付目录并返回 URL。

    Args:
        file_path: 源文件绝对路径（必填）
        filename: 交付后文件名（留空沿用源文件名）
    """
    _setup_logging()
    src = (file_path or "").strip()
    if not src:
        return "文件路径不能为空。"
    src_path = Path(os.path.expanduser(src)).resolve()
    if not src_path.is_file():
        return f"文件不存在：{src_path}"
    if filename and filename.strip():
        base = re.sub(r"[^\w\u4e00-\u9fff\-]", "_", filename.strip()).strip("_") or "file"
        full = f"{base}{src_path.suffix}"
    else:
        full = src_path.name
    target = _ensure_delivery_dir()
    dest, counter = target / full, 1
    while dest.exists() and dest.stat().st_size != src_path.stat().st_size:
        stem, suf = Path(full).stem, Path(full).suffix
        dest = target / f"{stem}_{counter}{suf}"
        counter += 1
    if not dest.exists():
        shutil.copy2(src_path, dest)
    logger.info("deliver_file | %s -> %s", src_path, dest.name)
    return json.dumps({"status": "ok", "filename": dest.name,
                       "url": f"{EXTERNAL_BASE}/files/{dest.name}",
                       "size_bytes": dest.stat().st_size,
                       "format": dest.suffix.lstrip(".") or "unknown",
                       "icloud_sync": "已写入 iCloud（原格式不变）"},
                      ensure_ascii=False, indent=2)


def _build_http_app():
    """HTTP 模式：/files 静态路由 + 鉴权中间件（复用 law-search 模式）。"""
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    delivery = _ensure_delivery_dir()
    token = os.environ.get("JUDGMENT_TOKEN", "abcd1234ef")
    public = frozenset({"/health", "/healthz", "/", "/favicon.ico"})

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if path in public or path.startswith("/files/"):
                return await call_next(request)
            auth = request.headers.get("authorization", "")
            if auth != f"Bearer {token}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)

    async def health(_r):
        dbs = _available_dbs()
        total = 0
        for p in dbs:
            try:
                con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
                # max(id) 瞬时返回（COUNT(*) 在千万级表上需数秒）
                total += con.execute("SELECT COALESCE(MAX(id),0) FROM docs").fetchone()[0]
                con.close()
            except sqlite3.Error:
                pass
        return JSONResponse({"service": "judgment-search", "years": len(dbs),
                             "docs_total": total, "status": "ok"})

    async def root(_r):
        return PlainTextResponse(
            "judgment-search MCP server\n"
            "- endpoint: /mcp (streamable HTTP)\n"
            "- files:    /files/<filename>\n"
            f"- auth:     Bearer Token ({token[:4]}****)\n")

    # stateless_http：不维护会话。检索类工具调用可能长达 60s+，客户端超时后
    # 取消请求会触发 mcp 库的 "Request already responded to" 断言并让整个会话
    # 崩溃，之后同一 Mcp-Session-Id 的调用全部返回 "Session not found"。
    # 无状态模式下每次请求独立处理，取消不会再污染后续调用。
    mcp_asgi = mcp.http_app(path="/mcp", stateless_http=True)
    return Starlette(
        lifespan=mcp_asgi.lifespan,
        middleware=[Middleware(AuthMiddleware)],
        routes=[
            Route("/", root),
            Route("/health", health),
            Route("/healthz", health),
            Mount("/files", app=StaticFiles(directory=str(delivery), html=False)),
            Mount("/", app=mcp_asgi),
        ],
    )


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", default="stdio", choices=["stdio", "http"])
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8766)
    args = ap.parse_args()
    _setup_logging()

    if args.transport == "http":
        import uvicorn
        app = _build_http_app()
        logger.info("judgment-search http on %s:%s | dbs=%s",
                    args.host, args.port, len(_available_dbs()))
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
