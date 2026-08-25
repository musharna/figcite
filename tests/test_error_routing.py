"""What each handler catches, and what it must NOT catch.

The exception-routing mutation tier: broaden a handler to `except Exception`,
or delete one type from a caught tuple, and see whether anything notices. It
killed 7 of 35, a 20% rate against 96% for the verdict-constructor tier --
which is the published shape of the problem, exception paths being
disproportionately under-tested (Lima, Rocha, Bezerra & Paixao 2021,
arXiv:2105.00500, the study that motivated running this tier at all).

These are the ranked survivors. Every one is the same failure in a different
place: a handler that catches MORE than it can justify converts a defect into
a diagnosis. figcite's whole job is distinguishing "searched, not there" from
"could not look", and a broadened `except` quietly relabels the first as the
second -- or worse, relabels OUR bug as the user's outage.

The tests are written as pairs on purpose: the legitimate failure must still
be handled, AND the illegitimate one must still escape. Asserting only the
first passes against `except Exception`, which is the mutant.
"""

from __future__ import annotations

import json

import pytest

from figcite import cli, corpus, match, service, store, zotero
from figcite.provenance import Record
from figcite.crossref import LookupUnavailable
from figcite.zotero import NotConfigured


def _row(dhash="0f0f0f0f0f0f0f0f"):
    return corpus.FigureRow(
        pmcid="PMC1",
        doi="10.1/x",
        label="Figure 1",
        caption="c",
        licence="cc-by",
        source_url="http://example.invalid/x",
        dhash=dhash,
        width=64,
        height=64,
        image_path="fig.png",
    )


# ------------------------------------------------- match.by_dhash decode guard


def test_the_decode_guard_catches_pils_refusal_and_nothing_else(monkeypatch):
    """`except OSError` survived being broadened to `except Exception`.

    The guard exists for one thing: PIL declining to read the bytes. Broadened,
    a genuine defect inside `dhash_bytes` -- a TypeError from a bad refactor --
    is reported to the user as "the query image could not be decoded", blaming
    their file for our bug, and no test noticed.
    """
    boom = TypeError("a bug in dhash_bytes, not a bad image")

    def exploding(*a, **kw):
        raise boom

    monkeypatch.setattr(match, "dhash_bytes", exploding)

    with pytest.raises(TypeError):
        match.by_dhash(b"anything", [_row()])


def test_the_decode_guard_still_handles_the_failure_it_is_for(monkeypatch):
    """Positive control: narrowing must not have removed the handling."""

    def refuses(*a, **kw):
        raise OSError("Truncated File Read")

    monkeypatch.setattr(match, "dhash_bytes", refuses)

    verdict = match.by_dhash(b"anything", [_row()])
    assert isinstance(verdict, match.CouldNotDecide), verdict
    assert "could not be decoded" in verdict.reason


# ------------------------------------------------- zotero.resolve outage arms


def test_our_own_bug_is_not_reported_as_the_users_library_being_down(monkeypatch):
    """`except LookupUnavailable` survived being broadened to `except Exception`.

    This is the LAST handler in `resolve`, so broadening it swallows anything
    `resolve_title` can raise. A TypeError in figcite would then reach the user
    as `available: False` and "Zotero unreachable -- this is NOT a miss": our
    defect, described as their outage, with a reassurance attached. The user
    goes and checks their network.
    """

    def exploding(*a, **kw):
        raise TypeError("a bug in resolve_title")

    monkeypatch.setattr(zotero, "resolve_title", exploding)

    with pytest.raises(TypeError):
        zotero.resolve("a title")


@pytest.mark.parametrize(
    "exc,expected",
    [
        (NotConfigured("no key"), "not configured"),
        (LookupUnavailable("timeout"), "unreachable"),
    ],
    ids=["not-configured", "unreachable"],
)
def test_both_real_outages_are_still_reported_as_outages(exc, expected, monkeypatch):
    """Positive control, and it covers BOTH arms.

    The two handlers are siblings producing the same `available: False` shape,
    and the tier killed the mutant on one while the other survived -- the twin
    asymmetry this suite keeps rediscovering. Driving both here means neither
    can rot alone.
    """

    def raising(*a, **kw):
        raise exc

    monkeypatch.setattr(zotero, "resolve_title", raising)

    out = zotero.resolve("a title")
    assert out["available"] is False, out
    assert out["doi"] is None and out["grounded"] is False, out
    assert expected in out["evidence"].lower(), out["evidence"]


# ------------------------------------------- service sidecar scan, both arms


