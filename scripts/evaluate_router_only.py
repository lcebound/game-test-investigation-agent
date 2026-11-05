"""Evaluate only Router decisions; no API app, dispatcher, MCP or Worker is started."""

from __future__ import annotations
import argparse
import asyncio
from collections import defaultdict
from dataclasses import replace
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from game_test_agent.config import Settings
from game_test_agent.contracts.models import InvestigationRequest
from game_test_agent.events.semantic_router import RouterModelAssessor, SemanticConversationRouter
from game_test_agent.local_config import load_local_environment
from game_test_agent.runtime.execution import ExecutionLimits
from game_test_agent.storage import Repository


class Assessor:
    def __init__(self, delegate):
        self.delegate = delegate
        self.profile = delegate.profile
        self.settings = delegate.settings
        self.calls = 0
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.last = None

    async def assess(self, payload, scope):
        self.calls += 1
        result = await self.delegate.assess(payload, scope)
        self.last = result.output.model_dump(mode="json")
        for key in self.usage:
            self.usage[key] += getattr(result.usage, key)
        return result


def fixture(repo, case, serial):
    def create(key, text, root=None, tenant="router-eval", project="fixtures"):
        return repo.create_investigation(
            InvestigationRequest(
                tenant_id=tenant,
                project_id=project,
                objective={"objective_id": "routing-only", "description": text},
                budget={"max_real_runs": 0, "max_wall_seconds": 3600},
            ),
            f"{serial}-{key}",
            conversation_root_id=root,
        )[0]

    root = create("perf", "战斗场景 CPU 帧耗时异常")
    views = {"perf": root}
    targets = [("login", "登录闪退"), ("memory", "内存持续增长与泄漏")]
    random.Random(serial).shuffle(targets)
    for name, title in targets:
        views[name] = create(name, title, root.investigation_id)
    if case.get("scenario") == "duplicate":
        create("duplicate", "登录闪退", root.investigation_id)
    if case.get("scenario") == "overflow":
        for index in range(19):
            create(str(index), f"独立任务 {index}", root.investigation_id)
    views["foreign"] = create(
        "foreign", "其他项目的登录闪退", tenant="different", project="different"
    )
    return root, views


def score(case, decision, views):
    if decision["kind"] != case["expected_kind"]:
        return False
    return (
        decision["kind"] != "task"
        or decision.get("task_id") == views[case["expected_target"]].investigation_id
    )


def summarize(rows):
    summary = {}
    for mode in ("rules", "semantic"):
        selected = [row for row in rows if row["mode"] == mode]
        categories = defaultdict(lambda: {"total": 0, "correct": 0})
        for row in selected:
            categories[row["category"]]["total"] += 1
            categories[row["category"]]["correct"] += row["correct"]
        summary[mode] = {
            "total": len(selected),
            "correct": sum(row["correct"] for row in selected),
            "accuracy": sum(row["correct"] for row in selected) / max(1, len(selected)),
            "wrong_task_deliveries": sum(
                not row["correct"] and row["decision"]["kind"] == "task" for row in selected
            ),
            "wrong_new_task": sum(
                not row["correct"] and row["decision"]["kind"] == "new_task" for row in selected
            ),
            "clarifications": sum(row["decision"]["kind"] == "clarify" for row in selected),
            "unnecessary_clarifications": sum(
                not row["correct"] and row["decision"]["kind"] == "clarify" for row in selected
            ),
            "model_calls": sum(row["model_calls"] for row in selected),
            "assessment_unavailable": sum(
                row["decision"].get("basis") == "assessment_unavailable" for row in selected
            ),
            "categories": dict(categories),
        }
    return summary


async def main(args):
    load_local_environment()
    configured = Settings.from_env()
    settings = replace(configured, enable_model=True, enable_semantic_router=True)
    cases = json.loads(args.fixtures.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    limits = ExecutionLimits(models=2)
    semaphore = asyncio.Semaphore(2)
    rows = []

    async def run(case, repeat, serial):
        async with semaphore:
            repo = Repository(args.output.parent / (f"case-{serial}.db"))
            try:
                root, views = fixture(repo, case, serial)
                message = case["message"].format(
                    **{key: value.investigation_id for key, value in views.items()}
                )
                context = case.get("context", "perf")
                kwargs = {
                    "context_investigation_id": views[context].investigation_id
                    if context
                    else None,
                    "excluded_task_ids": frozenset(
                        views[key].investigation_id for key in case.get("excluded", [])
                    ),
                    "allow_only_task": case.get("allow_only_task", True),
                }
                assessor = Assessor(RouterModelAssessor(settings, limits))
                router = SemanticConversationRouter(
                    repo, assessor, timeout_seconds=settings.router_timeout_seconds
                )
                for mode in ("rules", "semantic"):
                    started = time.perf_counter()
                    decision = (
                        router.rules.resolve(
                            root.investigation_id, message, "router-eval", "fixtures", **kwargs
                        )
                        if mode == "rules"
                        else await router.resolve(
                            root.investigation_id, message, "router-eval", "fixtures", **kwargs
                        )
                    )
                    rows.append(
                        {
                            "case_id": case["case_id"],
                            "category": case["category"],
                            "repeat": repeat,
                            "mode": mode,
                            "message": case["message"],
                            "expected_kind": case["expected_kind"],
                            "expected_target": case.get("expected_target"),
                            "actual_target": next(
                                (
                                    key
                                    for key, value in views.items()
                                    if value.investigation_id == decision.get("task_id")
                                ),
                                None,
                            ),
                            "correct": score(case, decision, views),
                            "decision": decision,
                            "seconds": round(time.perf_counter() - started, 3),
                            "model_calls": assessor.calls if mode == "semantic" else 0,
                            "usage": dict(assessor.usage) if mode == "semantic" else {},
                            "model_proposal": assessor.last if mode == "semantic" else None,
                        }
                    )
                # A queued fixture is not executed: no model turns, actions, analysis jobs or operations.
                assert (
                    repo._connection.execute(
                        "SELECT COUNT(*) FROM investigation_actions"
                    ).fetchone()[0]
                    == 0
                )
                assert (
                    repo._connection.execute("SELECT COUNT(*) FROM native_tool_jobs").fetchone()[0]
                    == 0
                )
                for view in views.values():
                    usage = repo.budget_usage(view.investigation_id)
                    assert not any(
                        usage.get(key, 0)
                        for key in ("real_runs", "native_model_turns", "tool_reads")
                    )
                    assert repo.get_investigation(view.investigation_id).status == "accepted"
                result = {
                    "model": settings.model_name,
                    "configured_semantic_router": configured.enable_semantic_router,
                    "router_timeout_seconds": settings.router_timeout_seconds,
                    "repeats": args.repeats,
                    "rows": sorted(
                        rows, key=lambda row: (row["repeat"], row["case_id"], row["mode"])
                    ),
                    "summary": summarize(rows),
                    "worker_executions": 0,
                    "platform_calls": 0,
                    "limitation": "Hand-labeled synthetic routing inputs, not production traffic. Mixed-intent cases are labeled clarify because no single destination preserves both intents.",
                }
                args.output.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(
                    case["case_id"], repeat, "ok" if rows[-1]["correct"] else "mismatch", flush=True
                )
            finally:
                repo.close()

    await asyncio.gather(
        *(
            run(case, repeat, index + repeat * len(cases))
            for repeat in range(args.repeats)
            for index, case in enumerate(cases)
        )
    )
    print(json.dumps(summarize(rows)), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=Path("evals/router-routing-cases.json"))
    parser.add_argument("--repeats", type=int, choices=range(1, 4), default=2)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
