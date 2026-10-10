/**
 * @law-agent/ui-legal-citations — 法律 UI 定制插件
 *
 * 通过 DSH Cordis 的 UI Plugin API 注册法律专用 UI 组件：
 *   1. 法条原文折叠卡片（Collapsible Law Article Cards）
 *      - 在回答中嵌入可折叠的法条原文卡片
 *      - 点击展开查看完整法条内容
 *      - 支持复制法条原文
 *
 *   2. 证据标记（Evidence Markers）
 *      - 在回答中用特殊标记高亮关键证据点
 *      - 悬停显示证据详情
 *
 *   3. 引用跳转原文页（Citation Jump-to-Original）
 *      - 点击法条引用可跳转到原文页
 *      - 支持悬停预览
 *
 *   4. 证据链时间轴（Evidence Chain Timeline）
 *      - 按时间顺序展示相关事件
 *      - 支持水平/垂直布局
 *
 * 设计原则：
 *   - 当前为骨架实现，完整功能待 DSH UI Plugin API 稳定后落地
 *   - 提供 ConversationNodeDefinition 注册入口
 *   - 所有组件可通过 cordis.patch.yml 配置启用/禁用
 *   - 降级兼容：DSH 版本不支持 UI Plugin 时静默跳过
 *
 * @see MCP/HARNESS_PLUGIN_ARCHITECTURE.md — 扩展点 #9 UI Plugin
 */

'use strict';

// ============================================================================
// 默认配置
// ============================================================================

const DEFAULT_LOG_LEVEL = 'warn';

/**
 * 默认组件配置
 */
const DEFAULT_COMPONENTS = [
  {
    name: 'law-article-card',
    enabled: true,
    description: '法条原文折叠卡片',
    trigger: 'response_contains_citation',
    style: {
      collapsed: true,
      maxHeight: '200px',
      showSource: true,
      copyButton: true,
    },
  },
  {
    name: 'evidence-marker',
    enabled: true,
    description: '证据标记',
    trigger: 'always',
    style: {
      highlightColor: '#fff3cd',
      icon: '⚖️',
      tooltip: true,
    },
  },
  {
    name: 'citation-jump',
    enabled: true,
    description: '引用跳转原文页',
    trigger: 'response_contains_citation',
    style: {
      jumpIcon: '📖',
      openInNewTab: false,
      previewOnHover: true,
    },
  },
  {
    name: 'evidence-timeline',
    enabled: false,
    description: '证据链时间轴',
    trigger: 'case_analysis',
    style: {
      layout: 'horizontal',
      showDates: true,
      collapsible: true,
    },
  },
];

/**
 * 默认全局样式
 */
const DEFAULT_GLOBAL_STYLE = {
  fontFamily: 'system-ui, -apple-system, sans-serif',
  fontSize: '14px',
  lineHeight: '1.6',
  primaryColor: '#1a73e8',
  borderColor: '#e0e0e0',
};

// ============================================================================
// 日志工具
// ============================================================================

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };

function createLogger(level) {
  const threshold = LOG_LEVELS[level] !== undefined ? LOG_LEVELS[level] : LOG_LEVELS.warn;
  return {
    error: (msg) => { if (threshold >= 0) process.stderr.write(`[ui-legal-citations] [ERROR] ${msg}\n`); },
    warn:  (msg) => { if (threshold >= 1) process.stderr.write(`[ui-legal-citations] [WARN]  ${msg}\n`); },
    info:  (msg) => { if (threshold >= 2) process.stderr.write(`[ui-legal-citations] [INFO]  ${msg}\n`); },
    debug: (msg) => { if (threshold >= 3) process.stderr.write(`[ui-legal-citations] [DEBUG] ${msg}\n`); },
  };
}

// ============================================================================
// ConversationNodeDefinition 生成
// ============================================================================

/**
 * 生成法条原文折叠卡片的 HTML
 *
 * @param {object} citation - 法条引用信息
 * @param {object} style - 样式配置
 * @returns {string} HTML 字符串
 */
