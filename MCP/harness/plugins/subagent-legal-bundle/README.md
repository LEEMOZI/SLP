# @law-agent/subagent-legal-bundle

> **P1 Bundle** — 法律子代理并行执行插件

## 定位

通过 DSH Cordis 的 `ctx.subagents.register()` 接口注册 3 个专用子代理，实现 fork-in-process 并行执行。每个子代理有独立的系统提示词、工具白名单和模型偏好。

**改造前**：所有法律任务（检索+审查+起草）由单 Python server.py 串行处理。

**改造后**：3 个子代理可并行执行，各自有独立上下文和工具权限，互不干扰。

---

## 子代理清单

| 子代理 | 名称 | 工具白名单 | 模型偏好 | 并发 | 超时 |
|--------|------|-----------|----------|------|------|
| `legal-retrieval` | 法律检索代理 | search_law, search_case, search_external, chroma_status | lm_studio_local → deepseek_api | 3 | 120s |
| `contract-review` | 合同审查代理 | review_contract, evaluate_evidence, read_document, search_law | deepseek_api → lm_studio_local | 2 | 300s |
| `litigation-drafting` | 文书起草代理 | write_litigation, read_document, search_law | deepseek_api → lm_studio_local | 1 | 600s |

---

## 并行策略

| 策略 | 说明 | 适用场景 |
|------|------|---------|
| `eager` | 尽可能并行（所有子代理同时启动） | 独立任务（如：检索+审查同时进行） |
| `pipeline` | 流水线（检索 → 审查 → 起草，串行依赖） | 有依赖关系的任务链 |
| `manual` | 手动控制（由调用方决定并行策略） | 复杂编排场景 |

---

## 配置（cordis.patch.yml）

```yaml
- insert:
    - id: subagent-legal
      name: '@law-agent/subagent-legal-bundle'
      config:
        agents:
          - name: legal-retrieval
            displayName: '法律检索代理'
            systemPrompt: |
              你是一个专业的法律检索代理...
            allowedTools:
              - search_law
              - search_case
            deniedTools:
              - write_litigation
            modelHint:
              preferred: lm_studio_local
              fallback: deepseek_api
            maxConcurrency: 3
            timeoutSec: 120
          # ... 更多子代理
        
        parallelStrategy: eager   # eager / pipeline / manual
        logLevel: warn
```

---

## 使用示例

### 通过 DSH 主代理调用

```javascript
// 主代理可以委托任务给子代理
const result = await ctx.subagents.execute('legal', 'legal-retrieval', {
  task: '检索关于合同违约的最新司法解释',
  args: { question: '合同违约赔偿标准', top_k: 8 }
});
```

### 并行执行多个子代理

```javascript
// 通过 ctx.provide 注册的辅助函数
const executeParallel = ctx.consume('subagent-legal:executeParallel');

const results = await executeParallel(
  [
    { agentName: 'legal-retrieval', task: '检索合同法相关法条' },
    { agentName: 'contract-review', task: '审查合同文件', args: { file_path: '...' } },
  ],
  async (agentConfig, task) => {
    // 实际的执行逻辑
    return await ctx.subagents.execute('legal', agentConfig.name, task);
  }
);
```

---

## 与 policy-legal-safety-bundle 的协作

子代理的工具白名单与 policy-legal-safety-bundle 的 Layer 1 工具分类叠加校验：

```
子代理 allowedTools: [search_law, review_contract]
     ↓
policy-legal-safety Layer 1: search_law=readonly, review_contract=readonly
     ↓
最终允许: search_law, review_contract（均为 readonly，免审批）
```

如果子代理尝试调用 `write_litigation`（write 类），即使不在 deniedTools 中，也会被 policy-legal-safety 拦截并要求用户确认。

---

## 设计原则

1. **无状态共享** — 子代理之间无共享状态，每个子代理独立上下文
2. **工具权限最小化** — 每个子代理只拥有完成任务所需的最小工具集
3. **异常隔离** — 单个子代理失败不影响其他子代理
4. **降级兼容** — DSH 版本不支持 Sub-agent API 时降级为日志输出
5. **审计日志** — 写入 stderr，不污染 MCP stdio 协议

---

## 单元测试

```javascript
const { _internal } = require('./src/subagents');
const { createSubagentProvider, validateAgentDef, createLogger } = _internal;

const logger = createLogger('debug');

// 测试子代理定义校验
const result = validateAgentDef({
  name: 'test-agent',
  systemPrompt: 'You are a test agent',
  allowedTools: ['search_law'],
  modelHint: { preferred: 'deepseek_api' },
});
console.assert(result.valid === true);

// 测试 Provider
const provider = createSubagentProvider(_internal.DEFAULT_AGENTS, logger);
provider.list().then(agents => console.log(agents));
```
