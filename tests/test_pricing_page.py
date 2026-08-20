"""
The public pricing page, rendered in a real browser.

Tested through Chromium rather than by parsing the HTML, because the thing
worth testing is what a prospective customer SEES: whether a price appears,
whether it is the backend's price, and -- most importantly -- whether anything
appears when the backend is unreachable.

The last one is the point of the file. This page is where somebody decides to
pay. A hardcoded price here would eventually disagree with what checkout
charges, and the person who noticed would be right to call it a bait and
switch. Showing nothing is the honest failure.
"""

from __future__ import annotations

import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from billing.mount import BillingMount
from student.api import StudentAPI
from student.db import Database
from student.server import make_handler

playwright_api = pytest.importorskip("playwright.sync_api")
PAGE = Path("frontend/pricing.html").resolve()

# Every test in this file drives a real browser.
pytestmark = pytest.mark.browser


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pricing")
    api = StudentAPI(Database(tmp / "student.db"))
    mount = BillingMount(tmp / "billing.db")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, mount))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


CHROMIUM = next(
    (str(path) for path in (
        Path("/opt/pw-browsers/chromium-1194/chrome-linux/chrome"),
        Path("/opt/pw-browsers/chromium/chrome-linux/chrome"),
    ) if path.exists()),
    None)


@pytest.fixture(scope="module")
def browser():
    if CHROMIUM is None:
        # Discovered rather than assumed: the pinned browser directory carries
        # a build number, and hard-coding one turns a browser upgrade into a
        # test suite that skips silently.
        candidates = sorted(Path("/opt/pw-browsers").glob("chromium*/chrome-linux/chrome"))
        if not candidates:
            pytest.skip("no chromium available")
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM)
        yield b
        b.close()


def open_page(browser, query: str = ""):
    page = browser.new_page()
    page.goto(f"file://{PAGE}{query}")
    return page


def cards(page) -> list[dict]:
    return page.evaluate("""() => [...document.querySelectorAll('.plan')].map((c) => ({
        name: c.querySelector('.plan-name').textContent,
        price: c.querySelector('.price').textContent,
        per: c.querySelector('.per').textContent,
        equiv: (c.querySelector('.equiv') || {}).textContent || '',
        limits: [...c.querySelectorAll('.limits li')].map((l) => l.textContent),
        cta: c.querySelector('.cta').textContent,
    }))""")


# ------------------------------------------------------------- no backend

def test_with_no_backend_no_price_is_shown_at_all(browser) -> None:
    page = open_page(browser)
    page.wait_for_selector(".notice")
    assert cards(page) == [], "a price appeared with no backend to have produced it"
    body = page.inner_text("body")
    assert "₹" not in body, "a rupee figure is on the page with no backend configured"
    assert "no backend is configured" in body
    page.close()


def test_an_unreachable_backend_shows_the_failure_not_a_guess(browser) -> None:
    page = open_page(browser, "?api=http://127.0.0.1:1")
    page.wait_for_selector(".notice.bad", timeout=15_000)
    assert cards(page) == []
    assert "could not be loaded" in page.inner_text("body")
    assert "rather than a price that might be wrong" in page.inner_text("body")
    page.close()


# ------------------------------------------------------------- live prices

def test_the_prices_are_the_backends_prices(browser, backend) -> None:
    page = open_page(browser, f"?api={backend}")
    page.wait_for_selector(".plan", timeout=15_000)

    import json
    import urllib.request
    payload = json.load(urllib.request.urlopen(backend + "/billing/pricing"))
    expected = {f["name"]: f["intervals"].get("monthly", f["intervals"].get("none"))
                ["price_display"] for f in payload["families"]}

    shown = {c["name"]: c["price"] for c in cards(page)}
    assert shown == expected, "the page is not showing what the backend served"
    page.close()


def test_every_plan_shows_all_three_limits(browser, backend) -> None:
    """
    Monthly, daily and session. Omitting the daily cap is how somebody buys a
    5,000-question plan and is surprised at 300.
    """
    page = open_page(browser, f"?api={backend}")
    page.wait_for_selector(".plan", timeout=15_000)
    for card in cards(page):
        joined = " ".join(card["limits"])
        assert "/ month" in joined and "/ day" in joined and "/ session" in joined, card
    page.close()


