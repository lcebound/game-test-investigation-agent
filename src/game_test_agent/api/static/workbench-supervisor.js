"use strict";

// Interaction model adapted from Super-agent's supervision panel: attention,
// health, then history. Domain execution remains on the authenticated server.
const supervisor = {view: "overview", data: null, serial: 0, timer: null, taskOffset: 0, lastRoot: null};
const viewTitles = {overview: "值得关注的进展", conversation: "主会话", tasks: "后台调查", events: "事件与判断", sources: "信息来源"};
const viewDescriptions = {overview: "先处理需要你的事项，其余调查在后台继续。", conversation: "统一交代目标、补充线索，任务在后台各自推进。", tasks: "查看调查进度与结果，无需先选任务才能对话。", events: "查看收到的线索、归属判断与待处理事件。", sources: "查看 Agent 正在接收什么，以及它被允许自动做什么。"};

function supervisorView(view) {
  supervisor.view = view;
  for (const button of document.querySelectorAll("[data-view]")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active); button.setAttribute("aria-current", active ? "page" : "false");
  }
  $("supervisor-board").hidden = view === "conversation";
  $("conversation-scroll").hidden = view !== "conversation";
  $("board-title").textContent = viewTitles[view]; $("board-subtitle").textContent = viewDescriptions[view];
  $("prompt").placeholder = "告诉 Agent 要关注什么，或补充任一调查的线索…";
  closeDrawers(); renderSupervisor();
}

function supervisorButton(text, action, cls = "outline-button") {
  const button = node("button", cls, text); button.onclick = () => action(); return button;
}
function supervisorEmpty(title, text, action) {
  const box = node("div", "supervisor-empty"); box.append(node("span", "empty-orbit", "◌"), node("h3", "", title), node("p", "", text));
  if (action) box.append(supervisorButton("在主会话交代目标", () => {supervisorView("conversation"); $("prompt").focus();}));
  return box;
}
function supervisorCard(card, needsAction = false) {
  const article = node("article", "supervisor-task");
  const heading = node("div", "task-heading");
  heading.append(node("span", "task-status " + (needsAction ? "needs-attention" : ""), needsAction ? card.attention.map(key => attentionLabels[key] || key).join(" · ") : labels[card.status] || card.status));
  article.append(heading, node("h3", "", card.objective_excerpt));
  if (card.report) article.append(node("p", "task-excerpt", card.report.summary_excerpt));
  const actions = node("div", "card-actions");
  actions.append(supervisorButton(needsAction ? "处理此事项" : "查看详情", () => inspectSupervisorTask(card, needsAction ? "thread" : "execution")));
  if (card.attention?.includes("input_return")) actions.append(supervisorButton("查看退回消息", openReturnedInputs, "text-link"));
  if (!terminal(card.status) && card.status !== "canceling") actions.append(supervisorButton("停止此调查", async () => {
    const root=state.rootId, epoch=state.identityEpoch;
    if (!confirm(`停止“${card.objective_excerpt}”？已取得的证据仍会保留。`)) return;
    try {
      await api(`/v1/investigations/${encodeURIComponent(card.investigation_id)}/cancel`, {method:"POST",body:{command_id:"cancel-"+crypto.randomUUID()}});
      if(root===state.rootId&&epoch===state.identityEpoch) await refreshSupervisor();
    } catch(error) {if(root===state.rootId&&epoch===state.identityEpoch) toast(error.message,true);}
  }, "text-link"));
  article.append(actions); return article;
}

async function inspectSupervisorTask(card, tab = "execution") {
  const root = state.rootId, epoch = state.identityEpoch, generation = state.generation;
  try {
    const snapshot = await api(`/v1/investigations/${encodeURIComponent(card.investigation_id)}/workspace`);
    if (root !== state.rootId || epoch !== state.identityEpoch || generation !== state.generation) return;
    state.supplementalInspection = snapshot; state.inspectId = card.investigation_id;
    supervisor.detailKey=null; renderInspection();
    $("reply-to-task").dataset.taskId = card.task_id;
    showTab(tab); openContext();
  } catch (error) { if (root === state.rootId && epoch === state.identityEpoch) toast(error.message, true); }
}

