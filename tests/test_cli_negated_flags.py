"""`--no-X` flags, asserted to actually mean "no X".

`cli.py` was the largest survivor pool in the sweep (135 of 173 mutants alive,
22% kill), and the non-const survivors sort into two families: cosmetic print
fallbacks (`x or '?'`), and this one -- eight sites of the shape

    captions=not a.no_captions

where **dropping the `not` inverts the user's explicit choice**. `--no-captions`
would then ADD captions and omitting it would suppress them. Every one of those
mutants survived, because nothing in the suite passes a `--no-` flag and checks
what the code below argparse actually received.

One of the eight is not cosmetic at all:

    auto_confirm=not a.no_auto_confirm

Inverted, `figcite watch` auto-files captures the user never confirmed, and
`--no-auto-confirm` -- the flag whose whole purpose is "leave even GROUNDED
captures pending" -- becomes the flag that files them. That is the tool's
central rule (a machine guess is never auto-cited) defeated by a missing three
letters, which is why this file exists.

**Why these are behavioural tests and not one structural check.** The obvious
move for an open set of flags is an AST guard: every `no_*` dest must be
consumed under a `Not`. It does not work here, and the reason is worth
recording rather than rediscovering. `a.no_manifest` is legitimately read raw:

    None if a.no_manifest else str(Path(out).with_suffix(""))

The inversion is in the branch ORDER, not in the syntax, so a `Not`-shaped
predicate cannot observe it -- it would flag correct code and, worse, would pass
if someone swapped the two branches. A guard only works where its predicate
fully observes its referent; here the referent is "the flag's meaning survives
to the callee", and the only thing that observes that is calling the CLI and
looking at what the callee got.

Each test asserts BOTH polarities. A one-sided assertion ("passing the flag
yields False") is satisfied by a callee that is handed False unconditionally,
which is the same defect class as the rest of this audit.
"""

from __future__ import annotations

import pytest

from figcite import cli

PDF_REPORT = {
    "out": "deck.cited.pdf",
    "pictures": 0,
    "cited": 0,
    "unsourced": 0,
    "manifest": None,
    "entries": [],
    "rows": [],
}
PPTX_REPORT = dict(PDF_REPORT, out="deck.cited.pptx")


@pytest.fixture
def apply_spy(monkeypatch):
    """Capture the kwargs each `apply` implementation is handed.

    `cmd_apply` imports both inside the function body, so the patch has to land
    on the module attribute rather than on a name bound in `cli`.
    """
    seen: dict = {}

    def _pdf(path, out, **kw):
        seen.clear()
        seen.update(kw, _impl="pdf", _path=path, _out=out)
        return PDF_REPORT

    def _pptx(path, out, **kw):
        seen.clear()
        seen.update(kw, _impl="pptx", _path=path, _out=out)
        return PPTX_REPORT

    monkeypatch.setattr("figcite.pdfdeck.apply", _pdf)
    monkeypatch.setattr("figcite.deck.apply", _pptx)
    return seen


# --- captions / credits, on both output formats --------------------------


@pytest.mark.parametrize("deck", ["deck.pptx", "deck.pdf"])
@pytest.mark.parametrize(
    "flag,kwarg", [("--no-captions", "captions"), ("--no-credits", "credits")]
)
def test_a_no_flag_turns_its_feature_off_and_absence_turns_it_on(
    apply_spy, deck, flag, kwarg
):
    """Both polarities, because either alone is satisfied by a constant.

    Both formats, because the pdf and pptx branches of `cmd_apply` each spell
    the inversion out separately -- four sites, four surviving mutants.
    """
    assert cli.main(["apply", deck, flag]) == 0
    assert apply_spy[kwarg] is False, (
        f"{flag} on {deck} did not reach {kwarg}=False: {dict(apply_spy)}"
    )

    assert cli.main(["apply", deck]) == 0
    assert apply_spy[kwarg] is True, (
        f"{kwarg} defaulted to False on {deck} without {flag}, so the flag "
        f"controls nothing: {dict(apply_spy)}"
    )


@pytest.mark.parametrize("deck", ["deck.pptx", "deck.pdf"])
def test_the_two_feature_flags_are_not_wired_to_each_other(apply_spy, deck):
    """`captions` and `credits` are separate switches. A single shared flag
    passes every test above while ignoring one of the two options."""
    assert cli.main(["apply", deck, "--no-captions"]) == 0
    assert apply_spy["captions"] is False
    assert apply_spy["credits"] is True, (
        f"--no-captions also suppressed credits on {deck}: {dict(apply_spy)}"
    )

    assert cli.main(["apply", deck, "--no-credits"]) == 0
    assert apply_spy["credits"] is False
    assert apply_spy["captions"] is True, (
        f"--no-credits also suppressed captions on {deck}: {dict(apply_spy)}"
    )


