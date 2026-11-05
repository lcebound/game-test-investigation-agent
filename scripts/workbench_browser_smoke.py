"""Optional browser QA. Run against an isolated local Agent, with no provider configured.

Install Playwright separately; this creates bounded no-execution investigations.
"""

import argparse
import json
import uuid
from pathlib import Path

from playwright.sync_api import sync_playwright


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--output", default="data/workbench-qa/screenshots")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1440, "height": 960}, device_scale_factor=1, reduced_motion="reduce"
        )
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        identity = {
            "tenant": "workbench-browser-qa",
            "project": "qa-" + uuid.uuid4().hex,
            "principal": "qa-tester",
            "scopes": "tests:read",
        }
        page.add_init_script(
            "localStorage.setItem('trace.workspace', " + json.dumps(json.dumps(identity)) + ");"
        )
        page.goto(args.url)
        page.wait_for_function("state.config !== null")
        # Never dispatch a platform test during this UI-only check.
        assert page.evaluate("state.config.providers.length") == 0
        page.screenshot(path=str(output / "welcome.png"), full_page=True)
        page.locator("#task-options").click()
        page.locator('#options-form input[name="runs"]').fill("0")
        page.locator('#options-form input[name="reads"]').fill("7")
        page.locator('#options-form input[name="build"]').fill("qa-build-original")
        page.locator('#options-form input[name="testId"]').fill("qa-case-original")
        page.locator("#options-form button.primary-button").click()
        page.locator("[data-prompt]").first.click()
        page.locator("#send").click()
        page.wait_for_function("state.snapshot?.investigation.status === 'completed'")
        page.locator(".report-download").wait_for()
        assert page.evaluate("state.snapshot.runs.length") == 0
        assert page.evaluate("state.snapshot.investigation.request.budget.max_tool_reads") == 7
        assert (
            page.evaluate("state.snapshot.investigation.request.build_ref.id")
            == "qa-build-original"
        )
        assert page.evaluate("state.snapshot.report.report.resolution") == "inconclusive"
        first_id = page.evaluate("state.id")
        page.locator('[data-tab="execution"]').click()
        page.screenshot(path=str(output / "investigation.png"), full_page=True)
        with page.expect_download() as download:
            page.locator(".report-download").click()
        assert download.value.suggested_filename.endswith("-report.md")
        page.locator("#task-options").click()
        page.locator('#options-form input[name="reads"]').fill("2")
        page.locator('#options-form input[name="build"]').fill("qa-build-followup")
        page.locator('#options-form input[name="testId"]').fill("qa-case-followup")
        page.locator("#options-form button.primary-button").click()
        page.locator("#prompt").fill("补充：解释还缺少哪些运行证据。")

        def lose_message_response(route):
            # Commit on the real backend, then hide the response from the client.
            real = route.fetch()
            assert real.status == 202
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"detail": "QA response lost after commit"}),
            )

        page.route("**/v1/conversations/*/messages", lose_message_response)
        page.locator("#send").click()
        page.wait_for_function("!state.busy && state.pendingMutation !== null")
        assert page.locator("#prompt").input_value() == "补充：解释还缺少哪些运行证据。"
        page.unroute("**/v1/conversations/*/messages", lose_message_response)
        page.locator("#send").click()
        page.wait_for_function(
            "previous => state.id !== previous && state.snapshot?.investigation.status === 'completed'",
            arg=first_id,
        )
        assert page.evaluate("state.history.length") == 1
        assert page.evaluate("state.thread.length") == 2
        assert page.evaluate("state.rootId") == first_id
        assert (
            page.evaluate("state.snapshot.investigation.request.build_ref.id")
            == "qa-build-followup"
        )
        assert page.evaluate("state.snapshot.investigation.request.budget.max_tool_reads") == 2
        assert (
            page.evaluate(
                "state.snapshot.investigation.request.constraints.action_parameters.test_ref.test_id"
            )
            == "qa-case-followup"
        )
        page.locator("#rename-chat").click()
        page.locator('#conversation-form input[name="title"]').fill("QA 持续对话")
        page.locator('#conversation-form input[name="pinned"]').check()
        page.locator("#conversation-form button.primary-button").click()
        page.wait_for_function(
            "state.conversation.title === 'QA 持续对话' && state.conversation.pinned"
        )
        page.locator("#prompt").fill("这是一条尚未发送的草稿")
        page.reload()
        page.wait_for_function(
            "state.thread.length === 2 && state.conversation.title === 'QA 持续对话'"
        )
        assert page.locator("#prompt").input_value() == "这是一条尚未发送的草稿"
        assert page.locator(".report-download").count() == 2
        page.locator("#run-selector").select_option(first_id)
        assert page.evaluate("state.inspectId") == first_id
        page.locator("#history-search").fill("不存在的查询")
        page.wait_for_function("state.history.length === 0")
        page.locator("#history-search").fill("QA 持续对话")
        page.wait_for_function("state.history.length === 1")
        page.locator("#history-search").fill("")
        page.locator("#archive-chat").click()
        page.wait_for_function("state.conversation.archived && state.history.length === 0")
        assert page.locator("#send").is_disabled()
        page.locator("#history-archived").click()
        page.wait_for_function("state.history.length === 1")
        page.locator("#archive-chat").click()
        page.wait_for_function("!state.conversation.archived && state.history.length === 0")
        page.locator("#history-archived").click()
        page.wait_for_function("state.history.length === 1")
        page.locator("#theme-toggle").click()
        assert page.evaluate("document.body.dataset.theme") == "dark"
        page.screenshot(path=str(output / "dark-conversation.png"), full_page=True)
        page.locator("#theme-toggle").click()
        page.screenshot(path=str(output / "conversation.png"), full_page=True)
        page.locator("#connections").click()
        assert page.locator("#guide-integration").count() == 0
        assert "平台 API" not in page.locator("#connection-dialog").inner_text()
        page.locator("#guide-mcp").click()
        assert "GTA_MCP_PROVIDERS_JSON" in page.locator("#connection-config").inner_text()
        assert page.evaluate("state.guideConfig").splitlines()[0] == "GTA_MCP_NATIVE_TOOLS=true"
        generic = json.loads(page.evaluate("state.guideConfig").splitlines()[1].split("=", 1)[1])
        assert "contract" not in generic[0] and generic[0]["url"].endswith("/mcp")
        assert "token_env" not in generic[0]
        page.locator("#guide-lyra").click()
        lyra = json.loads(page.evaluate("state.guideConfig").splitlines()[1].split("=", 1)[1])
        assert "contract" not in lyra[0]
        assert lyra[0]["url"] == "http://127.0.0.1:8010/mcp"
        page.screenshot(path=str(output / "connection-guide.png"), full_page=True)
        page.locator("#connection-dialog [data-close]").first.click()
        page.locator("#settings").click()
        page.locator('#settings-form input[name="token"]').fill("browser-memory-only-test")
        page.locator("#settings-form button.primary-button").click()
        page.wait_for_function("state.identity.token === 'browser-memory-only-test'")
        assert "browser-memory-only-test" not in page.evaluate(
            "localStorage.getItem('trace.workspace')"
        )
        page.reload()
        page.wait_for_function("state.config !== null && state.thread.length === 2")
        assert page.evaluate("state.identity.token") == ""
        page.set_viewport_size({"width": 390, "height": 844})
        page.locator("#context-toggle").click()
        assert page.locator("#inspector").is_visible()
        page.screenshot(path=str(output / "mobile-context.png"), full_page=True)
        page.locator("#context-close").click()
        assert not page.locator("#inspector").is_visible()
        page.locator("#new-chat").evaluate("el => el.click()")
        page.screenshot(path=str(output / "mobile.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator("#menu").click()
        page.locator("#new-chat").click()
        assert not page.locator("#drawer-backdrop").is_visible()
        page.locator("#menu").click()
        page.locator("#settings").click()
        assert page.locator("#settings-dialog").is_visible()
        assert errors == [], errors
        browser.close()
        print(
            f"Browser QA passed: grouped conversations, retry-safe submission, metadata, search, archive, drafts, reports, theme, token storage, mobile. Screenshots: {output}"
        )


if __name__ == "__main__":
    main()
