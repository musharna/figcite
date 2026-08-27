"""A process killed by a signal is not a process that succeeded.

Four places in figcite shell out and read the result the same way:

    r = subprocess.run(...)
    if r.returncode == 0:
        return r.stdout.strip()
    ...safe default...

        clipboard._win_userprofile   ->  None
        clipboard.win_to_wsl         ->  the path unchanged
        mplhook._git_head            ->  None
        cli.cmd_register             ->  git_commit = None

All four survived the reduced-ROR sweep widened to `<=`, and all four survive
for the same reason: every test supplies returncode 0 or a small positive
number, and `<= 0` and `== 0` agree on every non-negative value.

They part company below zero. `subprocess` reports a process terminated by a
signal as the NEGATIVE signal number -- -15 for SIGTERM, -9 for SIGKILL -- so a
child killed by a timeout, by the OOM killer, or by a shutting-down WSL session
returns -15 with an empty stdout. Under the widened comparison that reads as
success, and the empty string becomes the answer:

    an empty USERPROFILE, treated as the user's home
    an empty path, returned in place of the one that could not be translated
    an empty git commit, written into a provenance record as if it were a commit

Which is the failure this project is otherwise careful about everywhere: an
outage recorded as a finding. The safe default exists precisely so that "I
could not ask" stays distinguishable from "the answer is empty".
"""

from __future__ import annotations

import subprocess

import pytest

from figcite import clipboard, mplhook

# SIGTERM and SIGKILL as subprocess reports them.
KILLED = [-15, -9]


def _killed(returncode):
    """A CompletedProcess exactly as subprocess builds one for a killed child:
    the negative signal number, and whatever had been written before it died."""

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(
            args=a[0] if a else kw.get("args", []),
            returncode=returncode,
            stdout="",
            stderr="",
        )

    return fake_run


@pytest.mark.parametrize("rc", KILLED)
def test_a_killed_powershell_does_not_become_an_empty_userprofile(monkeypatch, rc):
    monkeypatch.setattr(clipboard.subprocess, "run", _killed(rc))

    assert clipboard._win_userprofile() is None, (
        "a killed powershell reported an empty USERPROFILE as the answer"
    )


@pytest.mark.parametrize("rc", KILLED)
def test_a_killed_wslpath_returns_the_path_it_was_given(monkeypatch, rc):
    monkeypatch.setattr(clipboard.subprocess, "run", _killed(rc))
    original = r"C:\Users\someone\fig.png"

    assert clipboard.win_to_wsl(original) == original, (
        "a killed wslpath turned a real path into an empty string"
    )


@pytest.mark.parametrize("rc", KILLED)
def test_a_killed_git_is_not_a_commit(monkeypatch, rc, tmp_path):
    monkeypatch.setattr(mplhook.subprocess, "run", _killed(rc))

    assert mplhook._git_head(str(tmp_path)) is None, (
        "a killed `git rev-parse` reported an empty commit as the commit"
    )


@pytest.mark.parametrize("rc", [0])
def test_a_successful_call_is_still_used(monkeypatch, rc, tmp_path):
    """Positive control for all three above.

    Each asserts that something is NOT used, and a function that had stopped
    calling out at all -- or that always returned its default -- would satisfy
    every one of them.
    """

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(
            args=a[0] if a else [], returncode=0, stdout="  VALUE  \n", stderr=""
        )

    monkeypatch.setattr(clipboard.subprocess, "run", fake_run)
    monkeypatch.setattr(mplhook.subprocess, "run", fake_run)

    assert clipboard._win_userprofile() == "VALUE"
    assert clipboard.win_to_wsl(r"C:\x") == "VALUE"
    assert mplhook._git_head(str(tmp_path)) == "VALUE"


