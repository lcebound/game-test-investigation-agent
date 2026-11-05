"""Durable, content-free spans across API and Temporal execution boundaries."""
from __future__ import annotations

import hashlib
import logging
import time
import uuid
import inspect
from functools import wraps
from contextvars import ContextVar


_current_span: ContextVar[tuple[str, str] | None] = ContextVar("investigation_span", default=None)


def trace_operation(name, *, identity="investigation"):
    """Service boundary instrumentation; resolver inputs never enter span attributes."""
    def decorate(function):
        parameter = list(inspect.signature(function).parameters)[1]
        def resolve(owner, value):
            if identity == "object":
                return value.investigation_id
            if identity == "action_id":
                return owner.repository.get_action(value)[0].investigation_id
            return value
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(owner, *args, **kwargs):
                value = args[0] if args else kwargs[parameter]
                with InvestigationTracer(owner.repository).span(resolve(owner, value), name):
                    return await function(owner, *args, **kwargs)
            return asynchronous
        @wraps(function)
        def synchronous(owner, *args, **kwargs):
            value = args[0] if args else kwargs[parameter]
            with InvestigationTracer(owner.repository).span(resolve(owner, value), name):
                return function(owner, *args, **kwargs)
        return synchronous
    return decorate


class InvestigationTracer:
    def __init__(self, repository):
        self.repository = repository

    def span(self, investigation_id: str, name: str, **attributes):
        return _Span(self.repository, investigation_id, name, attributes)


class _Span:
    def __init__(self, repository, investigation_id, name, attributes):
        self.repository, self.investigation_id = repository, investigation_id
        self.name, self.attributes = name, attributes

    def __enter__(self):
        self.trace_id = hashlib.sha256(self.investigation_id.encode()).hexdigest()[:32]
        self.parent = _current_span.get()
        self.span_id = uuid.uuid4().hex[:16]
        self.token = _current_span.set((self.trace_id, self.span_id))
        self.start = time.perf_counter()
        return {"trace_id": self.trace_id, "span_id": self.span_id}

    def __exit__(self, exc_type, error, traceback):
        # A class context manager preserves even immutable provider exception objects.
        _current_span.reset(self.token)
        safe = {k: v for k, v in self.attributes.items()
                if k in {"turn", "tool_name", "provider_id", "mode", "backend"}
                and isinstance(v, (str, int, bool))}
        payload = {"trace_id": self.trace_id, "span_id": self.span_id,
                   "parent_span_id": self.parent[1] if self.parent and self.parent[0] == self.trace_id else None,
                   "name": self.name, "duration_ms": round((time.perf_counter() - self.start) * 1000, 3),
                   "status": "error" if exc_type else "ok", "error_type": exc_type.__name__ if exc_type else None,
                   "attributes": safe}
        try:
            self.repository.record_investigation_event(self.investigation_id, "trace.span", payload)
        except Exception:
            logging.getLogger(__name__).warning("trace_recording_failed")
        return False