function generateLawArticleCardHTML(citation, style = {}) {
  const {
    collapsed = true,
    maxHeight = '200px',
    showSource = true,
    copyButton = true,
  } = style;

  const collapsedClass = collapsed ? 'collapsed' : 'expanded';
  
  return `
<div class="law-article-card ${collapsedClass}" data-law="${citation.lawName}" data-article="${citation.articleNumber}">
  <div class="card-header" onclick="this.parentElement.classList.toggle('collapsed')">
    <span class="card-title">《${citation.lawName}》第${citation.articleNumber}条</span>
    <span class="card-toggle">${collapsed ? '▶' : '▼'}</span>
  </div>
  <div class="card-content" style="max-height: ${maxHeight}; overflow-y: auto;">
    <pre class="article-text">${citation.text || '法条原文加载中...'}</pre>
    ${showSource ? `<div class="card-source">来源: ${citation.source || '未知'}</div>` : ''}
    ${copyButton ? `<button class="copy-btn" onclick="navigator.clipboard.writeText(this.parentElement.querySelector('.article-text').textContent)">复制</button>` : ''}
  </div>
</div>
<style>
  .law-article-card {
    border: 1px solid var(--border-color, #e0e0e0);
    border-radius: 8px;
    margin: 8px 0;
    font-family: var(--font-family, system-ui);
  }
  .law-article-card .card-header {
    padding: 8px 12px;
    background: #f5f5f5;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }
  .law-article-card .card-title {
    font-weight: 500;
    color: var(--primary-color, #1a73e8);
  }
  .law-article-card.collapsed .card-content {
    display: none;
  }
  .law-article-card .article-text {
    margin: 0;
    padding: 12px;
    white-space: pre-wrap;
    font-size: var(--font-size, 14px);
    line-height: var(--line-height, 1.6);
  }
  .law-article-card .card-source {
    padding: 4px 12px;
    font-size: 12px;
    color: #666;
    border-top: 1px solid var(--border-color, #e0e0e0);
  }
  .law-article-card .copy-btn {
    position: absolute;
    top: 8px;
    right: 8px;
    padding: 4px 8px;
    font-size: 12px;
    cursor: pointer;
  }
</style>
`.trim();
}

/**
 * 生成证据标记的 HTML
 *
 * @param {string} text - 需要标记的文本
 * @param {object} evidence - 证据信息
 * @param {object} style - 样式配置
 * @returns {string} HTML 字符串
 */
function generateEvidenceMarkerHTML(text, evidence, style = {}) {
  const {
    highlightColor = '#fff3cd',
    icon = '⚖️',
    tooltip = true,
  } = style;

  const tooltipAttr = tooltip && evidence.detail
    ? `title="${evidence.detail.replace(/"/g, '&quot;')}"`
    : '';

  return `<span class="evidence-marker" style="background-color: ${highlightColor};" ${tooltipAttr}>${icon} ${text}</span>`;
}

/**
 * 生成引用跳转的 HTML
 *
 * @param {object} citation - 法条引用信息
 * @param {object} style - 样式配置
 * @returns {string} HTML 字符串
 */
function generateCitationJumpHTML(citation, style = {}) {
  const {
    jumpIcon = '📖',
    openInNewTab = false,
    previewOnHover = true,
  } = style;

  const target = openInNewTab ? '_blank' : '_self';
  const hoverAttr = previewOnHover
    ? `onmouseover="showCitationPreview(this, '${citation.text || ''}')" onmouseout="hideCitationPreview()"`
    : '';

  return `<a class="citation-jump" href="${citation.url || '#'}" target="${target}" ${hoverAttr}>${jumpIcon} 《${citation.lawName}》第${citation.articleNumber}条</a>`;
}

/**
 * 生成证据链时间轴的 HTML
 *
 * @param {Array} events - 事件列表 [{ date, title, description }]
 * @param {object} style - 样式配置
 * @returns {string} HTML 字符串
 */
function generateEvidenceTimelineHTML(events, style = {}) {
  const {
    layout = 'horizontal',
    showDates = true,
    collapsible = true,
  } = style;

  if (!Array.isArray(events) || events.length === 0) {
    return '<div class="evidence-timeline empty">暂无证据事件</div>';
  }

  const timelineItems = events.map(event => `
    <div class="timeline-item">
      ${showDates ? `<div class="timeline-date">${event.date || '未知日期'}</div>` : ''}
      <div class="timeline-content">
        <div class="timeline-title">${event.title || ''}</div>
        <div class="timeline-description">${event.description || ''}</div>
      </div>
    </div>
  `).join('');

  return `
<div class="evidence-timeline ${layout} ${collapsible ? 'collapsible' : ''}">
  <div class="timeline-header" ${collapsible ? 'onclick="this.parentElement.classList.toggle(\'collapsed\')"' : ''}>
    <span>证据链时间轴 (${events.length} 个事件)</span>
    ${collapsible ? '<span class="toggle">▼</span>' : ''}
  </div>
  <div class="timeline-body">
    ${timelineItems}
  </div>
</div>
<style>
  .evidence-timeline {
    border: 1px solid var(--border-color, #e0e0e0);
    border-radius: 8px;
    margin: 12px 0;
    font-family: var(--font-family, system-ui);
  }
  .evidence-timeline .timeline-header {
    padding: 8px 12px;
    background: #f5f5f5;
    cursor: pointer;
    display: flex;
    justify-content: space-between;
  }
  .evidence-timeline.collapsed .timeline-body {
    display: none;
  }
  .evidence-timeline.horizontal .timeline-body {
    display: flex;
    overflow-x: auto;
    padding: 12px;
  }
  .evidence-timeline .timeline-item {
    min-width: 150px;
    padding: 8px;
    border-left: 2px solid var(--primary-color, #1a73e8);
    margin-left: 8px;
  }
  .evidence-timeline .timeline-date {
    font-size: 12px;
    color: #666;
  }
  .evidence-timeline .timeline-title {
    font-weight: 500;
    margin: 4px 0;
  }
  .evidence-timeline .timeline-description {
    font-size: 13px;
    color: #444;
  }
</style>
`.trim();
}

