"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  accepted: "已受理",
  discovering: "发现平台能力",
  planning: "制定计划",
  acting: "执行测试",
  waiting_external: "等待平台结果",
  waiting_input: "等待补充信息",
  analyzing: "分析证据",
  evaluating: "评估目标",
  reporting: "生成报告",
  canceling: "正在停止",
  completed: "已完成",
  canceled: "已停止",
  failed: "执行失败",
  experiment: "运行实验",
  query_run: "查询运行",
  retrieve_evidence: "读取证据",
  search_evidence: "搜索证据",
  analyze: "分析",
  execute_code: "编写代码分析",
  report: "生成结论",
  request_input: "需要补充信息",
  passed: "通过",
  not_run: "未执行",
  unknown: "未知",
  succeeded: "执行成功",
  running: "运行中",
  pending: "待调查",
  in_progress: "调查中",
  blocked: "待补充证据",
  skipped: "已跳过",
  queued: "排队中",
  inconclusive: "证据不足",
  goal_met: "目标达成",
  goal_failed: "目标未达成",
  diagnosed: "已定位",
  not_reproduced: "未复现",
  system_error: "系统错误",
};
const errors = {
  INTEGRATION_NOT_CONFIGURED:
    "测试平台尚未配置服务身份。请在平台服务端配置访问凭据后重启服务。",
  PERMISSION_DENIED: "平台服务身份无效或权限不足，请检查令牌与项目授权。",
  CONNECTION_LOST: "无法连接平台，请确认服务地址和网络。",
  CONNECTION_FAILED: "连接失败，请检查服务端 Provider 配置。",
  TIMEOUT: "平台连接超时，请稍后重试。",
  SCHEMA_MISMATCH: "工具参数或返回数据不符合声明的 Schema。",
};
const terminal = (status) =>
  ["completed", "canceled", "failed"].includes(status);
