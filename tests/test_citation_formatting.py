"""The author-count rule, and the thresholds around it -- in both copies.

`crossref.format_citation` and `zotero._short_cite` implement the same
convention independently: one author is named, two are joined with "&", three
or more become "et al.". Neither had a test that varied the count, so in both
modules the boundaries could move by one and nothing noticed.

This is citation text. A figure credited to "Smith & Jones 2020" when the
paper is by Smith, Jones and Okafor is a wrong citation in a talk, and it is
wrong in a way no checker downstream can catch -- the DOI is right, so
ghostcite passes it.

Four counts are needed, not three: zero, one, two, and three. Each boundary
mutant (`== 1` -> `== 2`, `== 2` -> `== 3`, `>` -> `>=`) leaves at least one
of those four unchanged, so a fixture short of all four cannot separate them.
"""

from __future__ import annotations

import time

import pytest

from figcite import crossref, zotero


# ------------------------------------------------ crossref.format_citation


def _msg(*authors, year=2020, title="A paper about pollen"):
    return {
        "author": list(authors),
        "issued": {"date-parts": [[year]]},
        "title": [title],
        "container-title": ["Journal of Things"],
    }


def _a(family=None, given=None, name=None):
    out = {}
    if family:
        out["family"] = family
    if given:
        out["given"] = given
    if name:
        out["name"] = name
    return out


@pytest.mark.parametrize(
    "authors,expect_short",
    [
        ([], None),  # falls back to the publisher
        ([_a("Smith", "A")], "Smith 2020"),
        ([_a("Smith", "A"), _a("Jones", "B")], "Smith & Jones 2020"),
        ([_a("Smith", "A"), _a("Jones", "B"), _a("Okafor", "C")], "Smith et al. 2020"),
    ],
    ids=["none", "one", "two", "three"],
)
def test_the_author_count_picks_the_citation_form(authors, expect_short):
    """`if not auth / elif len(auth) == 1 / elif len(auth) == 2 / else`
    survived five mutants between them.

    Under `== 2` in the first branch a two-author paper is credited to the
    first author alone; under `== 3` in the second, a three-author paper is
    credited to "Smith & Jones" and the third author vanishes from the slide.
    """
    msg = _msg(*authors)
    full, short = crossref.format_citation(msg)

    if expect_short is None:
        assert "Smith" not in short, short
    else:
        assert short == expect_short, short


def test_a_paper_with_no_authors_is_credited_to_its_publisher():
    """The `not auth` branch, stated on its own. "Anon." rather than an empty
    string, so a credit line never renders as a bare year."""
    msg = _msg(year=2019)
    msg["publisher"] = "Some Society"

    full, short = crossref.format_citation(msg)

    assert "Some Society" in short, short


def test_an_author_with_no_given_name_is_not_credited_to_None():
    """`if fam and giv` survived `And -> Or`.

    CrossRef records consortium and single-name authors with `family` and no
    `given`. Under `or` the first branch fires anyway and the citation
    formats an f-string over a missing value -- "Consortium, None".
    """
    got = crossref._authors(_msg(_a("Smith", "A"), _a(family="Consortium")))

    assert got == ["Smith, A", "Consortium"], got
    assert not any("None" in g for g in got), got


def test_an_author_recorded_only_as_a_name_is_still_credited():
    """`elif a.get("name")` survived mutating the key.

    Group authors ("The 1000 Genomes Project Consortium") arrive with neither
    family nor given, only `name`. Under the mutant they are dropped from the
    author list entirely -- and dropping every author is what sends
    `format_citation` down its publisher fallback, so a real paper gets
    credited to its journal.
    """
    got = crossref._authors(_msg(_a(name="The Pollen Consortium")))

    assert got == ["The Pollen Consortium"], got


# ------------------------------------------------ zotero._short_cite


@pytest.mark.parametrize(
    "creators,expect",
    [
        ([], "2020"),
        (["Smith"], "Smith 2020"),
        (["Smith", "Jones"], "Smith & Jones 2020"),
        (["Smith", "Jones", "Okafor"], "Smith et al. 2020"),
    ],
    ids=["none", "one", "two", "three"],
)
def test_zotero_short_cite_uses_the_same_rule(creators, expect):
    """The second implementation of the same convention, with the same five
    mutants alive in it. Tested here beside the first so the two cannot drift
    apart silently -- they are the strings a reader compares when a figure's
    credit looks wrong."""
    item = {"creators": list(creators), "date": "2020-05-01"}

    assert zotero._short_cite(item) == expect


