from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from game_test_agent.contracts.models import AdmissionTicket, ResourceRequest
from game_test_agent.storage import Repository


class ResourceScheduler:
    """Fair admission controller; execution platforms still own device allocation."""

    def __init__(
        self,
        *,
        project_device_minute_limits: dict[str, float] | None = None,
        max_concurrent_per_project: int = 1,
    ) -> None:
        if max_concurrent_per_project < 1:
            raise ValueError("project concurrency must be positive")
        self._limits = dict(project_device_minute_limits or {})
        self._concurrency = max_concurrent_per_project
        self._queues: dict[str, deque[ResourceRequest]] = defaultdict(deque)
        self._project_ring: deque[str] = deque()
        self._active: dict[str, AdmissionTicket] = {}
        self._domain_owner: dict[str, str] = {}
        self._used: dict[str, float] = defaultdict(float)
        self._fencing: dict[str, int] = defaultdict(int)
        self._known_nodes: set[tuple[str, str]] = set()

    def enqueue(self, request: ResourceRequest) -> bool:
        identity = (request.task_id, request.node_id)
        if identity in self._known_nodes:
            return False
        self._known_nodes.add(identity)
        queue = self._queues[request.project_id]
        queue.append(request)
        self._queues[request.project_id] = deque(
            sorted(queue, key=lambda item: (-item.priority, item.node_id))
        )
        if request.project_id not in self._project_ring:
            self._project_ring.append(request.project_id)
        return True

    def admit_next(self, *, now: datetime | None = None) -> AdmissionTicket | None:
        current = now or datetime.now(timezone.utc)
        self.expire(now=current)
        for _ in range(len(self._project_ring)):
            project = self._project_ring[0]
            self._project_ring.rotate(-1)
            queue = self._queues[project]
            if not queue:
                self._remove_project(project)
                continue
            active_count = sum(ticket.project_id == project for ticket in self._active.values())
            if active_count >= self._concurrency:
                continue
            request = queue[0]
            limit = self._limits.get(project, float("inf"))
            if self._used[project] + request.device_minutes > limit:
                continue
            if request.exclusive and request.resource_domain in self._domain_owner:
                continue
            queue.popleft()
            if not queue:
                self._remove_project(project)
            self._fencing[request.resource_domain] += 1
            fence = self._fencing[request.resource_domain]
            identity = (
                f"{request.project_id}:{request.task_id}:{request.node_id}:"
                f"{request.resource_domain}:{fence}"
            )
            ticket = AdmissionTicket(
                ticket_id="admit-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
                project_id=request.project_id,
                task_id=request.task_id,
                node_id=request.node_id,
                plan_step_id=request.plan_step_id,
                plan_node_revision=request.plan_node_revision,
                resource_domain=request.resource_domain,
                cost_limit=request.device_minutes,
                fencing_token=fence,
                expires_at=current + timedelta(seconds=request.ttl_seconds),
            )
            self._active[ticket.ticket_id] = ticket
            if request.exclusive:
                self._domain_owner[request.resource_domain] = ticket.ticket_id
            return ticket
        return None

    def release(self, ticket_id: str, *, consumed_device_minutes: float = 0) -> None:
        if consumed_device_minutes < 0:
            raise ValueError("consumed device minutes cannot be negative")
        ticket = self._active.pop(ticket_id, None)
        if ticket is None:
            raise LookupError(ticket_id)
        if consumed_device_minutes > ticket.cost_limit:
            self._active[ticket_id] = ticket
            raise ValueError("consumption exceeds the ticket cost limit")
        self._used[ticket.project_id] += consumed_device_minutes
        if self._domain_owner.get(ticket.resource_domain) == ticket_id:
            self._domain_owner.pop(ticket.resource_domain, None)

    def cancel(self, ticket_id: str) -> AdmissionTicket:
        ticket = self._active.get(ticket_id)
        if ticket is None:
            raise LookupError(ticket_id)
        canceled = ticket.model_copy(update={"canceled": True})
        self._active[ticket_id] = canceled
        return canceled

    def expire(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(timezone.utc)
        expired = [
            ticket_id for ticket_id, ticket in self._active.items() if ticket.expires_at <= current
        ]
        for ticket_id in expired:
            ticket = self._active.pop(ticket_id)
            if self._domain_owner.get(ticket.resource_domain) == ticket_id:
                self._domain_owner.pop(ticket.resource_domain, None)
        return sorted(expired)

    def accepts_fencing_token(self, resource_domain: str, token: int) -> bool:
        return token == self._fencing.get(resource_domain, 0)

    def usage(self, project_id: str) -> float:
        return self._used[project_id]

    def _remove_project(self, project_id: str) -> None:
        self._project_ring = deque(item for item in self._project_ring if item != project_id)


class DurableResourceScheduler:
    """Database-backed fair admission with restart-safe tickets and fencing tokens."""

    def __init__(
        self,
        repository: Repository,
        *,
        project_device_minute_limits: dict[str, float] | None = None,
        max_concurrent_per_project: int = 1,
    ) -> None:
        if max_concurrent_per_project < 1:
            raise ValueError("project concurrency must be positive")
        self.repository = repository
        self._limits = dict(project_device_minute_limits or {})
        self._concurrency = max_concurrent_per_project

    def enqueue(self, request: ResourceRequest) -> bool:
        return self.repository.enqueue_resource_request(request)

    def admit_next(self, *, now: datetime | None = None) -> AdmissionTicket | None:
        return self.repository.admit_next_resource(
            project_device_minute_limits=self._limits,
            max_concurrent_per_project=self._concurrency,
            now=now,
        )

    def acquire(
        self, request: ResourceRequest, *, now: datetime | None = None
    ) -> AdmissionTicket | None:
        self.enqueue(request)
        existing = self.repository.get_admission_ticket(
            request.task_id, request.node_id, active_only=True
        )
        current = now or datetime.now(timezone.utc)
        if existing is not None and existing.expires_at > current and not existing.canceled:
            return existing
        admitted = self.admit_next(now=current)
        if admitted and (admitted.task_id, admitted.node_id) == (
            request.task_id,
            request.node_id,
        ):
            return admitted
        return self.repository.get_admission_ticket(
            request.task_id, request.node_id, active_only=True
        )

    def release(self, ticket_id: str, *, consumed_device_minutes: float = 0) -> bool:
        return self.repository.release_admission_ticket(
            ticket_id, consumed_device_minutes=consumed_device_minutes
        )

    def cancel(self, ticket_id: str) -> AdmissionTicket:
        return self.repository.cancel_admission_ticket(ticket_id)

    def cancel_request(self, task_id: str, node_id: str) -> bool:
        return self.repository.cancel_resource_request(task_id, node_id)

    def accepts_fencing_token(self, resource_domain: str, token: int) -> bool:
        return self.repository.accepts_fencing_token(resource_domain, token)
