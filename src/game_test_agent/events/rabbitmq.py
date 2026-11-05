from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from game_test_agent.events.ingress import EventIngress
from game_test_agent.storage import ConflictError


_ROUTING_SEGMENT = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def validate_routing_segment(value: str) -> str:
    if not _ROUTING_SEGMENT.fullmatch(value):
        raise ValueError("routing segments must use lowercase letters, digits and hyphens")
    return value


@dataclass(frozen=True, slots=True)
class DeadLetterTopology:
    exchange_name: str
    queue_name: str
    routing_key: str
    source_queue_arguments: dict[str, str | int]


def dead_letter_topology(
    exchange_name: str, queue_name: str, *, delivery_limit: int = 5
) -> DeadLetterTopology:
    if not 1 <= delivery_limit <= 100:
        raise ValueError("delivery_limit must be between 1 and 100")
    if not queue_name or len(queue_name) > 200 or any(ord(char) < 32 for char in queue_name):
        raise ValueError("queue_name is not a safe broker entity name")
    dead_exchange = f"{exchange_name}.dead-letter"
    dead_queue = f"{queue_name}.dead-letter"
    routing_key = f"dead.{queue_name}"
    return DeadLetterTopology(
        exchange_name=dead_exchange,
        queue_name=dead_queue,
        routing_key=routing_key,
        source_queue_arguments={
            "x-queue-type": "quorum",
            "x-delivery-limit": delivery_limit,
            "x-dead-letter-exchange": dead_exchange,
            "x-dead-letter-routing-key": routing_key,
        },
    )


class RabbitEventPublisher:
    def __init__(self, url: str, exchange_name: str, platform_id: str) -> None:
        self.url = url
        self.exchange_name = exchange_name
        self.platform_id = validate_routing_segment(platform_id)
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None

    async def connect(self) -> None:
        import aio_pika

        self._connection = await aio_pika.connect_robust(self.url)
        self._channel = await self._connection.channel(publisher_confirms=True)
        self._exchange = await self._channel.declare_exchange(
            self.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
        )

    async def publish(
        self, event_id: str, event_type: str, payload: dict[str, Any], payload_hash: str
    ) -> None:
        import json

        import aio_pika

        if self._exchange is None:
            await self.connect()
        suffix = "completed" if event_type.endswith(".completed") else "updated"
        routing_key = f"investigation.{suffix}.{self.platform_id}"
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        if len(body) > EventIngress.MAX_MESSAGE_BYTES:
            raise ValueError("result event exceeds the message size limit")
        message = aio_pika.Message(
            body=body,
            message_id=event_id,
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            headers={"payload_hash": payload_hash},
        )
        await self._exchange.publish(message, routing_key=routing_key, mandatory=True)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()


class RabbitEventConsumer:
    def __init__(
        self,
        url: str,
        exchange_name: str,
        queue_name: str,
        platform_id: str,
        ingress: EventIngress,
        *,
        prefetch: int = 8,
        routing_key: str | None = None,
        delivery_limit: int = 5,
    ) -> None:
        self.url = url
        self.exchange_name = exchange_name
        self.queue_name = queue_name
        self.platform_id = validate_routing_segment(platform_id)
        self.ingress = ingress
        self.prefetch = prefetch
        self.routing_key = routing_key or f"anomaly.detected.{self.platform_id}"
        self.dead_letters = dead_letter_topology(
            exchange_name, queue_name, delivery_limit=delivery_limit
        )
        self._connection: Any = None

    async def run(self) -> None:
        import aio_pika

        self._connection = await aio_pika.connect_robust(self.url)
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=self.prefetch)
        exchange = await channel.declare_exchange(
            self.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
        )
        dead_exchange = await channel.declare_exchange(
            self.dead_letters.exchange_name,
            aio_pika.ExchangeType.DIRECT,
            durable=True,
        )
        dead_queue = await channel.declare_queue(
            self.dead_letters.queue_name,
            durable=True,
            arguments={"x-queue-type": "quorum"},
        )
        await dead_queue.bind(dead_exchange, routing_key=self.dead_letters.routing_key)
        queue = await channel.declare_queue(
            self.queue_name,
            durable=True,
            arguments=self.dead_letters.source_queue_arguments,
        )
        await queue.bind(exchange, routing_key=self.routing_key)
        await queue.consume(self._on_message, no_ack=False)

    async def _on_message(self, message: Any) -> None:
        try:
            self.ingress.accept(message.body)
        except (ValidationError, ValueError, ConflictError):
            # Contract violations require operator review and must not poison the queue.
            await message.reject(requeue=False)
            return
        except Exception:
            await message.nack(requeue=True)
            return
        await message.ack()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()


RabbitAnomalyConsumer = RabbitEventConsumer
