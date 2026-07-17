import json
import os
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.getenv("OPERATIONS_BASE_URL", "http://127.0.0.1:5001")


def read_operations_key() -> str:
    configured_key = os.getenv("OPERATIONS_API_KEY", "").strip()
    if configured_key:
        return configured_key
    for line in (PROJECT_ROOT / ".env").read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("OPERATIONS_API_KEY="):
            key = line.split("=", 1)[1].strip()
            if key:
                return key
    raise RuntimeError("Operations API key is not configured.")


def unlock(page: Page, key: str, *, verify_wrong_key: bool = False) -> None:
    page.wait_for_selector("#keyDialog[open]")
    if verify_wrong_key:
        page.fill("#keyInput", "wrong-browser-test-key")
        page.click("#keyForm button[type=submit]")
        page.wait_for_function(
            "document.querySelector('#keyDialog').open && "
            "document.querySelector('#keyError').textContent.includes('不正确')"
        )
    page.fill("#keyInput", key)
    page.click("#keyForm button[type=submit]")
    page.wait_for_function(
        "document.querySelector('#kpiUnique').textContent !== '—'"
    )


def assert_no_horizontal_overflow(page: Page) -> None:
    dimensions = page.evaluate(
        "({ width: document.documentElement.clientWidth, "
        "scrollWidth: document.documentElement.scrollWidth })"
    )
    if dimensions["scrollWidth"] > dimensions["width"]:
        raise AssertionError(f"Horizontal overflow detected: {dimensions}")


def install_handoff_routes(page: Page) -> None:
    items = [
        {
            "conversation_id": 41,
            "handoff_reason": "explicit_human_request",
            "human_since": 1_720_000_000,
            "last_customer_message_at": 1_720_000_100,
            "last_human_reply_at": None,
            "last_fallback_at": None,
            "waiting_seconds": 120,
            "sla_status": "normal",
            "has_blocking_job": False,
            "chatwoot_url": "http://127.0.0.1:3000/app/accounts/1/conversations/41",
        },
        {
            "conversation_id": 42,
            "handoff_reason": "knowledge_gap",
            "human_since": 1_720_000_000,
            "last_customer_message_at": 1_720_000_100,
            "last_human_reply_at": None,
            "last_fallback_at": 1_720_000_300,
            "waiting_seconds": 300,
            "sla_status": "overdue",
            "has_blocking_job": True,
            "chatwoot_url": "http://127.0.0.1:3000/app/accounts/1/conversations/42",
        },
        {
            "conversation_id": 43,
            "handoff_reason": "complaint_or_legal",
            "human_since": 1_720_000_000,
            "last_customer_message_at": 1_720_000_100,
            "last_human_reply_at": None,
            "last_fallback_at": None,
            "waiting_seconds": 240,
            "sla_status": "due_soon",
            "has_blocking_job": False,
            "chatwoot_url": "http://127.0.0.1:3000/app/accounts/1/conversations/43",
        },
    ]
    resume_attempts = 0

    def list_handler(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"items": items}, ensure_ascii=False),
        )

    def resume_handler(route) -> None:
        nonlocal resume_attempts
        conversation_id = int(route.request.url.split("/conversations/")[1].split("/")[0])
        resume_attempts += 1
        if conversation_id == 42 and resume_attempts == 1:
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "Conversation has a blocking job."}),
            )
            return
        items[:] = [
            item for item in items if item["conversation_id"] != conversation_id
        ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "resumed"}),
        )

    page.route("**/v1/operations/human-conversations", list_handler)
    page.route("**/v1/operations/conversations/*/resume-ai", resume_handler)


