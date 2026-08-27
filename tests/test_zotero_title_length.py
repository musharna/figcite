"""A two-character library title must not partial-match everything.

`resolve_title` falls back to a substring comparison in BOTH directions,
because a window title is often truncated with an ellipsis and a library title
sometimes carries the subtitle the window dropped:

    q in normalize_title(i["title"]) or normalize_title(i["title"]) in q

That second direction is the dangerous one. Any short library title is a
substring of almost any query, so the comprehension guards it:

    len(normalize_title(i["title"])) >= 8

Forced True by the sweep -- and it survived, because every library fixture in
the suite holds real paper titles, all comfortably longer than eight
characters. Nothing short had ever been in the library.

The query side already has this guard and is tested: `resolve_title` returns
"too short to search the library with" for a short QUERY. The mirror on the
LIBRARY side had no test at all, and it is the one that matters more -- a user
controls what they searched for, not what someone else filed in Zotero years
ago. A note titled "Data" or an item called "SI" would attach itself as a
candidate to every lookup that happened to contain those letters.
"""

from __future__ import annotations


from figcite import zotero

REAL = "AUXIN RESPONSE FACTORs: from transcription factors to auxin effectors"
QUERY = "AUXIN RESPONSE FACTORs: from transcription factors to auxin effectors"


def _library(monkeypatch, items):
    monkeypatch.setattr(zotero, "library", lambda max_age_hours=None: items)


def _item(title, doi):
    return {"key": title[:4], "title": title, "doi": doi, "creators": [], "date": ""}


def test_a_short_library_title_is_not_offered_as_a_partial_match(monkeypatch):
    """ "Data" normalises to four characters and is a substring of the query, so
    without the length guard it becomes a candidate for a paper it has nothing
    to do with."""
    _library(
        monkeypatch,
        [
            _item("Data", "10.1/junk"),
            _item("SI", "10.1/alsojunk"),
        ],
    )

    out = zotero.resolve_title("Auxin data and other things in transcription")

    dois = [c["doi"] for c in out["candidates"]]
    assert "10.1/junk" not in dois, (
        f"a library item titled 'Data' was offered as a partial title match: "
        f"{out['candidates']}"
    )
    assert "10.1/alsojunk" not in dois, out["candidates"]
    assert out["doi"] is None and out["grounded"] is False, out


def test_a_real_title_is_still_matched_by_substring(monkeypatch):
    """Positive control. "Offer nothing" satisfies the test above and removes
    the whole fallback, which exists because window titles get truncated."""
    _library(monkeypatch, [_item(REAL, "10.1111/nph.71477")])

    truncated = REAL[:40]  # what a browser tab shows before the ellipsis
    out = zotero.resolve_title(truncated)

    dois = [c["doi"] for c in out["candidates"]]
    assert "10.1111/nph.71477" in dois, (
        f"a truncated window title no longer finds its paper: {out}"
    )


def test_the_short_titles_really_are_below_the_threshold(monkeypatch):
    """The premise. If these fixtures grew past eight characters they would
    stop exercising the guard while the test above still passed."""
    assert len(zotero.normalize_title("Data")) < 8
    assert len(zotero.normalize_title("SI")) < 8
    assert len(zotero.normalize_title(REAL)) >= 8


def test_the_query_side_guard_is_the_mirror_of_this_one(monkeypatch):
    """Stated so the two are not confused. The query guard rejects a short
    SEARCH; the library guard rejects a short STORED title. Both are needed,
    and only the first had a test."""
    _library(monkeypatch, [_item(REAL, "10.1111/nph.71477")])

    out = zotero.resolve_title("short")

    assert out["doi"] is None
    assert "too short" in out["evidence"], out["evidence"]


# --- which variant matched, and the title that hides it ----------------------


def test_the_matched_variant_is_named_even_for_a_title_with_leading_space(
    monkeypatch,
):
    """`resolve_page_title` appends "(matched on <variant>)" when the form that
    grounded is not the title it was handed.

    `cand != title` narrowed to `<` keeps that note only when the variant sorts
    BELOW the original. Variants are progressively trimmed prefixes, so they
    normally do -- which is why the mutant survived.

    A leading space inverts it. `title_variants` strips before it trims, so for
    " Some Paper | Journal" the first candidate is "Some Paper | Journal", and
    "S" sorts ABOVE " ". Under the mutant every variant compares False and the
    note disappears: the user is told the lookup grounded, but not on what.
    Which variant matched is the difference between a citation a person can
    check and one they have to trust.
    """
    grounded = {
        "doi": "10.1111/nph.71477",
        "grounded": True,
        "evidence": "exact title match in Zotero",
        "candidates": [],
    }
    monkeypatch.setattr(
        zotero, "resolve", lambda cand, max_age_hours=None: dict(grounded)
    )

    out = zotero.resolve_page_title(" Some Paper | Journal")

    assert out["grounded"] is True, out
    assert "matched on" in out["evidence"], (
        f"the grounding variant was not named: {out['evidence']!r}"
    )


def test_no_variant_is_named_when_the_title_itself_matched(monkeypatch):
    """The other half: when the form that grounded IS what was handed in,
    there is nothing to disclose and the note would be noise."""
    grounded = {
        "doi": "10.1111/nph.71477",
        "grounded": True,
        "evidence": "exact title match in Zotero",
        "candidates": [],
    }
    monkeypatch.setattr(
        zotero, "resolve", lambda cand, max_age_hours=None: dict(grounded)
    )

    exact = "Some Paper"
    out = zotero.resolve_page_title(exact)

    assert out["grounded"] is True
    assert "matched on" not in out["evidence"], out["evidence"]


def test_a_leading_space_really_does_invert_the_ordering():
    """The premise, so the test above cannot quietly stop exercising it."""
    variants = zotero.title_variants(" Some Paper | Journal")
    assert variants, "no variants were produced"
    assert all(c != " Some Paper | Journal" for c in variants)
    assert all(not (c < " Some Paper | Journal") for c in variants), (
        f"a variant now sorts below the original; the narrowed guard would "
        f"keep the note and this test would stop covering it: {variants}"
    )
