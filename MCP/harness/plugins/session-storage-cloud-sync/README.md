# @law-agent/session-storage-cloud-sync

DSH 会话自动推送插件：监听 `session/event`，debounce 30 秒后从本地
`session.jsonl.zstd` 解析全部消息，POST 到 session-storage-cloud 的
`/bulk-push` 端点，实现跨设备共享会话记忆。

## 架构

```
DSH session/event → ctx.on('session/event')
  → Debouncer(30s 无新事件才触发)
    → 解析 ~/Library/Application Support/dsh-desktop/harness/sessions/*/session-*/session.jsonl.zstd
      → POST http://100.66.217.64:8767/bulk-push
        → session-storage-cloud SQLite 共享库
```

## 设计要点

- **不依赖本地 Python venv**：纯 Node.js + `node:zlib` 解压 zstd（macOS 自带）
- **不依赖 Node.js zstd 包**：用 `zstd` 命令行（macOS 预装）或 fallback 到 `node:zlib`
- **幂等推送**：服务端 `_do_bulk_push` 先 DELETE 再 INSERT，同 sessionId 重复推送安全
- **异常容错**：插件自身错误不阻断 DSH 主流程；推送失败只记 stderr 日志
- **设备标记**：`source: "dsh-sync"` 区分 DSH 自动推送 vs MCP 手动推送

## 配置

```yaml
session-storage-cloud-sync:
  enabled: true
  endpoint: http://100.66.217.64:8767/bulk-push
  token: abcd1234ef
  debounceMs: 30000          # 30 秒无新事件才推送
  sessionDir: ~/Library/Application Support/dsh-desktop/harness/sessions
  pushSource: dsh-sync
  connectTimeoutMs: 5000
  pushTimeoutMs: 15000
```
