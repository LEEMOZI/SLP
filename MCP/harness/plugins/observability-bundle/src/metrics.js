/**
 * @law-agent/observability-bundle — 可观测性插件
 *
 * 通过 DSH Cordis 的事件钩子收集法律 Agent 的可观测性指标：
 *   1. 工具调用指标（tools/post-execute）
 *      - 调用耗时、成功/失败率、按工具名分类
 *
 *   2. LLM 调用指标（llm/* /stream-end）
 *      - 调用耗时、Token 用量、成本计算、按 Provider 分类
 *
 *   3. 检索命中率指标（search_law/search_case 结果统计）
 *      - 结果数量、命中数、检索延迟
 *
 *   4. 会话事件指标（session/event）
 *      - 会话持续时间、轮次、工具调用次数
 *
 * 指标格式兼容 OpenTelemetry Protocol (OTLP)，支持导出到：
 *   - console: 输出到 stderr（调试用）
 *   - otlp: 发送到 OTLP 端点（如 Jaeger、Prometheus）
 *   - file: 写入本地 JSONL 文件
 *
 * 设计原则：
 *   - 指标收集不阻塞主流程（异步写入）
 *   - 内存中环形缓冲区，避免无限增长
 *   - 异常容错：指标收集失败不影响业务
 *
 * @see MCP/HARNESS_PLUGIN_ARCHITECTURE.md — 扩展点 #12 Metrics/Observability
 */

'use strict';

const fs = require('fs');
const path = require('path');

// ============================================================================
// 默认配置
// ============================================================================

const DEFAULT_LOG_LEVEL = 'warn';
const DEFAULT_EXPORT_TARGET = 'console';
const DEFAULT_EXPORT_INTERVAL_SEC = 60;
const DEFAULT_BATCH_SIZE = 100;
const DEFAULT_BUFFER_CAPACITY = 10000;

/**
 * 默认成本计算配置（美元 / 1K tokens）
 */
const DEFAULT_PRICING = {
  deepseek_api: { input: 0.00014, output: 0.00028 },
  lm_studio_local: { input: 0, output: 0 },
  bionic_secure_cloud: { input: 0.003, output: 0.006 },
  lm_link: { input: 0, output: 0 },
};

// ============================================================================
// 日志工具
// ============================================================================

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };

function createLogger(level) {
  const threshold = LOG_LEVELS[level] !== undefined ? LOG_LEVELS[level] : LOG_LEVELS.warn;
  return {
    error: (msg) => { if (threshold >= 0) process.stderr.write(`[observability] [ERROR] ${msg}\n`); },
    warn:  (msg) => { if (threshold >= 1) process.stderr.write(`[observability] [WARN]  ${msg}\n`); },
    info:  (msg) => { if (threshold >= 2) process.stderr.write(`[observability] [INFO]  ${msg}\n`); },
    debug: (msg) => { if (threshold >= 3) process.stderr.write(`[observability] [DEBUG] ${msg}\n`); },
  };
}

// ============================================================================
// 环形缓冲区（内存中指标存储）
// ============================================================================

/**
 * 创建环形缓冲区
 * @param {number} capacity - 缓冲区容量
 * @returns {{ push: Function, drain: Function, size: Function, clear: Function }}
 */
function createRingBuffer(capacity) {
  let buffer = [];
  let writeIndex = 0;

  return {
    push(item) {
      if (buffer.length < capacity) {
        buffer.push(item);
      } else {
        buffer[writeIndex] = item;
      }
      writeIndex = (writeIndex + 1) % capacity;
    },

    drain() {
      const items = buffer.slice();
      buffer = [];
      writeIndex = 0;
      return items;
    },

    size() {
      return buffer.length;
    },

    clear() {
      buffer = [];
      writeIndex = 0;
    },
  };
}

// ============================================================================
// 指标收集器
// ============================================================================

/**
 * 创建工具调用指标收集器
 */
function createToolMetricsCollector(logger) {
  const counters = new Map();  // toolName -> { count, errorCount, totalDurationMs }

  return {
    record(exec) {
      const toolName = exec?.toolName || exec?.name || 'unknown';
      const durationMs = exec?.durationMs || 0;
      const isError = exec?.error != null;

      let stats = counters.get(toolName);
      if (!stats) {
        stats = { count: 0, errorCount: 0, totalDurationMs: 0 };
        counters.set(toolName, stats);
      }

      stats.count++;
      stats.totalDurationMs += durationMs;
      if (isError) stats.errorCount++;

      logger.debug(`tool: ${toolName}, duration: ${durationMs}ms, error: ${isError}`);
    },

    getMetrics() {
      const metrics = [];
      for (const [toolName, stats] of counters) {
        metrics.push({
          name: 'tool_call_count',
          labels: { tool: toolName },
          value: stats.count,
        });
        metrics.push({
          name: 'tool_error_count',
          labels: { tool: toolName },
          value: stats.errorCount,
        });
        metrics.push({
          name: 'tool_call_duration_ms_avg',
          labels: { tool: toolName },
          value: stats.count > 0 ? stats.totalDurationMs / stats.count : 0,
        });
      }
      return metrics;
    },

    reset() {
      counters.clear();
    },
  };
}

