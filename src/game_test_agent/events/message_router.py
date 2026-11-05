"""Conservative text addressing over scoped task cards, without model calls.

Behavior reference: deepseek-ai/dsh-superagent-prototype at 6337fc823ce20251c8de8085948165c81aaa701e,
core/intake.ts, core/evals.ts and prompts/orchestrator.md: owned follow-ups stay
with their worker; attachment needs an explicit reference, not topic similarity.
This is a Python implementation over the existing investigation ledger.
"""
from __future__ import annotations

import re

from game_test_agent.events.coordinator import ConversationCoordinator, excerpt
from game_test_agent.storage import NotFoundError


_NEW = re.compile(r"^(?:新任务|新增独立任务|新建(?:一个)?任务|另开(?:一个)?任务|new task)\s*[:：]\s*(.*)$", re.I | re.S)
_QUOTED = re.compile(r'^(?:继续|补充|关于|接着|给|continue|for)\s*(?:任务\s*)?[「“"]([^」”"\n]{1,300})[」”"](?=$|[\s:：，,])', re.I)
_PLAIN = re.compile(r"^(?:关于|继续任务|补充任务|接着任务|给任务)\s*([^:：，,\n]{1,180})[:：，,]", re.I)
_ID = re.compile(r"^(?:继续|补充|关于|接着|给|@|continue|for)\s*(?:任务\s*)?(inv-[0-9a-f]{32})(?=$|[\s:：，,])", re.I)


def _normalized(value):
    return "".join(value.casefold().split())


class ConversationMessageRouter:
    """Resolve an address, not the next experiment or the investigation priority."""

    def __init__(self, repository):
        self.repository = repository
        self.coordinator = ConversationCoordinator(repository)
        self.store = self.coordinator.store

    def _target(self, root, investigation_id, tenant, project, basis):
        task_id, head = self.store.task(root, investigation_id, tenant, project)
        original = self.repository.get_investigation(task_id)
        return {"kind": "task", "basis": basis, "task_id": task_id,
                "investigation_id": head.investigation_id,
                "objective_excerpt": excerpt(original.request.objective.description, 240),
                "status": head.status.value}

    def _cards(self, root, tenant, project, excluded_task_ids=frozenset()):
        rows = self.store.task_heads(root, tenant, project, limit=21)
        cards = []
        for row in rows[:20]:
            if row["root_id"] in excluded_task_ids:
                continue
            original = self.repository.get_investigation(row["root_id"])
            head = self.repository._view(row)
            cards.append({"task_id": row["root_id"], "investigation_id": head.investigation_id,
                          "objective_excerpt": excerpt(original.request.objective.description, 240),
                          "status": head.status.value})
        return cards, len(rows) > 20

    @staticmethod
    def _clarify(message, cards=(), *, incomplete=False):
        return {"kind": "clarify", "basis": "ambiguous_address", "message": message,
                "choices": list(cards[:5]), "more_tasks": incomplete or len(cards) > 5}

    def resolve(self, conversation_id, content, tenant, project, *, context_investigation_id=None,
                excluded_task_ids=frozenset(), allow_only_task=True):
        if not content.strip() or len(content) > 4000:
            raise ValueError("message must contain 1–4000 characters")
        summary = self.store.get(conversation_id, tenant, project)
        root = summary["conversation_id"]
        # A context hint is always scoped, even if an explicit address follows it.
        context = self._target(root, context_investigation_id, tenant, project, "current_task") if context_investigation_id else None
        text = content.strip()
        new = _NEW.match(text)
        if new:
            decision = ({"kind": "new_task", "basis": "explicit_new_task", "goal": new[1].strip()}
                        if new[1].strip() else self._clarify("请在“新任务：”后写明要调查的问题。"))
        elif match := _ID.match(text):
            try:
                decision = self._target(root, match[1].lower(), tenant, project, "explicit_task_id")
                if decision["task_id"] in excluded_task_ids:
                    decision = self._clarify("这条消息已被该任务退回，不能再次交回。")
            except NotFoundError:
                decision = self._clarify("这个任务不在当前会话中，请核对任务编号。")
        elif match := (_QUOTED.match(text) or _PLAIN.match(text)):
            label = _normalized(match[1])
            cards, incomplete = self._cards(root, tenant, project, excluded_task_ids)
            matches = [card for card in cards if len(label) >= 2 and label in _normalized(card["objective_excerpt"])]
            if len(matches) == 1 and not incomplete:
                decision = {"kind": "task", "basis": "literal_task_label", **matches[0]}
            elif matches:
                decision = self._clarify("这段任务名称还不能唯一确定归属，请选择目标任务或写更完整的名称。", matches, incomplete=incomplete)
            else:
                decision = self._clarify("没有找到可唯一匹配的任务。可以选择任务，或用“新任务：问题”另开调查。", cards, incomplete=incomplete)
        elif re.match(r'^(?:继续|补充|关于|接着|给)\s*(?:任务\s*)?[「“"]', text):
            decision = self._clarify("任务名称的引号不完整。可写“继续「任务名称」：补充内容”。")
        elif context:
            # Ordinary follow-ups keep the displayed task's stale-head guard.
            context["investigation_id"] = context_investigation_id
            decision = context
        else:
            cards, incomplete = self._cards(root, tenant, project, excluded_task_ids)
            decision = ({"kind": "task", "basis": "only_task", **cards[0]} if allow_only_task and len(cards) == 1 and not incomplete
                        else self._clarify("这条消息属于哪个任务？也可用“新任务：问题”开始独立调查。", cards, incomplete=incomplete))
        return {"conversation_id": root, "context_policy": "bounded_task_cards_no_worker_history",
                "router_version": "rules-v1", **decision}
