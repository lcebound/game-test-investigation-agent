"use strict";

function clearReturnedInputs() {
  state.returnedInputPage = null;
  state.returnedInputSerial = (state.returnedInputSerial || 0) + 1;
  if ($("input-returns-dialog").open) $("input-returns-dialog").close();
  clear($("input-returns-content"));
}

async function resolveReturnedInput(card, action, targetId = null) {
  if (state.busy) return;
  const generation = state.generation, root = state.rootId;
  setBusy(true);
  $("input-returns-content").querySelectorAll("button,select,input").forEach(e => e.disabled = true);
  try {
    await api(`/v1/conversations/${encodeURIComponent(root)}/input-returns/${encodeURIComponent(card.return_id)}/resolve`, {
      method: "POST", body: {expected_revision: card.revision, action,
        ...(targetId ? {target_investigation_id: targetId} : {})},
    });
    if (generation !== state.generation || root !== state.rootId) return;
    state.returnedInputPage = null;
    await refreshConversation(true);
    await refreshReturnedInputs();
  } catch (error) {
    if (generation === state.generation) toast(error.message, true);
  } finally {
    if (generation === state.generation) {
      setBusy(false);
      $("input-returns-content").querySelectorAll("button,select,input").forEach(e => e.disabled = false);
    }
  }
}

async function inspectReturnedInput(card, panel) {
  const root = state.rootId, generation = state.generation;
  try {
    const detail = await api(`/v1/conversations/${encodeURIComponent(root)}/input-returns/${encodeURIComponent(card.return_id)}`);
    if (generation !== state.generation || root !== state.rootId || !panel.isConnected) return;
    clear(panel);
    panel.append(node("pre", "returned-input-original", detail.content));
    const select = node("select");
    select.setAttribute("aria-label", "接收原消息的任务");
    const blank = node("option", "", "选择仍在进行中的任务");
    blank.value = "";
    select.append(blank);
    for (const target of state.taskCards || []) {
      if (terminal(target.status) || target.status === "canceling" || detail.excluded_task_ids.includes(target.task_id)) continue;
      const option = node("option", "", target.objective_excerpt);
      option.value = target.investigation_id;
      select.append(option);
    }
    const identifier = node("input");
    identifier.placeholder = "或填写目标调查编号（使用当前任务头）";
    identifier.maxLength = 96;
    identifier.setAttribute("aria-label", "其他目标调查编号");
    const attach = node("button", "outline-button", "转交原消息");
    attach.onclick = () => {
      const targetId = identifier.value.trim() || select.value;
      if (!targetId) return toast("请指定接收原消息的任务。", true);
      resolveReturnedInput(card, "attach", targetId);
    };
    panel.append(select, identifier, attach);
  } catch (error) { if (generation === state.generation) toast(error.message, true); }
}

async function refreshReturnedInputs(offset = 0) {
  const root = state.rootId, generation = state.generation, epoch = state.identityEpoch;
  if (!root || !$("input-returns-dialog").open) return;
  const serial = state.returnedInputSerial = (state.returnedInputSerial || 0) + 1;
  try {
    const page = await api(`/v1/conversations/${encodeURIComponent(root)}/input-returns?offset=${offset}`);
    if (generation !== state.generation || epoch !== state.identityEpoch || root !== state.rootId || serial !== state.returnedInputSerial) return;
    $("input-returns-summary").textContent = `还有 ${page.pending_count} 条补充等待重新归属。退回原因是 Worker 的判断；转交保留原文，目标任务沿用自己的权限和预算。`;
    const signature = JSON.stringify({offset, items: page.items});
    if (signature === state.returnedInputPage?.signature) return;
    state.returnedInputPage = {...page, signature};
    clear($("input-returns-content"));
    for (const card of page.items) {
      const row = node("article", "returned-input-card");
      row.append(node("p", "", card.content_excerpt), node("small", "", `Worker 退回原因（未独立核实）：${card.reason_excerpt}`));
      const panel = node("div", "returned-input-detail");
      const inspect = node("button", "outline-button", "查看原文并选择归属");
      inspect.disabled = !!card.unavailable;
      inspect.onclick = () => inspectReturnedInput(card, panel);
      const dismiss = node("button", "outline-button", "不再处理这条补充");
      dismiss.onclick = () => resolveReturnedInput(card, "dismiss");
      row.append(inspect, dismiss, panel);
      $("input-returns-content").append(row);
    }
    if (!page.items.length) $("input-returns-content").append(node("p", "", "当前页没有待归属消息。"));
    if (offset) {
      const first = node("button", "outline-button", "返回第一页");
      first.onclick = () => refreshReturnedInputs(0);
      $("input-returns-content").append(first);
    }
    if (page.next_offset !== null) {
      const more = node("button", "outline-button", "下一页");
      more.onclick = () => refreshReturnedInputs(page.next_offset);
      $("input-returns-content").append(more);
    }
  } catch (error) { if (generation === state.generation && epoch === state.identityEpoch) toast(error.message, true); }
}

function openReturnedInputs() {
  if (state.busy) return;
  if ($("overview-dialog").open) $("overview-dialog").close();
  state.returnedInputPage = null;
  $("input-returns-dialog").showModal();
  refreshReturnedInputs();
}

document.addEventListener("DOMContentLoaded", () => { $("input-returns-open").onclick = openReturnedInputs; });
