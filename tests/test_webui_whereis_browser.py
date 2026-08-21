"""The Where-is screen, driven by a real browser.

Everything in `test_webui_whereis_screen.py` runs `whereisHtml` under node.
That proves the renderer is right and proves nothing at all about whether the
page ever CALLS it: a screen whose tab never reveals its section, or whose
button was never wired to a listener, passes every one of those tests. This
project has been bitten by exactly that gap before -- a real browser found an
open panel eating every toolbar click after hundreds of green unit tests --
so the DOM wiring gets driven for real.

Skipped when playwright or its browser is not installed, matching how the
node-based tests skip when node is absent.
"""

import threading

import pytest
from PIL import Image

from figcite import corpus, match, service, session_tabs, web

pytest.importorskip("playwright", reason="playwright is not installed")

from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), "blue").save(path)
    return path


@pytest.fixture
def page(tmp_path):
    """A chromium page pointed at a real figcite server in this process.

    In-process on purpose: the server thread shares module globals with the
    test, so monkeypatch still reaches the matcher and this needs no corpus
    on disk.
    """
    srv = web.make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except PlaywrightError as e:  # pragma: no cover - env-dependent
                pytest.skip(f"chromium is not installed: {e}")
            pg = browser.new_page()
            pg.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            yield pg
            browser.close()
    finally:
        srv.shutdown()


@pytest.fixture(autouse=True)
def _quiet_pending(monkeypatch):
    """The page loads /api/pending on open; keep that off the real staging
    dir so this test does not depend on what the user happens to have
    snipped."""
    monkeypatch.setattr(service, "pending_items", lambda: [])


def _a_match(monkeypatch, doi="10.1/pixel"):
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match(doi, "PMC9", "Figure 1", "dhash", 0.0, 9.0),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])


def test_the_tab_reveals_the_screen(page):
    """A section that stays hidden is a feature nobody can reach."""
    assert page.is_hidden("#whereis"), "the screen starts hidden, behind its tab"
    assert page.is_visible("#pending"), "premise: pending is the open tab"

    page.click("#tab-whereis")

    assert page.is_visible("#whereis"), "clicking the tab did not reveal the screen"
    assert page.is_hidden("#pending"), "the previous tab was left on screen"
    assert page.get_attribute("#tab-whereis", "aria-selected") == "true"


def test_the_button_actually_searches(page, tmp_path, monkeypatch):
    """THE wiring test: renderer correct, listener never attached.

    `whereisHtml` is pure and separately tested, so this asserts only that a
    click reaches it and its output lands in the DOM -- the one thing a node
    test structurally cannot see.
    """
    _a_match(monkeypatch)
    page.click("#tab-whereis")
    page.fill("#whereisref", str(_png(tmp_path / "q.png")))

    assert page.inner_html("#whereisresult") == "", "premise: the box starts empty"

    page.click("#whereis-btn")
    page.wait_for_selector("#whereisresult p", timeout=15000)

    body = page.inner_text("#whereisresult")
    assert "10.1/pixel" in body, body
    assert "found in your corpus" in body, body


def test_a_failed_search_says_so_rather_than_going_blank(page, tmp_path, monkeypatch):
    """The positive control's negative twin.

    A thrown fetch that leaves the result box empty is indistinguishable from
    "nothing found" -- the failure mode this project keeps re-finding. The
    server 400s on a ref naming nothing, so drive that.
    """
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])
    # post() alerts before re-throwing; auto-dismiss so the click can finish.
    page.on("dialog", lambda d: d.dismiss())

    page.click("#tab-whereis")
    page.fill("#whereisref", str(tmp_path / "not-there.png"))
    page.click("#whereis-btn")
    page.wait_for_selector("#whereisresult .fail", timeout=15000)

    body = page.inner_text("#whereisresult")
    assert "COULD NOT SEARCH" in body, body


def test_the_other_screens_still_work(page, tmp_path, monkeypatch):
    """A positive control for the whole file: if the new tab broke `show()`
    every assertion above would still pass while the app was unusable."""
    _a_match(monkeypatch)
    page.click("#tab-whereis")
    assert page.is_visible("#whereis")

    page.click("#tab-deck")
    assert page.is_visible("#deck"), "the deck screen no longer opens"
    assert page.is_hidden("#whereis"), "the where-is screen stayed on top of it"

    page.click("#tab-pending")
    assert page.is_visible("#pending"), "the pending screen no longer opens"
    assert page.is_hidden("#deck")
