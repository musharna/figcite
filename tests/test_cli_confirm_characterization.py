"""Characterization tests for `figcite pending` and `figcite confirm`.

Written against the UN-refactored cli.py, before Task 3 moved these two
commands onto the service layer. Nothing else in the suite calls
`cli.main(["pending"])` or `cli.main(["confirm", ...])`, so without these the
refactor would have been "validated" by 141 tests that never executed a line
of the code being rewritten.
"""

import json
import re
from pathlib import Path

import pytest
from PIL import Image

from figcite import _actions, cli, clipboard, store
from figcite.provenance import Record, now_stamps


def _stage(tmp_path, monkeypatch, inference):
    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-1.png"
    Image.new("RGB", (40, 40), "white").save(png)
    (staging / "clip-1.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {
                    "process": "firefox",
                    "title": "A paper",
                    "width": 40,
                    "height": 40,
                },
                "inference": inference,
            }
        )
    )
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    return png


def test_pending_lists_a_staged_capture_with_its_window(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, monkeypatch, {"kind": "browser", "candidates": []})
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "[0]" in out
    assert "firefox" in out
    assert "A paper" in out


def test_pending_says_so_when_nothing_is_staged(tmp_path, monkeypatch, capsys):
    staging = tmp_path / "empty"
    staging.mkdir()
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    # The manifest is shared by the whole pytest session and other test files
    # file unconfirmed clipboard records into it. "nothing staged" is the thing
    # under test here, so the filed side is pinned empty rather than left to
    # collection order.
    monkeypatch.setattr(store, "all_records", dict)
    assert cli.main(["pending"]) == 0
    assert "nothing pending" in capsys.readouterr().out


def test_confirm_refuses_a_guessed_doi(tmp_path, monkeypatch, capsys):
    """The behavior that must survive the refactor."""
    _stage(
        tmp_path,
        monkeypatch,
        {"doi": "10.1/guess", "grounded": False, "candidates": []},
    )
    assert cli.main(["confirm", "0"]) == 2
    assert "only guessed" in capsys.readouterr().err


def test_confirm_with_a_cite_files_the_capture(tmp_path, monkeypatch):
    png = _stage(tmp_path, monkeypatch, {"candidates": []})
    # Pick the record out by identity, not by timestamp: captured_utc has
    # second resolution, so `max(..., key=captured_utc)` ties with whatever
    # another test file happened to file in the same second and returns that
    # one instead (it did, on the first full-suite run).
    before = set(store.all_records())
    assert cli.main(["confirm", "0", "--cite", "Band et al. 2014"]) == 0
    new = set(store.all_records()) - before
    assert len(new) == 1, f"confirm should file exactly one record, got {new}"
    rec = store.all_records()[new.pop()]
    assert rec.citation == "Band et al. 2014"
    assert rec.confirmed is True
    assert not png.exists(), "the staged png is consumed on confirm"


def test_confirm_rejects_an_out_of_range_index(tmp_path, monkeypatch, capsys):
    _stage(tmp_path, monkeypatch, {"candidates": []})
    assert cli.main(["confirm", "7"]) == 2
    assert "no pending item" in capsys.readouterr().err


# --------------------------------------------------------------- fix round 1


def test_confirm_prints_where_it_filed_the_image_and_its_sha(
    tmp_path, monkeypatch, capsys
):
    """Ruling 6. The four lines the service-layer refactor silently dropped.

    The sha256 hint is not decoration: it names WHICH file to put in the deck.
    """
    _stage(tmp_path, monkeypatch, {"candidates": []})
    assert cli.main(["confirm", "0", "--cite", "Band et al. 2014"]) == 0
    out = capsys.readouterr().out
    assert "tagged -> " in out
    assert "license: not stated by publisher" in out
    assert "sha256: " in out
    assert "insert THIS file into your deck" in out
    dest = Path(out.splitlines()[0].split("tagged -> ", 1)[1])
    assert dest.exists(), "the printed path must name the file that actually landed"


def test_confirm_warns_when_the_source_is_retracted(tmp_path, monkeypatch, capsys):
    """Ruling 6. A correctness warning about the source, not decoration."""
    _stage(tmp_path, monkeypatch, {"candidates": []})

    def _retracted(doi, **kw):
        u, loc = now_stamps()
        return Record(
            doi=doi,
            citation="Retracted et al. 2019",
            short_cite="Retracted et al. 2019",
            retracted=True,
            source_kind=kw.get("source_kind", ""),
            source_detail=kw.get("source_detail", {}),
            captured_utc=u,
            captured_local=loc,
            confirmed=True,
        )

    # Patched at the seam _actions.record_for actually calls, so the retracted
    # record travels the real finalize/store path without touching CrossRef.
    monkeypatch.setattr(_actions, "record_from_doi", _retracted)
    assert cli.main(["confirm", "0", "--doi", "10.1/retracted"]) == 0
    out = capsys.readouterr().out
    assert "tagged -> " in out, "positive control: the confirm actually printed"
    assert "** THIS WORK IS FLAGGED AS RETRACTED IN CROSSREF **" in out


