from __future__ import annotations

import asyncio

from temporalio.client import Client

from game_test_agent.config import Settings
from game_test_agent.events.dispatchers import DurableDispatcher
from game_test_agent.events.ingress import EventIngress
from game_test_agent.events.rabbitmq import RabbitEventConsumer, RabbitEventPublisher
from game_test_agent.events.policy import load_event_source_policies
from game_test_agent.storage import create_repository
from game_test_agent.workflows.client import TemporalWorkflowClient
from game_test_agent.observability import configure_logging


async def run_bridge() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    if settings.workflow_backend != "temporal":
        raise RuntimeError("event bridge requires GTA_WORKFLOW_BACKEND=temporal")
    repository = create_repository(settings)
    temporal = await Client.connect(
        settings.temporal_address, namespace=settings.temporal_namespace
    )
    workflow_client = TemporalWorkflowClient(temporal, settings.temporal_task_queue)
    publisher = RabbitEventPublisher(
        settings.rabbitmq_url, settings.rabbitmq_exchange, settings.platform_id
    )
    source_policies = load_event_source_policies(settings.event_source_policies_json)
    anomaly_consumer = RabbitEventConsumer(
        settings.rabbitmq_url,
        settings.rabbitmq_exchange,
        "agent.anomalies.v1",
        settings.platform_id,
        EventIngress(repository, source_policies, settings.default_budget()),
    )
    evidence_consumer = RabbitEventConsumer(
        settings.rabbitmq_url,
        settings.rabbitmq_exchange,
        "agent.evidence.v1",
        settings.platform_id,
        EventIngress(repository, source_policies, settings.default_budget()),
        routing_key=f"evidence.updated.{settings.platform_id}",
    )
    dispatcher = DurableDispatcher(repository, workflow_client, publisher)
    feedback_consumer = RabbitEventConsumer(
        settings.rabbitmq_url, settings.rabbitmq_exchange, "agent.run-feedback.v1", settings.platform_id,
        EventIngress(repository, source_policies, settings.default_budget()),
        routing_key=f"run.feedback.{settings.platform_id}")
    observation_consumer = RabbitEventConsumer(settings.rabbitmq_url, settings.rabbitmq_exchange,
        "agent.observations.v1", settings.platform_id, EventIngress(repository,source_policies,settings.default_budget()),
        routing_key=f"observation.{settings.platform_id}")
    try:
        await publisher.connect()
        await anomaly_consumer.run()
        await evidence_consumer.run()
        await feedback_consumer.run()
        await observation_consumer.run()
        while True:
            await dispatcher.dispatch_starts()
            await dispatcher.dispatch_commands()
            await dispatcher.dispatch_events()
            await asyncio.sleep(settings.dispatcher_poll_seconds)
    finally:
        await anomaly_consumer.close()
        await evidence_consumer.close()
        await feedback_consumer.close()
        await observation_consumer.close()
        await publisher.close()
        repository.close()


def main() -> None:
    asyncio.run(run_bridge())


if __name__ == "__main__":
    main()
