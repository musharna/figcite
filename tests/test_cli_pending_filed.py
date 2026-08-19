"""What `figcite pending` prints for an already-filed capture.

The filed branch printed only `note` -- so it never showed candidates, and when
`note` was deduped against the identical `doi_evidence` string it stopped
showing a reason at all. Both front ends read the same PendingItem; only the web
card was rendering what was in it.
"""

from figcite import cli, service, store
from figcite.provenance import Record, now_stamps

TAB = {
    "source": "open-tab",
    "score": "",
    "doi": "10.1111/nph.71477",
    "title": "AUXIN RESPONSE FACTORs: from transcription factors to auxin effectors",
    "container": "nph.onlinelibrary.wiley.com",
    "year": "",
    "type": "",
}


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


def test_a_filed_capture_lists_its_candidates(capsys):
    _file_one(
        "4" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "candidates": [TAB],
            "doi_evidence": "'snippingtool' held focus at the snip",
        },
    )
    cli.cmd_pending(None)
    out = capsys.readouterr().out

    assert "10.1111/nph.71477" in out, (
        "a candidate the web card shows is invisible here"
    )
    assert "--pick" in out, "candidates listed with no way to choose one"


def test_a_filed_capture_still_explains_itself(capsys):
    """The reason must survive `note` being deduped away.

    `note` and `doi_evidence` carry the same sentence on an auto-filed capture,
    so the read path blanks the duplicate -- which silently removed the only
    line this printer was showing.
    """
    _file_one(
        "5" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "doi_evidence": "held focus, so the title names the capture tool",
        },
    )
    cli.cmd_pending(None)
    out = capsys.readouterr().out
    assert "held focus" in out


def test_a_filed_capture_with_no_reason_at_all_still_prints(capsys):
    """Positive control: the printer must not require a reason to render a row."""
    _file_one(
        "6" * 64,
        {"clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"}},
    )
    cli.cmd_pending(None)
    out = capsys.readouterr().out
    assert "SnippingTool" in out


def test_both_front_ends_offer_the_same_candidates():
    """Parity: the service is the single source, so neither may drop from it."""
    _file_one(
        "7" * 64,
        {
            "clipboard_capture": {"process": "SnippingTool", "title": "Snipping Tool"},
            "candidates": [TAB],
        },
    )
    it = [i for i in service.pending_items() if i.ref == f"filed:{'7' * 64}"][0]
    assert [c["doi"] for c in it.candidates] == [TAB["doi"]]