def test_the_page_explains_that_monthly_and_daily_are_different(browser, backend) -> None:
    page = open_page(browser, f"?api={backend}")
    page.wait_for_selector(".plan", timeout=15_000)
    body = page.inner_text("body")
    assert "daily cap" in body.lower()
    assert "still be finished for today" in body
    page.close()


# ------------------------------------------------------------- the toggle

def test_the_toggle_changes_the_prices(browser, backend) -> None:
    page = open_page(browser, f"?api={backend}")
    page.wait_for_selector(".plan", timeout=15_000)
    monthly = {c["name"]: c["price"] for c in cards(page)}

    page.click("#btn-annual")
    page.wait_for_function(
        "() => document.querySelector('#btn-annual').getAttribute('aria-pressed') === 'true'")
    annual = {c["name"]: c["price"] for c in cards(page)}

    paid = [n for n in monthly if monthly[n] != "₹0"]
    assert paid, "no paid plan on the page"
    for name in paid:
        assert annual[name] != monthly[name], f"{name} did not change with the toggle"
    page.close()


def test_an_annual_card_shows_the_per_month_equivalent(browser, backend) -> None:
    """Otherwise the toggle compares ₹4,990 against ₹499 and annual looks dearer."""
    page = open_page(browser, f"?api={backend}&interval=annual")
    page.wait_for_selector(".plan", timeout=15_000)
    page.click("#btn-annual")
    paid = [c for c in cards(page) if c["price"] != "₹0"]
    assert paid
    for card in paid:
        assert "/mo" in card["equiv"], card
    page.close()


def test_the_toggle_is_visibly_selected(browser, backend) -> None:
    """
    A toggle bound to a boolean rendered `background:true`, which browsers drop
    -- the selected side then looked identical to the unselected one. Checked
    as a computed colour, since that is the thing that was wrong.
    """
    page = open_page(browser, f"?api={backend}")
    page.wait_for_selector(".plan", timeout=15_000)

    def bg(selector):
        return page.evaluate(
            f"() => getComputedStyle(document.querySelector('{selector}')).backgroundColor")

    monthly_selected, annual_unselected = bg("#btn-monthly"), bg("#btn-annual")
    assert monthly_selected != annual_unselected

    page.click("#btn-annual")
    # The toggle animates over 150ms. Reading the computed colour immediately
    # returns the value at t=0, which is the OLD one -- a green test would then
    # be asserting the opposite of what it means to.
    page.wait_for_function(
        "([sel, want]) => getComputedStyle(document.querySelector(sel)).backgroundColor === want",
        arg=["#btn-annual", monthly_selected], timeout=5_000)
    assert bg("#btn-monthly") == annual_unselected
    page.close()


# ------------------------------------------------------------- not a till

def test_the_page_cannot_start_a_payment(browser, backend) -> None:
    """
    A pricing page that could charge an unknown account is a fraud surface.
    Choosing a plan sends you into the app, signed in.
    """
    page = open_page(browser, f"?api={backend}")
    page.wait_for_selector(".plan", timeout=15_000)
    requests = []
    page.on("request", lambda r: requests.append(r.url))

    page.click(".plan.featured .cta")
    page.wait_for_selector(".notice")
    assert "Payment happens signed in" in page.inner_text(".notice")
    assert not any("checkout" in url or "subscription" in url for url in requests)
    page.close()


def test_the_page_carries_no_key_or_secret() -> None:
    source = PAGE.read_text(encoding="utf-8")
    for forbidden in ("rzp_live", "key_secret", "RAZORPAY_KEY_SECRET"):
        assert forbidden not in source


def test_no_price_is_hardcoded_in_the_page() -> None:
    """
    The whole argument for this page fetching its prices. A number baked in
    here would eventually disagree with what checkout charges.
    """
    import re

    source = PAGE.read_text(encoding="utf-8")
    script = re.search(r"<script>(.*?)</script>", source, re.S).group(1)
    body = re.search(r"<body>(.*?)</body>", source, re.S).group(1)
    for text in (script, body):
        assert not re.search(r"₹\s?\d", text), "a rupee figure is hardcoded in the page"


# ------------------------------------------------------------- responsive

def test_the_page_does_not_scroll_sideways_on_a_phone(browser, backend) -> None:
    page = browser.new_page(viewport={"width": 360, "height": 740})
    page.goto(f"file://{PAGE}?api={backend}")
    page.wait_for_selector(".plan", timeout=15_000)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"the page overflows by {overflow}px at 360 wide"
    page.close()
