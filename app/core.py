"""Replacement for dbgpt_serve.core / dbgpt.component in the microservice.

- BaseService -> plain classes
- SystemApp dependency injection -> app.state + singletons
- Result wrapper -> direct Pydantic models / dicts
- blocking_func_to_async -> run_in_threadpool
- HTTP error mapping -> _http_error helper
"""

from __future__ import annotations

from typing import Any, Dict

from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException


def blocking_func_to_async(*args, **kwargs):
    """Drop-in for dbgpt.util.executor_utils.blocking_func_to_async.

    Run a CPU/IO-bound sync callable in the threadpool so the event loop
    is never blocked (critical under SSE streams).
    """
    if args:
        fn = args[0]
        return run_in_threadpool(fn, *args[1:], **kwargs)
    fn = kwargs.pop("func")
    return run_in_threadpool(fn, **kwargs)


class RevisionConflictError(ValueError):
    pass


class ResourceBusyError(RuntimeError):
    pass


def http_error(exc: Exception) -> HTTPException:
    """Map domain exceptions to HTTP status codes (mirrors DB-GPT endpoints)."""
    if isinstance(exc, RevisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ResourceBusyError):
        return HTTPException(
            status_code=429, detail=str(exc), headers={"Retry-After": "5"}
        )
    if isinstance(exc, PermissionError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))
