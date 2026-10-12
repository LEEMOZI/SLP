/**
 * @law-agent/session-storage-cloud-sync — DSH 会话自动推送插件
 *
 * 核心逻辑：
 *   1. 监听 ctx.on('session/event') — 每条事件到来时重置 debounce 定时器
 *   2. 30 秒无新事件 → 读取该 sessionId 的 session.jsonl.zstd
 *   3. 解析出全部消息 → POST 到 session-storage-cloud /bulk-push
 *   4. 服务端 upsert（先删再插），幂等安全
 *
 * 不依赖本地 Python venv，不依赖 npm zstd 包：
 *   - macOS 预装 zstd CLI 做解压
 *   - 解析 JSONL 是纯 Node.js
 */

'use strict';

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const os = require('os');
const { URL } = require('url');

// ============================================================================
// 日志
// ============================================================================

function log(level, msg) {
  const ts = new Date().toISOString();
  process.stderr.write(`[session-sync] [${level}] [${ts}] ${msg}\n`);
}

// ============================================================================
// zstd 解压
// ============================================================================

/**
 * 用 macOS 预装的 zstd CLI 解压 session.jsonl.zstd
 * 返回解析后的 JSONL 数组
 */
function decompressZstd(zstdPath) {
  return new Promise((resolve, reject) => {
    const proc = spawn('zstd', ['-d', '-f', '-c', zstdPath]);
    let data = '';
    let err = '';
    proc.stdout.on('data', chunk => { data += chunk.toString(); });
    proc.stderr.on('data', chunk => { err += chunk.toString(); });
    proc.on('error', reject);
    proc.on('close', code => {
      if (code !== 0) {
        reject(new Error(`zstd exit=${code}: ${err.trim()}`));
      } else {
        resolve(data);
      }
    });
  });
}

// ============================================================================
// 解析 session.jsonl.zstd
// ============================================================================

/**
 * 解析 DSH 的 session.jsonl.zstd，提取全部消息
 * 格式参考 MCP/session-storage/importer.py 的 _parse_zstd()
 *
 * @param {string} zstdPath - 会话文件路径
 * @returns {{title: string, provider: string, model: string, created_at: number, messages: Array}}
 */
async function parseSessionFile(zstdPath) {
  const raw = await decompressZstd(zstdPath);
  const messages = [];
  let title = '';
  let provider = '';
  let model = '';
  let created_at = 0;
  let seq = 0;

  for (const line of raw.split('\n')) {
    if (!line.trim()) continue;
    let obj;
    try { obj = JSON.parse(line); } catch { continue; }

    const ltype = obj.type;
    if (ltype === 'session') {
      created_at = obj.data?.createdAt || obj.createdAt || 0;
    } else if (ltype === 'session/title') {
      title = (obj.data?.title) || '';
    } else if (ltype === 'model/selection') {
      provider = obj.data?.provider || '';
      model = obj.data?.model || '';
    } else if (ltype === 'user/message' || ltype === 'assistant/message') {
      const data = obj.data || {};
      const msg = data.message || data;
      for (const block of (msg.content || [])) {
        if (!block || typeof block !== 'object') continue;
        const btype = block.type;
        if (btype === 'reasoning') continue;
        if (btype === 'text') {
          messages.push({
            role: msg.role || (ltype === 'user/message' ? 'user' : 'assistant'),
            content_type: 'text',
            content: block.text || '',
            tool_name: null,
            time: obj.time || 0,
          });
          seq++;
        } else if (btype === 'tool-call') {
          messages.push({
            role: 'assistant',
            content_type: 'tool-call',
            content: block.arguments || '{}',
            tool_name: block.name || '',
            time: obj.time || 0,
          });
          seq++;
        }
      }
    }
  }

  return { title, provider, model, created_at, messages };
}

// ============================================================================
// 找到 session.jsonl.zstd 路径
// ============================================================================

function findSessionFile(sessionDir, sessionId) {
  // DSH 路径: sessions/<workspace-slug>/session-<uuid>/session.jsonl.zstd
  if (!fs.existsSync(sessionDir)) return null;
  const workspaces = fs.readdirSync(sessionDir);
  for (const ws of workspaces) {
    const sessionPath = path.join(sessionDir, ws, sessionId, 'session.jsonl.zstd');
    if (fs.existsSync(sessionPath)) return sessionPath;
    // 也可能 ws 名就是 sessionId 的前缀
    for (const sub of fs.readdirSync(path.join(sessionDir, ws))) {
      if (sub === sessionId) {
        const p = path.join(sessionDir, ws, sub, 'session.jsonl.zstd');
        if (fs.existsSync(p)) return p;
      }
    }
  }
  // 直接递归搜
  try {
    const cmd = `find "${sessionDir}" -name "${sessionId}" -type d 2>/dev/null | head -1`;
    const result = require('child_process').execSync(cmd, { timeout: 3000 }).toString().trim();
    if (result) {
      const p = path.join(result, 'session.jsonl.zstd');
      if (fs.existsSync(p)) return p;
    }
  } catch (_) {}
  return null;
}

// ============================================================================
// HTTP POST 到 /bulk-push
// ============================================================================

