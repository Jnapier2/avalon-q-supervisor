"""Small cross-platform interprocess file lock."""

from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class LockTimeoutError(OSError):
    """Raised when an exclusive lock cannot be obtained within its bound."""


_REGISTRY_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve()))
    with _REGISTRY_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.RLock())


def _try_file_lock(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def exclusive_file_lock(path: str | Path, *, timeout_seconds: float = 5.0) -> Iterator[None]:
    """Hold a process and thread exclusive lock for a bounded interval."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    local_lock = _thread_lock(lock_path)
    if not local_lock.acquire(timeout=timeout_seconds):
        raise LockTimeoutError("timed out waiting for the in-process state lock")
    handle = None
    locked = False
    try:
        handle = lock_path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                _try_file_lock(handle)
                locked = True
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeoutError("timed out waiting for the interprocess state lock") from exc
                time.sleep(0.01)
        yield
    finally:
        if handle is not None:
            try:
                if locked:
                    _unlock_file(handle)
            finally:
                handle.close()
        local_lock.release()