const legacyDefaultScopes = "tests:read,runs:read,runs:submit,runs:cancel:own,evidence:read";
const defaultScopes = legacyDefaultScopes + ",agent:analysis:approve";
let localApprovalKey = "";
try {
  const fragment = new URLSearchParams(location.hash.slice(1));
  const suppliedKey = fragment.get("approval_key");
  if (suppliedKey && /^[A-Za-z0-9_-]{64}$/.test(suppliedKey)) {
    sessionStorage.setItem("trace.codeApprovalKey", suppliedKey);
    fragment.delete("approval_key");
    const remaining = fragment.toString();
    history.replaceState(null, "", location.pathname + location.search + (remaining ? "#" + remaining : ""));
  }
  localApprovalKey = sessionStorage.getItem("trace.codeApprovalKey") || "";
} catch {}
let saved = {};
try {
  saved = JSON.parse(localStorage.getItem("trace.workspace") || "{}");
} catch {}
const state = {
  identity: {
    tenant: saved.tenant || "studio-a",
    project: saved.project || "default",
    principal: saved.principal || "developer",
    scopes:
      (saved.scopes === legacyDefaultScopes ? defaultScopes : saved.scopes) || defaultScopes,
    token: "",
  },
  options: {
    build: "",
    target: "windows_client",
    runs: 3,
    rounds: 4,
    reads: 40,
    minutes: 120,
    seconds: 7200,
    parameters: "",
    constraints: "",
    testId: "",
    criteria: "",
  },
  config: null,
  history: [],
  historyCursor: null,
  id: null,
  snapshot: null,
  selected: null,
  provider: null,
  generation: 0,
  identityEpoch: 0,
  rootId: null,
  thread: [],
  busy: false,
  timer: null,
  guide: "local",
  catalogPage: 0,
  catalogTotal: 0,
  catalogItems: [],
  evidence: null,
  pendingMutation: null,
};
function node(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = String(text);
  return e;
}
function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const u = document.createElementNS(s.namespaceURI, "use");
  u.setAttribute("href", `#i-${name}`);
  s.append(u);
  return s;
}
function clear(element) {
  element.replaceChildren();
}
function toast(text, error = false) {
  $("toast").textContent = text;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => ($("toast").hidden = true), 6000);
}
function time(value) {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    timeZone: "Asia/Hong_Kong",
  });
}
function date(value) {
  return new Date(value).toLocaleDateString("zh-CN", {
    month: "short",
    day: "numeric",
    timeZone: "Asia/Hong_Kong",
  });
}
async function api(path, { method = "GET", body, key, codeApproval = false } = {}) {
  const a = state.identity,
    headers = {
      "X-Principal-ID": a.principal,
      "X-Tenant-ID": a.tenant,
      "X-Project-ID": a.project,
      "X-Scopes": a.scopes,
    };
  if (a.token) headers.Authorization = `Bearer ${a.token}`;
  if (codeApproval && localApprovalKey) headers["X-Code-Approval-Key"] = localApprovalKey;
  if (key) headers["Idempotency-Key"] = key;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const controller = new AbortController(),
    timer = setTimeout(() => controller.abort(), 45000);
  try {
    const response = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await response
      .json()
      .catch(() => ({ detail: "服务返回了非预期响应，请检查服务状态。" }));
    if (!response.ok) {
      const detail = data.detail;
      let message =
        typeof detail === "string" ? detail : JSON.stringify(detail);
      if ([401, 403, 422].includes(response.status) && !Array.isArray(detail))
        message += "；请检查工作台设置中的身份、项目和权限。";
      const error = new Error(`${response.status} · ${message}`);
      error.status = response.status;
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError")
      throw new Error("请求超时。提交操作可以重试，同一请求会复用幂等键。");
    throw error;
  } finally {
    clearTimeout(timer);
  }
}
function setBusy(value) {
  state.busy = value;
  $("send").disabled =
    value ||
    !state.config ||
    !!state.conversation?.archived ||
    (!state.newTask && state.snapshot?.investigation.status === "canceling");
  $("stop").disabled =
    value || state.snapshot?.investigation.status === "canceling";
  if (state.conversation) renderTaskSelector();
  renderConversationSummary();
}
function showTab(tab) {
  document.querySelectorAll("[data-tab]").forEach((b) => {
    const active = b.dataset.tab === tab;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", String(active));
  });
  for (const p of ["platform", "execution", "evidence"])
    $("panel-" + p).hidden = p !== tab;
}
function renderHistory() {
  renderConversationHistory();
}
async function loadHistory(more = false) {
  await fetchConversationHistory(more);
}
function emptyPanel(target, title, description, iconName = "grid") {
  clear(target);
  const e = node("div", "empty-panel");
  e.append(
    icon(iconName),
    node("strong", "", title),
    node("p", "", description),
  );
  target.append(e);
}
function newChat(initial = false) {
  resetConversation(initial);
  if (state.config?.default_budget) {
    const b = state.config.default_budget;
    Object.assign(state.options, {runs: b.max_real_runs, rounds: b.max_rounds,
      reads: b.max_tool_reads, minutes: b.max_device_minutes, seconds: b.max_wall_seconds});
  }
  state.generation++;
  clearTimeout(state.timer);
  state.id = null;
  state.snapshot = null;
  state.pendingMutation = null;
  $("welcome").hidden = false;
  $("messages").hidden = true;
  $("chat-title").textContent = "新的调查";
  $("task-state").hidden = true;
  $("stop").hidden = true;
  $("prompt").placeholder =
    "你想验证什么？描述目标、现象，或选择右侧的测试用例…";
  $("evidence-count").textContent = "0";
  clear($("messages"));
  emptyPanel(
    $("panel-execution"),
    "从一个目标开始",
    "任务开始后，这里会显示计划、运行与预算。",
    "play",
  );
  emptyPanel(
    $("panel-evidence"),
    "等待收集证据",
    "日志、报告和采集产物会显示在这里。",
    "file",
  );
  renderHistory();
  closeDrawers();
  restoreDraft();
  setBusy(false);
  $("prompt").focus();
}
async function selectInvestigation(id) {
  await openConversation(id);
}
function message(role, title, created) {
  const e = node("article", `message ${role}`),
    avatar = node("div", "message-avatar", role === "user" ? "YOU" : "✳"),
    body = node("div", "message-body"),
    heading = node("div", "message-heading", title);
  if (created) {
    const t = node("time", "", time(created));
    t.dateTime = created;
    heading.append(t);
  }
  body.append(heading);
  e.append(avatar, body);
  return { e, body };
}
function markdown(target, text) {
  safeMarkdown(target, text);
}
function downloadReport(s = state.snapshot) {
  if (!s?.report) return;
  const blob = new Blob([s.report.markdown], {
      type: "text/markdown;charset=utf-8",
    }),
    url = URL.createObjectURL(blob),
    a = node("a");
  a.href = url;
  a.download = `${s.investigation.investigation_id}-report.md`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function renderMessages(s, append = false, detailTarget = null) {
  const target = detailTarget || $("messages"),
    scroll = detailTarget || $("conversation-scroll"),
    atBottom =
      scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
  if (!append) clear(target);
  const v = s.investigation,
    first = message("user", "你", v.created_at);
  first.body.append(node("p", "message-text", v.request.objective.description));
  target.append(first.e);
  const entries = [
    ...s.inputs.map((i) => ({
      type: "input",
      created: i.created_at,
      value: i,
    })),
    ...(s.code_permissions || []).map((p) => ({
      type: "permission", created: p.requested_at, value: p,
    })),
    ...s.decisions.map((d) => ({
      type: "decision",
      created: d.created_at,
      value: d,
    })),
    ...s.events.filter((e) => e.event_type === "mcp.native.tool_completed").map((e) => ({
      type: "mcp", created: e.created_at, value: e.data,
    })),
    ...s.events.filter((e) => ["analysis.code_started", "analysis.code_completed"].includes(e.event_type)).map((e) => ({
      type: "code", created: e.created_at, value: e.data, completed: e.event_type.endsWith("completed"),
    })),
    ...(s.native_plan_history || []).map((e) => ({
      type: "plan", created: e.created_at, value: e.data,
    })),
  ].sort((a, b) => new Date(a.created) - new Date(b.created));
  for (const entry of entries) {
    if (entry.type === "permission") {
      const p = entry.value, request = p.request;
      const status = {pending: "等待你的授权", approved: "已授权本次执行", denied: "你已拒绝", expired: "申请已过期", canceled: "申请已取消"};
      const m = message("assistant", `TRACE · ${status[p.status] || p.status}`, entry.created);
      const card = node("div", "permission-card");
      card.append(node("p", "message-text", request.purpose),
        node("p", "message-text", request.permissions.reason));
      const list = node("ul", "permission-scopes");
      for (const item of request.permissions.directories) {
        list.append(node("li", "", `${item.access === "write" ? "读取和修改" : "只读"}目录：${item.path}`));
      }
      if (request.permissions.network) list.append(node("li", "", "允许本次代码访问网络，包含本机及局域网，无域名限制。"));
      card.append(list, node("p", "form-note", "仅授权这份代码和本次任务；修改代码需重新申请。"));
      const source = node("details", "permission-source");
      source.dataset.decisionId = p.approval_id;
      source.open = p.status === "pending";
      source.append(node("summary", "", `查看将执行的 ${request.language} 代码`), node("pre", "", request.code));
      card.append(source);
      if (p.status === "pending" && !terminal(v.status) && v.status !== "canceling") {
        const actions = node("div", "permission-actions");
        const approve = node("button", "primary-button", "仅本次允许");
        const deny = node("button", "outline-button", "拒绝");
        const canApprove = state.identity.scopes.split(",").includes("agent:analysis:approve") || Boolean(state.identity.token);
        approve.disabled = deny.disabled = !canApprove;
        if (!canApprove) card.append(node("p", "form-note", "当前身份没有审批权限，请切换到有权限的身份。"));
        const decide = async (decision) => {
          approve.disabled = deny.disabled = true;
          try {
            await api(`/v1/investigations/${encodeURIComponent(v.investigation_id)}/code-permissions/${encodeURIComponent(p.approval_id)}/decision`, {
              method: "POST", body: {request_hash: p.request_hash, decision}, codeApproval: true,
            });
            if (state.rootId || state.id) await refreshConversation();
          } catch (error) {
            toast(error.message, true);
            approve.disabled = deny.disabled = false;
          }
        };
        approve.onclick = () => decide("approve");
        deny.onclick = () => decide("deny");
        actions.append(deny, approve);
        card.append(actions, node("p", "form-note", `申请有效至 ${time(p.expires_at)}，等待期间不会执行代码。`));
      }
      m.body.append(card);
      target.append(m.e);
      continue;
    }
    if (entry.type === "input") {
      const applied = s.native_runtime?.applied_input_ids?.includes(entry.value.command_id);
      const returned = s.input_returns?.find(item => item.source_command_id === entry.value.command_id);
      const routeState = {pending: "已退回，等待重新归属", routed: "已转交其他任务", dismissed: "已退回，不再处理"};
      const m = message("user", `你 · 补充信息${returned ? ' · ' + routeState[returned.state] : s.native_runtime ? (applied ? " · 已用于调查" : " · 等待下一步处理") : ""}`, entry.created);
      m.body.append(node("p", "message-text", entry.value.payload.content));
      if (returned) m.body.append(node("small", "", `Worker 退回原因（未独立核实）：${returned.reason_excerpt}`));
      target.append(m.e);
      continue;
    }
    if (entry.type === "mcp") {
      const m = message("assistant", "TRACE · 读取平台证据", entry.created);
      m.body.append(node("p", "message-text", `已通过 ${entry.value.tool_name} 读取调查资料。`));
      const detail = node("details");
      detail.append(node("summary", "", "查看调用与证据编号"),
        node("pre", "", JSON.stringify(entry.value, null, 2)));
      m.body.append(detail);
      target.append(m.e);
      continue;
    }
    if (entry.type === "code") {
      const succeeded = entry.value.execution_status === "succeeded";
      const title = entry.completed ? (succeeded ? "代码分析完成" : "代码分析失败") : "编写并执行分析代码";
      const m = message("assistant", `TRACE · ${title}`, entry.created);
      m.body.append(node("p", "message-text", entry.completed
        ? (succeeded ? "分析结果已保存，可在证据中查看。" : "执行反馈已返回，Agent 可在预算内修改代码。")
        : entry.value.purpose));
      target.append(m.e);
      continue;
    }
    if (entry.type === "plan") {
      const plan = entry.value;
      const m = message("assistant", plan.revision === 1 ? "TRACE · 初步调查计划" : "TRACE · 调整调查计划", entry.created);
      m.body.append(node("p", "message-text", plan.reason));
      const list = node("ol", "investigation-plan-list");
      for (const step of plan.steps) {
        list.append(node("li", "", `${step.title} · ${labels[step.status] || step.status}`));
      }
      m.body.append(list);
      target.append(m.e);
      continue;
    }
    const d = entry.value,
      m = message("assistant", "TRACE", entry.created),
      card = node("div", "decision-card"),
      label = node("div", "decision-label");
    label.append(
      icon(
        d.decision.kind === "experiment"
          ? "play"
          : d.decision.kind === "request_input"
            ? "chat"
            : "file",
      ),
      node("span", "", labels[d.decision.kind] || d.decision.kind),
      node("span", "", `第 ${d.round} 轮`),
    );
    card.append(label, node("p", "message-text", d.decision.reason_summary));
    if (d.decision.missing_fields?.length)
      card.append(
        node(
          "p",
          "message-text",
          `请补充：${d.decision.missing_fields.join("、")}`,
        ),
      );
    const detail = node("details");
    detail.dataset.decisionId = d.decision_id;
    detail.append(
      node("summary", "", "查看决策依据与预期"),
      node(
        "pre",
        "",
        JSON.stringify(
          {
            预期观察: d.decision.expected_observation,
            平台: d.decision.provider_id,
            能力: d.decision.capability_id,
            引用证据: d.decision.evidence_ids,
          },
          null,
          2,
        ),
      ),
    );
    card.append(detail);
    m.body.append(card);
    target.append(m.e);
  }
  if (s.report) {
    const m = message(
      "assistant",
      "TRACE · 调查报告",
      s.report.report.generated_at,
    );
    const content = renderReportCard(s);
    const button = node("button", "report-download", "导出 Markdown 报告");
    button.prepend(icon("download"));
    button.onclick = () => downloadReport(s);
    m.body.append(content, button);
    target.append(m.e);
  } else if (!entries.length) {
    const m = message("assistant", "TRACE");
    m.body.append(
      node(
        "p",
        "message-text",
        terminal(v.status)
          ? `任务${labels[v.status]}。停止原因：${v.stop_reason || "尚无报告"}`
          : "已经收到目标，正在检查能力和调查条件。",
      ),
    );
    target.append(m.e);
  }
  if (atBottom && !append) scroll.scrollTop = scroll.scrollHeight;
}
function renderExecution(s) {
  const p = $("panel-execution");
  clear(p);
  const b = s.investigation.request.budget,
    u = s.budget_usage,
    stats = node("div", "stat-grid");
  for (const [value, title] of [
    [`${u.real_runs || 0} / ${b.max_real_runs}`, "真实运行"],
    [`${u.native_model_turns || s.decisions.length} / ${b.max_rounds}`, "推理轮数"],
    [
      `${u.reserved_device_minutes || 0} / ${b.max_device_minutes}`,
      "预留设备分钟",
    ],
    [`${u.tool_reads || 0} / ${b.max_tool_reads}`, "工具读取"],
    ...(s.native_runtime ? [
      [s.native_runtime.input_tokens.toLocaleString(), "累计输入 Token"],
      [s.native_runtime.output_tokens.toLocaleString(), "累计输出 Token"],
      [s.native_runtime.model_responses, "模型成功响应"],
      [s.native_runtime.cache_hits, "复用已有读取"],
      ...(s.native_runtime.model_budget_usage ? [[
        `${s.native_runtime.model_budget_usage.tokens.toLocaleString()} / ${s.native_runtime.model_budget_limits.tokens.toLocaleString()}`,
        "模型 Token 用量（含未结算预留）",
      ]] : []),
      ...(s.native_runtime.model_budget_limits?.estimated_cost != null ? [[
        `${s.native_runtime.model_budget_usage.estimated_cost.toFixed(4)} / ${s.native_runtime.model_budget_limits.estimated_cost}`,
        "模型估算费用（配置币种）",
      ]] : []),
    ] : []),
  ]) {
    const e = node("div", "stat");
    e.append(node("strong", "", value), node("small", "", title));
    stats.append(e);
  }
  p.append(stats, node("div", "panel-section-title", s.native_runtime ? "调查计划" : "执行计划"));
  if (s.native_plan?.steps?.length) {
    const plan = s.native_plan;
    const coverage = s.native_runtime?.question_coverage;
    const questions = new Map((coverage?.questions || []).map((q) => [q.question_id, q]));
    const answerLabels = {open: "未回答", partial: "部分回答", answered: "已回答（模型判断）", blocked: "受阻/跳过"};
    p.append(node("p", "plan-caption", `模型制定 · 第 ${plan.revision} 版 · 问题状态不代表验收通过或根因确认`),
      node("p", "plan-reason", plan.reason));
    plan.steps.forEach((step, index) => {
      const question = questions.get(step.step_id);
      const displayStatus = step.status === "completed" && question && question.state !== "answered"
        ? "in_progress" : step.status;
      const e = node("div", `step investigation-step ${displayStatus}`);
      e.dataset.stepId = step.step_id;
      const body = node("div", "step-body");
      body.append(node("strong", "", step.title),
        node("small", "step-status", question?.needs_review ? "新补充待复核"
          : answerLabels[question?.state] || labels[step.status] || step.status),
        node("p", "", step.question));
      const detail = node("details");
      detail.open = step.status === "in_progress" || Boolean(question?.needs_review) || Boolean(step.remaining_gaps?.length);
      detail.append(node("summary", "", "证据与完成条件"),
        node("p", "", `所需证据：${step.evidence_needed.join("；")}`),
        node("p", "", `完成条件：${step.completion_condition}`));
      if (step.outcome) detail.append(node("p", "", `答复草稿（模型判断）：${step.outcome}`));
      if (step.remaining_gaps?.length) detail.append(node("p", "", `剩余缺口：${step.remaining_gaps.join("；")}`));
      if (step.source_input_ids?.length) detail.append(node("p", "", `来源补充：${step.source_input_ids.join("、")}`));
      if (question?.unavailable_evidence_ids?.length) detail.append(node("p", "", "关联证据已不可读，答案需要重新核对。"));
      for (const [eid, role] of [
        ...(step.evidence_ids || []).map((eid) => [eid, "证据"]),
        ...(step.counterevidence_ids || []).map((eid) => [eid, "反证"]),
      ]) {
        const record = s.evidence.find((item) => item.evidence_id === eid);
        const button = node("button", "plan-evidence", `查看${role} ${eid}`);
        button.disabled = !record || record.retrieval_status !== "ready";
        button.onclick = () => { showTab("evidence"); if (record) openEvidence(record); };
        detail.append(button);
      }
      body.append(detail);
      e.append(node("span", "step-mark", index + 1), body);
      p.append(e);
    });
    if (terminal(s.investigation.status) && plan.steps.some((step) => {
      const question = questions.get(step.step_id);
      return question ? question.state !== "answered" || question.needs_review
        : ["pending", "in_progress", "blocked"].includes(step.status);
    })) {
      p.append(node("p", "plan-caption", "调查已结束，以上未完成步骤保留原状态；请结合报告中的证据缺口判断下一步。"));
    }
  } else if (s.plan?.steps?.length) {
    s.plan.steps.forEach((step, index) => {
      const e = node("div", "step"),
        body = node(
          "div",
          "",
          step.description ||
            step.title ||
            labels[step.action_type] ||
            step.step_id,
        );
      e.append(node("span", "step-mark", index + 1));
      body.append(
        node(
          "small",
          "",
          `${labels[step.status] || step.status} · ${step.capability_id || labels[step.action_type] || ""}`,
        ),
      );
      e.append(body);
      p.append(e);
    });
  } else
    p.append(
      node("p", s.native_runtime ? "message-text" : "error-inline", s.native_runtime
        ? (terminal(s.investigation.status) ? "本次历史调查未保存结构化计划。新调查会由模型先制定计划。" : "模型正在制定初步调查计划，之后会随证据更新步骤。")
        : "暂无可执行计划，先检查平台能力和目标条件。"),
    );
  const hypotheses = s.native_runtime?.hypotheses || [];
  if (hypotheses.length) {
    p.append(node("div", "panel-section-title", `待验证假设 · ${hypotheses.length}`),
      node("p", "plan-caption", "支持与反驳状态是调查评估，不代表根因已确认。"));
    const hypothesisLabels = { proposed: "待验证", unresolved: "未解决", supported: "有支持证据", refuted: "有反驳证据" };
    for (const hypothesis of hypotheses) {
      const card = node("details", "run-card");
      card.append(node("summary", "", `${hypothesisLabels[hypothesis.status] || hypothesis.status} · ${hypothesis.statement}`),
        node("p", "", `可验证预测：${hypothesis.falsifiable_prediction}`));
      if (hypothesis.missing_evidence.length) card.append(node("p", "", `证据缺口：${hypothesis.missing_evidence.join("；")}`));
      if (hypothesis.next_experiment) card.append(node("p", "", `建议验证：${hypothesis.next_experiment}`));
      for (const [title, ids] of [["支持", hypothesis.supporting_evidence_ids], ["反驳", hypothesis.contradicting_evidence_ids]]) {
        for (const eid of ids) {
          const record = s.evidence.find(item => item.evidence_id === eid);
          const button = node("button", "plan-evidence", `${title}证据 ${eid}`);
          button.onclick = () => { showTab("evidence"); if (record) openEvidence(record); };
          card.append(button);
        }
      }
      p.append(card);
    }
  }
  const choice = s.native_runtime?.next_action_selection?.selected_candidate;
  if (choice) p.append(node("div", "panel-section-title", "最近比较的问题"), node("p", "", choice.question));
  p.append(node("div", "panel-section-title", `平台运行 · ${s.runs.length}`));
  for (const snapshot of s.runs) {
    const run = snapshot.result || snapshot;
    const e = node("div", "run-card");
    e.append(
      node("strong", "", run.run_ref?.run_id || "运行"),
      node("small", "", run.run_ref?.provider_id || ""),
      node(
        "span",
        "badge",
        labels[run.execution_status] || run.execution_status,
      ),
      node(
        "span",
        "badge" + (run.test_verdict === "failed" ? " warn" : ""),
        labels[run.test_verdict] || run.test_verdict,
      ),
      node(
        "span",
        "badge" + (run.execution_source === "simulated" ? " warn" : ""),
        run.execution_source === "real"
          ? "真实执行"
          : run.execution_source === "simulated" ? "模拟执行" : "来源未知",
      ),
    );
    if (run.missing_fields?.length)
      e.append(node("small", "", `信息缺口：${run.missing_fields.join("、")}`));
    p.append(e);
  }
  const cleanupResources = s.pending_cleanup_resources ?? s.pending_cleanup_action_ids ?? [];
  if (cleanupResources.length)
    p.append(
      node(
        "p",
        "error-inline",
        `仍有 ${cleanupResources.length} 项资源待确认清理。`,
      ),
    );
  if (s.historical_sources?.items?.length) {
    p.append(node("div", "panel-section-title", "已关联的历史来源"));
    p.append(node("p", "plan-caption", "历史报告与原文保留原调查范围，当前结论仍需独立证据。"));
    for (const source of s.historical_sources.items) {
      const row = node("div", "event-row");
      row.append(node("span", "", source.objective_excerpt || source.source_investigation_id));
      const read = node("button", "text-link", "读取历史报告");
      read.disabled = !!source.unavailable;
      read.onclick = () => inspectHistoricalSource(s.investigation.investigation_id, source);
      const revoke = node("button", "text-link", "撤销关联");
      revoke.onclick = async () => {
        try {
          await api(`/v1/investigations/${encodeURIComponent(s.investigation.investigation_id)}/recall/${encodeURIComponent(source.source_investigation_id)}`, {method: "DELETE"});
          await refreshConversation(true);
        } catch (error) { toast(error.message, true); }
      };
      row.append(read, revoke);
      p.append(row);
    }
    if (s.historical_sources.total_sources > s.historical_sources.items.length) {
      const all = node("button", "text-link", "查看全部历史来源");
      all.onclick = () => inspectHistoricalSource(s.investigation.investigation_id, {read_ref: "history"});
      p.append(all);
    }
  }
  if (s.external_run_feedback?.items?.length) {
    p.append(node("div", "panel-section-title", "外部运行反馈"));
    p.append(node("p", "plan-caption", "平台通知尚未独立核验，请结合原始证据判断。"));
    for (const item of s.external_run_feedback.items) {
      const row = node("div", "event-row");
      row.append(node("span", "", `${item.kind} · ${item.status}${item.input_stale ? " · 较早目标版本" : ""}`),
        node("span", "", item.summary_excerpt));
      const original = node("button", "text-link", item.unavailable ? "原始通知不可读" : "查看原始通知");
      original.disabled = !!item.unavailable;
      original.onclick = async () => {
        try {
          const cursor = item.read_ref.split("/")[1];
          const data = await api(`/v1/investigations/${encodeURIComponent(s.investigation.investigation_id)}/feedback/${cursor}`);
          const detail = node("pre", "message-text", data.result);
          row.append(detail);
          original.disabled = true;
          if (data.truncated) row.append(node("small", "", "仅显示原文首段；完整通知可通过反馈读取接口分页获取。"));
        } catch (error) { toast(error.message, true); }
      };
      row.append(original);
      p.append(row);
    }
  }
  p.append(node("div", "panel-section-title", "任务事件"));
  for (const event of s.events) {
    const row = node("div", "event-row");
    row.append(
      node("time", "", time(event.created_at)),
      node("span", "", event.event_type),
    );
    p.append(row);
  }
}
function renderEvidence(s) {
  const p = $("panel-evidence");
  $("evidence-count").textContent = s.evidence.length;
  if (!s.evidence.length) {
    emptyPanel(
      p,
      "尚未取得证据",
      "测试执行或证据摄取后，产物会出现在这里。",
      "file",
    );
    return;
  }
  clear(p);
  for (const item of s.evidence) {
    const e = node("div", "evidence-card"),
      button = node("button");
    button.append(
      node("strong", "", item.evidence_id),
      node("small", "", `${item.kind} · ${item.mime_type}`),
      node("small", "", `${item.producer} · ${item.quality}`),
      node(
        "span",
        "badge",
        item.execution_source === "simulated"
          ? "模拟证据"
          : item.source_type || "证据",
      ),
    );
    const source = s.native_runtime?.evidence_sources?.[item.evidence_id]?.values;
    if (source) button.append(node("small", "", [
      source.run_id ? `运行 ${source.run_id}` : "运行未知",
      source.build_id ? `构建 ${source.build_id}` : "构建未知",
      source.environment, source.phase, source.timestamp,
    ].filter(Boolean).join(" · ")));
    button.onclick = () => openEvidence(item, s.investigation.investigation_id);
    e.append(button);
    p.append(e);
  }
}
async function openEvidence(item, investigationId = state.id) {
  clearEvidenceImage();
  state.evidence = {
    item,
    offset: 0,
    generation: state.generation,
    id: investigationId,
  };
  $("evidence-metadata").textContent = JSON.stringify(
    {
      id: item.evidence_id,
      producer: item.producer,
      sha256: item.sha256,
      quality: item.quality,
      source_type: item.source_type,
      run_ref: item.run_ref,
    },
    null,
    2,
  );
  $("evidence-content").textContent = "正在读取…";
  $("evidence-next").hidden = true;
  $("evidence-dialog").showModal();
  if (
    ["image/png", "image/jpeg", "image/webp", "image/gif"].includes(
      item.mime_type,
    )
  )
    await previewEvidenceImage(state.evidence);
  else if (
    item.mime_type.startsWith("text/") ||
    ["application/json", "application/xml"].includes(item.mime_type)
  )
    await readEvidence();
  else
    $("evidence-content").textContent = "此类型暂无文本预览，可下载原始证据。";
}
async function readEvidence() {
  const context = state.evidence;
  if (!context) return;
  $("evidence-next").disabled = true;
  try {
    const excerpt = await api(
      `/v1/investigations/${encodeURIComponent(context.id)}/evidence/${encodeURIComponent(context.item.evidence_id)}?byte_offset=${context.offset}&max_bytes=32768`,
    );
    if (context !== state.evidence) return;
    if (context.offset === 0) $("evidence-content").textContent = "";
    $("evidence-content").textContent +=
      excerpt.text || "此类型证据暂无文本预览。";
    context.offset = excerpt.next_byte_offset;
    $("evidence-next").hidden =
      excerpt.next_byte_offset === null ||
      excerpt.next_byte_offset === undefined;
  } catch (e) {
    if (context === state.evidence) toast(e.message, true);
  } finally {
    $("evidence-next").disabled = false;
  }
}
async function refresh(first = false) {
  await refreshConversation(first);
}
function budget() {
  const o = state.options;
  return {
    max_rounds: Number(o.rounds),
    max_real_runs: Number(o.runs),
    max_tool_reads: Number(o.reads),
    max_device_minutes: Number(o.minutes),
    max_wall_seconds: Number(o.seconds),
  };
}
function jsonObject(value, label) {
  if (!value.trim()) return {};
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${label}必须是有效 JSON。`);
  }
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object")
    throw new Error(`${label}必须是 JSON 对象。`);
  return parsed;
}
function constraints() {
  const o = state.options,
    c = jsonObject(o.constraints, "额外约束"),
    parameters = jsonObject(o.parameters, "动作参数");
  if (o.testId) parameters.test_ref = { test_id: o.testId };
  if (state.historySelections?.length) c.recall_references = state.historySelections.map(({source_investigation_id, report_sha256}) => ({source_investigation_id, report_sha256}));
  c.action_parameters = parameters;
  c.action_target_selector = o.target ? { target_kind: o.target } : {};
  if (state.provider) c.provider_allowlist = [state.provider];
  return c;
}
async function send() {
  await sendConversationMessage();
}
async function stop() {
  if (!state.id || state.busy) return;
  setBusy(true);
  const id = state.id;
  try {
    await api(`/v1/investigations/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: { command_id: "cancel-" + crypto.randomUUID() },
    });
    clearTimeout(state.timer);
    await refresh(true);
  } catch (e) {
    toast(e.message, true);
  } finally {
    setBusy(false);
  }
}
function renderSelected() {
  const p = $("selected-test");
  p.hidden = !state.selected;
  if (!state.selected) return;
  clear(p);
  const close = node("button", "", "×");
  close.setAttribute("aria-label", "取消选择用例");
  close.onclick = () => {
    state.selected = null;
    state.options.testId = "";
    const p = jsonObject(state.options.parameters, "动作参数");
    delete p.test_ref;
    state.options.parameters = Object.keys(p).length
      ? JSON.stringify(p, null, 2)
      : "";
    renderSelected();
  };
  p.append(icon("play"), node("span", "", state.selected.name), close);
}
function selectTest(item) {
  state.selected = item;
  state.options.testId = item.test_ref.test_id;
  const parameters = jsonObject(state.options.parameters, "动作参数");
  parameters.test_ref = { test_id: item.test_ref.test_id };
  state.options.parameters = JSON.stringify(parameters, null, 2);
  if (
    item.target_kinds?.length &&
    !item.target_kinds.includes(state.options.target)
  )
    state.options.target = item.target_kinds[0];
  renderSelected();
  renderCatalog();
  if (!state.id && !$("prompt").value.trim())
    $("prompt").value =
      `执行「${item.name}」，检查测试结果、运行环境和证据缺口。`;
  $("prompt").focus();
}
function renderCatalog() {
  clear($("catalog"));
  $("catalog-count").textContent = state.catalogTotal;
  for (const item of state.catalogItems) {
    const b = node(
        "button",
        "catalog-item" +
          (state.selected?.test_ref.test_id === item.test_ref.test_id
            ? " selected"
            : ""),
      ),
      body = node("div");
    b.append(icon("play"));
    body.append(
      node("strong", "", item.name),
      node("small", "", item.target_kinds.join(" · ") || "未声明目标"),
      node("small", "", item.test_ref.test_id),
    );
    b.append(body);
    b.onclick = () => selectTest(item);
    $("catalog").append(b);
  }
  if (!state.catalogItems.length)
    emptyPanel(
      $("catalog"),
      "没有匹配的用例",
      "试试其他关键词，或手动填写动作参数。",
      "search",
    );
  $("catalog-more").hidden = state.catalogItems.length >= state.catalogTotal;
}
async function loadCatalog(more = false) {
  const provider = state.provider,
    generation = state.generation;
  if (!provider) return;
  const configured = state.config?.providers.find(
    (p) => p.provider_id === provider,
  );
  if (!configured?.catalog_supported) {
    emptyPanel(
      $("catalog"),
      "此平台未提供用例目录",
      "仍可通过测试条件填写动作参数，使用发现的能力。",
      "file",
    );
    return;
  }
  try {
    const page = more ? state.catalogPage + 1 : 1,
      result = await api(
        `/v1/providers/${encodeURIComponent(provider)}/tests?query=${encodeURIComponent($("catalog-query").value)}&page=${page}&page_size=30`,
      );
    if (provider !== state.provider || generation !== state.generation) return;
    state.catalogItems = more
      ? [...state.catalogItems, ...result.items]
      : result.items;
    state.catalogPage = result.page;
    state.catalogTotal = result.total;
    renderCatalog();
  } catch (e) {
    if (provider !== state.provider || generation !== state.generation) return;
    emptyPanel($("catalog"), "暂时无法读取用例", e.message, "file");
    $("catalog-more").hidden = true;
  }
}
function connectionStatus(available, text) {
  for (const id of ["sidebar-dot", "platform-dot"]) {
    $(id).classList.toggle("connected", available);
    $(id).classList.toggle("failed", !available);
  }
  $("connection-pill")
    .querySelector(".small-dot")
    .classList.toggle("connected", available);
  $("connection-label").textContent = available ? "平台已连接" : "平台未连接";
  $("platform-status").textContent = text;
  $("platform-status").classList.toggle("error", !available);
}
async function probe() {
  const provider = state.provider,
    generation = state.generation;
  if (!provider) {
    showGuide();
    return;
  }
  $("probe").disabled = true;
  $("platform-status").textContent = "正在检查平台能力…";
  try {
    const result = await api(
      `/v1/providers/${encodeURIComponent(provider)}/probe`,
      { method: "POST" },
    );
    if (provider !== state.provider || generation !== state.generation) return;
    connectionStatus(
      result.available,
      result.available
        ? `已连接 · ${result.capabilities.length} 项已授权能力`
        : errors[result.error_code] || `平台不可用：${result.error_code}`,
    );
    $("platform-status").querySelector(".capability-tags")?.remove();
    if (result.available) {
      const option = [...$("provider").options].find(
        (item) => item.value === provider,
      );
      if (option)
        option.textContent = `${provider} · MCP`;
      state.capabilities = result.capabilities;
      clear($("target-kinds"));
      [...new Set(result.capabilities.flatMap((c) => c.target_kinds))].forEach(
        (kind) => {
          const option = node("option");
          option.value = kind;
          $("target-kinds").append(option);
        },
      );
      const tags = node("div", "capability-tags");
      result.capabilities.forEach((c) =>
        tags.append(node("span", "", c.capability_id)),
      );
      $("platform-status").append(tags);
      await loadCatalog();
    } else {
      state.catalogItems = [];
      state.catalogTotal = 0;
      $("catalog-count").textContent = "—";
      emptyPanel(
        $("catalog"),
        "连接尚未就绪",
        errors[result.error_code] || result.error_code,
        "link",
      );
      $("catalog-more").hidden = true;
    }
  } catch (e) {
    if (provider === state.provider && generation === state.generation) {
      connectionStatus(false, e.message);
      toast(e.message, true);
    }
  } finally {
    $("probe").disabled = false;
  }
}
async function openSettings() {
  try { state.config = await api("/v1/workspace"); } catch {}
  const f = $("settings-form");
  for (const [key, value] of Object.entries(state.identity))
    f.elements.namedItem(key).value = value;
  const model = state.config?.model;
  $("configured-model").value = model?.name || "尚未配置";
  $("configured-compatible-url").value =
    model?.compatible_base_url || "尚未配置";
  $("configured-native-url").value = model?.native_base_url || "尚未配置";
  $("model-connection-status").textContent = model
    ? `${model.enabled ? "模型已启用" : "模型未启用"} · ${model.key_configured ? "API Key 已配置" : "API Key 未配置"}${model.local_configured ? " · 本地文件配置" : ""}`
    : "无法读取配置，请先连接服务。";
  const channel = state.config?.event_channel;
  const statuses = {disabled: "未启用", connecting: "正在连接", ready: "已连接",
    reconnecting: "正在重连", unavailable: "不可用，正在重试"};
  $("event-connection-status").textContent = `自动调查事件通道：${statuses[channel?.status] || "状态未知"}。平台能力连接不代表游戏执行环境已就绪。`;
  $("settings-dialog").showModal();
}
async function initialize() {
  const generation = state.identityEpoch;
  $("principal-label").textContent = state.identity.principal;
  $("project-label").textContent =
    `${state.identity.tenant} / ${state.identity.project}`;
  try {
    const config = await api("/v1/workspace");
    if (generation !== state.identityEpoch) return;
    state.config = config;
    if (!state.rootId && config.default_budget) {
      const b = config.default_budget;
      Object.assign(state.options, {runs: b.max_real_runs, rounds: b.max_rounds,
        reads: b.max_tool_reads, minutes: b.max_device_minutes, seconds: b.max_wall_seconds});
    }
    $("model-chip").textContent = config.model.enabled
      ? config.model.name
      : "规则模式 · 模型未启用";
    $("model-chip").title = config.model.enabled
      ? "模型推理已启用"
      : "服务端 GTA_ENABLE_MODEL 未开启";
    $("composer-note").textContent = config.model.enabled
      ? "结论来自调查记录。未取得证据时，会明确说明缺口。"
      : "当前为规则模式。可验证流程与接口；模型规划需在服务端启用。";
    clear($("provider"));
    for (const [index, p] of config.providers.entries()) {
      const option = node(
        "option",
        "",
        `测试平台 ${index + 1} · MCP`,
      );
      option.value = p.provider_id;
      $("provider").append(option);
    }
    state.provider = config.providers[0]?.provider_id || null;
    if (!state.provider) {
      $("provider").append(node("option", "", "尚未配置平台"));
      connectionStatus(
        false,
        "尚未配置测试平台。打开连接指南，设置平台地址和访问凭据。",
      );
    }
    state.initialized = true;
    setBusy(false);
    await loadHistory();
    await restoreConversation();
    if (state.provider) await probe();
  } catch (e) {
    connectionStatus(false, "工作台身份验证或服务连接失败。");
    syncStatus(e.message);
    toast(e.message, true);
  }
}
function updateGuide() {
  const local = state.guide === "local";
  const item = {
    provider_id: $("guide-provider").value.trim(),
    url: $("guide-url").value.trim(),
    project_ids: [$("guide-project").value.trim()],
    allow_insecure_localhost: $("guide-url").value.trim().startsWith("http://"),
  };
  const tokenEnv = $("guide-token-env").value.trim();
  if (tokenEnv) item.token_env = tokenEnv;
  const environment = "GTA_MCP_PROVIDERS_JSON";
  state.guideConfig = `GTA_MCP_NATIVE_TOOLS=true\n${environment}=${JSON.stringify([item])}\n\n${tokenEnv ? `# 使用密钥系统为 ${tokenEnv} 注入 MCP 访问令牌。\n` : ""}# 配置完成后重新启动 Agent 服务。`;
  $("connection-config").textContent = [
    "接入方式：原生 MCP 工具",
    `连接标识：${item.provider_id}`,
    `项目：${item.project_ids[0]}`,
    `MCP 地址：${item.url}`,
    `令牌环境变量：${tokenEnv || "未配置（可选）"}`,
    `服务端配置项：${environment}`,
  ].join("\n");
  $("guide-note").textContent = "Agent 通过 tools/list 发现工具，按原始 Schema 调用 tools/call。调查只使用服务端授权的只读工具；未配置 allowed_tools 时仅采用标记为只读的工具。";
  $("guide-lyra").classList.toggle("active", local);
  $("guide-mcp").classList.toggle("active", !local);
}
function showGuide() {
  showTab("platform");
  $("guide-project").value = state.identity.project;
  updateGuide();
  $("connection-dialog").showModal();
}
$("send").onclick = send;
$("stop").onclick = stop;
$("new-chat").onclick = () => {
  if (!state.busy) newChat();
};
$("menu").onclick = () => $("sidebar").classList.toggle("open");
$("settings").onclick = openSettings;
$("connections").onclick = showGuide;
$("connection-guide").onclick = showGuide;
$("connection-pill").onclick = () => {
  showTab("platform");
  probe();
};
$("probe").onclick = probe;
$("provider").onchange = () => {
  state.provider = $("provider").value;
  state.selected = null;
  state.catalogItems = [];
  renderSelected();
  probe();
};
$("catalog-form").onsubmit = (e) => {
  e.preventDefault();
  loadCatalog();
};
$("catalog-more").onclick = () => loadCatalog(true);
$("history-search").oninput = renderHistory;
$("load-more").onclick = () =>
  loadHistory(true).catch((e) => toast(e.message, true));
