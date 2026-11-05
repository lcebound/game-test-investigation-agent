"""Lazy public adapters keep Temporal's deterministic sandbox free of API/model imports."""
from importlib import import_module

_EXPORTS = {
    "LocalWorkflowBackend": ".local",
    "DeferredWorkflowBackend": ".backend",
    "WorkflowBackend": ".backend",
    "InvestigationController": ".controller",
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(_EXPORTS[name], __name__), name)
    globals()[name] = value
    return value

__all__ = [
    "DeferredWorkflowBackend",
    "InvestigationController",
    "LocalWorkflowBackend",
    "WorkflowBackend",
]
