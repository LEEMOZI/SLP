#!/usr/bin/env bash
# ============================================================
# deploy_remote.sh — law-search 远程 Mac Studio 一键部署脚本
#
# 用法：
#   bash deploy_remote.sh <StudioIP> [username]     # 同步代码+数据到远程
#   bash deploy_remote.sh --check <StudioIP>        # 只检查远程连通性
#
# 前提条件（远程 Mac Studio）：
#   1. 已安装 Homebrew + Python 3.12+
#   2. 已安装 LM Studio 并开启 headless server
#   3. 本机与远程主机在同一内网（或已配 Tailscale/ZeroTier）
#
# 部署目标目录：远程 /opt/law-search/
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAW_SEARCH_DIR="$(dirname "${SCRIPT_DIR}")"   # MCP/law-search/
PROJECT_ROOT="$(dirname "$(dirname "${LAW_SEARCH_DIR}")")"  # 1-SLP/

REMOTE_IP="${1:-}"
REMOTE_USER="${2:-$(whoami)}"
REMOTE_DIR="/opt/law-search"

if [[ -z "${REMOTE_IP}" ]]; then
  echo "用法: bash deploy_remote.sh <StudioIP> [username]"
  echo "     bash deploy_remote.sh --check <StudioIP>"
  exit 1
fi

if [[ "${REMOTE_IP}" == "--check" ]]; then
  REMOTE_IP="${2:-}"
  if [[ -z "${REMOTE_IP}" ]]; then
    echo "用法: bash deploy_remote.sh --check <StudioIP>"
    exit 1
  fi
  echo "=== 检查远程主机 ${REMOTE_IP} ==="
  ping -c 1 -W 2 "${REMOTE_IP}" && echo "✅ 网络连通" || echo "❌ 网络不通"
  ssh -o ConnectTimeout=3 "${REMOTE_USER}@${REMOTE_IP}" "uname -a" 2>/dev/null && echo "✅ SSH 可用" || echo "❌ SSH 不可用"
  ssh -o ConnectTimeout=3 "${REMOTE_USER}@${REMOTE_IP}" "python3 --version" 2>/dev/null && echo "✅ Python 可用" || echo "❌ Python 未安装"
  ssh -o ConnectTimeout=3 "${REMOTE_USER}@${REMOTE_IP}" "ls /Applications/LM\ Studio.app 2>/dev/null || ls ~/Applications/LM\ Studio.app 2>/dev/null" && echo "✅ LM Studio 已安装" || echo "❌ LM Studio 未安装"
  exit 0
fi

echo "============================================"
echo " law-search 远程部署"
echo "============================================"
echo "  远程地址:  ${REMOTE_USER}@${REMOTE_IP}"
echo "  目标目录:  ${REMOTE_DIR}"
echo "  源目录:    ${LAW_SEARCH_DIR}"
echo "============================================"
echo

# 确认
read -p "确认部署到 ${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}？(y/N) " confirm
[[ "${confirm}" != "y" && "${confirm}" != "Y" ]] && { echo "已取消"; exit 0; }

# ------------------------------------------------------------
# Step 1: 创建远程目录
# ------------------------------------------------------------
echo "[Step 1] 创建远程目录..."
ssh "${REMOTE_USER}@${REMOTE_IP}" "sudo mkdir -p ${REMOTE_DIR} && sudo chown ${REMOTE_USER} ${REMOTE_DIR}"
echo "✅ 远程目录就绪"

# ------------------------------------------------------------
# Step 2: 同步 law-search 代码
# ------------------------------------------------------------
echo "[Step 2] 同步 law-search 代码（含 chroma 数据）..."
rsync -avz --progress \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude '.kb_sync_state.json' \
  --exclude 'file_hash_index.json' \
  --exclude 'logs/' \
  --exclude '.lawsearch_chroma.lock' \
  "${LAW_SEARCH_DIR}/" \
  "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}/"
echo "✅ 代码同步完成"

