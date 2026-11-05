"""Question coverage derived from the existing plan, inputs and evidence ledger."""
from __future__ import annotations

import hashlib

from game_test_agent.storage import canonical_json


def input_fingerprint(inputs):
    """Bind a review to all current input identities and original content hashes."""
    identities = [(row["command_id"], row.get("sha256") or (
        hashlib.sha256(row["payload"]["content"].encode()).hexdigest()
        if "payload" in row else "unavailable")) for row in inputs]
    return hashlib.sha256(canonical_json(identities).encode()).hexdigest()


def question_coverage(plan, inputs, evidence):
    """Readable references are checked; answer semantics remain model assessments."""
    if not plan:
        return None
    stamp, rows = input_fingerprint(inputs), []
    # Legacy plans without user follow-ups remain usable. Plans with inputs
    # need an explicit review instead of silently inheriting current relevance.
    legacy_stamp = input_fingerprint([])
    for step in plan["steps"]:
        linked = set(step.get("evidence_ids", []) + step.get("counterevidence_ids", []))
        unavailable = sorted(linked - set(evidence))
        stale = plan.get("reviewed_inputs", {}).get(step["step_id"], legacy_stamp) != stamp
        state = "open"
        if step["status"] in {"blocked", "skipped"}:
            state = "blocked"
        elif linked or step.get("outcome"):
            state = "partial"
        if step["status"] == "completed":
            state = ("answered" if step.get("evidence_ids") and step.get("outcome")
                     and not (unavailable or stale or step.get("remaining_gaps")) else "partial")
        row = {"question_id": step["step_id"], "state": state}
        if stale:
            row["needs_review"] = True
        if unavailable:
            row["unavailable_evidence_ids"] = unavailable
        rows.append(row)
    steps = {step["step_id"]: step for step in plan["steps"]}
    eligible = [row for row in rows if row["state"] != "answered"
                and steps[row["question_id"]]["status"] != "skipped"]
    active = next((s["step_id"] for s in plan["steps"] if s["status"] == "in_progress"
                   and any(r["question_id"] == s["step_id"] for r in eligible)), None)
    fallback = next((row["question_id"] for row in eligible if row["state"] != "blocked"),
                    eligible[0]["question_id"] if eligible else None)
    return {"questions": rows, "next_question_id": active or fallback,
            "assessment": "model; readable references do not verify answers or acceptance"}


def question_evidence_links(plan):
    links = {}
    for step in plan["steps"] if plan else []:
        for field, role in (("evidence_ids", "question_ids"), ("counterevidence_ids", "counter_question_ids")):
            for eid in step.get(field, []):
                links.setdefault(eid, {}).setdefault(role, []).append(step["step_id"])
    return links


def report_question_progress(repository, view):
    """Rebuild at publication, including recovery paths that have no NativeMemory."""
    events = repository.list_recent_events_by_type(view.investigation_id, "mcp.native.plan_updated", limit=1)
    if not events:
        return None
    plan = events[0]["data"]
    inputs = repository.list_user_inputs(view.investigation_id)
    evidence = {e.evidence_id for e in repository.list_current_evidence(view.investigation_id)
                if e.retrieval_status == "ready"}
    coverage = question_coverage(plan, inputs, evidence)
    rows = {row["question_id"]: row for row in coverage["questions"]}
    lines, limitations = [], []
    labels = {"open": "未回答", "partial": "部分回答", "answered": "已回答（模型判断）", "blocked": "受阻/跳过"}
    for step in plan["steps"]:
        row = rows[step["step_id"]]
        gaps = list(step.get("remaining_gaps", []))
        if row.get("needs_review"):
            gaps.append("用户补充已变化，本问题尚未按当前输入复核")
        if row.get("unavailable_evidence_ids"):
            gaps.append("关联证据已不可读，需重新取证或修订答案")
        if row["state"] != "answered" and not gaps:
            gaps.append(step.get("outcome") or "尚未记录满足完成条件的答案及证据")
        line = f"- [{step['step_id']}] {step['question']}：{labels[row['state']]}"
        if step.get("outcome"):
            line += "；答复草稿（未核实）：" + step["outcome"]
        if step.get("counterevidence_ids"):
            line += "；反证引用：" + ", ".join(step["counterevidence_ids"])
        lines.append(line)
        if gaps:
            limitations.append(f"问题[{step['step_id']}] {step['question']}：" + "；".join(gaps))
    return {"summary": "问题进展（模型判断，不作为验收或语义验证）：\n" + "\n".join(lines),
            "limitations": limitations, "plan_revision": plan["revision"], **coverage}
