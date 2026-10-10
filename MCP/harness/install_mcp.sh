#!/usr/bin/env bash
# ============================================================
# install_mcp.sh — 把本项目 Harness 配置接入本机的 DeepSeek Harness（dsh）
#
# 用法（在真实终端中，本项目 harness/ 目录下执行）：
#     bash install_mcp.sh                 # 默认 Dry-run（不写文件，仅打印操作）
#     bash install_mcp.sh --apply         # 真正写入：symlink registry + profiles/skills
#     bash install_mcp.sh --dry-run       # 同默认行为
#
# 功能：
#   1) 探测 DSH_HOME：默认 ~/.dsh，可用环境变量覆盖
#   2) 校验 dsh 命令存在，否则报错退出
#   3) 在 $DSH_HOME/servers/ 下创建 mcp_registry.json（symlink → 项目内 mcp_registry.json）
#      或当 DSH 不识别 servers/mcp_registry.json 时，按 ~/.dsh/ 官方目录布局选择
#   4) 把 profiles/*.json 和 skills/*.json 拷贝到 $DSH_HOME/{profiles,skills}/
#      （symlink 有插件安全校验问题，默认 copy；--symlink 可切换）
#
# 依赖：真实终端运行（TRAE 沙盒写 ~/.dsh 受限）；dsh 已安装
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"   # /Users/.../1-SLP/MCP
LAW_SEARCH_DIR="$PROJECT_DIR/law-search"
REGISTRY="$SCRIPT_DIR/mcp_registry.json"
PROFILES_DIR="$SCRIPT_DIR/dsh-home/profiles"
SKILLS_DIR="$SCRIPT_DIR/skills"
CONSISTENCY_CHECK="$SCRIPT_DIR/check_config_consistency.py"

# P0 新增：DSH HOME 标准布局模板 + 插件工作区
DSH_HOME_TEMPLATE="$SCRIPT_DIR/dsh-home"
PLUGINS_DIR="$SCRIPT_DIR/plugins"

APPLY=0
USE_SYMLINK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)   APPLY=1; shift ;;
    --dry-run) APPLY=0; shift ;;
    --symlink) USE_SYMLINK=1; shift ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

# ------------------------------------------------------------
# 0) 预校验
# ------------------------------------------------------------
echo "[0/4] 预校验..."
[[ -f "$REGISTRY" ]]          || { echo "❌ registry 不存在: $REGISTRY" >&2; exit 1; }
[[ -d "$PROFILES_DIR" ]]      || { echo "❌ profiles 目录不存在: $PROFILES_DIR" >&2; exit 1; }
[[ -d "$SKILLS_DIR" ]]        || { echo "❌ skills 目录不存在: $SKILLS_DIR" >&2; exit 1; }

if command -v dsh >/dev/null 2>&1; then
  echo "   dsh 已安装：$(command -v dsh) ($(dsh --version 2>/dev/null || echo 'version check n/a'))"
else
  echo "⚠️  未发现 dsh 命令。请先在真实终端执行安装："
  echo "     sudo chown -R \$(id -u):\$(id -g) "\$HOME/.npm""
  echo "     npm install -g @deepseek-ai/dsh"
  echo "   或者：先下载桌面版 DMG（脚本下一行会指引）完成安装。"
fi

if [[ -f "$CONSISTENCY_CHECK" ]]; then
  python3 "$CONSISTENCY_CHECK" || { echo "❌ registry/profiles/skills 交叉引用不一致" >&2; exit 1; }
fi

DSH_HOME="${DSH_HOME:-$HOME/.dsh}"
echo "   DSH_HOME=$DSH_HOME"

MODE="DRY-RUN"
[[ $APPLY -eq 1 ]] && MODE="APPLY"
echo "   模式：$MODE"

# ------------------------------------------------------------
# 1) 路径策略（兼容 DSH 两种常见布局 + P0 新增：dsh-home 模板完整铺设 + P0 插件 link）
# ------------------------------------------------------------
echo "[1/6] 确定 DSH 配置路径策略..."
# DSH 官方默认布局：
#   ~/.dsh/
#     cordis.patch.yml             （home 全局补丁）
#     servers/<name>.json          （单个 server 定义文件）
#     mcp/registry.json            （在某些版本里使用此路径）
#     profiles/<profile-name>/     （每个 profile 是目录，内含 package.json + cordis.patch.yml）
#     skills/*.json
# 本项目同时：
#   · 按 dsh-home/ 模板整体铺 cordis.patch.yml + profiles 标准目录
#   · 把 mcp_registry.json 同时铺设到 servers/ 和 mcp/（兼容两种 loader）
SERVERS_DIR="$DSH_HOME/servers"
MCP_DIR="$DSH_HOME/mcp"
DSPROFILES_DIR="$DSH_HOME/profiles"
DSSKILLS_DIR="$DSH_HOME/skills"

