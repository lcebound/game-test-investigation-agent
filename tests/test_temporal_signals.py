from __future__ import annotations

import asyncio
import importlib

import pytest

pytest.importorskip("temporalio")
InvestigationWorkflow = importlib.import_module(
    "game_test_agent.workflows.temporal"
).InvestigationWorkflow
WorkflowRunInput = importlib.import_module("game_test_agent.workflows.temporal").WorkflowRunInput


def test_signals_before_run_initialization_are_preserved_and_deduplicated():
    async def scenario() -> None:
        instance = InvestigationWorkflow()

        await instance.receive_input({"command_id": "cmd-input", "payload": {}})
        await instance.receive_input({"command_id": "cmd-input", "payload": {}})
        await instance.evidence_updated({"command_id": "cmd-evidence", "payload": {}})
        await instance.evidence_updated({"command_id": "cmd-evidence", "payload": {}})
        await instance.cancel("cmd-cancel")
        await instance.cancel("cmd-cancel")

        snapshot = instance.state_snapshot()
        assert snapshot == {
            "investigation_id": "",
            "canceled": True,
            "input_revision": 1,
            "evidence_revision": 1,
            "processed_signal_count": 3,
        }
        # The run method now mutates only this field instead of replacing state.
        instance.state.investigation_id = "inv-early-signals"
        assert instance.state_snapshot()["input_revision"] == 1
        assert instance.state_snapshot()["investigation_id"] == "inv-early-signals"

    asyncio.run(scenario())


def test_continue_as_new_input_restores_budget_watermarks_and_early_signals():
    async def scenario() -> None:
        instance = InvestigationWorkflow()
        await instance.receive_input({"command_id": "early-input", "payload": {}})
        restored = instance._restore_input(
            WorkflowRunInput(
                investigation_id="inv-continued",
                next_plan_revision=6,
                deadline_epoch=1_800_000_000,
                input_revision=3,
                evidence_revision=2,
                processed_signal_ids=["old-1", "old-2"],
                continued=True,
            )
        )

        assert restored.next_plan_revision == 6
        assert instance.state.input_revision == 4
        assert instance.state.evidence_revision == 2
        assert instance.state.processed_signal_ids == {"old-1", "old-2", "early-input"}

    asyncio.run(scenario())
