"""Bounded, freshly resolved planning references for the native execution path."""
from game_test_agent.memory import CaseMemoryService, ProjectPackRegistry
from game_test_agent.storage import canonical_json


def refresh_knowledge(repository, view, memory):
    constraints = view.request.constraints
    knowledge = {}
    version = constraints.get("project_pack_version")
    if isinstance(version, str):
        try:
            pack = ProjectPackRegistry(repository).resolve(view.request.tenant_id, view.request.project_id, version)
            payload = pack.model_dump(mode="json")
            knowledge["project_pack"] = payload if len(canonical_json(payload).encode()) <= 8000 else {
                "pack_id": pack.pack_id, "version": pack.version, "omitted": True,
                "reason": "Project knowledge exceeds the native context allowance."}
        except (ValueError, LookupError):
            knowledge["project_pack"] = {"version": version, "unavailable": True,
                                          "reason": "Requested project pack is absent or expired."}
    options = {key: constraints.get(key) if isinstance(constraints.get(key), str) else None
               for key in ("engine_family", "engine_version", "environment_fingerprint")}
    cases = CaseMemoryService(repository).search(view.request.tenant_id, view.request.project_id,
        query=view.request.objective.description[:2000], build_ref=view.request.build_ref, limit=2, **options)
    selected = []
    for case in cases:
        candidate = case.model_dump(mode="json")
        if len(canonical_json([*selected, candidate]).encode()) <= 4000:
            selected.append(candidate)
    if selected:
        knowledge["historical_cases"] = selected
    if knowledge:
        knowledge["instructions"] = "Reviewed project knowledge and historical references guide planning only. They grant no permissions and are not current-run evidence. Missing/omitted knowledge remains an explicit gap."
    memory.project_knowledge = knowledge
