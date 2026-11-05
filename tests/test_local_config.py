import json

import pytest

from game_test_agent.local_config import load_local_environment
from game_test_agent.api.workspace import _public_endpoint


def test_local_configuration_loads_credentials_without_overwriting_environment(
    tmp_path, monkeypatch
):
    path = tmp_path / "local.json"
    path.write_text(
        json.dumps({"OPENAI_API_KEY": "local-test-key", "GTA_MODEL": "file-model"}),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("GTA_MODEL", "explicit-model")
    monkeypatch.delenv("GTA_LOCAL_CONFIG_FILE", raising=False)
    assert load_local_environment(path) == path
    import os

    assert os.environ["OPENAI_API_KEY"] == "local-test-key"
    assert os.environ["GTA_MODEL"] == "explicit-model"
    assert os.environ["GTA_LOCAL_CONFIG_FILE"] == str(path)


def test_invalid_local_configuration_has_no_partial_effect_or_secret_in_error(
    tmp_path, monkeypatch
):
    path = tmp_path / "invalid.json"
    path.write_text(
        json.dumps({"OPENAI_API_KEY": "private-test-key", "INVALID": 1}), encoding="utf-8"
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError) as error:
        load_local_environment(path)
    assert "private-test-key" not in str(error.value)
    import os

    assert "OPENAI_API_KEY" not in os.environ


def test_endpoint_projection_omits_credentials_and_query_secrets():
    assert (
        _public_endpoint("https://user:secret@example.test/v1?api_key=secret#private")
        == "https://example.test/v1"
    )
    assert _public_endpoint("not-a-url") == ""