# --- --no-manifest, the one whose inversion is NOT syntactic -------------


@pytest.mark.parametrize("deck", ["deck.pptx", "deck.pdf"])
def test_no_manifest_suppresses_the_manifest_path(apply_spy, deck):
    """The site an AST guard could not have checked: on the pdf branch this is
    a ternary (`None if a.no_manifest else ...`), on the pptx branch an `if`.
    Both must land on `manifest_path=None`."""
    assert cli.main(["apply", deck, "--no-manifest"]) == 0
    assert apply_spy["manifest_path"] is None, (
        f"--no-manifest still requested a manifest on {deck}: {dict(apply_spy)}"
    )

    assert cli.main(["apply", deck]) == 0
    assert apply_spy["manifest_path"], (
        f"no manifest was requested on {deck} even without --no-manifest, so "
        f"the flag changes nothing: {dict(apply_spy)}"
    )


@pytest.mark.parametrize("deck", ["deck.pptx", "deck.pdf"])
def test_an_explicit_manifest_path_beats_the_default(apply_spy, deck):
    assert cli.main(["apply", deck, "--manifest", "/tmp/mine"]) == 0
    assert apply_spy["manifest_path"] == "/tmp/mine"


# --- the one with provenance consequences --------------------------------


def test_no_auto_confirm_leaves_captures_pending(monkeypatch):
    """THE finding in this family.

    Inverted, `figcite watch` auto-files captures the user never confirmed and
    the flag that exists to prevent exactly that becomes the flag that causes
    it. Both polarities, because "always False" and "always True" each satisfy
    one half.
    """
    seen: dict = {}

    def _watch(**kw):
        seen.clear()
        seen.update(kw)
        return 0

    monkeypatch.setattr("figcite.clipboard.watch", _watch)

    assert cli.main(["watch", "--no-auto-confirm"]) == 0
    assert seen["auto_confirm"] is False, (
        "--no-auto-confirm still auto-confirmed: a machine guess would be "
        f"filed as a citation without anyone approving it. {seen}"
    )

    assert cli.main(["watch"]) == 0
    assert seen["auto_confirm"] is True, (
        f"auto-confirm was off without the flag, so the flag controls nothing: {seen}"
    )


def test_watch_passes_its_other_settings_through_unchanged(monkeypatch):
    """Positive control for the test above: if the fake were being handed a
    fixed kwargs dict, the polarity assertions would prove nothing about
    plumbing."""
    seen: dict = {}
    monkeypatch.setattr("figcite.clipboard.watch", lambda **kw: (seen.update(kw), 0)[1])

    assert cli.main(["watch", "--hours", "3.5"]) == 0
    assert seen["max_hours"] == 3.5


def test_watch_rejects_the_poll_interval_it_no_longer_has(monkeypatch, capsys):
    """`--poll-ms` set the interval of a loop that no longer exists: the watcher
    is woken by WM_CLIPBOARDUPDATE and never asks on a timer.

    It is REJECTED rather than accepted-and-ignored. A flag that is quietly
    swallowed reads to whoever typed it as a setting that took effect, which is
    the worse of the two failures -- they would go on believing they had turned
    the poll rate down on something that does not poll.
    """
    monkeypatch.setattr("figcite.clipboard.watch", lambda **kw: 0)
    with pytest.raises(SystemExit) as e:
        cli.main(["watch", "--poll-ms", "250"])
    assert e.value.code == 2
    assert "poll-ms" in capsys.readouterr().err


def test_no_start_installs_without_launching(monkeypatch):
    """`autostart install --no-start` must not start the watcher."""
    seen: dict = {}

    def _install(**kw):
        seen.clear()
        seen.update(kw)
        return {
            "ok": True,
            "vbs": "x.vbs",
            "log": "x.log",
            "hours": kw.get("hours", 24.0),
            "started": False,
        }

    monkeypatch.setattr("figcite.autostart.install", _install)

    assert cli.main(["autostart", "install", "--no-start"]) == 0
    assert seen["start_now"] is False, seen

    assert cli.main(["autostart", "install"]) == 0
    assert seen["start_now"] is True, (
        f"install did not start the watcher even without --no-start: {seen}"
    )
