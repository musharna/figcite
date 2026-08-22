"""Four inverted-guard survivors from the logic sweep re-run.

`drop not` is the sharpest operator in the sweep -- 84% killed, and the 19
survivors cannot be equivalent by construction, because removing a `not`
always inverts the condition. Nine of the nineteen are autostart/Windows
(backlog item 6, an accepted gap). These four are not.

  cli.py:543       if not zotero.configured()
  cli.py:69        if not (a.doi or a.cite or a.url)
  clipboard.py:112 if not up            (staging_dirs, no userprofile)
  zotero.py:490    if len(exact) == 1 and not exact[0]["doi"]

The first and last are the tool's three-state discipline at two different
layers: NOT CONFIGURED is not the same as configured-but-unreachable, and an
exact title match with no DOI is not the same as no match. Both distinctions
were rendered by code no test drove.

clipboard.py:112 is a coverage regression I INTRODUCED. conftest now sets
FIGCITE_STAGING_WIN so the unit suite stops shelling out to PowerShell --
correct, but it also means `staging_dirs`'s fallback half no longer executes,
so the guard that turns an unreadable userprofile into a clear error became
untestable by accident. The fix is to drive that branch directly with the
override removed and `_win_userprofile` stubbed, which needs no interop at
all. Removing a live dependency should not cost the coverage that depended
on it.
"""

from __future__ import annotations

import pytest

from figcite import cli, clipboard, zotero


# --- cli.py:543 -- NOT CONFIGURED is a third state, not a failure ---------


def _fake_creds(monkeypatch, configured: bool):
    if configured:
        monkeypatch.setattr(zotero, "configured", lambda: True)
        monkeypatch.setattr(zotero, "credentials", lambda: ("k", "6532713", "group"))
    else:
        monkeypatch.setattr(zotero, "configured", lambda: False)


def test_an_unconfigured_zotero_says_so_and_does_not_pretend_to_look(
    monkeypatch, capsys
):
    """THE finding. Inverted, a configured install reports NOT CONFIGURED and
    an unconfigured one falls through to `credentials()`, which raises."""
    _fake_creds(monkeypatch, configured=False)

    rc = cli.main(["zotero", "status"])
    out = capsys.readouterr().out

    assert "NOT CONFIGURED" in out, out
    assert rc == 1, f"an unconfigured library should not report success: {rc}"
    assert "unavailable" not in out, (
        f"absent credentials were reported as an outage: {out!r}"
    )


def test_a_configured_but_unreachable_zotero_is_an_outage_not_an_absence(
    monkeypatch, capsys
):
    """The middle state, and the one the whole project exists to keep apart
    from the other two. Credentials are present; the library cannot be
    reached. That is NOT 'not configured' and NOT 'no items'."""
    _fake_creds(monkeypatch, configured=True)
    monkeypatch.setattr(
        zotero, "library", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("429"))
    )

    rc = cli.main(["zotero", "status"])
    out = capsys.readouterr().out

    assert "configured" in out, out
    assert "NOT CONFIGURED" not in out, (
        f"a configured library reported as unconfigured: {out!r}"
    )
    assert "unavailable" in out, f"an outage was not named as one: {out!r}"
    assert rc == 1, rc


def test_a_working_zotero_reports_its_counts(monkeypatch, capsys):
    """Positive control on the set. "Always say NOT CONFIGURED" passes the
    first test; "always say unavailable" passes the second. Only a third
    distinct outcome pins all three apart."""
    _fake_creds(monkeypatch, configured=True)
    monkeypatch.setattr(
        zotero,
        "library",
        lambda *a, **kw: [
            {
                "title": "A paper",
                "doi": "10.1/a",
                "doi_source": "doi-field",
                "key": "K1",
            },
            {"title": "No doi here", "doi": "", "doi_source": "", "key": "K2"},
        ],
    )

    rc = cli.main(["zotero", "status"])
    out = capsys.readouterr().out

    assert rc == 0, out
    assert "NOT CONFIGURED" not in out and "unavailable" not in out, out
    assert "2 item(s) cached, 1 resolvable" in out, out


# --- cli.py:69 -- tag needs something to attribute TO --------------------


def test_tag_refuses_when_given_nothing_to_cite(tmp_path, capsys):
    """Inverted, `figcite tag img.png` with no metadata files a record that
    attributes the figure to nothing, and supplying --doi is rejected."""
    img = tmp_path / "fig.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)

    rc = cli.main(["tag", str(img)])
    err = capsys.readouterr().err

    assert rc == 2, f"tag accepted a figure with no source at all: {rc}"
    assert "--doi" in err, err