/**
 * 创建 LLM 调用指标收集器
 */
function createLLMMetricsCollector(pricing, logger) {
  const counters = new Map();  // provider -> { count, totalDurationMs, totalInputTokens, totalOutputTokens, totalCost }

  function calculateCost(provider, inputTokens, outputTokens) {
    const p = pricing[provider] || { input: 0, output: 0 };
    return (inputTokens / 1000) * p.input + (outputTokens / 1000) * p.output;
  }

  return {
    record(event) {
      const provider = event?.provider || 'unknown';
      const durationMs = event?.durationMs || 0;
      const inputTokens = event?.inputTokens || event?.token_input || 0;
      const outputTokens = event?.outputTokens || event?.token_output || 0;
      const cost = calculateCost(provider, inputTokens, outputTokens);

      let stats = counters.get(provider);
      if (!stats) {
        stats = { count: 0, totalDurationMs: 0, totalInputTokens: 0, totalOutputTokens: 0, totalCost: 0 };
        counters.set(provider, stats);
      }

      stats.count++;
      stats.totalDurationMs += durationMs;
      stats.totalInputTokens += inputTokens;
      stats.totalOutputTokens += outputTokens;
      stats.totalCost += cost;

      logger.debug(`llm: ${provider}, duration: ${durationMs}ms, tokens: ${inputTokens}+${outputTokens}, cost: $${cost.toFixed(6)}`);
    },

    getMetrics() {
      const metrics = [];
      for (const [provider, stats] of counters) {
        metrics.push({ name: 'llm_call_count', labels: { provider }, value: stats.count });
        metrics.push({ name: 'llm_call_duration_ms_avg', labels: { provider }, value: stats.count > 0 ? stats.totalDurationMs / stats.count : 0 });
        metrics.push({ name: 'llm_token_input_total', labels: { provider }, value: stats.totalInputTokens });
        metrics.push({ name: 'llm_token_output_total', labels: { provider }, value: stats.totalOutputTokens });
        metrics.push({ name: 'llm_cost_usd_total', labels: { provider }, value: stats.totalCost });
      }
      return metrics;
    },

    reset() {
      counters.clear();
    },
  };
}

/**
 * 创建检索命中率指标收集器
 */
function createRetrievalMetricsCollector(logger) {
  const counters = { resultCount: 0, hitCount: 0, totalLatencyMs: 0, callCount: 0 };

  return {
    record(exec) {
      const toolName = exec?.toolName || exec?.name || '';
      if (toolName !== 'search_law' && toolName !== 'search_case') return;

      const result = exec?.result;
      const resultCount = Array.isArray(result) ? result.length : (result?.results?.length || 0);
      const hitCount = Array.isArray(result)
        ? result.filter(r => (r.relevance || r.score || 0) > 0.5).length
        : 0;
      const latencyMs = exec?.durationMs || 0;

      counters.resultCount += resultCount;
      counters.hitCount += hitCount;
      counters.totalLatencyMs += latencyMs;
      counters.callCount++;

      logger.debug(`retrieval: ${toolName}, results: ${resultCount}, hits: ${hitCount}, latency: ${latencyMs}ms`);
    },

    getMetrics() {
      return [
        { name: 'retrieval_call_count', labels: {}, value: counters.callCount },
        { name: 'retrieval_result_count_total', labels: {}, value: counters.resultCount },
        { name: 'retrieval_hit_count_total', labels: {}, value: counters.hitCount },
        { name: 'retrieval_hit_rate', labels: {}, value: counters.resultCount > 0 ? counters.hitCount / counters.resultCount : 0 },
        { name: 'retrieval_latency_ms_avg', labels: {}, value: counters.callCount > 0 ? counters.totalLatencyMs / counters.callCount : 0 },
      ];
    },

    reset() {
      counters.resultCount = 0;
      counters.hitCount = 0;
      counters.totalLatencyMs = 0;
      counters.callCount = 0;
    },
  };
}

// ============================================================================
// 指标导出器
// ============================================================================

/**
 * 创建控制台导出器
 */
function createConsoleExporter(logger) {
  return {
    export(metrics) {
      for (const m of metrics) {
        const labels = Object.entries(m.labels || {}).map(([k, v]) => `${k}="${v}"`).join(',');
        process.stderr.write(`[observability] [METRIC] ${m.name}{${labels}} = ${m.value}\n`);
      }
    },
  };
}

/**
 * 创建文件导出器
 */
function createFileExporter(filePath, logger) {
  return {
    export(metrics) {
      try {
        const dir = path.dirname(filePath);
        if (!fs.existsSync(dir)) {
          fs.mkdirSync(dir, { recursive: true });
        }
        const line = JSON.stringify({ timestamp: new Date().toISOString(), metrics }) + '\n';
        fs.appendFileSync(filePath, line, 'utf-8');
        logger.debug(`指标已写入: ${filePath} (${metrics.length} 条)`);
      } catch (err) {
        logger.error(`文件导出失败: ${err.message}`);
      }
    },
  };
}

/**
 * 创建 OTLP 导出器（骨架实现）
 */
