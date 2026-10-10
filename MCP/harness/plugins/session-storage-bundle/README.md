# @law-agent/session-storage-bundle

P1 会话持久化层（2026-09-02 实现）。

## 架构（重要）

Cordis bundle 只能做配置注入（不能跑代码），本子系统因此实现为**独立 Python MCP Server**：

```
MCP/session-storage/
├── server.py     FastMCP 服务（stdio，6 个工具）
├── importer.py   DSH 会话增量导入（session.jsonl.zstd 解析 + 投影缓存元数据）
├── db.py         SQLite + FTS5 trigram 中文全文检索 + OS 级跨进程写锁
└── data/         sessions.db（本地库，纯本地不联网——符合律师保密义务）
```

数据源：`~/Library/Application Support/dsh-desktop/harness/sessions/` 下各工作区的
`session.jsonl.zstd`（完整对话，含 user/assistant 消息与 tool-call 块）+ 
`storages/session_projcache/`（标题/token/模型等元数据）。

## 6 个工具

| 工具 | 用途 |
|---|---|
| `sync_sessions_tool` | 增量同步 DSH 本地会话 → SQLite（mtime 去重，重复调用无害） |
| `search_sessions` | 全文检索历史咨询（标题+对话内容；≥3 字走 FTS5，2 字自动降级模糊匹配） |
| `get_session` | 调阅完整对话（可选含工具调用链——当时的检索证据） |
| `tag_session` | 打标签归档（客户名/案件编号/法律领域）+ 摘要 |
| `export_session` | 导出 Markdown（默认 `~/Desktop/dsh_sessions/`，可贴工作底稿） |
| `session_stats` | 库概况（总量/时间范围/模型分布/已归档数） |

## 注册

- **DSH Desktop**：已注册于 `harness/profiles/web/cordis.patch.yml` 的 insert 列表
  （`mcp-session-storage` 条目）——patch 层 insert 是 Desktop 唯一的 MCP spawn 通道
- **LM Studio**：MCP → Add Server → 手动添加：

```json
{
  "mcpServers": {
    "session-storage": {
      "command": "/Users/simon/Documents/1-SLP/venv/bin/python",
      "args": ["-u", "/Users/simon/Documents/1-SLP/MCP/session-storage/server.py"]
    }
  }
}
```

## 工作流（律师场景）

1. 咨询结束 → 对 Agent 说「归档本次咨询」→ Agent 调 `sync_sessions_tool` + `tag_session`
2. 新会话问「之前关于 X 的咨询」→ `search_sessions` → `get_session`（含当时引用的法条）
3. 需要存档 → `export_session` 导出 Markdown 贴入工作底稿

依赖：`zstandard`（已装入 1-SLP/venv）。
