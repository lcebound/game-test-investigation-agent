"""Headless real-browser validation against an isolated, model-free workbench."""

from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def main(output):
    from playwright.sync_api import sync_playwright
    import uvicorn
    from game_test_agent.api import create_app
    from game_test_agent.config import Settings
    from game_test_agent.storage import Repository
    from game_test_agent.contracts.models import InvestigationRequest, Resolution
    from game_test_agent.reports import ReportService
    from game_test_agent.reports.builder import ReportBuilder
    from game_test_agent.events.observation_router import ObservationBatchRouter
    from game_test_agent.events.semantic_router import SemanticConversationRouter

    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="gta-harness-browser-") as folder:
        settings = Settings(
            database_path=Path(folder) / "browser.db",
            artifact_root=Path(folder) / "artifacts",
            enable_model=False,
            enable_router_recovery=False,
        )
        repo = Repository(settings.database_path)

        def create(key, root=None):
            return repo.create_investigation(
                InvestigationRequest(
                    tenant_id="browser-qa",
                    project_id="harness-qa",
                    objective={
                        "objective_id": "review",
                        "description": {
                            "First bounded investigation": "登录闪退调查",
                            "Second bounded investigation": "战斗帧耗时异常",
                        }.get(key, key),
                    },
                    budget={"max_real_runs": 0},
                ),
                key,
                conversation_root_id=root,
            )[0]

        root = create("First bounded investigation")
        other = create("Second bounded investigation", root.investigation_id)
        for view in (root, other):
            report = ReportBuilder(repo).build(
                view.investigation_id,
                "browser-fixture",
                status="failed" if view.investigation_id == other.investigation_id else "completed",
                resolution=Resolution.SYSTEM_ERROR
                if view.investigation_id == other.investigation_id
                else Resolution.INCONCLUSIVE,
                summary="调查受阻：尚未取得可核验日志，需要补充证据。"
                if view.investigation_id == other.investigation_id
                else "现有证据不足，暂不能确认根因，需要匹配的日志与运行信息。",
            )
            ReportService(repo).publish(report)
            with repo.transaction() as connection:
                connection.execute(
                    "UPDATE investigations SET status=? WHERE investigation_id=?",
                    (
                        "failed"
                        if view.investigation_id == other.investigation_id
                        else "completed",
                        view.investigation_id,
                    ),
                )
        router = ObservationBatchRouter(repo, SemanticConversationRouter(repo))
        key = router.accept(
            {
                "event_type": "game_test.observation",
                "event_id": "browser-event",
                "source": "fixture",
                "tenant_id": "browser-qa",
                "project_id": "harness-qa",
                "conversation_id": root.investigation_id,
                "thread_key": "fixture",
                "kind": "counterevidence",
                "summary": "Unresolved source event",
                "details": {"original": "untrusted event content"},
            }
        )["event_key"]
        now = datetime.now(timezone.utc).isoformat()
        with repo.transaction() as connection:
            connection.execute(
                "UPDATE coordination_observations SET state='unresolved' WHERE event_key=?", (key,)
            )
            connection.execute(
                """INSERT INTO native_tool_jobs (job_id,investigation_id,call_id,provider_id,tool_name,request_json,request_hash,config_hash,state,polls,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "browser-operation",
                    root.investigation_id,
                    "fixture",
                    "fixture",
                    "scan_fixture",
                    "{}",
                    "hash",
                    "hash",
                    "collected",
                    2,
                    now,
                    now,
                ),
            )
        repo.close()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("isolated browser server failed")
                time.sleep(0.05)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    identity = {
                        "tenant": "browser-qa",
                        "project": "harness-qa",
                        "principal": "browser-tester",
                        "scopes": "tests:read,events:write",
                    }
                    page.add_init_script(
                        "localStorage.setItem('trace.workspace', "
                        + json.dumps(json.dumps(identity))
                        + ");"
                    )
                    page.goto(f"http://127.0.0.1:{port}")
                    page.wait_for_function("state.config !== null")
                    page.evaluate("id => selectInvestigation(id)", root.investigation_id)
                    page.wait_for_function("!state.busy && state.rootId !== null")
                    # Exercise the actual routing API and frontend adapter only;
                    # stop before POSTing any message/task or dispatching a Worker.
                    routes = page.evaluate("""async () => {
                      const base = `/v1/conversations/${encodeURIComponent(state.rootId)}`;
                      const bound = {key: crypto.randomUUID(), body: {content:'新任务：这是任务内的一条原始消息', expected_investigation_id:state.id}, routing:{base,context:state.id,bound:state.id,newTaskBody:investigationBody('placeholder')}};
                      await resolveMessageRoute(bound,bound.body.content,state.generation);
                      const fresh = {key:crypto.randomUUID(),body:{content:'新任务：检查加载超时',expected_investigation_id:state.id},routing:{base,context:state.id,newTaskBody:investigationBody('placeholder')}};
                      await resolveMessageRoute(fresh,fresh.body.content,state.generation);
                      return {bound:bound.route.kind,boundId:bound.route.investigation_id,newKind:fresh.route.kind,newPath:fresh.path};
                    }""")
                    assert routes["bound"] == "task" and routes["boundId"] == root.investigation_id
                    assert routes["newKind"] == "new_task" and routes["newPath"].endswith("/tasks")
                    page.get_by_role("button", name="概览", exact=False).first.click()
                    page.locator(".supervisor-stats").wait_for()
                    page.screenshot(path=str(output / "supervisor-overview.png"), full_page=True)
                    page.locator("#theme-toggle").click()
                    assert page.evaluate("document.body.dataset.theme") == "dark"
                    page.screenshot(path=str(output / "supervisor-dark.png"), full_page=True)
                    page.locator("#theme-toggle").click()
                    page.get_by_role("button", name="后台调查", exact=False).first.click()
                    page.locator("#board-content .supervisor-task").first.wait_for()
                    assert page.locator("#board-content .supervisor-task").count() == 2
                    page.get_by_role("button", name="查看详情", exact=True).first.click()
                    page.locator("#inspector.open").wait_for()
                    page.get_by_role("tab", name="交流与授权", exact=True).click()
                    page.locator("#task-detail-messages .message").first.wait_for()
                    page.screenshot(path=str(output / "supervisor-detail.png"), full_page=True)
                    page.locator("#context-close").click()
                    page.get_by_role("button", name="主会话", exact=True).click()
                    page.locator(".coordinator-message").first.wait_for()
                    assert page.locator(".coordinator-message.user").count() == 2
                    assert page.locator(".coordinator-message.report").count() == 2
                    page.screenshot(
                        path=str(output / "supervisor-conversation.png"), full_page=True
                    )
                    page.get_by_role("button", name="信息来源", exact=True).click()
                    page.get_by_text("当前项目尚未登记事件源。", exact=True).wait_for()
                    page.get_by_role("button", name="事件与判断", exact=True).click()
                    page.get_by_role("button", name="原文与处理", exact=True).first.click()
                    page.get_by_role("button", name="查看原事件", exact=True).click()
                    page.get_by_text("untrusted event content", exact=False).wait_for()
                    page.get_by_role("button", name="重新判断归属", exact=True).click()
                    page.get_by_text("等待自动归属", exact=True).wait_for()
                    page.get_by_text("scan_fixture", exact=False).wait_for()
                    page.locator('#harness-content input[type="checkbox"]').nth(0).check()
                    page.locator('#harness-content input[type="checkbox"]').nth(1).check()
                    page.get_by_role("button", name="生成综合", exact=True).click()
                    page.get_by_text(
                        "跨任务推断仍需验证，不代表已确认共同根因。", exact=True
                    ).wait_for()
                    page.get_by_role("button", name="查看来源：", exact=False).first.click()
                    page.locator("#harness-content section pre").wait_for()
                    assert (
                        root.investigation_id
                        in page.locator("#harness-content section pre").inner_text()
                    )
                    page.screenshot(path=str(output / "harness-desktop.png"), full_page=True)
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(output / "harness-mobile.png"), full_page=True)
                    page.get_by_role("button", name="关闭", exact=True).last.click()
                    page.evaluate("supervisorView('overview')")
                    page.screenshot(path=str(output / "supervisor-mobile.png"), full_page=True)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    # A new identity must not retain the previous conversation,
                    # task transcript or open source dialog in the visible UI.
                    page.evaluate("""() => {
                        supervisorView('conversation');
                        state.identityEpoch++;
                        state.identity={...state.identity,project:'different-project'};
                        state.config=null;
                        newChat(true);
                    }""")
                    assert page.evaluate("state.rootId === null && supervisor.data === null")
                    assert page.locator("#task-detail-messages").inner_text() == ""
                    assert page.locator("dialog[open]").count() == 0
                    assert page.evaluate("!document.body.classList.contains('details-open')")
                    assert errors == [], errors
                finally:
                    browser.close()
            (output / "result.json").write_text(
                json.dumps(
                    {
                        "passed": True,
                        "scenarios": [
                            "supervisor_overview",
                            "all_tasks",
                            "unified_conversation",
                            "task_details",
                            "source_status",
                            "bound_reply",
                            "main_route_new_task",
                            "event_original",
                            "retry_unresolved",
                            "operation_progress",
                            "task_synthesis",
                            "source_report",
                            "mobile_width",
                        ],
                        "model_calls": 0,
                        "real_runs": 0,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            print("Harness browser validation passed")
        finally:
            server.should_exit = True
            thread.join(15)
            if thread.is_alive():
                raise RuntimeError("isolated browser server did not stop")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("data/local/harness-browser"))
    main(parser.parse_args().output)
