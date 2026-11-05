from __future__ import annotations

import pytest

from game_test_agent.analysis.windows_security import compile_policy


@pytest.mark.parametrize("change", [
    {"network": "approved"}, {"network": 1}, {"read_write": []},
    {"read_only": ["relative/path"]}, {"deny": ["bad\0path"]},
    {"allow_unsafe_fallback": True},
])
def test_native_policy_rejects_ambiguous_or_injected_permissions(tmp_path, change):
    policy = {"read_only": [str(tmp_path)], "read_write": [str(tmp_path)],
              "deny": [str(tmp_path.anchor)], "network": False}
    with pytest.raises(ValueError):
        compile_policy({**policy, **change})