# ------------------------------------------------------------
# Step 3: 同步 venv（重建）
# ------------------------------------------------------------
echo "[Step 3] 在远程创建 venv 并安装依赖..."
ssh "${REMOTE_USER}@${REMOTE_IP}" bash -c "'
  cd ${REMOTE_DIR}
  if [[ ! -d venv ]]; then
    python3 -m venv venv
    echo \"venv 已创建\"
  fi
  venv/bin/pip install --upgrade pip
  venv/bin/pip install -r requirements.txt
  echo \"依赖安装完成\"
'"
echo "✅ Python 环境就绪"

# ------------------------------------------------------------
# Step 4: 同步密钥文件（手动确认）
# ------------------------------------------------------------
echo "[Step 4] 同步密钥文件..."
echo "⚠️  密钥文件（.secrets/）包含 API Key，需手动确认是否同步"
read -p "是否同步 .secrets/ 目录？(y/N) " sync_secrets
if [[ "${sync_secrets}" == "y" || "${sync_secrets}" == "Y" ]]; then
  rsync -avz --progress \
    "${LAW_SEARCH_DIR}/.secrets/" \
    "${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}/.secrets/"
  ssh "${REMOTE_USER}@${REMOTE_IP}" "chmod 600 ${REMOTE_DIR}/.secrets/*"
  echo "✅ 密钥已同步（权限 600）"
else
  echo "⏭️  跳过密钥同步，请在远程手动配置 .secrets/"
fi

# ------------------------------------------------------------
# Step 5: 同步法律文件目录（可选，体积大）
# ------------------------------------------------------------
echo "[Step 5] 同步法律源文件目录..."
echo "⚠️  法律文件目录体积可能很大（数十 GB），确认后同步"
read -p "是否同步法律文件目录？(y/N) " sync_law_files
if [[ "${sync_law_files}" == "y" || "${sync_law_files}" == "Y" ]]; then
  echo "同步 /Users/simon/Desktop/Laws&Rules/ ..."
  rsync -avz --progress \
    "/Users/simon/Desktop/Laws&Rules/" \
    "${REMOTE_USER}@${REMOTE_IP}:/data/Laws&Rules/"
  echo "同步 /Users/simon/Documents/1-Work/ ..."
  rsync -avz --progress \
    --exclude '.dsh-uploads' \
    --exclude '.DS_Store' \
    "/Users/simon/Documents/1-Work/" \
    "${REMOTE_USER}@${REMOTE_IP}:/data/1-Work/"
  echo "✅ 法律文件同步完成"
else
  echo "⏭️  跳过法律文件同步（需后续手动 rsync 或 NFS 挂载）"
fi

# ------------------------------------------------------------
# Step 6: 生成远程启动脚本
# ------------------------------------------------------------
echo "[Step 6] 生成远程启动配置..."
REMOTE_TOKEN=$(openssl rand -hex 32)
ssh "${REMOTE_USER}@${REMOTE_IP}" bash -c "'
  mkdir -p ${REMOTE_DIR}/.secrets
  echo \"${REMOTE_TOKEN}\" > ${REMOTE_DIR}/.secrets/cloud_auth_token
  chmod 600 ${REMOTE_DIR}/.secrets/cloud_auth_token
'"
echo "✅ 远程 Bearer Token 已生成并写入 ${REMOTE_DIR}/.secrets/cloud_auth_token"

# ------------------------------------------------------------
# 完成
# ------------------------------------------------------------
echo
echo "============================================"
echo " 部署完成"
echo "============================================"
echo "  远程目录: ${REMOTE_USER}@${REMOTE_IP}:${REMOTE_DIR}"
echo "  启动命令: ssh ${REMOTE_USER}@${REMOTE_IP} 'cd ${REMOTE_DIR} && bash run_cloud.sh --transport http --host 0.0.0.0 --port 8765 --token ${REMOTE_TOKEN}'"
echo "  Cherry Studio MCP URL: http://${REMOTE_IP}:8765/mcp"
echo "  Bearer Token: ${REMOTE_TOKEN}"
echo
echo "  下一步:"
echo "  1. 在远程启动 LM Studio 并加载模型"
echo "  2. 设置环境变量 LM_STUDIO_BASE_URL=http://${REMOTE_IP}:1234/v1"
echo "  3. 启动 law-search MCP（上面命令）"
echo "  4. 在 Cherry Studio 导入 deploy/cherry_studio_mcp.json"
echo "============================================"
