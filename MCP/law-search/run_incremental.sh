#!/bin/bash
# run_incremental.sh — 定时增量入库（launchd 调用，每日 03:30）
# 逻辑：NAS 未挂载则跳过 → 已有 ingest 运行则跳过 → 执行增量 ingest（file_hash 去重，仅处理新增/变更文件）
set -u

BASE="/Users/simon/Documents/1-SLP/MCP/law-search"
VENV_PY="/Users/simon/Documents/1-SLP/venv/bin/python"
LOG_DIR="$BASE/logs"
NAS="/Volumes/sata11-138XXXX2122"
TS="$(date '+%F %T')"

mkdir -p "$LOG_DIR"

# 1. NAS 挂载检查：未挂载直接退出（避免空跑）
if [ ! -d "$NAS" ]; then
  echo "[$TS] NAS 未挂载，跳过本次增量入库"
  exit 0
fi

# 2. 并发保护：已有 ingest 进程在跑则跳过
if pgrep -f "python.*ingest\.py" > /dev/null 2>&1; then
  echo "[$TS] 已有 ingest 进程在运行，跳过本次"
  exit 0
fi

# 3. 增量入库（file_hash_index.json 去重，未变更文件秒级跳过）
LOG="$LOG_DIR/ingest_auto_$(date +%Y%m%d_%H%M%S).log"
echo "[$TS] 开始增量入库，日志: $LOG"
cd "$BASE"
"$VENV_PY" ingest.py >> "$LOG" 2>&1
RC=$?
echo "[$(date '+%F %T')] 增量入库完成 exit=$RC"