def test_the_two_implementations_agree_on_the_same_paper():
    """Neither module imports the other's formatter, so nothing but a test
    holds them to the same convention."""
    names = ["Smith", "Jones", "Okafor"]
    z = zotero._short_cite({"creators": names, "date": "2020"})
    _full, c = crossref.format_citation(_msg(*[_a(n, "X") for n in names], year=2020))

    assert z == c == "Smith et al. 2020", (z, c)


# ------------------------------------------------ crossref: the rate limiter


def test_the_throttle_waits_for_a_partial_interval(monkeypatch):
    """`if wait > 0` survived `0 -> 1`.

    The interval is 1.05s, so under `> 1` any wait SHORTER than a second is
    skipped -- which is every wait that matters. CrossRef's limit is what this
    exists for, and the module's own comment records that blowing through it
    made every 429 read as "no DOI".
    """
    naps = []
    monkeypatch.setattr(crossref.time, "sleep", lambda s: naps.append(s))
    monkeypatch.setattr(crossref.requests, "get", lambda url, **kw: _R200())
    monkeypatch.setattr(crossref, "_last_call", time.monotonic() - 0.5, raising=False)

    crossref.throttled_get("https://api.crossref.org/works/10.1/x")

    assert naps and 0 < naps[0] < 1, (
        f"a sub-second wait was skipped, so the rate limit is not honoured: {naps}"
    )


def test_the_throttle_does_not_sleep_when_the_interval_has_passed(monkeypatch):
    """`wait > 0` survived `Gt -> GtE`, which sleeps for zero seconds at the
    exact boundary. Pinned by freezing the clock so `wait` is exactly 0 --
    the only way to reach that edge deliberately."""
    naps = []
    frozen = 1000.0
    monkeypatch.setattr(crossref.time, "sleep", lambda s: naps.append(s))
    monkeypatch.setattr(crossref.time, "monotonic", lambda: frozen)
    monkeypatch.setattr(crossref.requests, "get", lambda url, **kw: _R200())
    # Exactly zero, not nearly zero. Deriving `_last_call` from a subtraction
    # left `wait` at 4.5e-14 and the boundary was never actually reached --
    # the test failed for float reasons rather than the reason it names.
    # FIGCITE_CROSSREF_MIN_INTERVAL=0 is a real setting (throttling off), so
    # this is a state the code genuinely has to handle.
    monkeypatch.setattr(crossref, "_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(crossref, "_last_call", frozen, raising=False)

    crossref.throttled_get("https://api.crossref.org/works/10.1/x")

    assert naps == [], f"slept at exactly the interval boundary: {naps}"


class _R200:
    status_code = 200
    headers: dict = {}

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"DOI": "10.1/x"}}


# ------------------------------------------------ crossref: DOI trimming


@pytest.mark.parametrize(
    "text,expect",
    [
        ("see 10.1111/nph.71477.", "10.1111/nph.71477"),
        ("see 10.1111/nph.71477,", "10.1111/nph.71477"),
        ("see 10.1111/nph.71477;", "10.1111/nph.71477"),
        ("see 10.1111/nph.71477:", "10.1111/nph.71477"),
    ],
    ids=[".", ",", ";", ":"],
)
def test_prose_punctuation_is_trimmed_off_a_doi(text, expect):
    """`while d and d[-1] in ".,;:": d = d[:-1]` survived `1 -> 2` and
    mutating the character set.

    Under `d[:-2]` one trailing full stop takes a real character of the DOI
    with it, and the result resolves to nothing -- a citation that looks
    right and is not. Each punctuation mark is driven separately, because a
    mutated character SET still contains whichever mark the single test case
    happened to use.
    """
    assert crossref.find_dois(text) == [expect]


def test_a_doi_with_no_trailing_punctuation_is_left_alone():
    """Positive control: "always strip a character" passes the cases above."""
    assert crossref.find_dois("see 10.1111/nph.71477 here") == ["10.1111/nph.71477"]


# ------------------------------------------------ crossref: cache and 404


