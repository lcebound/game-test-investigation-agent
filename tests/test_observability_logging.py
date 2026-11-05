from __future__ import annotations

import asyncio
import json
import logging

import httpx

from game_test_agent.api import create_app
from game_test_agent.observability import JsonLogFormatter


def test_json_log_formatter_redacts_structured_secrets() -> None:
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="game_test_agent.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="structured_event",
        args=(),
        exc_info=None,
    )
    record.structured_fields = {
        "token": "secret-token",
        "fencing_token": "lease-capability",
        "nested": {"password": "secret-password", "result": "ok"},
    }

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "structured_event"
    assert payload["token"] == "[REDACTED]"
    assert payload["fencing_token"] == "[REDACTED]"
    assert payload["nested"] == {"password": "[REDACTED]", "result": "ok"}


def test_request_id_is_preserved_or_replaced(settings) -> None:
    async def scenario() -> None:
        app = create_app(settings)
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                supplied = await client.get("/healthz", headers={"X-Request-ID": "trace-123"})
                invalid = await client.get("/healthz", headers={"X-Request-ID": "bad request id"})

        assert supplied.headers["X-Request-ID"] == "trace-123"
        generated = invalid.headers["X-Request-ID"]
        assert len(generated) == 32
        assert generated.isalnum()

    asyncio.run(scenario())
