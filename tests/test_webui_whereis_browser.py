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
import time

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


# Transport errors reaching a server we started ourselves, on loopback, are
# facts about the machine rather than about figcite.
#
# Measured across 20 gate runs this session: 2 spurious reds, a 10% flake rate,
# in TWO distinct modes -- a page that had not rendered (fixed below by waiting
# for the root screen) and `net::ERR_NETWORK_CHANGED` raised by `goto` itself
# when a WSL adapter flapped mid-test. Both surface through the equivalence
# registry as COULD NOT BE CERTIFIED on an UNRELATED claim, which reads like a
# regression in code nobody touched.
#
# The retry is deliberately narrow. Only `net::` transport codes are retried:
# a loopback connection that failed to open says nothing about the application,
# while a TIMEOUT might be a real hang and an assertion is a real answer. Both
# of those still propagate. Retrying everything would trade a flaky suite for a
# suite that cannot report a broken server, which is the worse failure.
_TRANSPORT_RETRIES = 3


def _goto_through_transport_hiccups(pg, url: str):
    last = None
    for attempt in range(_TRANSPORT_RETRIES):
        try:
            return pg.goto(url, wait_until="load")
        except PlaywrightError as e:
            if "net::" not in str(e):
                raise
            last = e
            time.sleep(0.4 * (attempt + 1))
    raise AssertionError(
        f"loopback transport kept failing after {_TRANSPORT_RETRIES} attempts, "
        f"so this is not a passing hiccup: {last}"
    )


def _wait_for_ui_or_say_why(pg):
    """Wait for the root screen, and on timeout report WHAT was there instead.

    Mode 1 of this fixture's flake (2026-08-26) is mitigated but its cause is
    NOT established, and the three live candidates are not distinguishable from
    the symptom:

      H1 a timing race -- the page had not rendered when `load` fired
      H2 the chromium renderer died under memory pressure, leaving nothing
      H3 the server answered something with no `#pending` in it -- `_host_ok`
         returns a JSON 403, and `serve_forever` runs on a thread started
         immediately before `goto`

    H1 predicts the wait simply resolves. H2 and H3 BOTH predict a timeout, and
    they have completely different fixes: worker resource limits versus server
    startup or Host handling. So a bare timeout cannot tell them apart, and
    guessing between them from a green suite would be inventing a cause.

    What it dumps decides it. A blank body means the renderer went away; a JSON
    error body means the server answered and this is not about memory at all.
    """
    try:
        pg.wait_for_selector("#pending", state="visible", timeout=30_000)
    except PlaywrightError as e:
        try:
            url, body = pg.url, pg.content()[:400]
        except Exception as inner:  # pragma: no cover - renderer already gone
            url, body = "<unreachable>", f"<could not read the page: {inner}>"
        raise AssertionError(
            "the figcite UI never rendered. This is the evidence that tells "
            "H2 (renderer died: empty/unreachable body) from H3 (server "
            "answered: a JSON error body), which a bare timeout cannot.\n"
            f"  url  : {url}\n"
            f"  body : {body!r}\n"
            f"  wait : {e}"
        ) from e


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
            _goto_through_transport_hiccups(pg, f"http://127.0.0.1:{port}/")
            # `load` fires when the document and its subresources are done, not
            # when this page is usable, and Playwright's `is_hidden` returns
            # True for an element that IS NOT THERE. So a page that never
            # rendered satisfies every `is_hidden` assertion in this file and
            # fails first at whichever `is_visible` premise comes next --
            # reporting "the wrong tab is open" for "there is no page".
            #
            # Measured 2026-08-26: under the equivalence registry, which runs
            # this suite once per claim under four xdist workers, that race
            # turned into three red gate runs whose message named a tab.
            # Waiting for the root screen makes the fixture deliver what it
            # claims to -- a loaded UI -- and turns a genuine failure to render
            # into a timeout that says so.
            _wait_for_ui_or_say_why(pg)
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
    # NOT `#whereisresult p`: the "searching…" placeholder is a <p>, so that
    # selector is satisfied the instant the click lands and the assertions
    # below race the fetch. `.nosrc`/`.fail` appear only in a FINAL state.
    page.wait_for_selector("#whereisresult .nosrc, #whereisresult .fail", timeout=15000)

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


# --- the transport retry itself, which must not swallow a real failure -------


class _Flaky:
    """A stand-in page whose `goto` fails a set number of times first."""

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def goto(self, url, **kw):
        self.calls += 1
        if self.errors:
            raise PlaywrightError(self.errors.pop(0))
        return "loaded"


def test_a_loopback_transport_hiccup_is_retried(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    pg = _Flaky(["Page.goto: net::ERR_NETWORK_CHANGED at http://127.0.0.1:1/"])

    assert _goto_through_transport_hiccups(pg, "http://127.0.0.1:1/") == "loaded"
    assert pg.calls == 2, pg.calls


def test_a_timeout_is_NOT_retried(monkeypatch):
    """The narrowing that makes this retry safe rather than a blindfold.

    A loopback connection that would not open says nothing about figcite. A
    timeout might be a real hang in the server or the page, and retrying it
    would trade a flaky suite for one that cannot report a broken server --
    the worse failure of the two.
    """
    monkeypatch.setattr(time, "sleep", lambda s: None)
    pg = _Flaky(["Page.goto: Timeout 30000ms exceeded"])

    with pytest.raises(PlaywrightError, match="Timeout"):
        _goto_through_transport_hiccups(pg, "http://127.0.0.1:1/")
    assert pg.calls == 1, f"a timeout was retried {pg.calls} times"


def test_a_persistent_transport_failure_still_fails(monkeypatch):
    """Positive control for the retry: it must give up, and say that it is not
    a passing hiccup, rather than loop or return a broken page."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    pg = _Flaky(["net::ERR_CONNECTION_REFUSED"] * 10)

    with pytest.raises(AssertionError, match="kept failing"):
        _goto_through_transport_hiccups(pg, "http://127.0.0.1:1/")
    assert pg.calls == _TRANSPORT_RETRIES, pg.calls


class _NeverRenders:
    """A page whose selector wait times out, carrying a body to be reported."""

    def __init__(self, body, url="http://127.0.0.1:1/"):
        self._body, self.url = body, url

    def wait_for_selector(self, *a, **kw):
        raise PlaywrightError("Timeout 30000ms exceeded waiting for selector")

    def content(self):
        return self._body


def test_a_page_that_never_renders_reports_what_was_there_instead():
    """H2 and H3 both look like a timeout; only the BODY tells them apart.

    Without this the next occurrence would say "timed out waiting for
    #pending" and leave the same three hypotheses live -- which is how a
    mitigated flake turns into a guessed cause.
    """
    server_answered = _NeverRenders('{"error": "unrecognized Host"}')
    with pytest.raises(AssertionError) as e:
        _wait_for_ui_or_say_why(server_answered)
    assert "unrecognized Host" in str(e.value), str(e.value)
    assert "H3" in str(e.value)


def test_a_renderer_that_went_away_is_reported_differently():
    """The other side. An empty body is the H2 signature, and a page object
    that cannot even be read must not turn into a second, confusing error."""

    class _Gone(_NeverRenders):
        def content(self):
            raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(AssertionError) as e:
        _wait_for_ui_or_say_why(_Gone(""))
    assert "could not read the page" in str(e.value), str(e.value)
    assert "H2" in str(e.value)
