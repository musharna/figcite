"""A filed capture must be able to show what it found.

`pending_items` built filed items from ref/kind/context/error/note alone, so a
capture the watcher auto-filed could never display a candidate, a DOI, an
evidence string, or its own pixel dimensions -- even where the record on disk
already held them. Any inference improvement upstream is inert until this is
fixed, which is how a real capture came out with two open-tab candidates
computed and none rendered.
"""

import pytest

from figcite import service, store
from figcite.provenance import Record, now_stamps

TAB = {
    "source": "open-tab",
    "score": "",
    "doi": "10.1111/nph.71477",
    "title": "AUXIN RESPONSE FACTORs",
    "container": "nph.onlinelibrary.wiley.com",
    "year": "",
    "type": "",
}
OTHER = dict(TAB, doi="10.3389/fpls.2017.00491", title="Metabolic Investigation")


def _file_one(sha, detail, note=""):
    u, loc = now_stamps()
    store.put(
        Record(
            sha256=sha,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            captured_utc=u,
            captured_local=loc,
            source_detail=detail,
            note=note,
        )
    )
    return f"filed:{sha}"


def _item(ref):
    return [i for i in service.pending_items() if i.ref == ref][0]


def test_a_filed_capture_surfaces_its_stored_candidates():
    ref = _file_one(
        "b" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "candidates": [TAB, OTHER],
            "doi_evidence": "'snippingtool' held focus at the snip",
        },
    )
    it = _item(ref)
    assert [c["doi"] for c in it.candidates] == [TAB["doi"], OTHER["doi"]]
    assert "focus" in it.doi_evidence


def test_a_filed_capture_with_nothing_found_still_reports_no_candidates():
    """Positive control: the test above must not pass by surfacing junk."""
    ref = _file_one(
        "c" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
        },
    )
    it = _item(ref)
    assert it.candidates == []
    assert it.error is None  # looked, found nothing -- not "could not look"


def test_a_filed_capture_surfaces_its_pixel_dimensions():
    """The card lays the thumbnail out from these; they were already on disk."""
    ref = _file_one(
        "d" * 64,
        {
            "clipboard_capture": {
                "process": "SnippingTool",
                "title": "Snipping Tool",
                "width": 1280,
                "height": 885,
            },
        },
    )
    it = _item(ref)
    assert (it.width, it.height) == (1280, 885)


def test_a_filed_capture_never_reports_itself_grounded():
    """Nothing filed-and-unconfirmed is grounded, whatever it stored.

    A stored `grounded` flag must not be able to turn into an auto-confirm on
    the read path: these items are in the pending list precisely because no
    human has accepted them.
    """
    ref = _file_one(
        "e" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "candidates": [TAB],
            "grounded": True,
            "doi": TAB["doi"],
        },
    )
    it = _item(ref)
    assert it.grounded is False
    assert it.doi is None


# --------------------------------------------------------------- confirming


def test_picking_a_candidate_on_a_filed_capture_uses_that_candidates_doi(monkeypatch):
    """Without this, the radio buttons render and Confirm cannot act on them."""
    captured = {}

    def fake_record_for(doi, cite, _x, **kw):
        captured["doi"] = doi
        return Record(
            sha256="f" * 64,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=True,
            doi=doi,
            captured_utc="",
            captured_local="",
        )

    monkeypatch.setattr(service, "record_for", fake_record_for)
    ref = _file_one(
        "f" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "candidates": [TAB, OTHER],
        },
    )
    service.confirm(ref, pick=1)
    assert captured["doi"] == OTHER["doi"]


def test_picking_a_candidate_that_is_not_there_is_refused(monkeypatch):
    ref = _file_one(
        "0" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "candidates": [TAB],
        },
    )
    with pytest.raises(KeyError):
        service.confirm(ref, pick=7)


def test_a_filed_capture_with_no_selector_is_still_refused():
    """Ruling 4's guard must survive on this path too.

    Zero selectors on a filed capture cannot silently resolve to a candidate;
    displaying a radio option is not choosing it.
    """
    ref = _file_one(
        "1" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "candidates": [TAB],
        },
    )
    with pytest.raises(ValueError):
        service.confirm(ref)


def test_the_same_reason_is_not_printed_twice():  # IRON_LAW_OK
    """`note` and `doi_evidence` hold the same string on an auto-filed capture.

    Surfacing the evidence made the card print its own failure reason twice,
    verbatim, one line apart -- invisible to every assertion in this file until
    someone looked at the rendered page.
    """
    reason = "no rule for process 'snippingtool' with title 'Snipping Tool'"
    ref = _file_one(
        "2" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "doi_evidence": reason,
        },
        note=reason,
    )
    it = _item(ref)
    assert it.doi_evidence == reason
    assert it.note != reason, "the same sentence is rendered twice on the card"


def test_a_note_that_says_something_else_is_kept():  # IRON_LAW_OK
    """Positive control: the dedupe must not degrade into 'drop the note'."""
    ref = _file_one(
        "3" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "doi_evidence": "held focus, so the title is the tool's own",
        },
        note="adapted from figure 2",
    )
    it = _item(ref)
    assert it.note == "adapted from figure 2"
