# @law-agent/agent-loop-legal

> **P2 Plugin** — 法律专用 Agent Loop 插件

## 定位

通过 DSH Cordis 的 `ctx.agent.loop` 接口替换默认思考-行动循环，在标准 `plan → tool → re-plan → done` 流程中插入法律专用步骤。

**当前状态**：Python `answer_guard.py` 已实现核心逻辑，本插件在 DSH 层提供编排入口，为未来迁移做准备。

---

## 功能

| 步骤 | 说明 |
|------|------|
| 法条引用核对 | 从回答中提取法条引用（如"《民法典》第143条"），与检索结果比对，标记不一致的引用 |
| 证据覆盖率检查 | 分析检索到的关键证据点，检查回答是否覆盖，计算覆盖率百分比 |

---

## Loop 策略预设

| 预设 | 法条核对 | 覆盖率检查 | 阈值 | 最大轮次 | 适用场景 |
|------|---------|-----------|------|---------|---------|
| `standard` | ✅ | ✅ | 60% | 1 | 标准法律检索（默认） |
| `thorough` | ✅ | ✅ | 80% | 3 | 深度审查（多轮覆盖率检查） |
| `quick` | ✅ | ❌ | — | 1 | 快速回答（跳过覆盖率检查） |

---

## 配置（cordis.patch.yml）

```yaml
- insert:
    - id: agent-loop-legal
      config:
        extraSteps:
          - name: citation-verification
            enabled: true
            onFailure: warn      # warn / block
            maxRetries: 2
          - name: evidence-coverage
            enabled: true
            onFailure: warn
            coverageThreshold: 0.6
            maxRetries: 1
        preset: standard         # standard / thorough / quick
        logLevel: warn
```

---

## 法条引用正则

支持匹配以下格式：
- `《民法典》第143条`
- `《刑法》第二百六十四条`
- `《合同法》第60条第1款`
- `《公司法》第16条之2`

---

## 单元测试

```javascript
const { _internal } = require('./src/loop');
const { extractCitations, verifyCitations, checkEvidenceCoverage } = _internal;

// 测试法条引用提取
const citations = extractCitations('根据《民法典》第143条和第234条的规定...');
console.log(citations);
// [{ lawName: '民法典', articleNumber: '143', ... }, { lawName: '民法典', articleNumber: '234', ... }]

// 测试覆盖率检查
const result = checkEvidenceCoverage(
  '合同有效需要满足以下要件：当事人具有民事行为能力...',
  ['当事人具有民事行为能力', '意思表示真实', '不违反法律强制性规定'],
  0.6,
  console
);
console.log(`覆盖率: ${(result.coverage * 100).toFixed(1)}%`);
```

---

## 与 Python answer_guard 的关系

| 层 | 职责 |
|---|---|
| Python `answer_guard.py` | 核心检查逻辑（语义覆盖率、诚实拒绝、引用校验），在 MCP server 内部执行 |
| DSH `agent-loop-legal` | 编排层，在 DSH Agent Loop 中插入额外检查步骤，为未来迁移做准备 |

当前两者并存，不冲突。未来可逐步将 Python 侧逻辑迁移到 DSH 插件。

---

## 设计原则

1. **不破坏现有链路** — Python 侧 answer_guard 继续工作
2. **异常容错** — 插件自身错误不阻断主流程
3. **可配置** — 通过预设和配置项灵活控制
4. **降级兼容** — DSH 版本不支持自定义 Loop 时降级为日志输出
