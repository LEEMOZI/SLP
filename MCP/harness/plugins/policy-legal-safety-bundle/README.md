# @law-agent/policy-legal-safety-bundle

> **P0 必装 Bundle**：法律业务的审批闸。在 DSH Cordis 的 `tools/pre-execute` waterfall 上注册三层拦截，确保模型不能随意调用高危工具、访问未授权目录、执行危险命令。

## 版本：v0.3.0（P0 补齐完成）

| 组件 | 状态 | 说明 |
|------|------|------|
| `src/safety.js` | ✅ 已实现 | 3层拦截逻辑（455行 CommonJS） |
| `cordis.patch.yml` | ✅ 已声明 | 工具分类 + 授权目录 + 高危关键词配置 |
| `package.json` | ✅ 已更新 | main 入口指向 src/safety.js |

## 三层拦截机制

```
工具调用请求
    ↓
Layer 1: 工具分类
    ├─ readonly → 直接放行（search_law / search_case / chroma_status 等）
    ├─ write → 需用户确认（write_litigation / write_file 等）
    ├─ dangerous → 默认拒绝（ingest_rebuild 需 confirm=true）
    └─ unknown → 按 unknownPolicy 处理（confirm/deny/allow）
    ↓
Layer 2: 路径越权校验
    ├─ 检查 file_path / path 参数是否在授权目录内
    ├─ 授权目录：1-Work / uploads / .dsh-uploads / Downloads
    └─ 越权 → deny + 记录审计日志
    ↓
Layer 3: 高危关键词拦截
    ├─ 检查参数文本是否包含 shell 注入/删库等关键词
    ├─ 关键词列表：rm -rf / drop table / chmod 777 等
    └─ 命中 → deny + 记录审计日志
    ↓
放行到下一个 hook 或执行工具
```

## 配置（cordis.patch.yml）

所有可调阈值都在 `cordis.patch.yml` 的 `config` 段，无需改代码：

```yaml
config:
  # 工具分类表
  toolClassification:
    search_law:        readonly    # 免审批
    write_litigation:  write       # 需用户确认
    ingest_rebuild:    dangerous   # 默认拒绝

  # 授权目录（Layer 2）
  authorizedRoots:
    - /Users/simon/Documents/1-Work
    - /Users/simon/Documents/1-SLP/MCP/law-search/uploads
    - /Users/simon/Documents/1-SLP/DSH WorkSpace/.dsh-uploads
    - /Users/simon/Downloads

  # 高危关键词（Layer 3）
  blockedKeywords:
    - 'rm -rf'
    - 'drop table'
    - 'chmod 777'

  # 未登记工具策略
  unknownPolicy: confirm  # allow / confirm / deny
```

## Profile 级别策略覆盖

不同工作台可以有不同的安全策略：

| Profile | unknownPolicy | 说明 |
|---------|---------------|------|
| `lm-studio-workbench` | `confirm` | 以只读检索为主，未登记工具需确认 |
| `work-project` | `deny` | 涉及正式法律文书，未登记工具默认拒绝 |

## 与 Python 侧的分工

| 层 | 职责 | 实现位置 |
|----|------|----------|
| Python law-search | 路径白名单校验（review_contract / read_document） | server.py + contract_review.py |
| Python law-search | ingest_rebuild 的 confirm 参数 | server.py |
| **DSH 本插件** | **第二道闸**：工具分类 + 路径越权 + 高危词 | src/safety.js |

两层校验互补，不重复：
- Python 侧校验是"业务逻辑层"的安全（与具体工具绑定）
- DSH 插件校验是"编排层"的安全（与工具名/参数绑定，跨工具统一）

## 审计日志

所有工具调用都会写入 stderr（DSH 控制台可见）：

```
[2026-09-02T12:00:00.000Z] [policy-legal-safety] [ALLOW] tool=search_law action=allow
[2026-09-02T12:00:01.000Z] [policy-legal-safety] [DENY] tool=ingest_rebuild action=deny reason=...
[2026-09-02T12:00:02.000Z] [policy-legal-safety] [AUDIT] tool=search_law completed in 1234ms
```

## 为什么把审批做成插件？

审批策略是法律 Agent 的高变更区——不同客户、不同律所、不同阶段有不同合规要求。做成 Cordis 插件的好处是：
- 多家律所 = 多家各自独立的 `policy-<customer>-bundle`，代码完全隔离
- 升级审批策略 = `dsh plugin update @law-agent/policy-legal-safety-bundle`，秒级生效
- 回滚 = `dsh plugin remove` + 重装旧版，不影响 Python 业务

## 开发 & 调试

```bash
# 本插件是纯 JavaScript（CommonJS），无需 TypeScript 编译
cd MCP/harness/plugins/policy-legal-safety-bundle

# 单元测试（内部函数已导出供测试）
node -e "
const safety = require('./src/safety.js');
const { layer1ToolClassification, layer2PathAuthorization } = safety._internal;
console.log(layer1ToolClassification('search_law', {}));
console.log(layer1ToolClassification('ingest_rebuild', {}));
console.log(layer2PathAuthorization('review_contract', { file_path: '/etc/passwd' }, {}));
"

# 安装到 DSH
bash MCP/harness/install_mcp.sh --apply
```