function renderSupervisor() {
  const panel = $("board-content"); if (!panel) return;
  clear(panel); const data = supervisor.data;
  if (supervisor.view === "sources") {renderSupervisorSources(panel); return;}
  if (!state.rootId) {
    panel.append(supervisorEmpty("把第一个问题交给 Agent", "创建主会话后，后台调查、外部线索与需要你处理的事项都会汇集到这里。", true));
    const strip = node("div", "start-hints");
    for (const [title, text] of [["统一对话", "描述目标，不必安排每一步。"], ["后台调查", "任务独立推进，保留各自证据。"], ["必要时介入", "授权、补充信息和重要结果集中呈现。"]]) {
      const item = node("div"); item.append(node("strong", "", title), node("p", "", text)); strip.append(item);
    }
    panel.append(strip); return;
  }
  if (!data || data.root !== state.rootId) {panel.append(node("p", "loading-line", "正在读取工作状态…")); return;}
  if (data.error) {panel.append(supervisorEmpty("暂时无法读取工作状态", data.error)); return;}
  const overview = data.overview, summary = overview.task_summary;
  if (supervisor.view === "overview") {
    const stats = node("div", "supervisor-stats");
    for (const [label, value, target] of [["需要关注", summary.attention_tasks, "tasks"], ["进行中", summary.active_tasks, "tasks"], ["已结束", summary.completed_tasks + summary.failed_tasks + summary.canceled_tasks, "tasks"]]) {
      const button = node("div", "stat-tile"); button.append(node("span", "", label), node("strong", "", value)); stats.append(button);
    }
    panel.append(stats);
    const title = node("div", "section-heading"); title.append(node("h2", "", "需要你关注"), node("span", "", "授权 · 缺失信息 · 异常")); panel.append(title);
    const cards = node("div", "task-grid");
    if (!overview.attention.length) cards.append(supervisorEmpty("目前没有需要你介入的事项", "已有调查会按授权与预算继续；新的结果可以在主会话查看。"));
    for (const card of overview.attention) cards.append(supervisorCard(card, true)); panel.append(cards);
    if (overview.next_attention_offset !== null) panel.append(supervisorButton("更多待关注事项", () => {supervisorView("tasks"); openOverviewDialog();}, "text-link"));
    const bottom = node("div", "overview-bottom");
    const recent = node("section"); recent.append(node("h2", "", "最近结果"));
    if (!overview.results.length) recent.append(node("p", "subtle", "尚无已发布结果。"));
    for (const card of overview.results.slice(0, 3)) recent.append(supervisorCard(card));
    const health = node("section", "health-panel"); health.append(node("h2", "", "协调状态"));
    const routing = state.config?.coordination?.semantic_router;
    health.append(node("p", "", routing ? "语义协调已启用" : "语义协调未启用"), node("p", "subtle", routing ? "自由表达由 Router 判断归属，明确回复和已订阅事件直达任务。" : "明确绑定仍可投递；自由表达可能需要澄清。"));
    health.append(supervisorButton("查看信息来源", () => supervisorView("sources"), "text-link"));
    bottom.append(recent, health); panel.append(bottom);
  } else if (supervisor.view === "tasks") {
    const tools = node("div", "section-heading"); tools.append(node("h2", "", "所有后台调查"), supervisorButton("综合调查结果", openHarnessPanel, "text-link")); panel.append(tools);
    const cards = node("div", "task-grid");
    for (const card of data.tasks.items) cards.append(supervisorCard(card));
    if (!data.tasks.items.length) cards.append(supervisorEmpty("暂无后台调查", "你可以在主会话提出新目标。"));
    panel.append(cards); const pager = node("div", "card-actions");
    if (supervisor.taskOffset) pager.append(supervisorButton("上一页", () => {supervisor.taskOffset=Math.max(0,supervisor.taskOffset-20); refreshSupervisor();}));
    if (data.tasks.next_offset !== null) pager.append(supervisorButton("下一页", () => {supervisor.taskOffset=data.tasks.next_offset; refreshSupervisor();}));
    panel.append(pager);
  } else if (supervisor.view === "events") {
    panel.append(node("h2", "", "外部线索"));
    if (!data.observations.items.length) panel.append(supervisorEmpty("暂无外部线索", "来源接收的观察事件将在这里展示。"));
    for (const item of data.observations.items) {
      const row=node("article","event-row");
      const names={pending:"等待归属",assessing:"正在判断",routed:"已归属任务",unresolved:"归属待处理"};
      row.append(node("span","task-status",names[item.state]||item.state),node("p","",item.summary_excerpt));
      row.append(supervisorButton("原文与处理",openHarnessPanel,"text-link")); panel.append(row);
    }
    if (data.observations.next_offset !== null) panel.append(supervisorButton("更多事件",openHarnessPanel,"text-link"));
    panel.append(node("h2","","最近归属判断"));
    const names={task:"关联已有任务",new_task:"建议新建调查",clarify:"需要澄清"};
    for (const item of data.activity.routing) {
      const row=node("article","event-row");row.append(node("span","task-status",names[item.kind]||item.kind),node("p","",item.text),node("small","subtle","这是归属决定；实际执行以任务状态为准。"));panel.append(row);
    }
    if (!data.activity.routing.length) panel.append(node("p","subtle","暂无当前身份的归属判断记录。"));
  }
}

