"""A capture with no truthful provenance had no way out of the queue.

Every verb in this CLI attaches provenance -- tag, grab, confirm, register.
That is the right bias for a tool whose whole job is "a guessed citation never
reaches a slide", but it left one state unrepresentable: a capture that is not
a figure at all. A stray Ctrl+C over a blank region files a featureless image,
`whereis` correctly declines to identify it, and then it sits in `pending`
forever, because the only exit is a citation it can never truthfully carry.

Measured on the real queue 2026-09-10: 17 of 26 remaining captures were
featureless or too smooth to identify. The queue could only grow.

`skip()` already existed and is NOT this. It is process-local, explicitly
"never persisted", and reorders the queue for the web UI -- "deal with this
later", not "this is resolved". Dismissal is the missing PERSISTENT terminal
state, and it sits beside `confirmed` rather than replacing it:

    unresolved   -> still pending
    confirmed    -> carries provenance
    dismissed    -> resolved as NOT ATTRIBUTABLE, with a stated reason

The reason is required rather than optional, because a dismissal with no
reason is indistinguishable from a mistake six months later.
"""

import pytest

from figcite import service, store
from figcite.provenance import Record


def _filed(monkeypatch, **kw):
    """One unconfirmed clipboard capture, as `pending_items` selects them."""
    rec = Record(
        sha256="a" * 64,
        dhash="f0" * 8,
        source_kind="clipboard",
        confirmed=False,
        captured_utc="2026-09-10T00:00:00Z",
        source_detail={"clipboard_capture": {"width": 10, "height": 10}},
        **kw,
    )
    written = {}
    monkeypatch.setattr(store, "all_records", lambda: dict(written) or {rec.sha256: rec})
    monkeypatch.setattr(
        store, "get", lambda sha: written.get(sha, rec if sha == rec.sha256 else None)
    )
    monkeypatch.setattr(store, "put", lambda r: written.__setitem__(r.sha256, r))
    return rec, written


def test_the_record_can_carry_a_dismissal(monkeypatch):
    """The state has to be representable before anything can set it."""
    rec = Record(sha256="a" * 64)
    assert hasattr(rec, "dismissed"), "Record cannot express a dismissal"
    assert rec.dismissed == "", "a fresh record must not be dismissed"
    # It has to survive the round trip, or it does not persist.
    back = Record.from_dict({**rec.__dict__, "dismissed": "not a figure"})
    assert back.dismissed == "not a figure"


def test_a_dismissed_capture_leaves_the_pending_queue(monkeypatch):
    rec, written = _filed(monkeypatch)

    before = [i for i in service.pending_items() if i.kind == "filed"]
    assert len(before) == 1, f"setup is wrong, expected 1 pending, got {before}"

    service.dismiss("filed:" + rec.sha256, reason="featureless; nothing to attribute")

    after = [i for i in service.pending_items() if i.kind == "filed"]
    assert after == [], f"dismissed capture is still pending: {after}"
    assert written[rec.sha256].dismissed == "featureless; nothing to attribute"


def test_a_dismissal_is_not_a_citation(monkeypatch):
    """Dismissing must never manufacture provenance -- that is the whole point.

    The failure this guards against is 'resolve' being implemented as a
    confirm with an empty citation, which would make the capture LOOK sourced.
    """
    rec, written = _filed(monkeypatch)
    service.dismiss("filed:" + rec.sha256, reason="blank")
    out = written[rec.sha256]
    assert out.confirmed is False, "a dismissal must not mark the record confirmed"
    assert not out.doi, "a dismissal must not attach a DOI"
    assert not out.citation, "a dismissal must not attach a citation"


def test_a_dismissal_requires_a_reason(monkeypatch):
    rec, _ = _filed(monkeypatch)
    with pytest.raises(ValueError):
        service.dismiss("filed:" + rec.sha256, reason="")
    with pytest.raises(ValueError):
        service.dismiss("filed:" + rec.sha256, reason="   ")


def test_a_dismissal_can_be_undone(monkeypatch):
    """Recoverable, or a fat-fingered index is permanent data loss."""
    rec, written = _filed(monkeypatch)
    service.dismiss("filed:" + rec.sha256, reason="blank")
    assert [i for i in service.pending_items() if i.kind == "filed"] == []

    service.undismiss("filed:" + rec.sha256)
    back = [i for i in service.pending_items() if i.kind == "filed"]
    assert len(back) == 1, "an undismissed capture must return to the queue"
    assert written[rec.sha256].dismissed == ""


def test_dismissing_an_unknown_ref_is_an_error(monkeypatch):
    _filed(monkeypatch)
    with pytest.raises(KeyError):
        service.dismiss("filed:" + "b" * 64, reason="blank")


def test_a_confirmed_capture_is_not_silently_dismissable(monkeypatch):
    """Dismissal addresses UNRESOLVED captures. A confirmed one carries a
    citation someone accepted; quietly dropping that is data loss, not tidying.
    """
    rec, written = _filed(monkeypatch)
    rec.confirmed = True
    rec.citation = "Harris et al. 2020"
    with pytest.raises(ValueError):
        service.dismiss("filed:" + rec.sha256, reason="oops")


def test_dismissed_items_are_listed_not_vanished(monkeypatch):
    """A dismissal that leaves no trace cannot be reviewed or undone."""
    rec, _ = _filed(monkeypatch)
    service.dismiss("filed:" + rec.sha256, reason="featureless")
    listed = service.dismissed_items()
    assert len(listed) == 1, "dismissed capture is not listed anywhere"
    assert listed[0].note == "featureless", "the reason is not readable"
    assert listed[0].kind == "dismissed"


def test_a_confirmed_record_never_appears_as_dismissed(monkeypatch):
    """`dismissed` and `confirmed` are separate terminal states. A record
    carrying a citation must not show up in the not-attributable list even if
    a stale `dismissed` string is somehow on it.
    """
    rec, _ = _filed(monkeypatch)
    rec.confirmed = True
    rec.citation = "Harris et al. 2020"
    rec.dismissed = "stale"
    assert service.dismissed_items() == []
