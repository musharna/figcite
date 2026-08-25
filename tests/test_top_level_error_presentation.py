"""Nothing reaches the terminal as a crash, and nothing is hidden either.

`cli.main` caught `(LookupError, RuntimeError, ValueError)` -- a list of names
guarding an open set. Any type nobody thought to list was presented to the
user as a Python traceback, which is not hypothetical: `figcite whereis` on an
unreadable image raised OSError out of the matcher and dumped a stack, because
OSError is in none of those three. The exception-routing mutation tier then
showed that two of the three listed arms had no coverage at all, so the tuple
was both too narrow AND mostly unexercised.

Adding OSError would have been a tripwire. The next unlisted type does it
again, and the tenth instance of "a list of names cannot guard an open set" is
still that. The boundary stops enumerating types instead.

## The objection this has to answer

"`except Exception` masks bugs" is the right worry about the WRONG shape of
fix. It masks a bug when the detail is thrown away. Here the message always
prints and the traceback is one environment variable away, so nothing is lost
-- and the pair of tests below is what makes that a property of the code
rather than a claim in a docstring.

## The two things a broad catch must NOT swallow

`except Exception` leaves BaseException alone, and that is load-bearing rather
than incidental. Ctrl-C is not an error to dress up in "error: ", and
argparse's own exit for a misused command line is a different thing from a
command that ran and failed. Both are pinned, because a later edit to
`BaseException` would break them silently and the exit codes are what scripts
read.
"""

from __future__ import annotations

import pytest

from figcite import cli, corpus


def _explodes(exc):
    def boom():
        raise exc

    return boom


# ------------------------------------------------ no type escapes as a crash


@pytest.mark.parametrize(
    "exc",
    [
        OSError("Truncated File Read"),
        TypeError("a bad refactor"),
        AttributeError("'NoneType' object has no attribute 'x'"),
        KeyError("some key"),
    ],
    ids=["OSError", "TypeError", "AttributeError", "KeyError"],
)
def test_any_failure_is_reported_rather_than_dumped(exc, monkeypatch, capsys):
    """None of these four is in the tuple that used to be here.

    OSError is the one that actually reached a user. The rest are listed
    because the point is not to have caught OSError -- it is to have stopped
    keeping a list.
    """
    monkeypatch.setattr(corpus, "status", _explodes(exc))

    rc = cli.main(["corpus", "status"])

    err = capsys.readouterr().err
    assert rc == 1, rc
    assert "error:" in err, err
    assert "Traceback (most recent call last)" not in err, (
        "a traceback reached the terminal without being asked for"
    )


def test_the_traceback_is_available_when_asked_for(monkeypatch, capsys):
    """The half that makes the broad catch honest.

    Without this the handler would be indistinguishable from one that
    swallows: same message, same exit code, and the defect gone. The env var
    is the difference between presenting an error and hiding one.
    """
    monkeypatch.setenv(cli.TRACEBACK_ENV, "1")
    monkeypatch.setattr(corpus, "status", _explodes(RuntimeError("boom")))

    rc = cli.main(["corpus", "status"])

    err = capsys.readouterr().err
    assert rc == 1, rc
    assert "Traceback (most recent call last)" in err, err
    assert "boom" in err


def test_the_message_says_how_to_get_the_traceback(monkeypatch, capsys):
    """A debug channel nobody is told about is not a debug channel."""
    monkeypatch.delenv(cli.TRACEBACK_ENV, raising=False)
    monkeypatch.setattr(corpus, "status", _explodes(RuntimeError("boom")))

    cli.main(["corpus", "status"])

    assert cli.TRACEBACK_ENV in capsys.readouterr().err


# --------------------------------------- what a broad catch must NOT swallow


def test_ctrl_c_is_not_dressed_up_as_an_error(monkeypatch):
    """KeyboardInterrupt derives from BaseException, so `except Exception`
    leaves it alone.

    Printing "error: " and exiting 1 for a deliberate Ctrl-C would report the
    user's own decision as a failure, and would stop a shell seeing the
    interrupt. Pinned because widening to BaseException is a one-word edit
    that nothing else would catch.
    """
    monkeypatch.setattr(corpus, "status", _explodes(KeyboardInterrupt()))

    with pytest.raises(KeyboardInterrupt):
        cli.main(["corpus", "status"])


def test_a_misused_command_line_still_exits_two(capsys):
    """argparse raises SystemExit, also a BaseException.

    "You typed the command wrong" is a different outcome from "the command ran
    and failed", and scripts read the difference. Catching it here would
    collapse 2 into 1 -- the same two-into-one flattening this project spends
    its time preventing elsewhere.
    """
    with pytest.raises(SystemExit) as caught:
        cli.main(["definitely-not-a-command"])

    assert caught.value.code == 2, caught.value.code
