# @law-agent/ui-legal-citations

> **P2 Plugin** — 法律 UI 定制插件

## 定位

通过 DSH Cordis 的 UI Plugin API 注册法律专用 UI 组件，在 DSH Web UI 中提供富文本法律内容展示。

**当前状态**：骨架实现，完整功能待 DSH UI Plugin API 稳定后落地。

---

## UI 组件清单

| 组件 | 说明 | 默认状态 |
|------|------|---------|
| `law-article-card` | 法条原文折叠卡片 | ✅ 启用 |
| `evidence-marker` | 证据标记（高亮关键证据点） | ✅ 启用 |
| `citation-jump` | 引用跳转原文页 | ✅ 启用 |
| `evidence-timeline` | 证据链时间轴 | ❌ 禁用（需更多开发） |

---

## 配置（cordis.patch.yml）

```yaml
- insert:
    - id: ui-legal-citations
      config:
        components:
          - name: law-article-card
            enabled: true
            style:
              collapsed: true
              maxHeight: '200px'
              showSource: true
              copyButton: true
          - name: evidence-marker
            enabled: true
            style:
              highlightColor: '#fff3cd'
              icon: '⚖️'
              tooltip: true
        globalStyle:
          fontFamily: 'system-ui'
          fontSize: '14px'
          primaryColor: '#1a73e8'
```

---

## 使用示例

### 生成法条原文折叠卡片

```javascript
const { _internal } = require('./src/ui');
const { generateLawArticleCardHTML } = _internal;

const html = generateLawArticleCardHTML({
  lawName: '民法典',
  articleNumber: '143',
  text: '具备下列条件的民事法律行为有效：\n（一）行为人具有相应的民事行为能力；\n（二）意思表示真实；\n（三）不违反法律、行政法规的强制性规定，不违背公序良俗。',
  source: '中国法律法规信息库',
}, { collapsed: true, copyButton: true });

console.log(html);
```

### 生成证据链时间轴

```javascript
const { generateEvidenceTimelineHTML } = _internal;

const html = generateEvidenceTimelineHTML([
  { date: '2024-01-15', title: '签订合同', description: '双方签订房屋买卖合同' },
  { date: '2024-02-01', title: '支付定金', description: '买方支付定金10万元' },
  { date: '2024-03-10', title: '违约通知', description: '卖方通知无法履行合同' },
], { layout: 'horizontal', showDates: true });

console.log(html);
```

---

## ConversationNodeDefinition

每个 UI 组件都注册为 DSH 的 ConversationNodeDefinition：

```javascript
{
  id: 'legal-law-article-card',
  type: 'law-article-card',
  description: '法条原文折叠卡片',
  trigger: 'response_contains_citation',
  render: (data) => string  // 返回 HTML
}
```

---

## 样式定制

通过 `globalStyle` 配置全局样式，各组件的 `style` 配置会覆盖全局：

| 属性 | 默认值 | 说明 |
|------|--------|------|
| `fontFamily` | `system-ui, -apple-system, sans-serif` | 字体 |
| `fontSize` | `14px` | 字号 |
| `lineHeight` | `1.6` | 行高 |
| `primaryColor` | `#1a73e8` | 主色调 |
| `borderColor` | `#e0e0e0` | 边框色 |

---

## 设计原则

1. **骨架优先** — 先提供注册入口，完整渲染逻辑待 API 稳定后落地
2. **可配置** — 所有组件可通过配置启用/禁用
3. **降级兼容** — DSH 版本不支持 UI Plugin 时静默跳过
4. **辅助函数** — 提供 HTML 生成函数供其他插件调用
