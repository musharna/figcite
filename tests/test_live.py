"""Real-execution checks. No mocks: live CrossRef, a real paper PDF, the real
Windows clipboard. Run with `pytest -m live`.

These are the tests that can catch a broken system boundary. The synthetic
tests elsewhere cannot -- they only prove the code is self-consistent.
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from figcite.crossref import record_from_doi, search_bibliographic
from figcite.pdfgrab import crop, discover_doi
from figcite.provenance import read_embedded

pytestmark = pytest.mark.live

REAL_PDF = (
    "/mnt/c/Users/a2b32/Zotero/storage/497VPIMU/"
    "Shiragaki et al. - 2020 - Phylogenetic Analysis and Molecular "
    "Diversity of Capsicum Based on rDNA-ITS Region.pdf"
)
PS_EXE = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"


def test_crossref_live_returns_the_real_byline():
    """The byline comes from CrossRef, never from recall.

    Positive control (a DOI that exists) and negative control (one that does
    not) in one test, so a dead network cannot read as a pass.
    """
    rec = record_from_doi("10.1038/s41586-020-2649-2", confirmed=True)
    assert rec.authors and rec.authors[0].startswith("Harris"), rec.authors[:2]
    assert rec.year == 2020
    assert rec.container == "Nature"
    assert "creativecommons.org/licenses/by" in (rec.license_url or "")
    assert rec.reuse == "reuse-ok-attribution-required"
    assert rec.retracted is False

    with pytest.raises(LookupError):
        record_from_doi("10.9999/this-doi-does-not-exist-figcite", confirmed=True)


def test_crossref_title_search_is_untrustworthy_by_design():
    """Documents WHY title-inferred DOIs are never auto-confirmed.

    Searching the exact title of a very famous paper does not reliably put that
    paper first -- reviews and commentaries outrank it. If this ever starts
    passing trivially, the unconfirmed gate could be revisited.
    """
    hits = search_bibliographic("Array programming with NumPy", rows=5)
    assert hits, "CrossRef search returned nothing at all"
    top = hits[0]
    assert (
        top["doi"] != "10.1038/s41586-020-2649-2" or top["type"] != "journal-article"
    ), "top hit is now the real paper; re-examine the auto-confirm policy"


def test_real_pdf_crop_end_to_end(tmp_path):
    if not Path(REAL_PDF).exists():
        pytest.skip(f"fixture PDF not present: {REAL_PDF}")
    doi, where = discover_doi(REAL_PDF)
    assert doi == "10.3390/horticulturae6040087", (doi, where)
    assert "page" in where

    out = tmp_path / "fig.png"
    detail = crop(REAL_PDF, 2, out, rect=(0.1, 0.1, 0.9, 0.5), frac=True, dpi=150)
    assert out.exists() and out.stat().st_size > 5000, "crop produced an empty image"
    assert detail["pixels"][0] > 200 and detail["pixels"][1] > 100

    # tag it for real, through the CLI, against live CrossRef
    from figcite.cli import main

    tagged = tmp_path / "tagged.png"
    rc = main(
        [
            "grab",
            REAL_PDF,
            "--page",
            "2",
            "--rect",
            "0.1,0.1,0.9,0.5",
            "--frac",
            "--dpi",
            "150",
            "-o",
            str(tagged),
        ]
    )
    assert rc == 0
    rec = read_embedded(tagged.read_bytes())
    assert rec is not None and rec.doi == "10.3390/horticulturae6040087"
    assert "Shiragaki" in rec.citation, rec.citation
    assert rec.confirmed is True, "a DOI read out of the PDF itself should be confirmed"
    assert rec.source_kind == "pdf-crop"
    assert rec.source_detail.get("page") == 2


@pytest.mark.skipif(not Path(PS_EXE).exists(), reason="no Windows PowerShell")
def test_windows_clipboard_watcher_captures_a_real_snip(tmp_path, monkeypatch):
    """Drives the actual watcher against the actual Windows clipboard.

    NOTE: this overwrites the clipboard with a small test bitmap.
    """
    from figcite.clipboard import PS1, staging_dirs, wsl_to_win, enrich

    def set_clipboard(w, h, color):
        subprocess.run(
            [
                PS_EXE,
                "-sta",
                "-NoProfile",
                "-Command",
                "Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
                f"$b = New-Object System.Drawing.Bitmap {w},{h}; "
                "$g = [System.Drawing.Graphics]::FromImage($b); "
                f"$g.Clear([System.Drawing.Color]::{color}); $g.Dispose(); "
                "[System.Windows.Forms.Clipboard]::SetImage($b)",
            ],
            check=True,
            timeout=90,
            capture_output=True,
        )

    def captures_since(before):
        """Completed captures only.

        A capture is TWO writes: watch_clipboard.ps1 writes the .png first and
        the .capture.json after it, and only then prints CAPTURED -- which is
        the signal production actually consumes. Polling for the .png alone
        therefore treats a half-written capture as finished, and the sidecar
        assertion below lands in the gap between the two writes. That is
        load-dependent, so it passed for months in an isolated run and failed
        once the suite grew to 126 tests. Wait for the same marker production
        waits for.
        """
        done = {
            p.name
            for p in wsl_dir.glob("clip-*.png")
            if Path(str(p)[:-4] + ".capture.json").exists()
        }
        return sorted(done - before)

    # Claim a private staging queue. Sharing the default one with an installed
    # autostart watcher means production finalizes this test's snip out of
    # staging before captures_since() can see it -- the test then fails looking
    # like "the watcher never captured", which is not what went wrong.
    # Pause any installed production watcher for the duration.
    #
    # A private staging dir is not enough: the Windows clipboard is ONE global
    # object, so the bitmaps this test copies are visible to every watcher
    # running on the machine. Without this, running the suite files test
    # bitmaps into the user's real provenance manifest -- measured, 7 of them.
    from figcite import autostart

    was_installed = autostart.installed_path() is not None
    autostart.stop()

    private = tmp_path / "staging"
    private.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FIGCITE_STAGING_WIN", wsl_to_win(private))
    win_dir, wsl_dir = staging_dirs()
    wsl_dir.mkdir(parents=True, exist_ok=True)
    before = {p.name for p in wsl_dir.glob("clip-*.png")}

    # An image is on the clipboard BEFORE the watcher starts. Capturing it would
    # stamp it with whatever window is focused now, which is not where it came
    # from -- that bug shipped once and this is its regression test.
    set_clipboard(60, 40, "Teal")

    proc = subprocess.Popen(
        [
            PS_EXE,
            "-sta",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            wsl_to_win(PS1),
            "-StagingDir",
            win_dir,
            "-PollMs",
            "400",
            "-MaxHours",
            "0.02",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    created = []
    try:
        time.sleep(8)  # warm-up + several poll cycles
        stale = captures_since(before)
        assert not stale, (
            f"watcher captured the pre-existing clipboard image {stale} and would "
            "have attributed it to the wrong window"
        )

        # positive control: a genuinely NEW copy must still be caught, otherwise
        # the assertion above would pass on a watcher that simply never works.
        set_clipboard(137, 91, "Firebrick")
        deadline = time.time() + 40
        fresh = []
        while time.time() < deadline:
            fresh = captures_since(before)
            if fresh:
                break
            time.sleep(1)
        assert fresh, "the watcher never captured a genuinely new clipboard image"
        created = fresh

        png = wsl_dir / fresh[-1]
        cap_file = Path(str(png)[:-4] + ".capture.json")
        assert cap_file.exists(), "no capture sidecar was written"
        cap = json.loads(cap_file.read_text(encoding="utf-8-sig"))
        assert cap["width"] == 137 and cap["height"] == 91, cap
        assert cap["process"], "foreground process was not recorded"
        assert cap["captured_local"]

        # inference must run and must NOT invent a DOI for a random bitmap
        pending = enrich(png)
        assert pending["inference"]["doi"] is None
        assert pending["inference"]["doi_evidence"], (
            "inference gave no reason for the gap"
        )
    finally:
        proc.terminate()
        for name in created:
            stem = str(wsl_dir / name)[:-4]
            for suffix in (".png", ".capture.json", ".pending.json"):
                Path(stem + suffix).unlink(missing_ok=True)
        # Put production back the way we found it.
        if was_installed:
            autostart.start()
