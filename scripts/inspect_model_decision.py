"""Read-only replay of a frozen reasoning context with safe output diagnostics.

This does not create investigations, actions, or provider runs. It prints only
JSON shape, selected routing fields, validation locations, and an output hash.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from game_test_agent.agents.investigator import Investigator
from game_test_agent.config import Settings
from game_test_agent.contracts.models import ContextSnapshot


def load_context(database: Path, investigation_id: str, round_number: int) -> dict:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute(
            """SELECT snapshot_json FROM context_snapshots
               WHERE investigation_id=? AND plan_revision=?""",
            (investigation_id, round_number),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("frozen context snapshot not found")
    return ContextSnapshot.model_validate_json(row[0]).context.model_dump(
        mode="json", by_alias=True
    )


async def inspect(context: dict, settings: Settings) -> None:
    from agents import Runner

    original_run = Runner.run

    async def observed_run(*args, **kwargs):
        result = await original_run(*args, **kwargs)
        raw = result.final_output
        if not isinstance(raw, str):
            print(json.dumps({"output_type": type(raw).__name__}, sort_keys=True))
            return result
        shape: dict = {
            "output_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "output_length": len(raw),
        }
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            shape["json_error"] = error.msg
        else:
            shape["root_type"] = type(parsed).__name__
            if isinstance(parsed, dict):
                shape["keys"] = sorted(parsed)
                shape["kind"] = parsed.get("kind")
                shape["objective_id"] = parsed.get("objective_id")
                shape["capability_id"] = parsed.get("capability_id")
                shape["provider_id"] = parsed.get("provider_id")
                shape["expected_observation_type"] = type(
                    parsed.get("expected_observation")
                ).__name__
                shape["reason_summary_type"] = type(parsed.get("reason_summary")).__name__
                shape["missing_fields_type"] = type(parsed.get("missing_fields")).__name__
        print(json.dumps(shape, ensure_ascii=False, sort_keys=True), flush=True)
        return result

    Runner.run = observed_run
    try:
        investigator = Investigator(
            settings.model_name,
            enabled=True,
            model_api=settings.model_api,
            output_mode=settings.model_output_mode,
            timeout_seconds=settings.model_timeout_seconds,
            max_output_tokens=settings.model_max_output_tokens,
            reasoning_effort=settings.model_reasoning_effort,
            extra_body=settings.model_extra_body,
        )
        decision = await investigator.reason_next_step(context)
        print(json.dumps({"decision_kind": decision.kind}, sort_keys=True))
    except ValidationError as error:
        print(
            json.dumps(
                {
                    "validation_errors": [
                        {"loc": list(item["loc"]), "type": item["type"]}
                        for item in error.errors(include_input=False)
                    ]
                },
                sort_keys=True,
            )
        )
    finally:
        Runner.run = original_run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("investigation_id")
    parser.add_argument("round_number", type=int)
    args = parser.parse_args()
    context = load_context(args.database, args.investigation_id, args.round_number)
    asyncio.run(inspect(context, Settings.from_env()))


if __name__ == "__main__":
    main()
