#!/usr/bin/env bash
# SLP 法律工作台 - 完整构建脚本
# 用法: ./build.sh [--skip-python] [--skip-frontend] [--sign]
#
# 流程:
#   1. 前端构建 (vite build → dist/)
#   2. Python 服务打包 (PyInstaller → slp-sidecar, slp-workbench)
#   3. 资源收集 (dist + binaries → src-tauri/resources/)
#   4. Tauri 构建 (tauri build → .app + .dmg)
#
# 可选:
#   --sign        使用 Apple Developer 证书签名 + 公证
#   --skip-python 跳过 Python 打包（使用已有二进制）
#   --skip-frontend 跳过前端构建（使用已有 dist）

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKBENCH_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FRONTEND_DIR="$WORKBENCH_DIR/frontend"
SRC_TAURI_DIR="$FRONTEND_DIR/src-tauri"
PROJECT_ROOT="$(cd "$WORKBENCH_DIR/../.." && pwd)"
RESOURCES_DIR="$SRC_TAURI_DIR/resources"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[BUILD]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# 解析参数
SKIP_PYTHON=false
SKIP_FRONTEND=false
SIGN=false
for arg in "$@"; do
  case "$arg" in
    --skip-python) SKIP_PYTHON=true ;;
    --skip-frontend) SKIP_FRONTEND=true ;;
    --sign) SIGN=true ;;
    *) error "未知参数: $arg" ;;
  esac
done