def test_tag_accepts_a_doi(tmp_path, monkeypatch):
    """Positive control: "refuse everything" passes the test above."""
    # `_finalize` is stubbed so the assertion is about the ARGUMENT GATE, not
    # about the filing pipeline behind it -- and so this test cannot fail for
    # a reason unrelated to the guard it exists to pin.
    monkeypatch.setattr(cli, "_finalize", lambda src, rec, out, quiet=False: src)
    img = tmp_path / "fig.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)

    rc = cli.main(["tag", str(img), "--doi", "10.1/x"])

    assert rc != 2, "a tag WITH a doi was refused as though it had none"


def test_tag_reports_a_missing_image_rather_than_tagging_it(tmp_path, capsys):
    """`if not src.exists()` -- the same guard appears five times across cli,
    browser and clipboard. Inverted, a real image is reported missing and a
    missing one is tagged."""
    rc = cli.main(["tag", str(tmp_path / "nope.png"), "--doi", "10.1/x"])
    err = capsys.readouterr().err

    assert rc == 2, rc
    assert "no such image" in err, err


# --- clipboard.py:112 -- an unreadable userprofile is a clear error -------


def test_staging_says_why_when_the_userprofile_cannot_be_read(monkeypatch):
    """Restores coverage that conftest's FIGCITE_STAGING_WIN removed.

    Driven with the override deleted and `_win_userprofile` stubbed, so it
    exercises the guard without launching PowerShell -- the dependency the
    conftest change exists to remove.
    """
    monkeypatch.delenv("FIGCITE_STAGING_WIN", raising=False)
    monkeypatch.setattr(clipboard, "_win_userprofile", lambda: None)

    with pytest.raises(RuntimeError) as got:
        clipboard.staging_dirs()

    assert "USERPROFILE" in str(got.value), (
        f"the failure did not say what could not be read: {got.value}"
    )


def test_staging_uses_the_userprofile_when_it_can_be_read(monkeypatch):
    """Positive control. "Always raise" passes the test above and breaks every
    capture on a machine that works."""
    monkeypatch.delenv("FIGCITE_STAGING_WIN", raising=False)
    monkeypatch.setattr(clipboard, "_win_userprofile", lambda: r"C:\Users\someone")

    win, _wsl = clipboard.staging_dirs()

    assert win == r"C:\Users\someone\.figcite\staging", win


def test_an_explicit_staging_override_wins(monkeypatch):
    """The branch above both of them, which conftest relies on."""
    monkeypatch.setenv("FIGCITE_STAGING_WIN", "/tmp/mine-staging")
    monkeypatch.setattr(
        clipboard,
        "_win_userprofile",
        lambda: pytest.fail("the override should short-circuit before this"),
    )

    win, _wsl = clipboard.staging_dirs()

    assert win == "/tmp/mine-staging", win


# --- zotero.py:490 -- an exact match with no DOI says WHY ----------------


def _library(monkeypatch, items):
    monkeypatch.setattr(zotero, "library", lambda *a, **kw: items)


def test_an_exact_title_match_with_no_doi_explains_itself(monkeypatch):
    """THE finding. The branch above this one already returns for an exact
    match that HAS a DOI, so dropping the `not` makes this branch dead: a
    match with no DOI loses its specific evidence and degrades to whatever
    generic outcome follows.

    "I found your paper but it has no DOI recorded" is a different message
    from "I found nothing", and it is actionable -- add the DOI in Zotero.
    """
    _library(
        monkeypatch,
        [{"title": "A study of orchids", "doi": "", "key": "K9", "doi_source": ""}],
    )

    out = zotero.resolve_title("A study of orchids")

    assert not out.get("grounded"), f"a DOI-less item was reported as grounded: {out}"
    assert "no DOI" in out["evidence"], (
        f"the reason the match could not be used was not given: {out['evidence']!r}"
    )
    assert out["candidates"], "the match was found but not offered as a candidate"


def test_an_exact_title_match_with_a_doi_is_grounded(monkeypatch):
    """Positive control, and the branch that must keep firing."""
    _library(
        monkeypatch,
        [
            {
                "title": "A study of orchids",
                "doi": "10.1/orchid",
                "key": "K1",
                "doi_source": "doi-field",
            }
        ],
    )

    out = zotero.resolve_title("A study of orchids")

    assert out.get("grounded") is True, out
    assert out["doi"] == "10.1/orchid", out
    assert "no DOI" not in out["evidence"], out["evidence"]