def test_use_cache_false_actually_bypasses_the_cache(tmp_path, monkeypatch):
    """`if use_cache and cf.exists()` survived `And -> Or`.

    Under `or` an existing cache file is read even when the caller explicitly
    asked not to use it -- so `use_cache=False`, which exists to force a
    refetch, silently returns the stale answer it was called to replace.
    """
    monkeypatch.setattr(crossref, "CACHE", tmp_path)
    cf = crossref._cache_file("10.1/x")
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text('{"DOI": "10.1/x", "title": ["STALE"]}', encoding="utf-8")

    class _Fresh(_R200):
        def json(self):
            return {"message": {"DOI": "10.1/x", "title": ["FRESH"]}}

    monkeypatch.setattr(crossref, "throttled_get", lambda url, **kw: _Fresh())

    got = crossref.fetch_work("10.1/x", use_cache=False)

    assert got["title"] == ["FRESH"], (
        f"use_cache=False read the cache anyway: {got['title']}"
    )


def test_the_cache_is_used_when_it_is_allowed(tmp_path, monkeypatch):
    """Positive control: "never read the cache" passes the test above and
    turns every lookup into a throttled network call."""
    monkeypatch.setattr(crossref, "CACHE", tmp_path)
    cf = crossref._cache_file("10.1/y")
    cf.parent.mkdir(parents=True, exist_ok=True)
    cf.write_text('{"DOI": "10.1/y", "title": ["CACHED"]}', encoding="utf-8")
    monkeypatch.setattr(
        crossref,
        "throttled_get",
        lambda url, **kw: pytest.fail("the network was used despite a warm cache"),
    )

    assert crossref.fetch_work("10.1/y")["title"] == ["CACHED"]


def test_a_404_is_a_miss_and_anything_else_is_an_error(tmp_path, monkeypatch):
    """`if r.status_code == 404` survived `404 -> 405`.

    404 is the ONE status that means "CrossRef has no such work" -- an
    answer. Everything else is a failure to get an answer, and the two must
    not collapse: this module exists because an outage read as an absence
    once already. Under `== 405` a real 404 falls through to
    `raise_for_status`, and a 405 becomes a silent "no such DOI".
    """
    monkeypatch.setattr(crossref, "CACHE", tmp_path)

    class _R:
        def __init__(self, code):
            self.status_code = code

        def raise_for_status(self):
            import requests as rq

            raise rq.exceptions.HTTPError(str(self.status_code))

        def json(self):
            return {"message": {}}

    monkeypatch.setattr(crossref, "throttled_get", lambda url, **kw: _R(404))
    assert crossref.fetch_work("10.1/missing") is None, (
        "a 404 was not reported as a miss"
    )

    import requests as rq

    monkeypatch.setattr(crossref, "throttled_get", lambda url, **kw: _R(405))
    with pytest.raises(rq.exceptions.HTTPError):
        crossref.fetch_work("10.1/broken")


# ------------------------------------------------ zotero: credentials