def verify_desktop(browser, key: str, console_errors: list[str]) -> None:
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    install_handoff_routes(page)
    page.goto(f"{BASE_URL}/operations")
    page.wait_for_load_state("networkidle")
    unlock(page, key, verify_wrong_key=True)
    assert page.locator("#serviceStatus").get_attribute("data-status") == "online"
    page.wait_for_function(
        "document.querySelector('[data-queue-health]').dataset.status !== 'loading'"
    )
    assert page.locator("[data-worker-heartbeat]").text_content() != "—"
    assert page.locator("[data-failed-jobs]").text_content() != "—"
    assert page.locator("#trendChart").locator("*").count() > 0
    assert page.locator("#reasonChart").locator("*").count() > 0
    page.wait_for_selector('.human-item[data-conversation-id="42"]')
    assert page.locator('.sla-badge[data-status="normal"]').get_by_text("等待中").count() == 1
    assert page.locator('.sla-badge[data-status="due_soon"]').get_by_text("即将超时").count() == 1
    assert page.locator('.sla-badge[data-status="overdue"]').get_by_text("已超时").count() == 1
    assert page.evaluate("sessionStorage.getItem('operationsKey') !== null")
    assert_no_horizontal_overflow(page)

    page.click('button[data-resume-conversation="42"]')
    assert page.locator("#resumeDialog").get_attribute("open") is not None
    page.click("#resumeCancelButton")
    assert page.locator("#resumeDialog").get_attribute("open") is None

    page.click('button[data-resume-conversation="42"]')
    page.click("#resumeConfirmButton")
    page.wait_for_function(
        "document.querySelector('#resumeError').textContent.includes('投递中')"
    )
    page.click("#resumeConfirmButton")
    page.wait_for_function("!document.querySelector('#resumeDialog').open")
    page.wait_for_function(
        "document.querySelector('[data-conversation-id=\"42\"]') === null"
    )

    with page.expect_response("**/v1/operations/summary?window=30d") as response_info:
        page.click('button[data-window="30d"]')
    assert response_info.value.status == 200

    with page.expect_response("**/v1/operations/gaps?status=all**") as response_info:
        page.select_option("#gapStatus", "all")
    assert response_info.value.status == 200
    page.wait_for_selector(".gap-item")

    with page.expect_download() as download_info:
        page.click("#exportEvents")
    assert download_info.value.suggested_filename == "operations-events-30d.csv"

    page.screenshot(
        path=str(PROJECT_ROOT / ".runtime" / "operations-dashboard-desktop.png"),
        full_page=True,
    )
    context.close()


def verify_mobile(browser, key: str, console_errors: list[str]) -> None:
    context = browser.new_context(viewport={"width": 360, "height": 800})
    page = context.new_page()
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    install_handoff_routes(page)
    page.goto(f"{BASE_URL}/operations")
    page.wait_for_load_state("networkidle")
    unlock(page, key)
    page.wait_for_function(
        "document.querySelector('[data-queue-health]').dataset.status !== 'loading'"
    )
    assert_no_horizontal_overflow(page)
    assert page.locator("[data-queue-health]").is_visible()
    assert page.locator("#kpiGrid").is_visible()
    assert page.locator("#trendChart").is_visible()
    page.wait_for_selector('.human-item[data-conversation-id="42"]')
    assert page.locator("#humanConversationList").is_visible()
    assert page.locator('button[data-resume-conversation="42"]').is_visible()
    page.click('button[data-resume-conversation="42"]')
    assert page.locator("#resumeDialog").is_visible()
    assert_no_horizontal_overflow(page)
    page.click("#resumeCancelButton")
    page.select_option("#gapStatus", "all")
    page.wait_for_selector(".gap-item")
    assert page.locator(".gap-item").first.is_visible()
    page.screenshot(
        path=str(PROJECT_ROOT / ".runtime" / "operations-dashboard-mobile.png"),
        full_page=True,
    )
    context.close()


def main() -> None:
    key = read_operations_key()
    console_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        verify_desktop(browser, key, console_errors)
        verify_mobile(browser, key, console_errors)
        browser.close()
    unexpected_errors = [
        message
        for message in console_errors
        if "401 (Unauthorized)" not in message
        and "409 (Conflict)" not in message
    ]
    if unexpected_errors:
        raise AssertionError(f"Browser console errors: {unexpected_errors}")
    print(
        "PASS: Operations dashboard desktop, mobile, auth, handoff resume, "
        "filters, and CSV are verified."
    )


if __name__ == "__main__":
    main()
