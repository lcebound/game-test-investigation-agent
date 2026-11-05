"use strict";

const attentionLabels = {waiting_input: "等待补充信息", permission: "等待执行授权", cleanup: "资源仍待清理",
  execution_failed: "调查执行失败", goal_failed: "目标未达成", missing_report: "报告尚不可用", input_return: "有消息待重新归属"};

function conversationStatus(summary) {
  const s = summary?.task_summary;
  if (!s) return "";
  const ended = s.completed_tasks + s.failed_tasks + s.canceled_tasks;
  return `${s.task_count} 个任务 · ${s.active_tasks} 进行中 · ${s.waiting_input_tasks} 等待补充 · ${ended} 已结束 · ${s.attention_tasks} 待关注${s.inconclusive_tasks ? ` · ${s.inconclusive_tasks} 证据不足` : ""}`;
}

function renderConversationSummary() {
  const button = $("overview-open");
  const s = state.conversation?.task_summary;
  button.textContent = s?.attention_tasks ? `会话总览 · ${s.attention_tasks} 待关注` : "会话总览";
  button.title = conversationStatus(state.conversation);
  button.disabled = state.busy;
  $("input-returns-open").textContent = `退回消息${s?.pending_input_returns ? ' · ' + s.pending_input_returns : ''}`;
  $("input-returns-open").disabled = state.busy;
}

async function openTaskFromOverview(card, cleanup = false) {
  if (state.busy) return;
  $("overview-dialog").close();
  try {
    await selectTask(card.task_id);
    if (!state.snapshot || state.taskId !== card.task_id) return;
    const generation = state.generation;
    if (cleanup && card.cleanup_investigation_id && card.cleanup_investigation_id !== state.id) {
      const projection = await api(`/v1/investigations/${encodeURIComponent(card.cleanup_investigation_id)}/workspace`);
      if (generation !== state.generation) return;
      state.supplementalInspection = projection;
      state.inspectId = projection.investigation.investigation_id;
    } else state.inspectId = state.id;
    renderInspection();
    showTab("execution");
    openContext();
    if (card.attention.includes("waiting_input")) $("prompt").focus();
  } catch (error) { toast(error.message, true); }
}

async function refreshOverview(attentionOffset = 0, resultOffset = 0) {
  const root = state.rootId, generation = state.generation, epoch = state.identityEpoch;
  if (!root) return;
  const serial = state.overviewSerial = (state.overviewSerial || 0) + 1;
  try {
    const page = await api(`/v1/conversations/${encodeURIComponent(root)}/overview?attention_offset=${attentionOffset}&result_offset=${resultOffset}`);
    if (serial !== state.overviewSerial || generation !== state.generation || epoch !== state.identityEpoch) return;
    state.overview = page;
    $("overview-summary").textContent = conversationStatus({task_summary: page.task_summary});
    clear($("overview-content"));
    $("overview-content").append(node("p", "plan-caption", "待关注事项可能属于进行中或已结束的任务。"));
    for (const [key, title, cursorKey] of [["attention", "待关注事项", "next_attention_offset"], ["results", "最近结果", "next_result_offset"]]) {
      $("overview-content").append(node("h3", "", title));
      if (!page[key].length) $("overview-content").append(node("p", "", key === "attention" ? "本页没有待关注事项。" : "本页暂无已发布结果。"));
      for (const card of page[key]) {
        const row = node("article", "overview-card");
        const resolution = card.resolution || card.report?.resolution;
        row.append(node("strong", "", card.objective_excerpt),
          node("p", "", card.attention.map(reason => attentionLabels[reason] || reason).join(" · ") || `${labels[card.status] || card.status}${resolution ? ` · ${labels[resolution] || resolution}` : ""}`));
        if (card.report) row.append(node("p", "", card.report.summary_excerpt), node("small", "", "任务报告摘要；技术结论以原报告及证据为准。"));
        if (card.pending_cleanup_count) row.append(node("p", "", `${card.pending_cleanup_count} 项资源仍待清理确认。`));
        const open = node("button", "outline-button", key === "attention" ? "查看任务" : "查看结果");
        open.onclick = () => openTaskFromOverview(card);
        row.append(open);
        if (card.cleanup_investigation_id) {
          const cleanup = node("button", "text-link", "查看清理调查");
          cleanup.onclick = () => openTaskFromOverview(card, true);
          row.append(cleanup);
        }
        if (card.attention.includes("input_return")) {
          const returns = node("button", "outline-button", "处理退回消息");
          returns.onclick = openReturnedInputs;
          row.append(returns);
        }
        $("overview-content").append(row);
      }
      if (page[cursorKey] !== null) {
        const more = node("button", "outline-button", key === "attention" ? "更多待关注事项" : "更多结果");
        more.onclick = () => refreshOverview(key === "attention" ? page[cursorKey] : attentionOffset,
                                             key === "results" ? page[cursorKey] : resultOffset);
        $("overview-content").append(more);
      }
    }
  } catch (error) { if (generation === state.generation && epoch === state.identityEpoch) toast(error.message, true); }
}

document.addEventListener("DOMContentLoaded", () => {
  $("overview-open").onclick = () => {
    if (state.busy) return;
    clear($("overview-content"));
    $("overview-summary").textContent = "正在读取会话状态…";
    $("overview-dialog").showModal();
    refreshOverview();
  };
});
