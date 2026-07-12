from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


_guard = threading.Lock()
_process_locks: dict[Path, threading.RLock] = {}
_local = threading.local()


def _process_lock(path: Path) -> threading.RLock:
    with _guard:
        return _process_locks.setdefault(path, threading.RLock())


def _lock_file(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle: object) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def run_lock(run_dir: Path) -> Iterator[None]:
    """Hold a non-blocking process and OS lock for one run mutation.

    The lock is re-entrant in the owning thread so public operations may call
    other locked operations without weakening cross-process exclusion.
    """

    lock_path = (run_dir / "run.lock").resolve()
    process_lock = _process_lock(lock_path)
    if not process_lock.acquire(blocking=False):
        raise ValueError("this run already has an active operation")
    depths = getattr(_local, "depths", None)
    if depths is None:
        depths = {}
        _local.depths = depths
    depth = int(depths.get(lock_path, 0))
    if depth:
        depths[lock_path] = depth + 1
        try:
            yield
        finally:
            depths[lock_path] -= 1
            process_lock.release()
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"\0")
            handle.flush()
        try:
            _lock_file(handle)
        except (OSError, BlockingIOError) as error:
            raise ValueError("this run already has an active operation") from error
        depths[lock_path] = 1
        try:
            yield
        finally:
            depths.pop(lock_path, None)
            _unlock_file(handle)
    finally:
        handle.close()
        process_lock.release()
