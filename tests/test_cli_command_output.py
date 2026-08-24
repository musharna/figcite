"""CLI commands whose OUTPUT is the whole product, driven end to end.

Twenty-six survivors sat in cli.py, and nine of them in one command: nothing
in the suite ever called `figcite autostart status`. That command exists to
answer a single question -- "is my clipboard actually being watched right
now" -- and every line of its report could be deleted or inverted without a
test noticing. The dict-key mutants there are not subtle: `st["MUTANT"]`
raises KeyError, so a single call to the command kills them. They survived
because there was no call.

The rest are the same shape one layer along: an audit line that prints
"NO SOURCE", a bibliography that picks its own output path, a status count
that says how many titles are unambiguous. All of them are things a user
reads and acts on, and none of them were asserted.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

from figcite import cli


# ------------------------------------------------- autostart status


def _status(**over):
    """A full status dict; each test overrides only what it is about."""
    st = {
        "installed": True,
        "launcher": r"C:\Users\me\Startup\figcite.vbs",
        "watching": True,
        "processes": [{"pid": "4242", "started": "10:01"}],
        "supervisors": [{"pid": "4243"}],
        "log": r"C:\Users\me\.figcite\watch.log",
        "log_mtime": 1_700_000_000.0,
        "log_tail": ["=== figcite watch session ==="],
        "sessions": 3,
        "staged_pngs": 2,
        "clipboard_records": 7,
    }
    st.update(over)
    return st


def _run_status(monkeypatch, capsys, **over):
    from figcite import autostart

    monkeypatch.setattr(autostart, "status", lambda: _status(**over))
    rc = cli.main(["autostart", "status"])
    return rc, capsys.readouterr().out


def test_status_says_whether_the_launcher_is_installed(monkeypatch, capsys):
    """`if not st["installed"]` survived dropping its `not`, and the key name
    survived being mutated -- because nothing called this command."""
    rc, out = _run_status(monkeypatch, capsys, installed=True)
    assert "autostart: installed" in out, out
    assert "NOT installed" not in out, out

    rc, out = _run_status(monkeypatch, capsys, installed=False, supervisors=[])
    assert "NOT installed" in out, out


def test_status_reports_whether_a_poller_is_actually_running(monkeypatch, capsys):
    """The load-bearing line, per the comment above it: a launcher in Startup
    is an INTENTION, and only this says the clipboard is being read.

    `st["watching"]` gates both the message and the exit code, so a wrapper
    script polling `figcite autostart status` is told everything is fine
    while nothing is being captured.
    """
    rc, out = _run_status(monkeypatch, capsys, watching=True)
    assert "WATCHING" in out and "pid=4242" in out, out
    assert rc == 0, rc

    rc, out = _run_status(monkeypatch, capsys, watching=False)
    assert "NOT WATCHING" in out, out
    assert rc == 1, "a stopped watcher reported success to the caller"


def test_status_distinguishes_a_dead_supervisor_from_a_missing_install(
    monkeypatch, capsys
):
    """`if st["supervisors"] ... elif st["installed"]` -- three states, not
    two. Installed-but-not-running is actionable ("starts at next logon");
    not-installed is a different instruction entirely."""
    rc, out = _run_status(monkeypatch, capsys, supervisors=[{"pid": "9"}])
    assert "restart loop(s) alive" in out, out

    rc, out = _run_status(monkeypatch, capsys, supervisors=[], installed=True)
    assert "supervisor NOT running" in out, out

    rc, out = _run_status(monkeypatch, capsys, supervisors=[], installed=False)
    assert "supervisor NOT running" not in out, (
        "a machine with no launcher was told its supervisor had stopped"
    )


def test_status_prints_the_log_age_only_when_there_is_a_log(monkeypatch, capsys):
    """`if st["log_mtime"]` -- with no log there is no age to report, and
    printing one computed from a missing mtime is worse than silence."""
    rc, out = _run_status(monkeypatch, capsys)
    assert "min ago" in out and "session(s)" in out, out

    rc, out = _run_status(monkeypatch, capsys, log_mtime=None)
    assert "min ago" not in out, out


def test_status_prints_a_log_tail_only_when_there_is_one(monkeypatch, capsys):
    rc, out = _run_status(monkeypatch, capsys, log_tail=["one", "two"])
    assert "--- log tail ---" in out and "  two" in out, out

    rc, out = _run_status(monkeypatch, capsys, log_tail=[])
    assert "log tail" not in out, out


def test_status_always_reports_the_backlog(monkeypatch, capsys):
    """These two counts are unconditional, and they are how a user notices
    captures piling up unprocessed."""
    rc, out = _run_status(monkeypatch, capsys, staged_pngs=5, clipboard_records=9)
    assert "staged     5" in out, out
    assert "filed      9" in out, out


# ------------------------------------------------- autostart install/uninstall


def test_uninstall_says_which_of_the_two_things_it_did(monkeypatch, capsys):
    """`res["removed_launcher"]` picks between two opposite reports. Removing
    nothing and removing the launcher are different outcomes and the operator
    needs to know which happened."""
    from figcite import autostart

    monkeypatch.setattr(
        autostart, "uninstall", lambda: {"removed_launcher": True, "ok": True}
    )
    rc = cli.main(["autostart", "uninstall"])
    out = capsys.readouterr().out
    assert "launcher removed" in out, out
    assert rc == 0

    monkeypatch.setattr(
        autostart,
        "uninstall",
        lambda: {"removed_launcher": False, "ok": False, "stderr": "denied"},
    )
    rc = cli.main(["autostart", "uninstall"])
    out = capsys.readouterr().out
    assert "no launcher was installed" in out, out
    assert "warning: denied" in out, out
    assert rc == 1, "a failed uninstall reported success"


def test_install_says_it_left_a_running_watcher_alone(monkeypatch, capsys):
    """`elif not a.no_start` survived dropping its `not`.

    Three outcomes: started it now, found it already running, or was told not
    to start it. Inverted, `--no-start` claims the watcher was already
    running and a plain install says nothing at all.
    """
    from figcite import autostart

    monkeypatch.setattr(
        autostart,
        "install",
        lambda hours, start_now: {
            "ok": True,
            "started": False,
            "vbs": "v.vbs",
            "log": "l.log",
            "hours": hours,
        },
    )

    cli.main(["autostart", "install"])
    assert "already running; left it alone" in capsys.readouterr().out

    cli.main(["autostart", "install", "--no-start"])
    out = capsys.readouterr().out
    assert "already running" not in out, (
        f"--no-start claimed a watcher was already running: {out!r}"
    )


# ------------------------------------------------- register


def test_register_refuses_an_image_that_is_not_there(tmp_path, capsys):
    """`if not src.exists()` survived dropping its `not`, at a fifth site."""
    rc = cli.main(["register", str(tmp_path / "nope.png"), "--doi", "10.1/x"])
    assert rc == 2, rc
    assert "no such image" in capsys.readouterr().err


def test_register_records_the_commit_only_when_git_answers(tmp_path, monkeypatch):
    """`if r.returncode == 0` survived `Eq -> NotEq`.

    `--this-work` records the git commit that produced a figure. Inverted,
    the commit is taken from a FAILED `git rev-parse` -- whose stdout is
    empty outside a repository, and whose output is not a commit at all -- and
    stored as the provenance of your own figure.
    """
    import subprocess

    from figcite import store

    monkeypatch.setattr(store, "MANIFEST", tmp_path / "m.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    img = tmp_path / "fig.png"
    Image.new("RGB", (8, 8), "white").save(img)

    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout = rc, out

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R(0, "abc123\n"))
    cli.main(["register", str(img), "--this-work"])
    recs = list(store.all_records().values())
    assert recs[-1].source_detail["git_commit"] == "abc123", recs[-1].source_detail

    img2 = tmp_path / "fig2.png"
    Image.new("RGB", (8, 8), "black").save(img2)
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: _R(128, "not a repo\n"))
    cli.main(["register", str(img2), "--this-work"])
    recs = list(store.all_records().values())
    assert recs[-1].source_detail["git_commit"] is None, (
        f"a failed git call was recorded as the producing commit: "
        f"{recs[-1].source_detail}"
    )


# ------------------------------------------------- zotero status counts


def test_zotero_status_counts_titles_that_appear_exactly_once(monkeypatch, capsys):
    """`sum(1 for _t, n in titles.items() if n == 1)` survived `1 -> 2` and
    `Eq -> NotEq`.

    "Unambiguous" means exactly one item carries that title. Under `n == 2`
    the count reports the DUPLICATED titles, and under `!=` it reports every
    title that is NOT unique -- the precise inverse of what the label says,
    printed under the word "unambiguous".
    """
    from figcite import zotero

    monkeypatch.setattr(zotero, "configured", lambda: True)
    monkeypatch.setattr(zotero, "credentials", lambda: ("k", "1", "group"))
    monkeypatch.setattr(
        zotero,
        "library",
        lambda *a, **kw: [
            {"title": "Alone", "doi": "10.1/a", "doi_source": "doi-field", "key": "K1"},
            {
                "title": "Also alone",
                "doi": "10.1/d",
                "doi_source": "doi-field",
                "key": "K4",
            },
            {
                "title": "Twinned",
                "doi": "10.1/b",
                "doi_source": "doi-field",
                "key": "K2",
            },
            {
                "title": "Twinned",
                "doi": "10.1/c",
                "doi_source": "doi-field",
                "key": "K3",
            },
        ],
    )

    cli.main(["zotero", "status"])
    out = capsys.readouterr().out

    # TWO unique titles and ONE duplicated one, deliberately. My first version
    # of this fixture had one of each -- and a 1-and-1 fixture cannot tell a
    # set from its complement, so `n != 1` and `n == 2` both also produced 1
    # and both mutants survived a test written to kill them. The same defect
    # this whole audit is hunting, written while hunting it, for the third
    # time in it.
    assert "2 unambiguous title(s)" in out, (
        f"expected the two singleton titles; 1 would mean the count is "
        f"reporting duplicated titles instead: {out!r}"
    )


# ------------------------------------------------- bib output path


def _manifest_json(tmp_path, name="applied.json"):
    p = tmp_path / name
    p.write_text(json.dumps([{"record": None}]), encoding="utf-8")
    return p


def test_bib_writes_next_to_a_deck_but_not_next_to_a_manifest(
    tmp_path, capsys, monkeypatch
):
    """`Path(a.source).suffix.lower() != ".json"` survived `NotEq -> Eq`,
    dropping the `not`, and mutating the literal.

    A .pptx source gets a .bib beside it. An apply-manifest .json must NOT:
    the manifest already lives beside the deck, so deriving `<source>.bib`
    from it would write the same path the deck itself would claim -- and
    inverted, the deck prints to stdout while the manifest silently
    overwrites a file.
    """
    monkeypatch.setattr(cli, "_records_for_bib", lambda src: [])

    manifest = _manifest_json(tmp_path)
    cli.main(["bib", str(manifest)])
    out = capsys.readouterr().out
    assert not (tmp_path / "applied.bib").exists(), (
        "a .bib was derived from an apply-manifest's path"
    )
    assert "wrote" not in out, out

    # Positive control: the same command on a non-.json source DOES derive a
    # path, or the assertion above passes because `bib` never writes anything.
    deck = tmp_path / "talk.pptx"
    deck.write_bytes(b"not really a deck")

    cli.main(["bib", str(deck)])

    assert (tmp_path / "talk.bib").exists(), "no .bib was derived from a .pptx source"


# ------------------------------------------------- audit report lines


def _deck_with(tmp_path, records):
    """A fake audit report; `cmd_audit` only ever reads its shape."""
    return {
        "pptx": str(tmp_path / "t.pptx"),
        "file": str(tmp_path / "t.pdf"),
        "pictures": len(records),
        "tagged": sum(1 for r in records if r["record"]),
        "unconfirmed": 0,
        "untagged_substantive": sum(1 for r in records if not r["record"]),
        "rows": records,
    }


class _Rec:
    def __init__(self, short_cite="Someone 2020", confirmed=True):
        self.short_cite, self.confirmed, self.doi = short_cite, confirmed, "10.1/x"

    def context_line(self):
        return "ctx"


def test_audit_prints_NO_SOURCE_for_a_picture_with_no_record(
    tmp_path, monkeypatch, capsys
):
    """`if rec is None` survived `Is -> IsNot` in BOTH audit branches (pptx at
    one site, pdf at another).

    Inverted, every sourced figure is reported as having no source and every
    unsourced one is printed with `rec.short_cite` -- an attribute access on
    None, so the command dies partway through a report it has already begun
    printing. The whole point of `audit` is the list of figures that still
    need attribution.
    """
    from figcite import deck

    rows = [
        {
            "slide": 1,
            "shape": "Picture 1",
            "decorative": False,
            "matched_by": "sha256",
            "record": _Rec(),
        },
        {
            "slide": 2,
            "shape": "Picture 2",
            "decorative": False,
            "matched_by": "none",
            "record": None,
        },
    ]
    monkeypatch.setattr(
        deck, "audit", lambda p, min_inches=1.0: _deck_with(tmp_path, rows)
    )

    cli.main(["audit", str(tmp_path / "t.pptx")])
    out = capsys.readouterr().out

    assert "Picture 2" in out and "NO SOURCE" in out, out
    assert "Someone 2020" in out, "the sourced picture lost its citation"
    line_for_1 = [ln for ln in out.splitlines() if "Picture 1" in ln][0]
    assert "NO SOURCE" not in line_for_1, (
        f"a picture WITH a record was reported as unsourced: {line_for_1!r}"
    )


def test_audit_marks_decorative_pictures_as_such(tmp_path, monkeypatch, capsys):
    """`tag = "decorative" if r["decorative"] else ""` survived mutating the
    key name -- which makes every picture print the same blank tag, so the
    reason a small image was left out of the tally disappears from the report
    that is supposed to explain it."""
    from figcite import deck

    rows = [
        {
            "slide": 1,
            "shape": "Icon",
            "decorative": True,
            "matched_by": "none",
            "record": None,
        },
        {
            "slide": 2,
            "shape": "Figure",
            "decorative": False,
            "matched_by": "none",
            "record": None,
        },
    ]
    monkeypatch.setattr(
        deck, "audit", lambda p, min_inches=1.0: _deck_with(tmp_path, rows)
    )

    cli.main(["audit", str(tmp_path / "t.pptx")])
    out = capsys.readouterr().out

    icon = [ln for ln in out.splitlines() if "Icon" in ln][0]
    figure = [ln for ln in out.splitlines() if "Figure" in ln][0]
    assert "decorative" in icon, icon
    assert "decorative" not in figure, (
        f"a substantive figure was marked decorative: {figure!r}"
    )


def test_a_pdf_audit_goes_through_the_pdf_reporter(tmp_path, monkeypatch, capsys):
    """The pdf branch has its OWN copy of the `rec is None` guard, and prints
    pages rather than slides. A test driving only .pptx cannot reach it."""
    from figcite import pdfdeck

    rows = [
        {"page": 3, "matched_by": "none", "record": None},
        {"page": 4, "matched_by": "sha256", "record": _Rec("Other 2021")},
    ]
    monkeypatch.setattr(
        pdfdeck, "audit", lambda p, min_inches=1.0: _deck_with(tmp_path, rows)
    )

    cli.main(["audit", str(tmp_path / "t.pdf")])
    out = capsys.readouterr().out

    assert "page   3" in out and "NO SOURCE" in out, out
    assert "Other 2021" in out, out


# ------------------------------------------------- bib --check exit code


def test_bib_check_exits_nonzero_only_when_ghostcite_finds_something(
    tmp_path, monkeypatch, capsys
):
    """`return 1 if res.get("findings") else 0` survived mutating the key.

    `--check` exists to be wired into CI. Under the mutant `.get` always
    returns None and the command always exits 0, so a pipeline that gates on
    it passes with ghost citations in the bibliography -- the one thing the
    flag was added to prevent.
    """
    from figcite import bibtex
    from figcite.provenance import Record

    rec = Record(
        doi="10.1/x",
        citation="Someone (2020). A paper.",
        short_cite="Someone 2020",
        confirmed=True,
        source_kind="pdf-crop",
    )
    monkeypatch.setattr(cli, "_records_for_bib", lambda src: [rec])
    deck = tmp_path / "t.pptx"
    deck.write_bytes(b"x")
    out_bib = tmp_path / "out.bib"

    monkeypatch.setattr(
        bibtex,
        "run_ghostcite",
        lambda path: {
            "summary": {"total": 1, "with_doi": 1, "findings": 1},
            "findings": [{"tier": "A", "key": "k", "message": "retracted"}],
        },
    )
    rc = cli.main(["bib", str(deck), "-o", str(out_bib), "--check"])
    assert rc == 1, "a ghostcite finding did not fail the check"
    assert "retracted" in capsys.readouterr().out

    monkeypatch.setattr(
        bibtex,
        "run_ghostcite",
        lambda path: {
            "summary": {"total": 1, "with_doi": 1, "findings": 0},
            "findings": [],
        },
    )
    rc = cli.main(["bib", str(deck), "-o", str(out_bib), "--check"])
    assert rc == 0, "a clean bibliography failed the check"


# ------------------------------------------------- the europe-pmc caveat


def test_the_not_in_europe_pmc_caveat_is_printed_only_when_it_applies(
    monkeypatch, capsys
):
    """`if tally.get("not-in-europe-pmc")` survived mutating the key.

    The caveat distinguishes two very different outcomes -- a DOI that may be
    WRONG, versus a paper that is known but paywalled. Under the mutant the
    key is never found and the explanation never prints, leaving a user to
    read "not-in-europe-pmc" as "paywalled" and stop checking their DOI.
    """
    from figcite import corpus

    class _O:
        def __init__(self, status, doi="10.1/x"):
            self.status, self.doi, self.detail = status, doi, ""

    monkeypatch.setattr(cli, "_corpus_dois", lambda: ["10.1/a", "10.1/b"])
    monkeypatch.setattr(
        corpus,
        "build",
        lambda dois, limit=None: [_O("not-in-europe-pmc"), _O("indexed")],
    )
    cli.main(["corpus", "build"])
    out = capsys.readouterr().out
    assert "can also mean a DOI is wrong" in out, out

    monkeypatch.setattr(corpus, "build", lambda dois, limit=None: [_O("indexed")])
    cli.main(["corpus", "build"])
    out = capsys.readouterr().out
    assert "can also mean a DOI is wrong" not in out, (
        f"the caveat printed for a run that had no such outcome: {out!r}"
    )


# ------------------------------------------------- what goes into a bibliography


def test_records_for_bib_keeps_only_the_pictures_that_have_one(tmp_path, monkeypatch):
    """`[r["record"] for r in audit(...)["rows"] if r["record"]]` survived
    mutating both key names, in BOTH the pptx and the pdf branch.

    My first pass at the `bib` tests stubbed `_records_for_bib` out entirely
    to isolate the output-path rule, which left its body as unexercised as it
    had been before -- a stub can silence the thing you came to measure. The
    keys here are read from a dict the audit built, so a mutated name raises
    KeyError; they survived only because nothing called the function.

    Asymmetric on purpose: one picture with a record and one without, so a
    filter that dropped the wrong half would change the count.
    """
    from figcite import deck, pdfdeck

    rows = [
        {
            "slide": 1,
            "record": _Rec("Kept 2020"),
            "decorative": False,
            "shape": "P1",
            "matched_by": "sha256",
        },
        {
            "slide": 2,
            "record": None,
            "decorative": False,
            "shape": "P2",
            "matched_by": "none",
        },
    ]
    monkeypatch.setattr(deck, "audit", lambda p, **kw: _deck_with(tmp_path, rows))
    monkeypatch.setattr(pdfdeck, "audit", lambda p, **kw: _deck_with(tmp_path, rows))

    for name in ("talk.pptx", "paper.pdf"):
        # The function checks the file exists before reading it, so the bytes
        # are irrelevant but the path must be real.
        (tmp_path / name).write_bytes(b"stand-in; the audit is stubbed")
        got = cli._records_for_bib(str(tmp_path / name))
        assert [r.short_cite for r in got] == ["Kept 2020"], (name, got)


def test_records_for_bib_reads_an_apply_manifest_instead_of_opening_the_deck(tmp_path):
    """The .json branch is authoritative about what actually went INTO a deck,
    including images later removed from the store -- so it must not be routed
    through the deck reader."""
    from figcite.provenance import Record

    rec = Record(doi="10.1/x", citation="C", short_cite="Manifest 2020", confirmed=True)
    p = tmp_path / "applied.json"
    p.write_text(
        json.dumps([{"record": json.loads(rec.to_json())}, {"record": None}]),
        encoding="utf-8",
    )

    got = cli._records_for_bib(str(p))

    assert [r.short_cite for r in got] == ["Manifest 2020"], got


# ------------------------------------------------- the module entry points


@pytest.mark.parametrize("module", ["figcite", "figcite.cli"])
def test_the_package_is_runnable_with_python_m(module):
    """`if __name__ == "__main__"` survived mutating the literal, at both
    entry points -- and the comment on cli.py's own copy records that
    `python -m figcite.cli` once "silently did nothing".

    No in-process test can reach this: under pytest the module's __name__ is
    its import path, so the guard is False either way. It takes a subprocess,
    which is why a whole entry point went unmeasured. Under the mutant the
    module imports, defines everything, and exits 0 having done nothing --
    which is exactly what a user reports as "the command does nothing".

    Run with no arguments: argparse requires a subcommand, so a working entry
    point exits 2 with a usage message. Doing nothing exits 0 with silence,
    and the two are trivially distinguishable.
    """
    import subprocess
    import sys
    from pathlib import Path

    import figcite

    root = Path(figcite.__file__).resolve().parent.parent
    r = subprocess.run(
        [sys.executable, "-m", module],
        cwd=str(root),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert r.returncode != 0, (
        f"`python -m {module}` exited 0 with no subcommand -- the entry point "
        f"ran nothing at all (stdout={r.stdout!r})"
    )
    assert "usage" in (r.stderr + r.stdout).lower(), (r.returncode, r.stderr[:400])
