# @law-agent/observability-bundle

> **P2 Bundle** — 可观测性插件

## 定位

通过 DSH Cordis 的事件钩子收集法律 Agent 的可观测性指标，支持 OTLP 兼容格式导出。

---

## 指标收集器

| 收集器 | 事件 | 指标 | 默认状态 |
|--------|------|------|---------|
| `tool-metrics` | `tools/post-execute` | 调用耗时、成功/失败率 | ✅ 启用 |
| `llm-metrics` | `llm/*/stream-end` | Token 用量、成本、延迟 | ✅ 启用 |
| `retrieval-metrics` | `tools/post-execute` (search_law/case) | 命中率、结果数量 | ✅ 启用 |
| `session-metrics` | `session/event` | 会话持续时间、轮次 | ❌ 禁用 |

---

## 导出目标

| 目标 | 说明 | 配置 |
|------|------|------|
| `console` | 输出到 stderr（调试用） | 默认 |
| `otlp` | 发送到 OTLP 端点 | `otlpEndpoint` |
| `file` | 写入本地 JSONL 文件 | `filePath` |

---

## 成本计算

内置各 Provider 的 Token 单价（美元 / 1K tokens）：

| Provider | 输入 | 输出 |
|----------|------|------|
| `deepseek_api` | $0.14/1M | $0.28/1M |
| `lm_studio_local` | 免费 | 免费 |
| `bionic_secure_cloud` | $3/1M | $6/1M |

---

## 配置（cordis.patch.yml）

```yaml
- insert:
    - id: observability-legal
      config:
        collectors:
          - name: tool-metrics
            enabled: true
          - name: llm-metrics
            enabled: true
          - name: retrieval-metrics
            enabled: true
        export:
          target: console        # console / otlp / file
          intervalSec: 60
          batchSize: 100
        costCalculation:
          pricing:
            deepseek_api:
              input: 0.00014
              output: 0.00028
```

---

## 查询接口

```javascript
// 获取所有指标
const metrics = ctx.consume('observability:getMetrics')();
console.log(metrics);
// {
//   tool: [{ name: 'tool_call_count', labels: { tool: 'search_law' }, value: 42 }, ...],
//   llm: [{ name: 'llm_cost_usd_total', labels: { provider: 'deepseek_api' }, value: 0.0123 }, ...],
//   retrieval: [{ name: 'retrieval_hit_rate', labels: {}, value: 0.78 }, ...],
// }

// 重置指标
ctx.consume('observability:resetMetrics')();
```

---

## 设计原则

1. **不阻塞主流程** — 指标收集异步写入
2. **内存安全** — 环形缓冲区，避免无限增长
3. **异常容错** — 收集失败不影响业务
4. **OTLP 兼容** — 指标格式可直接对接 Prometheus/Jaeger
