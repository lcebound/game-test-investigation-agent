from __future__ import annotations

import hashlib
from collections import defaultdict, deque
from dataclasses import dataclass

from game_test_agent.contracts.models import (
    DependencyEdge,
    InvalidationPlan,
    ProvenanceNode,
)
from game_test_agent.storage import Repository, canonical_json


@dataclass(frozen=True, slots=True)
class ProvenanceGraph:
    nodes: dict[str, ProvenanceNode]
    edges: list[DependencyEdge]


class ProvenanceIndex:
    """Relational-ledger projection for trace and precise downstream invalidation."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    def build(self, investigation_id: str) -> ProvenanceGraph:
        investigation = self.repository.get_investigation(investigation_id)
        scope = {
            "tenant_id": investigation.request.tenant_id,
            "project_id": investigation.request.project_id,
        }
        nodes: dict[str, ProvenanceNode] = {}
        edges: list[DependencyEdge] = []
        evidence = self.repository.list_evidence(investigation_id)
        superseded = self.repository.superseded_evidence_ids(investigation_id)
        for item in evidence:
            nodes[item.evidence_id] = ProvenanceNode(
                node_id=item.evidence_id,
                node_type="observation",
                revision=str(item.source_revision),
                scope=scope,
                stale=item.retrieval_status != "ready" or item.evidence_id in superseded,
            )
            for source_id in item.derived_from:
                nodes.setdefault(
                    source_id,
                    ProvenanceNode(node_id=source_id, node_type="observation", scope=scope),
                )
                edges.append(
                    DependencyEdge(
                        source_id=source_id,
                        target_id=item.evidence_id,
                        edge_kind="data",
                        relation="derived_from",
                        algorithm_version=item.parser_version or item.producer_version,
                    )
                )
        for fact in self.repository.list_facts(investigation_id):
            nodes[fact.fact_id] = ProvenanceNode(
                node_id=fact.fact_id,
                node_type="fact",
                revision=fact.extractor_version,
                scope=scope,
            )
            parameters_hash = hashlib.sha256(canonical_json(fact.data).encode()).hexdigest()
            for evidence_id in fact.evidence_ids:
                edges.append(
                    DependencyEdge(
                        source_id=evidence_id,
                        target_id=fact.fact_id,
                        edge_kind="data",
                        relation="extracted_by",
                        algorithm_version=fact.extractor_version,
                        parameters_hash=parameters_hash,
                    )
                )
        for assessment in self.repository.list_assessments(investigation_id):
            assessment_id = (
                f"assessment:{assessment.criterion_id}:{assessment.evaluated_state_revision}"
            )
            nodes[assessment_id] = ProvenanceNode(
                node_id=assessment_id,
                node_type="assessment",
                revision=assessment.rule_version,
                scope=scope,
            )
            for fact_id in assessment.supporting_fact_ids + assessment.contradicting_fact_ids:
                edges.append(
                    DependencyEdge(
                        source_id=fact_id,
                        target_id=assessment_id,
                        edge_kind="data",
                        relation="evaluated_by",
                        algorithm_version=assessment.rule_version,
                    )
                )
        for relation in self.repository.list_evidence_relations(investigation_id):
            edge_kind = "data" if relation.relation_type == "derived_from" else "argument"
            edges.append(
                DependencyEdge(
                    source_id=relation.source_evidence_id,
                    target_id=relation.target_id,
                    edge_kind=edge_kind,
                    relation=relation.relation_type,
                )
            )
        for report in self.repository.list_reports(investigation_id):
            summary_id = f"summary:report:{investigation_id}:{report.revision}"
            nodes[summary_id] = ProvenanceNode(
                node_id=summary_id,
                node_type="summary",
                revision=str(report.revision),
                scope=scope,
            )
            for claim in report.claims:
                claim_id = f"claim:{investigation_id}:{report.revision}:{claim.claim_id}"
                nodes[claim_id] = ProvenanceNode(
                    node_id=claim_id,
                    node_type="claim",
                    revision=str(report.revision),
                    scope=scope,
                )
        edges.extend(self.repository.list_provenance_edges(investigation_id))
        edges = self._deduplicate(edges)
        self._propagate_stale(nodes, edges)
        return ProvenanceGraph(nodes, edges)

    def sources(
        self,
        investigation_id: str,
        target_id: str,
        *,
        max_depth: int = 20,
    ) -> list[str]:
        graph = self.build(investigation_id)
        reverse: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            reverse[edge.target_id].append(edge.source_id)
        found: list[str] = []
        queue = deque([(target_id, 0)])
        seen = {target_id}
        while queue:
            node, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for source in sorted(reverse.get(node, [])):
                if source in seen:
                    continue
                seen.add(source)
                found.append(source)
                queue.append((source, depth + 1))
        return found

    def invalidate(self, investigation_id: str, source_id: str) -> InvalidationPlan:
        graph = self.build(investigation_id)
        downstream: dict[str, list[str]] = defaultdict(list)
        for edge in graph.edges:
            downstream[edge.source_id].append(edge.target_id)
        affected: set[str] = set()
        queue = deque([source_id])
        while queue:
            node = queue.popleft()
            for target in downstream.get(node, []):
                if target not in affected and target != source_id:
                    affected.add(target)
                    queue.append(target)
        order, cycles = self._topological_subset(source_id, affected, downstream)
        return InvalidationPlan(
            changed_source_id=source_id,
            affected_node_ids=sorted(affected),
            recompute_order=order,
            cycles=cycles,
        )

    @staticmethod
    def computation_identity(
        input_revisions: dict[str, str],
        algorithm_version: str,
        parameters: dict,
        rule_version: str,
    ) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "inputs": input_revisions,
                    "algorithm_version": algorithm_version,
                    "parameters": parameters,
                    "rule_version": rule_version,
                }
            ).encode()
        ).hexdigest()

    @staticmethod
    def _deduplicate(edges: list[DependencyEdge]) -> list[DependencyEdge]:
        unique: dict[tuple[str, str, str, str], DependencyEdge] = {}
        for edge in edges:
            key = (edge.source_id, edge.target_id, edge.edge_kind, edge.relation)
            unique[key] = edge
        return [unique[key] for key in sorted(unique)]

    @staticmethod
    def _propagate_stale(nodes: dict[str, ProvenanceNode], edges: list[DependencyEdge]) -> None:
        downstream: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            downstream[edge.source_id].append(edge.target_id)
        queue = deque(node_id for node_id, node in nodes.items() if node.stale)
        seen = set(queue)
        while queue:
            source_id = queue.popleft()
            for target_id in downstream.get(source_id, []):
                target = nodes.get(target_id)
                if target is None:
                    continue
                if not target.stale:
                    nodes[target_id] = target.model_copy(update={"stale": True})
                if target_id not in seen:
                    seen.add(target_id)
                    queue.append(target_id)

    @staticmethod
    def _topological_subset(
        source_id: str,
        affected: set[str],
        downstream: dict[str, list[str]],
    ) -> tuple[list[str], list[list[str]]]:
        nodes = set(affected)
        indegree = {node: 0 for node in nodes}
        for parent in nodes | {source_id}:
            for child in downstream.get(parent, []):
                if parent in nodes and child in nodes:
                    indegree[child] += 1
        ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
        order: list[str] = []
        while ready:
            node = ready.popleft()
            order.append(node)
            for child in sorted(downstream.get(node, [])):
                if child not in indegree:
                    continue
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        remaining = sorted(nodes - set(order))
        return order, [remaining] if remaining else []
