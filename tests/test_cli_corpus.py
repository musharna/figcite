"""`figcite corpus` and `figcite whereis`.

The CLI's job here is to not lose the distinctions the service worked to keep:
why a DOI is uncovered, and whether an empty answer means "not in your corpus"
or "I could not look".
"""

from figcite import cli, corpus, service


def test_build_reports_every_uncovered_doi_with_its_reason(capsys, monkeypatch):
    monkeypatch.setattr(
        corpus,
        "build",
        lambda dois, limit=None: [
            corpus.BuildOutcome("10.1/a", "PMC1", "indexed"),
            corpus.BuildOutcome("10.1/b", "PMC2", "not-open-access"),
            corpus.BuildOutcome("10.1/c", "", "no-pmc-copy"),
            corpus.BuildOutcome("10.1/d", "", "not-in-europe-pmc"),
            corpus.BuildOutcome("10.1/e", "PMC5", "failed", "connection reset"),
        ],
    )
    monkeypatch.setattr(cli, "_corpus_dois", lambda: ["10.1/%s" % c for c in "abcde"])

    class A:
        limit = None

    cli.cmd_corpus_build(A())
    out = capsys.readouterr().out
    for reason in ("indexed", "not-open-access", "no-pmc-copy", "not-in-europe-pmc"):
        assert reason in out, f"{reason} never reached the user"
    assert "connection reset" in out, "a failure must name its reason"


def test_whereis_prints_could_not_decide_with_the_reason(capsys, monkeypatch):
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {
            "verdict": "could-not-decide",
            "matches": [],
            "reason": "too smooth",
        },
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out
    assert "could not" in out.lower()
    assert "too smooth" in out


def test_whereis_distinguishes_a_real_no_match(capsys, monkeypatch):
    """Positive control: the two outcomes must not print the same words."""
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {"verdict": "no-match", "matches": [], "reason": ""},
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out.lower()
    assert "no match" in out or "not in your corpus" in out
    assert "could not" not in out


def test_whereis_shows_how_each_candidate_was_found(capsys, monkeypatch):
    """An open-tab lead and a pixel match must not read as equal evidence."""
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {
            "verdict": "match",
            "reason": "",
            "matches": [
                {
                    "source": "orb",
                    "score": 91.0,
                    "doi": "10.1/pixel",
                    "title": "A figure",
                    "container": "PMC1",
                    "year": "",
                    "type": "figure",
                    "evidence": True,
                },
                {
                    "source": "open-tab",
                    "score": "",
                    "doi": "10.1/tab",
                    "title": "A tab",
                    "container": "example.org",
                    "year": "",
                    "type": "",
                    "evidence": False,
                },
            ],
        },
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out
    assert "orb" in out and "open-tab" in out
    assert "10.1/pixel" in out and "10.1/tab" in out
    assert "confirm" in out.lower(), "no way to act on the answer"
    # This test's docstring claimed the two must not read as equal evidence
    # and then asserted only that both strings appear -- which a single
    # undifferentiated numbered list satisfies perfectly. It could not fail
    # for the reason it names. The distinction, asserted:
    lead_at = out.index("10.1/tab")
    assert "lead" in out[:lead_at].lower(), (
        "the tab DOI is printed with nothing before it marking it a lead:\n" + out
    )
    assert out.index("10.1/pixel") < lead_at, "a lead outranked the pixel match"


def test_corpus_status_reports_the_size(capsys, monkeypatch):
    monkeypatch.setattr(corpus, "status", lambda: {"figures": 1200, "papers": 205})

    class A:
        pass

    cli.cmd_corpus_status(A())
    out = capsys.readouterr().out
    assert "1200" in out and "205" in out


def _tab(doi="10.1/tab"):
    return {
        "source": "open-tab", "score": "", "doi": doi, "title": "A tab",
        "container": "example.org", "year": "", "type": "", "evidence": False,
    }


def _pixel(doi="10.1/pixel"):
    return {
        "source": "orb", "score": 91.0, "score_label": "91 inliers", "doi": doi,
        "title": "A figure", "container": "PMC1", "year": "", "type": "figure",
        "evidence": True,
    }


def test_a_no_match_still_reports_the_leads_it_was_given(capsys, monkeypatch):
    """The CLI dropped them on the floor.

    `service.whereis` returns open tabs on EVERY verdict -- it ranks them
    last, it does not withhold them. `cmd_whereis` only ever looked at
    `matches` inside its `match` branch, so on a no-match the user was told
    "not in your corpus" and never shown the leads the service had just
    handed over. Information the service worked to produce, discarded by the
    printer.
    """
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {"verdict": "no-match", "reason": "", "matches": [_tab()]},
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out

    assert "not in your corpus" in out, out
    assert "10.1/tab" in out, "the lead the service returned was dropped:\n" + out
    lead_at = out.index("10.1/tab")
    assert "lead" in out[:lead_at].lower(), out


def test_a_no_match_does_not_offer_to_confirm_a_lead(capsys, monkeypatch):
    """The other half of the above, and the reason it is delicate.

    Printing the leads on a no-match is right; inviting the user to cite one
    is not. "accept one with figcite confirm --doi ..." under a verdict that
    found no evidence would turn a tab into a citation, which is the exact
    move this tool exists to refuse.
    """
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {"verdict": "no-match", "reason": "", "matches": [_tab()]},
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out.lower()

    assert "10.1/tab" in out
    assert "confirm" not in out, "offered to cite a lead on a no-match:\n" + out


def test_could_not_decide_keeps_both_its_reason_and_its_leads(capsys, monkeypatch):
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {
            "verdict": "could-not-decide",
            "reason": "too smooth",
            "matches": [_tab()],
        },
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out

    assert "too smooth" in out, out
    assert "10.1/tab" in out, out
    assert "not in your corpus" not in out, out


def test_no_leads_means_no_leads_heading(capsys, monkeypatch):
    """Positive control for the three above: the heading is conditional, so a
    run with nothing to lead with must not print an empty section."""
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {"verdict": "match", "reason": "", "matches": [_pixel()]},
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out

    assert "10.1/pixel" in out, out
    assert "lead" not in out.lower(), "printed an empty leads section:\n" + out
    assert "confirm" in out.lower(), "a real match must still be actionable"


def test_an_unlabelled_candidate_is_treated_as_a_lead(capsys, monkeypatch):
    """Fail safe, not open.

    A candidate arriving without `evidence` -- an older producer, a new
    source added upstream -- must fall to the lead side. Promoting an
    unlabelled thing to evidence is the failure that actually costs
    something; demoting it only under-claims.
    """
    bare = {k: v for k, v in _pixel().items() if k != "evidence"}
    monkeypatch.setattr(
        service,
        "whereis",
        lambda p: {"verdict": "match", "reason": "", "matches": [bare]},
    )

    class A:
        image = "x.png"

    cli.cmd_whereis(A())
    out = capsys.readouterr().out

    lead_at = out.index("10.1/pixel")
    assert "lead" in out[:lead_at].lower(), (
        "an unlabelled candidate was presented as evidence:\n" + out
    )