@pytest.mark.parametrize(
    "payload,arm",
    [(b"\xff\xfe not utf-8 or json", "ValueError"), (None, "OSError")],
    ids=["unparseable-json", "unreadable-file"],
)
def test_one_bad_sidecar_does_not_derail_the_library_scan(
    payload, arm, tmp_path, monkeypatch
):
    """`except (OSError, ValueError): continue` -- BOTH arms survived deletion.

    `_library_path_for` globs every sidecar in the library looking for one
    whose sha256 matches. A single corrupt or unreadable sidecar must be
    stepped over, not allowed to end the search: the file the caller actually
    wants may sort after it. Nothing tested either arm, so a library with one
    bad sidecar could hide every file behind it.
    """
    monkeypatch.setattr(store, "LIBRARY", tmp_path)

    bad = tmp_path / "aaa-first.png.figcite.json"
    if payload is None:
        bad.mkdir()  # a directory where a file is expected -> OSError on read
    else:
        bad.write_bytes(payload)

    good_img = tmp_path / "zzz-last.png"
    good_img.write_bytes(b"pretend image")
    (tmp_path / "zzz-last.png.figcite.json").write_text(
        json.dumps({"sha256": "abc123"}), encoding="utf-8"
    )

    found = service._library_path_for("abc123")

    assert found == good_img, (
        f"the {arm} arm let one bad sidecar hide the file sorted after it"
    )


def test_a_scan_that_finds_nothing_still_raises(tmp_path, monkeypatch):
    """Positive control for the two above.

    A `_library_path_for` that returned the first path it saw, or swallowed
    everything, would satisfy them. Absence must still be reported.
    """
    monkeypatch.setattr(store, "LIBRARY", tmp_path)
    (tmp_path / "x.png.figcite.json").write_text(
        json.dumps({"sha256": "different"}), encoding="utf-8"
    )

    with pytest.raises(service.LibraryFileMissing):
        service._library_path_for("abc123")


# ------------------------------------------------- cli.main's handled types


@pytest.mark.parametrize(
    "exc",
    [LookupError("nothing indexed"), ValueError("bad input")],
    ids=["LookupError", "ValueError"],
)
def test_the_top_level_handler_covers_every_type_it_claims(exc, monkeypatch, capsys):
    """`except (LookupError, RuntimeError, ValueError)` -- 2 of 3 arms survived.

    Only the RuntimeError arm was exercised. A tuple whose members are not all
    reachable is a claim about what can fail that nobody has checked, and this
    particular tuple is already known to be too NARROW: an OSError out of the
    matcher escaped it as a raw traceback. Pinning the three it does list is
    the precondition for arguing about the ones it does not.
    """

    def exploding():
        raise exc

    monkeypatch.setattr(corpus, "status", exploding)

    rc = cli.main(["corpus", "status"])

    assert rc == 1, rc
    assert str(exc) in capsys.readouterr().err


# ------------------------------------------- cmd_confirm: a typo is not a crash


def test_a_non_numeric_index_is_refused_not_a_traceback(monkeypatch, capsys):
    """`except (ValueError, IndexError)` survived `drop ValueError`.

    `cmd_confirm` does `pool[int(n)].ref`, and `n` is whatever the user typed.
    `figcite confirm abc` raises ValueError on the `int()`, and without that
    arm a typo at the command line is a raw traceback rather than the "no
    pending item" message the IndexError arm already gives for `confirm 99`.
    Only the IndexError arm was covered, so the two halves of one refusal had
    one test between them.
    """
    monkeypatch.setattr(service, "pending_items", lambda: [])

    assert cli.main(["confirm", "abc"]) == 2
    assert "no pending item abc" in capsys.readouterr().err


# --------------------------------- cmd_confirm: our bug is not your misuse


def _confirm_raising(monkeypatch, exc):
    item = service.PendingItem(ref="staged:q.png", kind="staged", context="ctx")
    monkeypatch.setattr(service, "pending_items", lambda: [item])

    def exploding(*a, **kw):
        raise exc

    monkeypatch.setattr(service, "confirm", exploding)


