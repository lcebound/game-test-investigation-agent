"""Process-local, bounded admission for model, tool and analysis work.

Durable action/resource ownership remains in the repository and external runner.
These limits protect one API/worker process; replicas each have their own pool.
"""
from __future__ import annotations

import asyncio
import time


class CapacityExceeded(RuntimeError):
    pass


class ExecutionLimits:
    def __init__(self, *, investigations=4, models=4, tools=8, analyses=2, max_waiters=64):
        sizes = dict(investigations=investigations, models=models, tools=tools, analyses=analyses)
        if any(not isinstance(v, int) or v < 1 for v in sizes.values()) or max_waiters < 0:
            raise ValueError("execution limits must be positive with a nonnegative wait queue")
        self._slots = {key: asyncio.BoundedSemaphore(value) for key, value in sizes.items()}
        self._sizes = sizes
        self._active = dict.fromkeys(sizes, 0)
        self._waiting = dict.fromkeys(sizes, 0)
        self.max_waiters = max_waiters
        self._scopes = {}
        self._background = {}

    @classmethod
    def from_settings(cls, settings):
        return cls(investigations=settings.max_concurrent_investigations,
                   models=settings.max_concurrent_models, tools=settings.max_concurrent_tools,
                   analyses=settings.max_concurrent_analyses, max_waiters=settings.max_queued_operations)

    def snapshot(self, scope=None):
        return {key: {"limit": self._sizes[key],
                      "active": self._active[key] if scope is None else self._scopes.get((scope, key), {}).get("active", 0),
                      "waiting": self._waiting[key] if scope is None else self._scopes.get((scope, key), {}).get("waiting", 0)}
                for key in self._slots}

    def slot(self, kind: str, *, deadline: float | None = None, scope=None):
        return _Admission(self, kind, deadline, scope)

    def available(self, kind):
        return not self._slots[kind].locked()

    async def try_background(self, key, *, kind="analyses", scope=None):
        """Keep a resource permit across bounded control calls, without a waiter."""
        if key in self._background:
            return True
        if self._slots[kind].locked():
            return False
        counts = await self._acquire(kind, None, scope)
        self._background[key] = {"kind": kind, "scope": scope, "counts": counts, "permit": True}
        return True

    async def adopt_background(self, key, *, kind="analyses", scope=None):
        """Account for already running work even if a restart lowered the limit."""
        if key in self._background or await self.try_background(key, kind=kind, scope=scope):
            return
        counts = self._scopes.setdefault((scope, kind), {"active": 0, "waiting": 0})
        counts["active"] += 1
        self._active[kind] += 1
        self._background[key] = {"kind": kind, "scope": scope, "counts": counts, "permit": False}

    def release_background(self, key):
        lease = self._background.pop(key, None)
        if lease:
            self._release(lease["kind"], lease["scope"], lease["counts"], permit=lease["permit"])

    async def _acquire(self, kind, deadline, scope):
        semaphore = self._slots[kind]
        if deadline is not None and deadline <= time.time():
            raise TimeoutError("operation deadline reached while waiting for capacity")
        if (kind == "analyses" and semaphore.locked()
                and any(lease["kind"] == kind for lease in self._background.values())):
            # Waiting here can occupy the only investigation slot needed to
            # poll the job that owns this permit. Defer the foreground owner.
            raise CapacityExceeded("analysis capacity belongs to persistent jobs; yield before retrying")
        if semaphore.locked() and self._waiting[kind] >= self.max_waiters:
            raise CapacityExceeded(f"{kind} queue is full")
        acquired = False
        counts = self._scopes.setdefault((scope, kind), {"active": 0, "waiting": 0})
        counts["waiting"] += 1
        self._waiting[kind] += 1
        try:
            if deadline is None:
                await semaphore.acquire()
            else:
                await asyncio.wait_for(semaphore.acquire(), timeout=max(0, deadline - time.time()))
            acquired = True
        finally:
            self._waiting[kind] -= 1
            counts["waiting"] -= 1
            if not acquired and not counts["active"] and not counts["waiting"]:
                self._scopes.pop((scope, kind), None)
        self._active[kind] += 1
        counts["active"] += 1
        return counts

    def _release(self, kind, scope, counts, *, permit=True):
        self._active[kind] -= 1
        counts["active"] -= 1
        if not counts["active"] and not counts["waiting"]:
            self._scopes.pop((scope, kind), None)
        if permit:
            debt = next((lease for lease in self._background.values()
                         if lease["kind"] == kind and not lease["permit"]), None)
            if debt:
                debt["permit"] = True  # Transfer the slot to recovered work first.
            else:
                self._slots[kind].release()


class _Admission:
    def __init__(self, owner, kind, deadline, scope):
        self.owner, self.kind, self.deadline, self.scope = owner, kind, deadline, scope
        self.entered = False

    async def __aenter__(self):
        if self.entered:
            raise RuntimeError("admission context is already entered")
        self.counts = await self.owner._acquire(self.kind, self.deadline, self.scope)
        self.entered = True
        return self

    async def __aexit__(self, exc_type, error, traceback):
        if self.entered:
            self.entered = False
            self.owner._release(self.kind, self.scope, self.counts)
        return False