mkdirs=("$DSH_HOME" "$SERVERS_DIR" "$MCP_DIR" "$DSPROFILES_DIR" "$DSSKILLS_DIR")
if [[ $APPLY -eq 1 ]]; then
  mkdir -p "${mkdirs[@]}"
  echo "   已确保目录存在"
else
  echo "   将在 apply 时创建: ${mkdirs[*]}"
fi

run() {
  # $1=描述 $2..=实际命令
  local desc="$1"; shift
  echo "   · $desc"
  if [[ $APPLY -eq 1 ]]; then
    "$@"
  else
    echo "     (dry-run) 执行： $*"
  fi
}

link_or_copy() {
  # $1=源 $2=目标
  local src="$1" dst="$2"
  if [[ $USE_SYMLINK -eq 1 ]]; then
    rm -f "$dst"
    ln -sf "$src" "$dst"
  else
    cp -f "$src" "$dst"
  fi
}

# ------------------------------------------------------------
# 2) 铺设 DSH HOME 模板（cordis.patch.yml 全局补丁 + 标准 profile 目录）
#    —— 这是 P0 最关键的一步：把项目内 dsh-home/ 的标准布局整体镜像到 $DSH_HOME
# ------------------------------------------------------------
echo "[2/6] 铺设 DSH HOME 模板（cordis.patch.yml + 标准 profiles 目录）..."
[[ -d "$DSH_HOME_TEMPLATE" ]] || { echo "❌ dsh-home 模板不存在: $DSH_HOME_TEMPLATE" >&2; exit 1; }

# 全局补丁：直接复制（若 DSH 用户侧已有 cordis.patch.yml，我们采用 merge 策略：
# 把项目补丁改名为 cordis.patch.00-law-agent.yml 叠加进去，避免覆盖用户的手工设置。
GLOBAL_PATCH_TARGET="$DSH_HOME/cordis.patch.yml"
GLOBAL_PATCH_LAWAGENT_TARGET="$DSH_HOME/cordis.patch.00-law-agent.yml"
if [[ -f "$GLOBAL_PATCH_TARGET" ]]; then
  run "全局补丁已存在 → 追加 law-agent 叠加补丁 cordis.patch.00-law-agent.yml" \
    link_or_copy "$DSH_HOME_TEMPLATE/cordis.patch.yml" "$GLOBAL_PATCH_LAWAGENT_TARGET"
else
  run "全局补丁 cordis.patch.yml ← dsh-home/cordis.patch.yml" \
    link_or_copy "$DSH_HOME_TEMPLATE/cordis.patch.yml" "$GLOBAL_PATCH_TARGET"
fi

