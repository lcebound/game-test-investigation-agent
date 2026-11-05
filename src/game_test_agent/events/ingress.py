from __future__ import annotations

import json

from game_test_agent.contracts.models import (
    AnomalyDetectedEvent,
    Budget,
    EvidenceUpdatedEvent,
    InvestigationView,
)
from game_test_agent.storage import Repository
from game_test_agent.events.policy import EventSourcePolicy
from game_test_agent.events.feedback import RunFeedbackEvent
from game_test_agent.storage.feedback import FeedbackStore


class EventIngress:
    """Broker-independent handler. ACK only after this method returns successfully."""

    MAX_MESSAGE_BYTES = 256 * 1024

    def __init__(
        self,
        repository: Repository,
        source_policies: dict[tuple[str, str, str], EventSourcePolicy] | None = None,
        default_budget: Budget | None = None,
    ) -> None:
        self.repository = repository
        self.source_policies = source_policies or {}
        self.default_budget = default_budget

    def accept(self, body: bytes) -> tuple[InvestigationView, bool] | dict:
        if len(body) > self.MAX_MESSAGE_BYTES:
            raise ValueError("event exceeds the 256 KiB contract limit; use an artifact reference")
        try:
            envelope = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("event body must be valid UTF-8 JSON") from error
        event_type = envelope.get("event_type") if isinstance(envelope, dict) else None
        if event_type == "game_test.observation":
            from game_test_agent.events.observation_router import ObservationEvent, ObservationBatchRouter
            from game_test_agent.events.semantic_router import SemanticConversationRouter
            event = ObservationEvent.model_validate(envelope)
            if (event.source, event.tenant_id, event.project_id) not in self.source_policies:
                raise ValueError("observation source is not configured for this project")
            return ObservationBatchRouter(self.repository, SemanticConversationRouter(self.repository)).accept(event)
        if event_type == "game_test.run.feedback":
            event = RunFeedbackEvent.model_validate(envelope)
            if (event.source, event.tenant_id, event.project_id) not in self.source_policies:
                raise ValueError("run feedback source is not configured for this project")
            return FeedbackStore(self.repository).accept(event)
        if event_type == "game_test.anomaly.detected":
            event = AnomalyDetectedEvent.model_validate(envelope)
            policy = self.source_policies.get((event.source, event.tenant_id, event.project_id))
            return self.repository.ingest_anomaly(
                event,
                principal_id=(f"event:{event.source}" if policy else "event:unprivileged"),
                scopes=(set(policy.scopes) if policy else set()),
                policy_profile=(policy.policy_profile if policy else "bounded-diagnosis"),
                budget=self.default_budget,
            )
        if event_type == "game_test.evidence.updated":
            event = EvidenceUpdatedEvent.model_validate(envelope)
            return self.repository.ingest_evidence_update(event)
        raise ValueError(f"unsupported event_type: {event_type!r}")