def test_pending_shows_why_a_filed_capture_is_unresolved(
    tmp_path, monkeypatch, capsys
):
    """Ruling 7. figcite records the note; the surface must not drop it."""
    staging = tmp_path / "empty"
    staging.mkdir()
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    u, loc = now_stamps()
    rec = Record(
        sha256="a" * 64,
        dhash="0" * 16,
        source_kind="clipboard",
        confirmed=False,
        note="the tab was closed before the DOI could be read",
        captured_utc=u,
        captured_local=loc,
        source_detail={"clipboard_capture": {"process": "firefox", "title": "A paper"}},
    )
    monkeypatch.setattr(store, "all_records", lambda: {rec.sha256: rec})
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "[m0]" in out, "positive control: the filed capture is listed at all"
    assert "why: the tab was closed before the DOI could be read" in out


def test_an_explicit_cite_outranks_a_guessed_doi(tmp_path, monkeypatch):
    """Ruling 8. A human-supplied citation beats a machine guess; the old CLI
    refused this because it read the guess before it read --cite."""
    _stage(
        tmp_path,
        monkeypatch,
        {"doi": "10.1/guess", "grounded": False, "candidates": []},
    )
    before = set(store.all_records())
    assert cli.main(["confirm", "0", "--cite", "Whitcomb 2021"]) == 0
    new = set(store.all_records()) - before
    assert len(new) == 1, f"confirm should file exactly one record, got {new}"
    rec = store.all_records()[new.pop()]
    assert rec.citation == "Whitcomb 2021"
    assert rec.doi is None, "the guessed DOI must not ride along"


def test_confirm_refuses_a_doi_and_a_cite_together(tmp_path, monkeypatch, capsys):
    """Ruling 8. Silently discarding one of two conflicting citations is the
    failure mode this project exists to prevent."""
    png = _stage(tmp_path, monkeypatch, {"candidates": []})
    assert cli.main(["confirm", "0", "--doi", "10.1/x", "--cite", "Someone 2020"]) == 2
    assert "at most one" in capsys.readouterr().err
    assert png.exists(), "a refusal must not consume the capture"


def test_confirming_a_filed_capture_says_the_image_is_unchanged(
    tmp_path, monkeypatch, capsys
):
    """Fix round 2. The `m` branch's trailing line went missing in the refactor
    and nothing noticed, because no test read this command's stdout at all.

    It is not decoration either: it is the answer to "so where is my file?"
    for the one confirm path that does NOT produce a new file.
    """
    staging = tmp_path / "empty"
    staging.mkdir()
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    u, loc = now_stamps()
    filed = Record(
        sha256="c" * 64,
        dhash="2" * 16,
        source_kind="clipboard",
        confirmed=False,
        captured_utc=u,
        captured_local=loc,
        source_detail={"clipboard_capture": {"process": "firefox", "title": "A paper"}},
    )
    # store.get() reads through all_records(), so this one patch serves both.
    monkeypatch.setattr(store, "all_records", lambda: {filed.sha256: filed})
    monkeypatch.setattr(store, "put", lambda rec: None)

    def _resolved(doi, **kw):
        return Record(
            doi=doi,
            citation="Resolved et al. 2020",
            short_cite="Resolved et al. 2020",
            source_kind=kw.get("source_kind", ""),
            source_detail=kw.get("source_detail", {}),
            captured_utc=u,
            captured_local=loc,
            confirmed=True,
        )

    monkeypatch.setattr(_actions, "record_from_doi", _resolved)
    assert cli.main(["confirm", "m0", "--doi", "10.1/real"]) == 0
    out = capsys.readouterr().out
    assert "resolved m0: Resolved et al. 2020" in out, "positive control"
    assert (
        "  (the image file itself is unchanged; the manifest now carries the citation)"
        in out
    )
    assert "tagged -> " not in out, "nothing was filed, so nothing may be claimed filed"


# --------------------------------------------------------------- fix round 3


def test_confirm_rejects_an_out_of_range_pick(tmp_path, monkeypatch, capsys):
    """I1. cmd_confirm's `except KeyError` had no coverage at all: mutating it
    to `raise` left the suite green, one revert away from a traceback."""
    png = _stage(
        tmp_path,
        monkeypatch,
        {"candidates": [{"score": 88, "doi": "10.2/x", "title": "A title"}]},
    )
    assert cli.main(["confirm", "0", "--pick", "7"]) == 2
    assert "no candidate" in capsys.readouterr().err
    assert png.exists(), "a refusal must not consume the capture"


def test_pending_distinguishes_a_failed_lookup_from_an_empty_one(
    tmp_path, monkeypatch, capsys
):
    """I2. The brief's own new branch, and the bug Task 11's `error` field
    exists to fix: a lookup that could not run used to print identically to a
    lookup that ran and found nothing. Both directions are asserted, because
    only the pair distinguishes the two states."""
    _stage(
        tmp_path,
        monkeypatch,
        {"kind": "browser", "error": "crossref timed out", "candidates": []},
    )
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "LOOKUP FAILED: crossref timed out" in out
    assert "no source inferred" not in out


