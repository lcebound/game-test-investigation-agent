"use strict";

function renderHistorySelections() {
  const target = $("recall-selections");
  clear(target);
  const count = state.historySelections?.length || 0;
  target.hidden = !count;
  if (!count) return;
  target.append(node("span", "", `下次调查将参考 ${count} 份历史报告`));
  const reset = node("button", "text-link", "移除");
  reset.onclick = () => { state.historySelections = []; state.pendingMutation = null; renderHistorySelections(); };
  target.append(reset);
}

async function searchHistoricalSources(more = false) {
  const query = $("recall-query").value.trim();
  if (!query) return;
  $("recall-heading").textContent = "查找历史调查";
  const serial = state.recallSerial = (state.recallSerial || 0) + 1;
  const generation = state.generation, epoch = state.identityEpoch;
  const cursor = more && query === state.recallQuery ? state.recallCursor : null;
  state.recallQuery = query;
  if (!cursor) clear($("recall-results"));
  try {
    const page = await api(`/v1/history/search?query=${encodeURIComponent(query)}${cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`);
    if (serial !== state.recallSerial || generation !== state.generation || epoch !== state.identityEpoch) return;
    for (const source of page.items) {
      const card = node("article");
      card.append(node("strong", "", source.objective_excerpt), node("p", "", source.summary_excerpt),
        node("small", "", `历史报告 · 第 ${source.report_revision} 版`));
      const attach = node("button", "outline-button", "关联为历史参考");
      attach.onclick = async () => {
        if (state.busy || generation !== state.generation || epoch !== state.identityEpoch) return;
        const link = {source_investigation_id: source.source_investigation_id, report_sha256: source.report_sha256};
        attach.disabled = true;
        try {
          if (state.id && state.snapshot && !terminal(state.snapshot.investigation.status) && !state.newTask) {
            await api(`/v1/investigations/${encodeURIComponent(state.id)}/recall`, {method: "POST", body: link});
            if (generation !== state.generation || epoch !== state.identityEpoch) return;
            await refreshConversation(true);
            toast("已关联历史来源，调查将按需读取。");
          } else {
            state.historySelections = [...new Map([...(state.historySelections || []), link].map(item => [item.source_investigation_id, item])).values()];
            state.pendingMutation = null;
            renderHistorySelections();
            toast("已加入下一次调查的历史参考。");
          }
          $("recall-dialog").close();
        } catch (error) { toast(error.message, true); attach.disabled = false; }
      };
      card.append(attach);
      $("recall-results").append(card);
    }
    if (!page.items.length) $("recall-results").append(node("p", "", page.complete ? "未找到符合关键词的历史报告。" : "本页没有匹配结果，可继续查找更早的调查。"));
    state.recallCursor = page.next_cursor;
    $("recall-more").hidden = !page.next_cursor;
  } catch (error) { if (epoch === state.identityEpoch) toast(error.message, true); }
}

async function inspectHistoricalSource(investigationId, source) {
  const generation = state.generation, epoch = state.identityEpoch;
  clear($("recall-results"));
  $("recall-more").hidden = true;
  $("recall-heading").textContent = "历史报告与原文";
  $("recall-dialog").showModal();
  const load = async (ref, byteOffset = 0, offset = 0) => {
    try {
      const page = await api(`/v1/investigations/${encodeURIComponent(investigationId)}/recall/read`,
        {method: "POST", body: {ref, byte_offset: byteOffset, offset, max_bytes: 4096}});
      if (generation !== state.generation || epoch !== state.identityEpoch) return;
      if (page.result !== undefined) {
        let report = null;
        if (page.resource_kind === "historical_report" && !page.truncated) {
          try { report = JSON.parse(page.result); } catch {}
        }
        if (report) {
          $("recall-results").append(node("h3", "", `历史报告 · 第 ${page.report_revision} 版`), node("p", "", report.summary));
          if (report.unresolved_questions?.length) {
            $("recall-results").append(node("strong", "", "当时保留的问题"));
            for (const question of report.unresolved_questions.slice(0, 8)) $("recall-results").append(node("p", "", question));
          }
          const details = node("details");
          details.append(node("summary", "", "报告记录"), node("pre", "message-text", page.result));
          $("recall-results").append(details);
        } else $("recall-results").append(node("pre", "message-text", page.result));
      }
      for (const item of page.items || []) {
        const button = node("button", "text-link", `读取历史原文 · ${item.source_evidence_id || item.source_investigation_id}`);
        button.disabled = !!item.unavailable;
        button.onclick = () => load(item.read_ref);
        $("recall-results").append(button);
      }
      if (page.evidence_directory_ref && byteOffset === 0) {
        const directory = node("button", "outline-button", "查看报告引用的原文");
        directory.onclick = () => load(page.evidence_directory_ref);
        $("recall-results").append(directory);
      }
      if (page.next_byte_offset !== null && page.next_byte_offset !== undefined || page.next_offset !== null && page.next_offset !== undefined) {
        const next = node("button", "outline-button", "继续读取");
        next.onclick = () => { next.disabled = true; load(ref, page.next_byte_offset || 0, page.next_offset || 0); };
        $("recall-results").append(next);
      }
    } catch (error) { if (epoch === state.identityEpoch) toast(error.message, true); }
  };
  await load(source.read_ref || `history/${source.source_investigation_id}/report`);
}

document.addEventListener("DOMContentLoaded", () => {
  $("recall-open").onclick = () => { if (!state.busy) $("recall-dialog").showModal(); };
  $("recall-search").onclick = () => searchHistoricalSources();
  $("recall-more").onclick = () => searchHistoricalSources(true);
  $("recall-query").onkeydown = event => { if (event.key === "Enter") { event.preventDefault(); searchHistoricalSources(); } };
});
