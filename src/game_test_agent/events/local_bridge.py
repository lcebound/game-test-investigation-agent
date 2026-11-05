from __future__ import annotations

import asyncio
import logging
from typing import Any

from game_test_agent.config import Settings
from game_test_agent.events.dispatchers import DurableDispatcher
from game_test_agent.events.ingress import EventIngress
from game_test_agent.events.policy import load_event_source_policies
from game_test_agent.events.rabbitmq import RabbitEventConsumer, RabbitEventPublisher
from game_test_agent.storage import Repository
from game_test_agent.workflows.backend import WorkflowBackend


class LocalWorkflowClient:
    """Dispatch durable broker work into the API's existing local workflow owner."""

    def __init__(self, repository: Repository, workflows: WorkflowBackend) -> None:
        self.repository = repository
        self.workflows = workflows

    async def start(self, workflow_id: str, investigation_id: str) -> None:
        if self.repository.get_investigation(investigation_id).workflow_id != workflow_id:
            raise ValueError("workflow identity mismatch")
        self.workflows.start(investigation_id)

    async def signal(
        self, workflow_id: str, kind: str, command_id: str, payload: dict[str, Any]
    ) -> None:
        investigation_id = workflow_id.removeprefix("investigation/")
        if self.repository.get_investigation(investigation_id).workflow_id != workflow_id:
            raise ValueError("workflow identity mismatch")
        if kind == "cancel":
            if not self.workflows.request_cancel(investigation_id):
                raise RuntimeError("cancellation was not accepted")
        else:
            # Inputs/evidence are already durable; waking the same owner consumes them.
            self.workflows.start(investigation_id)


class LocalEventBridge:
    """Optional MQ bridge hosted by the single-process local API, sharing its owner."""

    def __init__(
        self, settings: Settings, repository: Repository, workflows: WorkflowBackend
    ) -> None:
        self.settings = settings
        ingress = EventIngress(
            repository, load_event_source_policies(settings.event_source_policies_json),
            settings.default_budget(),
        )
        self.publisher = RabbitEventPublisher(
            settings.rabbitmq_url, settings.rabbitmq_exchange, settings.platform_id
        )
        self.consumers = [
            RabbitEventConsumer(
                settings.rabbitmq_url,
                settings.rabbitmq_exchange,
                "agent.anomalies.v1",
                settings.platform_id,
                ingress,
            ),
            RabbitEventConsumer(
                settings.rabbitmq_url,
                settings.rabbitmq_exchange,
                "agent.evidence.v1",
                settings.platform_id,
                ingress,
                routing_key=f"evidence.updated.{settings.platform_id}",
            ),
            RabbitEventConsumer(settings.rabbitmq_url, settings.rabbitmq_exchange,
                "agent.run-feedback.v1", settings.platform_id, ingress,
                routing_key=f"run.feedback.{settings.platform_id}"),
            RabbitEventConsumer(settings.rabbitmq_url, settings.rabbitmq_exchange,
                "agent.observations.v1", settings.platform_id, ingress,
                routing_key=f"observation.{settings.platform_id}"),
        ]
        self.dispatcher = DurableDispatcher(
            repository, LocalWorkflowClient(repository, workflows), self.publisher
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done() and all(
            getattr(item, "_connection", None) is not None
            and not item._connection.is_closed for item in [self.publisher, *self.consumers]
        )

    async def start(self) -> None:
        try:
            await self.publisher.connect()
            for consumer in self.consumers:
                await consumer.run()
            self._task = asyncio.create_task(self._dispatch(), name="local-event-bridge")
        except BaseException:
            await self.close()
            raise

    async def _dispatch(self) -> None:
        while True:
            try:
                await self.dispatcher.dispatch_starts()
                await self.dispatcher.dispatch_commands()
                await self.dispatcher.dispatch_events()
            except Exception as error:
                logging.getLogger(__name__).warning(
                    "local event dispatch failed (%s)", type(error).__name__
                )
            await asyncio.sleep(self.settings.dispatcher_poll_seconds)

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await asyncio.gather(
            *(consumer.close() for consumer in self.consumers), self.publisher.close()
        )


async def dispatch_local_commands(settings, repository, workflows):
    """Recover durable feedback/input commands even with no broker connection."""
    dispatcher = DurableDispatcher(repository, LocalWorkflowClient(repository, workflows), None)
    while True:
        try:
            await dispatcher.dispatch_commands()
        except Exception as error:
            logging.getLogger(__name__).warning("local command dispatch failed (%s)", type(error).__name__)
        await asyncio.sleep(max(.1, settings.dispatcher_poll_seconds))
