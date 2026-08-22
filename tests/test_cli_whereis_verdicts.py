"""`figcite whereis` printing all three verdicts, not two of them.

`cli.cmd_whereis` renders the three-outcome discipline the whole tool is
built around:

    if res["verdict"] == "match":        print("  found in your corpus")
    elif res["verdict"] == "no-match":   print("  no match: ...")
    else:                                print(f"  could not decide: {reason}")

The string sweep killed `"no-match"` and `"match"` SURVIVED. That asymmetry
is the finding: something drives the no-match path, nothing drives the match
path, and the fall-through `else` means a broken `"match"` comparison does
not error -- a genuine hit silently reports "could not decide" instead.

Reporting a hit as an undecidable is the safe direction and still wrong: the
figure IS in the corpus, the citation IS available, and the user is told the
tool could not tell. The whole point of separating "no" from "could not
look" is that they lead to different actions, and this collapses "yes" into
the third one.

Each verdict is asserted to produce its OWN wording and NOT the others', so
no branch can cover for another -- the same reason the pdfdeck caption tests
assert per surface instead of over joined output.
"""

from __future__ import annotations

import pytest

from figcite import cli


def _res(verdict, **kw):
    base = {
        "verdict": verdict,
        "matches": [],
        "reason": "the query image has no gradients to compare",
    }
    base.update(kw)
    return base


@pytest.fixture
def whereis(monkeypatch):
    """Stub the service so the test is about the CLI's rendering only."""

    def _install(result):
        monkeypatch.setattr("figcite.service.whereis", lambda ref, **kw: result)

    return _install


def test_a_match_says_it_was_found(whereis, capsys):
    """THE finding. Nothing drove this branch, and the `else` swallows a
    broken comparison instead of raising."""
    whereis(
        _res(
            "match",
            matches=[
                {
                    "doi": "10.3390/horticulturae6040087",
                    "evidence": True,
                    "score_label": "hamming 0",
                    "label": "Figure 1",
                    "pmcid": "PMC1",
                }
            ],
        )
    )

    cli.main(["whereis", "fig.png"])
    out = capsys.readouterr().out

    assert "found in your corpus" in out, f"a match did not report as found: {out!r}"
    assert "could not decide" not in out, (
        f"a match fell through to the could-not-decide branch: {out!r}"
    )
    assert "no match" not in out, out


def test_no_match_says_it_is_not_there(whereis, capsys):
    whereis(_res("no-match"))

    cli.main(["whereis", "fig.png"])
    out = capsys.readouterr().out

    assert "no match" in out, out
    assert "found in your corpus" not in out, out
    assert "could not decide" not in out, out


def test_could_not_decide_says_why(whereis, capsys):
    """The third outcome, and the one that must never be mistaken for the
    second. It also has to carry its reason -- "could not decide" with no
    reason reads exactly like "no"."""
    whereis(_res("could-not-decide"))

    cli.main(["whereis", "fig.png"])
    out = capsys.readouterr().out

    assert "could not decide" in out, out
    assert "no gradients to compare" in out, (
        f"the undecidable verdict was printed without its reason: {out!r}"
    )
    assert "found in your corpus" not in out, out


def test_the_three_verdicts_do_not_print_the_same_thing(whereis, capsys):
    """Positive control on the set.

    Each test above checks one verdict. If the renderer collapsed to a single
    message they would still pass individually only if that message happened
    to contain every phrase -- but a renderer that printed, say, the
    could-not-decide line for everything would pass NONE of them. This
    asserts the property directly, so the discrimination is pinned even if
    the wording changes.
    """
    seen = {}
    for verdict in ("match", "no-match", "could-not-decide"):
        whereis(_res(verdict))
        cli.main(["whereis", "fig.png"])
        seen[verdict] = capsys.readouterr().out.strip()

    assert len(set(seen.values())) == 3, (
        f"two verdicts rendered identically, so the user cannot tell them apart: {seen}"
    )
