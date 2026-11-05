"use strict";

async function openHarnessPanel(offset = 0) {
  if (state.busy || !state.rootId) return;
  const root = state.rootId, investigation = state.id;
  const generation = state.generation, epoch = state.identityEpoch;
  const current = () => root === state.rootId && generation === state.generation && epoch === state.identityEpoch;
  const panel = $("harness-content");
  $("harness-dialog").showModal();
  clear(panel);
  panel.append(node("p", "", "正在读取事件与操作状态…"));
  try {
    const [events, operations, overview] = await Promise.all([
      api(`/v1/conversations/${encodeURIComponent(root)}/observations?offset=${offset}`),
      api(`/v1/investigations/${encodeURIComponent(investigation)}/operations`),
      api(`/v1/conversations/${encodeURIComponent(root)}/overview?limit=8`),
    ]);
    if (!current()) return;
    clear(panel);
    panel.append(node("h3", "", "外部观察事件"));
    if (!events.items.length) panel.append(node("p", "", "暂无外部观察事件。"));
    for (const item of events.items) {
      const row = node("article", "overview-card");
      const states = {pending: "等待自动归属", assessing: "正在判断归属", routed: "已交给调查任务", unresolved: "暂未找到明确的活动任务"};
      row.append(node("strong", "", item.summary_excerpt), node("p", "", states[item.state] || item.state));
      const detail = node("pre", "returned-input-original");
      detail.hidden = true;
      const read = node("button", "outline-button", "查看原事件");
      read.onclick = async () => {
        try {
          const value = await api(`/v1/conversations/${encodeURIComponent(root)}/observations/${encodeURIComponent(item.event_key)}`);
          if (current() && detail.isConnected) { detail.hidden = false; detail.textContent = JSON.stringify(value.event, null, 2); }
        } catch (error) { if (current()) toast(error.message, true); }
      };
      row.append(read, detail);
      if (item.state === "unresolved") {
        const retry = node("button", "outline-button", "重新判断归属");
        retry.onclick = async () => {
          if (!current()) return;
          retry.disabled = true;
          try {
            await api(`/v1/conversations/${encodeURIComponent(root)}/observations/${encodeURIComponent(item.event_key)}/retry`, {method: "POST"});
            if (current()) await openHarnessPanel(offset);
          } catch (error) { if (current()) { toast(error.message, true); retry.disabled = false; } }
        };
        row.append(retry);
      }
      panel.append(row);
    }
    if (events.next_offset !== null) {
      const next = node("button", "outline-button", "更多事件");
      next.onclick = () => { if (current()) openHarnessPanel(events.next_offset); };
      panel.append(next);
    }
    panel.append(node("h3", "", "当前任务的长操作"));
    if (!operations.items.length) panel.append(node("p", "", "没有长操作记录。"));
    for (const item of operations.items) {
      const states = {submitting: "提交中", running: "运行中", uncertain: "正在确认提交结果", completed: "结果待采集", collected: "已采集", canceled: "已结束", unresolved_result: "查询预算已用尽"};
      panel.append(node("p", "", `${item.tool_name} · ${states[item.state] || item.state} · 已查询 ${item.polls} 次`));
    }
    if (operations.has_more) panel.append(node("p", "form-note", "仅显示最近 16 项操作。"));
    if (operations.pending_cleanup.length) panel.append(node("p", "form-note", `${operations.pending_cleanup.length} 项资源仍待清理确认。`));
    panel.append(node("h3", "", "综合已结束的调查"));
    const tasks = new Map([...overview.results, ...(state.taskCards || [])].filter(card => terminal(card.status)).map(card => [card.task_id, card]));
    const selected = new Set();
    for (const card of tasks.values()) {
      const label = node("label", "");
      const checkbox = node("input"); checkbox.type = "checkbox";
      checkbox.onchange = () => checkbox.checked ? selected.add(card.task_id) : selected.delete(card.task_id);
      label.append(checkbox, document.createTextNode(card.objective_excerpt || card.task_id));
      panel.append(label);
    }
    const question = node("textarea"); question.maxLength = 1000;
    question.placeholder = "比较这些调查的共同现象、差异与尚缺的验证";
    question.setAttribute("aria-label", "综合问题");
    const output = node("section");
    const submit = node("button", "outline-button", "生成综合");
    submit.onclick = async () => {
      if (!current()) return;
      if (selected.size < 2 || selected.size > 8) return toast("请选择 2–8 个已结束任务。", true);
      submit.disabled = true;
      try {
        const value = await api(`/v1/conversations/${encodeURIComponent(root)}/synthesis`, {method: "POST", body: {task_ids: [...selected], ...(question.value.trim() ? {question: question.value.trim()} : {})}});
        if (!current()) return;
        clear(output);
        output.append(node("p", "form-note", "跨任务推断仍需验证，不代表已确认共同根因。"));
        for (const item of value.findings) output.append(node("p", "", item.statement), node("small", "", item.source_refs.join(" · ")));
        for (const gap of value.open_questions) output.append(node("p", "", `待验证：${gap}`));
        for (const source of value.sources) {
          const read = node("button", "text-link", `查看来源：${source.objective}`);
          read.onclick = async () => {
            try { const report = await api(source.report_url); if (current()) output.append(node("pre", "returned-input-original", JSON.stringify(report, null, 2))); }
            catch (error) { if (current()) toast(error.message, true); }
          };
          output.append(read);
        }
      } catch (error) { if (current()) toast(error.message, true); }
      finally { if (current()) submit.disabled = false; }
    };
    panel.append(question, submit, output);
  } catch (error) { if (current()) { clear(panel); panel.append(node("p", "", error.message)); } }
}

document.addEventListener("DOMContentLoaded", () => {
  $("harness-open").onclick = () => openHarnessPanel();
});
