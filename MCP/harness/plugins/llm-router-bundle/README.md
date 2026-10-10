# @law-agent/llm-router-bundle

> **P0 Bundle**：向 DSH Cordis 声明本项目的 LLM Provider 配置元数据，让 DSH Web UI 的 Models 面板能展示可用的 Provider 列表和路由规则。

## 版本：v0.3.0（P0 补齐完成）

| 组件 | 状态 | 说明 |
|------|------|------|
| `cordis.patch.yml` | ✅ 已声明 | 4个Provider配置（DeepSeek + LM Studio + 2个预留） |
| `package.json` | ✅ 已完成 | bundle 声明正确 |

## 设计决策（P0）

模型 Provider 的**实际路由**由 DSH 原生机制管理：
- `settings.yaml`（Web Models 页可视化编辑）
- in-box LLM adapter 行（`dsh-llm-deepseek` / `dsh-llm-openai-compatible`）

Python 侧 `core/llm_client.py` 的 4-Provider 路由继续服务 MCP server 内部 pipeline（search_law 生成等），与 DSH 会话层互不干扰。

本 bundle 的 `cordis.patch.yml` 向 DSH Cordis 声明 Provider 配置元数据，让 DSH Web UI 能展示可用的 Provider 列表。

## 声明的 Provider

| Provider | 模型 | 状态 | 用途 |
|----------|------|------|------|
| `deepseek_api` | deepseek-v4-flash/pro | ✅ 活跃 | 长任务 + 内部生成 |
| `lm_studio_local` | qwen/qwen3.8-27b | ✅ 活跃 | 短任务（<8000字符） |
| `bionic_secure_cloud` | frontier-model-auto | ⏳ 预留 | 需配置 BIONIC_SC_API_KEY |
| `lm_link` | remote-device-default | ⏳ 预留 | 需配置 LM_LINK_API_KEY |

## 路由规则（与 Python 侧对齐）

| 条件 | Provider 选择 |
|------|---------------|
| prompt < 8000 chars | lm_studio_local → deepseek_api |
| prompt >= 8000 chars | deepseek_api → lm_studio_local |
| 内部生成（search_law 等） | deepseek_api（固定云端，避免本地 JIT ~3min） |

## 配置来源

唯一真相源：`MCP/harness/model_router.json`（该文件是 `config/llm_config.py` 的镜像——任何运行值改动请先改 Python，再同步 model_router.json）。

## P1 计划

实现自定义 Cordis 插件，通过 `ctx.llm.registerAdapter()` 注册 per-task 路由适配器（按 prompt 长度 + 任务类型自动选 Provider）。

## 开发 & 调试

```bash
# 本 bundle 是纯配置层（cordis.patch.yml），无需 JS 代码
cd MCP/harness/plugins/llm-router-bundle

# 安装到 DSH
bash MCP/harness/install_mcp.sh --apply

# 在 DSH Web UI 的 Models 面板查看 Provider 列表
```
