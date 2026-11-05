"use strict";

function investigationBody(content) {
  return {
    tenant_id: state.identity.tenant,
    project_id: state.identity.project,
    objective: {
      objective_id: "objective-" + crypto.randomUUID(), description: content,
      acceptance_criteria: state.options.criteria.split("\n").map(s => s.trim()).filter(Boolean),
    },
    constraints: constraints(), budget: budget(),
    ...(state.options.build ? {build_ref: {id: state.options.build}} : {}),
  };
}

function clearMessageRouting() {
  if ($("routing-dialog").open) $("routing-dialog").close();
  clear($("routing-choices"));
}

function clarifyMessageRoute(request, generation) {
  const route = request.route, target = $("routing-choices");
  clear(target);
  $("routing-message").textContent = route.message;
  for (const choice of route.choices || []) {
    const button = node("button", "outline-button routing-choice");
    button.append(node("strong", "", choice.objective_excerpt),
      node("small", "", labels[choice.status] || choice.status));
    button.onclick = () => {
      if (state.busy || generation !== state.generation || state.pendingMutation?.request !== request) return;
      request.route = {kind: "task", ...choice};
      clearMessageRouting();
      sendConversationMessage();
    };
    target.append(button);
  }
  $("routing-more").hidden = !route.more_tasks;
  $("routing-dialog").showModal();
}

async function resolveMessageRoute(request, content, generation) {
  if (!request.routing) return true;
  if (!request.route) {
    request.route = await api(`${request.routing.base}/messages/route`, {
      method: "POST", key: request.key,
      body: {content, context_investigation_id: request.routing.context,
        ...(request.routing.bound ? {bound_investigation_id: request.routing.bound} : {})},
    });
  }
  if (generation !== state.generation) return false;
  const route = request.route;
  if (route.kind === "clarify") {
    clarifyMessageRoute(request, generation);
    return false;
  }
  if (route.kind === "new_task") {
    request.path = `${request.routing.base}/tasks`;
    request.body = request.routing.newTaskBody;
    request.body.objective.description = route.goal;
  } else if (route.kind === "task") {
    request.path = `${request.routing.base}/messages`;
    // Another task keeps its own settings. This message carries only the user's text.
    if (request.body.expected_investigation_id !== route.investigation_id)
      request.body = {content, expected_investigation_id: route.investigation_id};
  } else throw new Error("无法识别消息归属，请刷新后重试。");
  return true;
}
