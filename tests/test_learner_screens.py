"""
The learner app's screens, rendered in a real browser.

Written because the previous round of these screens passed every static check
and was broken on the device: a "Choose your plan" screen with a working
toggle and nothing under it, and a "Choose file" button that opened a text box
you were meant to type a filename into. Neither is visible from parsing the
source -- both need the page actually rendered.

The bundle is the SHIPPED artifact, `frontend/dist/pg-revision.html`, which is
the same byte stream the APK loads out of its assets. Testing the design file
instead would test something the user never runs.
"""

from __future__ import annotations

import json
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
pytestmark = pytest.mark.browser

BUNDLE = Path("frontend/dist/pg-revision.html").resolve()

CHROMIUM = next(
    (str(p) for p in sorted(Path("/opt/pw-browsers").glob("chromium*/chrome-linux/chrome"))),
    None)


@pytest.fixture(scope="module")
def browser():
    if CHROMIUM is None:
        pytest.skip("no chromium available")
    with playwright_api.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROMIUM)
        yield b
        b.close()


@pytest.fixture(scope="module")
def backend(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("screens")
    api = StudentAPI(Database(tmp / "student.db"))
    mount = BillingMount(tmp / "billing.db")
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, mount))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    yield f"http://{host}:{port}"
    server.shutdown()
    server.server_close()


def open_app(browser, backend=None):
    """Load the shipped bundle, optionally pointed at a live backend."""
    if not BUNDLE.exists():
        pytest.skip("no build present; run tools_build_standalone.py")
    page = browser.new_page(viewport={"width": 390, "height": 780})
    if backend:
        # Exactly what WebScreenActivity.kt injects ahead of the document.
        page.add_init_script(
            f"window.__QUINTEK_API__ = {json.dumps(backend)};"
            f"window.__QUINTEK_STUDENT_API__ = {json.dumps(backend)};")
    page.goto(f"file://{BUNDLE}")
    page.wait_for_selector("button", timeout=20_000)
    return page


def tap(page, text, timeout=8_000):
    page.get_by_text(text, exact=False).first.click(timeout=timeout)


def goto_plans(page):
    tap(page, "More")
    tap(page, "Billing")
    tap(page, "Change plan")
    page.wait_for_timeout(400)


# --------------------------------------------------------------- the plans screen

def test_the_plans_screen_is_never_blank_without_a_backend(browser) -> None:
    """
    The reported bug, exactly: a "Choose your plan" heading, a working
    Monthly/Annual toggle, and nothing whatsoever underneath -- no prices, no
    error, no explanation.
    """
    page = open_app(browser)
    goto_plans(page)

    body = page.inner_text("body")
    assert "Monthly" in body and "Annual" in body, "not on the plans screen"
    assert "No prices to show" in body, (
        "the plans screen rendered neither prices nor an explanation")
    # And it must not invent one to fill the space.
    assert "₹" not in body, "a price appeared with no backend to have produced it"
    page.close()


def test_the_plans_screen_shows_real_prices_from_the_backend(browser, backend) -> None:
    page = open_app(browser, backend)
    goto_plans(page)
    page.wait_for_timeout(1_200)

    body = page.inner_text("body")
    payload = json.loads(
        __import__("urllib.request", fromlist=["x"]).urlopen(
            backend + "/billing/pricing").read())
    paid = [f for f in payload["families"] if f["family"] != "free"]
    assert paid

    for family in paid:
        price = family["intervals"]["monthly"]["price_display"]
        assert price in body, f"{family['name']} at {price} is not on the screen"
    page.close()


def test_prices_survive_a_signed_out_session(browser, backend) -> None:
    """
    The cause of the blank screen. `/pricing` is public; `/me/usage` needs a
    session. Loading both through one Promise.all meant a signed-out learner's
    401 threw away the public prices that had loaded perfectly well.
    """
    page = open_app(browser, backend)
    unauthorised = []
    page.on("response", lambda r: unauthorised.append(r.url) if r.status == 401 else None)
    # The app loads billing on mount, so the 401 has already been and gone by
    # the time a listener attached after `goto` could see it. Reload with the
    # listener in place.
    page.reload()
    page.wait_for_selector("button", timeout=20_000)

    goto_plans(page)
    page.wait_for_timeout(1_200)

    assert any("/me/" in url for url in unauthorised), (
        "expected the private calls to 401 -- this test proves nothing otherwise")
    assert "₹" in page.inner_text("body"), (
        "a 401 on the private calls wiped out the public prices")
    page.close()


# --------------------------------------------------------------- the source picker

def open_source_picker(page, kind):
    """
    Reach the source picker the way a learner does: an empty notebook.

    'Renal Physiology' ships with zero sources, so it renders the
    empty-notebook state whose shortcuts open the real picker. Going through
    the default notebook lands on one that already HAS sources, and the
    picker never appears.
    """
    tap(page, "More")
    tap(page, "Notebooks")
    page.wait_for_timeout(300)
    tap(page, "Renal Physiology")
    page.wait_for_timeout(300)
    page.get_by_text(kind, exact=False).first.click(timeout=8_000)
    page.wait_for_timeout(500)


def test_choosing_a_pdf_opens_a_file_picker_not_a_text_box(browser) -> None:
    """
    The reported bug: tapping "Choose file" revealed a single-line text input.
    A typed filename is not a document -- nothing uploads and nothing is read.
    """
    page = open_app(browser)
    open_source_picker(page, "PDF or document")

    file_inputs = page.locator("input[type=file]")
    assert file_inputs.count() >= 1, (
        "no file input on screen after choosing 'PDF or document'")
    accept = file_inputs.first.get_attribute("accept") or ""
    assert "pdf" in accept.lower(), f"the file input does not accept PDFs: {accept!r}"
    page.close()


