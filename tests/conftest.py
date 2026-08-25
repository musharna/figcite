import ipaddress
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Every test run gets its own manifest so the user's real library is never touched.
_TMP = tempfile.mkdtemp(prefix="figcite-tests-")
os.environ["FIGCITE_HOME"] = _TMP
os.environ.setdefault("FIGCITE_CACHE", str(Path(_TMP) / "crossref-cache"))

# Unit tests must never reach the real Zotero library. Credentials live in the
# environment, so on a machine that exports them a test would silently start
# hitting the network and asserting against 4,824 real items -- passing or
# failing for reasons that have nothing to do with the code under test.
for _var in (
    "FIGCITE_ZOTERO_API_KEY",
    "ZOTERO_API_KEY",
    "FIGCITE_ZOTERO_LIBRARY_ID",
    "ZOTERO_LIBRARY_ID",
    "FIGCITE_ZOTERO_LIBRARY_TYPE",
    "ZOTERO_LIBRARY_TYPE",
):
    os.environ.pop(_var, None)
os.environ["FIGCITE_ZOTERO_CACHE"] = str(Path(_TMP) / "zotero-cache")
# ...including the on-disk credential file, which a real install has.
os.environ["FIGCITE_ZOTERO_CONFIG"] = str(Path(_TMP) / "zotero-config.json")

# Unit tests must never shell out to Windows. Without this, `staging_dirs()`
# calls `_win_userprofile()`, which runs
#
#     powershell.exe -NoProfile -Command $env:USERPROFILE
#
# swallows every exception, and returns None on failure -- at which point
# `staging_dirs` raises RuntimeError and whatever test touched
# `service.pending_items()` fails for a reason that has nothing to do with
# the code under test. `clipboard.py` already offers this override and
# documents it as the way to say "I need my own queue instead of racing".
#
# Three things were wrong with leaving it unset:
#
#   1. It is a live external boundary inside the `not live` suite. PowerShell
#      startup over WSL interop is slow and gets slower under load; the 30s
#      timeout is generous until eight pytest processes compete for the CPU,
#      and then it is not. Observed: a mutation-sweep worker's baseline came
#      back red here while seven others were green.
#   2. The failure direction is the quiet one. A harness that reads a failing
#      test as evidence records the mutant as KILLED when no test caught it,
#      so an interop hiccup silently manufactures coverage.
#   3. The suite could not run at all off WSL -- no powershell.exe, so
#      `staging_dirs()` raises on any machine without Windows interop. That
#      is not a property a unit suite should have.
#
# A POSIX path is deliberate: `win_to_wsl` runs `wslpath -u`, which exits 1
# on a path that is already POSIX and falls back to returning it unchanged,
# so both halves of the tuple land on this directory and nothing shells out.
os.environ["FIGCITE_STAGING_WIN"] = str(Path(_TMP) / "staging")


# Findings I3/I4 (task-2 review, round 1). Imported down here, after the env
# vars above are set, since figcite.store reads FIGCITE_HOME at import time.
from figcite import service  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_skipped_items():
    """`service._skipped` is a process-global list with no reset of its own.

    Without this, a ref skipped in one test (e.g. "staged:clip-1.png", a name
    reused across test files because it is the obvious fixture name) stays
    skipped for the rest of the pytest session and silently reorders
    `pending_items()` in unrelated tests.
    """
    service._skipped.clear()
    yield
    service._skipped.clear()


def _is_loopback(address) -> bool:
    """True for a connect() address that cannot leave this machine.

    Round-4 finding I2. This is deliberately STRUCTURAL -- `ip_address(...)
    .is_loopback`, which covers all of 127.0.0.0/8 and ::1 -- rather than a
    list of spellings to compare against. A name list cannot guard an open
    set: `{"127.0.0.1", "::1"}` would refuse `127.0.0.2` (a legitimate
    loopback a test server could bind) while a hostname list would let
    `localhost` through even where /etc/hosts points it somewhere real.
    Anything that is not a literal loopback IP -- a DNS name, a public
    address, a non-tuple (AF_UNIX) address -- is not loopback here, so the
    caller blocks it exactly as before.
    """
    if not isinstance(address, tuple) or not address:
        return False
    try:
        return ipaddress.ip_address(address[0]).is_loopback
    except ValueError:  # a hostname, not an IP literal
        return False


