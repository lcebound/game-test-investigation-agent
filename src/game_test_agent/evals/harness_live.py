"""Real-model, fixed-evidence comparisons. No platform, game or code runner is connected."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace
import time
import uuid

from jsonschema import Draft202012Validator
from mcp import types

from game_test_agent.config import Settings
from game_test_agent.contracts.models import InvestigationRequest, InputCommand
from game_test_agent.local_config import load_local_environment
from game_test_agent.mcp_gateway.config import McpProviderConfig
from game_test_agent.reports import ReportService
from game_test_agent.storage import Repository, LocalArtifactStore
from game_test_agent.storage.repository import payload_hash, canonical_json
from game_test_agent.workflows.native_mcp import NativeMcpController
from game_test_agent.runtime.worker_ownership import WorkerOwnership


class FixedEvidenceProvider:
    provider_id = "fixed-evidence"

    def __init__(self, case):
        self.case = case
        self.config = McpProviderConfig(
            provider_id=self.provider_id,
            url="http://127.0.0.1:1/mcp",
            project_ids=["harness-eval"],
            allow_insecure_localhost=True,
            native_source_fields={"read_fixture": {"build_id": "/build"}},
        )
        self.tool = types.Tool(
            name="read_fixture",
            description="Read the saved JSON observation for this investigation. It is offline replay data, not a new real run.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            annotations=types.ToolAnnotations(read_only_hint=True),
        )

    async def native_tools(self):
        return [self.tool]

    async def call_native(self, tool, arguments):
        Draft202012Validator(self.tool.input_schema).validate(arguments)
        return types.CallToolResult(content=[], structured_content=self.case["data"])


async def run_case(case, variant, settings, directory):
    identity = case["case_id"] + "-" + variant
    directory.mkdir(parents=True, exist_ok=True)
    repo = Repository(directory / (identity + "-" + uuid.uuid4().hex[:8] + ".db"))
    artifacts = LocalArtifactStore(directory / "artifacts")
    selected = replace(
        settings,
        enable_model=True,
        mcp_native_tools=True,
        mcp_providers_json="[]",
        model_context_window_tokens=32768 if variant == "standard" else 12000,
        model_max_output_tokens=1600,
        model_max_total_tokens=60000,
        model_timeout_seconds=60,
    )
    provider = FixedEvidenceProvider(case)
    controller = NativeMcpController(
        selected,
        repo,
        SimpleNamespace(providers={provider.provider_id: provider}),
        artifacts,
        ReportService(repo, artifacts),
    )
    view, _ = repo.create_investigation(
        InvestigationRequest(
            tenant_id="harness-validation",
            project_id="harness-eval",
            objective={
                "objective_id": case["case_id"],
                "description": case["question"]
                + " 这是离线固定证据评测。先制定问题计划，再读取read_fixture工具，基于原文引用交付有限报告。不得声称做过真实运行或确认根因。",
            },
            budget={
                "max_rounds": 6,
                "max_real_runs": 0,
                "max_tool_reads": 12,
                "max_device_minutes": 0,
                "max_wall_seconds": 240,
            },
        ),
        identity,
        principal_id="offline-eval",
        scopes={"runs:read", "evidence:read"},
    )
    for index in range(7):
        repo.enqueue_command(
            view.investigation_id,
            "input-" + str(index),
            "input",
            InputCommand(
                command_id="input-" + str(index),
                expected_revision=view.revision,
                content="早期约束：不得把局部指标改善或单次通过当作独立恢复、修复或根因确认。"
                if index == 0
                else f"补充{index}：保留构建与证据来源，未知范围需要说明。",
            ),
        )
    started = time.perf_counter()
    try:
        async with WorkerOwnership(repo).hold(view.investigation_id, "local"):
            await controller.run(
                view.investigation_id,
                principal_id="offline-eval",
                scopes={"runs:read", "evidence:read"},
            )
        report, _, report_sha = repo.get_latest_report(view.investigation_id)
        rendered = canonical_json(report)
        usage = repo.native_model_usage(view.investigation_id)
        reads = repo.list_recent_events_by_type(
            view.investigation_id, "mcp.native.tool_completed", limit=100
        )
        supported_claims = []
        for claim in report.claims:
            if not claim.supporting_excerpt or not claim.evidence_ids:
                continue
            quote = " ".join(claim.supporting_excerpt.split())
            for record in repo.list_current_evidence(view.investigation_id):
                if record.evidence_id in claim.evidence_ids:
                    original = " ".join(
                        artifacts.read_bytes(record.artifact_ref)
                        .decode("utf-8", errors="replace")
                        .split()
                    )
                    if quote and quote in original:
                        supported_claims.append(claim.claim_id)
                        break
        reads_count = int(repo.budget_usage(view.investigation_id).get("tool_reads", 0))
        checks = {
            "source_read": any(row["data"].get("tool_name") == "read_fixture" for row in reads),
            "report_has_citations": bool(supported_claims),
            "bounded_conclusion": report.conclusion_level
            not in {"root_cause_confirmed", "bottleneck_supported"},
            "labeled_counterevidence_retained": all(
                any(term in rendered for term in alternatives)
                for alternatives in case["expected_terms"]
            ),
            "no_real_runs": not repo.budget_usage(view.investigation_id).get("real_runs", 0),
        }
        (directory / (identity + "-report.json")).write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        return {
            "case_id": case["case_id"],
            "variant": variant,
            "fixture_sha256": payload_hash(case),
            "model": selected.model_name,
            "checks": checks,
            "passed": all(checks.values()),
            "tokens": usage["tokens"],
            "factual_metrics": {
                "quoted_claims_supported_by_original": len(supported_claims),
                "total_claims": len(report.claims),
                "tool_reads": reads_count,
                "reused_reads": len(
                    repo.list_recent_events_by_type(
                        view.investigation_id, "mcp.native.tool_reused", limit=100
                    )
                ),
                "budget_limited_report": report.stop_reason
                in {"native_round_budget_exhausted", "wall_clock_budget_exhausted"},
            },
            "seconds": round(time.perf_counter() - started, 3),
            "estimated_cost": usage["estimated_cost"]
            if selected.model_cost_per_input_token and selected.model_cost_per_output_token
            else None,
            "report_sha256": report_sha,
            "stop_reason": report.stop_reason,
            "limitation": "Keyword labels are retention probes, not independent semantic adjudication or causal accuracy.",
        }
    except Exception as error:
        return {
            "case_id": case["case_id"],
            "variant": variant,
            "passed": False,
            "error_type": type(error).__name__,
            "seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        await controller.close()
        repo.close()


def compare_results(current, baseline):
    """Only compare identical fixtures, model and context variants; no causal claim."""
    old = {
        (row["case_id"], row["variant"], row.get("model"), row.get("fixture_sha256")): row
        for row in baseline.get("rows", [])
    }
    compared = []
    for row in current:
        prior = old.get(
            (row["case_id"], row["variant"], row.get("model"), row.get("fixture_sha256"))
        )
        if prior is None or not all(key in row and key in prior for key in ("tokens", "seconds")):
            continue
        compared.append(
            {
                "case_id": row["case_id"],
                "variant": row["variant"],
                "token_delta": row["tokens"] - prior["tokens"],
                "seconds_delta": round(row["seconds"] - prior["seconds"], 3),
                "current_retention_passed": row["passed"],
                "prior_retention_passed": prior["passed"],
            }
        )
    return {
        "matched_rows": compared,
        "limitation": "Single-run observations, not statistical improvement or independent semantic accuracy.",
    }


def apply_adjudications(rows, labels):
    """Keep independent semantic review separate from deterministic retention checks."""
    for row in rows:
        row["semantic_adjudication"] = None
        key = row["case_id"] + "/" + row["variant"]
        label = labels.get(key)
        if label is None:
            continue
        if label.get("report_sha256") != row.get("report_sha256") or label.get(
            "fixture_sha256"
        ) != row.get("fixture_sha256"):
            raise ValueError("adjudication is not pinned to this exact fixture and report")
        coverage = label.get("question_coverage")
        errors = label.get("unsupported_causal_claims")
        if (
            not isinstance(coverage, (int, float))
            or isinstance(coverage, bool)
            or not 0 <= coverage <= 1
            or type(errors) is not int
            or errors < 0
        ):
            raise ValueError("invalid semantic review scores")
        if not isinstance(label.get("reviewer"), str) or not label["reviewer"].strip():
            raise ValueError("semantic adjudication requires a reviewer identity")
        row["semantic_adjudication"] = label


async def main(args):
    load_local_environment()
    if not os.getenv("OPENAI_API_KEY"):
        raise ValueError("configured model key is missing")
    settings = Settings.from_env()
    cases = json.loads(args.fixtures.read_text(encoding="utf-8"))
    if args.case:
        cases = [case for case in cases if case["case_id"] == args.case]
    if not cases or len(cases) > 10:
        raise ValueError("select 1–10 fixed cases")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results = []
    labels = (
        json.loads(args.adjudications.read_text(encoding="utf-8")) if args.adjudications else {}
    )
    baseline = json.loads(args.baseline.read_text(encoding="utf-8")) if args.baseline else {}
    for case in cases:
        for variant in (args.variant,) if args.variant else ("standard", "compact"):
            row = await run_case(case, variant, settings, args.output.parent)
            results.append(row)
            apply_adjudications(results, labels)
            args.output.write_text(
                json.dumps(
                    {
                        "rows": results,
                        "baseline_comparison": compare_results(results, baseline),
                        "comparison": "Same current harness, two context budgets; not a pre-migration baseline.",
                        "max_total_token_reservations": 60000
                        * len(cases)
                        * (1 if args.variant else 2),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(case["case_id"], variant, "passed" if row["passed"] else "failed", flush=True)
    return 0 if all(row["passed"] for row in results) else 1


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", type=Path, default=Path("evals/harness-fixtures.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--variant", choices=["standard", "compact"])
    raise SystemExit(asyncio.run(main(parser.parse_args())))


if __name__ == "__main__":
    cli()
