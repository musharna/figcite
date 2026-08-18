"""Characterization tests for `figcite pending` and `figcite confirm`.

Written against the UN-refactored cli.py, before Task 3 moved these two
commands onto the service layer. Nothing else in the suite calls
`cli.main(["pending"])` or `cli.main(["confirm", ...])`, so without these the
refactor would have been "validated" by 141 tests that never executed a line
of the code being rewritten.
"""

import json

from PIL import Image

from figcite import cli, clipboard, store


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
