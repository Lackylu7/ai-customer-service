from pathlib import Path

from playwright.sync_api import Page, sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "http://127.0.0.1:5001"


def read_operations_key() -> str:
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


def verify_desktop(browser, key: str, console_errors: list[str]) -> None:
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.goto(f"{BASE_URL}/operations")
    page.wait_for_load_state("networkidle")
    unlock(page, key, verify_wrong_key=True)
    assert page.locator("#serviceStatus").get_attribute("data-status") == "online"
    assert page.locator("#trendChart").locator("*").count() > 0
    assert page.locator("#reasonChart").locator("*").count() > 0
    assert page.evaluate("sessionStorage.getItem('operationsKey') !== null")
    assert_no_horizontal_overflow(page)

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
    page.goto(f"{BASE_URL}/operations")
    page.wait_for_load_state("networkidle")
    unlock(page, key)
    assert_no_horizontal_overflow(page)
    assert page.locator("#kpiGrid").is_visible()
    assert page.locator("#trendChart").is_visible()
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
        message for message in console_errors if "401 (Unauthorized)" not in message
    ]
    if unexpected_errors:
        raise AssertionError(f"Browser console errors: {unexpected_errors}")
    print("PASS: Operations dashboard desktop, mobile, auth, filters, and CSV are verified.")


if __name__ == "__main__":
    main()
