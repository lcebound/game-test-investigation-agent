import json
from types import SimpleNamespace


def initial_plan():
    return {
        "expected_revision": 0,
        "reason": "Inspect the original failure, experiment intent and recovery before judging a defect.",
        "steps": [
            {
                "step_id": key,
                "title": title,
                "question": question,
                "evidence_needed": [needed],
                "completion_condition": condition,
                "status": "in_progress" if index == 0 else "pending",
            }
            for index, (key, title, question, needed, condition) in enumerate(
                [
                    (
                        "source",
                        "Inspect source failure",
                        "Which assertion failed?",
                        "Original run and assertions",
                        "Record the failed assertion and measured value",
                    ),
                    (
                        "intent",
                        "Check experimental intent",
                        "Was the observed effect expected?",
                        "Declared expectations and injection parameters",
                        "Compare with declared expectations or explicitly record the missing baseline",
                    ),
                    (
                        "recovery",
                        "Assess recovery",
                        "Is recovery independently verified?",
                        "Recovery observations and cleanup receipts",
                        "Distinguish cleanup from independently observed recovery",
                    ),
                ]
            )
        ],
    }


def initial_plan_call():
    arguments = json.dumps(initial_plan())
    result = SimpleNamespace(
        id="initial-plan", function=SimpleNamespace(name="agent_update_plan", arguments=arguments)
    )
    result.model_dump = lambda **_: {
        "id": result.id,
        "type": "function",
        "function": {"name": result.function.name, "arguments": arguments},
    }
    return result
