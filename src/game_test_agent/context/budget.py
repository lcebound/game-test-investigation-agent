from __future__ import annotations

from game_test_agent.contracts.models import ContextBundle
from game_test_agent.storage import canonical_json


def fit_final_context(context: ContextBundle) -> ContextBundle:
    """Account for the serialized final bundle, including packs and its manifest.

    Only optional material is removed. Mandatory overflow stays explicit and the
    kernel refuses to send it to the model.
    """
    result = context.model_copy(deep=True)
    usage = result.context_window
    available = usage.window_limit_tokens - usage.reserved_output_tokens
    omitted = dict(usage.omitted_records)
    optional_names = (
        "project_pack",
        "summary",
        "historical_cases",
        "excerpts",
        "actions",
        "hypotheses",
        "evidence",
        "facts",
    )

    def estimate() -> int:
        return (len(canonical_json(result).encode("utf-8")) + 1) // 2

    def synchronize() -> int:
        if result.context_manifest is not None:
            allowed = {
                "evidence": {item.evidence_id for item in result.evidence},
                "evidence_excerpt": {
                    f"{item.evidence_id}:{item.byte_offset}:{item.returned_bytes}"
                    for item in result.excerpts
                },
                "fact": {item.fact_id for item in result.facts},
                "project_pack": {result.project_pack.pack_id} if result.project_pack else set(),
            }
            result.context_manifest.included = [
                item
                for item in result.context_manifest.included
                if item.object_type not in allowed or item.object_id in allowed[item.object_type]
            ]
            result.context_manifest.omitted = dict(omitted)
        usage.omitted_records = dict(omitted)
        # Metadata includes the estimate itself; settle its serialized size.
        for _ in range(12):
            measured = estimate()
            optional_tokens = (
                len(
                    canonical_json({name: getattr(result, name) for name in optional_names}).encode(
                        "utf-8"
                    )
                )
                + 1
            ) // 2
            mandatory_tokens = max(0, measured - optional_tokens)
            if (
                usage.estimated_input_tokens == measured
                and usage.mandatory_tokens == mandatory_tokens
                and usage.optional_tokens == optional_tokens
            ):
                break
            usage.estimated_input_tokens = measured
            usage.mandatory_tokens = mandatory_tokens
            usage.optional_tokens = optional_tokens
            if result.context_manifest is not None:
                result.context_manifest.estimated_tokens = measured
        return estimate()

    while synchronize() > available - 32:
        if result.project_pack is not None:
            result.project_pack = None
            omitted["project_pack"] = omitted.get("project_pack", 0) + 1
        elif result.summary is not None:
            result.summary = None
            omitted["summary"] = omitted.get("summary", 0) + 1
        else:
            for name in ("historical_cases", "actions", "hypotheses", "evidence", "facts", "excerpts"):
                items = getattr(result, name)
                if items:
                    items.pop(0)
                    omitted[name] = omitted.get(name, 0) + 1
                    break
            else:
                usage.over_budget = synchronize() > available
                if (
                    usage.over_budget
                    and "mandatory_context_exceeds_token_budget" not in result.limitations
                ):
                    result.limitations.append("mandatory_context_exceeds_token_budget")
                break
    usage.over_budget = synchronize() > available
    synchronize()
    return result
