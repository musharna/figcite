"""Whether a history match is AMBIGUOUS, tested — it gates citability.

`browser.resolve_from_capture` does:

    out["grounded"] = hit["match"] == "exact-title" and not hit["ambiguous"]

and `grounded` is what lets `service.confirm()` accept a DOI with no explicit
user selection (its `NotGrounded` check refuses otherwise). So the ambiguity
flag is the last thing standing between "two different pages happen to share a
title" and a citation this tool cannot support.

Nothing tested it. Found by mutation sweep: `len(rows) > 1` survives being
changed to `>= 1` AND to `> 2`, and `lookup_by_time`'s permanent
`"ambiguous": True` survives being flipped to False. Both directions matter --
over-flagging makes the tool useless, under-flagging makes it wrong -- so both
are pinned here.
"""

import os
import sqlite3

from figcite import browser


def _places(tmp_path, rows):
    """A minimal places.sqlite. `rows` is [(url, title, visit_epoch_us)]."""
    db = tmp_path / "places.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "create table moz_places (id integer primary key, url text, "
        "title text, last_visit_date integer)"
    )
    con.execute(
        "create table moz_historyvisits (id integer primary key, "
        "place_id integer, visit_date integer)"
    )
    for i, (url, title, ts) in enumerate(rows, start=1):
        con.execute(
            "insert into moz_places (id, url, title, last_visit_date) values (?,?,?,?)",
            (i, url, title, ts),
        )
        con.execute(
            "insert into moz_historyvisits (id, place_id, visit_date) values (?,?,?)",
            (i, i, ts),
        )
    con.commit()
    con.close()
    return db


TITLE = "A Paper About Orchids"


def test_one_page_with_that_title_is_not_ambiguous(tmp_path):
    """The positive control. Every assertion below says some case IS
    ambiguous; without this, a function that hard-coded `ambiguous: True`
    would satisfy all of them."""
    db = _places(tmp_path, [("https://example.org/a", TITLE, 1_000_000)])

    hit = browser.lookup_by_title(db, TITLE)

    assert hit is not None
    assert hit["ambiguous"] is False, hit
    assert hit["url"] == "https://example.org/a"


def test_two_pages_sharing_a_title_are_ambiguous(tmp_path):
    """THE finding.

    Two distinct URLs, one title. figcite cannot know which was on screen, and
    saying so is the entire difference between a citation it can support and a
    guess. `len(rows) > 2` passes every other test in the suite.
    """
    db = _places(
        tmp_path,
        [
            ("https://example.org/a", TITLE, 2_000_000),
            ("https://elsewhere.test/b", TITLE, 1_000_000),
        ],
    )

    hit = browser.lookup_by_title(db, TITLE)

    assert hit is not None
    assert hit["ambiguous"] is True, (
        "two different URLs share this title and the match was reported "
        f"unambiguous, which makes it citable: {hit}"
    )


def test_ambiguity_is_what_stops_a_match_being_grounded(tmp_path, monkeypatch):
    """The consequence, asserted end-to-end rather than left as a comment.

    A test on the flag alone would keep passing if someone dropped the
    `not hit["ambiguous"]` conjunct from `resolve_from_capture`.
    """
    db = _places(
        tmp_path,
        [
            ("https://example.org/a", TITLE, 2_000_000),
            ("https://elsewhere.test/b", TITLE, 1_000_000),
        ],
    )
    monkeypatch.setattr(browser, "firefox_profiles", lambda: [tmp_path])
    monkeypatch.setattr(browser, "snapshot_history", lambda p: db)
    monkeypatch.setattr(
        browser, "url_to_doi", lambda url, allow_fetch=True: ("10.1/x", "a stub")
    )

    out = browser.resolve_from_capture(
        {"title": TITLE, "process": "firefox"}, allow_fetch=False
    )

    assert out["doi"] == "10.1/x", out
    assert out["grounded"] is False, (
        "an ambiguous title match was reported as grounded, which is what "
        f"lets service.confirm() accept it with no user selection: {out}"
    )
    assert "guess" in out["evidence"].lower(), out


def test_a_single_match_is_grounded(tmp_path, monkeypatch):
    """Positive control for the above: if `resolve` grounded nothing at all,
    the assertion above would pass while the feature was dead."""
    db = _places(tmp_path, [("https://example.org/a", TITLE, 1_000_000)])
    monkeypatch.setattr(browser, "firefox_profiles", lambda: [tmp_path])
    monkeypatch.setattr(browser, "snapshot_history", lambda p: db)
    monkeypatch.setattr(
        browser, "url_to_doi", lambda url, allow_fetch=True: ("10.1/x", "a stub")
    )

    out = browser.resolve_from_capture(
        {"title": TITLE, "process": "firefox"}, allow_fetch=False
    )

    assert out["grounded"] is True, out


def test_a_nearest_visit_match_is_always_ambiguous(tmp_path):
    """`lookup_by_time`'s docstring calls itself "A GUESS about which tab was
    showing". The flag saying so flips to False with nothing failing.

    Contained today by resolve_from_capture's `match == "exact-title"` conjunct, so it
    cannot currently produce false grounding -- but nothing enforces that
    containment, and the flag reaches the user.
    """
    db = _places(tmp_path, [("https://example.org/a", TITLE, 10_000_000)])

    hit = browser.lookup_by_time(db, when_epoch=10.0, window_s=180)

    assert hit is not None, "premise: a visit falls inside the window"
    assert hit["match"] == "nearest-visit"
    assert hit["ambiguous"] is True, (
        f"a time-window guess was reported unambiguous: {hit}"
    )


def test_the_newest_firefox_profile_is_searched_first(tmp_path, monkeypatch):
    """F4: `firefox_profiles` promises "newest history first" and nothing
    checked it.

    `reverse=True` on the mtime sort survives being flipped, which makes the
    function return the OLDEST profile first -- so figcite reads stale history
    and reports "no history entry titled ..." for a page that is in the other
    profile. Not hypothetical: this machine has two profiles.
    """
    from figcite import clipboard

    root = tmp_path / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    old = root / "aaaa.old"
    new = root / "zzzz.new"
    for d in (old, new):
        d.mkdir(parents=True)
        (d / "places.sqlite").write_bytes(b"x")
    # Names are deliberately ordered opposite to the mtimes, so an
    # implementation that sorted by NAME (or not at all) cannot pass by luck.
    os.utime(old / "places.sqlite", (1_000_000, 1_000_000))
    os.utime(new / "places.sqlite", (2_000_000, 2_000_000))

    monkeypatch.setattr(browser, "_win_userprofile", lambda: r"C:\Users\someone")
    monkeypatch.setattr(clipboard, "win_to_wsl", lambda p: str(tmp_path))

    profs = browser.firefox_profiles()

    assert [p.name for p in profs] == ["zzzz.new", "aaaa.old"], (
        f"profiles are not newest-first, so the stale one is searched: {profs}"
    )


def test_a_profile_without_a_places_db_is_not_offered(tmp_path, monkeypatch):
    """Positive control for the above: the filter has to actually filter, or
    the ordering assertion is being made over a list built by accident."""
    from figcite import clipboard

    root = tmp_path / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    real = root / "has-db"
    empty = root / "no-db"
    real.mkdir(parents=True)
    empty.mkdir(parents=True)
    (real / "places.sqlite").write_bytes(b"x")

    monkeypatch.setattr(browser, "_win_userprofile", lambda: r"C:\Users\someone")
    monkeypatch.setattr(clipboard, "win_to_wsl", lambda p: str(tmp_path))

    assert [p.name for p in browser.firefox_profiles()] == ["has-db"]
