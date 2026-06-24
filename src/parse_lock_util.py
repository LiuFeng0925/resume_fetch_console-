"""parse.lock 辅助：检测/清理解析任务锁。"""
from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def _read_lock_pid(lock_path: Path) -> int | None:
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        pid = int(raw)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _try_acquire_flock(lock_path: Path) -> bool:
    """尝试非阻塞获取锁，成功则立即释放。返回是否曾成功独占。"""
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except OSError:
        return False
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        fh.close()


def clear_stale_parse_lock(lock_path: Path) -> bool:
    """仅当锁文件对应进程已退出且能独占锁时删除。返回是否清理过。"""
    if not lock_path.exists():
        return False

    pid = _read_lock_pid(lock_path)
    if pid is not None and _pid_alive(pid):
        return False

    if not _try_acquire_flock(lock_path):
        return False

    if pid is not None and _pid_alive(pid):
        return False

    try:
        lock_path.unlink()
        logger.info("removed stale parse.lock (pid=%s)", pid)
        return True
    except OSError:
        return False


def is_parse_lock_active(lock_path: Path, *, db_path: Path | None = None) -> bool:
    """是否有解析任务在运行（锁 + 进度双重检测）。"""
    if lock_path.exists():
        pid = _read_lock_pid(lock_path)
        if pid is not None and _pid_alive(pid):
            return True
        if not _try_acquire_flock(lock_path):
            return True

    if db_path is not None:
        from src.parse_progress import read_progress

        prog = read_progress(db_path)
        if prog:
            pending = int(prog.get("pending") or 0)
            processed = int(prog.get("processed") or 0)
            if pending > 0 and processed < pending:
                return True

    return False