@pytest.fixture(autouse=True)
def _block_network(request):
    """Non-live tests must never reach a real socket. Loopback is not one.

    Without this, removing a safety-critical guard (as happened with the
    Step-5 experiment, and as I2/C1's mutation testing does on purpose) can
    fall through to a real network call instead of raising the exception the
    test is checking for -- so the test's pass/fail starts depending on
    whether CrossRef happens to be reachable, not on the guard.

    Round-4 finding I2: this used to patch `connect` unconditionally, which
    made every HTTP test of the local web server `@pytest.mark.live` -- and
    `.githooks/pre-push` runs `pytest -m "not live"`. The whole HTTP security
    net (the arbitrary-file-write test, the CSRF/Origin test, the forged-Host
    test, the Content-Length test, the lock-serialization tests) therefore
    never ran on push. None of them need anything but a socket to 127.0.0.1,
    which reaches no network and no third party. Exempting loopback is what
    lets them run in the gate; everything else stays blocked, which is the
    property this fixture actually exists for.
    """
    if request.node.get_closest_marker("live"):
        yield
        return

    real_connect = socket.socket.connect

    def _blocked_connect(self, address, *a, **kw):
        if _is_loopback(address):
            return real_connect(self, address, *a, **kw)
        raise RuntimeError(
            f"network access blocked in a non-live test (tried to connect to "
            f"{address!r}); mark the test @pytest.mark.live if it legitimately "
            "needs the network"
        )

    # A PRIVATE MonkeyPatch, deliberately not the `monkeypatch` fixture.
    #
    # `monkeypatch` is function-scoped, so this fixture and the test function
    # were handed THE SAME instance -- and `undo()` is instance-wide, not
    # per-caller. Any test calling `monkeypatch.undo()` therefore revoked this
    # guard along with its own patches, and the rest of that test ran with the
    # network wide open. `test_browser.py` did exactly that in its positive
    # control and reached api.crossref.org on every non-live run, which is
    # what made it fail intermittently under load: CrossRef rate-limited it.
    #
    # A guard the guarded code can revoke is not a guard. This instance is
    # unreachable from the test, so no test can turn it off by accident.
    mp = pytest.MonkeyPatch()
    mp.setattr(socket.socket, "connect", _blocked_connect)
    try:
        yield
    finally:
        mp.undo()


class _InteropBlocked(BaseException):
    """Derived from BaseException, not Exception, and that is the whole point.

    The first version raised RuntimeError. `clipboard._win_userprofile` wraps
    its PowerShell call in a bare `except Exception: pass` and returns None on
    any failure, so it CAUGHT the guard's exception and swallowed the message;
    what surfaced instead was `staging_dirs`'s own "is this WSL with Windows
    interop enabled?", which sends the reader off diagnosing their machine
    rather than the test. The block still worked -- no PowerShell was launched
    -- but the reason for it did not survive the trip.

    Same lesson as `_block_network` one fixture up, where a test's
    `monkeypatch.undo()` revoked the network guard: a guard the guarded code
    can neutralise is not a guard. `except Exception` cannot catch this.
    """


