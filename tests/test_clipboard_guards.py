"""clipboard.py guards: the Windows shims, the disk search, and filing.

Eighteen survivors. Six are the same guard three times over -- `if
r.returncode == 0` in each of the three subprocess shims -- and they survived
for a reason conftest introduced deliberately: the unit suite now blocks
PowerShell interop, so those functions never run their success path. Blocking
a live dependency must not cost the coverage that depended on it, so the
subprocess is supplied here instead of forbidden.

The last one is the same bug as `service._clear_staged`: `png.exists() and
png != dest` under `or` DELETES the image that was just filed. Two
independent copies of one destructive guard, and neither was covered.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from figcite import clipboard


class _R:
    def __init__(self, returncode, stdout=""):
        self.returncode, self.stdout = returncode, stdout


# ------------------------------------------------- the three subprocess shims


@pytest.mark.parametrize(
    "fn,args,good_out,expect_good,expect_bad",
    [
        (clipboard._win_userprofile, (), r"C:\Users\me" + "\n", r"C:\Users\me", None),
        (
            clipboard.win_to_wsl,
            (r"C:\Users\me",),
            "/mnt/c/Users/me\n",
            "/mnt/c/Users/me",
            r"C:\Users\me",
        ),
        (
            clipboard.wsl_to_win,
            ("/mnt/c/Users/me",),
            r"C:\Users\me" + "\n",
            r"C:\Users\me",
            "/mnt/c/Users/me",
        ),
    ],
    ids=["_win_userprofile", "win_to_wsl", "wsl_to_win"],
)
def test_a_shim_uses_its_output_only_when_the_command_succeeded(
    monkeypatch, fn, args, good_out, expect_good, expect_bad
):
    """`if r.returncode == 0` survived `Eq -> NotEq` and `0 -> 1` at all three
    sites.

    Inverted, the shims return the output of FAILED commands and discard the
    output of successful ones -- so a working machine falls back to the
    unconverted path (a Windows path used as a POSIX one, or the reverse) and
    a broken one confidently returns whatever the failure printed. Each shim
    falls back to something different, which is why each is driven separately
    rather than through one representative.
    """
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R(0, good_out))
    assert fn(*args) == expect_good, "a successful conversion was discarded"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R(1, "an error message"))
    assert fn(*args) == expect_bad, "a FAILED command's output was used"


# ------------------------------------------------- searching the disk


def test_a_search_root_that_does_not_exist_is_skipped(tmp_path, monkeypatch):
    """`if not root.exists(): continue` survived dropping its `not`.

    Inverted, every root that EXISTS is skipped and every missing one is
    searched -- so the search runs `find` over paths that are not there and
    never looks in the ones that are. The result is a clean "not found" for a
    PDF sitting in Downloads.
    """
    monkeypatch.setattr(clipboard, "_win_userprofile", lambda: None)
    real = tmp_path / "here"
    real.mkdir()
    (real / "paper.pdf").write_bytes(b"%PDF-1.4")

    got = clipboard.find_pdf_on_disk("paper.pdf", roots=[tmp_path / "missing", real])

    assert got and got.endswith("paper.pdf"), got


def test_a_find_hit_must_match_the_filename_exactly(tmp_path, monkeypatch):
    """`if line.strip() and Path(line).name.lower() == target` survived
    `And -> Or` and `Eq -> NotEq`.

    `find -iname` is case-insensitive, and the check re-verifies the basename
    because the loop is over raw stdout lines. Under `or` a blank line passes
    (`"" or ...`) and is returned as a path; under `!=` the first NON-matching
    line is returned, which is some other PDF entirely -- attached to a figure
    as its source.
    """
    monkeypatch.setattr(clipboard, "_win_userprofile", lambda: None)
    root = tmp_path / "r"
    root.mkdir()

    def fake_run(cmd, **kw):
        return _R(0, f"\n{root}/other-paper.pdf\n{root}/Paper.PDF\n")

    monkeypatch.setattr(subprocess, "run", fake_run)

    got = clipboard.find_pdf_on_disk("paper.pdf", roots=[root])

    assert got == f"{root}/Paper.PDF", (
        f"a line that is not the requested file was returned: {got!r}"
    )


# ------------------------------------------------- inference thresholds


def test_a_long_enough_query_falls_back_to_a_bibliographic_search(monkeypatch):
    """`if len(q) > 12 and not out["candidates"]` survived `12 -> 13` and
    `Gt -> GtE`.

    Thirteen characters is the shortest query worth sending to CrossRef's
    bibliographic search -- shorter ones return noise. Both mutants raise the
    floor by one, so a 13-character title silently stops producing candidates
    and the capture is filed with nothing for a human to pick from.
    """
    seen = {}

    def fake_search(q, rows=5):
        seen["q"] = q
        return [{"doi": "10.1/found", "title": "T"}]

    monkeypatch.setattr(clipboard, "search_bibliographic", fake_search)
    monkeypatch.setattr(clipboard, "_zotero_try", lambda *a, **kw: "no zotero hit")
    # infer_source reaches find_pdf_on_disk, which asks Windows for the user
    # profile. conftest blocks that (correctly -- it caught this test), so the
    # answer is supplied rather than the guard suppressed.
    monkeypatch.setattr(clipboard, "_win_userprofile", lambda: None)

    q13 = "abcdefghijklm"
    assert len(q13) == 13
    out = clipboard.infer_source({"title": q13, "process": "firefox.exe"})

    assert seen.get("q"), (
        f"a {len(q13)}-character title never reached the bibliographic search: {out}"
    )
    assert out["candidates"], out


def test_a_short_query_is_not_sent_to_crossref(monkeypatch):
    """Positive control: "always search" passes the test above and puts a
    throttled network call behind every snip of a 3-word window title."""
    monkeypatch.setattr(
        clipboard,
        "search_bibliographic",
        lambda q, rows=5: pytest.fail(f"searched CrossRef for {q!r}"),
    )
    monkeypatch.setattr(clipboard, "_zotero_try", lambda *a, **kw: "no zotero hit")
    monkeypatch.setattr(clipboard, "_win_userprofile", lambda: None)

    clipboard.infer_source({"title": "abcdefghijkl", "process": "firefox.exe"})


def test_zotero_candidates_do_not_overwrite_ones_already_found(monkeypatch):
    """`if z.get("candidates") and not out.get("candidates")` survived
    mutating the key on the second call.

    Under the mutant `out.get("MUTANT")` is always None, so Zotero's
    candidates overwrite whatever an earlier route already found rather than
    filling a gap. The earlier routes are the more specific ones.
    """
    out = {"candidates": [{"doi": "10.1/already", "title": "found earlier"}]}
    monkeypatch.setattr(
        clipboard.zotero,
        "resolve",
        lambda *a, **kw: {
            "doi": None,
            "grounded": False,
            "evidence": "zotero says",
            "candidates": [{"doi": "10.1/zotero", "title": "from zotero"}],
        },
    )

    clipboard._zotero_try("a query", out)

    assert [c["doi"] for c in out["candidates"]] == ["10.1/already"], (
        f"candidates found earlier were replaced by Zotero's: {out['candidates']}"
    )


# ------------------------------------------------- filing a capture


def _pending(png, *, grounded, doi):
    return {
        "png": str(png),
        "capture": {"title": "A paper", "process": "firefox.exe"},
        "inference": {
            "grounded": grounded,
            "doi": doi,
            "kind": "browser",
            "doi_evidence": "because",
            "candidates": [],
        },
    }


@pytest.fixture
def _own_store(tmp_path, monkeypatch):
    from figcite import store

    monkeypatch.setattr(store, "MANIFEST", tmp_path / "m.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY", tmp_path / "library")


def test_only_a_grounded_capture_with_a_doi_gets_a_citation(tmp_path, monkeypatch, _own_store):
    """`if inf.get("grounded") and inf.get("doi")` survived `And -> Or`.

    Under `or` EITHER half is enough: a capture that is grounded but whose DOI
    is None goes looking one up from None, and one with a DOI that nothing
    grounded is filed CONFIRMED -- a machine guess written to the manifest as
    though a human had checked it. That is the single thing this tool exists
    to prevent.
    """
    from figcite import crossref
    from figcite.provenance import Record

    monkeypatch.setattr(
        crossref,
        "record_from_doi",
        lambda doi, **kw: Record(
            doi=doi,
            citation="C",
            short_cite="S",
            **{k: v for k, v in kw.items() if k in ("confirmed", "source_kind", "source_detail")},
        ),
    )

    png = tmp_path / "clip-1.png"
    Image.new("RGB", (8, 8), "white").save(png)
    dest = clipboard.auto_finalize(png, _pending(png, grounded=False, doi="10.1/guess"))

    from figcite import store

    rec = list(store.all_records().values())[-1]
    assert rec.confirmed is False, "an ungrounded capture was filed as a confirmed citation"
    assert dest is not None


def test_enrich_reports_candidates_when_there_is_no_doi(tmp_path, monkeypatch, capsys):
    """`elif inf.get("candidates")` survived mutating the key -- so a capture
    with candidates printed "no source inferred", and the user was told to
    look for something the tool had already found."""
    monkeypatch.setattr(
        clipboard,
        "infer_source",
        lambda cap: {"doi": "", "candidates": [{"doi": "10.1/a"}], "doi_evidence": "e"},
    )
    png = tmp_path / "clip-4.png"
    Image.new("RGB", (8, 8), "white").save(png)

    clipboard.enrich(png)
    out = capsys.readouterr().out

    assert "unconfirmed candidates" in out, out
    assert "no source inferred" not in out, out


def test_filing_in_place_keeps_the_file(tmp_path, monkeypatch, _own_store):
    """`if png.exists() and png != dest` survived `And -> Or` -- the SAME
    guard, and the same mutant, as `service._clear_staged`.

    Two independent copies of one destructive rule, neither covered. Under
    `or` the first operand alone fires the unlink, so filing in place deletes
    the image the manifest was just told about: the capture reports success,
    a record exists, and the file it points at is gone.

    `finalize_into_library` is made to return the source path, which is what
    filing in place means; my first attempt at this stubbed a function that
    does not exist (`raising=False` accepted it silently) so the branch was
    never reached and the mutant lived through a test written to kill it.
    """
    from figcite import store

    png = tmp_path / "clip-inplace.png"
    Image.new("RGB", (8, 8), "white").save(png)
    monkeypatch.setattr(store, "finalize_into_library", lambda src, rec: Path(src))

    dest = clipboard.auto_finalize(png, _pending(png, grounded=False, doi=""))

    assert dest == png
    assert png.exists(), "the capture was deleted by the cleanup that runs after filing it in place"


def test_a_staged_copy_separate_from_its_destination_is_removed(tmp_path, monkeypatch, _own_store):
    """Positive control: "never unlink" passes the test above and leaves every
    snip in staging forever, which is what the cleanup exists to prevent."""
    from figcite import store

    png = tmp_path / "clip-staged.png"
    filed = tmp_path / "library" / "filed.png"
    filed.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), "white").save(png)
    Image.new("RGB", (8, 8), "white").save(filed)
    monkeypatch.setattr(store, "finalize_into_library", lambda src, rec: filed)

    dest = clipboard.auto_finalize(png, _pending(png, grounded=False, doi=""))

    assert dest == filed and filed.exists()
    assert not png.exists(), "the staged copy was left behind"


def test_the_capture_sidecar_is_found_for_a_name_containing_a_dot(tmp_path, monkeypatch):
    """`if not cap_file.exists()` survived dropping its `not`.

    There are two spellings of the sidecar name and this picks between them.
    They only DIFFER when the filename has a dot in its stem -- `with_suffix`
    replaces everything after the last dot, so "clip-1.5.png" becomes
    "clip-1.capture.json", while the string form gives
    "clip-1.5.capture.json". My first test used a dotless name, where both
    spellings agree and the guard cannot be observed at all.

    Inverted, the spelling that EXISTS is discarded in favour of the one that
    does not, so the capture is enriched against an empty dict and the window
    title, process and URL are all silently absent from the inference.
    """
    monkeypatch.setattr(clipboard, "infer_source", lambda cap: {"seen": cap, "doi": ""})
    png = tmp_path / "clip-1.5.png"
    Image.new("RGB", (8, 8), "white").save(png)
    (tmp_path / "clip-1.5.capture.json").write_text(
        json.dumps({"title": "A paper", "process": "firefox.exe"}), encoding="utf-8"
    )

    clipboard.enrich(png)

    pending = json.loads((tmp_path / "clip-1.5.pending.json").read_text())
    assert pending["inference"]["seen"]["title"] == "A paper", (
        f"the capture sidecar was not found: {pending['inference']}"
    )


def test_the_watcher_reports_a_second_watcher_rather_than_racing_it(tmp_path, monkeypatch, capsys):
    """`if line.startswith("WATCH_ALREADY_RUNNING")` survived mutating the
    literal.

    Two watchers on one staging directory both claim the same PNG, and the
    loser's copy is deleted out from under it mid-read. The PowerShell side
    detects that and says so on stdout; this branch turns it into an
    explanation. Under the mutant the sentinel is not recognised and the raw
    protocol line is printed as though it were a captured file.
    """
    import sys

    monkeypatch.setattr(clipboard, "PS_EXE", sys.executable)
    monkeypatch.setattr(clipboard, "staging_dirs", lambda: (r"C:\st", tmp_path))

    class _Proc:
        stdout = iter([r"WATCH_ALREADY_RUNNING C:\st", ""])

        def terminate(self):
            pass

    monkeypatch.setattr(clipboard.subprocess, "Popen", lambda *a, **kw: _Proc())

    rc = clipboard.watch(max_hours=0.001)
    out = capsys.readouterr().out

    assert rc == 0
    assert "another watcher already owns this staging directory" in out, out
    assert "WATCH_ALREADY_RUNNING" not in out.replace(
        "another watcher already owns this staging directory", ""
    ), f"the raw protocol line was printed to the user: {out!r}"
