#!/usr/bin/env bash
# ============================================================
# auto_sync_kb.sh — 知识库增量维护自动化
#
# 功能：
#   1. 从 paths.yaml 动态读取所有 enabled 库的 sources（目录 + 散文件），
#      不再硬编码，避免 paths.yaml / 脚本双重维护（S2a fix）
#   2. 检测文件变更（新增 / 修改 / 删除），通过 NEED_INGEST 全局标志传递；
#      无变更时完全跳过 ingest（--dry-run 预览扫描也取消），
#      解决每 6 小时无脑全量重扫问题（S2b fix）
#   3. 仅当 NEED_INGEST=1 时才触发增量 ingest，且只跑 1 次 ingest.py
#   4. 可选：--force 全量重建；--watch 持续监控（cron / launchd）
#
# 用法：
#   bash auto_sync_kb.sh                    # 检测变更；有变更才增量同步
#   bash auto_sync_kb.sh --dry-run          # 仅检测变更，不执行 ingest
#   bash auto_sync_kb.sh --force            # 强制全量重建（忽略状态文件）
#   bash auto_sync_kb.sh --watch            # 持续监控模式（默认 5 分钟间隔）
#   bash auto_sync_kb.sh --interval 1800    # 自定义 --watch 间隔（秒）
#
# 定时任务（launchd / crontab）：
#   # crontab -e  （例：每 6 小时执行一次，写日志到 logs/kb_sync.log）
#   0 */6 * * * cd /Users/simon/Documents/1-SLP/MCP/law-search && bash auto_sync_kb.sh >> logs/kb_sync.log 2>&1
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 全部路径绝对化：无论用户在哪里执行脚本，都稳定落到 1-SLP 项目目录。
# venv 位置：law-search 在 MCP/ 下，向上两级是 1-SLP/ → 1-SLP/venv/bin/python
VENV_PYTHON="${SCRIPT_DIR}/../../venv/bin/python"
PATHS_YAML="${SCRIPT_DIR}/config/paths.yaml"
STATE_FILE="${SCRIPT_DIR}/.kb_sync_state.json"
LOG_FILE="${SCRIPT_DIR}/logs/kb_sync.log"
LOCK_FILE="${SCRIPT_DIR}/.kb_sync.lock"
mkdir -p "${SCRIPT_DIR}/logs"

# P1-3: 并发锁 — 防止 launchd 定时任务或手动执行重复触发
# flock -n: 非阻塞获取锁；获取失败则说明上一次还在跑，直接退出
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  echo "[auto_sync_kb.sh] ⚠️ 另一个实例正在运行（${LOCK_FILE}），跳过本次执行。"
  exit 0
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "[auto_sync_kb.sh] ❌ 找不到 venv Python: $VENV_PYTHON" >&2
  echo "                   cwd=$(pwd)" >&2
  echo "                   确认 /Users/simon/Documents/1-SLP/venv/bin/python 是否存在。" >&2
  exit 1
fi

DRY_RUN=0
FORCE=0
WATCH=0
WATCH_INTERVAL=300  # 5 分钟
# 全局标志：detect_changes 写 1 / 0，主流程和 --watch 决定是否执行 ingest
NEED_INGEST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN=1; shift ;;
    --force)      FORCE=1; shift ;;
    --watch)      WATCH=1; shift ;;
    --interval)   WATCH_INTERVAL="$2"; shift 2 ;;
    -h|--help)
      head -20 "$0" | tail -16
      exit 0
      ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "$msg"
  if [[ -d "logs" ]]; then
    echo "$msg" >> "$LOG_FILE"
  fi
}

# ============================================================
# 从 paths.yaml 抽取所有 enabled 库的 source paths（统一真相源，S2a fix）
# 输出两行：DIRS / FILES，空格分隔（路径含空格由 Python 端 JSON 列表保证后续安全）
# ============================================================
collect_sources() {
  "$VENV_PYTHON" - "$PATHS_YAML" <<'PY'
import json, sys, yaml
from pathlib import Path

yaml_path = Path(sys.argv[1])
cfg = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}

dirs, files = [], []
for lib_name, lib in (cfg.get("libraries") or {}).items():
    if not lib.get("enabled", True):
        continue
    for src in lib.get("sources") or []:
        p = src.get("path")
        if not p:
            continue
        pt = Path(p)
        if pt.is_dir():
            dirs.append(p)
        elif pt.is_file():
            files.append(p)
        # 不存在的路径静默跳过（可能移动/删除）

print(json.dumps({"dirs": dirs, "files": files}, ensure_ascii=False))
PY
}

