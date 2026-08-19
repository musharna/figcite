"""Two gaps the screenshot review turned up on a filed capture.

D3: a filed capture could not be marked as your own work through either front
end -- `_confirm_filed` took only a DOI, and the web card hid the button to
match. Snip your own plot with Snipping Tool and there was no way to say so.

D4: the card never showed the `m0` the CLI addresses it by, because the CLI
invented that label inside its own printer by enumerating a filtered list. The
one thing both front ends must agree on -- how to name an item -- was the one
thing only one of them knew.
"""

import pytest

from figcite import service, store
from figcite.provenance import Record, now_stamps


def _file_one(sha, detail=None, note=""):
    u, loc = now_stamps()
    store.put(
        Record(
            sha256=sha,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            captured_utc=u,
            captured_local=loc,
            source_detail=detail
            or {
                "clipboard_capture": {
                    "process": "SnippingTool",
                    "title": "Snipping Tool",
                }
            },
            note=note,
        )
    )
    return f"filed:{sha}"


def _item(ref):
    return [i for i in service.pending_items() if i.ref == ref][0]


# ------------------------------------------------------------------- own work


def test_a_filed_capture_can_be_marked_as_your_own_work(monkeypatch):
    seen = {}

    def fake_record_for(doi, cite, _x, **kw):
        seen["doi"], seen["cite"] = doi, cite
        return Record(
            sha256="a1" * 32,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=True,
            captured_utc="",
            captured_local="",
        )

    monkeypatch.setattr(service, "record_for", fake_record_for)
    ref = _file_one("a1" * 32)
    service.confirm(ref, own_work=True)

    assert seen["doi"] is None
    assert seen["cite"] == "This work", "own work must not be filed with a DOI"


def test_own_work_and_a_doi_are_still_mutually_exclusive():
    """The arity guard must not be bypassed by the filed path."""
    ref = _file_one("b1" * 32)
    with pytest.raises(ValueError):
        service.confirm(ref, own_work=True, doi="10.1/x")


def test_a_filed_capture_with_no_selector_is_still_refused():
    """Positive control: widening the path must not weaken Ruling 4's guard."""
    ref = _file_one("c1" * 32)
    with pytest.raises(ValueError):
        service.confirm(ref)


# ----------------------------------------------------------------- the cli ref


def test_every_pending_item_carries_the_name_the_cli_uses():
    _file_one("d1" * 32)
    items = service.pending_items()
    filed = [i for i in items if i.kind == "filed"]
    assert filed, "fixture did not produce a filed item"
    for n, it in enumerate(filed):
        assert it.cli_ref == f"m{n}"


def test_the_cli_prints_the_ref_the_service_assigned(capsys):
    """The two must not be able to disagree, which they could while the CLI
    derived the label itself from a list it re-filtered."""
    from figcite import cli

    _file_one("e1" * 32)
    cli.cmd_pending(None)
    out = capsys.readouterr().out

    for it in service.pending_items():
        if it.kind == "filed":
            assert f"[{it.cli_ref}]" in out