def test_a_defect_in_confirm_exits_one_not_two(monkeypatch, capsys):
    """Three handlers -- NotGrounded, ValueError, KeyError -- all survived
    broadening to `except Exception`.

    Their `try` is the whole `service.confirm(...)` pipeline: CrossRef, store
    writes, file operations. Each handler PRINTS and RETURNS 2, and 2 is the
    code that means "you invoked me wrong" -- argparse's own. Broadening any
    of them reports figcite's failure as the user's mistake, and tells a
    wrapper not to retry, because misuse is not worth retrying.

    RuntimeError is the probe because it is NOT one of the three: `cli.main`
    catches it at the top level and returns 1. So 1 vs 2 is the distinction
    that survives to the caller -- "I ran and broke" against "you called me
    wrong" -- and under the mutant it collapses to 2.

    ValueError deliberately is NOT used here: `service.confirm` raises it for
    genuine misuse ("needs doi="), so exit 2 is the CORRECT answer for it. A
    probe has to be a type that is not already a misuse, or the test asserts
    the opposite of what it means.
    """
    _confirm_raising(monkeypatch, RuntimeError("a defect inside confirm"))

    rc = cli.main(["confirm", "0", "--doi", "10.1/x"])

    assert rc == 1, (
        f"a defect inside confirm came back as exit {rc}; 2 would claim the "
        "user invoked the command wrongly"
    )


def test_an_unlisted_fault_in_confirm_is_not_swallowed(monkeypatch):
    """The same three broadenings, from the other side.

    TypeError is in neither `cmd_confirm`'s handlers nor `cli.main`'s tuple,
    so today it propagates. Broadening any of the three catches it and returns
    2 instead, which is the swallow this pins.
    """
    _confirm_raising(monkeypatch, TypeError("a bad refactor inside confirm"))

    with pytest.raises(TypeError, match="bad refactor"):
        cli.main(["confirm", "0", "--doi", "10.1/x"])


@pytest.mark.parametrize(
    "exc,fragment",
    [
        (ValueError("a filed capture needs doi="), "needs --doi"),
        (KeyError("no candidate 9 on staged:q.png"), "no candidate 9"),
    ],
    ids=["ValueError", "KeyError"],
)
def test_the_real_misuses_are_still_reported_as_misuse(
    exc, fragment, monkeypatch, capsys
):
    """Positive control for the pair above, and it is required.

    A `cmd_confirm` with no handlers at all satisfies the previous test
    perfectly. The two genuine misuse cases must still come back as exit 2
    with a message rather than as a traceback.
    """
    item = service.PendingItem(ref="staged:q.png", kind="staged", context="ctx")
    monkeypatch.setattr(service, "pending_items", lambda: [item])

    def raising(*a, **kw):
        raise exc

    monkeypatch.setattr(service, "confirm", raising)

    assert cli.main(["confirm", "0", "--doi", "10.1/x"]) == 2
    assert fragment in capsys.readouterr().err


# ------------------------- the library fallback must not mask a real fault


def test_a_broken_library_scan_does_not_silently_return_the_original(
    tmp_path, monkeypatch
):
    """`except LibraryFileMissing` survived broadening to `except Exception`.

    The handler exists for records that never had a library file -- `figcite
    register` and the matplotlib hook deliberately do not copy the image -- so
    it falls back to the `original_file` the record stores. It ends in `raise`,
    but only when that fallback is unavailable.

    So when the original DOES exist, broadening turns any fault in the library
    scan into a successful answer: the caller gets a path, and the failure that
    produced it is gone. That is the fallback covering for a defect rather than
    for the documented absence.
    """
    original = tmp_path / "mine.png"
    original.write_bytes(b"pretend image")

    rec = Record(
        sha256="c" * 64,
        dhash="0" * 16,
        source_kind="generated",
        confirmed=True,
        source_detail={"original_file": str(original)},
    )
    monkeypatch.setattr(store, "all_records", lambda: {rec.sha256: rec})

    def exploding(sha):
        raise RuntimeError("the library index is corrupt")

    monkeypatch.setattr(service, "_library_path_for", exploding)

    with pytest.raises(RuntimeError, match="library index is corrupt"):
        service._resolve_ref_to_path(f"filed:{rec.sha256}")


def test_a_record_that_never_had_a_library_file_still_gets_its_original(
    tmp_path, monkeypatch
):
    """Positive control: the documented fallback must still work.

    This is the whole reason the handler exists -- a registered figure has no
    library copy and never will -- so a test that only asserts faults escape
    would pass against a handler that had been deleted.
    """
    original = tmp_path / "mine.png"
    original.write_bytes(b"pretend image")

    rec = Record(
        sha256="d" * 64,
        dhash="0" * 16,
        source_kind="generated",
        confirmed=True,
        source_detail={"original_file": str(original)},
    )
    monkeypatch.setattr(store, "all_records", lambda: {rec.sha256: rec})

    def missing(sha):
        raise service.LibraryFileMissing("no library file for this record")

    monkeypatch.setattr(service, "_library_path_for", missing)

    assert service._resolve_ref_to_path(f"filed:{rec.sha256}") == original