# 检查前置条件
check_prereqs() {
  info "检查前置条件..."
  command -v node >/dev/null 2>&1 || error "node 未安装"
  command -v python3 >/dev/null 2>&1 || error "python3 未安装"

  # venv
  if [[ -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
    VENV="$PROJECT_ROOT/.venv/bin/python"
  elif [[ -x "$PROJECT_ROOT/venv/bin/python" ]]; then
    VENV="$PROJECT_ROOT/venv/bin/python"
  else
    error "未找到 Python venv (.venv 或 venv)"
  fi
  info "  Python: $VENV ($($VENV --version))"
  info "  Node: $(node --version)"
}

# Step 1: 前端构建
build_frontend() {
  if $SKIP_FRONTEND; then
    info "跳过前端构建 (--skip-frontend)"
    return
  fi
  info "构建前端 (vite build)..."
  cd "$FRONTEND_DIR"
  npm run build
  info "  前端构建完成: $FRONTEND_DIR/dist/"
}

# Step 2: Python 服务打包
build_python() {
  if $SKIP_PYTHON; then
    info "跳过 Python 打包 (--skip-python)"
    return
  fi

  info "打包 Python 服务 (PyInstaller)..."
  cd "$PROJECT_ROOT"

  # 确保 pyinstaller 已安装
  if ! "$VENV" -m pyinstaller --version >/dev/null 2>&1; then
    warn "  安装 PyInstaller..."
    "$VENV" -m pip install pyinstaller --quiet
  fi

  # 清理旧构建
  rm -rf "$PROJECT_ROOT/build" "$PROJECT_ROOT/dist" \
    "$PROJECT_ROOT/apps/workbench/scripts/slp_sidecar_entry.spec" \
    "$PROJECT_ROOT/apps/workbench/scripts/slp_workbench_entry.spec"

  # 打包 sidecar
  info "  打包 slp-sidecar..."
  "$VENV" -m pyinstaller \
    --name slp-sidecar \
    --onefile \
    --clean \
    --noconfirm \
    --paths "$PROJECT_ROOT" \
    --hidden-import apps.capability_adapter \
    --hidden-import apps.capability_adapter.app \
    --hidden-import apps.capability_adapter.parser \
    --hidden-import apps.capability_adapter.retrieval \
    --hidden-import apps.capability_adapter.judgments \
    --hidden-import apps.capability_adapter.summarizer \
    --hidden-import apps.capability_adapter.review_generator \
    --hidden-import apps.capability_adapter.litigation_generator \
    --hidden-import fitz \
    --hidden-import docx \
    --collect-all apps.capability_adapter \
    "$WORKBENCH_DIR/scripts/slp_sidecar_entry.py" 2>&1 | tail -5

  # 打包 workbench
  info "  打包 slp-workbench..."
  "$VENV" -m pyinstaller \
    --name slp-workbench \
    --onefile \
    --clean \
    --noconfirm \
    --paths "$PROJECT_ROOT" \
    --hidden-import apps.workbench.backend \
    --hidden-import apps.workbench.backend.app \
    --collect-all apps.workbench.backend \
    "$WORKBENCH_DIR/scripts/slp_workbench_entry.py" 2>&1 | tail -5

  info "  Python 打包完成"
}

# Step 3: 资源收集
collect_resources() {
  info "收集 Tauri 资源..."

  # 清理旧资源
  rm -rf "$RESOURCES_DIR/binaries" "$RESOURCES_DIR/frontend-dist"
  mkdir -p "$RESOURCES_DIR/binaries" "$RESOURCES_DIR/frontend-dist"

  # 复制 Python 二进制
  if [[ -f "$PROJECT_ROOT/dist/slp-sidecar" ]]; then
    cp "$PROJECT_ROOT/dist/slp-sidecar" "$RESOURCES_DIR/binaries/"
    chmod +x "$RESOURCES_DIR/binaries/slp-sidecar"
    info "  slp-sidecar: $(du -h "$RESOURCES_DIR/binaries/slp-sidecar" | cut -f1)"
  else
    warn "  slp-sidecar 二进制不存在（使用 --skip-python 或先运行 build_python）"
  fi

  if [[ -f "$PROJECT_ROOT/dist/slp-workbench" ]]; then
    cp "$PROJECT_ROOT/dist/slp-workbench" "$RESOURCES_DIR/binaries/"
    chmod +x "$RESOURCES_DIR/binaries/slp-workbench"
    info "  slp-workbench: $(du -h "$RESOURCES_DIR/binaries/slp-workbench" | cut -f1)"
  else
    warn "  slp-workbench 二进制不存在"
  fi

  # 复制前端 dist
  if [[ -d "$FRONTEND_DIR/dist" ]]; then
    cp -R "$FRONTEND_DIR/dist/." "$RESOURCES_DIR/frontend-dist/"
    info "  frontend-dist: $(du -sh "$RESOURCES_DIR/frontend-dist" | cut -f1)"
  else
    warn "  frontend/dist 不存在"
  fi
}

# Step 4: Tauri 构建
build_tauri() {
  info "构建 Tauri 应用..."
  cd "$FRONTEND_DIR"

  # 确保 @tauri-apps/cli 已安装
  if [[ ! -f "node_modules/.bin/tauri" ]]; then
    warn "  安装 @tauri-apps/cli..."
    npm install --save-dev @tauri-apps/cli
  fi

  local tauri_args=("build")
  if $SIGN; then
    info "  启用签名模式"
    # 签名需要环境变量:
    #   APPLE_CERTIFICATE / APPLE_CERTIFICATE_PASSWORD
    #   APPLE_SIGNING_IDENTITY
    #   APPLE_ID / APPLE_PASSWORD / APPLE_TEAM_ID (公证)
    tauri_args+=("--config" '{"bundle":{"macOS":{"signingIdentity":"Developer ID Application"}}}')
  fi

  node_modules/.bin/tauri "${tauri_args[@]}"
  info "Tauri 构建完成!"
  info "  输出: $SRC_TAURI_DIR/target/release/bundle/"
}

# 签名 + 公证（macOS）
sign_and_notarize() {
  if ! $SIGN; then
    return
  fi
  info "签名 + 公证..."
  local app_path="$SRC_TAURI_DIR/target/release/bundle/macos/SLP 法律工作台.app"
  local dmg_path="$SRC_TAURI_DIR/target/release/bundle/dmg/SLP 法律工作台_*.dmg"

  if [[ ! -d "$app_path" ]]; then
    warn "  .app 不存在，跳过签名"
    return
  fi

  # 签名
  codesign --force --deep --sign "Developer ID Application: $APPLE_SIGNING_IDENTITY" "$app_path"
  info "  签名完成"

  # 公证
  if [[ -n "${APPLE_ID:-}" ]]; then
    xcrun notarytool submit "$dmg_path" \
      --apple-id "$APPLE_ID" \
      --password "$APPLE_PASSWORD" \
      --team-id "$APPLE_TEAM_ID" \
      --wait
    info "  公证完成"
  else
    warn "  未设置 APPLE_ID，跳过公证"
  fi
}

# 主流程
main() {
  echo ""
  echo "=========================================="
  echo "  SLP 法律工作台 - 构建"
  echo "=========================================="
  echo ""

  check_prereqs
  build_frontend
  build_python
  collect_resources
  build_tauri
  sign_and_notarize

  echo ""
  info "构建全部完成!"
  echo ""
}

main "$@"
