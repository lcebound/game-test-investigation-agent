"""Control-turn exclusivity, crash recovery and fail-closed session ownership."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from game_test_agent.reports import ReportService
from game_test_agent.runtime.worker_ownership import (
    WorkerOwnership, WorkerOwnershipBusy, WorkerOwnershipLost, WorkerBackendMismatch,
    owned_activity,
)
from game_test_agent.storage import Repository, NotFoundError
from game_test_agent.storage.mysql_repository import MySqlRepository
from game_test_agent.workflows.local import LocalWorkflowBackend
from test_conversation_tasks import request


def test_same_database_different_connections_are_exclusive(repository):
    async def scenario():
        first, _ = repository.create_investigation(request(), "first")
        second, _ = repository.create_investigation(request(), "second")
        other = Repository(repository.path)
        try:
            async with WorkerOwnership(repository).hold(first.investigation_id, "local"):
                with pytest.raises(WorkerOwnershipBusy):
                    async with WorkerOwnership(other).hold(first.investigation_id, "local"):
                        pytest.fail("duplicate owner")
                async with WorkerOwnership(other).hold(second.investigation_id, "local"):
                    repository.record_investigation_event(first.investigation_id, "test.owner", {})
            async with WorkerOwnership(other).hold(first.investigation_id, "local"):
                pass
        finally:
            other.close()
    asyncio.run(scenario())


def test_backend_binding_survives_restart_and_rejects_mixing(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        async with WorkerOwnership(repository).hold(view.investigation_id, "temporal"):
            pass
        other = Repository(repository.path)
        try:
            with pytest.raises(WorkerBackendMismatch):
                async with WorkerOwnership(other).hold(view.investigation_id, "local"):
                    pytest.fail("changed backend")
            assert other.investigation_execution_backend(view.investigation_id, "local") == "temporal"
            rows = repository._connection.execute("SELECT * FROM investigation_events WHERE event_type='investigation.backend_selected'").fetchall()
            assert len(rows) == 1
        finally:
            other.close()
    asyncio.run(scenario())


def test_reentry_is_only_for_the_same_control_task(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        ownership = WorkerOwnership(repository)
        async with ownership.hold(view.investigation_id, "local"):
            async with ownership.hold(view.investigation_id, "local"):
                pass

            async def competitor():
                with pytest.raises(WorkerOwnershipBusy):
                    async with ownership.hold(view.investigation_id, "local"):
                        pass

            await asyncio.create_task(competitor())
            with pytest.raises(WorkerBackendMismatch):
                async with ownership.hold(view.investigation_id, "temporal"):
                    pass
            repository.record_investigation_event(view.investigation_id, "test.still_owned", {})
    asyncio.run(scenario())


def test_sync_cancellation_guard_works_without_an_event_loop(repository):
    view, _ = repository.create_investigation(request(), "view")
    with WorkerOwnership(repository).hold_now(view.investigation_id, "local"):
        repository.record_investigation_event(view.investigation_id, "test.sync", {})


def test_closed_lock_handle_cannot_write_after_another_owner_takes_over(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        other = Repository(repository.path)
        try:
            with pytest.raises(WorkerOwnershipLost):
                async with WorkerOwnership(repository).hold(view.investigation_id, "local") as owner:
                    owner.handle.close()
                    async with WorkerOwnership(other).hold(view.investigation_id, "local"):
                        other.record_investigation_event(view.investigation_id, "test.new_owner", {})
                    with pytest.raises(WorkerOwnershipLost):
                        repository.record_investigation_event(view.investigation_id, "test.stale", {})
            assert not repository._connection.execute("SELECT 1 FROM investigation_events WHERE event_type='test.stale'").fetchone()
        finally:
            other.close()
    asyncio.run(scenario())


def test_late_child_task_keeps_the_old_write_fence(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        proceed = asyncio.Event()

        async def late_result():
            await proceed.wait()
            with pytest.raises(WorkerOwnershipLost):
                repository.record_investigation_event(view.investigation_id, "test.late", {})

        async with WorkerOwnership(repository).hold(view.investigation_id, "local"):
            child = asyncio.create_task(late_result())
        proceed.set()
        await child
    asyncio.run(scenario())


def test_real_process_lock_released_after_process_termination(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        script = """
import asyncio,sys,json
sys.path[:0]=json.loads(sys.argv[4])
sys.path.insert(0,sys.argv[3])
from game_test_agent.storage import Repository
from game_test_agent.runtime.worker_ownership import WorkerOwnership
async def main():
    repo=Repository(sys.argv[1])
    async with WorkerOwnership(repo).hold(sys.argv[2],'local'):
        print('owned',flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""
        # Windows venv redirectors may have a child interpreter. Launch the base
        # interpreter with this environment's import paths so kill targets the owner.
        process = subprocess.Popen([getattr(sys, "_base_executable", sys.executable), "-c", script,
            str(repository.path), view.investigation_id,
            str(Path(__file__).parents[1] / "src"), json.dumps(sys.path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), 10) == "owned\n"
            with pytest.raises(WorkerOwnershipBusy):
                async with WorkerOwnership(repository).hold(view.investigation_id, "local"):
                    pass
            process.kill()
            await asyncio.to_thread(process.wait, 5)
            async with WorkerOwnership(repository).hold(view.investigation_id, "local"):
                repository.record_investigation_event(view.investigation_id, "test.recovered", {})
        finally:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 5)
            process.stdout.close()
            process.stderr.close()
    asyncio.run(scenario())


def backend(repository, controller=None):
    return LocalWorkflowBackend(repository, None, SimpleNamespace(model_name="disabled"), ReportService(repository), controller=controller)


def test_busy_local_worker_defers_without_model_or_failure(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        controller = SimpleNamespace(execution_mode="native_mcp", run_step=AsyncMock(), _operation_updates={})
        runner = backend(repository, controller)

        async def competing_task():
            await runner._run_guarded(view.investigation_id)

        async with WorkerOwnership(repository).hold(view.investigation_id, "local"):
            await asyncio.create_task(competing_task())
            assert view.investigation_id in runner._pending
            controller.run_step.assert_not_called()
            assert repository.get_investigation(view.investigation_id).status == "accepted"
        await runner.close()
    asyncio.run(scenario())


def test_local_does_not_fail_an_investigation_owned_by_temporal(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        async with WorkerOwnership(repository).hold(view.investigation_id, "temporal"):
            pass
        runner = backend(repository)
        await runner._run_guarded(view.investigation_id)
        assert repository.get_investigation(view.investigation_id).status == "accepted"
        with pytest.raises(NotFoundError):
            repository.get_report(view.investigation_id, 1)
        await runner.close()
    asyncio.run(scenario())


def test_cancel_waits_for_owner_and_finishes_after_release(repository):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        runner = backend(repository)

        async def cancel_from_another_task():
            runner.request_cancel(view.investigation_id)
            await runner._tasks[view.investigation_id]
            assert repository.get_investigation(view.investigation_id).status == "canceling"

        async with WorkerOwnership(repository).hold(view.investigation_id, "local"):
            await asyncio.create_task(cancel_from_another_task())
        await runner._run_guarded(view.investigation_id)
        assert repository.get_investigation(view.investigation_id).status == "canceled"
        await runner.close()
    asyncio.run(scenario())


def test_temporal_native_contention_yields_to_workflow_timer(repository, settings):
    from game_test_agent.workflows.worker import CoreActivities

    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        activities = CoreActivities(repository, settings)

        async def compete():
            result = await activities.native_investigation_step(view.investigation_id, True)
            assert not result["done"] and result["ownership_deferred"] and result["capacity_deferred"]

        async with WorkerOwnership(repository).hold(view.investigation_id, "temporal"):
            await asyncio.create_task(compete())
    asyncio.run(scenario())


def test_temporal_query_derives_investigation_from_action(repository, monkeypatch):
    async def scenario():
        view, _ = repository.create_investigation(request(), "view")
        monkeypatch.setattr(repository, "get_action", lambda action: (SimpleNamespace(investigation_id=view.investigation_id),))

        class Activities:
            def __init__(self):
                self.repository = repository
                self.ownership = WorkerOwnership(repository)

            @owned_activity
            async def query_action(self, action_id: str, poll_number: int) -> str:
                return self.repository.investigation_execution_backend(view.investigation_id, "local")

        assert await Activities().query_action("act-1", 1) == "temporal"
        assert await Activities().query_action(action_id="act-1", poll_number=2) == "temporal"
    asyncio.run(scenario())


class FakeRows:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class FakeMysqlConnection:
    def __init__(self, server, session):
        self.server = server
        self.session = session
        self.connection = object()
        self.begin_count = 0
        self.get_lock_calls = 0

    def execute(self, sql, params=()):
        if "GET_LOCK" in sql:
            self.get_lock_calls += 1
            name = params[0]
            current = self.server.setdefault("locks", {}).get(name)
            if current is not None and current != self.session:
                return FakeRows({"acquired": 0})
            self.server["locks"][name] = self.session
            return FakeRows({"acquired": 1})
        if "IS_USED_LOCK" in sql:
            return FakeRows({"owner": self.server["locks"].get(params[0]), "session_id": self.session})
        if "RELEASE_LOCK" in sql:
            if self.server["locks"].get(params[0]) == self.session:
                self.server["locks"].pop(params[0])
            return FakeRows({"released": 1})
        if "DATABASE()" in sql:
            return FakeRows({"db": "test", "session_id": self.session})
        if "CONNECTION_ID()" in sql:
            return FakeRows({"session_id": self.session})
        raise AssertionError(sql)

    def begin(self):
        self.begin_count += 1

    def commit(self):
        pass

    def rollback(self):
        pass


class FakeMysqlRepo:
    def __init__(self, server, session):
        self._lock = threading.RLock()
        self._connection = FakeMysqlConnection(server, session)
        self.server = server

    def get_investigation(self, identity):
        return SimpleNamespace(investigation_id=identity)

    def investigation_execution_backend(self, identity, backend):
        return self.server.setdefault("backends", {}).setdefault(identity, backend)


def test_mysql_distinct_sessions_are_exclusive_and_release():
    async def scenario():
        server = {}
        first, second = FakeMysqlRepo(server, 1), FakeMysqlRepo(server, 2)
        async with WorkerOwnership(first).hold("inv-1", "local"):
            with pytest.raises(WorkerOwnershipBusy):
                async with WorkerOwnership(second).hold("inv-1", "local"):
                    pass
        async with WorkerOwnership(second).hold("inv-1", "local"):
            pass
        assert not server["locks"]
    asyncio.run(scenario())


def test_mysql_same_session_cannot_reenter_from_another_task():
    async def scenario():
        repo = FakeMysqlRepo({}, 1)

        async def competing_task():
            with pytest.raises(WorkerOwnershipBusy):
                async with WorkerOwnership(repo).hold("inv-1", "local"):
                    pass

        async with WorkerOwnership(repo).hold("inv-1", "local"):
            await asyncio.create_task(competing_task())
        assert repo._connection.get_lock_calls == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("replace_session", [False, True])
def test_mysql_lost_lock_or_reconnected_session_cannot_start_transaction(replace_session):
    async def scenario():
        repo = FakeMysqlRepo({}, 1)
        with pytest.raises(WorkerOwnershipLost):
            async with WorkerOwnership(repo).hold("inv-1", "local") as owner:
                repo.server["locks"][owner.name] = 99
                if replace_session:
                    repo._connection.session = 2
                with pytest.raises(WorkerOwnershipLost):
                    with MySqlRepository.transaction(repo):
                        pytest.fail("stale write")
                assert repo._connection.begin_count == 0
        assert repo.server["locks"][owner.name] == 99
    asyncio.run(scenario())


def test_mysql_watch_cancels_a_waiting_owner_after_session_loss():
    async def scenario():
        repo = FakeMysqlRepo({}, 1)
        with pytest.raises(WorkerOwnershipLost):
            async with WorkerOwnership(repo).hold("inv-1", "local") as owner:
                repo.server["locks"].pop(owner.name)
                await asyncio.wait_for(asyncio.Event().wait(), 3)
    asyncio.run(scenario())


@pytest.mark.parametrize("answer,exception", [(0, WorkerOwnershipBusy), (None, WorkerOwnershipLost)])
def test_mysql_lock_failure_does_not_leave_a_reentrant_claim(answer, exception):
    async def scenario():
        repo = FakeMysqlRepo({}, 1)
        execute = repo._connection.execute

        def reject(sql, params=()):
            return FakeRows({"acquired": answer}) if "GET_LOCK" in sql else execute(sql, params)

        repo._connection.execute = reject
        with pytest.raises(exception):
            async with WorkerOwnership(repo).hold("inv-1", "local"):
                pass
        repo._connection.execute = execute
        async with WorkerOwnership(repo).hold("inv-1", "local"):
            pass
    asyncio.run(scenario())


def test_temporal_typed_contention_is_retryable(repository):
    from temporalio.exceptions import ApplicationError

    async def scenario():
        view, _ = repository.create_investigation(request(), "view")

        class Activities:
            def __init__(self):
                self.repository = repository
                self.ownership = WorkerOwnership(repository)

            @owned_activity
            async def project_status(self, investigation_id: str, status: str) -> None:
                pytest.fail("duplicate activity entered its body")

        async def compete():
            with pytest.raises(ApplicationError) as error:
                await Activities().project_status(investigation_id=view.investigation_id, status="planning")
            assert error.value.type == "WorkerOwnershipBusy" and not error.value.non_retryable

        async with WorkerOwnership(repository).hold(view.investigation_id, "temporal"):
            await asyncio.create_task(compete())
    asyncio.run(scenario())
