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
                },
                {
                    "source": "open-tab",
                    "score": "",
                    "doi": "10.1/tab",
                    "title": "A tab",
                    "container": "example.org",
                    "year": "",
                    "type": "",
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


def test_corpus_status_reports_the_size(capsys, monkeypatch):
    monkeypatch.setattr(corpus, "status", lambda: {"figures": 1200, "papers": 205})

    class A:
        pass

    cli.cmd_corpus_status(A())
    out = capsys.readouterr().out
    assert "1200" in out and "205" in out
