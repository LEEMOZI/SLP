/**
 * @law-agent/session-storage-fs — DSH 会话存储后端插件
 *
 * 通过 DSH Cordis 的 ctx.sessions 接口注册 StorageBackend，桥接 DSH 原生
 * 会话管理与现有 Python session-storage MCP 的 SQLite+FTS5 后端。
 *
 * 核心职责：
 *   1. 注册 StorageBackend（append/replay 接口）
 *   2. 监听 DSH 会话事件（session/event）并转发到 Python 后端
 *   3. 提供会话检索、标签归档、导出等功能
 *   4. 自动同步 DSH Desktop 会话文件到 SQLite
 *
 * 架构设计：
 *   - 本插件运行在 DSH Node.js 进程中
 *   - 通过 child_process 调用 Python 脚本执行 SQLite 操作
 *   - 与 Python session-storage MCP 共享同一个 SQLite 数据库
 *   - FTS5 全文检索由 Python 侧实现（trigram tokenizer 支持中文）
 *
 * 设计原则：
 *   - 与 Python 侧零耦合（共享数据库文件，不共享进程）
 *   - 异常容错：插件自身错误不阻断 DSH 主流程
 *   - 审计日志写入 stderr，不污染 stdout
 *
 * @see MCP/HARNESS_PLUGIN_ARCHITECTURE.md — 扩展点 #8 Session / Storage
 * @see MCP/session-storage/server.py — Python MCP 侧实现
 */

'use strict';

const { execFileSync } = require('child_process');
const path = require('path');

// ============================================================================
// 默认配置
// ============================================================================

const DEFAULT_DB_PATH = '/Users/simon/Documents/1-SLP/MCP/session-storage/data/sessions.db';
const DEFAULT_DSH_SESSIONS_DIR = '~/Library/Application Support/dsh-desktop/harness/sessions';
const DEFAULT_EXPORT_DIR = '~/Desktop/dsh_sessions';
const DEFAULT_LOG_LEVEL = 'warn';
const DEFAULT_AUTO_SYNC_INTERVAL_SEC = 300;
const DEFAULT_SEARCH_LIMIT = 10;

// Python session-storage 目录
const SESSION_STORAGE_DIR = '/Users/simon/Documents/1-SLP/MCP/session-storage';
const PYTHON_BIN = '/Users/simon/Documents/1-SLP/venv/bin/python';

// ============================================================================
// 日志工具
// ============================================================================

const LOG_LEVELS = { error: 0, warn: 1, info: 2, debug: 3 };

function createLogger(level) {
  const threshold = LOG_LEVELS[level] !== undefined ? LOG_LEVELS[level] : LOG_LEVELS.warn;
  return {
    error: (msg) => { if (threshold >= 0) process.stderr.write(`[session-storage-fs] [ERROR] ${msg}\n`); },
    warn:  (msg) => { if (threshold >= 1) process.stderr.write(`[session-storage-fs] [WARN]  ${msg}\n`); },
    info:  (msg) => { if (threshold >= 2) process.stderr.write(`[session-storage-fs] [INFO]  ${msg}\n`); },
    debug: (msg) => { if (threshold >= 3) process.stderr.write(`[session-storage-fs] [DEBUG] ${msg}\n`); },
  };
}

// ============================================================================
// Python 桥接层
// ============================================================================

/**
 * 调用 Python session-storage 脚本执行数据库操作
 * 使用 execFileSync 同步调用，避免异步复杂性
 *
 * @param {string} script - Python 脚本名（不含路径）
 * @param {string[]} args - 命令行参数
 * @param {object} logger - 日志对象
 * @returns {object} 解析后的 JSON 结果，或 null
 */