def test_the_photo_kind_asks_for_the_camera(browser) -> None:
    page = open_app(browser)
    open_source_picker(page, "Photo or scan")

    inp = page.locator("input[type=file]").first
    assert "image" in (inp.get_attribute("accept") or "")
    assert inp.get_attribute("capture") == "environment", (
        "the photo kind must ask Android for the camera, not the file browser")
    page.close()


def test_typed_notes_get_a_textarea_not_one_line(browser) -> None:
    page = open_app(browser)
    open_source_picker(page, "Typed or pasted text")

    assert page.locator("textarea").count() >= 1, (
        "pasting a page of notes into a single-line input is not usable")
    assert page.locator("input[type=file]").count() == 0
    page.close()


def test_a_link_gets_a_url_field(browser) -> None:
    page = open_app(browser)
    open_source_picker(page, "Website link")

    assert page.locator("input[type=url]").count() >= 1, (
        "a URL field gives the right keyboard and stops autocapitalisation")
    page.close()


# --------------------------------------------------------------- demo questions

def test_step_two_offers_reference_questions_and_says_it_is_optional(browser) -> None:
    page = open_app(browser)
    open_source_picker(page, "Typed or pasted text")
    page.fill("textarea", "Ferritin below 30 with a normal CRP indicates iron deficiency.")
    tap(page, "Continue")
    page.wait_for_timeout(500)

    body = page.inner_text("body")
    assert "Question style" in body, "step 2 has no reference-question section"
    assert "OPTIONAL" in body, "the section must be visibly optional"
    assert "Only the shape is copied" in body, (
        "a learner uploading a past paper expects its questions back; that has "
        "to be corrected before they upload, not after")
    page.close()


def test_reference_questions_accept_a_photo_or_typed_text(browser) -> None:
    page = open_app(browser)
    open_source_picker(page, "Typed or pasted text")
    page.fill("textarea", "A source sentence long enough to be usable as a passage.")
    tap(page, "Continue")
    page.wait_for_timeout(500)

    upload = page.locator("input[type=file]")
    assert upload.count() >= 1, "no file input for reference questions"
    accept = upload.first.get_attribute("accept") or ""
    assert "image" in accept, f"a screenshot must be acceptable: {accept!r}"

    tap(page, "Type it out")
    page.wait_for_timeout(300)
    assert page.locator("textarea").count() >= 1
    page.close()


def test_the_optional_section_never_blocks_continuing(browser) -> None:
    """OPTIONAL is a promise. A disabled Continue would break it."""
    page = open_app(browser)
    open_source_picker(page, "Typed or pasted text")
    page.fill("textarea", "A source sentence long enough to be usable as a passage.")
    tap(page, "Continue")
    page.wait_for_timeout(500)

    cta = page.get_by_text("See the graph", exact=False).first
    assert cta.is_enabled(), "the optional section blocked the step"
    page.close()


# --------------------------------------------------------------- capabilities

def test_the_picker_marks_kinds_this_build_cannot_read(browser, backend) -> None:
    """
    Three of the five source kinds raise ExtractionUnavailable the moment they
    are tried. Offering five doors and opening two is the same defect as a
    "Choose file" button that shows a text box: the learner finds out after
    committing, not before choosing.
    """
    page = open_app(browser, backend)
    tap(page, "More")
    tap(page, "Notebooks")
    page.wait_for_timeout(300)
    tap(page, "Renal Physiology")
    page.wait_for_timeout(800)

    body = page.inner_text("body")
    assert "Unavailable" in body, "no source kind was marked unavailable"
    page.close()


def test_an_unavailable_kind_says_why_and_what_to_do_instead(browser, backend) -> None:
    page = open_app(browser, backend)
    tap(page, "More")
    tap(page, "Notebooks")
    page.wait_for_timeout(300)
    tap(page, "Renal Physiology")
    page.wait_for_timeout(300)
    tap(page, "Typed or pasted text")
    page.wait_for_timeout(800)

    body = page.inner_text("body")
    assert "OCR" in body, "the photo kind does not say it needs OCR"
    assert "Type the notes out instead" in body, (
        "an unavailable kind must name the way round it")
    page.close()


def test_the_working_kinds_are_still_offered(browser, backend) -> None:
    """A capability check that hid everything would be worse than the bug."""
    page = open_app(browser, backend)
    tap(page, "More")
    tap(page, "Notebooks")
    page.wait_for_timeout(300)
    tap(page, "Renal Physiology")
    page.wait_for_timeout(800)

    tap(page, "Typed or pasted text")
    page.wait_for_timeout(400)
    assert page.locator("textarea").count() >= 1, (
        "text ingestion works on this backend and must stay selectable")
    page.close()


def test_the_capability_report_matches_what_ingestion_actually_does() -> None:
    """
    Both are derived from the same conditions. If they ever disagree, the
    picker is lying in one direction or the other.
    """
    from student.ingestion import ExtractionUnavailable, extract_for_kind, source_capabilities

    caps = source_capabilities()
    for kind, entry in caps.items():
        if entry["available"]:
            continue
        with pytest.raises(ExtractionUnavailable):
            extract_for_kind(kind, raw_text="", path=None)
