"""Keep the clipboard watcher running without anyone remembering to start it.

Capture-time is the only time provenance exists. The retro-match measured this:
a deck built from clipboard snips recovered 1 image out of 40 after the fact,
while decks built from saved plots recovered 15/15. So the watcher is only worth
anything if it is actually running when a snip happens -- which means it cannot
depend on the user having typed `figcite watch` in a terminal that is still open.

**Why the Startup folder and not Task Scheduler.** Task Scheduler was the first
design and it is the wrong one here: `Register-ScheduledTask` and `schtasks.exe`
BOTH return "Access is denied" for a non-elevated user (verified via both APIs,
not assumed), so installing would mean making the user clear a UAC prompt. The
Startup folder needs no privileges at all. What Task Scheduler would have given
us -- a repeating trigger to restart a dead watcher -- is instead a loop inside
the launcher itself, which is fewer moving parts and works the same.

Three things make this survive real use:

  1. The watcher has a self-imposed wall-clock deadline (`--hours`), so a
     launcher that runs it once becomes a watcher that silently stops watching
     partway through the day. The launcher therefore RE-RUNS it in a loop, with
     a backoff so a broken setup degrades to a slow retry rather than a hot spin.

  2. `status` probes the ACTUAL PowerShell process polling the clipboard, not
     whether the launcher file exists on disk. A file sitting in Startup tells
     you an intention, not a fact; asking it whether the clipboard is being
     watched is asking one level above the referent.

  3. Everything the watcher prints goes to a log with a timestamped session
     banner, so "is it working" and "how often does it restart" both have
     readable answers after the fact.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from .clipboard import PS_EXE, _win_userprofile, win_to_wsl, wsl_to_win

VBS_NAME = "figcite-watch.vbs"
WSL_EXE = r"C:\Windows\System32\wsl.exe"

# The watcher's own deadline. The launcher loop heals the gap when it lapses.
DEFAULT_HOURS = 24.0
RESTART_SECONDS = 15
BACKOFF_SECONDS = 60
# A watcher that exits faster than this did not do useful work; back off.
TOO_FAST_SECONDS = 30

# Find the clipboard poller -- and NOT the process doing the finding.
#
# A PowerShell process that greps command lines for 'watch_clipboard.ps1' has
# that string in its OWN command line, so the naive filter matches itself. That
# made `status` report WATCHING with no watcher alive (an observable that cannot
# report failure) and made `uninstall` a script that kills itself partway
# through. Measured: with zero watchers running the naive filter returned 1.
# `-ne $PID` is the fix, and it has to be $PID rather than a smarter pattern:
# any pattern precise enough to exclude the probe still appears verbatim inside
# the probe that carries it.
_MATCH_WATCHERS = """
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
  Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*watch_clipboard.ps1*' }"""

# The supervising wscript host. Filtering on Name='wscript.exe' already excludes
# the powershell probe, but the guard costs nothing and survives a refactor.
_MATCH_SUPERVISOR = f"""
Get-CimInstance Win32_Process -Filter "Name='wscript.exe'" |
  Where-Object {{ $_.ProcessId -ne $PID -and $_.CommandLine -like '*{VBS_NAME}*' }}"""


def _log_path() -> Path:
    from . import store

    return store.DATA_DIR / "watch.log"


def _distro() -> str:
    return os.environ.get("WSL_DISTRO_NAME", "Ubuntu")


def _launcher() -> str:
    """Absolute path to the figcite entry point, as WSL sees it."""
    p = Path.home() / ".local" / "bin" / "figcite"
    if p.exists():
        return str(p)
    raise RuntimeError(
        f"figcite launcher not found at {p}; autostart needs a stable entry point"
    )