async function bulkPush(endpoint, token, payload, timeoutMs) {
  return new Promise((resolve, reject) => {
    const url = new URL(endpoint);
    const body = JSON.stringify(payload);
    const req = http.request({
      method: 'POST',
      hostname: url.hostname,
      port: url.port || (url.protocol === 'https:' ? 443 : 80),
      path: url.pathname,
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Authorization': `Bearer ${token}`,
      },
      timeout: timeoutMs,
    }, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); } catch { resolve({ raw: data }); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(new Error('timeout')); });
    req.write(body);
    req.end();
  });
}

// 动态 import http/https（用 require 兼容 CommonJS）
const http = require('http');
const https = require('https');

// 修正 bulkPush 支持 https
function bulkPushFixed(endpoint, token, payload, timeoutMs) {
  return new Promise((resolve, reject) => {
    const url = new URL(endpoint);
    const lib = url.protocol === 'https:' ? https : http;
    const body = JSON.stringify(payload);
    const req = lib.request({
      method: 'POST',
      hostname: url.hostname,
      port: url.port || (url.protocol === 'https:' ? 443 : 80),
      path: url.pathname,
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        'Authorization': `Bearer ${token}`,
      },
      timeout: timeoutMs,
    }, (res) => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); } catch { resolve({ raw: data }); }
      });
    });
    req.on('error', reject);
    req.on('timeout', () => { req.destroy(new Error('timeout')); });
    req.write(body);
    req.end();
  });
}

// ============================================================================
// Debouncer
// ============================================================================

class Debouncer {
  constructor(waitMs, fn) {
    this.waitMs = waitMs;
    this.fn = fn;
    this.timers = new Map(); // sessionId → setTimeout
  }
  trigger(sessionId) {
    if (this.timers.has(sessionId)) clearTimeout(this.timers.get(sessionId));
    this.timers.set(sessionId, setTimeout(async () => {
      this.timers.delete(sessionId);
      try { await this.fn(sessionId); }
      catch (e) { log('error', `push ${sessionId}: ${e.message}`); }
    }, this.waitMs));
  }
  cancel(sessionId) {
    if (this.timers.has(sessionId)) {
      clearTimeout(this.timers.get(sessionId));
      this.timers.delete(sessionId);
    }
  }
}

// ============================================================================
// 主推送函数
// ============================================================================

async function pushSession(config, sessionId) {
  const zstdPath = findSessionFile(config.sessionDir, sessionId);
  if (!zstdPath) {
    log('warn', `${sessionId}: 找不到 session.jsonl.zstd，跳过`);
    return;
  }
  const parsed = await parseSessionFile(zstdPath);
  if (!parsed.messages.length) {
    log('info', `${sessionId}: 0 条消息，跳过`);
    return;
  }
  const payload = {
    session_id: sessionId,
    title: parsed.title || '',
    provider: parsed.provider || '',
    model: parsed.model || '',
    created_at_ms: parsed.created_at || 0,
    source: config.pushSource || 'dsh-sync',
    messages: parsed.messages,
  };
  const result = await bulkPushFixed(config.endpoint, config.token, payload, config.pushTimeoutMs || 15000);
  if (result && result.success) {
    log('info', `${sessionId}: ✅ 推送 ${result.messages} 条`);
  } else {
    log('warn', `${sessionId}: 推送失败 ${JSON.stringify(result).slice(0, 200)}`);
  }
}

// ============================================================================
// Cordis 插件入口
// ============================================================================

const name = 'session-storage-cloud-sync';
const inject = ['sessions'];

async function apply(ctx) {
  const cfg = (ctx.config && ctx.config[name]) || {};
  if (cfg.enabled === false) { log('info', '插件已禁用'); return; }

  // 路径展开 ~
  let sessionDir = cfg.sessionDir || '~/Library/Application Support/dsh-desktop/harness/sessions';
  sessionDir = sessionDir.replace(/^~/, os.homedir());
  const endpoint = cfg.endpoint || 'http://100.66.217.64:8767/bulk-push';
  const token = cfg.token || 'abcd1234ef';
  const debounceMs = cfg.debounceMs || 30000;

  const debouncer = new Debouncer(debounceMs, (sid) => pushSession({ ...cfg, sessionDir, endpoint, token }, sid));

  // 关键：如果本机就是服务端（Studio），跳过 — 本地已经有 sync_sessions + bridge.append
  // 用 endpoint 里的 IP 判断
  const myIps = await new Promise(resolve => {
    require('dns').lookup(require('os').hostname(), { all: true }, (err, addrs) => {
      resolve(err ? [] : addrs.map(a => a.address));
    });
  }).catch(() => []);
  const epHost = new URL(endpoint).hostname;
  if (myIps.includes(epHost) || epHost === '127.0.0.1' || epHost === 'localhost') {
    log('info', `本机就是 session-storage-cloud 服务端（${epHost}），跳过自动推送`);
    return;
  }

  // 监听 session/event
  if (ctx.on && typeof ctx.on === 'function') {
    ctx.on('session/event', async (event) => {
      const sid = event?.sessionId;
      if (!sid) return;
      log('debug', `session/event → ${sid}`);
      debouncer.trigger(sid);
    });
    log('info', `已监听 session/event，debounce=${debounceMs}ms → ${endpoint}`);
  } else {
    log('warn', 'ctx.on 不可用');
  }

  // 卸载时清理所有 pending 推送
  if (ctx.on && typeof ctx.on === 'function') {
    ctx.on('plugin:dispose', () => {
      for (const sid of debouncer.timers.keys()) debouncer.cancel(sid);
      log('info', '插件已卸载');
    });
  }
}

module.exports = { name, inject, apply };