# ============================================================
# 检测文件变更（把信号写入全局 NEED_INGEST，S2b fix）
# 大对象（含 1 万+ 文件 mtime）绝不走 shell 变量或 argv，
# 全部落盘到 ${STATE_FILE}.analysis 临时文件，Python 之间通过文件交换。
# ============================================================
detect_changes() {
  # --force 模式：强制标为需要 ingest，不做对比
  if [[ $FORCE -eq 1 ]]; then
    log "--force：忽略状态对比，强制进入全量 ingest"
    NEED_INGEST=1
    return
  fi

  # 首次运行（状态文件不存在）→ 需要 ingest
  if [[ ! -f "$STATE_FILE" ]]; then
    log "首次运行：状态文件不存在，需要全量扫描 + ingest"
    NEED_INGEST=1
    return
  fi

  # 把 paths.yaml sources（几百字节的小 JSON）先落盘，避免 argv 传中文路径被截断
  local src_tmp="${STATE_FILE}.sources"
  if ! collect_sources > "$src_tmp"; then
    log "⚠️ 无法从 paths.yaml 提取 sources，跳过本次同步"
    rm -f "$src_tmp"
    NEED_INGEST=0
    return
  fi

  local analysis_file="${STATE_FILE}.pending"

  # 唯一一次全量扫描 Python：产出 added/modified/deleted + _pending_state（current_map）
  # 输出直接写到 analysis_file，不经过 shell 变量（否则 1 万+ 路径会爆 Argument list too long）
  if ! "$VENV_PYTHON" - "$STATE_FILE" "$src_tmp" "$analysis_file" <<'PY'
import json, os, sys
from pathlib import Path

state_file = Path(sys.argv[1])
src_cfg_file = Path(sys.argv[2])
analysis_out = Path(sys.argv[3])

src_cfg = json.loads(src_cfg_file.read_text(encoding="utf-8"))
dirs = src_cfg.get("dirs", [])
files = src_cfg.get("files", [])

state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.exists() else {"files": {}}
old_map = state.get("files", {})
current_map = {}

def add_file(full_path: str) -> None:
    try:
        st = os.stat(full_path)
        current_map[full_path] = int(st.st_mtime)
    except OSError:
        pass

for d in dirs:
    if not os.path.isdir(d):
        continue
    for root, _, names in os.walk(d):
        for n in names:
            if n.lower().endswith((".pdf", ".docx", ".doc", ".txt", ".md",
                                     ".pptx", ".ppt", ".xlsx", ".xls")):
                add_file(os.path.join(root, n))
for f in files:
    if os.path.isfile(f):
        add_file(f)

added, modified = [], []
for path, mtime in current_map.items():
    prev = old_map.get(path)
    if prev is None:
        added.append(path)
    elif int(prev) != int(mtime):
        modified.append(path)
deleted = [p for p in old_map if p not in current_map]

# 把 diff 摘要（短）也和 _pending_state 一起写出去，后续计数/预览都读同一个文件
result = {
    "added": added,
    "modified": modified,
    "deleted": deleted,
    "_pending_state": {"files": current_map},
}
analysis_out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
print(f"SCANNED {len(current_map)} FILES; ADDED={len(added)} MODIFIED={len(modified)} DELETED={len(deleted)}")
PY
  then
    log "⚠️ 文件扫描阶段 Python 异常，跳过本次同步"
    rm -f "$src_tmp"
    NEED_INGEST=0
    return
  fi
  rm -f "$src_tmp"

  # 从 analysis_file 读摘要计数 + 变更预览（短操作，analysis_file 本身可能大，但 Python 只切片了前 5 条字符串）
  local summary
  summary=$(
    "$VENV_PYTHON" - "$analysis_file" <<'PY'
import json, sys
from pathlib import Path
res = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
a, m, d = len(res["added"]), len(res["modified"]), len(res["deleted"])
print(f"COUNTS {a} {m} {d}")
for label, arr in (("新增", res["added"]), ("修改", res["modified"]), ("删除", res["deleted"])):
    for p in arr[:5]:
        print(f"[{label}] {p}")
    if len(arr) > 5:
        print(f"[{label}] … 共 {len(arr)} 条（仅显示前 5）")
PY
  )

  local added_count modified_count deleted_count
  added_count=$(echo "$summary" | awk '/^COUNTS/ {print $2}')
  modified_count=$(echo "$summary" | awk '/^COUNTS/ {print $3}')
  deleted_count=$(echo "$summary" | awk '/^COUNTS/ {print $4}')
  added_count=${added_count:-0}
  modified_count=${modified_count:-0}
  deleted_count=${deleted_count:-0}

  # 日志：变更详情（过滤 COUNTS 行）
  while IFS= read -r sline; do
    [[ "$sline" == COUNTS* ]] && continue
    [[ -z "$sline" ]] && continue
    log "  $sline"
  done <<< "$summary"

  if [[ "$added_count" == "0" && "$modified_count" == "0" && "$deleted_count" == "0" ]]; then
    log "未检测到文件变更（跳过 ingest）"
    rm -f "$analysis_file"  # 没变更则 pending 不需要保留
    NEED_INGEST=0
  else
    log "检测到变更：新增 $added_count，修改 $modified_count，删除 $deleted_count"
    NEED_INGEST=1
  fi
}

