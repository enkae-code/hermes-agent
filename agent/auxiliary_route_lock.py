"""A route-locked session's guard on auxiliary calls: under ``locked_route`` only the client for the one frozen
endpoint and credential may be built, and the recovery ladder never rotates a key or walks a fallback chain."""

from __future__ import annotations

import contextlib
import contextvars
import functools
from typing import Any, Callable, Optional

from agent.auxiliary_unavailable import AuxiliaryClientUnavailable

_LOCKED_ROUTE: contextvars.ContextVar[
    Optional[Callable[[Any, Optional[str]], bool]]
] = contextvars.ContextVar("auxiliary_locked_route", default=None)


@contextlib.contextmanager
def locked_route(allows: Callable[[Any, Optional[str]], bool]):
    """Bind ``allows(client, model)``: true only for the session's frozen endpoint, credential and model."""
    token = _LOCKED_ROUTE.set(allows)
    try:
        yield
    finally:
        _LOCKED_ROUTE.reset(token)


def route_is_locked() -> bool:
    return _LOCKED_ROUTE.get() is not None


def require_locked_route(client: Any, model: Optional[str]) -> None:
    allows = _LOCKED_ROUTE.get()
    if allows is not None and not allows(client, model):
        raise AuxiliaryClientUnavailable(
            "this session is locked to one route; the auxiliary call was not sent"
        )


def guarded(client_at: int = 0):
    """Decorate a client getter returning a tuple with ``(client, model)`` at ``client_at``: under a locked route,
    a client for any other endpoint is refused, so every caller of the getter inherits the check."""

    def wrap(getter):
        @functools.wraps(getter)
        def inner(*args, **kwargs):
            result = getter(*args, **kwargs)
            if result[client_at] is not None:
                require_locked_route(result[client_at], result[client_at + 1])
            return result

        return inner

    return wrap