function renderSupervisorSources(panel) {
  const config=state.config;
  if (!config) {panel.append(supervisorEmpty("尚未连接工作台", "打开设置核对当前身份和项目。"));return;}
  panel.append(node("h2","","测试能力"));
  for (const provider of config.providers) {
    const row=node("article","source-card");row.append(node("span","source-icon","↗"),node("h3","",provider.provider_id),node("p","subtle","已配置；实际可用性以连接检查为准。"),supervisorButton("检查连接",()=>{showTab("platform");openContext();}));panel.append(row);
  }
  if (!config.providers.length) panel.append(supervisorEmpty("尚未接入测试平台", "连接平台后，Agent 才能发现能力并读取运行证据。"));
  panel.append(node("h2","","事件来源"));
  for (const source of config.coordination?.sources || []) {
    const row=node("article","source-card");row.append(node("h3","",source.source),node("p","",source.automatic_investigation&&source.readonly_ready ? "允许自动只读调查" : source.automatic_investigation ? "自动调查缺少读取权限" : "接收线索，自动新建未开启"),node("small","subtle",`自动任务总额上限 ${source.task_limit}`));panel.append(row);
  }
  if (!(config.coordination?.sources || []).length) panel.append(node("p","subtle","当前项目尚未登记事件源。"));
  const channel={disabled:"消息桥未启用",ready:"消息桥已连接",connecting:"消息桥连接中",reconnecting:"消息桥重连中",unavailable:"消息桥暂不可用"};
  panel.append(node("p","subtle",channel[config.event_channel?.status]||"消息桥状态未知"),supervisorButton("连接与配置说明",showGuide));
}

function renderSupervisorConversation() {
  if (!supervisor.data || supervisor.data.root !== state.rootId || !supervisor.data.activity) return;
  const target=$("messages"), scroller=$("conversation-scroll");
  const nearBottom=scroller.scrollHeight-scroller.scrollTop-scroller.clientHeight<100, top=scroller.scrollTop;
  clear(target);
  for (const item of supervisor.data.activity.items) {
    const article=node("article",`coordinator-message ${item.kind}`);
    const heading=node("div","coordinator-message-heading");
    heading.append(node("strong","",item.kind==="user"?"你":item.kind==="event"?"外部线索":"调查结果"),node("time","",time(item.at)));
    const task=supervisorButton(item.title,()=>inspectSupervisorTask(item),"message-task-link");
    article.append(heading,task,node("p","message-text",item.text));
    if(item.truncated) article.append(node("small","subtle","已截取摘要，完整报告可在调查详情中查看。"));
    target.append(article);
  }
  if (supervisor.data.activity.has_older) target.prepend(node("p","subtle","显示最近的会话动态；更早记录可在对应调查详情中查看。"));
  if (!supervisor.data.activity.items.length) target.append(supervisorEmpty("暂时没有消息", "在下方交代目标或补充线索。"));
  $("messages").hidden=false; $("welcome").hidden=true;
  scroller.scrollTop=nearBottom?scroller.scrollHeight:top;
}