// ============================================================================
// ConversationNodeDefinition 注册
// ============================================================================

/**
 * 创建 ConversationNodeDefinition
 * 用于在 DSH Web UI 中渲染法律专用 UI 组件
 */
function createConversationNodeDefinitions(components, globalStyle) {
  const definitions = [];

  for (const component of components) {
    if (!component.enabled) continue;

    const definition = {
      id: `legal-${component.name}`,
      type: component.name,
      description: component.description,
      trigger: component.trigger,
      style: { ...globalStyle, ...component.style },

      /**
       * 渲染函数
       * @param {object} data - 节点数据
       * @returns {string} HTML 字符串
       */
      render(data) {
        switch (component.name) {
          case 'law-article-card':
            return generateLawArticleCardHTML(data.citation || {}, this.style);
          case 'evidence-marker':
            return generateEvidenceMarkerHTML(data.text || '', data.evidence || {}, this.style);
          case 'citation-jump':
            return generateCitationJumpHTML(data.citation || {}, this.style);
          case 'evidence-timeline':
            return generateEvidenceTimelineHTML(data.events || [], this.style);
          default:
            return `<div class="legal-ui-unknown">未知组件: ${component.name}</div>`;
        }
      },
    };

    definitions.push(definition);
  }

  return definitions;
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

const name = 'ui-legal-citations';
const inject = ['ui'];

/**
 * 插件 apply 函数 — 注册法律 UI 组件
 *
 * @param {object} ctx — Cordis Context
 */
async function apply(ctx) {
  const pluginConfig = (ctx.config && ctx.config['ui-legal-citations']) || {};
  const logLevel = pluginConfig.logLevel || DEFAULT_LOG_LEVEL;
  const logger = createLogger(logLevel);

  const components = pluginConfig.components || DEFAULT_COMPONENTS;
  const globalStyle = pluginConfig.globalStyle || DEFAULT_GLOBAL_STYLE;

  logger.info(`插件初始化: ${components.filter(c => c.enabled).length}/${components.length} 个组件启用`);

  // 创建 ConversationNodeDefinitions
  const definitions = createConversationNodeDefinitions(components, globalStyle);
  logger.info(`创建 ${definitions.length} 个 ConversationNodeDefinition`);

  // 注册到 Cordis
  if (ctx.ui && typeof ctx.ui.registerNodeDefinition === 'function') {
    for (const def of definitions) {
      ctx.ui.registerNodeDefinition(def);
      logger.info(`注册 UI 组件: ${def.id} (${def.type})`);
    }
  } else {
    logger.warn('ctx.ui.registerNodeDefinition 不可用 — 当前 DSH 版本可能不支持 UI Plugin API');
    logger.warn('降级模式：UI 组件定义已创建但无法注册到 DSH 运行时');
    
    // 降级方案：输出组件列表供调试参考
    for (const def of definitions) {
      logger.info(`  [降级] UI 组件可用: ${def.id} — ${def.description}`);
    }
  }

  // 注册辅助函数供其他插件使用
  if (ctx.provide && typeof ctx.provide === 'function') {
    ctx.provide('ui-legal:generateLawCard', generateLawArticleCardHTML);
    ctx.provide('ui-legal:generateEvidenceMarker', generateEvidenceMarkerHTML);
    ctx.provide('ui-legal:generateCitationJump', generateCitationJumpHTML);
    ctx.provide('ui-legal:generateTimeline', generateEvidenceTimelineHTML);
    logger.info('辅助函数已注册: generateLawCard, generateEvidenceMarker, generateCitationJump, generateTimeline');
  }

  logger.info('插件加载完成');
}

// ============================================================================
// 导出（CommonJS，兼容 Cordis loader）
// ============================================================================

module.exports = {
  name,
  inject,
  apply,
  _internal: {
    createLogger,
    createConversationNodeDefinitions,
    generateLawArticleCardHTML,
    generateEvidenceMarkerHTML,
    generateCitationJumpHTML,
    generateEvidenceTimelineHTML,
    DEFAULT_COMPONENTS,
    DEFAULT_GLOBAL_STYLE,
    DEFAULT_LOG_LEVEL,
  },
};
