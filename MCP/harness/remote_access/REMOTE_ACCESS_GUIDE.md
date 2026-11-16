# 远程访问配置指南

> MAC Studio（算力主机）部署 SLP + LM Studio，MacBook（移动办公）通过 WorkBuddy / LM Studio Bionic / DSH Desktop / Cherry Studio 远程连接使用本地大模型算力。

## 架构

```
MacBook（客户端）                          MAC Studio（算力主机）
 ┌──────────────────────────┐              ┌──────────────────────────────┐
│ WorkBuddy                │  HTTP/SSE    │ LM Studio :1234 / 代理 :1235 │
│ LM Studio Bionic         │ ───────────→ │ law-search-cloud :8765 (SSE) │
│ DSH Desktop              │              │ ChromaDB (37.7 万 chunks)    │
│ Cherry Studio            │              │ DeepSeek API (fallback)      │
└──────────────────────────┘              └──────────────────────────────┘
```

## 一、主机端部署（MAC Studio）

### 1. 开启 SSH 远程登录
- 系统设置 → 通用 → 共享 → 远程登录 → 打开
- 允许访问：选「所有用户」或指定用户

### 2. 确认主机 IP
```bash
# 局域网 IP
ipconfig getifaddr en0

# 如安装了 Tailscale（推荐跨互联网访问）
tailscale ip -4
```

### 3. LM Studio 开启远程访问
1. 打开 LM Studio → 加载模型（如 `qwen/qwen3.6-35b-a3b`）
2. 左侧 **Developer** → **Local Server**
3. 确认端口为 `1234`
4. **Settings → Network → Allow LAN access** 开启（允许局域网访问）
   - 或保持仅 localhost，通过 SSH 隧道访问（更安全）

### 4. 启动 law-search-cloud（SSE 远程服务）
```bash
cd /Users/simon/Documents/1-SLP

# 一键部署（含 launchd 自启动）
bash MCP/harness/remote_access/setup_host.sh

# 或手动启动（监听 0.0.0.0:8765，Bearer Token 鉴权）
bash MCP/law-search/run_cloud.sh --host 0.0.0.0
```

部署完成后会打印鉴权 token，保存在：
```
/Users/simon/Documents/1-SLP/MCP/law-search/.secrets/cloud_auth_token
```

### 5. 模型自动换模代理（端口 1235）

LM Studio 的内存护栏不会自动换出已驻留模型：A3B 常驻时直接请求 27B 会返回
HTTP 400（insufficient system resources）。SLP 内部的 `quality` 只覆盖 MCP
工具调用，客户端直接聊天（Cherry / DSH / WorkBuddy）不经过 SLP。

因此 Studio 上常驻一个转发代理，客户端 base URL 指向 **1235**：

```
客户端 → :1235 (lmstudio_proxy) → 判断 model → 必要时 unload/load → :1234 (LM Studio)
```

- 服务：`com.lawsearch.lmstudio-proxy`（launchd，RunAtLoad + KeepAlive）
- 日志：`~/Library/Logs/lmstudio-proxy.log`
- 启动：`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.lawsearch.lmstudio-proxy.plist`
- 换模约 10s，会打断同时进行的另一个模型的流式请求

## 二、客户端连接方案

### 方案 A：直接网络访问（局域网/Tailscale，推荐）

适用于两台机器在同一局域网，或已通过 Tailscale 组网。

**Tailscale 组网已就绪（2026-10-10）**，三台设备同账号（leemozi@）在线：

| 设备 | Tailscale IP |
|------|-------------|
| **MAC Studio**（本机） | `100.66.217.64` |
| MacBook Pro | `100.110.118.113` |
| iPhone | `100.84.151.14` |

**MacBook/iPhone 客户端最终连接地址**（无论在家还是在外都通用）：

| 服务 | 地址 | 说明 |
|------|------|------|
| LM Studio API | `http://100.66.217.64:1235/v1` | 本地大模型推理 |
| law-search-cloud | `http://100.66.217.64:8765/mcp` | 法律检索 MCP（需 Bearer Token） |

> 已实测：Tailscale IP 下 LM Studio 200 ✅ / law-search-cloud 401 鉴权正常 ✅

