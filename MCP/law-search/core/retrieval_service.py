"""
core/retrieval_service.py — 统一检索层（Chroma + Embedding + Reranker）

RAG-12.11 版本。消除历史上 server.py 与 core/* 的重复实现。
作为 LegalPipeline 的唯一检索入口，同时也可独立被脚本/测试调用。

关键设计：
  - **默认宽进严出**：where 条件不再使用 metadata.law_name（填充率 ~13%）
    精确匹配，改为优先使用 document_title/filename（填充率 100%）
    的 $contains 匹配；条文号使用「阿拉伯数字版 $or 中文数字版」。
  - **字段补全**：chroma_to_evidence_items 输出后立即做程序化字段
    补齐（law_name / article / title / source_document / authority /
    page），确保下游 citation_builder 能拿到完整的引用定位信息。
  - **Schema 单点归一化（P0-2）**：_enrich_evidence_fields 后立刻
    调 evidence_schema.normalize_evidence — 保证检索端输出的顶层
    字段，与外部非 Chroma 证据进入 Pipeline 的格式化路径完全一致，
    不再存在「retrieval_adapter 平铺 vs schema 层再 normalize」
    的双重实现漂移。
  - **懒加载**：Embedding / Reranker / Chroma 全部 lazy init，
    支持「import 不加载」，适合 stdio MCP server 的快速冷启动。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import chromadb
from FlagEmbedding import FlagReranker
from huggingface_hub import snapshot_download
from sentence_transformers import SentenceTransformer

from config import (
    CHROMA_PATH,
    COLLECTION_NAME,
    EMBEDDING_MODEL,
    RERANKER_MODEL,
)
from core.retrieval_adapter import chroma_to_evidence_items
from core.evidence_schema import normalize_evidence  # schema 单点归一化
from core.query_understanding import understand  # P1-1 QU 层

logger = logging.getLogger("law-search.retrieval")

# ==========================================================================
# Constants
# ==========================================================================

INITIAL_CANDIDATES: int = 60  # 粗排候选数（从 30 → 60，宽进严出）
DEFAULT_TOP_K: int = 8       # 精排返回数

# 用户问题中法条编号关键词 → 用于 $contains 匹配的文件名/标题关键词
# （metadata.law_name 填充率仅 ~13%，不能做精确匹配 where 条件）
LAW_KEYWORDS_FOR_CONTAINS: List[str] = [
    "公司法", "民法典", "刑法", "合同法", "劳动合同法", "证券法",
    "行政诉讼法", "民事诉讼法", "刑事诉讼法", "合伙企业法",
    "企业破产法", "保险法", "票据法", "反垄断法", "反不正当竞争法",
    "著作权法", "专利法", "商标法", "个人所得税法", "企业所得税法",
    "税收征收管理法", "未成年人保护法", "妇女权益保障法", "消费者权益保护法",
    "道路交通安全法", "食品安全法", "环境保护法", "土地管理法",
    "物权法", "侵权责任法", "担保法", "婚姻法", "继承法", "收养法",
    "仲裁法", "行政复议法", "国家赔偿法", "立法法",
    "商业银行法", "招标投标法", "会计法", "审计法",
]


# ==========================================================================
# 中文数字 ↔ 阿拉伯数字 转换（用于条文号 $or 匹配）
# ==========================================================================

_CN_DIGIT = "零〇一二三四五六七八九"
_AR_DIGIT = "00123456789"

# 「第三百一十二条」→ 312 的解析只需要 个十百千 四位
_UNIT_MAP = {"十": 10, "百": 100, "千": 1000}


def _cn_num_to_int(cn: str) -> Optional[int]:
    """解析中文数字（仅覆盖条文号场景：个~千位）。失败返回 None。"""
    cn = cn.strip()
    if not cn:
        return None
    if cn.isdigit():
        return int(cn)
    total = 0
    current = 0
    last_unit = 1
    has_unit = False
    try:
        for ch in cn:
            if ch in _CN_DIGIT:
                digit = {
                    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2,
                    "三": 3, "四": 4, "五": 5, "六": 6,
                    "七": 7, "八": 8, "九": 9,
                }[ch]
                current = digit
                last_unit = 1
            elif ch in _UNIT_MAP:
                unit = _UNIT_MAP[ch]
                total += (current or 1) * unit
                current = 0
                has_unit = True
                last_unit = unit
            else:
                return None
        if current:
            total += current * (last_unit if has_unit and last_unit > 1 else 1)
        return total if total > 0 or cn in ("零", "〇") else None
    except Exception:
        return None


def _int_to_cn_num(n: int) -> str:
    """小范围 int → 中文数字（条文号用，上限到千万足够）。"""
    if n < 0:
        return ""
    digits = ["零", "一", "二", "三", "四", "五", "六", "七", "八", "九"]
    units = ["", "十", "百", "千", "万"]
    s = str(n)
    if len(s) == 1:
        return digits[n]
    result = ""
    length = len(s)
    for i, ch in enumerate(s):
        d = int(ch)
        pos = length - 1 - i
        if d == 0:
            if not result.endswith("零") and i < length - 1:
                result += "零"
            continue
        result += digits[d] + units[pos]
    # 常见的「一十三」→ 「十三」简写
    result = result.rstrip("零")
    if result.startswith("一十"):
        result = result[1:]
    return result


def article_number_variants(article_text: str) -> List[str]:
    """
    根据「第X条」原文，生成阿拉伯+中文两种变体用于 $or where。

    例：
        "第3条"   → ["第3条", "第三条"]
        "第三条" → ["第3条", "第三条"]
        "第一百八十八条" → ["第188条", "第一百八十八条"]
    """
    m = re.match(
        r"第\s*([零〇一二三四五六七八九十百千万两\d]+)\s*条",
        article_text.strip(),
    )
    if not m:
        return [article_text.strip()]
    body = m.group(1)
    as_int = _cn_num_to_int(body)
    if as_int is None:
        if body.isdigit():
            as_int = int(body)
        else:
            return [article_text.strip()]
    return [f"第{as_int}条", f"第{_int_to_cn_num(as_int)}条"]


# ==========================================================================
# 法条号 & 法律名提取
# ==========================================================================

_ARTICLE_PATTERN = re.compile(r"第\s*[一二三四五六七八九十百千万零〇两\d]+\s*条")


def extract_article_number(text: str) -> Optional[str]:
    """从问题中提取「第X条」编号（保留原文写法）。"""
    if not text:
        return None
    match = _ARTICLE_PATTERN.search(text)
    return match.group(0) if match else None


def detect_law_keywords(text: str) -> List[str]:
    """从问题中识别法律关键词（允许命中多个，用于 $or $contains 匹配）。"""
    if not text:
        return []
    # 按关键词长度从长到短排序，保证「劳动合同法」比「合同法」先匹配
    ordered = sorted(set(LAW_KEYWORDS_FOR_CONTAINS), key=len, reverse=True)
    hits = []
    for kw in ordered:
        if kw in text:
            hits.append(kw)
    return hits


# ==========================================================================
# Evidence Items 字段程序化补全（步骤 4：law_name / article / source_document 等）
# ==========================================================================

# 从文档标题/文件名中提取法律名的正则（优先匹配「中华人民共和国XX法」等正式格式）
_LAW_NAME_PATTERNS: List[re.Pattern] = [
    re.compile(r"(中华人民共和国[\u4e00-\u9fa5]{1,20}法)"),
    re.compile(r"(最高人民法院[\u4e00-\u9fa5]{1,30}?)"),
    re.compile(r"([\u4e00-\u9fa5]{2,20}法典)"),
    re.compile(r"([\u4e00-\u9fa5]{2,20}条例)"),
    re.compile(r"([\u4e00-\u9fa5]{2,20}规定)"),
    re.compile(r"([\u4e00-\u9fa5]{2,20}办法)"),
    re.compile(r"([\u4e00-\u9fa5]{2,20}解释)"),
]


def _infer_law_name(item: Dict[str, Any]) -> str:
    """程序化推断 law_name（用于 metadata.law_name 缺失时的兜底）。"""
    # 1) 已填充则直接返回
    existing = (item.get("law_name") or "").strip()
    if existing:
        return existing
    existing2 = (item.get("citation_name") or "").strip()
    if existing2 and len(existing2) >= 4:
        return existing2

    # 2) 从 document_title / filename 正则提取
    for field in ("document_title", "filename", "title"):
        val = str(item.get(field, "") or "")
        for pat in _LAW_NAME_PATTERNS:
            m = pat.search(val)
            if m:
                return m.group(1)

    # 3) 保留 document_title 的核心部分（去括号说明，如（2024）（含条文释义））
    dt = str(item.get("document_title", "") or "").strip()
    if dt:
        dt_clean = re.sub(r"[（(].*?[)）]", "", dt).strip()
        if dt_clean and 4 <= len(dt_clean) <= 40:
            return dt_clean
        # 最后 fallback：用原 document_title
        return dt[:40]

    fn = str(item.get("filename", "") or "").strip()
    if fn:
        # 去掉扩展名
        return fn.rsplit(".", 1)[0][:40]

    return "未知来源"


def _infer_authority(item: Dict[str, Any]) -> float:
    """推断 authority（1~100，越高越权威）。"""
    try:
        existing = float(item.get("authority") or 0)
        if existing > 0:
            return existing
    except (TypeError, ValueError):
        pass
    # 从 source_priority 推断（填充率 100%）
    try:
        sp = float(item.get("source_priority") or 0)
        if 0 < sp <= 10:
            return sp * 10
        if sp > 10:
            return min(sp, 100.0)
    except (TypeError, ValueError):
        pass
    # 从 law_identity 推断（填充率 100%）
    identity_map = {
        "正式法律": 100,
        "司法解释": 90,
        "行政法规": 85,
        "部门规章": 75,
        "地方法规": 70,
        "裁判规则": 65,
        "司法观点": 60,
        "专业参考": 50,
        "学理观点": 40,
        "其他": 30,
    }
    li = str(item.get("law_identity", "") or "")
    if li in identity_map:
        return float(identity_map[li])
    # 从 source_category 推断（填充率 100%）
    cat = str(item.get("source_category", "") or "")
    cat_map = {
        "法律规定": 100,
        "司法解释": 90,
        "最高院观点": 70,
        "学理观点": 40,
        "专业参考": 30,
        "其他观点": 20,
    }
    return float(cat_map.get(cat, 50.0))


def _get_meta(item: Dict[str, Any], key: str, default: Any = "") -> Any:
    """优先从顶层取，否则从 metadata 子字典取（补 retrieval_adapter 的字段遗漏）。"""
    top = item.get(key)
    if top not in (None, ""):
        return top
    meta = item.get("metadata")
    if isinstance(meta, dict):
        v = meta.get(key)
        if v not in (None, ""):
            return v
    return default


def _enrich_evidence_fields(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    对 chroma_to_evidence_items 输出做程序化字段补齐（步骤 4 核心）。

    修复：
        - article           ← citation_article ← article_list
        - document_title / filename ↑ 从子 metadata 提升到顶层（填充率 100%）
        - title             ← document_title  ← filename
        - page              ← page_number
        - law_name          ← citation_name → 正则从 document_title/filename 提取
        - authority         ← source_priority → law_identity → source_category 推断
        - source_document   ← document_title + filename
    """
    for item in items:
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}

        # --- 提升 document_title / filename 到顶层（它们填充率 100%，retrieval_adapter 漏提了）---
        for k in ("document_title", "filename", "law_part", "source_document"):
            if k not in item or item[k] in (None, ""):
                if meta.get(k) not in (None, ""):
                    item[k] = meta[k]

        # --- article ---
        if not item.get("article"):
            item["article"] = (
                item.get("citation_article")
                or item.get("article_list")
                or meta.get("citation_article")
                or meta.get("article_list")
                or ""
            )
        # --- title（retrieval_adapter 取的是 meta.title，填充率 0%；要换成 document_title）---
        if not item.get("title"):
            item["title"] = (
                _get_meta(item, "document_title")
                or _get_meta(item, "filename")
                or ""
            )
        # --- page ---
        if not item.get("page"):
            item["page"] = _get_meta(item, "page_number") or ""
        # --- law_name ---
        item["law_name"] = _infer_law_name(item)
        # --- authority ---
        item["authority"] = _infer_authority(item)
        # --- source_document ---
        if not item.get("source_document"):
            dt = str(_get_meta(item, "document_title") or "")
            fn = str(_get_meta(item, "filename") or "")
            if dt and fn and dt != fn:
                item["source_document"] = f"{dt} ({fn})"
            else:
                item["source_document"] = dt or fn or ""

    return items