def _ps(script: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PS_EXE, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _startup_dir_win() -> str:
    """The per-user Startup folder, asked of Windows rather than hardcoded."""
    r = _ps("[Environment]::GetFolderPath('Startup')")
    path = r.stdout.strip()
    if r.returncode != 0 or not path:
        raise RuntimeError(f"could not locate the Startup folder: {r.stderr.strip()}")
    return path


def _win_figcite_dir() -> str:
    up = _win_userprofile()
    if not up:
        raise RuntimeError("could not read %USERPROFILE% via powershell.exe")
    return up.rstrip("\\") + r"\.figcite"


def vbs_quote(s: str) -> str:
    """Render a Python string as a VBScript string literal.

    VBScript has no escape character; a literal quote is written by doubling it.
    Getting this wrong produced a file whose first statement was `sh.Run ""C:\\...`
    -- an empty string followed by garbage, i.e. a syntax error that would never
    have launched anything.
    """
    return '"' + s.replace('"', '""') + '"'


def vbs_source(hours: float = DEFAULT_HOURS) -> str:
    """A hidden, self-restarting launcher.

    wscript with WindowStyle 0 is the only way to start this without a console
    window flashing on screen. bWaitOnReturn is True so the Run call blocks for
    exactly as long as the watcher lives -- that is what turns the loop into a
    supervisor instead of a fork bomb.
    """
    log = _log_path()
    log_win = wsl_to_win(log)
    inner = (
        f"echo === figcite watch session $(date -Is) === >> {log}; "
        f"exec {_launcher()} watch --hours {hours:g} >> {log} 2>&1"
    )
    cmd = f'"{WSL_EXE}" -d {_distro()} -e /bin/bash -lc "{inner}"'
    return f"""' figcite clipboard watcher -- generated by `figcite autostart install`
' Runs the WSL-side watcher with no visible console window, and restarts it
' whenever it exits (it has its own {hours:g}h deadline, and WSL can be shut down).
' log: {log_win}
' remove with: figcite autostart uninstall
Set sh = CreateObject("WScript.Shell")
Do
  started = Timer
  sh.Run {vbs_quote(cmd)}, 0, True
  elapsed = Timer - started
  If elapsed < {TOO_FAST_SECONDS} And elapsed >= 0 Then
    ' Died immediately -- probably misconfigured. Retry slowly rather than spin.
    WScript.Sleep {BACKOFF_SECONDS * 1000}
  Else
    WScript.Sleep {RESTART_SECONDS * 1000}
  End If
Loop
"""


def installed_path() -> Optional[Path]:
    """WSL view of the installed launcher, or None if it is not installed."""
    try:
        p = Path(win_to_wsl(_startup_dir_win() + "\\" + VBS_NAME))
    except Exception:
        return None
    return p if p.exists() else None


def install(hours: float = DEFAULT_HOURS, start_now: bool = True) -> dict:
    """Drop the launcher into Startup. Idempotent: re-running overwrites it."""
    startup = _startup_dir_win()
    vbs_win = startup + "\\" + VBS_NAME
    vbs_wsl = Path(win_to_wsl(vbs_win))
    vbs_wsl.parent.mkdir(parents=True, exist_ok=True)
    vbs_wsl.write_text(vbs_source(hours), encoding="ascii")

    log = _log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch(exist_ok=True)

    started = False
    if start_now and not supervisor_processes():
        r = _ps(
            "Start-Process wscript.exe -ArgumentList "
            f"'{vbs_quote(vbs_win)}' -WindowStyle Hidden; Write-Output STARTED"
        )
        started = "STARTED" in r.stdout
    return {
        "ok": vbs_wsl.exists(),
        "vbs": vbs_win,
        "log": str(log),
        "hours": hours,
        "started": started,
    }


def stop() -> dict:
    """Stop the supervisor and the watcher, leaving the launcher installed.

    Order matters: the supervisor must die before the watcher, or the restart
    loop simply revives the watcher we just killed.

    Exposed separately from uninstall() because anything driving the clipboard
    for its own purposes has to pause production first. Staging directories can
    be isolated; the Windows clipboard is a single global object, so a live test
    that copies an image WILL be captured by whatever watcher is running. That
    is how seven test bitmaps ended up in a real provenance manifest.
    """
    script = f"""
{_MATCH_SUPERVISOR} |
  ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Start-Sleep -Milliseconds 500
{_MATCH_WATCHERS} |
  ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
Write-Output "STOPPED"
"""
    r = _ps(script)
    return {"ok": "STOPPED" in r.stdout, "stderr": r.stderr.strip()}


def uninstall() -> dict:
    """Remove the launcher and stop everything it started."""
    removed = False
    p = installed_path()
    if p is not None:
        p.unlink()
        removed = True
    r = stop()
    return {
        "ok": r["ok"],
        "removed_launcher": removed,
        "stderr": r["stderr"],
    }


def _enumerate(match: str, what: str) -> list[dict]:
    script = (
        match
        + """ |
  ForEach-Object { Write-Output ("PROC " + $_.ProcessId + " " + $_.CreationDate) }
Write-Output "PROBE_OK"
"""
    )
    r = _ps(script)
    if "PROBE_OK" not in r.stdout:
        raise RuntimeError(
            f"could not enumerate {what}; treat this as UNKNOWN, not as "
            f"'nothing running'. stderr={r.stderr.strip()[:300]}"
        )
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("PROC "):
            parts = line.split(None, 2)
            out.append({"pid": parts[1], "started": parts[2] if len(parts) > 2 else ""})
    return out


def watcher_processes() -> list[dict]:
    """The real observable: PowerShell processes actually polling the clipboard."""
    return _enumerate(_MATCH_WATCHERS, "clipboard watcher processes")


def supervisor_processes() -> list[dict]:
    """The wscript hosts running the restart loop."""
    return _enumerate(_MATCH_SUPERVISOR, "supervisor processes")


def start() -> dict:
    """Start the installed launcher now, without waiting for the next logon."""
    p = installed_path()
    if p is None:
        raise RuntimeError("not installed; run `figcite autostart install` first")
    if supervisor_processes():
        return {"ok": True, "already_running": True}
    vbs_win = _startup_dir_win() + "\\" + VBS_NAME
    r = _ps(
        "Start-Process wscript.exe -ArgumentList "
        f"'{vbs_quote(vbs_win)}' -WindowStyle Hidden; Write-Output STARTED"
    )
    return {
        "ok": "STARTED" in r.stdout,
        "already_running": False,
        "stderr": r.stderr.strip(),
    }


def status() -> dict:
    """Everything needed to answer 'is my clipboard actually being watched'."""
    from . import store
    from .clipboard import staging_dirs

    inst = installed_path()
    procs = watcher_processes()
    sup = supervisor_processes()
    log = _log_path()
    tail, mtime, sessions = [], None, 0
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-12:]
        sessions = sum(
            1 for line in lines if line.startswith("=== figcite watch session")
        )
        mtime = log.stat().st_mtime
    try:
        _, staging = staging_dirs()
        staged = len(list(staging.glob("*.png"))) if staging.exists() else 0
    except Exception:
        staged = -1
    filed = sum(1 for r in store.all_records().values() if r.source_kind == "clipboard")
    return {
        "installed": inst is not None,
        "launcher": str(inst) if inst else None,
        "watching": len(procs) > 0,
        "processes": procs,
        "supervisors": sup,
        "log": str(log),
        "log_mtime": mtime,
        "log_tail": tail,
        "sessions": sessions,
        "staged_pngs": staged,
        "clipboard_records": filed,
    }
