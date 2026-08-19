"""Which browsers figcite claims to handle vs. which it can actually read.

`browser.py` strips six browsers' name-suffixes from window titles and
`clipboard.BROWSERS` lists six, but only Firefox's history is ever opened. For
the other five, grounding searched **Firefox's** database for another browser's
page title -- a category error that can only fail, and it reported the failure
as "no history entry titled X (private-browsing windows leave no history)",
which names the wrong cause.

Nothing compared the two lists, so the drift was invisible.
"""

from figcite import browser, clipboard


def test_every_declared_browser_has_a_decision_about_its_history():
    """The reconciliation that did not exist.

    This is not a list of names guarding an open set -- it is two lists that
    must agree. Adding a browser to BROWSERS without deciding whether its
    history is readable fails here, rather than silently searching Firefox's.
    """
    decided = browser.HISTORY_BACKENDS | browser.BROWSERS_WITHOUT_HISTORY_BACKEND
    assert clipboard.BROWSERS == decided, (
        "clipboard.BROWSERS and browser.py's history coverage have drifted: "
        f"undecided={clipboard.BROWSERS - decided}, "
        f"unknown-to-BROWSERS={decided - clipboard.BROWSERS}"
    )


def test_the_two_coverage_sets_do_not_overlap():
    """A browser cannot be both readable and unreadable."""
    assert not (browser.HISTORY_BACKENDS & browser.BROWSERS_WITHOUT_HISTORY_BACKEND)


def test_a_capture_from_an_unreadable_browser_says_so(monkeypatch):
    """The reason must name the real cause, not the private-browsing one."""
    monkeypatch.setattr(browser, "firefox_profiles", lambda: [])

    out = browser.resolve_from_capture(
        {"process": "msedge", "title": "A paper - Microsoft Edge"}
    )
    assert out["doi"] is None
    ev = out["evidence"].lower()
    assert "msedge" in ev or "edge" in ev
    assert "firefox" in ev, "the evidence never says which history it CAN read"


def test_a_firefox_capture_is_not_told_its_browser_is_unreadable(monkeypatch):
    """Positive control: the message must not fire for the supported browser."""
    monkeypatch.setattr(browser, "firefox_profiles", lambda: [])

    out = browser.resolve_from_capture(
        {"process": "firefox", "title": "A paper - Mozilla Firefox"}
    )
    assert "cannot read" not in out["evidence"].lower()


def test_an_unknown_process_is_not_accused_of_being_a_browser(monkeypatch):
    """A capture with no process recorded must not claim an unreadable browser."""
    monkeypatch.setattr(browser, "firefox_profiles", lambda: [])

    out = browser.resolve_from_capture({"process": "", "title": "A paper"})
    assert "cannot read" not in out["evidence"].lower()


def test_the_common_case_a_profile_exists_but_the_page_was_in_another_browser(
    monkeypatch, tmp_path
):
    """The shape this actually takes on a machine that HAS Firefox installed.

    The no-profile branch is the rare one. Normally the profile is there, the
    lookup runs against Firefox's database, and the Edge page is simply absent
    -- which was reported as private browsing.
    """
    monkeypatch.setattr(browser, "firefox_profiles", lambda: [tmp_path])
    monkeypatch.setattr(browser, "snapshot_history", lambda p: tmp_path / "places.sqlite")
    monkeypatch.setattr(browser, "lookup_by_title", lambda db, t: None)
    monkeypatch.setattr(browser, "lookup_by_time", lambda db, ts, **kw: None)

    out = browser.resolve_from_capture(
        {"process": "msedge", "title": "A paper - Microsoft Edge",
         "captured_local": "2026-08-18T23:12:12"}
    )
    ev = out["evidence"].lower()
    assert "msedge" in ev, "the miss is blamed on private browsing, not on the browser"
    assert "firefox" in ev


def test_a_firefox_miss_is_still_reported_as_a_plain_miss(monkeypatch, tmp_path):
    """Positive control: the prefix must not attach to the supported browser."""
    monkeypatch.setattr(browser, "firefox_profiles", lambda: [tmp_path])
    monkeypatch.setattr(browser, "snapshot_history", lambda p: tmp_path / "places.sqlite")
    monkeypatch.setattr(browser, "lookup_by_title", lambda db, t: None)
    monkeypatch.setattr(browser, "lookup_by_time", lambda db, ts, **kw: None)

    out = browser.resolve_from_capture(
        {"process": "firefox", "title": "A paper - Mozilla Firefox"}
    )
    assert "cannot read" not in out["evidence"].lower()
    assert "no history entry" in out["evidence"].lower()