# ==========================================================================
# LegalRetriever
# ==========================================================================

class LegalRetriever:
    """统一检索服务：Embedding → Chroma → Reranker → Evidence Items。"""

    def __init__(
        self,
        chroma_path: str = CHROMA_PATH,
        collection_name: str = COLLECTION_NAME,
        embedding_model_id: str = EMBEDDING_MODEL,
        reranker_model_id: str = RERANKER_MODEL,
    ) -> None:
        self.chroma_path = chroma_path
        self.collection_name = collection_name
        self.embedding_model_id = embedding_model_id
        self.reranker_model_id = reranker_model_id

        self._chroma_client: Optional[chromadb.PersistentClient] = None
        self._collection: Optional[Any] = None
        self._embedding_model: Optional[SentenceTransformer] = None
        self._reranker: Optional[FlagReranker] = None

    # ------------------------------------------------------------------
    # Lazy init
    # ------------------------------------------------------------------

    @property
    def collection(self):
        if self._collection is None:
            logger.info("Chroma init: path=%s", self.chroma_path)
            self._chroma_client = chromadb.PersistentClient(path=self.chroma_path)
            self._collection = self._chroma_client.get_collection(self.collection_name)
            logger.info(
                "Chroma ready: collection=%s chunks=%s",
                self.collection_name, self._collection.count(),
            )
        return self._collection

    def _ensure_embedding(self) -> SentenceTransformer:
        if self._embedding_model is None:
            started = time.perf_counter()
            logger.info("Loading embedding model: %s", self.embedding_model_id)
            try:
                local_path = snapshot_download(
                    repo_id=self.embedding_model_id, local_files_only=False,
                )
            except Exception:
                logger.warning("Cache miss for %s, downloading...", self.embedding_model_id)
                local_path = snapshot_download(repo_id=self.embedding_model_id)
            self._embedding_model = SentenceTransformer(local_path)
            logger.info("Embedding ready in %.2fs", time.perf_counter() - started)
        return self._embedding_model

    def _ensure_reranker(self) -> FlagReranker:
        if self._reranker is None:
            started = time.perf_counter()
            logger.info("Loading reranker model: %s", self.reranker_model_id)
            try:
                local_path = snapshot_download(
                    repo_id=self.reranker_model_id, local_files_only=True,
                )
            except Exception:
                logger.warning("Cache miss for %s, downloading...", self.reranker_model_id)
                local_path = snapshot_download(repo_id=self.reranker_model_id)
            self._reranker = FlagReranker(local_path, use_fp16=True)
            logger.info("Reranker ready in %.2fs", time.perf_counter() - started)
        return self._reranker

    def warmup(self) -> None:
        """预加载全部懒加载组件（embedding/reranker/Chroma 双集合）。

        常驻 MCP 场景（DSH stdio spawn）下由 server 启动时后台调用，
        把 ~3 分钟的冷启动挪到服务就绪前，用户首次检索免付冷启动开销。
        幂等：组件已加载时为 no-op。"""
        started = time.perf_counter()
        self._ensure_embedding()
        self._ensure_reranker()
        _ = self.collection
        _ = self.case_collection
        logger.info("Retriever warmup done in %.2fs", time.perf_counter() - started)

    # ------------------------------------------------------------------
    # Core ops
    # ------------------------------------------------------------------

    def embed_query(self, query: str) -> List[float]:
        model = self._ensure_embedding()
        return model.encode(query).tolist()

    def _build_where_clause(
        self,
        article_number: Optional[str],
        law_keywords: List[str],
    ) -> Optional[Dict[str, Any]]:
        """
        构建 Chroma where 条件（填充率友好版）。

        策略：
          - 条文号：阿中两版 $or。
          - 法律名：document_title/filename 的 $contains $or（100% 填充）。
          - 两种都命中 → $and；否则只加对应的子句。
          - 任何情况下**不再**使用 metadata.law_name 精确匹配（填充率 13%）。
        """
        subclauses: List[Dict[str, Any]] = []

        # (a) 条文号：阿中两版
        if article_number:
            variants = article_number_variants(article_number)
            if len(variants) == 1:
                subclauses.append({"citation_article": variants[0]})
            else:
                subclauses.append(
                    {"$or": [{"citation_article": v} for v in variants]}
                )
            logger.info(
                "Query filter (article): %r → variants=%r",
                article_number, variants,
            )

        # (b) 法律关键词：document_title / filename $contains $or
        if law_keywords:
            per_kw_clauses = []
            for kw in law_keywords:
                per_kw_clauses.append({"document_title": {"$contains": kw}})
                per_kw_clauses.append({"filename": {"$contains": kw}})
            if len(per_kw_clauses) == 1:
                subclauses.append(per_kw_clauses[0])
            else:
                subclauses.append({"$or": per_kw_clauses})
            logger.info(
                "Query filter (law_keywords): %r ($contains over title/filename)",
                law_keywords,
            )

        if not subclauses:
            return None
        if len(subclauses) == 1:
            return subclauses[0]
        return {"$and": subclauses}

    def search_and_rerank(
        self,
        query: str,
        *,
        top_k: int = DEFAULT_TOP_K,
        initial_candidates: int = INITIAL_CANDIDATES,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, float]]:
        """
        端到端：Chroma 粗排 → Reranker 精排 → Evidence Items（字段已补全）。

        Returns:
            (evidence_items, timing_dict)
        """
        clean_query = (query or "").strip()
        if not clean_query:
            return [], {}

        overall = time.perf_counter()
        timing: Dict[str, float] = {}

        # 0. P1-1 Query Understanding（normalize + intent + synonyms + QU扩展 queries + where_hints）
        t0 = time.perf_counter()
        qu = understand(clean_query)
        timing["qu_s"] = time.perf_counter() - t0
        primary_query = qu["normalized"]
        expanded_queries = list(qu["queries"])  # [primary, synonym_replaced, law+art focused]
        qu_hints = qu["where_hints"]
        QU_ARTICLES = list(qu_hints.get("article_numbers") or [])
        QU_LAWS = list(qu_hints.get("law_keywords") or [])
        # 保留原 API：QU 没给出时退回 extract_article_number / detect_law_keywords（legacy shim）
        legacy_art = extract_article_number(primary_query)
        legacy_laws = detect_law_keywords(primary_query)
        article_list: List[str] = []
        for form in QU_ARTICLES:
            if form and form not in article_list:
                article_list.append(form)
        if legacy_art and legacy_art not in article_list:
            article_list.append(legacy_art)
        law_list: List[str] = []
        for lw in QU_LAWS:
            if lw not in law_list:
                law_list.append(lw)
        for lw in legacy_laws:
            if lw not in law_list:
                law_list.append(lw)
        logger.info(
            "QU | intent=%s laws=%s arts=%s | queries=%s",
            qu.get("intent"), law_list, article_list, len(expanded_queries),
        )

        # 1. Embedding（同义替换增强版 query 做 embedding，比口语化原始问法有更紧的召回）
        t0 = time.perf_counter()
        embed_text = qu["synonym_replaced"] or primary_query
        vector = self.embed_query(embed_text)
        timing["embedding_s"] = time.perf_counter() - t0

        # 2. Chroma search（QU 提供的「阿+中双版 article_number」+ QU law_list 做 where）
        #    - 有多个条号时 $or 并起来（「第3条、第50条都怎么规定的」召回两条都命中）
        #    - 不足时回落纯语义 + 扩展查询合并，保证 reranker 候选池足够大
        t0 = time.perf_counter()
        available = self.collection.count()
        candidate_count = min(initial_candidates, available)

        # where 构建：QU 多文章号 → $or；QU law_list → $contains $or；两者都有 → $and
        subclauses: List[Dict[str, Any]] = []
        if article_list:
            art_ors = []
            for form in article_list:
                variants = article_number_variants(form)
                for v in variants:
                    art_ors.append({"citation_article": v})
            if len(art_ors) == 1:
                subclauses.append(art_ors[0])
            else:
                subclauses.append({"$or": art_ors})
        if law_list:
            per_kw = []
            for kw in law_list:
                per_kw.append({"document_title": {"$contains": kw}})
                per_kw.append({"filename": {"$contains": kw}})
            subclauses.append({"$or": per_kw})
        if len(subclauses) == 0:
            where = None
        elif len(subclauses) == 1:
            where = subclauses[0]
        else:
            where = {"$and": subclauses}

        base_kwargs: Dict[str, Any] = {
            "query_embeddings": [vector],
            "n_results": candidate_count,
            "include": ["documents", "metadatas", "distances"],
        }

        def _unpack(res):
            return (
                (res.get("documents") or [[]])[0] or [],
                (res.get("metadatas") or [[]])[0] or [],
                (res.get("distances") or [[]])[0] or [],
            )

        # 2a. 有 where 先按 where → 少了就合并纯语义 + 扩展 QU 查询
        docs, metas, dists = [], [], []
        if where is not None:
            res_filtered = self.collection.query(**base_kwargs, where=where)
            docs, metas, dists = _unpack(res_filtered)
            timing["chroma_filtered"] = float(len(docs))
            if len(docs) < max(6, candidate_count // 2):
                res_open = self.collection.query(**base_kwargs)
                d2, m2, dist2 = _unpack(res_open)
                lists_to_merge = [(docs, metas, dists), (d2, m2, dist2)]
                # 2b. QU 扩展查询补召回（一般 1~2 个额外 query）
                for eq in expanded_queries[1:]:
                    if eq == (qu["synonym_replaced"] or primary_query):
                        # embedding 已经用过的文本不重复
                        continue
                    eq_vec = self.embed_query(eq)
                    eq_kwargs = dict(base_kwargs)
                    eq_kwargs["query_embeddings"] = [eq_vec]
                    try:
                        eq_res = self.collection.query(**eq_kwargs)
                    except Exception as exc:
                        logger.warning("扩展查询 Chroma 失败 %r: %s", eq[:30], exc)
                        continue
                    d3, m3, dist3 = _unpack(eq_res)
                    lists_to_merge.append((d3, m3, dist3))
                # 去重合并
                seen = set()
                merged_d, merged_m, merged_dist = [], [], []
                for lst in lists_to_merge:
                    for dd, mm, ddst in zip(*lst):
                        key = (
                            str(mm.get("document_title", ""))
                            + "|" + str(mm.get("citation_article", ""))
                            + "|" + str(mm.get("page_number", ""))
                            + "|" + dd[:80]
                        )
                        if key in seen:
                            continue
                        seen.add(key)
                        merged_d.append(dd)
                        merged_m.append(mm)
                        merged_dist.append(ddst)
                docs, metas, dists = merged_d, merged_m, merged_dist
                timing["chroma_merged"] = float(len(docs))
            logger.info(
                "Chroma (QU where): %s candidates (merged open+expanded if needed)",
                len(docs),
            )
        else:
            res_open = self.collection.query(**base_kwargs)
            docs, metas, dists = _unpack(res_open)
            logger.info("Chroma (open, no where): %s candidates", len(docs))

        timing["chroma_s"] = time.perf_counter() - t0
        timing["candidates"] = float(len(docs))
        timing["queries_n"] = float(len(expanded_queries))
        timing["intent"] = float({"article_lookup":1,"definition":2,"case_analysis":3,"procedural":4,"general":0}.get(qu.get("intent"), 0))

        if not docs:
            timing["total_s"] = time.perf_counter() - overall
            return [], timing

        # 3. Rerank（rerank 用 primary QU-normalized query 做 pair，更贴合口语表述）
        t0 = time.perf_counter()
        reranker = self._ensure_reranker()
        pairs = [[primary_query, doc] for doc in docs]
        scores = reranker.compute_score(pairs)
        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        else:
            scores = [float(s) for s in scores]
        timing["rerank_s"] = time.perf_counter() - t0

        # 4. 重排排序 + top_k
        ranked = sorted(
            zip(scores, docs, metas, dists),
            key=lambda x: x[0], reverse=True,
        )
        requested = max(1, int(top_k))
        selected = ranked[:requested]

        sel_scores = [s for s, _d, _m, _dist in selected]
        sel_docs = [d for _s, d, _m, _dist in selected]
        sel_metas = [m for _s, _d, m, _dist in selected]
        sel_dists = [dd for _s, _d, _m, dd in selected]

        # 5. 统一 Evidence Item 结构（retrieval_adapter 仅做浅映射，只保证 4 字段）
        chroma_format = {
            "documents": [sel_docs],
            "metadatas": [sel_metas],
            "distances": [sel_dists],
        }
        items = chroma_to_evidence_items(chroma_format, rerank_scores=sel_scores)

        # 6. 程序化字段补全（步骤 4 的核心 — 修复 law_name/article/authority 等）
        items = _enrich_evidence_fields(items)

        # 7. Schema 单点归一化（P0-2）
        #    不管 evidence 来自 Chroma 还是外部推送，都必须过本函数一次。
        #    顶层 26 个字段：统一类型 + 枚举收敛 + 空白 strip + 默认值。
        items = [normalize_evidence(item) for item in items if isinstance(item, dict)]

        # 命中日志（摘要）
        for idx, item in enumerate(items):
            title = (item.get("law_name") or item.get("title") or "未知")[:40]
            art = item.get("article") or item.get("citation_article") or "—"
            rs = item.get("rerank_score", 0) or 0
            auth = item.get("authority", "?")
            logger.info(
                "Hit %2d | rerank=%.4f auth=%s | %s | %s",
                idx + 1, rs, auth, title, art,
            )

        timing["total_s"] = time.perf_counter() - overall
        return items, timing

    # ------------------------------------------------------------------
    # 案例检索（case_library：指导性案例等），与法条检索链路独立
    # ------------------------------------------------------------------

    _case_collection: Optional[Any] = None

    @property
    def case_collection(self):
        if self._case_collection is None:
            if self._chroma_client is None:
                logger.info("Chroma init: path=%s", self.chroma_path)
                self._chroma_client = chromadb.PersistentClient(path=self.chroma_path)
            t0 = time.perf_counter()
            self._case_collection = self._chroma_client.get_collection("case_library")
            logger.info(
                "Chroma ready: collection=case_library chunks=%s (%.2fs)",
                self._case_collection.count(), time.perf_counter() - t0,
            )
        return self._case_collection

    def search_cases(self, query: str, top_k: int = 8):
        """检索指导性案例/案例语料。返回 (items, timing)，不做法条字段补全。"""
        return self._search_plain_collection(
            self.case_collection, query, top_k, log_tag="CaseHit",
        )

    # ------------------------------------------------------------------
    # 工具书/业务指引检索（reference_library：业务指南、操作指引等），
    # 链路与案例检索相同：embed → Chroma → rerank → 归一，不做法条补全
    # ------------------------------------------------------------------

    _reference_collection: Optional[Any] = None

    @property
    def reference_collection(self):
        if self._reference_collection is None:
            if self._chroma_client is None:
                logger.info("Chroma init: path=%s", self.chroma_path)
                self._chroma_client = chromadb.PersistentClient(path=self.chroma_path)
            t0 = time.perf_counter()
            self._reference_collection = self._chroma_client.get_collection(
                "reference_library"
            )
            logger.info(
                "Chroma ready: collection=reference_library chunks=%s (%.2fs)",
                self._reference_collection.count(), time.perf_counter() - t0,
            )
        return self._reference_collection

    def search_reference(self, query: str, top_k: int = 8):
        """检索工具书/业务学习/操作指引语料。返回 (items, timing)。"""
        return self._search_plain_collection(
            self.reference_collection, query, top_k, log_tag="RefHit",
        )

    def _search_plain_collection(
        self, collection: Any, query: str, top_k: int, log_tag: str = "Hit",
    ):
        """通用向量检索链路（案例/工具书等非法条库共用）：embed → query → rerank。

        返回 (items, timing)；不跑法条字段补全（metadata 非法条结构）。
        """
        overall = time.perf_counter()
        timing: Dict[str, Any] = {}
        q = (query or "").strip()
        if not q:
            return [], timing

        # 1. 向量化（复用法条检索的 embedding 缓存基建）
        t0 = time.perf_counter()
        q_vec = self.embed_query(q)
        timing["embed_s"] = time.perf_counter() - t0

        # 2. Chroma 语义查询（open，无 where——非法条库不用法条 where 语义）
        t0 = time.perf_counter()
        candidate_count = min(INITIAL_CANDIDATES, max(top_k * 4, 30))
        res = collection.query(
            query_embeddings=[q_vec],
            n_results=candidate_count,
            include=["documents", "metadatas", "distances"],
        )
        docs = (res.get("documents") or [[]])[0] or []
        metas = (res.get("metadatas") or [[]])[0] or []
        dists = (res.get("distances") or [[]])[0] or []
        timing["chroma_s"] = time.perf_counter() - t0
        timing["candidates"] = float(len(docs))
        if not docs:
            timing["total_s"] = time.perf_counter() - overall
            return [], timing

        # 3. Rerank
        t0 = time.perf_counter()
        reranker = self._ensure_reranker()
        scores = reranker.compute_score([[q, d] for d in docs])
        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        else:
            scores = [float(s) for s in scores]
        timing["rerank_s"] = time.perf_counter() - t0

        # 4. 排序截断
        ranked = sorted(zip(scores, docs, metas, dists), key=lambda x: x[0], reverse=True)
        ranked = ranked[: max(1, int(top_k))]
        sel_scores = [s for s, *_ in ranked]
        sel_docs = [d for _s, d, _m, _dd in ranked]
        sel_metas = [m for _s, _d, m, _dd in ranked]
        sel_dists = [dd for _s, _d, _m, dd in ranked]

        # 5. Schema 归一（跳过 _enrich_evidence_fields：非法条 metadata 结构，
        #    误跑法条补全会把标题错判成 law_name）
        chroma_format = {
            "documents": [sel_docs],
            "metadatas": [sel_metas],
            "distances": [sel_dists],
        }
        items = chroma_to_evidence_items(chroma_format, rerank_scores=sel_scores)
        items = [normalize_evidence(item) for item in items if isinstance(item, dict)]

        for idx, item in enumerate(items):
            logger.info(
                "%s %2d | rerank=%.4f | %s",
                log_tag,
                idx + 1, item.get("rerank_score", 0) or 0,
                (item.get("title") or item.get("document_title") or "未知")[:50],
            )
        timing["total_s"] = time.perf_counter() - overall
        return items, timing


# ==========================================================================
# 模块级默认实例（懒加载）
# ==========================================================================

_DEFAULT_RETRIEVER: Optional[LegalRetriever] = None


def get_retriever() -> LegalRetriever:
    global _DEFAULT_RETRIEVER
    if _DEFAULT_RETRIEVER is None:
        _DEFAULT_RETRIEVER = LegalRetriever()
    return _DEFAULT_RETRIEVER
