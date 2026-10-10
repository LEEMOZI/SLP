# @law-agent/session-storage-fs

> **[P1 插件]** DSH 会话存储后端 — 桥接 DSH 原生会话管理与 Python session-storage MCP 的 SQLite+FTS5 后端

## 定位

DSH 插件架构 12 类扩展点中的 **第 8 类：Session / Storage**。

通过 `ctx.sessions` 注册 `StorageBackend`（`append()` / `replay()` 接口），将 DSH 的实时会话事件流持久化到与 Python MCP 共享的 SQLite 数据库，实现：

- **实时事件捕获**：DSH 会话过程中的每条消息/工具调用通过 `session/event` 事件自动入库
- **全文检索**：FTS5 trigram tokenizer 支持中文子串匹配（≥3 字符走全文索引，2 字符降级 LIKE）
- **标签归档**：按客户名/案件编号/法律领域打标签，支持按标签过滤检索
- **案卷导出**：导出为 Markdown 文件，可直接贴入工作底稿
- **自动同步**：定时扫描 DSH Desktop 会话文件（session.jsonl.zstd）增量导入

## 架构

```
DSH Cordis
  ├── ctx.sessions.registerBackend('fs', backend)
  │     ├── append(event)   → child_process → Python → SQLite
  │     └── replay(sid)     → child_process → Python → SQLite
  ├── ctx.on('session/event') → backend.append(event)
  └── ctx.provide('session-storage:*')
        ├── search(query, options)  → Python FTS5 检索
        ├── get(sessionId)          → Python 读取完整会话
        ├── tag(sessionId, tags)    → Python 写入标签
        ├── export(sessionId)       → Python 导出 Markdown
        ├── stats()                 → Python 统计信息
        └── sync()                  → Python 增量导入
```

**数据流**：
- DSH 实时事件 → `session/event` → `backend.append()` → Python 脚本 → SQLite
- DSH Desktop 历史文件 → 自动同步 → Python `sync_sessions()` → SQLite
- 检索请求 → `manager.search()` → Python FTS5 → 返回匹配会话列表

**共享数据库**：本插件与 Python session-storage MCP 共享同一个 SQLite 数据库（`MCP/session-storage/data/sessions.db`），通过文件锁保证跨进程互斥。

## 文件结构

```
session-storage-fs/
├── package.json          # npm 包元数据 + dsh.bundle.patch 声明
├── cordis.patch.yml      # DSH 补丁合法格式（顶层 YAML 数组）
├── src/
│   └── storage.js        # 核心实现（465 行 CommonJS）
└── README.md             # 本文档
```

## 配置项

| 配置键 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `dbPath` | string | `MCP/session-storage/data/sessions.db` | SQLite 数据库路径 |
| `dshSessionsDir` | string | `~/Library/.../harness/sessions` | DSH 会话文件目录 |
| `autoSync.enabled` | boolean | `true` | 是否启用自动同步 |
| `autoSync.intervalSec` | number | `300` | 同步间隔（秒） |
| `autoSync.onStartup` | boolean | `true` | 启动时立即同步一次 |
| `export.defaultDir` | string | `~/Desktop/dsh_sessions` | 默认导出目录 |
| `export.format` | string | `markdown` | 导出格式 |
| `search.defaultLimit` | number | `10` | 默认返回条数 |
| `search.minQueryLength` | number | `2` | 最小查询长度 |
| `search.enableFts` | boolean | `true` | 启用 FTS5 全文检索 |
| `search.enableLikeFallback` | boolean | `true` | FTS 无命中时降级 LIKE |
| `logLevel` | string | `warn` | 日志级别（error/warn/info/debug） |

## 依赖

- **运行时**：Python session-storage MCP（`MCP/session-storage/`）
- **Python 解释器**：`/Users/simon/Documents/1-SLP/venv/bin/python`
- **共享数据库**：`MCP/session-storage/data/sessions.db`（SQLite + FTS5）
- **跨进程锁**：Python `filelock` 库（WAL 模式 + busy_timeout）

## 冒烟测试

```bash
# 语法检查
node -c MCP/harness/plugins/session-storage-fs/src/storage.js

# 核心函数测试
node -e "
const { _internal } = require('./MCP/harness/plugins/session-storage-fs/src/storage.js');
const logger = _internal.createLogger('debug');
const backend = _internal.createStorageBackend({ dbPath: '/tmp/test.db' }, logger);
console.log('StorageBackend name:', backend.name);
console.log('append type:', typeof backend.append);
console.log('replay type:', typeof backend.replay);
const manager = _internal.createSessionManager({}, logger);
console.log('search type:', typeof manager.search);
console.log('sync type:', typeof manager.sync);
console.log('ALL OK');
"
```

## 扩展点对应

| DSH 扩展点 | 本插件实现 |
|------------|-----------|
| `ctx.sessions.registerBackend()` | `apply()` 中注册 StorageBackend |
| `ctx.on('session/event')` | 监听实时会话事件 → `backend.append()` |
| `ctx.provide()` | 注册 `session-storage:{search,get,tag,export,stats,sync}` |
| `ctx.on('plugin:dispose')` | 停止自动同步定时器 |

## 版本

0.1.0 — 2026-09-03
