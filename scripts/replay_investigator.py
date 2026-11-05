"""Read-only model replay over an existing investigation's authoritative ledger."""

from __future__ import annotations

import argparse
import asyncio

from game_test_agent.agents import Investigator
from game_test_agent.capabilities import CapabilityRegistry
from game_test_agent.config import Settings
from game_test_agent.evidence import ContextBuilder
from game_test_agent.storage import create_repository


async def replay(investigation_id: str) -> None:
    settings = Settings.from_env()
    if not settings.enable_model:
        raise ValueError("GTA_ENABLE_MODEL must be true for model replay")
    repository = create_repository(settings)
    try:
        registry = CapabilityRegistry(repository)
        context = ContextBuilder(
            repository,
            registry,
            window_limit_tokens=settings.model_context_window_tokens,
            reserved_output_tokens=settings.model_max_output_tokens,
        ).build(investigation_id, scopes={"runs:submit"})
        investigator = Investigator(
            settings.model_name,
            enabled=True,
            model_api=settings.model_api,
            output_mode=settings.model_output_mode,
            timeout_seconds=settings.model_timeout_seconds,
            max_output_tokens=settings.model_max_output_tokens,
            reasoning_effort=settings.model_reasoning_effort,
        )
        decision = await investigator.reason_next_step(
            context.model_dump(mode="json", by_alias=True)
        )
        print(decision.model_dump_json(indent=2), flush=True)
    finally:
        repository.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--investigation", required=True)
    args = parser.parse_args()
    asyncio.run(replay(args.investigation))