def test_either_half_of_the_credentials_missing_is_not_configured(monkeypatch):
    """`if not key or not lib` survived `Or -> And`.

    Under `and` only a COMPLETELY empty configuration is refused, so a key
    with no library id (or a library id with no key) sails through and the
    failure surfaces later as an HTTP error -- reported to the user as an
    outage rather than as "you have not finished configuring this".
    """
    for var in (
        "FIGCITE_ZOTERO_API_KEY",
        "ZOTERO_API_KEY",
        "FIGCITE_ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_ID",
        "FIGCITE_ZOTERO_LIBRARY_TYPE",
        "ZOTERO_LIBRARY_TYPE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(zotero, "_from_file", lambda: (None, None, None))

    monkeypatch.setenv("FIGCITE_ZOTERO_API_KEY", "k")  # key but no library
    assert zotero.configured() is False, "a key with no library id read as configured"

    monkeypatch.delenv("FIGCITE_ZOTERO_API_KEY")
    monkeypatch.setenv("FIGCITE_ZOTERO_LIBRARY_ID", "6532713")  # library but no key
    assert zotero.configured() is False, "a library id with no key read as configured"

    monkeypatch.setenv("FIGCITE_ZOTERO_API_KEY", "k")  # both
    assert zotero.configured() is True, "a complete configuration was refused"


# ------------------------------------------------ zotero: title thresholds


def test_a_title_exactly_at_the_minimum_length_is_searched(monkeypatch):
    """`if len(q) < 8` survived `Lt -> LtE` and `8 -> 9`.

    Eight normalized characters is the shortest title still worth searching
    for, not the first length refused. Both mutants refuse a title of exactly
    eight -- and the refusal is reported as "too short to search the library
    with", so the user is told the query was the problem when it was not.
    """
    q8 = "abcdefgh"
    assert len(zotero.normalize_title(q8)) == 8, zotero.normalize_title(q8)
    monkeypatch.setattr(zotero, "library", lambda max_age_hours=None: [])

    out = zotero.resolve_title(q8)

    assert "too short" not in out["evidence"], (
        f"a title of exactly 8 characters was refused as too short: {out['evidence']!r}"
    )


def test_a_title_one_character_short_is_refused(monkeypatch):
    """Positive control for that floor."""
    monkeypatch.setattr(
        zotero, "library", lambda max_age_hours=None: pytest.fail("should not search")
    )

    out = zotero.resolve_title("abcdefg")

    assert "too short" in out["evidence"], out["evidence"]


def test_one_exact_title_match_is_not_ambiguous(monkeypatch):
    """`if len(exact) > 1` survived `Gt -> GtE`.

    Under `>=` a SINGLE exact match is reported as "1 Zotero items share this
    title -- ambiguous", so the one case that should ground confidently never
    does, and every match becomes a guess for the user to confirm by hand.
    """
    monkeypatch.setattr(
        zotero,
        "library",
        lambda max_age_hours=None: [
            {
                "title": "A study of orchids",
                "doi": "10.1/o",
                "key": "K1",
                "doi_source": "doi-field",
                "creators": ["Smith"],
                "date": "2020",
            }
        ],
    )

    out = zotero.resolve_title("A study of orchids")

    assert "ambiguous" not in out["evidence"], out["evidence"]
    assert out.get("grounded") is True, out


def test_two_items_sharing_a_title_are_ambiguous(monkeypatch):
    """Positive control: the boundary has to be reachable from both sides.

    The fixture used to be `[item, {**item, "key": "K2"}]` -- the same record
    twice, differing only in key. That is a DUPLICATE, not an ambiguity: both
    records name one paper, so there is no competing answer to be ambiguous
    between, and `resolve_title` now grounds it (see
    tests/test_zotero_duplicate_records.py).

    What this test is for is unchanged and still reachable: two DIFFERENT works
    filed under one title, which is the case the uniqueness rule was written
    for and which still refuses to ground.
    """
    item = {
        "title": "A study of orchids",
        "doi": "10.1/o",
        "key": "K1",
        "doi_source": "doi-field",
        "creators": ["Smith"],
        "date": "2020",
    }
    other = {**item, "key": "K2", "doi": "10.2/different-work"}
    monkeypatch.setattr(zotero, "library", lambda max_age_hours=None: [item, other])

    out = zotero.resolve_title("A study of orchids")

    assert "ambiguous" in out["evidence"], out["evidence"]
    assert not out.get("grounded"), out


def test_a_library_title_of_exactly_eight_characters_can_still_match(monkeypatch):
    """`len(normalize_title(i["title"])) >= 8` survived `GtE -> Gt` and
    `8 -> 9`.

    The substring fallback exists because window titles are truncated and
    library titles carry subtitles. The length floor is there to stop a
    two-word library title matching everything -- but at exactly eight it
    should still be usable, and both mutants exclude it.
    """
    monkeypatch.setattr(
        zotero,
        "library",
        lambda max_age_hours=None: [
            {
                "title": "abcdefgh",
                "doi": "10.1/short",
                "key": "K1",
                "doi_source": "doi-field",
                "creators": ["Smith"],
                "date": "2020",
            }
        ],
    )

    out = zotero.resolve_title("abcdefgh -- the extended version")

    assert out["candidates"], (
        f"an 8-character library title was excluded from the substring "
        f"fallback: {out['evidence']!r}"
    )


# ------------------------------------------------ zotero: browser tab titles


def test_an_empty_tab_title_yields_no_variants_to_search():
    """`if cur and cur.lower() not in seen` survived `And -> Or`.

    A browser tab with no title is ordinary -- a blank page, a PDF viewer, a
    page still loading. Under `or` the empty string passes the guard and is
    appended as a candidate title, so `resolve_page_title` goes on to look
    the empty string up in the library. The `cur and` operand is what makes
    "no title" mean "nothing to search for" rather than "search for nothing".
    """
    assert zotero.title_variants("") == []
    assert zotero.title_variants("   ") == []
    assert zotero.title_variants(None) == []


def test_a_real_title_still_produces_its_variants():
    """Positive control: "return nothing" passes the test above and disables
    the whole tab-title route."""
    got = zotero.title_variants("A paper about pollen | Nature")

    assert got == ["A paper about pollen | Nature", "A paper about pollen"], got


def test_a_site_suffix_of_exactly_the_word_limit_is_still_stripped():
    """`if len(tail.split()) > _MAX_TAIL_WORDS: break` survived `Gt -> GtE`.

    The word limit distinguishes a site suffix ("| Nature") from a title that
    merely contains a pipe -- eight words is the longest thing still treated
    as a suffix, not the first length refused. Under `>=` a suffix of exactly
    eight words stops the split, so the bare title is never offered and the
    lookup runs only against the full window title, which never matches a
    library record.
    """
    eight = "one two three four five six seven eight"
    got = zotero.title_variants(f"Real Title | {eight}")

    assert "Real Title" in got, (
        f"a {len(eight.split())}-word suffix was not stripped: {got}"
    )


def test_a_suffix_one_word_over_the_limit_is_left_attached():
    """Positive control: at nine words it is far more likely to be part of
    the title than a site name."""
    nine = "one two three four five six seven eight nine"
    got = zotero.title_variants(f"Real Title | {nine}")

    assert got == [f"Real Title | {nine}"], got


def _lib(monkeypatch, items):
    monkeypatch.setattr(zotero, "library", lambda max_age_hours=None: items)


def _item(title, doi="10.1/x", key="K1"):
    return {
        "title": title,
        "doi": doi,
        "key": key,
        "doi_source": "doi-field",
        "creators": ["Smith"],
        "date": "2020",
    }


def test_the_evidence_names_the_variant_it_matched_on(monkeypatch):
    """`f" (matched on {cand!r})" if cand != title else ""` survived
    `NotEq -> Eq` and mutating the literal.

    When the match came from a STRIPPED form of the window title rather than
    the title itself, saying so is how a reader checks the tool did not match
    on a site name. Inverted, the note appears exactly when the two are the
    same -- "(matched on 'X')" printed next to X -- and disappears in the one
    case it carries information.
    """
    _lib(monkeypatch, [_item("A paper about pollen")])

    out = zotero.resolve_page_title("A paper about pollen | Nature")

    assert out["grounded"] is True, out
    assert "matched on" in out["evidence"], out["evidence"]
    assert "A paper about pollen" in out["evidence"], out["evidence"]


def test_a_title_that_matched_as_typed_is_not_annotated(monkeypatch):
    """The other half of the same conditional: no note when there is nothing
    to explain."""
    _lib(monkeypatch, [_item("A paper about pollen")])

    out = zotero.resolve_page_title("A paper about pollen")

    assert out["grounded"] is True, out
    assert "matched on" not in out["evidence"], out["evidence"]


def test_a_variant_with_candidates_is_preferred_over_one_with_none(monkeypatch):
    """`if r.get("candidates") and not (first.get("candidates"))` survived
    `And -> Or`, dropping the `not`, and mutating both key names.

    When no variant grounds, the result returned is the most USEFUL failure:
    one carrying candidates a human can pick from beats one carrying none.
    The full window title matches nothing; the stripped title matches two
    items ambiguously. Under the mutants the empty first result is kept and
    the user is told nothing was found, while the candidates that were found
    are discarded.
    """
    # No DOI on either item, deliberately. The substring fallback only
    # considers items that HAVE a DOI, so the full window title finds nothing
    # at all -- which is what makes the first variant's result the empty one
    # and lets the preference rule be observed. With DOIs present the full
    # title substring-matches too, both variants carry candidates, and the
    # branch under test is never reached.
    _lib(
        monkeypatch,
        [
            _item("A paper about pollen", doi="", key="K1"),
            _item("A paper about pollen", doi="", key="K2"),
        ],
    )

    out = zotero.resolve_page_title("A paper about pollen | Nature")

    assert not out["grounded"], out
    assert out["candidates"], (
        f"the variant that found candidates was discarded in favour of the "
        f"one that found none: {out['evidence']!r}"
    )
    assert "ambiguous" in out["evidence"], out["evidence"]


# ------------------------------------------------ zotero: cache freshness


def test_a_snapshot_exactly_at_the_ttl_is_still_fresh(tmp_path, monkeypatch):
    """`if age_h <= ttl` survived `LtE -> Lt`.

    The TTL is the age at which a snapshot is still usable, not the first age
    refused. Under `<` a cache that has just reached its TTL is refetched --
    which on this path means a full library sync, throttled, on a boundary
    that a long-running process crosses repeatedly.

    The clock is frozen and the mtime set from it, so the age is exactly the
    TTL rather than nearly it: deriving one from the other through
    floating-point subtraction leaves the boundary unreached and the test
    passes for the wrong reason.
    """
    import json
    import os

    now = 1_000_000.0
    cf = tmp_path / "snap.json"
    cf.write_text(json.dumps({"items": [{"title": "cached"}]}), encoding="utf-8")
    os.utime(cf, (now - 3600.0, now - 3600.0))  # exactly one hour old

    monkeypatch.setattr(zotero.time, "time", lambda: now)
    monkeypatch.setattr(zotero, "credentials", lambda: ("k", "6532713", "group"))
    monkeypatch.setattr(zotero, "_cache_file", lambda lib, typ: cf)
    monkeypatch.setattr(
        zotero, "sync", lambda *a, **kw: pytest.fail("resynced a snapshot at its TTL")
    )

    got = zotero.library(max_age_hours=1.0)

    assert got == [{"title": "cached"}], got


def test_a_snapshot_past_the_ttl_is_refetched(tmp_path, monkeypatch):
    """Positive control: "always use the cache" passes the test above and the
    library never refreshes again."""
    import json
    import os

    now = 1_000_000.0
    cf = tmp_path / "snap.json"
    cf.write_text(json.dumps({"items": [{"title": "stale"}]}), encoding="utf-8")
    os.utime(cf, (now - 3601.0, now - 3601.0))

    monkeypatch.setattr(zotero.time, "time", lambda: now)
    monkeypatch.setattr(zotero, "credentials", lambda: ("k", "6532713", "group"))
    monkeypatch.setattr(zotero, "_cache_file", lambda lib, typ: cf)
    monkeypatch.setattr(
        zotero, "fetch_library", lambda progress=False: [{"title": "fresh", "doi": ""}]
    )
    monkeypatch.setattr(zotero, "CACHE_DIR", tmp_path)

    got = zotero.library(max_age_hours=1.0)

    assert got == [{"title": "fresh", "doi": ""}], got


def test_the_first_variant_that_found_candidates_is_the_one_kept(monkeypatch):
    """The other two mutants on the same line: `And -> Or`, and mutating the
    key on `first.get("candidates")`.

    The rule is "upgrade an empty result to one with candidates", and it must
    fire ONCE. Both mutants make it fire whenever the LATER variant has
    candidates, so the more specific match found on the full window title is
    thrown away for whatever the stripped title turned up -- the tool
    silently prefers the less informative of two answers.

    Two ambiguous pairs, one matching each variant, so both results carry
    candidates and only the choice between them varies. Nothing grounds, or
    `resolve_page_title` would return before reaching this line.
    """
    full = _item("Alpha beta gamma delta epsilon", doi="", key="FULL1")
    stripped = _item("Alpha beta gamma", doi="", key="STRIP1")
    _lib(
        monkeypatch,
        [
            full,
            {**full, "key": "FULL2"},
            stripped,
            {**stripped, "key": "STRIP2"},
        ],
    )

    out = zotero.resolve_page_title("Alpha beta gamma | delta epsilon")

    keys = sorted(c["zotero_key"] for c in out["candidates"])
    assert keys == ["FULL1", "FULL2"], (
        f"the full-title match was replaced by the stripped-title one: {keys}"
    )


def test_when_no_variant_finds_anything_the_first_answer_is_the_one_given(
    monkeypatch,
):
    """`And -> Or` again, from the other side. With NEITHER variant finding
    candidates, `not first.get("candidates")` is true on its own, so under
    `or` the last variant's answer replaces the first -- and the message
    names the stripped title rather than the one the user was looking at."""
    _lib(monkeypatch, [_item("Something else entirely", key="K9")])

    out = zotero.resolve_page_title("Alpha beta gamma | delta epsilon")

    assert not out["candidates"], out
    assert "Alpha beta gamma | delta epsilon" in out["evidence"], (
        f"the failure was reported against a variant rather than the title the "
        f"user actually had on screen: {out['evidence']!r}"
    )