$("evidence-next").onclick = readEvidence;
document
  .querySelectorAll("[data-tab]")
  .forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));
document
  .querySelectorAll("[data-close]")
  .forEach((b) => (b.onclick = () => b.closest("dialog").close()));
document.querySelectorAll("[data-prompt]").forEach(
  (b) =>
    (b.onclick = () => {
      $("prompt").value = b.dataset.prompt;
      $("prompt").focus();
    }),
);
$("prompt").onkeydown = (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    send();
  }
};
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (!state.busy) newChat();
  }
});
$("settings-form").onsubmit = async (e) => {
  e.preventDefault();
  saveDraft();
  stopStreaming();
  state.identityEpoch++;
  const identity = {};
  for (const key of Object.keys(state.identity))
    identity[key] = e.currentTarget.elements.namedItem(key).value.trim();
  state.identity = identity;
  const { token, ...persistent } = identity;
  cacheWrite("trace.workspace", JSON.stringify(persistent));
  state.history = [];
  state.config = null;
  state.selected = null;
  state.rootId = null;
  state.initialized = false;
  state.archivedView = false;
  $("history-search").value = "";
  $("history-archived").setAttribute("aria-pressed", "false");
  history.replaceState(null, "", location.pathname);
  newChat(true);
  setBusy(false);
  $("settings-dialog").close();
  await initialize();
};
$("task-options").onclick = () => {
  const f = $("options-form");
  for (const [key, value] of Object.entries(state.options))
    f.elements.namedItem(key).value = key === "seconds" ? Number(value) / 60 : value;
  $("options-dialog").showModal();
};
$("options-form").onsubmit = (e) => {
  e.preventDefault();
  const values = {};
  for (const key of Object.keys(state.options))
    values[key] = e.currentTarget.elements.namedItem(key).value.trim();
  values.seconds = String(Math.round(Number(values.seconds) * 60));
  try {
    const parameters = jsonObject(values.parameters, "动作参数");
    if (values.testId !== state.options.testId) {
      if (values.testId) parameters.test_ref = { test_id: values.testId };
      else delete parameters.test_ref;
      values.parameters = Object.keys(parameters).length
        ? JSON.stringify(parameters, null, 2)
        : "";
      state.selected = null;
      renderSelected();
    }
    jsonObject(values.constraints, "额外约束");
  } catch (error) {
    toast(error.message, true);
    return;
  }
  state.options = values;
  $("budget-label").textContent = `最多 ${values.runs} 次运行`;
  $("options-dialog").close();
};
$("guide-lyra").onclick = () => {
  state.guide = "local";
  $("guide-provider").value = "platform-local";
  $("guide-project").value = state.identity.project;
  $("guide-url").value = "http://127.0.0.1:8010/mcp";
  updateGuide();
};
$("guide-mcp").onclick = () => {
  state.guide = "remote";
  $("guide-provider").value = "test-platform";
  $("guide-project").value = state.identity.project;
  $("guide-url").value = "https://platform.example.com/mcp";
  updateGuide();
};
for (const id of [
  "guide-provider",
  "guide-project",
  "guide-url",
  "guide-token-env",
])
  $(id).oninput = updateGuide;
$("copy-config").onclick = async () => {
  try {
    await navigator.clipboard.writeText(state.guideConfig);
    toast("配置已复制。按需配置访问令牌并重启 Agent 服务。");
  } catch {
    saveBlob(
      new Blob([state.guideConfig], { type: "text/plain;charset=utf-8" }),
      "test-platform.env",
    );
    toast("剪贴板不可用，服务端配置已下载。");
  }
};
newChat(true);
setBusy(false);
initialize();