@pytest.fixture(autouse=True)
def _block_windows_interop(request):
    """Non-live tests must never shell out to Windows. Same shape as the
    network block above, different boundary.

    `staging_dirs()` used to reach `powershell.exe -Command $env:USERPROFILE`
    on any test that touched `service.pending_items()` -- 24 launches per
    `not live` run. PowerShell startup over WSL interop is slow, gets slower
    under load, and `_win_userprofile` swallows every exception and returns
    None, at which point `staging_dirs` raises. A mutation sweep running the
    suite in 8 processes hit exactly that: one worker's baseline came back red
    and it refused to run its ~231 mutants.

    Setting `FIGCITE_STAGING_WIN` in this file removed all 24. This fixture is
    what keeps it at 0 -- the difference between having fixed it once and it
    staying fixed. The failure it prevents is quiet in the worst way: an
    interop hiccup makes a test FAIL, and a harness that reads failure as
    evidence records the mutant as KILLED when no test caught it.

    Same private-MonkeyPatch reasoning as above: a guard the guarded code can
    revoke is not a guard.
    """
    if request.node.get_closest_marker("live"):
        yield
        return

    real_run = subprocess.run

    def _blocked_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        parts = (
            [argv] if isinstance(argv, (str, bytes, os.PathLike)) else list(argv or [])
        )
        if any("powershell" in str(p).lower() for p in parts):
            raise _InteropBlocked(
                f"a non-live test tried to launch PowerShell ({parts[:1]!r}). "
                "Unit tests must not depend on Windows interop: it is slow, it "
                "fails under load, and it makes the suite unrunnable off WSL. "
                "Set FIGCITE_STAGING_WIN (already set here) or stub the call; "
                "mark the test @pytest.mark.live if it genuinely needs Windows."
            )
        return real_run(*args, **kwargs)

    mp = pytest.MonkeyPatch()
    mp.setattr(subprocess, "run", _blocked_run)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _private_store(tmp_path, monkeypatch):
    """Every test gets its own store, corpus and library.

    The session used to share one `FIGCITE_HOME` created at import, so a
    figure filed by one test stayed visible to every test after it. That is
    not a hypothetical: two tests written during the mutation audit passed
    alone and failed only in the full suite, because both matchers fall back
    to `store.find_similar` over the GLOBAL store whatever manifest they are
    handed, and a blank query image perceptually matched a record some
    unrelated test had filed.

    It also cost the equivalence registry its parallelism. `conftest` is
    imported once per PROCESS, so under xdist each worker got its own store
    and the accumulation was partitioned rather than shared -- which makes a
    mutant killed only via accumulated state able to survive in a worker that
    never accumulates it. The registry had to run serially to model the gate
    honestly. With the store private per TEST there is no accumulation to
    partition, and the two topologies stop being able to disagree.

    Repointing rather than re-importing: nothing does `from .store import
    MANIFEST`, so every consumer reads these through the module and sees the
    new value. `corpus`'s four paths are computed from `store.DATA_DIR` at
    import and frozen, so they need repointing of their own -- a fixture that
    moved only the store would leave every corpus test sharing one sqlite
    file and look like it had isolated them.
    """
    from figcite import corpus, store

    home = tmp_path / "figcite-home"
    corpus_dir = home / "corpus"
    monkeypatch.setenv("FIGCITE_HOME", str(home))
    for obj, name, value in (
        (store, "DATA_DIR", home),
        (store, "MANIFEST", home / "manifest.jsonl"),
        (store, "STAGING", home / "staging"),
        (store, "LIBRARY", home / "library"),
        (corpus, "CORPUS_DIR", corpus_dir),
        (corpus, "DB_PATH", corpus_dir / "figures.sqlite"),
        (corpus, "IMAGE_DIR", corpus_dir / "images"),
        (corpus, "DESCRIPTOR_DIR", corpus_dir / "descriptors"),
    ):
        monkeypatch.setattr(obj, name, value)

    # The isolation has to be OBSERVED, not assumed. A fixture that silently
    # stopped repointing -- a renamed attribute, a new path added to store --
    # would leave the suite green and sharing one directory again, which is
    # exactly the state this replaced.
    assert str(home) in str(store.MANIFEST), store.MANIFEST
    assert str(home) in str(corpus.DB_PATH), corpus.DB_PATH


@pytest.fixture
def ppt_module():
    """The PowerPoint guard module, imported WITHOUT running its tests.

    Its safety predicates are pure string handling, so they belong in the
    ordinary suite; the tests around them are `live` and stay deselected.
    """
    import importlib.util

    path = Path(__file__).resolve().parent / "test_pptx_powerpoint.py"
    spec = importlib.util.spec_from_file_location("_ppt_guard", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
