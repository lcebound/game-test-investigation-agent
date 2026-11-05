"""Exclusive control turns, backed by OS file locks or MySQL session locks.

Behavior reference: dsh-superagent-prototype@6337fc823ce20251c8de8085948165c81aaa701e,
core/assignments.ts and app/effects.ts: defer busy work; recover without replaying effects.
Locks do not expire while a live owner remains, avoiding unsafe time-based takeover.
"""
from __future__ import annotations

import asyncio
import contextvars
import errno
import hashlib
import inspect
import logging
import os
import threading
from contextlib import asynccontextmanager, contextmanager
from functools import wraps
from typing import get_type_hints


class WorkerOwnershipBusy(RuntimeError):
    pass


class WorkerOwnershipLost(RuntimeError):
    pass


class WorkerBackendMismatch(RuntimeError):
    pass


_current = contextvars.ContextVar("investigation_worker_owner", default=None)
_mysql_held = set()
_mysql_registry_lock = threading.Lock()


def _running_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def validate_owned_write(repository):
    owner = _current.get()
    # Awaited model/tool children inherit the fence too. Independent persisted
    # jobs reenter through their own owner rather than retaining an old context.
    if owner is not None:
        owner.check()


class _Owner:
    def __init__(self, repository, investigation_id, backend):
        self.repository = repository
        self.investigation_id = investigation_id
        self.backend = backend
        self.task = _running_task()
        self.handle = None
        self.session_id = None
        self.name = None
        self.registry_key = None
        self.valid = False
        self.lost = False
        self.pid = os.getpid()

    def acquire(self):
        repo = self.repository
        if hasattr(repo._connection, "connection"):
            with repo._lock:
                identity = repo._connection.execute("SELECT DATABASE() AS db, CONNECTION_ID() AS session_id").fetchone()
                self.session_id = identity["session_id"]
                self.name = "gta.worker." + hashlib.sha256(f"{identity['db']}:{self.investigation_id}".encode()).hexdigest()[:48]
                self.registry_key = (id(repo._connection.connection), self.name)
                with _mysql_registry_lock:
                    if self.registry_key in _mysql_held:
                        raise WorkerOwnershipBusy("another task on this session owns the investigation")
                    _mysql_held.add(self.registry_key)
                try:
                    row = repo._connection.execute("SELECT GET_LOCK(?,0) AS acquired", (self.name,)).fetchone()
                    if row["acquired"] == 0:
                        raise WorkerOwnershipBusy("another worker owns the investigation")
                    if row["acquired"] != 1:
                        raise WorkerOwnershipLost("worker ownership service unavailable")
                except BaseException:
                    with _mysql_registry_lock:
                        _mysql_held.discard(self.registry_key)
                    raise
        else:
            path = repo.path.resolve()
            directory = path.parent / (path.name + ".worker-locks")
            directory.mkdir(parents=True, exist_ok=True)
            lock_path = directory / (hashlib.sha256(self.investigation_id.encode()).hexdigest() + ".lock")
            self.handle = lock_path.open("a+b")
            try:
                if self.handle.seek(0, os.SEEK_END) == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                self.handle.close()
                self.handle = None
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise WorkerOwnershipBusy("another worker owns the investigation") from None
                raise
        self.valid = True

    def check(self):
        if not self.valid or self.pid != os.getpid():
            raise WorkerOwnershipLost("worker no longer owns this investigation")
        if self.session_id is not None:
            try:
                with self.repository._lock:
                    row = self.repository._connection.execute("SELECT IS_USED_LOCK(?) AS owner, CONNECTION_ID() AS session_id", (self.name,)).fetchone()
                if row["owner"] != self.session_id or row["session_id"] != self.session_id:
                    raise WorkerOwnershipLost("worker session no longer owns this investigation")
            except Exception as error:
                self.lost = True
                raise WorkerOwnershipLost("worker ownership cannot be verified") from error
        elif self.handle is None or self.handle.closed:
            raise WorkerOwnershipLost("worker lock handle is closed")

    def release(self):
        self.valid = False
        if self.handle is not None:
            self.handle.close()  # Never unlink: a waiter may still refer to this inode.
            self.handle = None
        if self.session_id is not None:
            try:
                with self.repository._lock:
                    row = self.repository._connection.execute("SELECT CONNECTION_ID() AS session_id").fetchone()
                    if row["session_id"] == self.session_id:
                        self.repository._connection.execute("SELECT RELEASE_LOCK(?)", (self.name,))
            except Exception:
                logging.getLogger(__name__).warning("worker lock release failed; session close releases it")
            finally:
                with _mysql_registry_lock:
                    _mysql_held.discard(self.registry_key)


