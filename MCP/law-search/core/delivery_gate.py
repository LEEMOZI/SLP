"""P0-2：交付校验与 manifest。

正式交付必须带 case_id 和 source_docs；交付前抽取案号、日期、金额与
源文件比对。案号不一致时阻断交付，文件进入 _quarantine。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("law-search.delivery")

# 覆盖：（2025）京0105民初90109号、（2026）京03民终13295号、
# （2025）最高法民申123号 等；括号后允许中文/字母/数字混合。
_CASE_RE = re.compile(
    r"[（(]\s*\d{4}\s*[）)]\s*[^\s，。；、,;]{1,60}?号"
)
_DATE_RE = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日")
_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*万元")


def _norm_case(value: str) -> str:
    return (
        value.replace("（", "(")
        .replace("）", ")")
        .replace(" ", "")
        .replace("\u3000", "")
    )


def _norm_date(value: str) -> str:
    return value.replace(" ", "").replace("\u3000", "")


def _norm_amount(value: str) -> str:
    return value.replace(" ", "").replace("\u3000", "")


def sanitize_case_id(case_id: str) -> str:
    """把 case_id 限制为可做目录名的安全字符串。"""
    value = (case_id or "").strip()
    value = re.sub(r'[\\/:*?"<>|\s]+', "_", value)
    return value[:80].strip("._") or ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_text(path: Path) -> str:
    if path.suffix.lower() in {".txt", ".md", ".html", ".htm", ".csv", ".json"}:
        return path.read_text(encoding="utf-8", errors="ignore")

    from core.contract_review import extract_text

    text, _meta = extract_text(str(path))
    return text or ""


def extract_case_numbers(text: str) -> List[str]:
    # PDF 提取常在案号中夹空格/换行，例如「（2026）京 03 民终 13295 号」。
    compact = re.sub(r"\s+", "", text or "")
    return sorted({_norm_case(m.group(0)) for m in _CASE_RE.finditer(compact)})


def extract_dates(text: str) -> List[str]:
    return sorted({_norm_date(m.group(0)) for m in _DATE_RE.finditer(text or "")})


def extract_amounts(text: str) -> List[str]:
    return sorted({_norm_amount(m.group(0)) for m in _AMOUNT_RE.finditer(text or "")})


def verify_delivery(
    deliverable_path: Path,
    source_docs: Any = None,
) -> Dict[str, Any]:
    """校验交付物与源文件的关键事实。"""
    result: Dict[str, Any] = {
        "status": "unverified",
        "mismatches": [],
        "warnings": [],
        "source_docs": [],
        "facts": {
            "case_numbers": [],
            "dates": [],
            "amounts": [],
        },
    }

    if isinstance(source_docs, str):
        sources = [
            x.strip()
            for x in source_docs.replace("\n", ",").split(",")
            if x.strip()
        ]
    else:
        sources = [str(x).strip() for x in (source_docs or []) if str(x).strip()]

    result["source_docs"] = sources

    if not sources:
        result["warnings"].append("未提供 source_docs，交付物标记为 unverified")
        return result

    source_texts: List[str] = []
    for raw in sources:
        path = Path(raw).expanduser()
        info = {"path": str(path), "exists": path.exists()}
        if not path.exists():
            result["mismatches"].append(f"源文件不存在: {path}")
            result["source_docs_info"] = result.get("source_docs_info", [])
            result["source_docs_info"].append(info)
            continue
        try:
            text = _read_text(path)
            info["sha256"] = sha256_file(path)
            info["chars"] = len(text)
            if text.strip():
                source_texts.append(text)
            else:
                result["warnings"].append(f"源文件无可提取文本: {path}")
        except Exception as exc:
            info["error"] = type(exc).__name__
            result["warnings"].append(f"源文件读取失败: {path} ({type(exc).__name__})")
        result["source_docs_info"] = result.get("source_docs_info", [])
        result["source_docs_info"].append(info)

    if not source_texts:
        result["status"] = "blocked"
        result["mismatches"].append("所有源文件均无法提取文本，不能校验交付物")
        return result

    try:
        deliverable_text = _read_text(deliverable_path)
    except Exception as exc:
        result["status"] = "blocked"
        result["mismatches"].append(
            f"交付物无法提取文本: {deliverable_path} ({type(exc).__name__})"
        )
        return result

    source_all = "\n".join(source_texts)

    src_cases = set(extract_case_numbers(source_all))
    dst_cases = set(extract_case_numbers(deliverable_text))
    src_dates = set(extract_dates(source_all))
    dst_dates = set(extract_dates(deliverable_text))
    src_amounts = set(extract_amounts(source_all))
    dst_amounts = set(extract_amounts(deliverable_text))

    result["facts"] = {
        "case_numbers": sorted(dst_cases),
        "dates": sorted(dst_dates),
        "amounts": sorted(dst_amounts),
    }

    missing_cases = sorted(dst_cases - src_cases)
    if missing_cases:
        result["mismatches"].append(
            "交付物中的案号未在源文件中出现: " + "、".join(missing_cases)
        )
    if src_cases and not dst_cases:
        result["mismatches"].append("交付物未识别到任何案号，但源文件包含案号")
    if dst_cases and not src_cases:
        result["mismatches"].append("源文件未识别到案号，无法确认交付物案号")

    missing_dates = sorted(dst_dates - src_dates)
    if missing_dates:
        result["warnings"].append(
            "交付物日期未在源文件中出现: " + "、".join(missing_dates)
        )

    missing_amounts = sorted(dst_amounts - src_amounts)
    if missing_amounts:
        result["warnings"].append(
            "交付物金额未在源文件中出现: " + "、".join(missing_amounts)
        )

    if result["mismatches"]:
        result["status"] = "blocked"
    elif dst_cases or src_cases:
        result["status"] = "pass"
    else:
        result["status"] = "unverified"
        result["warnings"].append("源文件和交付物均未识别到案号")

    return result


def case_dir_for(delivery_dir: Path, case_id: str) -> Tuple[Path, str]:
    """返回案件目录与安全 case_id；空 case_id 生成 draft_ 目录。"""
    safe = sanitize_case_id(case_id)
    if not safe:
        safe = time.strftime("draft_%Y%m%d_%H%M%S")
    target = delivery_dir / safe
    target.mkdir(parents=True, exist_ok=True)
    return target, safe


def quarantine_dir_for(delivery_dir: Path, case_id: str) -> Path:
    safe = sanitize_case_id(case_id) or "draft"
    target = delivery_dir / "_quarantine" / safe
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_manifest(
    case_dir: Path,
    *,
    case_id: str,
    deliverable: Path,
    source_docs: Any,
    verification: Dict[str, Any],
    tool: str,
    status: str,
) -> Path:
    """把一次交付写入案件级 manifest.json。"""
    manifest_path = case_dir / "manifest.json"
    data: Dict[str, Any] = {
        "case_id": case_id,
        "updated_at": time.time(),
        "entries": [],
    }
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Existing manifest unreadable; rebuilding: %s", manifest_path)

    data["updated_at"] = time.time()
    data.setdefault("entries", []).append(
        {
            "created_at": time.time(),
            "tool": tool,
            "status": status,
            "deliverable": {
                "path": str(deliverable),
                "sha256": sha256_file(deliverable) if deliverable.exists() else None,
                "bytes": deliverable.stat().st_size if deliverable.exists() else None,
            },
            "source_docs": verification.get("source_docs", []),
            "verification": verification,
        }
    )

    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(manifest_path)
    return manifest_path
