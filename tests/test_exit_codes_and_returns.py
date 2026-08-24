"""Exit codes, sentinels, and the strings a caller matches on.

The RETURN tier held 70 survivors and 34 of them are `return 0` / `return 1`
/ `return 2` in cli.py. An exit code is the only machine-readable thing a CLI
says: `figcite bib --check` exists to be wired into CI, and a suite that
asserts stdout while ignoring the status code cannot tell success from
failure the way the caller does.

The rest are sentinels and dispatch strings -- `hamming`'s 999, the
`matched_by` labels both front ends print, the sidecar suffix that pairs a
file with its provenance.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from figcite import cli, corpus, match, provenance, store, zotero
from figcite.provenance import Record


# ------------------------------------------------- CLI exit codes


def test_a_usage_error_exits_two_not_one(tmp_path, capsys):
    """`return 2` survived `2 -> 3` at four sites.

    2 is the conventional "you called me wrong" code -- argparse's own -- and
    it is what distinguishes a bad invocation from work that ran and failed.
    A script that retries on failure but not on misuse needs the two apart.
    """
    img = tmp_path / "fig.png"
    Image.new("RGB", (8, 8), "white").save(img)

    assert cli.main(["tag", str(img)]) == 2, "tag with nothing to cite"
    capsys.readouterr()
    assert cli.main(["tag", str(tmp_path / "missing.png"), "--doi", "10.1/x"]) == 2
    capsys.readouterr()
    assert cli.main(["register", str(tmp_path / "missing.png")]) == 2
    capsys.readouterr()


def test_success_exits_zero(tmp_path, monkeypatch, capsys):
    """`return 0` survived `0 -> 1` at fifteen sites.

    Under the mutant every successful command reports failure, so any script
    or CI step that checks `$?` fails on a run that did exactly what it was
    asked. Nothing in the suite noticed, because the assertions were on what
    was printed.
    """
    monkeypatch.setattr(zotero, "configured", lambda: True)
    monkeypatch.setattr(zotero, "credentials", lambda: ("k", "1", "group"))
    monkeypatch.setattr(
        zotero,
        "library",
        lambda *a, **kw: [
            {"title": "A paper", "doi": "10.1/a", "doi_source": "doi-field", "key": "K"}
        ],
    )
    assert cli.main(["zotero", "status"]) == 0
    capsys.readouterr()

    monkeypatch.setattr(corpus, "status", lambda: {"figures": 3, "papers": 1})
    assert cli.main(["corpus", "status"]) == 0
    capsys.readouterr()


def test_a_command_that_ran_and_failed_exits_one(monkeypatch, capsys):
    """`return 1` survived `1 -> 2` at seven sites.

    1 is "I ran and the answer is no" -- distinct from 2 ("you called me
    wrong"). `zotero status` on an unreachable library is the clearest case:
    the command worked, the library did not.
    """
    monkeypatch.setattr(zotero, "configured", lambda: False)
    assert cli.main(["zotero", "status"]) == 1
    capsys.readouterr()


def test_an_unexpected_error_exits_one_rather_than_a_traceback(monkeypatch, capsys):
    """`return 1` in main()'s catch-all survived `1 -> 2`.

    The top-level handler turns an unforeseen exception into a message and a
    status code. Under the mutant it becomes 2, which claims the USER
    mis-invoked the command when in fact figcite broke.
    """
    monkeypatch.setattr(
        corpus, "status", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    rc = cli.main(["corpus", "status"])

    assert rc == 1, rc
    assert "boom" in capsys.readouterr().err


def test_the_status_exit_code_reports_whether_a_watcher_is_running(monkeypatch, capsys):
    """`return 0 if st["watching"] else 1` survived `0 -> 1` and `1 -> 2`.

    This is the one exit code meant to be POLLED. Under either mutant the two
    outcomes stop being 0 and non-zero, so a wrapper that checks `$?` is
    either always alarmed or never.
    """
    from figcite import autostart

    base = {
        "installed": True,
        "launcher": "l",
        "processes": [],
        "supervisors": [],
        "log": "x",
        "log_mtime": None,
        "log_tail": [],
        "sessions": 0,
        "staged_pngs": 0,
        "clipboard_records": 0,
    }
    monkeypatch.setattr(autostart, "status", lambda: {**base, "watching": True})
    assert cli.main(["autostart", "status"]) == 0
    capsys.readouterr()

    monkeypatch.setattr(autostart, "status", lambda: {**base, "watching": False})
    assert cli.main(["autostart", "status"]) == 1
    capsys.readouterr()


# ------------------------------------------------- sentinels


def test_the_uncomparable_sentinel_sorts_below_every_real_distance():
    """`return 999` survived `999 -> 1000`.

    999 means "these two hashes cannot be compared". A 64-bit dhash can
    differ by at most 64, so any value above that works arithmetically --
    which is why the mutant survives a threshold comparison. What the
    constant must guarantee is that an uncomparable pair NEVER outranks a
    real one, and that is worth pinning as a property rather than as a
    literal.
    """
    assert provenance.hamming("", "0" * 16) > 64, (
        "the uncomparable sentinel is inside the range of real distances"
    )
    assert provenance.hamming("0" * 16, "f" * 16) == 64, "the true maximum"


def test_the_sidecar_suffix_is_a_file_format_not_an_implementation_detail():
    """`return str(image_path) + ".figcite.json"` survived being mutated.

    Writer and reader both go through this function, so a changed suffix is
    self-consistent and nothing in the code notices -- which is exactly why
    it needs pinning here. Every sidecar already written next to every filed
    figure carries this name, and `service._library_path_for` GLOBS for it to
    find a library file from a manifest hash. Change it and the existing
    library goes dark while the code looks fine.
    """
    assert provenance.sidecar_path("fig.png") == "fig.png.figcite.json"


@pytest.mark.parametrize(
    "bits,comparable",
    [(0, False), (1, True), (63, True), (64, False)],
    ids=["all-zero", "one-bit", "63-bits", "all-ones"],
)
def test_the_comparable_hash_bounds_are_exclusive_at_both_ends(bits, comparable):
    """`return 0 < bits < 64` survived `0 -> 1` and `64 -> 65`.

    A hash of all zeros or all ones is degenerate -- a red square and a blue
    square hash identically -- so both ends are refused. But ONE set bit is
    real signal and must be accepted, and under `1 < bits` it is not; 63 bits
    likewise under `bits < 65`... which accepts all-ones instead, the exact
    degeneracy the function exists to refuse.
    """
    dh = format((1 << bits) - 1, "016x")
    assert corpus.can_compare_dhash(dh) is comparable, (bits, dh)


def test_opencv_availability_is_reported_the_right_way_round(monkeypatch):
    """`return False` / `return True` in `opencv_available` survived being
    swapped.

    Inverted, a machine WITH opencv reports it missing -- so every crop
    lookup returns "install figcite[match] to enable it" on a machine where
    it is installed, and a machine without it tries to import cv2 mid-match.
    """
    assert match.opencv_available() is True, "cv2 imports here, so this must be True"

    import builtins

    real_import = builtins.__import__

    def no_cv2(name, *a, **kw):
        if name == "cv2":
            raise ImportError("no cv2")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_cv2)
    assert match.opencv_available() is False


# ------------------------------------------------- the matched_by labels


@pytest.mark.parametrize("mod", ["deck", "pdfdeck"])
def test_the_matched_by_labels_are_the_ones_both_front_ends_print(
    mod, tmp_path, monkeypatch
):
    """`"embedded-metadata"`, `"manifest-sha256"` and `"no match"` survived in
    BOTH backends.

    These strings are not messages -- they are the value of `matched_by`,
    which the CLI prints in brackets on every audit row and the web UI keys
    its badge on. They also distinguish HOW a figure was identified, which is
    the difference between provenance carried in the file and provenance
    inferred from a hash.
    """
    import importlib

    m = importlib.import_module(f"figcite.{mod}")

    # A manifest per test. Both matchers fall back to `store.find_similar`
    # over the GLOBAL store no matter what manifest is passed, and conftest
    # gives the whole session one FIGCITE_HOME -- so the blank query image
    # perceptually matched a record another test had filed, and the "no
    # match" assertion failed ONLY when the full suite ran.
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "m.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)

    rec = Record(
        doi="10.1/x",
        citation="C",
        short_cite="S",
        confirmed=True,
        source_kind="pdf-crop",
    )
    src = tmp_path / f"raw-{mod}.png"
    Image.new("RGB", (40, 30), (7, 90, 200)).save(src)
    tagged = tmp_path / f"t-{mod}.png"
    filed = provenance.embed(src, tagged, rec)
    blob = tagged.read_bytes()

    def call(b, manifest):
        if mod == "deck":
            shape = type("_S", (), {"image": type("_I", (), {"blob": b})()})()
            return m.match_picture(shape, manifest)
        return m.match_image(b, manifest)

    assert call(blob, {})[1] == "embedded-metadata"

    plain = tmp_path / f"p-{mod}.png"
    Image.open(src).save(plain)
    pb = plain.read_bytes()
    assert call(pb, {filed.sha256: rec})[1] != "manifest-sha256", (
        "a re-saved copy should not match by sha"
    )

    from figcite.provenance import sha256_bytes

    assert call(pb, {sha256_bytes(pb): rec})[1] == "manifest-sha256"

    blank = io.BytesIO()
    Image.new("RGB", (40, 30), "white").save(blank, "PNG")
    got, how = call(blank.getvalue(), {})
    assert got is None and "no match" in how, (got, how)
