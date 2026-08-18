import os
import socket
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


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    """Non-live tests must never reach a real socket.

    Without this, removing a safety-critical guard (as happened with the
    Step-5 experiment, and as I2/C1's mutation testing does on purpose) can
    fall through to a real network call instead of raising the exception the
    test is checking for -- so the test's pass/fail starts depending on
    whether CrossRef happens to be reachable, not on the guard.
    """
    if request.node.get_closest_marker("live"):
        yield
        return

    def _blocked_connect(self, address, *a, **kw):
        raise RuntimeError(
            f"network access blocked in a non-live test (tried to connect to "
            f"{address!r}); mark the test @pytest.mark.live if it legitimately "
            "needs the network"
        )

    monkeypatch.setattr(socket.socket, "connect", _blocked_connect)
    yield