async function refreshSupervisor() {
  const root=state.rootId, epoch=state.identityEpoch, generation=state.generation;
  const serial=++supervisor.serial;
  if (!root) {supervisor.data=null;renderSupervisor();return;}
  try {
    const [overview,activity,observations,tasks]=await Promise.all([
      api(`/v1/conversations/${encodeURIComponent(root)}/overview?limit=10`),
      api(`/v1/conversations/${encodeURIComponent(root)}/activity`),
      api(`/v1/conversations/${encodeURIComponent(root)}/observations`),
      api(`/v1/conversations/${encodeURIComponent(root)}/tasks?offset=${supervisor.taskOffset}&limit=20`),
    ]);
    if (serial!==supervisor.serial||root!==state.rootId||epoch!==state.identityEpoch||generation!==state.generation) return;
    supervisor.data={root,overview,activity,observations,tasks};
    for (const [id,count] of [["attention-badge",overview.task_summary.attention_tasks],["tasks-badge",overview.task_summary.active_tasks]]) {$(id).textContent=count;$(id).hidden=!count;}
    renderSupervisor();renderSupervisorConversation();
  } catch (error) {if(serial===supervisor.serial&&root===state.rootId&&epoch===state.identityEpoch) {supervisor.data={root,error:error.message};renderSupervisor();}}
}
function scheduleSupervisor() {clearTimeout(supervisor.timer);supervisor.timer=setTimeout(refreshSupervisor,80);}
function openOverviewDialog() {$("overview-open").click();}

renderThread=function(){
  if (supervisor.data?.root===state.rootId && supervisor.data.activity) renderSupervisorConversation();
  else {clear($("messages"));$("messages").append(node("p","loading-line","正在汇集主会话动态…"));}
};
const originalInspection=renderInspection;
renderInspection=function(){
  originalInspection();
  const snapshot=state.thread.find(item=>item.investigation.investigation_id===state.inspectId)||state.supplementalInspection||state.snapshot;
  if (snapshot) {
    const key=JSON.stringify(snapshot);
    if(key!==supervisor.detailKey) {supervisor.detailKey=key;renderMessages(snapshot,false,$("task-detail-messages"));}
  }
};
const originalShowTab=showTab;
showTab=function(tab){originalShowTab(tab);$("panel-thread").hidden=tab!=="thread";};
openContext=function(){document.body.classList.add("details-open");$("inspector").classList.add("open");$("sidebar").classList.remove("open");$("drawer-backdrop").hidden=false;$("context-toggle").setAttribute("aria-expanded","true");};
const originalCloseDrawers=closeDrawers;
closeDrawers=function(){originalCloseDrawers();document.body.classList.remove("details-open");};
const originalOpenConversation=openConversation;
openConversation=async function(id){supervisorView("conversation");return originalOpenConversation(id);};
const originalSendConversation=sendConversationMessage;
sendConversationMessage=async function(){supervisorView("conversation");return originalSendConversation();};
const originalResetConversation=resetConversation;
resetConversation=function(initial){
  for(const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  clear($("task-detail-messages"));supervisor.detailKey=null;$("reply-to-task").dataset.taskId="";
  supervisor.data=null;supervisor.taskOffset=0;supervisor.serial++;$("attention-badge").hidden=true;$("tasks-badge").hidden=true;originalResetConversation(initial);supervisorView("overview");};

document.addEventListener("trace:conversation",()=>{
  if (supervisor.lastRoot!==state.rootId) {supervisor.data=null;supervisor.taskOffset=0;supervisor.lastRoot=state.rootId;}
  $("destination-label").textContent=$("auto-route").checked?"发送给主会话 · 自动协调":"回复指定调查 · "+(state.snapshot?.investigation.request.objective.description||"");
  $("reply-clear").hidden=$("auto-route").checked;
  scheduleSupervisor();
});
document.addEventListener("DOMContentLoaded",()=>{
  for (const button of document.querySelectorAll("[data-view]")) button.onclick=()=>{supervisorView(button.dataset.view);scheduleSupervisor();};
  $("board-refresh").onclick=async()=>{supervisor.taskOffset=0;try {state.config=await api("/v1/workspace");} catch(error){toast(error.message,true);}await refreshSupervisor();};
  $("context-close").onclick=closeDrawers;$("drawer-backdrop").onclick=closeDrawers;
  $("reply-to-task").onclick=async()=>{const id=$("reply-to-task").dataset.taskId||state.taskId;if(!id)return;await selectTask(id);$("auto-route").checked=false;$("reply-clear").hidden=false;$("destination-label").textContent="回复指定调查 · "+(state.snapshot?.investigation.request.objective.description||"");supervisorView("conversation");$("prompt").focus();};
  $("reply-clear").onclick=()=>{$("auto-route").checked=true;$("reply-clear").hidden=true;$("destination-label").textContent="发送给主会话 · 自动协调";};
  document.addEventListener("keydown",event=>{if(event.key==="Escape")closeDrawers();});
  supervisorView("overview");scheduleSupervisor();
});
