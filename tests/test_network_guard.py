"""The guard that stops a non-live test reaching the network, guarded.

`conftest._block_network` is the reason every other test in this suite can
assert "the lookup failed" without that outcome depending on whether CrossRef
is reachable. It is load-bearing for the push gate, and nothing tested it.

It was defeatable. The fixture took the function-scoped `monkeypatch` fixture,
which meant it and the test function shared ONE MonkeyPatch instance, and
`undo()` is instance-wide. Any test calling `monkeypatch.undo()` silently
revoked the network block for the remainder of its own body.
`test_browser.py` did precisely that in a positive control and reached
api.crossref.org on every non-live run -- which is how it came to fail
intermittently under parallel load, when CrossRef rate-limited it.

Found while mutation-sweeping the suite: one sweep worker's BASELINE (no
mutation at all) came back red on that test while five others were green.
A flaky baseline is not noise, it is a finding.
"""

import socket

import pytest


def test_a_plain_outbound_connect_is_refused(monkeypatch):
    """The guard does its job at all -- the positive control for everything
    below, since each test after this asserts the guard SURVIVES something,
    and 'survives' is meaningless if it was never active."""
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="network access blocked"):
            s.connect(("api.crossref.org", 443))
    finally:
        s.close()


def test_loopback_is_still_allowed():
    """The other half: the guard must not block the local web-server tests,
    which is why it exempts loopback. Without this a guard that refused
    everything would pass the test above and quietly break the HTTP suite."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    client = socket.socket()
    try:
        client.connect(("127.0.0.1", port))  # must NOT raise
    finally:
        client.close()
        srv.close()


def test_the_guard_survives_a_test_calling_monkeypatch_undo(monkeypatch):
    """THE finding.

    A test undoing its own patches must not take the safety net with it.
    Before the fix this failed at the first assert: `undo()` restored the
    real `socket.socket.connect`, and the connect below reached the internet.
    """
    guarded = socket.socket.connect

    monkeypatch.setattr("os.getcwd", lambda: "/tmp")
    monkeypatch.undo()

    assert socket.socket.connect is guarded, (
        "monkeypatch.undo() in a test reverted the network guard"
    )
    # ...and stated as behaviour, not just identity, because an identity
    # check alone would pass on a guard that had been replaced by another
    # equally-broken callable.
    s = socket.socket()
    try:
        with pytest.raises(RuntimeError, match="network access blocked"):
            s.connect(("api.crossref.org", 443))
    finally:
        s.close()


@pytest.mark.live
def test_a_live_marked_test_is_exempt():
    """The exemption is real, so the guard cannot be 'always on' by accident.

    Marked live so it only runs where the network is expected; without it
    nothing would notice the marker check being deleted from the fixture.
    """
    s = socket.socket()
    try:
        s.settimeout(10)
        s.connect(("api.crossref.org", 443))
    finally:
        s.close()