# ============================================================
# 更新状态文件
#   - --force：重新从 paths.yaml 扫描生成全新快照
#   - 正常路径：消费 detect_changes 写的 ${STATE_FILE}.pending（已经扫描过一遍）
#   无论哪条路径，ingest 成功后才写回，避免 ingest 挂掉状态却超前推进。
# ============================================================
update_state() {
  log "更新状态文件..."

  if [[ -f "${STATE_FILE}.pending" ]]; then
    # 复用 detect_changes 已产出的 current_map，省一次遍历
    "$VENV_PYTHON" - "$STATE_FILE" "${STATE_FILE}.pending" <<'PY'
import json, sys
from pathlib import Path
state_file = Path(sys.argv[1])
pending_file = Path(sys.argv[2])
pending = json.loads(pending_file.read_text(encoding="utf-8"))
new_state = pending.get("_pending_state") or {"files": {}}
state_file.write_text(json.dumps(new_state, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  已记录 {len(new_state.get('files', {}))} 个文件（复用 pending 快照）")
PY
    rm -f "${STATE_FILE}.pending"
  else
    # force / 异常兜底：重扫一遍
    local raw
    raw=$(collect_sources)
    "$VENV_PYTHON" - "$STATE_FILE" "$raw" <<'PY'
import json, os, sys
from pathlib import Path
state_file = Path(sys.argv[1])
src_cfg = json.loads(sys.argv[2])
dirs, files = src_cfg.get("dirs", []), src_cfg.get("files", [])
current_map = {}
for d in dirs:
    if not os.path.isdir(d): continue
    for root, _, names in os.walk(d):
        for n in names:
            if n.lower().endswith((".pdf",".docx",".doc",".txt",".md",".pptx",".ppt",".xlsx",".xls")):
                full = os.path.join(root, n)
                try:
                    current_map[full] = int(os.stat(full).st_mtime)
                except OSError:
                    pass
for f in files:
    if os.path.isfile(f):
        try:
            current_map[f] = int(os.stat(f).st_mtime)
        except OSError:
            pass
state = {"files": current_map}
state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"  已记录 {len(current_map)} 个文件（重新扫描）")
PY
  fi
}

# ============================================================
# 执行增量 ingest（只跑 1 次；S2b 去掉了 --dry-run 重跑那次）
# ============================================================
run_ingest() {
  log "启动 ingest..."

  if [[ $DRY_RUN -eq 1 ]]; then
    log "(dry-run) 跳过实际 ingest，仅做 ingest.py --dry-run 扫描预览"
    "$VENV_PYTHON" ingest.py --dry-run 2>&1 | while IFS= read -r line; do
      log "  $line"
    done || true
    return
  fi

  if [[ $FORCE -eq 1 ]]; then
    log "--force 模式：执行全量 ingest（会扫描 paths.yaml 全部 source）"
  else
    log "增量模式：ingest.py 内部 hash 去重只处理新/改文件（chroma_lock 进程级写锁保护）"
  fi

  "$VENV_PYTHON" ingest.py 2>&1 | while IFS= read -r line; do
    log "  $line"
  done

  log "ingest 完成"
}

# ============================================================
# 主流程
# ============================================================
main() {
  log "=========================================="
  log "知识库增量维护开始"
  log "=========================================="

  if [[ $WATCH -eq 0 ]]; then
    # ============== 单次运行模式 ==============
    detect_changes
    if [[ $FORCE -eq 1 ]] || [[ $NEED_INGEST -eq 1 ]]; then
      run_ingest
      update_state
    fi
    log "=========================================="
    log "知识库增量维护完成"
    log "=========================================="
    return
  fi

  # ============== 持续监控模式 ==============
  log "进入持续监控模式（间隔 ${WATCH_INTERVAL}s）..."
  # --watch 启动时也跑一轮首检，避免冷启动漏一次
  detect_changes
  if [[ $FORCE -eq 1 ]] || [[ $NEED_INGEST -eq 1 ]]; then
    run_ingest
    update_state
  fi

  while true; do
    sleep "$WATCH_INTERVAL"
    detect_changes
    if [[ $NEED_INGEST -eq 1 ]]; then
      run_ingest
      update_state
    fi
  done
}

main