function callPython(script, args = [], logger) {
  const scriptPath = path.join(SESSION_STORAGE_DIR, script);
  try {
    const result = execFileSync(PYTHON_BIN, [scriptPath, ...args], {
      cwd: SESSION_STORAGE_DIR,
      timeout: 30000,  // 30 秒超时
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    return JSON.parse(result.trim());
  } catch (err) {
    logger.error(`Python 调用失败: ${script} ${args.join(' ')} — ${err.message}`);
    return null;
  }
}

// ============================================================================
// StorageBackend 实现
// ============================================================================

/**
 * 创建 StorageBackend 实例
 * 实现 DSH 的 StorageBackend 接口：append() 和 replay()
 *
 * @param {object} config - 插件配置
 * @param {object} logger - 日志对象
 * @returns {object} StorageBackend 实例
 */
function createStorageBackend(config, logger) {
  const dbPath = config.dbPath || DEFAULT_DB_PATH;

  return {
    name: 'session-storage-fs',
    dbPath,

    /**
     * append(event) — 存储一个会话事件
     * DSH 在会话过程中调用此方法记录事件
     *
     * @param {object} event - 会话事件
     * @param {string} event.sessionId - 会话 ID
     * @param {string} event.type - 事件类型（user/message, assistant/message, tool-call 等）
     * @param {object} event.data - 事件数据
     * @param {number} event.time - 事件时间戳（ms epoch）
     */
    async append(event) {
      if (!event || !event.sessionId) {
        logger.warn('append: 无效事件（缺少 sessionId）');
        return;
      }

      logger.debug(`append: sessionId=${event.sessionId}, type=${event.type}`);

      // 将事件转发到 Python 后端
      // Python 侧负责写入 SQLite + FTS5 索引
      const result = callPython('bridge.py', [
        'append',
        '--session-id', event.sessionId,
        '--type', event.type || 'unknown',
        '--data', JSON.stringify(event.data || {}),
        '--time', String(event.time || Date.now()),
      ], logger);

      if (result === null) {
        logger.warn(`append: 事件写入失败 sessionId=${event.sessionId}`);
      }
    },

    /**
     * replay(sessionId) — 回放某个会话的全部事件
     * DSH 在恢复会话或查看历史时调用此方法
     *
     * @param {string} sessionId - 会话 ID
     * @returns {Array<object>} 事件列表（按时间排序）
     */
    async replay(sessionId) {
      if (!sessionId) {
        logger.warn('replay: 缺少 sessionId');
        return [];
      }

      logger.debug(`replay: sessionId=${sessionId}`);

      // 从 Python 后端读取会话消息
      const result = callPython('bridge.py', [
        'replay',
        '--session-id', sessionId,
      ], logger);

      if (result === null) {
        logger.warn(`replay: 会话回放失败 sessionId=${sessionId}`);
        return [];
      }

      return result.messages || [];
    },

    /**
     * _internal — 内部函数导出供单元测试使用
     */
    _internal: {
      getDbPath: () => dbPath,
    },
  };
}

// ============================================================================
// 会话管理器
// ============================================================================

/**
 * 创建会话管理器
 * 提供高级会话操作：搜索、标签、导出、统计
 *
 * @param {object} config - 插件配置
 * @param {object} logger - 日志对象
 * @returns {object} 会话管理器实例
 */
function createSessionManager(config, logger) {
  const searchLimit = config.search?.defaultLimit || DEFAULT_SEARCH_LIMIT;
  const exportDir = config.export?.defaultDir || DEFAULT_EXPORT_DIR;

  return {
    /**
     * search(query, options) — 全文检索历史会话
     *
     * @param {string} query - 搜索关键词
     * @param {object} options - 可选参数
     * @param {number} options.limit - 返回条数
     * @param {string} options.tag - 按标签过滤
     * @returns {Array<object>} 匹配的会话列表
     */
    async search(query, options = {}) {
      if (!query || query.length < 2) {
        logger.warn('search: 查询太短（需 ≥2 字符）');
        return [];
      }

      const limit = options.limit || searchLimit;
      const tag = options.tag || '';

      logger.info(`search: query="${query}", limit=${limit}, tag="${tag}"`);

      const result = callPython('bridge.py', [
        'search',
        '--query', query,
        '--limit', String(limit),
        '--tag', tag,
      ], logger);

      if (result === null) {
        return [];
      }

      return result.sessions || [];
    },

    /**
     * get(sessionId) — 获取会话详情
     *
     * @param {string} sessionId - 会话 ID
     * @param {boolean} includeToolCalls - 是否包含工具调用记录
     * @returns {object|null} 会话详情
     */
    async get(sessionId, includeToolCalls = false) {
      if (!sessionId) {
        logger.warn('get: 缺少 sessionId');
        return null;
      }

      const result = callPython('bridge.py', [
        'get',
        '--session-id', sessionId,
        ...(includeToolCalls ? ['--include-tool-calls'] : []),
      ], logger);

      return result;
    },

    /**
     * tag(sessionId, tags, summary) — 给会话打标签
     *
     * @param {string} sessionId - 会话 ID
     * @param {string} tags - 逗号分隔的标签
     * @param {string} summary - 可选摘要
     */
    async tag(sessionId, tags, summary = '') {
      if (!sessionId || !tags) {
        logger.warn('tag: 缺少 sessionId 或 tags');
        return;
      }

      const result = callPython('bridge.py', [
        'tag',
        '--session-id', sessionId,
        '--tags', tags,
        '--summary', summary,
      ], logger);

      if (result) {
        logger.info(`tag: 已归档 ${sessionId} → [${tags}]`);
      }
    },

    /**
     * export(sessionId, outDir) — 导出会话为 Markdown
     *
     * @param {string} sessionId - 会话 ID
     * @param {string} outDir - 输出目录
     * @returns {string|null} 导出文件路径
     */
    async export(sessionId, outDir = '') {
      if (!sessionId) {
        logger.warn('export: 缺少 sessionId');
        return null;
      }

      const targetDir = outDir || exportDir;
      const result = callPython('bridge.py', [
        'export',
        '--session-id', sessionId,
        '--out-dir', targetDir,
      ], logger);

      return result ? result.path : null;
    },

    /**
     * stats() — 获取会话库统计信息
     *
     * @returns {object} 统计信息
     */
    async stats() {
      const result = callPython('bridge.py', ['stats'], logger);
      return result;
    },

    /**
     * sync() — 同步 DSH Desktop 会话文件到 SQLite
     *
     * @returns {object} 同步统计
     */
    async sync() {
      logger.info('sync: 开始同步 DSH 会话文件...');
      const result = callPython('bridge.py', ['sync'], logger);
      if (result) {
        logger.info(`sync: 完成 — 总计 ${result.total}, 导入 ${result.imported}, 跳过 ${result.skipped}, 失败 ${result.failed}`);
      }
      return result;
    },
  };
}

// ============================================================================
// 自动同步
// ============================================================================

/**
 * 启动自动同步定时器
 *
 * @param {object} manager - 会话管理器
 * @param {object} config - 自动同步配置
 * @param {object} logger - 日志对象
 * @returns {Function} stop 函数
 */
function startAutoSync(manager, config, logger) {
  if (!config.enabled) {
    logger.info('自动同步已禁用');
    return () => {};
  }

  const intervalSec = config.intervalSec || DEFAULT_AUTO_SYNC_INTERVAL_SEC;
  logger.info(`自动同步已启用: 每 ${intervalSec} 秒`);

  // 启动时立即同步一次
  if (config.onStartup) {
    setTimeout(() => {
      manager.sync().catch(err => {
        logger.error(`启动同步失败: ${err.message}`);
      });
    }, 5000);  // 延迟 5 秒，等待 DSH 完全启动
  }

  const timer = setInterval(() => {
    manager.sync().catch(err => {
      logger.error(`定时同步失败: ${err.message}`);
    });
  }, intervalSec * 1000);

  return () => {
    clearInterval(timer);
    logger.info('自动同步已停止');
  };
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

const name = 'session-storage-fs';
const inject = ['sessions'];

/**
 * 插件 apply 函数 — 注册 StorageBackend 和会话管理器
 *
 * @param {object} ctx — Cordis Context
 */
async function apply(ctx) {
  const pluginConfig = (ctx.config && ctx.config['session-storage-fs']) || {};
  const logLevel = pluginConfig.logLevel || DEFAULT_LOG_LEVEL;
  const logger = createLogger(logLevel);

  logger.info('插件初始化');

  // 创建 StorageBackend
  const backend = createStorageBackend(pluginConfig, logger);

  // 创建会话管理器
  const manager = createSessionManager(pluginConfig, logger);

  // 注册 StorageBackend 到 Cordis
  if (ctx.sessions && typeof ctx.sessions.registerBackend === 'function') {
    ctx.sessions.registerBackend('fs', backend);
    logger.info('StorageBackend 已注册: ctx.sessions.registerBackend("fs", ...)');
  } else if (ctx.sessions) {
    // 尝试直接赋值
    ctx.sessions.storage = backend;
    logger.info('StorageBackend 已注册: ctx.sessions.storage = backend');
  } else {
    logger.warn('ctx.sessions 不可用 — 当前 DSH 版本可能不支持 StorageBackend API');
    logger.warn('降级模式：会话管理器仍可通过 ctx.provide 使用');
  }

  // 注册会话管理器供其他插件使用
  if (ctx.provide && typeof ctx.provide === 'function') {
    ctx.provide('session-storage:search', (query, options) => manager.search(query, options));
    ctx.provide('session-storage:get', (sessionId, includeToolCalls) => manager.get(sessionId, includeToolCalls));
    ctx.provide('session-storage:tag', (sessionId, tags, summary) => manager.tag(sessionId, tags, summary));
    ctx.provide('session-storage:export', (sessionId, outDir) => manager.export(sessionId, outDir));
    ctx.provide('session-storage:stats', () => manager.stats());
    ctx.provide('session-storage:sync', () => manager.sync());
    logger.info('会话管理器已注册: session-storage:{search,get,tag,export,stats,sync}');
  }

  // 监听会话事件
  if (ctx.on && typeof ctx.on === 'function') {
    ctx.on('session/event', async (event) => {
      try {
        await backend.append(event);
      } catch (err) {
        logger.error(`会话事件处理失败: ${err.message}`);
      }
    });
    logger.info('已监听: session/event');
  }

  // 启动自动同步
  const autoSyncConfig = pluginConfig.autoSync || { enabled: true, intervalSec: DEFAULT_AUTO_SYNC_INTERVAL_SEC, onStartup: true };
  const stopAutoSync = startAutoSync(manager, autoSyncConfig, logger);

  // 注册卸载钩子
  if (ctx.on && typeof ctx.on === 'function') {
    ctx.on('plugin:dispose', () => {
      stopAutoSync();
      logger.info('插件已卸载');
    });
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
    createStorageBackend: createStorageBackend,
    createSessionManager,
    startAutoSync,
    callPython,
    DEFAULT_DB_PATH,
    DEFAULT_DSH_SESSIONS_DIR,
    DEFAULT_EXPORT_DIR,
    DEFAULT_LOG_LEVEL,
    DEFAULT_AUTO_SYNC_INTERVAL_SEC,
    DEFAULT_SEARCH_LIMIT,
    SESSION_STORAGE_DIR,
    PYTHON_BIN,
  },
};