### 方案 B：SSH 隧道（加密，跨互联网推荐）

适用于不安全网络环境，所有流量经 SSH 加密。

```bash
# MacBook 上执行（替换为 MAC Studio IP）
bash /Users/simon/Documents/1-SLP/MCP/harness/remote_access/connect_remote.sh <StudioIP>
```

隧道建立后，客户端配置使用 `localhost`：
- LM Studio: `http://localhost:1235/v1`
- law-search-cloud: `http://localhost:8765/mcp`

## 三、各客户端配置

### 1. DSH Desktop

**LLM Provider 配置**（指向 MAC Studio LM Studio）：

打开 DSH Desktop → Settings → Models → Add Provider：

| 字段 | 值 |
|------|-----|
| Provider ID | `lm-studio-remote` |
| Display name | `LM Studio (MAC Studio)` |
| Base URL | `http://<StudioIP>:1235/v1`（方案 A）<br>或 `http://localhost:1235/v1`（方案 B） |
| API protocol | `openai-completions` |
| API Key | `lm-studio` |
| Models | `qwen/qwen3.6-35b-a3b` |

**MCP Server 配置**（连接 law-search-cloud）：

DSH Desktop → Settings → MCP → Add Server：

```json
{
  "mcpServers": {
    "law-search-cloud": {
      "transport": "streamable-http",
      "url": "http://<StudioIP>:8765/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

### 2. Cherry Studio

**LLM 配置**：

设置 → 模型提供商 → 添加 → OpenAI 兼容：

| 字段 | 值 |
|------|-----|
| 名称 | `LM Studio (MAC Studio)` |
| API Key | `lm-studio` |
| 接口地址 | `http://<StudioIP>:1235/v1` |
| 模型 | `qwen/qwen3.6-35b-a3b` |

**MCP 配置**：

设置 → MCP 服务器 → 添加服务器 → SSE：

| 字段 | 值 |
|------|-----|
| 名称 | `law-search` |
| 类型 | `streamableHttp` |
| URL | `http://<StudioIP>:8765/mcp` |
| Headers | `Authorization: Bearer <token>` |

### 3. LM Studio Bionic

Bionic 支持配置远程 LM Studio 作为推理后端：

**Bionic Settings → AI Providers → Add OpenAI-compatible**：

| 字段 | 值 |
|------|-----|
| Name | `LM Studio Remote` |
| Base URL | `http://<StudioIP>:1235/v1` |
| API Key | `lm-studio` |
| Model | `qwen/qwen3.6-35b-a3b` |

**MCP Server**：Bionic 的 MCP 配置指向 law-search-cloud SSE 端点（同 DSH 配置）。

### 4. WorkBuddy

WorkBuddy 通常支持 OpenAI 兼容接口和 MCP 协议：

**模型配置**：
- Base URL: `http://<StudioIP>:1235/v1`
- API Key: `lm-studio`
- Model: `qwen/qwen3.6-35b-a3b`

**MCP 配置**：添加 streamableHttp 类型 MCP Server，URL 为 `http://<StudioIP>:8765/mcp`，Header 带 `Authorization: Bearer <token>`。

## 四、Token 获取

```bash
# 在 MAC Studio 上执行
cat /Users/simon/Documents/1-SLP/MCP/law-search/.secrets/cloud_auth_token
```

## 五、连通性验证

在 MacBook 上测试：

```bash
# 测试 LM Studio 可达
curl http://<StudioIP>:1235/v1/models

# 测试 law-search-cloud 鉴权（无 token 应返回 401）
curl -s -o /dev/null -w "%{http_code}" http://<StudioIP>:8765/mcp

# 测试 law-search-cloud 鉴权（带 token 应返回 200/流式响应）
curl -H "Authorization: Bearer <token>" http://<StudioIP>:8765/mcp
```

## 六、安全建议

1. **局域网环境**：law-search-cloud 监听 0.0.0.0 + Bearer Token 已足够安全
2. **跨互联网**：强烈建议使用 Tailscale 组网，避免直接暴露公网
3. **LM Studio**：默认无 API key 验证，建议通过 SSH 隧道访问，或限制仅局域网访问
4. **Token 轮换**：定期重新生成 `cloud_auth_token` 并更新各客户端配置