def test_negative_returncodes_are_what_subprocess_actually_reports():
    """The premise, run for real rather than asserted from memory.

    The tests above are only about anything if a killed child really does
    surface as a negative returncode. This kills one and looks.
    """
    p = subprocess.run(
        [
            "python3",
            "-c",
            "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert p.returncode < 0, (
        f"a SIGTERMed child reported returncode {p.returncode}; the premise of "
        f"this file does not hold on this platform"
    )
    assert p.returncode == -15, p.returncode
    # And the thing that makes the mutation dangerous rather than merely wrong:
    assert (p.returncode == 0) is False
    assert (p.returncode <= 0) is True


def test_a_killed_git_is_not_recorded_as_the_commit_that_made_a_figure(
    tmp_path, monkeypatch, capsys
):
    """The fourth copy, inline in `cli.cmd_register`.

    `--this-work` records the git commit the figure was produced at, which is
    the whole provenance claim for a figure you made yourself: it says "this
    plot came from this state of this repo". `git_commit = None` means "not
    recorded"; `git_commit = ""` would be a claim about a commit with no name.
    """
    from PIL import Image

    from figcite import cli, store

    img = tmp_path / "myplot.png"
    Image.new("RGB", (20, 15), (10, 10, 60)).save(img)

    monkeypatch.setattr(cli.subprocess, "run", _killed(-15))

    rc = cli.main(["register", str(img), "--this-work", "--cite", "My figure"])
    assert rc == 0, capsys.readouterr()

    recs = [
        r for r in store.all_records().values() if r.source_kind == "generated"
    ]
    assert len(recs) == 1, recs
    commit = recs[0].source_detail.get("git_commit")
    assert commit is None, (
        f"a killed `git rev-parse` was recorded as the producing commit: "
        f"{commit!r}. None means 'not recorded'; '' is a claim about a commit "
        f"with no name."
    )


def test_a_real_git_head_is_still_recorded(tmp_path, monkeypatch, capsys):
    """Positive control. "Never record a commit" satisfies the test above and
    removes the feature."""
    from PIL import Image

    from figcite import cli, store

    img = tmp_path / "myplot2.png"
    Image.new("RGB", (20, 15), (60, 10, 10)).save(img)

    def fake_run(*a, **kw):
        return subprocess.CompletedProcess(
            args=a[0] if a else [], returncode=0, stdout="deadbeef1234\n", stderr=""
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    assert cli.main(["register", str(img), "--this-work", "--cite", "Mine"]) == 0
    recs = [
        r for r in store.all_records().values() if r.source_kind == "generated"
    ]
    assert len(recs) == 1, recs
    assert recs[0].source_detail.get("git_commit") == "deadbeef1234"


def test_no_module_in_this_package_sorts_below_dunder_main():
    """Why `__name__ == "__main__"` narrowed to `<=` is inert.

    Two of those guards survived the sweep, in `figcite/__main__.py` and
    `figcite/cli.py`. `<=` admits anything sorting below "__main__", and no
    module here does: "_" is 0x5F and every module in this package starts with
    a lowercase letter (0x61+), so `figcite.cli > __main__` and the widened
    guard fires on exactly one value, the same one `==` fires on.

    That is an argument about NAMES rather than about the code path, so it is a
    hypothesis rather than a proof -- a module named with a capital or a digit
    would sort below and break it. Cheap to make falsifiable, so: falsifiable.
    """
    import pathlib

    import figcite

    pkg = pathlib.Path(figcite.__file__).parent
    names = sorted(
        f"figcite.{p.stem}" for p in pkg.glob("*.py") if p.stem != "__init__"
    )
    assert names, "found no modules; this probe is broken"

    below = [n for n in names if n <= "__main__"]
    assert not below, (
        f"{below} sorts at or below '__main__', so `__name__ == \"__main__\"` "
        f"narrowed to `<=` would fire for it and the equivalence is stale"
    )
    # Positive control: the literal itself DOES compare equal, so the guard is
    # not vacuous -- "nothing ever satisfies it" would pass the assertion above.
    assert "__main__" <= "__main__"
