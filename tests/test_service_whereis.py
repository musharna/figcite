"""whereis: the one entry point both front ends call."""

from PIL import Image

from figcite import corpus, match, service, session_tabs


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (40, 30), "blue").save(path)
    return path


def _no_corpus(monkeypatch):
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [object()])


def test_a_pixel_match_is_returned_as_a_candidate(tmp_path, monkeypatch):
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match(
            "10.1/found", "PMC9", "Figure 3", "dhash", 0.0, 9.0
        ),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "match"
    assert out["matches"][0]["doi"] == "10.1/found"
    assert out["matches"][0]["source"] == "dhash"


def test_open_tabs_are_offered_but_rank_below_a_pixel_match(tmp_path, monkeypatch):
    """A tab is a lead about where a figure came from, not evidence."""
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match(
            "10.1/pixel", "PMC9", "Figure 1", "dhash", 0.0, 9.0
        ),
    )
    monkeypatch.setattr(
        session_tabs,
        "tab_candidates",
        lambda path=None: [
            {
                "source": "open-tab",
                "score": "",
                "doi": "10.1/tab",
                "title": "T",
                "container": "example.org",
                "year": "",
                "type": "",
            }
        ],
    )

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert [m["doi"] for m in out["matches"]] == ["10.1/pixel", "10.1/tab"]


def test_could_not_decide_is_not_reported_as_no_match(tmp_path, monkeypatch):
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match, "by_dhash", lambda b, rows: match.CouldNotDecide("too smooth")
    )
    monkeypatch.setattr(
        match,
        "by_orb",
        lambda b, rows, root, descriptor_dir=None: match.CouldNotDecide("too smooth"),
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "could-not-decide"
    assert "too smooth" in out["reason"]


def test_a_real_no_match_is_reported_as_such(tmp_path, monkeypatch):
    """Positive control for the test above: no-match must stay reachable.

    An ORB NoMatch is a real search of the corpus, so it outranks dhash's
    could-not-decide, which only ever meant "a crop is invisible to me".
    """
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match, "by_dhash", lambda b, rows: match.CouldNotDecide("no dhash hit")
    )
    monkeypatch.setattr(
        match, "by_orb", lambda b, rows, root, descriptor_dir=None: match.NoMatch()
    )
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "no-match"


def test_a_broken_tab_scan_does_not_sink_a_good_pixel_match(tmp_path, monkeypatch):
    """Open tabs are a garnish. Failing to read them must not lose the answer."""
    _no_corpus(monkeypatch)
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match(
            "10.1/pixel", "PMC9", "Figure 1", "dhash", 0.0, 9.0
        ),
    )

    def boom(path=None):
        raise RuntimeError("session store unreadable")

    monkeypatch.setattr(session_tabs, "tab_candidates", boom)

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "match"
    assert [m["doi"] for m in out["matches"]] == ["10.1/pixel"]


def test_an_empty_corpus_says_so_once_and_actionably(tmp_path, monkeypatch):
    """The first message a new user sees, so it must not compound reasons.

    Running ORB over zero rows adds "no corpus figure could be read for
    comparison", which implies the files are unreadable rather than absent.
    """
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [])
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])

    out = service.whereis(str(_png(tmp_path / "q.png")))
    assert out["verdict"] == "could-not-decide"
    assert "corpus build" in out["reason"], "the reason must say what to do"
    assert "could be read" not in out["reason"], out["reason"]
