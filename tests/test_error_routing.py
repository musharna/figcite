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
