# law-search 远程部署指南

## 文件清单

| 文件 | 用途 |
|------|------|
| `deploy_remote.sh` | 一键同步代码 + 依赖 + 数据到远程 Mac Studio |
| `cherry_studio_mcp.json` | Cherry Studio 导入的 MCP 配置模板 |

## 快速开始

```bash
# 1. 检查远程主机连通性
bash deploy/deploy_remote.sh --check <StudioIP>

# 2. 执行部署
bash deploy/deploy_remote.sh <StudioIP> [username]

# 3. 在远程启动服务
ssh <user>@<StudioIP> 'cd /opt/law-search && bash run_cloud.sh --transport http --host 0.0.0.0 --port 8765'

# 4. Cherry Studio 导入 MCP 配置
#    设置 → MCP 服务器 → 导入 JSON → 选择 deploy/cherry_studio_mcp.json
#    替换 <StudioIP> 和 <token>
```

## 远程启动后验证

```bash
# 无 token 应返回 401
curl -s -o /dev/null -w "%{http_code}" http://<StudioIP>:8765/mcp

# 带 token 应进入 MCP 协议层（非 401）
curl -s -H "Authorization: Bearer <token>" http://<StudioIP>:8765/mcp
```

## 路径映射说明

远程部署后，paths.yaml 中的路径需要与远程实际路径对应：

| 端侧路径 | 远程路径（deploy_remote.sh 默认同步到）|
|----------|--------------------------------------|
| `/Users/simon/Desktop/Laws&Rules` | `/data/Laws&Rules` |
| `/Users/simon/Documents/1-Work` | `/data/1-Work` |

如果选择不同步法律文件，paths.yaml 中的 `source` 路径需手动改为远程实际路径。
