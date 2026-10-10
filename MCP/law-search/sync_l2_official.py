# -*- coding: utf-8 -*-
"""sync_l2_official.py — L2 官方源同步定时任务（纯 CPU + Tavily，不加载模型）

定位（三级检索架构）：
    L1 本地向量库（law_library）→ L2 官方源（国家法律法规数据库/官方发布）
    → L3 web_search 查询时复核（pipeline _phase5b，已上线）

本脚本负责 L2 的"定时"部分：
    按周检查 watchlist.yaml 观察清单内法律的官方最新状态
    （最新修订年份 / 施行日期 / 是否废止），与本地库源文件的
    最高版本比对，产出同步报告，标记"本地库可能过期"的法律。

设计约束：
    - 不加载 Embedding/Reranker/Chroma（8GB 机器友好，任何时段可跑）
    - Tavily 免费额度 1000 次/月：9 部法律 × 每周 1 次 ≈ 40 次/月
    - 结论只是"信号"而非法律意见，过期标记需人工确认后再 ingest 更新版

产出：
    logs/l2_sync_report.md       人读报告（覆盖写）
    config/l2_sync_state.json    机器可读状态（含历史 last_checked）

用法：
    python sync_l2_official.py            # 全量检查
    python sync_l2_official.py --limit 2  # 只查前 N 部（调试用）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("law-search.l2sync")

WATCHLIST_PATH = PROJECT_ROOT / "config" / "watchlist.yaml"
PATHS_YAML = PROJECT_ROOT / "config" / "paths.yaml"
REPORT_PATH = PROJECT_ROOT / "logs" / "l2_sync_report.md"
STATE_PATH = PROJECT_ROOT / "config" / "l2_sync_state.json"

# 两次 Tavily 调用间隔（秒）——控速 + 控额度
_SEARCH_INTERVAL_S = 1.5

# 官方结果中的版本/时效信号（正则，best-effort）
_OFFICIAL_REVISION_RE = re.compile(r"(20\d{2})\s*年?\s*[修订]")
_OFFICIAL_EFFECTIVE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日[^。]{0,8}?(?:起)?施行")
_OFFICIAL_REPEALED_RE = re.compile(r"废止|失效")
_OFFICIAL_CURRENT_RE = re.compile(r"现行有效")


# --------------------------------------------------------------------------
# 配置加载（轻量：直接 yaml.safe_load，不 import pipeline）
# --------------------------------------------------------------------------

def load_watchlist() -> Dict[str, Any]:
    import yaml

    with open(WATCHLIST_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    if not cfg.get("enabled", False):
        raise SystemExit("watchlist.yaml enabled=false，L2 同步已禁用。")
    return cfg


def load_law_source_dirs() -> List[str]:
    """从 paths.yaml 取 law_library 的全部 source 路径（目录或文件）。"""
    import yaml

    with open(PATHS_YAML, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    lib = (cfg.get("libraries") or {}).get("law_library") or {}
    if not lib.get("enabled", False):
        return []
    dirs: List[str] = []
    for src in lib.get("sources") or []:
        path = str(src.get("path") or "")
        if path and os.path.exists(path):
            dirs.append(path)
    return dirs


# --------------------------------------------------------------------------
# 本地版本扫描（纯文件名，不碰 Chroma）
# --------------------------------------------------------------------------

def _filename_version_year(filename: str) -> int:
    """从文件名提取修订/版本年份（强信号，复用 doc_classifier 的模式集）。"""
    from core.doc_classifier import _VERSION_PATTERNS

    for pat in _VERSION_PATTERNS:
        m = re.search(pat, filename)
        if m:
            return int(m.group(1))
    return 0


# 弱信号：文件名中的裸年份（"汇编（2024）"、"2024 理解与适用"）——
# 释义/评注/汇编的出版年份，可近似视为"该年份前的法规环境已覆盖"
_WEAK_YEAR_RE = re.compile(r"(20\d{2})")


def _filename_weak_year(filename: str) -> int:
    years = [int(y) for y in _WEAK_YEAR_RE.findall(filename)]
    return max(years) if years else 0


def scan_local_versions(law_names: List[str], source_dirs: List[str]) -> Dict[str, Dict[str, int]]:
    """扫描源目录文件名，返回每个法律名的 (强版本年份, 弱覆盖年份)。"""
    strong: Dict[str, int] = {name: 0 for name in law_names}
    weak: Dict[str, int] = {name: 0 for name in law_names}
    seen_files = 0

    def _feed(filename: str) -> None:
        nonlocal seen_files
        seen_files += 1
        base = os.path.basename(filename)
        for name in law_names:
            if name in base:
                y_strong = _filename_version_year(base)
                if y_strong > strong.get(name, 0):
                    strong[name] = y_strong
                y_weak = max(y_strong, _filename_weak_year(base))
                if y_weak > weak.get(name, 0):
                    weak[name] = y_weak

    for root in source_dirs:
        if os.path.isfile(root):
            _feed(root)
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for fn in filenames:
                if fn.startswith(".") or fn.startswith("._"):
                    continue
                _feed(os.path.join(dirpath, fn))

    logger.info("本地源扫描完成：%d 个文件，覆盖 %d 个源目录", seen_files, len(source_dirs))
    return {name: {"strong": strong[name], "weak": weak[name]} for name in law_names}


# --------------------------------------------------------------------------
# 官方状态检查（Tavily，best-effort 信号提取）
# --------------------------------------------------------------------------

def check_official_status(name: str, query: str, max_results: int) -> Dict[str, Any]:
    """Tavily 查询单部法律的官方最新状态。返回信号字典。

    废止信号判定（防误报）：
        1) 废止/失效词必须出现在法律名 ±40 字窗口内（排除结果页
           无关的"废止汇编"等噪音）；
        2) 同时提取到新修订年份或施行日期 → 视为"旧版废止"的正常
           更替表述（如"原公司法同时废止"），降级为备注而非 ⛔。
    """
    from core.web_search import web_search

    text, meta = web_search(query, max_results=max_results, search_depth="basic")
    if meta.get("error"):
        return {"error": str(meta.get("error")), "query": query}

    revisions = [int(y) for y in _OFFICIAL_REVISION_RE.findall(text)]
    effectives = [
        f"{y}-{int(m):02d}-{int(d):02d}"
        for y, m, d in _OFFICIAL_EFFECTIVE_RE.findall(text)
    ]
    # 法律名短形态（去"中华人民共和国"前缀），用于窗口匹配
    short = re.sub(r"^中华人民共和国", "", name)
    name_re = re.escape(short)
    repeal_near = bool(
        re.search(rf".{{0,40}}{name_re}.{{0,40}}(?:废止|失效)", text)
    )
    has_current_signal = bool(revisions or effectives)
    return {
        "query": query,
        "official_latest_revision": max(revisions) if revisions else None,
        "official_effective_dates": sorted(set(effectives), reverse=True)[:3],
        "mentions_repealed": repeal_near and not has_current_signal,
        "superseded_note": repeal_near and has_current_signal,
        "mentions_current": bool(_OFFICIAL_CURRENT_RE.search(text)),
        "answer_head": text[:300],
    }


def build_verdict(official: Dict[str, Any], local_strong: int, local_weak: int) -> str:
    """根据官方信号与本地版本年份给出核对结论。

    local_strong：文件名明确标注的修订/版本年份（强信号）
    local_weak：  文件名裸年份（汇编/评注出版年，弱覆盖信号）
    """
    if official.get("error"):
        return f"⚠️ 检查失败（{official['error']}），需人工核对"
    if official.get("mentions_repealed"):
        return "⛔ 官方源提及废止/失效，立即人工核对本地版本"
    superseded = "（检索提及旧版废止，属新修订正常更替）" if official.get("superseded_note") else ""
    off_year = official.get("official_latest_revision")
    if off_year and local_strong and off_year > local_strong:
        return f"⚠️ 本地库可能过期：官方最新 {off_year} 年修订，本地标注最高 {local_strong} 年" + superseded
    if off_year and local_strong and off_year <= local_strong:
        return f"✅ 版本一致（本地已覆盖 {local_strong} 年修订）" + superseded
    if off_year and local_weak >= off_year:
        return (
            f"✅ 本地已有 ≥{local_weak} 年资料覆盖（文件名弱信号，"
            "建议抽查确认条文版本）" + superseded
        )
    if off_year and local_weak:
        return f"⚠️ 本地库可能过期：官方最新 {off_year} 年修订，本地弱信号仅到 {local_weak} 年" + superseded
    if off_year:
        return f"❓ 本地源文件未见版本标注，官方最新 {off_year} 年，建议人工核对" + superseded
    if superseded:
        return "✅ 检索结果仅提及旧版废止（正常更替），未见更新版本信号"
    return "❓ 未提取到明确版本信号，建议人工核对"


# --------------------------------------------------------------------------
# launchd 定时任务安装（每周一 08:30）
# --------------------------------------------------------------------------

PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.lawsearch.l2-sync.plist"
LAUNCHD_LOG = PROJECT_ROOT / "logs" / "l2_sync_launchd.log"

PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.lawsearch.l2-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{script}</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{cwd}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Weekday</key>
        <integer>1</integer>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
    <key>StandardOutPath</key>
    <string>{log}</string>
    <key>StandardErrorPath</key>
    <string>{log}</string>
</dict>
</plist>
"""


