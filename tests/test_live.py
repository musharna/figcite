"""Real-execution checks. No mocks: live CrossRef, a real paper PDF, the real
Windows clipboard. Run with `pytest -m live`.

These are the tests that can catch a broken system boundary. The synthetic
tests elsewhere cannot -- they only prove the code is self-consistent.
"""

import json
import subprocess
import time
from pathlib import Path

import os

import pytest

from figcite.crossref import record_from_doi, search_bibliographic
from figcite.pdfgrab import crop, discover_doi
from figcite.provenance import read_embedded

# `live` for the module -- every test here crosses a real system boundary. But
# only ONE of them mutates the machine the human is sitting at, and blanket
# marking the others `desktop_destructive` would make the marker mean "live",
# which is the conflation the marker was introduced to undo. The narrow mark
# goes on the test that earns it.
pytestmark = [pytest.mark.live]

# A real, DOI-bearing PDF on the machine running the live suite. Machine-local
# by nature, so it comes from the environment rather than a path in the repo;
# the tests that use it skip when it is unset or absent.
REAL_PDF = os.environ.get("FIGCITE_LIVE_PDF", "")
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
    assert top["doi"] != "10.1038/s41586-020-2649-2" or top["type"] != "journal-article", (
        "top hit is now the real paper; re-examine the auto-confirm policy"
    )


def test_real_pdf_crop_end_to_end(tmp_path):
    if not REAL_PDF or not Path(REAL_PDF).exists():
        pytest.skip(f"set FIGCITE_LIVE_PDF to a real DOI-bearing PDF (got {REAL_PDF!r})")
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
@pytest.mark.desktop_destructive
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
            "-MaxHours",
            "0.02",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    created = []
    try:
        time.sleep(8)  # warm-up, then long enough that a stale capture would have landed
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
        assert pending["inference"]["doi_evidence"], "inference gave no reason for the gap"
    finally:
        proc.terminate()
        for name in created:
            stem = str(wsl_dir / name)[:-4]
            for suffix in (".png", ".capture.json", ".pending.json"):
                Path(stem + suffix).unlink(missing_ok=True)
        # Put production back the way we found it.
        if was_installed:
            autostart.start()


def test_an_idle_watcher_costs_nothing(tmp_path, monkeypatch):
    """The watcher used to ask the clipboard 75 times a minute whether anything
    had changed, and every ask OPENS the clipboard, so no other application can
    while it is open. It is woken by WM_CLIPBOARDUPDATE now and sleeps between
    changes, which is only worth anything if idle really is free.

    Measured over 12 seconds of an untouched clipboard: the polling version
    burned ~100ms of CPU, the event-driven version burns 0.

    Deliberately NOT `desktop_destructive`: this starts a watcher on a private
    staging directory and then watches it do nothing. It reads the clipboard
    once at startup to prime its hash and never writes it, so nothing the human
    owns is touched.
    """
    from figcite.clipboard import PS1, wsl_to_win

    private = tmp_path / "idle-staging"
    private.mkdir(parents=True, exist_ok=True)
    win_dir = wsl_to_win(private)

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
            "-MaxHours",
            "0.02",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    def cpu_ms(win_pid):
        """CPU consumed so far, asked of Windows in 100-nanosecond ticks."""
        r = subprocess.run(
            [
                PS_EXE,
                "-NoProfile",
                "-Command",
                f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={win_pid}';"
                "if ($p) { ($p.KernelModeTime + $p.UserModeTime) } else { 'GONE' }",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        out = r.stdout.strip()
        if out == "GONE" or not out:
            return None
        return int(out) / 10_000.0

    try:
        # The Popen pid is the WSL-side interop stub, not the Windows process.
        # The private staging directory appears verbatim in the real process's
        # command line, so ask Windows for the one that has it.
        win_pid = None
        deadline = time.time() + 30
        while time.time() < deadline and win_pid is None:
            r = subprocess.run(
                [
                    PS_EXE,
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" |"
                    f" Where-Object {{ $_.CommandLine -like '*{private.name}*' }} |"
                    " Select-Object -First 1 -ExpandProperty ProcessId",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if r.stdout.strip().isdigit():
                win_pid = int(r.stdout.strip())
            else:
                time.sleep(1)
        assert win_pid, "the watcher process never appeared in the Windows process table"

        time.sleep(4)  # let startup -- Add-Type compiles C# -- finish and settle
        before = cpu_ms(win_pid)
        assert before is not None, "the watcher died during warm-up"
        time.sleep(12)  # twelve seconds of an untouched clipboard
        after = cpu_ms(win_pid)

        # Positive control FIRST: a watcher that has exited also burns no CPU,
        # and would sail through the assertion below. "Asleep" and "dead" are
        # two states and this test has to tell them apart.
        assert after is not None, (
            "the watcher exited during the idle window; 0ms of CPU would then mean nothing at all"
        )
        assert before > 0, (
            "the watcher reports zero CPU even for its own startup, so this "
            "measurement is not measuring anything"
        )

        burned = after - before
        assert burned < 30, (
            f"an idle watcher burned {burned:.0f}ms of CPU over 12 seconds; the "
            f"800ms poll loop this replaced burned about 100ms, so something is "
            f"asking the clipboard on a timer again"
        )
    finally:
        proc.terminate()
