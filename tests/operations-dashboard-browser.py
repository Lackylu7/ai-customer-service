import json
import os
from pathlib import Path
from time import monotonic

from playwright.sync_api import Page, expect, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = os.getenv("OPERATIONS_BASE_URL", "http://127.0.0.1:5001")


def wait_for_text_change(
    page: Page,
    selector: str,
    placeholder: str,
    *,
    timeout_seconds: float = 15,
) -> None:
    locator = page.locator(selector)
    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        if locator.text_content() != placeholder:
            return
        page.wait_for_timeout(100)
    page_message = page.locator("#pageMessage").text_content()
    raise AssertionError(
        f"{selector} did not leave its placeholder; page message={page_message!r}"
    )


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
        expect(page.locator("#keyDialog")).to_have_attribute("open", "")
        expect(page.locator("#keyError")).to_contain_text("不正确")
    page.fill("#keyInput", key)
    page.click("#keyForm button[type=submit]")
    wait_for_text_change(page, "#kpiUnique", "—")


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


def install_dingtalk_review_routes(page: Page) -> None:
    items = [
        {
            "ticket_code": "CS-20260727-000004",
            "question": "股东制3.0中，每家门店的风险金标准是多少？",
            "answer": "当前通用口径为每店15万元。",
            "domain": "shareholder",
            "action": "allow",
            "reason": None,
            "feedback": "unresolved",
            "status": "needs_human",
            "owner": None,
            "final_answer": None,
            "reviewed_by": None,
            "knowledge_status": "not_reviewed",
            "created_at": 1_785_145_303,
            "updated_at": 1_785_145_312,
        }
    ]

    def list_handler(route) -> None:
        status = route.request.url.split("status=", 1)[1].split("&", 1)[0]
        visible = items if status == "all" else [
            item for item in items if item["status"] == status
        ]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "items": visible,
                    "total": len(visible),
                    "limit": 50,
                    "offset": 0,
                },
                ensure_ascii=False,
            ),
        )

    def claim_handler(route) -> None:
        payload = route.request.post_data_json
        item = items[0]
        owner = payload.get("owner", "")
        if item["owner"] not in (None, owner):
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "DingTalk interaction cannot be claimed."}),
            )
            return
        item["owner"] = owner
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(item, ensure_ascii=False),
        )

    def resolve_handler(route) -> None:
        payload = route.request.post_data_json
        item = items[0]
        if item["owner"] != payload.get("owner"):
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": "DingTalk interaction cannot be resolved."}),
            )
            return
        item.update(
            {
                "final_answer": payload["final_answer"],
                "reviewed_by": payload["reviewed_by"],
                "knowledge_status": payload["knowledge_status"],
                "status": "resolved",
            }
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(item, ensure_ascii=False),
        )

    page.route("**/v1/operations/dingtalk-interactions?**", list_handler)
    page.route(
        "**/v1/operations/dingtalk-interactions/*/claim",
        claim_handler,
    )
    page.route(
        "**/v1/operations/dingtalk-interactions/*/resolve",
        resolve_handler,
    )


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
    install_dingtalk_review_routes(page)
    page.goto(f"{BASE_URL}/operations")
    page.wait_for_load_state("networkidle")
    unlock(page, key, verify_wrong_key=True)
    assert page.locator("#serviceStatus").get_attribute("data-status") == "online"
    expect(page.locator("[data-queue-health]")).not_to_have_attribute(
        "data-status", "loading"
    )
    assert page.locator("[data-worker-heartbeat]").text_content() != "—"
    assert page.locator("[data-failed-jobs]").text_content() != "—"
    assert page.locator("#trendChart").locator("*").count() > 0
    assert page.locator("#reasonChart").locator("*").count() > 0
    page.wait_for_selector('.human-item[data-conversation-id="42"]')
    page.wait_for_selector(
        '.interaction-item[data-ticket-code="CS-20260727-000004"]'
    )
    assert page.locator('.sla-badge[data-status="normal"]').get_by_text("等待中").count() == 1
    assert page.locator('.sla-badge[data-status="due_soon"]').get_by_text("即将超时").count() == 1
    assert page.locator('.sla-badge[data-status="overdue"]').get_by_text("已超时").count() == 1
    assert page.evaluate("sessionStorage.getItem('operationsKey') !== null")
    assert_no_horizontal_overflow(page)

    review_item = page.locator(
        '.interaction-item[data-ticket-code="CS-20260727-000004"]'
    )
    review_item.locator('input[id^="owner-"]').fill("小草")
    review_item.get_by_role("button", name="领取问题").click()
    expect(review_item.locator(".interaction-feedback").first).to_have_text("已领取")
    review_item.locator('textarea[id^="answer-"]').fill(
        "风险金标准需按当前审核口径执行。"
    )
    review_item.locator('input[id^="reviewer-"]').fill("稳哥")
    review_item.locator('select[id^="knowledge-"]').select_option("not_reusable")
    review_item.get_by_role("button", name="确认已回复并完成").click()
    expect(
        page.locator('.interaction-item[data-ticket-code="CS-20260727-000004"]')
    ).to_have_count(0)
    page.select_option("#dingtalkInteractionStatus", "resolved")
    page.wait_for_selector(
        '.interaction-item[data-ticket-code="CS-20260727-000004"]'
    )
    expect(page.locator("#dingtalkInteractionList")).to_contain_text("仅个案处理")

    page.click('button[data-resume-conversation="42"]')
    assert page.locator("#resumeDialog").get_attribute("open") is not None
    page.click("#resumeCancelButton")
    assert page.locator("#resumeDialog").get_attribute("open") is None

    page.click('button[data-resume-conversation="42"]')
    page.click("#resumeConfirmButton")
    expect(page.locator("#resumeError")).to_contain_text("投递中")
    page.click("#resumeConfirmButton")
    expect(page.locator("#resumeDialog")).to_be_hidden()
    expect(page.locator('[data-conversation-id="42"]')).to_have_count(0)

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
    install_dingtalk_review_routes(page)
    page.goto(f"{BASE_URL}/operations")
    page.wait_for_load_state("networkidle")
    unlock(page, key)
    expect(page.locator("[data-queue-health]")).not_to_have_attribute(
        "data-status", "loading"
    )
    assert_no_horizontal_overflow(page)
    assert page.locator("[data-queue-health]").is_visible()
    assert page.locator("#kpiGrid").is_visible()
    assert page.locator("#trendChart").is_visible()
    page.wait_for_selector('.human-item[data-conversation-id="42"]')
    page.wait_for_selector(
        '.interaction-item[data-ticket-code="CS-20260727-000004"]'
    )
    assert page.locator("#humanConversationList").is_visible()
    assert page.locator("#dingtalkInteractionList").is_visible()
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
        try:
            verify_desktop(browser, key, console_errors)
            verify_mobile(browser, key, console_errors)
        finally:
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