def install_launchd() -> None:
    import subprocess

    python = sys.executable
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(
        PLIST_TEMPLATE.format(python=python, script=PROJECT_ROOT / "sync_l2_official.py",
                              cwd=PROJECT_ROOT, log=LAUNCHD_LOG),
        encoding="utf-8",
    )
    label = PLIST_PATH.stem
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                   capture_output=True)  # 已存在则先卸载，失败忽略
    subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)], check=True)
    print(f"launchd 定时任务已安装：{PLIST_PATH}")
    print("计划：每周一 08:30 自动执行；日志：" + str(LAUNCHD_LOG))
    print("手动验证：launchctl kickstart gui/" + str(uid) + "/" + label)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="L2 官方源同步定时任务")
    parser.add_argument("--limit", type=int, default=0, help="只检查前 N 部法律（调试）")
    parser.add_argument("--install", action="store_true",
                        help="安装 launchd 周定时任务（每周一 08:30）后退出")
    args = parser.parse_args()

    if args.install:
        install_launchd()
        return

    watchlist = load_watchlist()
    max_results = int(watchlist.get("max_results", 4))
    laws = watchlist.get("laws") or []
    if args.limit > 0:
        laws = laws[: args.limit]
    if not laws:
        raise SystemExit("watchlist.yaml 未配置 laws。")

    # 法律名 = name + aliases（用于本地文件名匹配）
    names_and_aliases: List[str] = []
    for law in laws:
        nm = str(law.get("name") or "").strip()
        if nm:
            names_and_aliases.append(nm)
        for al in law.get("aliases") or []:
            al = str(al).strip()
            if al:
                names_and_aliases.append(al)

    logger.info("L2 同步开始：%d 部法律（本地匹配名 %d 个）", len(laws), len(names_and_aliases))
    local_versions = scan_local_versions(names_and_aliases, load_law_source_dirs())

    rows: List[Dict[str, Any]] = []
    for idx, law in enumerate(laws, 1):
        name = str(law.get("name") or "").strip()
        if not name:
            continue
        query = str(law.get("query") or "").strip() or f"{name} 现行有效 最新修订 施行日期"

        official = check_official_status(name, query, max_results)
        # 本地取主名 + 别名的最高年份
        match_names = [name] + [str(a) for a in (law.get("aliases") or [])]
        local_strong = max((local_versions[n]["strong"] for n in match_names if n in local_versions), default=0)
        local_weak = max((local_versions[n]["weak"] for n in match_names if n in local_versions), default=0)

        verdict = build_verdict(official, local_strong, local_weak)
        rows.append({
            "law": name,
            "local_max_version": local_strong or local_weak or None,
            "local_version_signal": "strong" if local_strong else ("weak" if local_weak else "none"),
            **{k: v for k, v in official.items() if k != "answer_head"},
            "verdict": verdict,
        })
        logger.info("[%d/%d] %s → %s", idx, len(laws), name, verdict)
        if idx < len(laws):
            time.sleep(_SEARCH_INTERVAL_S)

    # ----- 写报告 -----
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "# L2 官方源同步报告",
        "",
        f"- 检查时间：{now}",
        f"- 检查范围：watchlist {len(rows)} 部法律",
        "- 结论为 best-effort 信号，过期标记需人工确认后再决定是否 ingest 更新版",
        "",
        "| 法律 | 本地最高版本 | 官方最新修订 | 官方施行日期(近期) | 废止提及 | 结论 |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        sig = r.get("local_version_signal")
        local_cell = str(r.get("local_max_version") or "—")
        if local_cell != "—" and sig:
            local_cell = f"{local_cell}（{'强' if sig == 'strong' else '弱'}）"
        lines.append(
            "| {law} | {local} | {off} | {eff} | {rep} | {verdict} |".format(
                law=r["law"],
                local=local_cell,
                off=r.get("official_latest_revision") or "—",
                eff="、".join(r.get("official_effective_dates") or []) or "—",
                rep="是" if r.get("mentions_repealed") else "—",
                verdict=r["verdict"],
            )
        )
    outdated = [r["law"] for r in rows if r["verdict"].startswith(("⚠️", "⛔"))]
    if outdated:
        lines += [
            "",
            "## 需人工跟进",
            "",
            *[f"- {name}" for name in outdated],
        ]
    REPORT_PATH.parent.mkdir(exist_ok=True)
    REPORT_PATH.write_text("\n".join(lines), encoding="utf-8")
    logger.info("报告已写入 %s", REPORT_PATH)

    # ----- 写状态 -----
    state: Dict[str, Any] = {}
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
    history: List[Dict[str, Any]] = state.get("history") or []
    history.append({"checked_at": now, "outdated": outdated, "rows": rows})
    state["last_checked"] = now
    state["history"] = history[-12:]  # 保留最近 12 次
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    print(f"\nL2 同步完成：{len(rows)} 部法律，{len(outdated)} 部需人工跟进。")
    print(f"报告：{REPORT_PATH}")


if __name__ == "__main__":
    main()
