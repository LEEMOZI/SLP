# -*- coding: utf-8 -*-
"""
core.chroma_lock
================

P1-3：Chroma 并发 + 数据安全辅助工具。

非 git 项目的「最后一道防线」：
  1. FileLock：保证同一时刻只有 1 个 Python 进程在写 Chroma（跨 ingest / server / tool 调用）。
  2. post-ingest 自动快照：在 collection.add 成功后自动把 CHROMA_PATH 打 tar.gz 快照，
     按时间戳命名，保留最新 N=3 份，超过的自动删除。
  3. 提供 context managers `chroma_write_lock()` 和 `snapshot_after_write(collection_name)`，
     调用方直接用 `with` 即可。

⚠️  Chroma 自己是单文件 SQLite 写 + segment files mmap，内部有自己的锁，但：
    - ingest.py 是独立脚本、server.py 又是常驻进程 —— 两个进程同时写会冲突
    - 非 git 项目一次 ingest 坏了要全量重建（4727 chunks → ~30 分钟），快照防这个风险
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
import tarfile
from contextlib import contextmanager
from typing import Generator, List, Optional

from filelock import BaseFileLock, FileLock, Timeout

from config import CHROMA_PATH

logger = logging.getLogger("law-search.chroma_lock")

# ==========================================================================
# 常量（写死在代码里，不做配置化：P1-3 是一次性数据安全补丁）
# ==========================================================================

# 锁文件：放在 CHROMA_PATH 同级（避免被 tar 快照或 ingestion 清掉）
LOCK_FILENAME = ".lawsearch_chroma.lock"
SNAPSHOT_DIRNAME = "chroma_snapshots"
DEFAULT_KEEP = 3
DEFAULT_LOCK_TIMEOUT = 1800  # 秒 — 给 ingest 整个跑 30 分钟的余量


# ==========================================================================
# 路径 helpers
# ==========================================================================

def _chroma_root() -> str:
    return str(CHROMA_PATH).rstrip("/")


def _snapshot_dir() -> str:
    # 目录结构：chroma 是 law-search/chroma/ → 快照放 law-search/chroma_snapshots/
    root = _chroma_root()
    parent = os.path.dirname(root)
    return os.path.join(parent, SNAPSHOT_DIRNAME)


def _lock_file() -> str:
    return _chroma_root() + LOCK_FILENAME if False else os.path.join(
        os.path.dirname(_chroma_root()), LOCK_FILENAME,
    )


# ==========================================================================
# 1. 写锁
# ==========================================================================

def get_chroma_lock() -> BaseFileLock:
    """返回一个 FileLock 对象（跨进程、OS 级文件锁）。"""
    lock_path = _lock_file()
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    return FileLock(lock_path, timeout=DEFAULT_LOCK_TIMEOUT)


@contextmanager
def chroma_write_lock(timeout: Optional[int] = None) -> Generator[None, None, None]:
    """所有会触发 Chroma 写入的代码，必须用这个 context manager。

    用法::

        with chroma_write_lock():
            collection.add(documents=..., ids=..., embeddings=..., metadatas=...)

    ingest.py 和 后续 collection.delete() / collection.modify() / collection.upsert()
    全要包。只读（query / get / count）不包 — 让检索不被阻塞。
    """
    lock = get_chroma_lock()
    lock_timeout = timeout if timeout is not None else DEFAULT_LOCK_TIMEOUT
    t_start = _dt.datetime.now()
    try:
        lock.acquire(timeout=lock_timeout)
    except Timeout as exc:
        logger.error(
            "Chroma 写锁超时 %ss: 另一个 ingest 或写入进程长时间未释放（锁=%s）。"
            "请排查是否有卡住的 ingest.py / server.py 进程手动中止，然后重试。",
            lock_timeout, lock.lock_file,
        )
        raise RuntimeError(
            f"Chroma 写锁等待超过 {lock_timeout}s，未获取写权限。"
        ) from exc

    try:
        elapsed = (_dt.datetime.now() - t_start).total_seconds()
        if elapsed > 1.0:
            logger.info("Chroma 写锁获取（等待 %.1fs）→ 进入写入段", elapsed)
        yield
    finally:
        lock.release()


# ==========================================================================
# 2. 快照
# ==========================================================================

def list_snapshots(collection_name: Optional[str] = None) -> List[str]:
    """按时间戳倒序列出所有快照文件（tar.gz）。

    collection_name 若传，则只列这个 collection 的快照；否则列所有。"""
    snap_dir = _snapshot_dir()
    if not os.path.isdir(snap_dir):
        return []
    prefix = ""
    if collection_name:
        prefix = f"{collection_name}."
    files = [
        f for f in os.listdir(snap_dir)
        if f.endswith(".tar.gz") and f.startswith(prefix)
    ]
    files.sort(reverse=True)
    return [os.path.join(snap_dir, f) for f in files]


def _timestamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def make_snapshot(
    collection_name: str,
    keep: int = DEFAULT_KEEP,
) -> str:
    """打包 CHROMA_PATH 为 tar.gz 快照，返回快照绝对路径。

    - 快照命名：``{collection_name}.YYYYmmdd_HHMMSS.tar.gz``
    - 打包前 snapshot_dir 不存在则自动创建
    - 打包后自动删掉超量的旧快照，只留 keep 份
    - 返回新快照路径（供 ingest.py 日志打印）

    注意：快照是「拷贝+压缩」，不影响正在运行的 Chroma mmap 文件。
    """
    if not collection_name:
        raise ValueError("collection_name 不能为空")
    if not os.path.isdir(_chroma_root()):
        raise FileNotFoundError(f"CHROMA_PATH 不存在，无法快照: {_chroma_root()}")

    snap_dir = _snapshot_dir()
    os.makedirs(snap_dir, exist_ok=True)
    name = f"{collection_name}.{_timestamp()}.tar.gz"
    dst = os.path.join(snap_dir, name)

    logger.info(
        "Chroma snapshot 开始 | %s → %s (keep=%s)",
        _chroma_root(), dst, keep,
    )

    with tarfile.open(dst, "w:gz") as tar:
        # arcname=. 保证解压后直接得到 chroma/，而不是带绝对路径的垃圾
        tar.add(_chroma_root(), arcname=os.path.basename(_chroma_root()))

    size_mb = os.path.getsize(dst) / (1024 * 1024)
    logger.info(
        "Chroma snapshot 完成 | %s  %.1f MB", dst, size_mb,
    )

    # 超量清理（只对同 collection_name 的快照生效）
    snapshots = list_snapshots(collection_name)
    if len(snapshots) > keep:
        for old in snapshots[keep:]:
            try:
                os.remove(old)
                logger.info("Chroma snapshot 清理超量：%s", old)
            except OSError as exc:
                logger.warning("Chroma snapshot 清理失败 %s: %s", old, exc)

    return dst


@contextmanager
def snapshot_after_write(
    collection_name: str,
    *,
    keep: int = DEFAULT_KEEP,
    enable: bool = True,
) -> Generator[bool, None, None]:
    """写入段完成后自动做一次快照的 context manager。

    与 chroma_write_lock 叠用推荐::

        with chroma_write_lock():
            with snapshot_after_write("law_library") as snap_enabled:
                collection.add(...)
            # 出 context 时 collection.add 无异常 → 打快照；异常 → 不打快照

    被 yield 出来的 snap_enabled 是 True/False，可用来打日志。
    """
    if not enable:
        yield False
        return

    # 1. 进入：先记录进入前的快照列表（防止异常情况下把「进入前快照」误删，虽然我们
    #    只删 collection_name 自己的、超过 keep 数量的，但记一份更安心）
    _before = list_snapshots(collection_name)

    yielded_ok = False
    try:
        yield True
        yielded_ok = True
    finally:
        if yielded_ok:
            try:
                make_snapshot(collection_name, keep=keep)
            except Exception as exc:  # pragma: no cover - 日志层兜底
                logger.exception(
                    "Chroma 写入后快照失败（写入本身已成功，请手动补快照）: %s",
                    exc,
                )
        else:
            logger.warning(
                "Chroma 写入段发生异常 → 跳过 snapshot_after_write（%s）。",
                collection_name,
            )
            # 不 touch 任何文件（不删不新增）


# ==========================================================================
# 3. 便捷函数：restore_latest（防 ingest 损坏时手动回滚；不在流程中自动调用）
# ==========================================================================

def restore_latest(
    collection_name: str,
    *,
    into: Optional[str] = None,
) -> str:
    """把最近一次快照恢复到指定目录（默认覆盖 CHROMA_PATH；恢复前会把当前 CHROMA_PATH 改名
    为 .before_restore_时间戳，不直接覆盖保证可逆）。

    返回新目录路径。
    """
    snaps = list_snapshots(collection_name)
    if not snaps:
        raise FileNotFoundError(f"{collection_name} 没有任何快照可还原")
    src = snaps[0]
    target = into if into else _chroma_root()

    # 先归档当前
    if os.path.exists(target):
        backup = (
            target
            + ".before_restore_"
            + _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        shutil.move(target, backup)
        logger.info("Chromadb 恢复前归档：%s → %s", target, backup)

    os.makedirs(target, exist_ok=True)
    with tarfile.open(src, "r:gz") as tar:
        tar.extractall(path=os.path.dirname(target))
    logger.info("Chroma 恢复完成：%s → %s", src, target)
    return target
