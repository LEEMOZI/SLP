# -*- coding: utf-8 -*-
"""
session-storage.db
==================

会话持久化库（SQLite + FTS5）存储层。

设计要点（对齐 law-search 的 chroma_lock 模式）：
  1. OS 级文件锁（filelock）：session-storage MCP server 可能同时被
     DSH Desktop 和 LM Studio 两个 Host 拉起，写 SQLite 必须跨进程互斥。
  2. WAL 模式 + busy_timeout：读写分离，检索不被写入阻塞。
  3. FTS5 trigram tokenizer：支持中文子串全文检索（SQLite 3.34+，本机 3.50.4）。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Generator, Optional

from filelock import FileLock, Timeout

logger = logging.getLogger("session-storage.db")

# ==========================================================================
# 路径常量
# ==========================================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "sessions.db")
LOCK_PATH = os.path.join(DATA_DIR, ".session_storage.lock")

LOCK_TIMEOUT = 60  # 秒；会话写入都是轻量事务，60s 足够

# ==========================================================================
# 连接与锁
# ==========================================================================

def _ensure_dirs() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)


def get_write_lock() -> FileLock:
    """跨进程写锁（DSH Desktop / LM Studio 双 Host 同时挂本服务时互斥）。"""
    _ensure_dirs()
    return FileLock(LOCK_PATH, timeout=LOCK_TIMEOUT)


@contextmanager
def session_write_lock() -> Generator[None, None, None]:
    lock = get_write_lock()
    try:
        lock.acquire(timeout=LOCK_TIMEOUT)
    except Timeout as exc:
        raise RuntimeError(
            f"session-storage 写锁等待超过 {LOCK_TIMEOUT}s：另一个 Host 的 "
            f"session-storage 进程正在写入（锁={LOCK_PATH}）。"
        ) from exc
    try:
        yield
    finally:
        lock.release()


def connect() -> sqlite3.Connection:
    """新建一个连接（WAL + busy_timeout），调用方自行 close。"""
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_connection() -> Generator[sqlite3.Connection, None, None]:
    """数据库连接上下文管理器：自动 open/close，避免忘记关闭导致连接泄漏。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# ==========================================================================
# Schema
# ==========================================================================

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id      TEXT PRIMARY KEY,      -- session-<uuid>
  workspace       TEXT,                  -- DSH 工作区目录 slug
  cwd             TEXT,                  -- 会话工作目录
  title           TEXT,
  created_at      INTEGER,               -- ms epoch
  last_prompt_at  INTEGER,               -- ms epoch（projcache）
  turns           INTEGER,
  steps           INTEGER,
  provider        TEXT,
  model           TEXT,
  input_tokens    INTEGER,
  output_tokens   INTEGER,
  llm_ms          INTEGER,
  tool_ms         INTEGER,
  source          TEXT DEFAULT 'import', -- import=从 DSH 文件导入 / manual=LLM 归档
  tags            TEXT DEFAULT '',       -- 逗号分隔：客户/案件/领域
  summary         TEXT DEFAULT '',       -- 人工或 LLM 摘要
  zstd_mtime      INTEGER,               -- 增量导入判断依据
  zstd_path       TEXT,                  -- 溯源
  imported_at     INTEGER
);

CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL,
  seq           INTEGER,
  role          TEXT,                    -- user / assistant
  turn          INTEGER,
  step          INTEGER,
  content_type  TEXT,                    -- text / tool-call
  content       TEXT,                    -- 文本内容；tool-call 时为 arguments JSON
  tool_name     TEXT,
  time          INTEGER,
  FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_messages_role ON messages(session_id, role);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  content,
  session_id UNINDEXED,
  message_id UNINDEXED,
  tokenize='trigram'
);
"""


def init_db() -> None:
    """初始化 schema（幂等）。"""
    _ensure_dirs()
    conn = connect()
    try:
        with session_write_lock():
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        logger.info("DB ready: %s", DB_PATH)
    finally:
        conn.close()
