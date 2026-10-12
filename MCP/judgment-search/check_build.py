"""judgment-search 全量建库守护与验收脚本（供定时自动化调用，自包含）。

用法：
    python check_build.py            # 巡检：进程状态 / 建库进度 / 磁盘，输出 verdict
    python check_build.py accept     # 全量验收：/health 总量核对 + 抽样检索延迟

verdict 三种：
    running          —— 建库进程存活，正常推进
    dead_incomplete  —— 进程死亡但年份未建完，需排查并重启（用 launchctl，需绕过沙箱）
    complete         —— 1985-2021 全部建完，可执行 accept 验收
"""
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).parent
DB_DIR = BASE / "data" / "judgments"
LOG = BASE / "build_full2.log"
ERR_LOG = BASE / "build_full2.err"
LABEL = "com.judgment.build-full"
ALL_YEARS = [str(y) for y in range(1985, 2022)]  # 37 年
MCP_URL = "http://127.0.0.1:8766/mcp"
TOKEN = "abcd1234ef"


def build_pid() -> int | None:
    r = subprocess.run(["pgrep", "-f", "build_judgment_db.py"], capture_output=True, text=True)
    pids = [int(x) for x in r.stdout.split()]
    return pids[0] if pids else None


def done_years_from_log() -> dict[str, dict]:
    """从 build_full2.log 解析 ✅ 完成行 → {year: stats}（两次运行的历史一并统计）。"""
    done = {}
    if not LOG.exists():
        return done
    for m in re.finditer(r"✅ \[(\d{4})\] 完成: (\{.*?\})", LOG.read_text(errors="replace")):
        try:
            done[m.group(1)] = eval(m.group(2))  # 日志内容为本脚本自身打印的字面量
        except Exception:
            pass
    return done


def marker_done_years() -> set[str]:
    """直接读各库 progress 表 done 标记（最权威，含更早会话建的 1985/2018）。"""
    out = set()
    for p in DB_DIR.glob("judgments_*.db"):
        year = p.stem.replace("judgments_", "")
        try:
            con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
            row = con.execute("SELECT v FROM progress WHERE k='done'").fetchone()
            con.close()
            if row:
                out.add(year)
        except sqlite3.Error:
            pass
    return out


def status() -> str:
    pid = build_pid()
    done = marker_done_years()
    missing = [y for y in ALL_YEARS if y not in done]
    log_stats = done_years_from_log()
    dbs_size = sum(p.stat().st_size for p in DB_DIR.glob("judgments_*.db"))
    disk_free = shutil.disk_usage("/").free

    print(f"进程: {'运行中 pid=' + str(pid) if pid else '已退出'}")
    print(f"已完成年份: {len(done)}/{len(ALL_YEARS)}，缺失: {missing or '无'}")
    if log_stats:
        last = sorted(log_stats.items())[-1]
        print(f"最近完成: [{last[0]}] {last[1]}")
    tail = LOG.read_text(errors="replace").strip().splitlines()[-3:] if LOG.exists() else []
    for line in tail:
        print(f"  日志| {line}")
    if ERR_LOG.exists() and ERR_LOG.stat().st_size:
        err_tail = ERR_LOG.read_text(errors="replace").strip().splitlines()[-3:]
        for line in err_tail:
            print(f"  错误| {line}")
    print(f"库总体积: {dbs_size / 1e9:.1f} GB | 磁盘可用: {disk_free / 1e9:.0f} GB")

    if not missing:
        verdict = "complete"
    elif pid:
        verdict = "running"
    else:
        verdict = "dead_incomplete"
    print(f"verdict: {verdict}")
    if verdict == "dead_incomplete":
        print("处理建议: 查看 build_full2.err 定位原因后重启 launchd 任务：\n"
              f"  launchctl remove {LABEL}\n"
              f"  launchctl submit -l {LABEL} -o {LOG} -e {ERR_LOG} -- "
              f"{sys.executable} {BASE / 'build_judgment_db.py'}\n"
              "  （-o 会截断旧日志，重启前先备份 build_full2.log）")
    return verdict


def mcp_call(session: str, payload: dict) -> tuple[float, str, str]:
    req = urllib.request.Request(
        MCP_URL, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 **({"mcp-session-id": session} if session else {})})
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read().decode()
        new_session = resp.headers.get("mcp-session-id", session)
    return (time.perf_counter() - t0) * 1000, body, new_session


def accept() -> None:
    print("=" * 60)
    print("全量验收一：/health 总量核对")
    print("=" * 60)
    with urllib.request.urlopen("http://127.0.0.1:8766/health", timeout=60) as r:
        health = json.loads(r.read())
    print(f"服务可见年份: {health['years']} | docs_total: {health['docs_total']:,}")

    marker_sum, rows_sum = 0, 0
    for p in sorted(DB_DIR.glob("judgments_*.db")):
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            done = con.execute("SELECT v FROM progress WHERE k='done'").fetchone()
            marker_sum += int(done[0]) if done else 0
            rows_sum += con.execute("SELECT COALESCE(MAX(id),0) FROM docs").fetchone()[0]
        finally:
            con.close()
    delta = rows_sum - marker_sum
    print(f"库内实际行数合计 (MAX(id)): {rows_sum:,}")
    print(f"done 标记合计: {marker_sum:,} | 差值: {delta:,}"
          + ("（≈2018 旧版空全文计数口径差 476,582，属已知预期）"
             if abs(delta - 476582) < 1000 else "（异常，需排查！）"))

    print("=" * 60)
    print("全量验收二：端到端抽样检索延迟（8766 生产路径）")
    print("=" * 60)
    _, _, session = mcp_call("", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                             "clientInfo": {"name": "accept", "version": "1"}}})
    mcp_call(session, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    for q, extra in [("毛善亮", {}), ("盗窃罪", {}), ("民间借贷 利息", {}),
                     ("合同纠纷", {"limit": 10}), ("盗窃罪", {"court": "中级人民法院", "limit": 5})]:
        args = {"query": q, "limit": extra.pop("limit", 10), **extra}
        ms, body, _ = mcp_call(session, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                         "params": {"name": "search_judgment", "arguments": args}})
        hits = elapsed = "?"
        for line in body.splitlines():
            if line.startswith("data:"):
                try:
                    out = json.loads(json.loads(line[5:])["result"]["content"][0]["text"])
                    hits, elapsed = out["total_hits_shown"], out["elapsed_s"]
                except Exception:
                    pass
        print(f"search({q!r}, {args}) 端到端 {ms:.0f} ms | 服务端 {elapsed}s | 返回 {hits} 条")
    ms, body, _ = mcp_call(session, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                     "params": {"name": "get_judgment",
                                                "arguments": {"db": "2018", "rowid": 500}}})
    print(f"get_judgment(2018/500) 端到端 {ms:.0f} ms | 取回全文: {'全文' in body}")


if __name__ == "__main__":
    if "accept" in sys.argv:
        accept()
    else:
        status()
