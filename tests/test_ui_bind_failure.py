"""What `figcite ui` says when the port is not free.

Refusing a taken port is deliberate -- two servers writing one manifest is a
corruption path -- but the refusal surfaced as an unhandled OSError traceback.
The commonest way to hit it is restarting the UI within TIME_WAIT of stopping
it, which is transient and self-resolving, and a stack trace says none of that.
"""

import errno

import pytest

from figcite import cli, web


def _boom(port):
    raise OSError(errno.EADDRINUSE, "Address already in use")


def test_a_taken_port_is_explained_not_traced(monkeypatch, capsys):
    monkeypatch.setattr(web, "make_server", _boom)

    rc = web.serve(port=8765)

    err = capsys.readouterr().err
    assert rc != 0, "a failed bind must not report success"
    assert "8765" in err, "the message never says which port"
    assert "Traceback" not in err
    # The actionable half: it is in use, and figcite will not silently move.
    assert "in use" in err.lower()


def test_the_ui_command_returns_that_failure(monkeypatch):
    """cmd_ui returned 0 unconditionally, so a failed bind exited success."""
    monkeypatch.setattr(web, "make_server", _boom)

    class A:
        port = 8765
        open = False

    assert cli.cmd_ui(A()) != 0


def test_a_working_bind_still_serves(monkeypatch):
    """Positive control: the handler must not swallow a healthy start."""
    served = []

    class FakeSrv:
        server_address = ("127.0.0.1", 12345)

        def serve_forever(self):
            served.append(True)

    monkeypatch.setattr(web, "make_server", lambda port: FakeSrv())
    assert web.serve(port=0) == 0
    assert served, "serve_forever never ran on a healthy bind"


def test_other_oserrors_are_not_disguised_as_a_busy_port(monkeypatch, capsys):
    """Fail loud: a permission error is not 'port in use'."""

    def denied(port):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(web, "make_server", denied)
    with pytest.raises(OSError):
        web.serve(port=80)