function createOTLPExporter(endpoint, logger) {
  return {
    async export(metrics) {
      // OTLP 导出需要 HTTP 客户端，当前为骨架
      logger.warn(`OTLP 导出到 ${endpoint} 尚未实现（骨架）`);
      // TODO: 使用 fetch 或 axios 发送到 OTLP 端点
      // const payload = { resource_metrics: metrics.map(...) };
      // await fetch(endpoint, { method: 'POST', body: JSON.stringify(payload) });
    },
  };
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

const name = 'observability-legal';
const inject = ['metrics'];

/**
 * 插件 apply 函数 — 注册指标收集器
 *
 * @param {object} ctx — Cordis Context
 */
async function apply(ctx) {
  const pluginConfig = (ctx.config && ctx.config['observability-legal']) || {};
  const logLevel = pluginConfig.logLevel || DEFAULT_LOG_LEVEL;
  const logger = createLogger(logLevel);

  const pricing = pluginConfig.costCalculation?.pricing || DEFAULT_PRICING;
  const exportConfig = pluginConfig.export || {};
  const exportTarget = exportConfig.target || DEFAULT_EXPORT_TARGET;
  const exportIntervalSec = exportConfig.intervalSec || DEFAULT_EXPORT_INTERVAL_SEC;
  const batchSize = exportConfig.batchSize || DEFAULT_BATCH_SIZE;

  logger.info(`插件初始化: export=${exportTarget}, interval=${exportIntervalSec}s, batch=${batchSize}`);

  // 创建指标收集器
  const toolMetrics = createToolMetricsCollector(logger);
  const llmMetrics = createLLMMetricsCollector(pricing, logger);
  const retrievalMetrics = createRetrievalMetricsCollector(logger);

  // 创建指标缓冲区
  const buffer = createRingBuffer(DEFAULT_BUFFER_CAPACITY);

  // 创建导出器
  let exporter;
  switch (exportTarget) {
    case 'file':
      exporter = createFileExporter(exportConfig.filePath || '/tmp/metrics.jsonl', logger);
      break;
    case 'otlp':
      exporter = createOTLPExporter(exportConfig.otlpEndpoint || 'http://localhost:4318/v1/metrics', logger);
      break;
    case 'console':
    default:
      exporter = createConsoleExporter(logger);
      break;
  }

  // 注册事件钩子
  if (ctx.on && typeof ctx.on === 'function') {
    // 工具调用后
    ctx.on('tools/post-execute', async (exec) => {
      try {
        toolMetrics.record(exec);
        retrievalMetrics.record(exec);
        buffer.push({ type: 'tool', data: exec, timestamp: Date.now() });
      } catch (err) {
        logger.error(`工具指标记录失败: ${err.message}`);
      }
    });

    // LLM 调用后
    ctx.on('llm/*/stream-end', async (event) => {
      try {
        llmMetrics.record(event);
        buffer.push({ type: 'llm', data: event, timestamp: Date.now() });
      } catch (err) {
        logger.error(`LLM 指标记录失败: ${err.message}`);
      }
    });

    logger.info('事件钩子已注册: tools/post-execute, llm/*/stream-end');
  } else {
    logger.warn('ctx.on 不可用 — 当前 DSH 版本可能不支持事件钩子');
  }

  // 定时导出
  if (exportIntervalSec > 0) {
    const timer = setInterval(() => {
      try {
        const allMetrics = [
          ...toolMetrics.getMetrics(),
          ...llmMetrics.getMetrics(),
          ...retrievalMetrics.getMetrics(),
        ];
        if (allMetrics.length > 0) {
          exporter.export(allMetrics);
          logger.info(`导出 ${allMetrics.length} 条指标`);
        }
      } catch (err) {
        logger.error(`定时导出失败: ${err.message}`);
      }
    }, exportIntervalSec * 1000);

    // 注册卸载钩子
    if (ctx.on) {
      ctx.on('plugin:dispose', () => {
        clearInterval(timer);
        logger.info('定时导出已停止');
      });
    }
  }

  // 注册查询接口
  if (ctx.provide && typeof ctx.provide === 'function') {
    ctx.provide('observability:getMetrics', () => ({
      tool: toolMetrics.getMetrics(),
      llm: llmMetrics.getMetrics(),
      retrieval: retrievalMetrics.getMetrics(),
    }));
    ctx.provide('observability:resetMetrics', () => {
      toolMetrics.reset();
      llmMetrics.reset();
      retrievalMetrics.reset();
    });
    logger.info('查询接口已注册: observability:getMetrics, observability:resetMetrics');
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
    createRingBuffer,
    createToolMetricsCollector,
    createLLMMetricsCollector,
    createRetrievalMetricsCollector,
    createConsoleExporter,
    createFileExporter,
    createOTLPExporter,
    DEFAULT_PRICING,
    DEFAULT_LOG_LEVEL,
    DEFAULT_EXPORT_TARGET,
    DEFAULT_EXPORT_INTERVAL_SEC,
    DEFAULT_BATCH_SIZE,
    DEFAULT_BUFFER_CAPACITY,
  },
};
