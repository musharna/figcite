"""Regression tests for the autostart launcher.

Two of these pin bugs that shipped in the first draft and were caught only by
running the thing on real Windows. Both were silent: one produced a launcher
that could never start, the other an observable that could never report failure.
"""

from __future__ import annotations

import re

import pytest

from figcite import autostart


# ------------------------------------------------------- VBS string escaping


def test_vbs_quote_doubles_embedded_quotes():
    assert autostart.vbs_quote('a"b') == '"a""b"'
    assert autostart.vbs_quote("plain") == '"plain"'


def test_vbs_run_statement_is_not_an_empty_string_literal(monkeypatch):
    """The original bug: `sh.Run ""C:\\...` -- an empty literal, then garbage.

    VBScript has no escape character, so a command wrapped as `"{cmd}"` where cmd
    itself starts with a quote yields `""C:\\...`, which the host parses as the
    empty string followed by a syntax error. The launcher never ran, and nothing
    surfaced the failure because nothing ever executed it.
    """
    src = _fake_vbs(monkeypatch)
    run = [ln for ln in src.splitlines() if ln.strip().startswith("sh.Run")]
    assert len(run) == 1, "expected exactly one Run statement"
    stmt = run[0].strip()
    assert not stmt.startswith('sh.Run ""C:'), (
        "Run argument opens with an empty string literal -- this is the "
        "unparseable form that never launched"
    )
    # The executable path must arrive quoted, as a doubled quote pair.
    assert 'sh.Run """C:\\Windows\\System32\\wsl.exe""' in stmt


def test_vbs_quotes_are_balanced_when_doubled(monkeypatch):
    """Every quote in a VBS literal must be part of an even-length run."""
    src = _fake_vbs(monkeypatch)
    stmt = [ln for ln in src.splitlines() if ln.strip().startswith("sh.Run")][0]
    m = re.match(r'\s*sh\.Run\s+(".*")\s*,\s*0\s*,\s*True\s*$', stmt)
    assert m, f"could not parse the Run statement: {stmt!r}"
    inner = m.group(1)[1:-1]
    for run in re.findall(r'"+', inner):
        assert len(run) % 2 == 0, f"odd-length quote run {run!r} would not parse"


def test_vbs_restarts_the_watcher(monkeypatch):
    """The watcher has a wall-clock deadline; running it once is not enough."""
    src = _fake_vbs(monkeypatch)
    assert re.search(r"^Do\b", src, re.M), "no restart loop"
    assert re.search(r"^Loop\b", src, re.M), "loop is never closed"
    assert "bWaitOnReturn" not in src  # positional True, not a named arg
    assert ", 0, True" in src, "must run hidden (0) and block (True)"


def test_vbs_backs_off_when_the_watcher_dies_immediately(monkeypatch):
    """Without this, a misconfigured launcher is a hot spin, not a retry."""
    src = _fake_vbs(monkeypatch)
    assert f"elapsed < {autostart.TOO_FAST_SECONDS}" in src
    assert f"WScript.Sleep {autostart.BACKOFF_SECONDS * 1000}" in src
    assert f"WScript.Sleep {autostart.RESTART_SECONDS * 1000}" in src


def test_vbs_carries_the_watch_deadline(monkeypatch):
    src = _fake_vbs(monkeypatch, hours=3.5)
    assert "watch --hours 3.5" in src


# ------------------------------------------------ the probe must not self-match


def test_process_probes_exclude_the_probing_process():
    """The bug this pins, measured on Windows:

    a PowerShell process filtering command lines for 'watch_clipboard.ps1'
    carries that literal in its own command line, so it matched itself. With
    zero watchers alive the naive filter returned 1 -- `status` reported
    WATCHING when nothing was, and `uninstall` killed itself partway through.

    No cleverer pattern fixes this: any pattern specific enough to exclude the
    probe appears verbatim inside the probe that carries it. Only the pid can.
    """
    for name, script in (
        ("watchers", autostart._MATCH_WATCHERS),
        ("supervisor", autostart._MATCH_SUPERVISOR),
    ):
        assert "$_.ProcessId -ne $PID" in script, (
            f"{name} probe can match itself; it cannot report 'nothing running'"
        )


def test_stop_kills_supervisor_before_watcher():
    """Reverse that order and the restart loop revives the watcher you killed."""
    import inspect

    src = inspect.getsource(autostart.stop)
    sup = src.index("_MATCH_SUPERVISOR")
    wat = src.index("_MATCH_WATCHERS")
    assert sup < wat, "supervisor must be stopped first or the kill is undone"


def test_uninstall_delegates_to_stop():
    """Removing the launcher without stopping it leaves an orphan watcher."""
    import inspect

    assert "stop()" in inspect.getsource(autostart.uninstall)


def test_stop_is_available_without_uninstalling():
    """Anything driving the clipboard must be able to pause production.

    Staging dirs can be isolated; the Windows clipboard cannot -- it is one
    global object, so a live test that copies an image is seen by every watcher
    on the machine. Seven test bitmaps reached a real manifest before this
    existed, so `stop` has to be callable without destroying the install.
    """
    assert callable(autostart.stop)
    import inspect

    assert "installed_path" not in inspect.getsource(autostart.stop), (
        "stop() must not touch the installed launcher"
    )


def test_enumerate_raises_rather_than_reporting_nothing_running():
    """A failed probe must not be indistinguishable from a true negative."""
    import inspect

    src = inspect.getsource(autostart._enumerate)
    assert "PROBE_OK" in src
    assert "raise RuntimeError" in src
    assert "UNKNOWN" in src


# --------------------------------------------------------------- CLI wiring


def test_every_subcommand_resolves_to_a_handler():
    """Pins the gap that let a CLI wiring break slip past the whole suite.

    argparse fails at call time, not import time, so a subparser with no
    `func` default only explodes when a user runs it.
    """
    from figcite.cli import build_parser

    p = build_parser()
    (sub,) = [
        a for a in p._actions if isinstance(a, __import__("argparse")._SubParsersAction)
    ]
    assert sub.choices, "no subcommands registered"
    for name, parser in sub.choices.items():
        nested = [
            a
            for a in parser._actions
            if isinstance(a, __import__("argparse")._SubParsersAction)
        ]
        if nested:
            for sname, sparser in nested[0].choices.items():
                assert sparser.get_default("func") is not None, (
                    f"`figcite {name} {sname}` has no handler"
                )
        else:
            assert parser.get_default("func") is not None, (
                f"`figcite {name}` has no handler"
            )


def test_autostart_subcommands_are_registered():
    from figcite.cli import build_parser

    p = build_parser()
    (sub,) = [
        a for a in p._actions if isinstance(a, __import__("argparse")._SubParsersAction)
    ]
    assert "autostart" in sub.choices
    inner = [
        a
        for a in sub.choices["autostart"]._actions
        if isinstance(a, __import__("argparse")._SubParsersAction)
    ][0]
    assert set(inner.choices) == {"install", "status", "uninstall"}


def test_autostart_requires_an_action():
    """Bare `figcite autostart` must not crash with an AttributeError."""
    from figcite.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["autostart"])


# ------------------------------------------------------------------ helpers


def _fake_vbs(monkeypatch, hours: float = 24.0) -> str:
    """vbs_source() without touching Windows or requiring the launcher on disk."""
    monkeypatch.setattr(autostart, "_launcher", lambda: "/home/u/.local/bin/figcite")
    monkeypatch.setattr(autostart, "_distro", lambda: "Ubuntu")
    monkeypatch.setattr(autostart, "wsl_to_win", lambda p: r"C:\fake\watch.log")
    return autostart.vbs_source(hours)
