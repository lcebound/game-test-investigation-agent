"use strict";

// Conversation and browser presentation helpers. No platform credentials are persisted here.
function workspaceKey(name, root = "") {
  const a = state.identity;
  return `trace.${name}.${JSON.stringify([a.tenant, a.project, a.principal, root])}`;
}
function cacheRead(key, fallback = "") {
  try {
    return localStorage.getItem(key) ?? fallback;
  } catch {
    return fallback;
  }
}
function cacheWrite(key, value) {
  try {
    value ? localStorage.setItem(key, value) : localStorage.removeItem(key);
  } catch {}
}
function saveDraft() {
  cacheWrite(workspaceKey("draft", draftScope()), $("prompt").value);
}
function restoreDraft() {
  $("prompt").value = cacheRead(workspaceKey("draft", draftScope()));
}
function draftScope() {
  if (!state.rootId) return "new";
  if (state.newTask) return `${state.rootId}/new-task`;
  return state.taskId && state.taskId !== state.rootId
    ? `${state.rootId}/${state.taskId}` : state.rootId;
}
function syncStatus(text = "") {
  $("sync-status").hidden = !text;
  $("sync-status").querySelector("span").textContent = text;
}
function stopStreaming() {
  state.stream?.abort();
  state.stream = null;
  clearTimeout(state.timer);
}
function resetConversation(initial) {
  if (state.rootId || state.initialized) saveDraft();
  stopStreaming();
  state.rootId = null;
  document.dispatchEvent(new CustomEvent("trace:conversation", {detail:{root:null}}));
  state.taskId = null;
  state.newTask = false;
  state.taskCards = [];
  state.historySelections = [];
  renderHistorySelections();
  state.conversation = null;
  state.thread = [];
  state.inspectId = null;
  state.supplementalInspection = null;
  state.overview = null;
  clearMessageRouting();
  clearReturnedInputs();
  if ($("overview-dialog").open) $("overview-dialog").close();
  clear($("overview-content"));
  if ($("recall-dialog").open) $("recall-dialog").close();
  clear($("recall-results"));
  state.olderBefore = null;
  state.selected = null;
  state.refreshSerial = (state.refreshSerial || 0) + 1;
  if ($("evidence-dialog").open) $("evidence-dialog").close();
  $("conversation-actions").hidden = true;
  $("run-switcher").hidden = true;
  $("conversation-tasks").hidden = true;
  $("prompt").value = "";
  renderSelected();
  syncStatus();
  if (!initial) {
    cacheWrite(workspaceKey("active"), "");
    history.replaceState(null, "", location.pathname);
  }
}
function historyView(summary) {
  const v = summary.latest_investigation;
  return {
    ...v,
    conversation: summary,
    request: {
      ...v.request,
      objective: { ...v.request.objective, description: summary.title },
    },
  };
}
function renderConversationHistory() {
  clear($("history"));
  for (const view of state.history) {
    const c = view.conversation,
      row = node("div", "history-row");
    const button = node(
      "button",
      "history-item" + (c.conversation_id === state.rootId ? " active" : ""),
    );
    button.append(icon("chat"));
    const body = node("div");
    body.append(
      node("strong", "", `${c.pinned ? "★ " : ""}${c.title}`),
      node(
        "small",
        "",
        `${date(c.updated_at)} · ${conversationStatus(c) || labels[view.status] || view.status} · ${c.investigation_count} 次调查`,
      ),
    );
    button.append(body);
    button.title = `${c.title}\n${conversationStatus(c)}\n${c.investigation_count} 次调查`;
    button.onclick = () => openConversation(c.conversation_id);
    const menu = node("button", "history-menu", "···");
    menu.setAttribute("aria-label", `管理会话：${c.title}`);
    menu.onclick = () => editConversation(c);
    row.append(button, menu);
    $("history").append(row);
  }
  if (!state.history.length)
    $("history").append(
      node(
        "p",
        "history-empty",
        state.archivedView
          ? "没有已归档的会话。"
          : $("history-search").value.trim()
            ? "未找到匹配的会话。"
            : "从一个测试问题开始。",
      ),
    );
  $("history-count").textContent = state.history.length;
  $("load-more").hidden = state.historyCursor === null;
}
async function fetchConversationHistory(more = false) {
  const serial = (state.historySerial = (state.historySerial || 0) + 1),
    epoch = state.identityEpoch;
  const query = $("history-search").value.trim(),
    offset = more ? state.historyCursor || 0 : 0;
  const page = await api(
    `/v1/conversations?limit=50&offset=${offset}&archived=${!!state.archivedView}&query=${encodeURIComponent(query)}`,
  );
  if (serial !== state.historySerial || epoch !== state.identityEpoch) return;
  const items = page.items.map(historyView);
  state.history = more
    ? [
        ...new Map(
          [...state.history, ...items].map((v) => [
            v.conversation.conversation_id,
            v,
          ]),
        ).values(),
      ]
    : items;
  state.historyCursor = page.next_offset;
  renderConversationHistory();
}
async function restoreConversation() {
  const hash = new URLSearchParams(location.hash.slice(1)).get("conversation");
  const id = hash || cacheRead(workspaceKey("active"));
  if (id && !state.rootId) await openConversation(id);
}
async function openConversation(id) {
  if (state.busy) return;
  saveDraft();
  stopStreaming();
  state.generation++;
  state.historySelections = [];
  renderHistorySelections();
  state.rootId = id;
  state.taskId = cacheRead(workspaceKey("task", id)) || id;
  state.newTask = false;
  state.taskCards = [];
  state.id = id;
  state.snapshot = null;
  state.thread = [];
  state.conversation = null;
  $("chat-title").textContent = "正在读取会话…";
  $("task-state").hidden = true;
  $("stop").hidden = true;
  $("conversation-actions").hidden = true;
  state.inspectId = null;
  state.supplementalInspection = null;
  state.olderBefore = null;
  state.pendingMutation = null;
  $("welcome").hidden = true;
  $("messages").hidden = false;
  clear($("messages"));
  $("messages").append(node("p", "message-text", "正在读取完整会话…"));
  closeDrawers();
  restoreDraft();
  renderHistory();
  await refreshConversation(true);
}
function mergeThread(items) {
  state.thread = [
    ...new Map(
      [...(state.thread || []), ...items].map((s) => [
        s.investigation.investigation_id,
        s,
      ]),
    ).values(),
  ].sort(
    (a, b) =>
      a.investigation.created_at.localeCompare(b.investigation.created_at) ||
      a.investigation.investigation_id.localeCompare(
        b.investigation.investigation_id,
      ),
  );
}
function renderThread() {
  const scroller = $("conversation-scroll"),
    oldTop = scroller.scrollTop;
  const atBottom = scroller.scrollHeight - oldTop - scroller.clientHeight < 100;
  const opened = new Set(
    [...$("messages").querySelectorAll("details[open]")].map(
      (e) => e.dataset.decisionId,
    ),
  );
  const previousDetails = new Set(
    [...$("messages").querySelectorAll("details")].map((e) => e.dataset.decisionId),
  );
  clear($("messages"));
  if (state.olderBefore) {
    const older = node(
      "button",
      "older-messages outline-button",
      "加载更早的调查",
    );
    older.onclick = async () => {
      older.disabled = true;
      const generation = state.generation,
        height = scroller.scrollHeight;
      try {
        const data = await api(
          `/v1/conversations/${encodeURIComponent(state.rootId)}/workspace?task_id=${encodeURIComponent(state.taskId)}&before=${encodeURIComponent(state.olderBefore)}`,
        );
        if (generation !== state.generation) return;
        mergeThread(data.investigations);
        state.olderBefore = data.older_before;
        renderThread();
        scroller.scrollTop += scroller.scrollHeight - height;
        renderRunSwitcher();
      } catch (e) {
        toast(e.message, true);
        older.disabled = false;
      }
    };
    $("messages").append(older);
  }
  for (const s of state.thread) {
    const v = s.investigation,
      separator = node("div", "investigation-divider");
    separator.append(
      node(
        "span",
        "",
        `${date(v.created_at)} ${time(v.created_at)} · ${labels[v.status] || v.status}`,
      ),
    );
    const inspect = node("button", "text-link", "查看这次调查 ↗");
    inspect.onclick = () => {
      state.inspectId = v.investigation_id;
      renderInspection();
      showTab("execution");
      openContext();
    };
    separator.append(inspect);
    $("messages").append(separator);
    renderMessages(s, true);
  }
  $("messages")
    .querySelectorAll("details")
    .forEach((e) => {
      if (previousDetails.has(e.dataset.decisionId)) e.open = opened.has(e.dataset.decisionId);
    });
  scroller.scrollTop = atBottom ? scroller.scrollHeight : oldTop;
}
function renderRunSwitcher() {
  const select = $("run-selector");
  clear(select);
  $("run-switcher").hidden = !state.thread.length;
  state.thread.forEach((s, index) => {
    const v = s.investigation,
      o = node(
        "option",
        "",
        `${index + 1}. ${v.request.objective.description.slice(0, 30)} · ${labels[v.status] || v.status}`,
      );
    o.value = v.investigation_id;
    select.append(o);
  });
  if (state.supplementalInspection && !state.thread.some(s => s.investigation.investigation_id === state.supplementalInspection.investigation.investigation_id)) {
    const old = state.supplementalInspection.investigation;
    const option = node("option", "", `清理调查 · ${old.request.objective.description.slice(0, 30)}`);
    option.value = old.investigation_id;
    select.append(option);
  }
  select.value = state.inspectId || state.id;
}
function renderInspection() {
  const s =
    state.thread.find(
      (s) => s.investigation.investigation_id === state.inspectId,
    ) || (state.supplementalInspection?.investigation.investigation_id === state.inspectId ? state.supplementalInspection : null) || state.snapshot;
  if (!s) return;
  renderExecution(s);
  renderEvidence(s);
  renderRunSwitcher();
}
function hydrateConditions(v) {
  const c = v.request.constraints,
    b = v.request.budget;
  state.options = {
    ...state.options,
    build: v.request.build_ref?.id || "",
    target: c.action_target_selector?.target_kind || "",
    runs: b.max_real_runs,
    rounds: b.max_rounds,
    reads: b.max_tool_reads,
    minutes: b.max_device_minutes,
    seconds: b.max_wall_seconds,
    testId: c.action_parameters?.test_ref?.test_id || "",
    criteria: (v.request.objective.acceptance_criteria || []).join("\n"),
    parameters: c.action_parameters
      ? JSON.stringify(c.action_parameters, null, 2)
      : "",
    constraints: JSON.stringify(
      Object.fromEntries(
        Object.entries(c).filter(
          ([key]) =>
            ![
              "action_parameters",
              "action_target_selector",
              "provider_allowlist",
              "followup_reference",
              "recall_references",
            ].includes(key),
        ),
      ),
      null,
      2,
    ),
  };
  $("budget-label").textContent = `最多 ${state.options.runs} 次运行`;
  const previousProvider = state.provider;
  const provider = c.provider_allowlist?.find((id) =>
    state.config.providers.some((p) => p.provider_id === id),
  );
  if (provider) {
    state.provider = provider;
    $("provider").value = provider;
  }
  if (provider && provider !== previousProvider) probe();
}
async function refreshConversation(first = false) {
  const generation = state.generation,
    root = state.rootId || state.id;
  if (!root || document.hidden) return;
  const serial = (state.refreshSerial = (state.refreshSerial || 0) + 1);
  try {
    const supplementalId = state.supplementalInspection?.investigation.investigation_id;
    const [data, tasks, supplemental] = await Promise.all([
      api(`/v1/conversations/${encodeURIComponent(root)}/workspace?task_id=${encodeURIComponent(state.taskId || root)}`),
      api(`/v1/conversations/${encodeURIComponent(root)}/tasks`),
      supplementalId ? api(`/v1/investigations/${encodeURIComponent(supplementalId)}/workspace`) : Promise.resolve(null),
    ]);
    if (generation !== state.generation || serial !== state.refreshSerial)
      return;
    const before = JSON.stringify(state.thread),
      initial = !state.thread.length;
    const c = data.conversation;
    state.rootId = c.conversation_id;
    state.conversation = c;
    if (supplemental && state.supplementalInspection?.investigation.investigation_id === supplementalId)
      state.supplementalInspection = supplemental;
    renderConversationSummary();
    state.taskId = data.task_id;
    state.id = data.task_head_id;
    state.taskCards = tasks.items;
    state.tasksNext = tasks.next_offset;
    renderTaskSelector();
    document.dispatchEvent(new CustomEvent("trace:conversation", {detail:{root:state.rootId}}));
    cacheWrite(workspaceKey("task", c.conversation_id), state.taskId);
    mergeThread(data.investigations);
    state.snapshot = state.thread.find(
      (s) => s.investigation.investigation_id === state.id,
    );
    if (!state.snapshot) throw new Error("当前调查投影尚未就绪。");
    if (initial) {
      state.olderBefore = data.older_before;
      hydrateConditions(state.snapshot.investigation);
    }
    $("welcome").hidden = true;
    $("messages").hidden = false;
    $("chat-title").textContent = c.title;
    $("conversation-actions").hidden = false;
    $("pin-chat").textContent = c.pinned ? "★" : "☆";
    $("pin-chat").setAttribute("aria-pressed", String(c.pinned));
    $("archive-chat").title = c.archived ? "恢复会话" : "归档会话";
    $("archive-chat").setAttribute("aria-label", $("archive-chat").title);
    const v = state.snapshot.investigation;
    clear($("task-state"));
    $("task-state").hidden = false;
    $("task-state").append(
      node(
        "span",
        "small-dot" + (terminal(v.status) ? "" : " connected polling-dot"),
      ),
      node(
        "span",
        "",
        `${c.archived ? "已归档 · " : ""}${labels[v.status] || v.status}${v.resolution ? ` · ${labels[v.resolution] || v.resolution}` : ""}`,
      ),
    );
    $("stop").hidden = terminal(v.status);
    $("stop").disabled = state.busy || v.status === "canceling";
    $("prompt").placeholder = state.newTask
      ? "描述独立任务，将使用当前显示的测试条件与预算…" : c.archived
      ? "恢复会话后可继续对话…"
      : terminal(v.status)
        ? "继续追问。下一次调查将沿用显示的测试条件与预算…"
        : "补充环境、条件或期望结果…";
    $("send").disabled = state.busy || c.archived || (!state.newTask && v.status === "canceling");
    if (first || supplemental || before !== JSON.stringify(state.thread)) {
      renderThread();
      renderInspection();
    }
    const index = state.history.findIndex(
      (v) => v.conversation.conversation_id === c.conversation_id,
    );
    if (index >= 0 && c.archived !== !!state.archivedView)
      state.history.splice(index, 1);
    else if (index >= 0) state.history[index] = historyView(c);
    else if (
      c.archived === !!state.archivedView &&
      !$("history-search").value.trim()
    )
      state.history.unshift(historyView(c));
    renderHistory();
    syncStatus();
    cacheWrite(workspaceKey("active"), c.conversation_id);
    history.replaceState(
      null,
      "",
      `${location.pathname}#conversation=${encodeURIComponent(c.conversation_id)}`,
    );
    if (!state.stream) startStreaming(c.conversation_id);
    if ($("overview-dialog").open) refreshOverview();
    if ($("input-returns-dialog").open) refreshReturnedInputs();
  } catch (e) {
    if (generation !== state.generation) return;
    syncStatus(e.message);
    if ([401, 403, 404].includes(e.status)) {
      stopStreaming();
      return;
    }
    clearTimeout(state.timer);
    state.timer = setTimeout(() => refreshConversation(), 5000);
  }
}
function requestHeaders() {
  const a = state.identity,
    headers = {
      "X-Principal-ID": a.principal,
      "X-Tenant-ID": a.tenant,
      "X-Project-ID": a.project,
      "X-Scopes": a.scopes,
    };
  if (a.token) headers.Authorization = `Bearer ${a.token}`;
  return headers;
}
async function startStreaming(root) {
  if (document.hidden || state.stream) return;
  const controller = new AbortController(),
    generation = state.generation;
  state.stream = controller;
  let version = "",
    attempt = 0;
  try {
    while (!controller.signal.aborted && generation === state.generation) {
      try {
        const response = await fetch(
          `/v1/conversations/${encodeURIComponent(root)}/stream?after=${encodeURIComponent(version)}`,
          { headers: requestHeaders(), signal: controller.signal },
        );
        if (!response.ok) {
          if ([401, 403, 404].includes(response.status)) {
            syncStatus("会话同步未授权，请检查身份设置。");
            break;
          }
          throw new Error("连接暂时中断");
        }
        const reader = response.body.getReader(),
          decoder = new TextDecoder();
        let buffer = "";
        attempt = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary;
          while ((boundary = buffer.indexOf("\n\n")) >= 0) {
            const block = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const data = block
              .split("\n")
              .find((line) => line.startsWith("data: "));
            if (data) {
              const event = JSON.parse(data.slice(6));
              version = event.version;
              if (generation === state.generation) await refreshConversation();
            }
          }
        }
      } catch (error) {
        if (controller.signal.aborted) break;
        syncStatus("实时同步暂时中断，正在自动重连；对话内容已保留。");
        await refreshConversation();
        await new Promise((resolve) =>
          setTimeout(resolve, Math.min(1000 * 2 ** attempt++, 5000)),
        );
      }
    }
  } finally {
    if (state.stream === controller) state.stream = null;
  }
}
async function sendConversationMessage() {
  const content = $("prompt").value.trim();
  if (!content || state.busy || !state.config) return;
  if (state.conversation?.archived) {
    toast("先恢复会话，再继续发送。", true);
    return;
  }
  let request;
  try {
    if (!state.id || state.newTask) {
      request = {
        path: state.newTask
          ? `/v1/conversations/${encodeURIComponent(state.rootId)}/tasks` : "/v1/investigations",
        body: investigationBody(content),
      };
    } else {
      if (!state.snapshot) throw new Error("会话尚未加载完毕。");
      request = {
        path: `/v1/conversations/${encodeURIComponent(state.rootId)}/messages`,
        body: {
          content,
          expected_investigation_id: state.id,
          ...(terminal(state.snapshot.investigation.status)
            ? {
                budget: budget(),
                build_ref: state.options.build
                  ? { id: state.options.build }
                  : null,
                constraints: constraints(),
                acceptance_criteria: state.options.criteria
                  .split("\n")
                  .map((s) => s.trim())
                  .filter(Boolean),
              }
            : {}),
        },
      };
    }
    if (state.id && !state.newTask && state.config.features?.conversation_message_routing)
      request.routing = {base: `/v1/conversations/${encodeURIComponent(state.rootId)}`,
        context: state.id, bound: $("auto-route").checked ? null : state.id, newTaskBody: investigationBody(content)};
  } catch (e) {
    toast(e.message, true);
    return;
  }
  const intent = JSON.stringify({
    root: state.rootId,
    task: state.taskId,
    newTask: !!state.newTask,
    autoRoute: $("auto-route").checked,
    historySelections: state.historySelections,
    content,
    options: state.options,
    provider: state.provider,
    selected: state.selected?.test_ref,
    epoch: state.identityEpoch,
  });
  if (state.pendingMutation?.intent === intent)
    request = state.pendingMutation.request;
  else {
    request.key = crypto.randomUUID();
    state.pendingMutation = { intent, request };
  }
  const generation = state.generation;
  setBusy(true);
  saveDraft();
  try {
    if (!await resolveMessageRoute(request, content, generation)) return;
    const result = await api(request.path, {
      method: "POST",
      body: request.body,
      key: request.key,
    });
    if (generation !== state.generation) return;
    state.pendingMutation = null;
    state.historySelections = [];
    renderHistorySelections();
    if ($("prompt").value.trim() === content) $("prompt").value = "";
    saveDraft();
    const independent = !!state.newTask || request.route?.kind === "new_task" ||
      (request.route?.kind === "task" && request.route.task_id !== state.taskId);
    state.newTask = false;
    if (result.investigation_id) {
      state.id = result.investigation_id;
      state.rootId = result.investigation_id;
      state.taskId = result.investigation_id;
    } else {
      state.id = result.investigation.investigation_id;
      state.rootId = result.conversation_id;
      if (result.task_id) state.taskId = result.task_id;
      else if (request.route?.kind === "task") state.taskId = request.route.task_id;
    }
    if (independent) {
      state.thread = [];
      state.supplementalInspection = null;
      state.olderBefore = null;
      restoreDraft();
    }
    state.inspectId = null;
    await refreshConversation(true);
    await fetchConversationHistory();
  } catch (e) {
    toast(e.message, true);
    if (e.status === 409) {
      state.pendingMutation = null;
      await refreshConversation(true);
    }
  } finally {
    setBusy(false);
  }
}
function renderTaskSelector() {
  $("conversation-tasks").hidden = !state.rootId;
  const select = $("task-selector");
  clear(select);
  const cards = [...(state.taskCards || [])];
  if (!cards.some((card) => card.task_id === state.taskId))
    cards.push({task_id: state.taskId, objective_excerpt: "当前任务"});
  for (const card of cards) {
    const option = node("option", "", `${card.objective_excerpt} · ${labels[card.status] || card.status || ""}`);
    option.value = card.task_id;
    select.append(option);
  }
  select.value = state.taskId;
  select.disabled = state.busy;
  $("auto-route").disabled = state.busy || !!state.newTask;
  $("new-task").disabled = state.busy || !!state.conversation?.archived;
  $("new-task").textContent = state.newTask ? "返回当前任务" : "新增独立任务";
  $("new-task").setAttribute("aria-pressed", String(!!state.newTask));
  $("tasks-more").hidden = state.tasksNext === null;
}
async function selectTask(taskId) {
  if (state.busy) return;
  saveDraft();
  stopStreaming();
  state.generation++;
  state.historySelections = [];
  renderHistorySelections();
  state.taskId = taskId;
  state.newTask = false;
  state.thread = [];
  state.snapshot = null;
  state.inspectId = null;
  state.supplementalInspection = null;
  state.olderBefore = null;
  state.pendingMutation = null;
  clearMessageRouting();
  clearReturnedInputs();
  restoreDraft();
  await refreshConversation(true);
}
function editConversation(summary = state.conversation) {
  if (!summary) return;
  state.editingConversation = summary;
  const f = $("conversation-form");
  f.elements.title.value = summary.title;
  f.elements.pinned.checked = summary.pinned;
  f.elements.archived.checked = summary.archived;
  $("conversation-dialog").showModal();
}
async function updateConversation(summary, fields) {
  const epoch = state.identityEpoch;
  try {
    const updated = await api(
      `/v1/conversations/${encodeURIComponent(summary.conversation_id)}`,
      {
        method: "PATCH",
        body: { expected_revision: summary.revision, ...fields },
      },
    );
    if (epoch !== state.identityEpoch) return;
    if (state.rootId === updated.conversation_id) {
      state.conversation = updated;
      await refreshConversation(true);
    }
    await fetchConversationHistory();
    return updated;
  } catch (e) {
    toast(e.message, true);
    if (e.status === 409) {
      await fetchConversationHistory();
      if (state.rootId) await refreshConversation();
    }
  }
}
function closeDrawers() {
  $("sidebar").classList.remove("open");
  $("inspector").classList.remove("open");
  $("drawer-backdrop").hidden = true;
  $("context-toggle").setAttribute("aria-expanded", "false");
}
function openContext() {
  if (!matchMedia("(max-width:1100px)").matches) return;
  $("sidebar").classList.remove("open");
  $("inspector").classList.add("open");
  $("drawer-backdrop").hidden = false;
  $("context-toggle").setAttribute("aria-expanded", "true");
}
async function authenticatedBlob(path) {
  const response = await fetch(path, { headers: requestHeaders() });
  if (!response.ok)
    throw new Error(
      `无法读取文件（${response.status}），请检查权限与证据完整性。`,
    );
  return response.blob();
}
function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob),
    a = node("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function clearEvidenceImage() {
  if (state.evidenceImageUrl) URL.revokeObjectURL(state.evidenceImageUrl);
  state.evidenceImageUrl = null;
  $("evidence-image").hidden = true;
  $("evidence-image").removeAttribute("src");
}
async function previewEvidenceImage(context) {
  try {
    const blob = await authenticatedBlob(
      `/v1/investigations/${encodeURIComponent(context.id)}/evidence/${encodeURIComponent(context.item.evidence_id)}/download`,
    );
    if (context !== state.evidence || !$("evidence-dialog").open) return;
    state.evidenceImageUrl = URL.createObjectURL(
      new Blob([blob], { type: context.item.mime_type }),
    );
    $("evidence-image").src = state.evidenceImageUrl;
    $("evidence-image").hidden = false;
    $("evidence-content").textContent = "";
  } catch (e) {
    if (context === state.evidence)
      $("evidence-content").textContent = e.message;
  }
}
function inlineMarkdown(target, text) {
  const pattern = /(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^\s)]+\))/g;
  let previous = 0;
  for (const match of text.matchAll(pattern)) {
    target.append(document.createTextNode(text.slice(previous, match.index)));
    const token = match[0];
    if (token.startsWith("**"))
      target.append(node("strong", "", token.slice(2, -2)));
    else if (token.startsWith("`"))
      target.append(node("code", "", token.slice(1, -1)));
    else {
      const end = token.indexOf("]("),
        label = token.slice(1, end),
        url = token.slice(end + 2, -1);
      try {
        const parsed = new URL(url);
        if (!["http:", "https:"].includes(parsed.protocol)) throw new Error();
        const a = node("a", "", label);
        a.href = parsed.href;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        target.append(a);
      } catch {
        target.append(document.createTextNode(token));
      }
    }
    previous = match.index + token.length;
  }
  target.append(document.createTextNode(text.slice(previous)));
}
function renderReportCard(snapshot) {
  const r = snapshot.report.report,
    card = node("div", "report-card"),
    heading = node("div", "report-card-heading");
  const level = {
    unknown: "尚未确认",
    observation: "已取得观察，根因待验证",
    reproduced: "已复现",
    bottleneck_supported: "瓶颈证据支持",
    root_cause_confirmed: "根因已确认",
  };
  heading.append(
    node("strong", "", "本轮调查结果"),
    node(
      "span",
      "badge" + (r.resolution === "inconclusive" ? " warn" : ""),
      labels[r.resolution] || r.resolution,
    ),
  );
  card.append(heading, node("p", "message-text", r.summary));
  const facts = node("div", "report-facts");
  facts.append(
    node("span", "", `${snapshot.runs.length} 次 Agent 发起运行`),
    node("span", "", `${snapshot.evidence.length} 份证据`),
    node("span", "", level[r.conclusion_level] || r.conclusion_level),
  );
  card.append(facts);
  const section = (title, items) => {
    if (!items?.length) return;
    card.append(node("h3", "", title));
    const list = node("ul");
    items.slice(0, 5).forEach((item) => list.append(node("li", "", item)));
    card.append(list);
  };
  section(
    r.claims.length && r.claims.every((c) => c.basis === "evidence_excerpt" && c.text === c.supporting_excerpt) ? "已核对的原文引用" : "结论依据",
    r.claims.map((claim) => {
      const check = claim.verification_details;
      if (!check) return claim.text;
      const provenance = { verified: "已核验", provider_declared: "平台声明", unknown: "未知" };
      const support = { deterministic: "确定性校验通过", literal_only: "仅原文一致", not_checked: "未独立核验" };
      return `${claim.text}（引用：${check.citation === "verified" ? "已核对" : "未核对"}；来源：${provenance[check.provenance] || "未知"}；主张支持：${support[check.support] || "未独立核验"}；因果：${check.causality === "verified" ? "已验证" : "未验证"}）`;
    }),
  );
  section("待补充的问题", r.unresolved_questions);
  section("建议下一步", r.recommended_actions);
  if (snapshot.evidence.length) {
    const evidence = node("button", "text-link", "查看本轮证据 ↗");
    evidence.onclick = () => {
      state.inspectId = snapshot.investigation.investigation_id;
      renderInspection();
      showTab("evidence");
      openContext();
    };
    card.append(evidence);
  }
  const details = node("details", "full-report");
  details.dataset.decisionId =
    "report-" + snapshot.investigation.investigation_id;
  details.append(node("summary", "", "查看完整报告与记录信息"));
  const body = node("div", "report-block");
  safeMarkdown(body, snapshot.report.markdown);
  details.append(body);
  card.append(details);
  const trajectory = node("button", "text-link", "导出调查轨迹 ↗");
  trajectory.onclick = async () => {
    try {
      const data = await api(`/v1/investigations/${encodeURIComponent(snapshot.investigation.investigation_id)}/trajectory`);
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `${snapshot.investigation.investigation_id}-trajectory.json`;
      anchor.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) { toast(error.message, true); }
  };
  card.append(trajectory);
  return card;
}
function safeMarkdown(target, text) {
  const lines = text.split("\n");
  let code = null,
    list = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith("```")) {
      if (code) code = null;
      else {
        code = node("pre");
        target.append(code);
      }
      list = null;
      continue;
    }
    if (code) {
      code.textContent += line + "\n";
      continue;
    }
    if (line.includes("|") && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || "")) {
      const wrap = node("div", "table-wrap"),
        table = node("table");
      wrap.append(table);
      target.append(wrap);
      const cells = (value) =>
        value
          .trim()
          .replace(/^\||\|$/g, "")
          .split("|")
          .map((v) => v.trim());
      const head = node("tr");
      cells(line).forEach((v) => {
        const c = node("th");
        inlineMarkdown(c, v);
        head.append(c);
      });
      table.append(head);
      i++;
      while (lines[i + 1]?.includes("|") && lines[i + 1].trim()) {
        const row = node("tr");
        cells(lines[++i]).forEach((v) => {
          const c = node("td");
          inlineMarkdown(c, v);
          row.append(c);
        });
        table.append(row);
      }
      list = null;
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.+)/),
      li = line.match(/^\s*[-*]\s+(.+)/);
    if (h) {
      const e = node("h" + Math.min(h[1].length, 3));
      inlineMarkdown(e, h[2]);
      target.append(e);
      list = null;
    } else if (li) {
      if (!list) {
        list = node("ul");
        target.append(list);
      }
      const e = node("li");
      inlineMarkdown(e, li[1]);
      list.append(e);
    } else {
      list = null;
      if (line.trim()) {
        const e = node("p");
        inlineMarkdown(e, line);
        target.append(e);
      }
    }
  }
}
document.addEventListener("DOMContentLoaded", () => {
  $("task-selector").onchange = (event) => selectTask(event.target.value);
  $("new-task").onclick = () => {
    if (state.busy || state.conversation?.archived) return;
    saveDraft();
    state.newTask = !state.newTask;
    state.historySelections = [];
    renderHistorySelections();
    state.pendingMutation = null;
    restoreDraft();
    renderTaskSelector();
    setBusy(false);
    $("prompt").placeholder = state.newTask
      ? "描述独立任务，将使用当前显示的测试条件与预算…" : "继续当前任务…";
    $("prompt").focus();
  };
  $("tasks-more").onclick = async () => {
    const generation = state.generation;
    if (state.tasksNext === null) return;
    try {
      const page = await api(`/v1/conversations/${encodeURIComponent(state.rootId)}/tasks?offset=${state.tasksNext}`);
      if (generation !== state.generation) return;
      state.taskCards = [...new Map([...state.taskCards, ...page.items].map(card => [card.task_id, card])).values()];
      state.tasksNext = page.next_offset;
      renderTaskSelector();
    } catch (error) { toast(error.message, true); }
  };
  state.identityEpoch = state.identityEpoch || 0;
  state.archivedView = false;
  $("prompt").addEventListener("input", () => {
    saveDraft();
    $("prompt").style.height = "auto";
    $("prompt").style.height = Math.min($("prompt").scrollHeight, 180) + "px";
  });
  let searchTimer;
  $("history-search").oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(
      () => fetchConversationHistory().catch((e) => toast(e.message, true)),
      250,
    );
  };
  $("history-archived").onclick = () => {
    state.archivedView = !state.archivedView;
    $("history-archived").setAttribute(
      "aria-pressed",
      String(state.archivedView),
    );
    fetchConversationHistory().catch((e) => toast(e.message, true));
  };
  $("rename-chat").onclick = () => editConversation();
  $("pin-chat").onclick = () =>
    state.conversation &&
    updateConversation(state.conversation, {
      pinned: !state.conversation.pinned,
    });
  $("archive-chat").onclick = () =>
    state.conversation &&
    updateConversation(state.conversation, {
      archived: !state.conversation.archived,
    });
  $("conversation-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = e.currentTarget,
      b = f.querySelector(".primary-button");
    b.disabled = true;
    try {
      if (
        await updateConversation(state.editingConversation, {
          title: f.elements.title.value.trim(),
          pinned: f.elements.pinned.checked,
          archived: f.elements.archived.checked,
        })
      )
        $("conversation-dialog").close();
    } finally {
      b.disabled = false;
    }
  };
  $("run-selector").onchange = () => {
    state.inspectId = $("run-selector").value;
    renderInspection();
  };
  $("context-toggle").onclick = () =>
    $("inspector").classList.contains("open") ? closeDrawers() : openContext();
  $("context-close").onclick = closeDrawers;
  $("drawer-backdrop").onclick = closeDrawers;
  $("menu").onclick = () => {
    const show = !$("sidebar").classList.contains("open");
    closeDrawers();
    $("sidebar").classList.toggle("open", show);
    $("drawer-backdrop").hidden = !show;
  };
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawers();
  });
  $("retry-sync").onclick = () =>
    state.rootId ? refreshConversation(true) : initialize();
  $("evidence-download").onclick = async () => {
    const c = state.evidence;
    if (!c) return;
    $("evidence-download").disabled = true;
    try {
      saveBlob(
        await authenticatedBlob(
          `/v1/investigations/${encodeURIComponent(c.id)}/evidence/${encodeURIComponent(c.item.evidence_id)}/download`,
        ),
        c.item.evidence_id,
      );
    } catch (e) {
      toast(e.message, true);
    } finally {
      $("evidence-download").disabled = false;
    }
  };
  $("evidence-dialog").addEventListener("close", () => {
    state.evidence = null;
    clearEvidenceImage();
  });
  const applyTheme = (theme) => {
    document.body.dataset.theme = theme;
    $("theme-toggle").setAttribute(
      "aria-label",
      theme === "dark" ? "切换浅色主题" : "切换深色主题",
    );
  };
  applyTheme(cacheRead("trace.theme", "light"));
  $("theme-toggle").onclick = () => {
    const theme = document.body.dataset.theme === "dark" ? "light" : "dark";
    cacheWrite("trace.theme", theme);
    applyTheme(theme);
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      saveDraft();
      stopStreaming();
    } else if (state.rootId) {
      refreshConversation(true);
    }
  });
  window.addEventListener("beforeunload", saveDraft);
});
