"""Small in-process background job runner (thread pool).

Ingestion and pipeline runs are submitted here so HTTP requests return immediately.
State lives in the catalog (``status`` columns), so a restart only loses in-flight work,
which ``recover()`` re-queues on startup.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

from .config import get_settings

log = logging.getLogger("dah.jobs")

_pool: ThreadPoolExecutor | None = None
_lock = threading.Lock()
_startup_hooks: list[Callable[[], None]] = []


def on_startup(fn: Callable[[], None]) -> Callable[[], None]:
    _startup_hooks.append(fn)
    return fn


def start() -> None:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=get_settings().ingest_workers, thread_name_prefix="dah-job")
    for hook in _startup_hooks:
        try:
            hook()
        except Exception:  # pragma: no cover
            log.exception("startup hook failed")


def stop() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.shutdown(wait=True, cancel_futures=False)
            _pool = None


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    with _lock:
        pool = _pool
    if pool is None:
        # No running app (CLI / tests): run synchronously.
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:  # pragma: no cover
            f.set_exception(e)
        return f

    def _wrapped():
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("job %s failed", getattr(fn, "__name__", fn))
            raise

    return pool.submit(_wrapped)
