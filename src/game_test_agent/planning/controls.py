from typing import Any


def execution_controls(constraints: dict[str, Any]) -> dict[str, Any]:
    """Lineage and admission identity do not change a test's experimental controls."""
    transport_metadata = {"root_event_id", "causation_id", "investigation_depth", "_admission"}
    return {key: value for key, value in constraints.items() if key not in transport_metadata}