class WorkerOwnership:
    def __init__(self, repository):
        self.repository = repository

    @contextmanager
    def hold_now(self, investigation_id, backend):
        """For synchronous cancellation publication: no external awaits or polling."""
        if backend not in {"local", "temporal"}:
            raise ValueError("unknown worker backend")
        existing = _current.get()
        if (existing is not None and existing.repository is self.repository
                and existing.task is _running_task() and existing.investigation_id == investigation_id):
            if existing.backend != backend:
                raise WorkerBackendMismatch("cannot nest different workflow backends")
            existing.check()
            yield existing
            return
        self.repository.get_investigation(investigation_id)
        owner = _Owner(self.repository, investigation_id, backend)
        owner.acquire()
        token = _current.set(owner)
        try:
            selected = self.repository.investigation_execution_backend(investigation_id, backend)
            if selected != backend:
                raise WorkerBackendMismatch(f"investigation belongs to {selected} backend")
            yield owner
            owner.check()
        finally:
            _current.reset(token)
            owner.release()

    @asynccontextmanager
    async def hold(self, investigation_id, backend):
        if backend not in {"local", "temporal"}:
            raise ValueError("unknown worker backend")
        existing = _current.get()
        if (existing is not None and existing.repository is self.repository
                and existing.task is asyncio.current_task() and existing.investigation_id == investigation_id):
            if existing.backend != backend:
                raise WorkerBackendMismatch("cannot nest different workflow backends")
            existing.check()
            yield existing
            return
        # Validate identity before creating a lock file or contacting the lock service.
        self.repository.get_investigation(investigation_id)
        owner = _Owner(self.repository, investigation_id, backend)
        owner.acquire()
        token = _current.set(owner)
        pulse = None

        async def watch_session():
            while True:
                await asyncio.sleep(1)
                try:
                    owner.check()
                except WorkerOwnershipLost:
                    owner.lost = True
                    owner.task.cancel()
                    return

        try:
            selected = self.repository.investigation_execution_backend(investigation_id, backend)
            if selected != backend:
                raise WorkerBackendMismatch(f"investigation belongs to {selected} backend")
            if owner.session_id is not None:
                pulse = asyncio.create_task(watch_session(), name="worker-owner-watch")
            yield owner
            owner.check()
        except asyncio.CancelledError:
            if owner.lost:
                raise WorkerOwnershipLost("worker session ownership was lost") from None
            raise
        finally:
            if pulse is not None:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
            _current.reset(token)
            owner.release()


def owned_local(function):
    @wraps(function)
    async def wrapped(self, investigation_id, *args, **kwargs):
        async with self.ownership.hold(investigation_id, "local"):
            return await function(self, investigation_id, *args, **kwargs)
    return wrapped


def owned_activity(function):
    signature = inspect.signature(function)
    identity_parameter = list(signature.parameters)[1]

    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        from temporalio.exceptions import ApplicationError
        identity = signature.bind(self, *args, **kwargs).arguments[identity_parameter]
        investigation_id = (self.repository.get_action(identity)[0].investigation_id
                            if function.__name__ == "query_action" else identity)
        try:
            async with self.ownership.hold(investigation_id, "temporal"):
                return await function(self, *args, **kwargs)
        except WorkerOwnershipBusy as error:
            if function.__name__ == "native_investigation_step":
                view = self.repository.get_investigation(investigation_id)
                return {"done": False, "status": view.status, "capacity_deferred": True, "ownership_deferred": True}
            raise ApplicationError(str(error), type="WorkerOwnershipBusy") from error
        except WorkerBackendMismatch as error:
            raise ApplicationError(str(error), type="WorkerBackendMismatch", non_retryable=True) from error
    wrapped.__annotations__ = get_type_hints(function)
    return wrapped
