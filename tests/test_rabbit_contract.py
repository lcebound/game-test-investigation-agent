from __future__ import annotations

import asyncio

import pytest

from game_test_agent.events.rabbitmq import (
    RabbitEventConsumer,
    dead_letter_topology,
    validate_routing_segment,
)


def test_routing_segment_rejects_user_controlled_syntax():
    assert validate_routing_segment("lab-a") == "lab-a"
    for invalid in ("Lab-A", "lab.a", "#", "", "../other"):
        with pytest.raises(ValueError):
            validate_routing_segment(invalid)


def test_dead_letter_topology_is_deterministic_and_bounds_redelivery():
    topology = dead_letter_topology("game_test.events.v1", "agent.anomalies.v1")

    assert topology.exchange_name == "game_test.events.v1.dead-letter"
    assert topology.queue_name == "agent.anomalies.v1.dead-letter"
    assert topology.routing_key == "dead.agent.anomalies.v1"
    assert topology.source_queue_arguments == {
        "x-queue-type": "quorum",
        "x-delivery-limit": 5,
        "x-dead-letter-exchange": "game_test.events.v1.dead-letter",
        "x-dead-letter-routing-key": "dead.agent.anomalies.v1",
    }
    with pytest.raises(ValueError):
        dead_letter_topology("events", "queue", delivery_limit=0)


class FakeMessage:
    body = b"{}"

    def __init__(self) -> None:
        self.acked = False
        self.rejected = None
        self.nacked = None

    async def ack(self):
        self.acked = True

    async def reject(self, *, requeue):
        self.rejected = requeue

    async def nack(self, *, requeue):
        self.nacked = requeue


class StubIngress:
    def __init__(self, error=None) -> None:
        self.error = error

    def accept(self, body):
        if self.error:
            raise self.error
        return None


def test_contract_errors_dead_letter_and_transient_errors_requeue():
    async def scenario() -> None:
        invalid = FakeMessage()
        invalid_consumer = RabbitEventConsumer(
            "amqp://unused", "events", "queue-invalid", "local", StubIngress(ValueError())
        )
        await invalid_consumer._on_message(invalid)
        assert invalid.rejected is False

        transient = FakeMessage()
        transient_consumer = RabbitEventConsumer(
            "amqp://unused", "events", "queue-transient", "local", StubIngress(RuntimeError())
        )
        await transient_consumer._on_message(transient)
        assert transient.nacked is True

        valid = FakeMessage()
        valid_consumer = RabbitEventConsumer(
            "amqp://unused", "events", "queue-valid", "local", StubIngress()
        )
        await valid_consumer._on_message(valid)
        assert valid.acked

    asyncio.run(scenario())