# 标准 profiles 目录：逐 profile 复制 package.json + cordis.patch.yml 到 ~/.dsh/profiles/<name>/
for profile_dir in "$DSH_HOME_TEMPLATE"/profiles/*/; do
  [[ -d "$profile_dir" ]] || continue
  profile_name="$(basename "$profile_dir")"
  target_profile_dir="$DSPROFILES_DIR/$profile_name"
  run "Profile [$profile_name]: 确保目录 $target_profile_dir" true
  if [[ $APPLY -eq 1 ]]; then mkdir -p "$target_profile_dir"; fi
  for f in package.json cordis.patch.yml; do
    src="$profile_dir/$f"
    [[ -f "$src" ]] || continue
    run "  Profile [$profile_name]/$f" \
      link_or_copy "$src" "$target_profile_dir/$f"
  done
done

# ------------------------------------------------------------
# 3) registry 接入：mcp_registry.json 两种路径 + 单 server 拆解
# ------------------------------------------------------------
echo "[3/6] registry 接入..."
run "mcp/registry.json ← 项目 mcp_registry.json" \
  link_or_copy "$REGISTRY" "$MCP_DIR/registry.json"
run "servers/mcp_registry.json ← 项目 mcp_registry.json" \
  link_or_copy "$REGISTRY" "$SERVERS_DIR/mcp_registry.json"

# 拆解每一个 mcpServers：生成 <server-name>.json（DSH 某些版本按单文件 server 加载）
python3 - "$REGISTRY" "$SERVERS_DIR" "$LAW_SEARCH_DIR" $APPLY $USE_SYMLINK <<'PY'
import json, os, sys, shutil
registry_path, servers_dir, law_search_dir, apply, use_symlink = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5])
with open(registry_path, "r", encoding="utf-8") as f:
    data = json.load(f)
for name, srv in data.get("mcpServers", {}).items():
    # 写入时保留顶层 schema 字段 + server 条目本身，形成合法的单 server 声明
    payload = {
        "$schema": data.get("$schema", ""),
        "version": "1.0",
        "mcpServers": {name: srv}
    }
    dst = os.path.join(servers_dir, f"{name}.json")
    if apply:
        os.makedirs(servers_dir, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as w:
            json.dump(payload, w, ensure_ascii=False, indent=2)
        print(f"   · 写入单 server: {name}.json")
    else:
        print(f"   · (dry-run) 写入单 server: {name}.json")
PY

# ------------------------------------------------------------
# 4) profiles + skills 接入（历史兼容：profiles/*.json 静态卡片保留供用户参考）
# ------------------------------------------------------------
echo "[4/6] profiles / skills 卡片（历史兼容静态 JSON）..."
for pf in "$PROFILES_DIR"/*.json; do
  [[ -e "$pf" ]] || continue
  run "profile $(basename "$pf")" \
    link_or_copy "$pf" "$DSPROFILES_DIR/$(basename "$pf")"
done
for sk in "$SKILLS_DIR"/*.json; do
  [[ -e "$sk" ]] || continue
  run "skill $(basename "$sk")" \
    link_or_copy "$sk" "$DSSKILLS_DIR/$(basename "$sk")"
done

# ------------------------------------------------------------
# 5) P0 插件：符号链接到 $DSH_HOME/profiles/node_modules（DSH 官方 fallback 目录）
#    依据 packages/boot/app-boot/src/profile.ts：bundle 解析走 Node 父级查找，
#    该目录只增不删（healProfilesModuleFallback 不清理外来链接），scoped 布局
#    node_modules/@law-agent/<pkg> 对 require.resolve 与 Loader 均可见。
#    （旧方案 npm link 装到 ~/.npm-global，DSH 解析不到，已废弃。）
# ------------------------------------------------------------
echo "[5/6] P0/P1 插件符号链接 → $DSH_HOME/profiles/node_modules/@law-agent/ ..."
P0_BUNDLES=(
  "law-search-dsh-bundle"
  "llm-router-bundle"
  "policy-legal-safety-bundle"
)
P1_BUNDLES=(
  "skill-provider-legal"
  "subagent-legal-bundle"
  "session-storage-fs"
)
P2_BUNDLES=(
  "agent-loop-legal"
  "ui-legal-citations"
  "observability-bundle"
)
ALL_BUNDLES=("${P0_BUNDLES[@]}" "${P1_BUNDLES[@]}" "${P2_BUNDLES[@]}")
FALLBACK_NM="$DSH_HOME/profiles/node_modules/@law-agent"
for b in "${ALL_BUNDLES[@]}"; do
  bundle_dir="$PLUGINS_DIR/$b"
  [[ -d "$bundle_dir" ]] || { echo "❌ 插件目录不存在: $bundle_dir" >&2; exit 1; }
  run "link @law-agent/$b → profiles/node_modules" \
    bash -c "mkdir -p '$FALLBACK_NM' && ln -sfn '$bundle_dir' '$FALLBACK_NM/$b'"
done

# ------------------------------------------------------------
# 6) 报告 + 后续步骤
# ------------------------------------------------------------
echo "[6/6] 完成"
echo "   DSH 配置目录: $DSH_HOME"
echo
echo "后续请在真实终端操作："
echo "  A) 启动 DSH Desktop（已安装于 /Applications/AI/DSH Desktop.app）："
echo "     选择 lm-studio-workbench profile 即可"
echo "  B) 首次登录后在 DSH → Providers 中填入："
echo "     · DeepSeek API Key（env: DEEPSEEK_API_KEY）"
echo "     · 如用 LM Studio：保持 http://127.0.0.1:1234/v1，勾选 LM Studio 本地运行"
echo "  C) 在 DSH → MCP Servers 面板确认 law-search（stdio）/ filesystem / law-search-cloud（SSE）全部为绿色"
echo "  D) 如想装更多能力包（Ollama / session-storage / UI timeline / subagent 等）："
echo "     dsh plugin add <包名>     # 社区搜索 dsh- 前缀"
echo "  E) 如有问题，运行一致性检查：  python3 $CONSISTENCY_CHECK"
echo
if [[ $APPLY -eq 0 ]]; then
  echo "⚠️  当前是 DRY-RUN 模式，未真正写入文件。如确认无误，重跑："
  echo "      bash $0 --apply"
fi
