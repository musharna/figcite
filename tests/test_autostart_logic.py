"""autostart's branching, with PowerShell supplied rather than forbidden.

Backlog item 6 recorded autostart as an ACCEPTED GAP: everything in it goes
through Windows interop, the unit suite blocks interop, so eighteen mutants
sat here untested. But every call goes through ONE helper -- `_ps` -- and the
PowerShell *text* is not what the mutants change. They change how its OUTPUT
is interpreted: whether a probe that failed is read as "nothing is running",
whether a launcher already running is started a second time, whether a
returncode is checked at all.

That is ordinary logic and it is testable by supplying `_ps`'s result. The
live tests keep doing what only they can: proving the scripts themselves work
on Windows.

The gap was never really about Windows. It was about a boundary that had been
declared untestable and then not re-examined.
"""

from __future__ import annotations

import subprocess

import pytest

from figcite import autostart, store
from figcite.provenance import Record


def _cp(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["powershell"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _ps_returns(monkeypatch, result):
    seen = []

    def fake(script, timeout=120):
        seen.append(script)
        return result(script) if callable(result) else result

    monkeypatch.setattr(autostart, "_ps", fake)
    return seen


# ------------------------------------------------- locating the Startup dir


def test_a_failed_startup_lookup_raises_rather_than_returning_nothing(monkeypatch):
    """`if r.returncode != 0 or not path` survived four mutants.

    Two independent ways for the lookup to be useless -- the command failed,
    or it succeeded and printed nothing -- and each must raise. Under `and`
    both must hold at once, so a command that fails while printing something
    to stdout returns that something as the Startup folder, and the launcher
    is written to a path Windows never named.

    Each case makes exactly ONE operand true.
    """
    _ps_returns(monkeypatch, _cp(stdout=r"C:\some\junk", returncode=1))
    with pytest.raises(RuntimeError, match="could not locate the Startup folder"):
        autostart._startup_dir_win()

    _ps_returns(monkeypatch, _cp(stdout="   \n", returncode=0))
    with pytest.raises(RuntimeError, match="could not locate the Startup folder"):
        autostart._startup_dir_win()


def test_a_successful_startup_lookup_returns_the_path(monkeypatch):
    """Positive control: "always raise" passes both cases above and autostart
    can never be installed at all."""
    _ps_returns(monkeypatch, _cp(stdout="C:\\Users\\me\\Startup\n", returncode=0))

    assert autostart._startup_dir_win() == r"C:\Users\me\Startup"


def test_an_unreadable_userprofile_raises(monkeypatch):
    """`if not up` survived dropping its `not`. Inverted, a machine where the
    profile IS readable raises, and one where it is not builds a path out of
    None."""
    monkeypatch.setattr(autostart, "_win_userprofile", lambda: None)
    with pytest.raises(RuntimeError, match="USERPROFILE"):
        autostart._win_figcite_dir()

    monkeypatch.setattr(autostart, "_win_userprofile", lambda: "C:\\Users\\me\\")
    assert autostart._win_figcite_dir() == r"C:\Users\me\.figcite"


# ------------------------------------------------- enumerating processes


def test_a_probe_that_did_not_complete_is_UNKNOWN_not_empty(monkeypatch):
    """`if "PROBE_OK" not in r.stdout` survived `NotIn -> In` and mutating the
    sentinel.

    This is the module's central rule and it had no behavioural test at all --
    the existing one reads `inspect.getsource` and asserts the strings
    "PROBE_OK", "raise RuntimeError" and "UNKNOWN} appear in it. That cannot
    fail for the reason it names: `NotIn -> In` leaves every one of those
    substrings in place.

    Under the mutant a probe that never ran returns an EMPTY LIST, which the
    caller renders as "NOT WATCHING -- no clipboard watcher process is
    running". A failed measurement reported as a negative observation is the
    same error this whole tool exists to prevent, one layer down.
    """
    _ps_returns(monkeypatch, _cp(stdout="", stderr="access denied", returncode=1))

    with pytest.raises(RuntimeError, match="UNKNOWN"):
        autostart.watcher_processes()


def test_a_completed_probe_with_no_processes_is_a_real_empty_list(monkeypatch):
    """The positive control, and the distinction that matters: the probe RAN
    and found nothing. That is a negative observation, and it is allowed."""
    _ps_returns(monkeypatch, _cp(stdout="PROBE_OK\n", returncode=0))

    assert autostart.watcher_processes() == []


def test_process_lines_are_parsed_into_pid_and_start_time(monkeypatch):
    """`if line.startswith("PROC ")` survived mutating the prefix, and
    `parts[2] if len(parts) > 2 else ""` survived `2 -> 3` and `Gt -> GtE`.

    Under a mutated prefix every line is skipped and a running watcher
    reports as none. Under `len(parts) > 3` a normal three-field line loses
    its start time, so `figcite autostart status` prints a pid with no "since"
    -- and under `>= 2` a two-field line reaches `parts[2]` and raises
    IndexError out of a status command.
    """
    _ps_returns(
        monkeypatch,
        _cp(stdout="PROC 4242 20260823101500\nnoise\nPROC 4243\nPROBE_OK\n"),
    )

    got = autostart.watcher_processes()

    assert got == [
        {"pid": "4242", "started": "20260823101500"},
        {"pid": "4243", "started": ""},
    ], got


# ------------------------------------------------- starting and stopping


def test_install_does_not_start_a_second_supervisor(monkeypatch, tmp_path):
    """`if start_now and not supervisor_processes()` survived `And -> Or` and
    dropping the `not`.

    Under `or` a supervisor is started whenever EITHER holds -- so
    `--no-start` starts one anyway, and an install run while one is already
    running starts a second. The module's own comment records what happens
    next: three accumulated, raced on identical capture filenames, and
    destroyed a real capture.
    """
    started = []

    def fake_ps(script, timeout=120):
        if "Start-Process" in script:
            started.append(script)
            return _cp(stdout="STARTED\n")
        return _cp(stdout="PROBE_OK\n")

    monkeypatch.setattr(autostart, "_ps", fake_ps)
    monkeypatch.setattr(autostart, "_win_figcite_dir", lambda: str(tmp_path))
    monkeypatch.setattr(autostart, "_startup_dir_win", lambda: str(tmp_path))
    # win_to_wsl must yield a FILE path -- `install` writes the launcher to it.
    monkeypatch.setattr(
        autostart, "win_to_wsl", lambda p: str(tmp_path / "figcite.vbs")
    )
    monkeypatch.setattr(autostart, "_log_path", lambda: tmp_path / "watch.log")
    monkeypatch.setattr(autostart, "supervisor_processes", lambda: [{"pid": "1"}])

    res = autostart.install(hours=1.0, start_now=True)

    assert not started, "a second supervisor was started while one was already running"
    assert res["started"] is False, res


def test_install_starts_one_when_none_is_running(monkeypatch, tmp_path):
    """Positive control: "never start" passes the test above and autostart
    never actually starts anything."""
    started = []

    def fake_ps(script, timeout=120):
        if "Start-Process" in script:
            started.append(script)
            return _cp(stdout="STARTED\n")
        return _cp(stdout="PROBE_OK\n")

    monkeypatch.setattr(autostart, "_ps", fake_ps)
    monkeypatch.setattr(autostart, "_win_figcite_dir", lambda: str(tmp_path))
    monkeypatch.setattr(autostart, "_startup_dir_win", lambda: str(tmp_path))
    monkeypatch.setattr(
        autostart, "win_to_wsl", lambda p: str(tmp_path / "figcite.vbs")
    )
    monkeypatch.setattr(autostart, "_log_path", lambda: tmp_path / "watch.log")
    monkeypatch.setattr(autostart, "supervisor_processes", lambda: [])

    res = autostart.install(hours=1.0, start_now=True)

    assert started, "no supervisor was started on a machine running none"
    assert res["started"] is True, res


def test_start_refuses_when_a_watcher_is_running_even_with_no_supervisor(
    monkeypatch, tmp_path
):
    """`if supervisor_processes() or watcher_processes()` survived `Or -> And`.

    A watcher can OUTLIVE the supervisor that spawned it -- the comment above
    the line records that guarding on supervisors alone let three accumulate
    and destroy a capture. Under `and` both must be running, so the exact
    state the comment describes (a watcher with no supervisor) passes the
    guard and a second one is started.
    """
    started = []

    def fake_ps(script, timeout=120):
        started.append(script)
        return _cp(stdout="STARTED\n")

    monkeypatch.setattr(autostart, "_ps", fake_ps)
    monkeypatch.setattr(autostart, "installed_path", lambda: tmp_path / "f.vbs")
    monkeypatch.setattr(autostart, "supervisor_processes", lambda: [])
    monkeypatch.setattr(autostart, "watcher_processes", lambda: [{"pid": "77"}])

    res = autostart.start()

    assert res.get("already_running") is True, res
    assert not started, "a second watcher was started alongside a live one"


def test_start_refuses_when_not_installed(monkeypatch):
    """`if p is None` survived `Is -> IsNot`. Inverted, an INSTALLED launcher
    is reported as not installed, and a missing one is started -- from a path
    that is None."""
    monkeypatch.setattr(autostart, "installed_path", lambda: None)

    with pytest.raises(RuntimeError, match="not installed"):
        autostart.start()


def test_uninstall_removes_the_launcher_only_when_there_is_one(monkeypatch, tmp_path):
    """`if p is not None` survived `IsNot -> Is`. Inverted, uninstalling on a
    machine with no launcher calls `.unlink()` on None."""
    monkeypatch.setattr(autostart, "stop", lambda: {"ok": True, "stderr": ""})

    vbs = tmp_path / "figcite.vbs"
    vbs.write_text("' launcher")
    monkeypatch.setattr(autostart, "installed_path", lambda: vbs)
    assert autostart.uninstall()["removed_launcher"] is True
    assert not vbs.exists()

    monkeypatch.setattr(autostart, "installed_path", lambda: None)
    assert autostart.uninstall()["removed_launcher"] is False


# ------------------------------------------------- the status counts


def test_the_session_count_counts_session_banners(monkeypatch, tmp_path):
    """`line.startswith("=== figcite watch session")` survived mutating the
    banner.

    Under the mutant the count is always zero, so `autostart status` reports
    "0 session(s)" beside a log full of them -- which reads as "the watcher
    has never run" on a machine where it has run for weeks.
    """
    log = tmp_path / "watch.log"
    log.write_text(
        "=== figcite watch session 2026-08-20 ===\n"
        "  captured something\n"
        "=== figcite watch session 2026-08-21 ===\n"
        "  captured something else\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(autostart, "_log_path", lambda: log)
    monkeypatch.setattr(autostart, "installed_path", lambda: None)
    monkeypatch.setattr(autostart, "watcher_processes", lambda: [])
    monkeypatch.setattr(autostart, "supervisor_processes", lambda: [])
    # `status` imports staging_dirs from .clipboard at call time, so the
    # patch has to land on the clipboard module, not on autostart.
    from figcite import clipboard

    monkeypatch.setattr(
        clipboard, "staging_dirs", lambda: ("C:\\st", tmp_path / "staging")
    )

    st = autostart.status()

    assert st["sessions"] == 2, st["sessions"]
    assert st["log_tail"], "the log tail was not read"


def test_the_filed_count_counts_clipboard_captures_only(monkeypatch, tmp_path):
    """`if r.source_kind == "clipboard"` survived `Eq -> NotEq` and mutating
    the value.

    The number is "clipboard capture(s) in the manifest" -- the watcher's own
    output. Under `!=` it becomes every OTHER kind of record, so a library
    full of PDF crops reports a busy watcher on a machine where the watcher
    has never captured anything.

    Asymmetric: two clipboard records and three of other kinds, so no mutant
    can produce the same number by accident.
    """
    recs = {}
    for i, kind in enumerate(
        ["clipboard", "clipboard", "pdf-crop", "generated", "download"]
    ):
        r = Record(sha256=str(i) * 64, dhash="0" * 16, source_kind=kind, confirmed=True)
        recs[r.sha256] = r
    monkeypatch.setattr(store, "all_records", lambda: recs)
    monkeypatch.setattr(autostart, "_log_path", lambda: tmp_path / "none.log")
    monkeypatch.setattr(autostart, "installed_path", lambda: None)
    monkeypatch.setattr(autostart, "watcher_processes", lambda: [])
    monkeypatch.setattr(autostart, "supervisor_processes", lambda: [])
    # `status` imports staging_dirs from .clipboard at call time, so the
    # patch has to land on the clipboard module, not on autostart.
    from figcite import clipboard

    monkeypatch.setattr(
        clipboard, "staging_dirs", lambda: ("C:\\st", tmp_path / "staging")
    )

    st = autostart.status()

    assert st["clipboard_records"] == 2, (
        f"expected the two clipboard captures; 3 would mean the filter is "
        f"inverted: {st['clipboard_records']}"
    )


def test_status_reports_watching_when_a_watcher_process_exists(monkeypatch, tmp_path):
    """Both direct `status()` tests above hand it an EMPTY process list.

    `"watching": len(procs) > 0` is therefore only ever evaluated at zero,
    where `> 0`, `!= 0` and a hard False all agree -- so the reduced-ROR
    mutant that replaces it with False survived, and `status` would report
    NOT WATCHING with a watcher running. A wrapper polling this would restart
    a watcher that was already there, and the mutex in the PowerShell script
    would refuse it: a supervisor loop failing forever, reported as healthy.

    (`> 0` -> `!= 0` is equivalent and correct: a length is never negative.)
    """
    monkeypatch.setattr(autostart, "installed_path", lambda: None)
    monkeypatch.setattr(
        autostart,
        "watcher_processes",
        lambda: [{"pid": "4242", "started": "08/27/2026 09:00:00"}],
    )
    monkeypatch.setattr(autostart, "supervisor_processes", lambda: [])
    from figcite import clipboard

    monkeypatch.setattr(
        clipboard, "staging_dirs", lambda: ("C:\\st", tmp_path / "staging")
    )

    st = autostart.status()

    assert st["watching"] is True, st
    assert st["processes"] and st["processes"][0]["pid"] == "4242", st


def test_status_is_not_watching_with_no_processes(monkeypatch, tmp_path):
    """The other half, so "always watching" cannot satisfy the test above."""
    monkeypatch.setattr(autostart, "installed_path", lambda: None)
    monkeypatch.setattr(autostart, "watcher_processes", lambda: [])
    monkeypatch.setattr(autostart, "supervisor_processes", lambda: [])
    from figcite import clipboard

    monkeypatch.setattr(
        clipboard, "staging_dirs", lambda: ("C:\\st", tmp_path / "staging")
    )

    assert autostart.status()["watching"] is False


def test_a_startup_lookup_killed_midway_is_not_a_path(monkeypatch):
    """`if r.returncode != 0 or not path:` -- narrowed to `> 0`, a process
    killed by a signal reports a NEGATIVE code and slips the first operand.

    The second operand usually catches it, because a killed child writes
    nothing. Usually. A process killed AFTER writing some output leaves a
    partial line on stdout, and then neither operand fires and half a path is
    returned as the Startup folder.
    """
    import subprocess

    def killed_after_writing(*a, **kw):
        return subprocess.CompletedProcess(
            args=a[0] if a else [], returncode=-15, stdout="C:\\Users\\part", stderr=""
        )

    monkeypatch.setattr(autostart, "_ps", lambda script, timeout=120: killed_after_writing())

    with pytest.raises(RuntimeError):
        autostart._startup_dir_win()
