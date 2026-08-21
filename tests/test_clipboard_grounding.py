"""An ungrounded Zotero hit must stay ungrounded.

`_zotero_try` folds a Zotero lookup into the capture result:

    if z.get("doi") and z.get("grounded"):
        out.update(doi=z["doi"], grounded=True)

`and -> or` survived the whole suite. Under `or`, a Zotero item that matched
loosely -- a DOI with `grounded: False`, which is exactly what the resolver
returns when it is guessing -- gets stamped `grounded=True` on the way out.
`grounded` is what lets `service.confirm()` accept a DOI with no human
selection, so that mutant converts "Zotero has something vaguely like this"
into a citation.

The docstring right above it says an unreachable library "must degrade to 'no
answer from Zotero', never to a wrong answer". Nothing checked that it does.
"""

from figcite import clipboard, zotero


def _z(monkeypatch, payload):
    monkeypatch.setattr(zotero, "resolve", lambda q: payload)
    monkeypatch.setattr(zotero, "resolve_page_title", lambda q: payload)


def test_a_grounded_zotero_hit_is_taken(monkeypatch):
    """Positive control. Every assertion below says some case is REFUSED;
    without this, a function that folded in nothing at all would pass them
    all while the Zotero route was dead."""
    _z(monkeypatch, {"doi": "10.1/real", "grounded": True, "evidence": "exact title"})
    out = {}

    ev = clipboard._zotero_try("Some Paper Title", out)

    assert out.get("doi") == "10.1/real", out
    assert out.get("grounded") is True, out
    assert "exact title" in ev


def test_an_ungrounded_zotero_hit_is_not_folded_in(monkeypatch):
    """THE finding.

    A DOI with grounded=False is Zotero's way of saying "this is a guess".
    Taking it here, and relabelling it grounded=True, is the tool asserting a
    source it cannot support.
    """
    _z(monkeypatch, {"doi": "10.1/guess", "grounded": False, "evidence": "fuzzy match"})
    out = {}

    clipboard._zotero_try("Some Paper Title", out)

    assert out.get("grounded") is not True, (
        f"an ungrounded Zotero guess was marked grounded: {out}"
    )
    assert out.get("doi") != "10.1/guess", (
        f"an ungrounded Zotero DOI was adopted as the capture's DOI: {out}"
    )


def test_a_grounded_result_with_no_doi_is_not_folded_in(monkeypatch):
    """The mirror of the above: `grounded` alone is not a source either, and
    it is the other half the `and` is holding together."""
    _z(monkeypatch, {"doi": None, "grounded": True, "evidence": "no doi on the item"})
    out = {}

    clipboard._zotero_try("Some Paper Title", out)

    assert out.get("doi") is None, out
    assert out.get("grounded") is not True, out


def test_candidates_come_through_even_when_the_doi_is_refused(monkeypatch):
    """Refusing to GROUND a guess must not throw the guess away -- it is still
    a lead worth offering the user. Otherwise the fix for the finding above
    would be 'ignore ungrounded results', which loses information."""
    _z(
        monkeypatch,
        {
            "doi": "10.1/guess",
            "grounded": False,
            "evidence": "fuzzy",
            "candidates": [{"doi": "10.1/guess", "title": "Maybe This One"}],
        },
    )
    out = {}

    clipboard._zotero_try("Some Paper Title", out)

    assert out.get("grounded") is not True, out
    assert out.get("candidates"), "the lead was discarded along with the grounding"


def test_a_broken_zotero_is_an_error_not_an_absence(monkeypatch):
    """The distinction the docstring promises: "no answer from Zotero" and
    "Zotero said no" are different, and only one of them is about the paper."""

    def boom(q):
        raise RuntimeError("library unreachable")

    monkeypatch.setattr(zotero, "resolve", boom)
    monkeypatch.setattr(zotero, "resolve_page_title", boom)
    out = {}

    ev = clipboard._zotero_try("Some Paper Title", out)

    assert "error" in out, out
    assert "unreachable" in out["error"], out
    assert "unreachable" in ev, ev
    assert out.get("grounded") is not True, out


# --- the dead predicate ---------------------------------------------------


def test_a_pdf_title_from_an_unlisted_app_still_searches_the_disk(monkeypatch):
    """`infer_source` contains, since 0.1.0:

        if pdf_name and (proc in PDF_APPS or proc in BROWSERS or True):

    The trailing `or True` makes the two membership tests DEAD -- the whole
    condition reduces to `if pdf_name:`. The mutation sweep found it the way
    dead code always shows up: BOTH `in -> not in` mutants survived, because
    neither can change an expression that is `or True`.

    This pins the behaviour that is actually shipping -- any process at all,
    provided the window title names a PDF -- so the predicate can be
    simplified to what it really does without anyone having to guess whether
    the widening was intended.
    """
    seen = {}

    monkeypatch.setattr(clipboard, "pdf_name_from_title", lambda t: "paper.pdf")

    def _find(name):
        seen["name"] = name
        return None  # not on disk; the point is that we LOOKED

    monkeypatch.setattr(clipboard, "find_pdf_on_disk", _find)

    # BROWSERS is the list that still gates a real branch (the browser route
    # above this one), and notepad must not be in it or this test would be
    # exercising that path instead. `PDF_APPS` is gone entirely -- once the
    # dead predicate was written out it had no consumer, and this test is what
    # makes reinstating it a visible change rather than a quiet one.
    assert "notepad" not in clipboard.BROWSERS
    assert not hasattr(clipboard, "PDF_APPS"), (
        "PDF_APPS is back. It can only be gating this branch again, which "
        "narrows the shipped behaviour this test pins: a PDF viewer is an open "
        "set, and the title is the signal, not the process name."
    )

    clipboard.infer_source({"process": "notepad.exe", "title": "paper.pdf"})

    assert seen.get("name") == "paper.pdf", (
        "the on-disk PDF search was skipped for a process in neither list, so "
        "the `or True` no longer describes the shipped behaviour"
    )


def test_a_title_that_names_no_pdf_does_not_search_the_disk(monkeypatch):
    """Positive control for the above: `pdf_name` is the conjunct that IS
    doing work, and a test that only asserted 'we always look' would pass on
    an implementation that looked unconditionally."""
    called = []
    monkeypatch.setattr(clipboard, "pdf_name_from_title", lambda t: None)
    monkeypatch.setattr(clipboard, "find_pdf_on_disk", lambda name: called.append(name))

    clipboard.infer_source({"process": "notepad.exe", "title": "just some window"})

    assert not called, f"searched the disk with no PDF name in the title: {called}"
