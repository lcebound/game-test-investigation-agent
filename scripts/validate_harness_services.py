"""Isolated real MySQL/Temporal validation. Never touches a game or production database."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from game_test_agent.config import Settings
from game_test_agent.contracts.models import InvestigationRequest, TestObjective
from game_test_agent.storage import Repository
from game_test_agent.storage.mysql_repository import connect_mysql_repository
from game_test_agent.runtime.worker_ownership import (
    WorkerOwnership,
    WorkerOwnershipBusy,
    WorkerOwnershipLost,
)


def request():
    return InvestigationRequest(
        tenant_id="harness-validation",
        project_id="offline-fixtures",
        objective=TestObjective(
            objective_id="gap", description="Summarize the missing evidence; do not run a game."
        ),
        budget={
            "max_rounds": 2,
            "max_real_runs": 0,
            "max_tool_reads": 8,
            "max_device_minutes": 0,
            "max_wall_seconds": 90,
        },
    )


async def mysql_check(config):
    values = json.loads(config.read_text(encoding="utf-8"))

    database = values.get("mysql_database", "gta_harness")
    port = int(values.get("mysql_port", 3418))
    if not re.fullmatch(r"gta_[a-z0-9_]{1,50}", database) or not 1 <= port <= 65535:
        raise ValueError("only an explicitly named isolated gta_ test database is supported")

    def connect():
        return connect_mysql_repository(
            host="127.0.0.1",
            port=port,
            database=database,
            user="root",
            password=values["mysql_password"],
            ssl_ca=None,
        )

    first, second = connect(), connect()
    try:
        view, _ = first.create_investigation(request(), uuid.uuid4().hex)
        with_lost = False
        try:
            async with WorkerOwnership(first).hold(view.investigation_id, "local") as owner:
                try:
                    async with WorkerOwnership(second).hold(view.investigation_id, "local"):
                        raise AssertionError("duplicate MySQL owner")
                except WorkerOwnershipBusy:
                    pass
                second._connection.execute("KILL CONNECTION ?", (owner.session_id,))
                try:
                    first.record_investigation_event(
                        view.investigation_id, "validation.stale_write", {}
                    )
                except WorkerOwnershipLost:
                    with_lost = True
        except WorkerOwnershipLost:
            pass
        assert with_lost
        async with WorkerOwnership(second).hold(view.investigation_id, "local"):
            second.record_investigation_event(view.investigation_id, "validation.recovered", {})
        assert not second._connection.execute(
            "SELECT 1 FROM investigation_events WHERE investigation_id=? AND event_type='validation.stale_write'",
            (view.investigation_id,),
        ).fetchone()
        from types import SimpleNamespace
        from game_test_agent.contracts.models import ModelUsage
        from game_test_agent.events.observation_router import ObservationBatchRouter
        from game_test_agent.events.semantic_router import (
            SemanticConversationRouter,
            SemanticAddress,
        )
        from game_test_agent.storage.feedback import FeedbackStore

        root, _ = second.create_investigation(request(), uuid.uuid4().hex)
        sibling, _ = second.create_investigation(
            request(), uuid.uuid4().hex, conversation_root_id=root.investigation_id
        )

        class Assessor:
            async def assess(self, packet, scope):
                return SimpleNamespace(
                    output=SemanticAddress(
                        kind="task",
                        task_id=sibling.investigation_id,
                        reference="login investigation",
                    ),
                    usage=ModelUsage(input_tokens=0, output_tokens=0),
                )

        observations = ObservationBatchRouter(
            second, SemanticConversationRouter(second, Assessor())
        )
        for index, kind in enumerate(("failed", "recovered", "counterevidence")):
            observations.accept(
                {
                    "event_type": "game_test.observation",
                    "event_id": uuid.uuid4().hex,
                    "source": "validation",
                    "tenant_id": root.request.tenant_id,
                    "project_id": root.request.project_id,
                    "conversation_id": root.investigation_id,
                    "thread_key": "isolated-monitor",
                    "kind": kind,
                    "summary": "Back to the login investigation",
                    "details": {"index": index},
                }
            )
        counts = await observations.recover_due()
        assert counts["routed"] == 3
        feedback = FeedbackStore(second)
        feedback.prepare_batches()
        feedback.prepare_batches()  # Restart/retry does not duplicate the sealed batch.
        packet = feedback.inline(sibling.investigation_id)
        assert {item["kind"] for item in packet["items"]} >= {
            "failed",
            "recovered",
            "counterevidence",
        }
        assert not second.list_user_inputs(sibling.investigation_id)
        return {
            "passed": True,
            "schema_version": second.schema_version(),
            "scenarios": [
                "exclusive_session",
                "killed_connection_fence",
                "recovery",
                "observation_batch_delivery",
                "feedback_retry_idempotence",
            ],
        }
    finally:
        first.close()
        second.close()


async def temporal_check(address, directory):
    from temporalio.client import Client
    from temporalio.worker import Worker, Replayer
    from temporalio import activity
    from datetime import timedelta
    from game_test_agent.runtime.worker_ownership import owned_activity
    from game_test_agent.workflows.worker import CoreActivities
    from game_test_agent.workflows.temporal import InvestigationWorkflow, CleanupWorkflow

    directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_path=directory / (uuid.uuid4().hex + ".db"),
        artifact_root=directory / "artifacts",
        workflow_backend="temporal",
        enable_model=False,
    )
    repo = Repository(settings.database_path)
    activities = CoreActivities(repo, settings)
    client = await Client.connect(address)
    queue = "harness-validation-" + uuid.uuid4().hex
    names = [
        "project_status",
        "load_control_state",
        "native_investigation_step",
        "discover_capabilities",
        "reason_next_step",
        "execute_readonly_decision",
        "plan_and_submit_action",
        "query_action",
        "ingest_run_evidence",
        "evaluate_objective",
        "publish_inconclusive_report",
        "cancel_investigation",
        "reconcile_cleanup",
    ]
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[InvestigationWorkflow, CleanupWorkflow],
            activities=[getattr(activities, name) for name in names],
        ):
            view, _ = repo.create_investigation(request(), uuid.uuid4().hex)
            handle = await client.start_workflow(
                InvestigationWorkflow.run,
                view.investigation_id,
                id=view.workflow_id,
                task_queue=queue,
            )
            result = await asyncio.wait_for(handle.result(), 100)
            history = await handle.fetch_history()
        await Replayer(workflows=[InvestigationWorkflow, CleanupWorkflow]).replay_workflow(history)
        (directory / "completed-history.json").write_text(history.to_json(), encoding="utf-8")
        report = repo.get_latest_report(view.investigation_id)[0]
        assert report.investigation_id == view.investigation_id

        class PausingActivities(CoreActivities):
            @activity.defn(name="discover_capabilities")
            @owned_activity
            async def discover_capabilities(self, investigation_id: str) -> list[dict]:
                self.entered.set()
                await self.release.wait()
                return await CoreActivities.discover_capabilities.__wrapped__(
                    self, investigation_id
                )

        pausing = PausingActivities(repo, settings)
        pausing.entered, pausing.release = asyncio.Event(), asyncio.Event()
        restarted, _ = repo.create_investigation(request(), uuid.uuid4().hex)
        async with Worker(
            client,
            task_queue=queue,
            workflows=[InvestigationWorkflow, CleanupWorkflow],
            activities=[getattr(pausing, name) for name in names],
            graceful_shutdown_timeout=timedelta(seconds=0),
        ):
            recovery_handle = await client.start_workflow(
                InvestigationWorkflow.run,
                restarted.investigation_id,
                id=restarted.workflow_id,
                task_queue=queue,
            )
            await asyncio.wait_for(pausing.entered.wait(), 15)
        async with Worker(
            client,
            task_queue=queue,
            workflows=[InvestigationWorkflow, CleanupWorkflow],
            activities=[getattr(activities, name) for name in names],
        ):
            await asyncio.wait_for(recovery_handle.result(), 90)
        recovered_history = await recovery_handle.fetch_history()
        await Replayer(workflows=[InvestigationWorkflow, CleanupWorkflow]).replay_workflow(
            recovered_history
        )
        (directory / "restart-history.json").write_text(
            recovered_history.to_json(), encoding="utf-8"
        )
        await pausing.providers.close()

        pausing = PausingActivities(repo, settings)
        pausing.entered, pausing.release = asyncio.Event(), asyncio.Event()
        canceled, _ = repo.create_investigation(request(), uuid.uuid4().hex)
        async with Worker(
            client,
            task_queue=queue,
            workflows=[InvestigationWorkflow, CleanupWorkflow],
            activities=[getattr(pausing, name) for name in names],
        ):
            cancel_handle = await client.start_workflow(
                InvestigationWorkflow.run,
                canceled.investigation_id,
                id=canceled.workflow_id,
                task_queue=queue,
            )
            await asyncio.wait_for(pausing.entered.wait(), 15)
            await cancel_handle.signal(InvestigationWorkflow.cancel, "validation-cancel")
            pausing.release.set()
            await asyncio.wait_for(cancel_handle.result(), 90)
        canceled_history = await cancel_handle.fetch_history()
        await Replayer(workflows=[InvestigationWorkflow, CleanupWorkflow]).replay_workflow(
            canceled_history
        )
        assert repo.get_latest_report(canceled.investigation_id)[0].status == "canceled"
        (directory / "cancel-history.json").write_text(canceled_history.to_json(), encoding="utf-8")
        await pausing.providers.close()
        return {
            "passed": True,
            "result": result,
            "report_status": report.status,
            "replay_passed": True,
            "worker_restart_recovered": True,
            "cancel_replayed": True,
            "real_runs": repo.budget_usage(view.investigation_id).get("real_runs", 0),
        }
    finally:
        await activities.providers.close()
        repo.close()


async def main(args):
    results = {}
    for name, operation in (
        ("mysql", mysql_check(args.mysql_config)),
        ("temporal", temporal_check(args.temporal_address, args.output.parent / "temporal")),
    ):
        try:
            results[name] = await operation
        except Exception as error:
            import traceback

            results[name] = {"passed": False, "error_type": type(error).__name__}
            (args.output.parent / (name + "-failure.txt")).write_text(
                traceback.format_exc(), encoding="utf-8"
            )
        print(
            name,
            "passed" if results[name]["passed"] else "failed: " + results[name]["error_type"],
            flush=True,
        )
    args.output.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(row["passed"] for row in results.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mysql-config", type=Path, required=True)
    parser.add_argument("--temporal-address", default="127.0.0.1:7248")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    raise SystemExit(asyncio.run(main(args)))
