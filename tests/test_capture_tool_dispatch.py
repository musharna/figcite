"""Snips taken through a screenshot utility.

Provenance was dispatched on which app had FOCUS when the clipboard changed.
Under Win+Shift+S the app with focus is always the snipping tool, so the whole
grounding chain was skipped and the capture filed with context but no source --
found by running the tool, not by any of the 258 tests that preceded it.

The focus signal is not merely unhelpful here, it is meaningless: a snipping
tool is never the source of anything. What the user was looking at is in the
browser's open tabs.
"""

from figcite import clipboard, session_tabs

TAB = {
    "source": "open-tab",
    "score": "",
    "doi": "10.1111/nph.71477",
    "title": "AUXIN RESPONSE FACTORs",
    "container": "nph.onlinelibrary.wiley.com",
    "year": "",
    "type": "",
    "url": "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477",
}

SNIP = {"process": "SnippingTool", "title": "Snipping Tool"}


def test_a_snipping_tool_capture_offers_the_open_tabs(monkeypatch):
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [TAB])

    out = clipboard.infer_source(dict(SNIP))

    assert [c["doi"] for c in out["candidates"]] == ["10.1111/nph.71477"]
    assert out["error"] is None


def test_one_candidate_is_still_never_auto_confirmed(monkeypatch):
    """The dangerous shape: exactly one candidate looks like certainty.

    It is not. A snip may be of a PDF reader, a slide, or the desktop, and the
    tab list cannot tell which window was on screen. Filing a DOI here without
    a human click is precisely the confidently-wrong outcome this project
    exists to refuse.
    """
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [TAB])

    out = clipboard.infer_source(dict(SNIP))

    assert out["doi"] is None
    assert out["grounded"] is False


def test_a_snipping_tool_capture_is_not_routed_through_the_browser_path(monkeypatch):
    """Guards against the band-aid: adding 'snippingtool' to BROWSERS.

    That one-line change makes the symptom disappear and routes the capture
    into browser_resolve, whose exact-title hit sets grounded=True and
    auto-files. A history page titled 'Snipping Tool' would then be filed as
    the source of the figure.
    """

    def must_not_run(capture):
        raise AssertionError("browser_resolve ran for a snipping-tool capture")

    monkeypatch.setattr(clipboard, "browser_resolve", must_not_run)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [TAB])

    out = clipboard.infer_source(dict(SNIP))
    assert out["grounded"] is False


def test_browser_captures_still_use_the_browser_path(monkeypatch):
    """Positive control for the test above.

    If the capture-tool branch swallowed every capture, `must_not_run` would
    never fire and that test would pass while grounding was dead everywhere.
    """
    ran = []

    def browser(capture):
        ran.append(capture)
        return {
            "doi": "10.1/real",
            "url": "https://example.org/10.1/real",
            "grounded": True,
            "evidence": "DOI in URL",
        }

    monkeypatch.setattr(clipboard, "browser_resolve", browser)

    out = clipboard.infer_source({"process": "firefox", "title": "A paper"})
    assert ran, "browser_resolve did not run for a firefox capture"
    assert out["doi"] == "10.1/real"
    assert out["grounded"] is True


def test_no_resolvable_tab_reads_as_looked_and_found_nothing(monkeypatch):
    """Empty candidates with error=None is 'looked, found nothing'.

    error is not None is 'could not look'. The UI renders these differently and
    they must never collapse into one another.
    """
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = clipboard.infer_source(dict(SNIP))

    assert out["candidates"] == []
    assert out["error"] is None
    assert out["doi_evidence"], "an empty result still owes the user a reason"


def test_the_evidence_names_the_focus_problem_not_just_the_failure(monkeypatch):
    """The user needs to know WHY nothing resolved, or the card is a dead end.

    'no rule for process snippingtool' told them nothing actionable. Naming the
    mechanism -- the snipping tool held focus, so the title is the tool's own --
    is what makes the candidate list make sense.
    """
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [TAB])

    ev = clipboard.infer_source(dict(SNIP))["doi_evidence"].lower()
    assert "focus" in ev or "snipping" in ev or "capture tool" in ev
    assert "open tab" in ev or "tab" in ev


def test_a_tab_scan_failure_is_reported_not_swallowed(monkeypatch):
    """Fail loud. A session store we cannot read is 'could not look'."""

    def boom(path=None):
        raise RuntimeError("session store is being rewritten")

    monkeypatch.setattr(session_tabs, "tab_candidates", boom)

    out = clipboard.infer_source(dict(SNIP))
    assert out["error"] is not None
    assert "session store" in out["error"]
