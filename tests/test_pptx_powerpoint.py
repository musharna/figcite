"""The .pptx path, driven against Microsoft PowerPoint itself.

Every other deck test writes with python-pptx and reads back with python-pptx.
That is the same shape that hid the Ghostscript soft-mask bug: a library
agreeing with itself proves nothing about the application people actually open
the file in. PowerPoint is the reader that matters, and it is the one that
silently "repairs" a package it dislikes.

Three directions, because they can fail independently:

  1. PowerPoint WRITES  -> figcite reads it        (does audit find the images?)
  2. figcite WRITES     -> PowerPoint reads it     (does it open without repair,
                                                    and survive alt-text/captions?)
  3. PowerPoint EDITS AND RESAVES figcite's output (does provenance survive a
                                                    full package rewrite?)

Needs Windows PowerPoint via COM, so it is skipped everywhere else.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from figcite import store
from figcite.clipboard import PS_EXE, _win_userprofile, win_to_wsl
from figcite.provenance import Record, now_stamps

pytestmark = [pytest.mark.live, pytest.mark.desktop_destructive]


def _pwsh(command: str, timeout: int = 90) -> tuple[bool, str]:
    """(the command ran, its stdout). NOT just stdout, and that is the point.

    The first version returned `""` on every failure, so a timeout, a missing
    executable or a permission error was indistinguishable from PowerShell
    successfully reporting nothing. `_powerpoint_already_running` then read
    that silence as "not running" and authorised COM automation.

    A safety check that reads its own outage as an all-clear is the exact
    failure this repo exists to prevent, and it was written into the guard
    meant to prevent it. Inability to establish danger is not evidence of
    safety.
    """
    if not Path(PS_EXE).exists():
        return False, ""
    try:
        r = subprocess.run(
            [PS_EXE, "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode == 0, r.stdout
    except Exception:
        return False, ""


def _powerpoint_available() -> bool:
    """Is PowerPoint INSTALLED -- asked without starting it.

    This used to answer the question by doing

        New-Object -ComObject PowerPoint.Application ... $a.Quit()

    which is not a probe, it is the destructive operation itself. PowerPoint's
    COM server is SINGLE-INSTANCE: `New-Object` hands back the running
    application if there is one, so the availability check attached to the
    user's PowerPoint and quit it -- closing every open presentation, unsaved
    work included, before a single test had run.

    Installation is a fact about the disk, so it is read off the disk. The
    registry's App Paths key is written by the Office installer and answers
    "is it here" without answering "start it".
    """
    ok, out = _pwsh(
        r"if (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion"
        r"\App Paths\POWERPNT.EXE' -ErrorAction SilentlyContinue) "
        r"{'INSTALLED'} else {'NO'}"
    )
    return ok and out.strip() == "INSTALLED"


def _powerpoint_already_running() -> bool:
    """Is a HUMAN using PowerPoint right now?

    The whole hazard in this file is that COM attaches to a running instance.
    If nothing is running, the instance these tests create is THEIRS, and
    quitting it costs nobody anything. If something is running, it belongs to
    the person at the keyboard and must not be touched.

    `Get-Process` reads the process table. It does not open a COM channel, so
    asking cannot itself cause the thing it is asking about.
    """
    ok, out = _pwsh(
        "if (Get-Process POWERPNT -ErrorAction SilentlyContinue) "
        "{'RUNNING'} else {'NOT-RUNNING'}"
    )
    # EXACT match, not substring. `"NOT-RUNNING" not in out` AUTHORISES COM on
    # the output "RUNNING\nNOT-RUNNING", because the busy sentinel contains the
    # idle one. Only an exact, positive idle report proceeds; busy, garbage,
    # contaminated output, and a probe that did not run are all treated as busy.
    return not (ok and out.strip() == "NOT-RUNNING")


@pytest.fixture(autouse=True)
def _refuse_to_touch_someone_elses_powerpoint():
    """Every test in this module quits PowerPoint. Not if it is not ours.

    These tests are worth having -- python-pptx agreeing with python-pptx says
    nothing about the application people actually open the file in, and
    PowerPoint is the reader that silently "repairs" a package it dislikes.
    The danger was never the COM call, it was the COM call landing on someone
    else's session.

    So the rule is ownership: run only when nothing is running, which makes
    the instance we create ours to quit. This machine is a live workstation,
    and on 2026-08-25 a bare `pytest -q` ran this module nine times while the
    user had a deck open, closing it every time. No crash event, because
    nothing crashed -- the application was asked to exit.

    Outer layer is `pytest.ini`: `-m "not live"` is the default, so these are
    opt-in. This is the inner one, for when someone opts in on the wrong
    machine at the wrong moment. Residual and stated plainly: a human who
    opens PowerPoint AFTER this check passes is still exposed for the length
    of the run, which is why the outer layer is a default and not a habit.
    """
    if _powerpoint_already_running():
        pytest.skip(
            "PowerPoint is already running -- it belongs to whoever is at "
            "the keyboard, and these tests drive it. Close PowerPoint and "
            "re-run, or run them on a machine nobody is using."
        )


# The condition is a STRING on purpose. pytest evaluates a string skipif
# condition at test setup; a bare expression is evaluated when this module is
# imported. Every test here is `live` and gets deselected by the `not live`
# run the pre-push gate and the mutation sweep use -- but the module is still
# IMPORTED during collection, so the eager form launched a PowerShell COM
# probe on every single run of a suite that never executes one of these
# tests. Over a mutation sweep that is one probe per mutant.
#
# Lazy means it runs when a PowerPoint test is actually about to run, which
# is the only time the answer matters.
requires_powerpoint = pytest.mark.skipif(
    "not _powerpoint_available()", reason="Microsoft PowerPoint COM not available"
)


def _ps(script: str, timeout: int = 400) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture
def workdir():
    """A directory BOTH sides can see.

    pytest's tmp_path lives under /tmp, which Windows cannot open, so a COM call
    handed that path fails with a file-not-found that looks like a figcite bug.
    """
    up = _win_userprofile()
    if not up:
        pytest.skip("could not read %USERPROFILE%")
    win = up.rstrip("\\") + r"\AppData\Local\Temp\figcite-pytest"
    wsl = Path(win_to_wsl(win))
    wsl.mkdir(parents=True, exist_ok=True)
    for f in wsl.glob("*"):
        if f.is_file():
            f.unlink()
    yield win, wsl


@pytest.fixture
def figures(workdir):
    """Three registered figures, written where PowerPoint can reach them."""
    import random

    from PIL import Image, ImageDraw

    _win, wsl = workdir
    u, loc = now_stamps()
    out = []
    for i in range(3):
        rng = random.Random(500 + i)
        im = Image.new("RGB", (760, 520), (252, 252, 250))
        d = ImageDraw.Draw(im)
        for _ in range(35):
            x, y = rng.randrange(660), rng.randrange(430)
            d.rectangle(
                [x, y, x + rng.randrange(25, 90), y + rng.randrange(20, 80)],
                fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)),
            )
        p = wsl / f"ppfig{i}.png"
        im.save(p)
        store.register_existing(
            p,
            Record(
                doi=f"10.9999/ppt.{i}",
                citation=f"PowerPoint et al. ({2020 + i}).",
                short_cite=f"PPT{i} {2020 + i}",
                source_kind="pdf-crop",
                captured_utc=u,
                captured_local=loc,
                confirmed=True,
            ),
        )
        out.append(p)
    return out


@requires_powerpoint
def test_figcite_reads_a_deck_that_powerpoint_wrote(workdir, figures):
    """Direction 1. python-pptx writing its own fixtures cannot show this."""
    win, wsl = workdir
    out_win = win + r"\real-powerpoint.pptx"
    r = _ps(
        f"$ppt = New-Object -ComObject PowerPoint.Application; "
        f"$ppt.Visible = -1; $pres = $ppt.Presentations.Add(); $i = 1; "
        f"foreach ($f in (Get-ChildItem '{win}\\ppfig*.png')) {{ "
        f"  $s = $pres.Slides.Add($i, 12); "
        f"  $null = $s.Shapes.AddPicture($f.FullName, 0, -1, 60, 60, 520, 340); $i++ }}; "
        f"$pres.SaveAs('{out_win}'); $pres.Close(); Write-Output DONE"
    )
    assert "DONE" in r.stdout, f"PowerPoint failed to write the deck: {r.stderr[:300]}"

    from figcite.deck import audit

    rep = audit(str(wsl / "real-powerpoint.pptx"))
    assert rep["pictures"] == 3, "figcite could not enumerate PowerPoint's pictures"
    assert rep["tagged"] == 3, f"only {rep['tagged']}/3 recovered from a real deck"


@requires_powerpoint
def test_powerpoint_opens_figcite_output_without_repairing_it(workdir, figures):
    """Direction 2. A package PowerPoint dislikes is silently "repaired", which
    is exactly how alt-text and captions would disappear in the field."""
    win, wsl = workdir
    from figcite.deck import apply

    src = wsl / "src.pptx"
    _make_plain_deck(win, wsl, "src.pptx")
    out = wsl / "cited.pptx"
    rep = apply(str(src), str(out), caption_own_work=True, manifest_path=None)
    assert rep["cited"] >= 1

    r = _ps(
        f"$ppt = New-Object -ComObject PowerPoint.Application; $ppt.Visible = -1; "
        f"$pres = $ppt.Presentations.Open('{win}\\cited.pptx', -1, 0, -1); "
        f"Write-Output ('SLIDES=' + $pres.Slides.Count); "
        f"foreach ($s in $pres.Slides) {{ foreach ($sh in $s.Shapes) {{ "
        f"  if ($sh.Type -eq 13) {{ Write-Output ('ALT=' + $sh.AlternativeText) }}; "
        f"  Write-Output ('NAME=' + $sh.Name) }} }}; "
        f"$pres.Close(); Write-Output OPENED_CLEAN"
    )
    assert "OPENED_CLEAN" in r.stdout, (
        f"PowerPoint could not open figcite's output: {r.stderr[:400]}"
    )
    assert "SLIDES=4" in r.stdout, "the credits slide did not survive"

    # Assert on the CONTENT of the alt-text, never on its presence.
    #
    # python-pptx's add_picture pre-fills descr with the FILENAME -- measured,
    # 'ppfig0.png'. So "this shape has alt-text" is already true before figcite
    # touches it, and an assertion that merely counts non-empty alt-text passes
    # with set_alt_text stubbed out to `return`. That mutant survived exactly
    # this test until the assertion below started looking for the DOI.
    alts = [ln[4:] for ln in r.stdout.splitlines() if ln.startswith("ALT=")]
    assert len(alts) == 3, f"expected 3 pictures, saw {len(alts)}"
    for a in alts:
        assert "10.9999/ppt." in a, f"alt-text carries no DOI: {a!r}"
        assert not a.endswith(".png"), (
            f"alt-text is still python-pptx's filename default, so figcite "
            f"never wrote it: {a!r}"
        )
    assert "figcite-caption" in r.stdout, "captions did not survive"
    assert "figcite-credits-marker" in r.stdout, "credits slide body did not survive"


@requires_powerpoint
def test_powerpoint_opens_a_deck_applied_through_the_web_path(workdir, figures):
    """Spec success criterion 4, task 8. `service.apply()` is the function
    `/api/apply` actually calls -- it wraps `deck.apply` with the web
    route's own guard-rails (out-suffix match, no clobbering an existing
    `out` without `force=True`) before delegating. Every other test in this
    file drives `deck.apply` directly, which proves the CLI's write path
    opens cleanly but says nothing about a deck that went through the
    wrapper the browser's Apply button uses -- that path has never been
    opened by PowerPoint until this test.

    Deviates from the brief's own snippet for this test on two points, both
    load-bearing: the brief passed a bare `tmp_path` (lives under /tmp)
    straight into the Windows-side COM call, which `workdir`'s docstring
    above says fails outright (Windows cannot open a WSL /tmp path); and it
    pointed at `demo/auxin_talk-COPY.pptx`, whose figures only match against
    the REAL library, not this test's isolated FIGCITE_HOME -- so under the
    suite's own manifest that deck would apply with zero citations, proving
    only that an uncited file opens. Reusing this file's own `workdir` +
    `figures` fixtures and `_make_plain_deck` helper keeps the WSL/Windows
    path handling that the other three tests already rely on, and confirms
    a citation actually round-trips through `service.apply()`, not just
    that some file opens.
    """
    win, wsl = workdir
    from figcite import service

    _make_plain_deck(win, wsl, "src.pptx")
    out = wsl / "web-applied.pptx"
    rep = service.apply(
        str(wsl / "src.pptx"), str(out), caption_own_work=True, manifest_path=None
    )
    assert rep["cited"] >= 1

    r = _ps(
        f"$ppt = New-Object -ComObject PowerPoint.Application; $ppt.Visible = -1; "
        f"$pres = $ppt.Presentations.Open('{win}\\web-applied.pptx', -1, 0, -1); "
        f"Write-Output ('SLIDES=' + $pres.Slides.Count); "
        f"$pres.Close(); Write-Output OPENED_CLEAN"
    )
    assert "OPENED_CLEAN" in r.stdout, (
        f"PowerPoint could not open a deck applied through service.apply(): "
        f"{r.stderr[:400]}"
    )
    assert "SLIDES=4" in r.stdout, "the credits slide did not survive"
    assert "repair" not in (r.stdout + r.stderr).lower()


@requires_powerpoint
def test_provenance_survives_a_powerpoint_edit_and_save(workdir, figures):
    """Direction 3. Opening a cited deck, moving something and saving is the
    normal workflow, and it makes PowerPoint rewrite the entire package."""
    win, wsl = workdir
    from figcite.deck import apply, audit

    _make_plain_deck(win, wsl, "src.pptx")
    apply(str(wsl / "src.pptx"), str(wsl / "cited.pptx"), manifest_path=None)

    r = _ps(
        f"$ppt = New-Object -ComObject PowerPoint.Application; $ppt.Visible = -1; "
        f"$pres = $ppt.Presentations.Open('{win}\\cited.pptx', 0, 0, -1); "
        f"$pres.Slides[1].Shapes[1].Left = $pres.Slides[1].Shapes[1].Left + 5; "
        f"$pres.SaveAs('{win}\\resaved.pptx'); $pres.Close(); "
        f"Write-Output RESAVED"
    )
    assert "RESAVED" in r.stdout, f"PowerPoint could not resave: {r.stderr[:300]}"

    rep = audit(str(wsl / "resaved.pptx"))
    assert rep["tagged"] == 3, (
        f"only {rep['tagged']}/3 survived a PowerPoint rewrite "
        f"(matched by: {[x['matched_by'] for x in rep['rows']]})"
    )


def _make_plain_deck(win: str, wsl: Path, name: str) -> Path:
    """A deck with the three figures and nothing else, via python-pptx."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]
    for p in sorted(wsl.glob("ppfig*.png")):
        slide = prs.slides.add_slide(blank)
        slide.shapes.add_picture(str(p), Inches(0.6), Inches(0.6), Inches(5.4))
    prs.save(str(wsl / name))
    return wsl / name