@pytest.mark.parametrize("grounded", [True, False])
def test_pending_shows_an_inferred_doi_whether_or_not_it_is_grounded(
    tmp_path, monkeypatch, capsys, grounded
):
    """M1. Showing the DOI is not gated on `grounded` -- `pending` reports what
    was inferred and `confirm` is what refuses a guess. Gating the line here
    would hide the guess a user needs to see in order to accept it explicitly,
    and that mutation survived a green suite."""
    _stage(
        tmp_path,
        monkeypatch,
        {
            "doi": "10.5/real",
            "grounded": grounded,
            "doi_evidence": "citation_doi meta tag",
            "candidates": [],
        },
    )
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "DOI: 10.5/real   (from citation_doi meta tag)" in out
    assert "confirm: figcite confirm 0" in out
    assert "no source inferred" not in out


def test_pending_lists_candidates_with_their_scores(tmp_path, monkeypatch, capsys):
    """M1. The candidate branch was rewritten field-by-field with no net."""
    _stage(
        tmp_path,
        monkeypatch,
        {
            "candidates": [
                {
                    "score": 88,
                    "doi": "10.2/x",
                    "title": "Auxin transport in the root",
                    "container": "Plant Cell",
                    "year": 2020,
                    "type": "journal-article",
                }
            ]
        },
    )
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "cand 0: score    88  10.2/x" in out
    assert "Auxin transport in the root (Plant Cell 2020) [journal-article]" in out
    assert "confirm: figcite confirm 0 --pick <n>   (or --doi 10.x/y)" in out


def test_pending_heads_the_filed_section_so_m0_means_something(
    tmp_path, monkeypatch, capsys
):
    """M3. Without the header, `[m0]` is an unexplained index in a list whose
    other entries are numbered differently and mean something else."""
    staging = tmp_path / "empty"
    staging.mkdir()
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    u, loc = now_stamps()
    rec = Record(
        sha256="e" * 64,
        dhash="4" * 16,
        source_kind="clipboard",
        confirmed=False,
        captured_utc=u,
        captured_local=loc,
        source_detail={"clipboard_capture": {"process": "firefox", "title": "A paper"}},
    )
    monkeypatch.setattr(store, "all_records", lambda: {rec.sha256: rec})
    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert "1 filed capture(s) with context but no citation:" in out
    assert out.splitlines()[0].endswith("no citation:"), "the header comes first"


_KWARGISH = re.compile(r"\b\w+=")


def _assert_speaks_cli(err: str) -> None:
    """I3. Structural, not a list of the three kwarg names I know about today.

    A new selector added to `service.confirm()` would leak into CLI output the
    same way, and a name list could not see it -- which is how `own_work=True`
    got as far as the user in the first place.
    """
    assert err.strip(), "positive control: the refusal actually said something"
    assert "own_work" not in err, "the CLI has no --this-work on `confirm`"
    assert not _KWARGISH.search(err), f"service kwargs leaked to the CLI: {err!r}"


def test_a_bare_confirm_with_nothing_inferable_names_real_flags(
    tmp_path, monkeypatch, capsys
):
    """I3. Delta 13. The old CLI said `need --doi, --pick N, or --cite`; the
    refactor started telling the user to pass `own_work=True`, which
    `figcite confirm` has no flag for at all."""
    _stage(tmp_path, monkeypatch, {"candidates": []})
    assert cli.main(["confirm", "0"]) == 2
    err = capsys.readouterr().err
    _assert_speaks_cli(err)
    assert "--doi" in err and "--pick N" in err and "--cite" in err


def test_two_selectors_are_refused_in_cli_words(tmp_path, monkeypatch, capsys):
    """I3. Ruling 8 made this message user-visible for the first time."""
    _stage(tmp_path, monkeypatch, {"candidates": []})
    assert cli.main(["confirm", "0", "--doi", "10.1/x", "--cite", "Someone 2020"]) == 2
    err = capsys.readouterr().err
    _assert_speaks_cli(err)
    assert "at most one" in err


def test_a_filed_capture_refusal_names_the_flag(tmp_path, monkeypatch, capsys):
    """I3. `a filed capture can only be resolved with doi=` is the third path
    that reaches the user in service vocabulary."""
    staging = tmp_path / "empty"
    staging.mkdir()
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (None, staging))
    u, loc = now_stamps()
    rec = Record(
        sha256="f" * 64,
        dhash="5" * 16,
        source_kind="clipboard",
        confirmed=False,
        captured_utc=u,
        captured_local=loc,
        source_detail={"clipboard_capture": {"process": "firefox", "title": "A paper"}},
    )
    monkeypatch.setattr(store, "all_records", lambda: {rec.sha256: rec})
    assert cli.main(["confirm", "m0"]) == 2
    err = capsys.readouterr().err
    _assert_speaks_cli(err)
    assert "--doi" in err
