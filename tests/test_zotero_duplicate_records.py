"""Duplicate RECORDS are not ambiguity about the ANSWER.

`resolve_title` grounds a title only when the library holds exactly one item
with it. The reason is sound and stated in its docstring: two items sharing a
title makes the match a guess, and a guess is least defensible precisely when
it looks confident.

But the question asked is "what DOI does this title resolve to", and the
answer is a DOI, not an item. A reference manager permits duplicates -- Zotero
ships a duplicate-detection and merge screen because of it -- so one paper
saved twice is the ordinary case, not a warning sign. When every record
bearing the title names the SAME canonical DOI, there is no competing answer
to be ambiguous between.

Measured on the live library this was written against: 4,923 items, of which

    17 titles  every DOI-bearing record agrees on one DOI   <- refused today
     5 titles  DOI-bearing records name DIFFERENT DOIs      <- must stay refused
    24 titles  one DOI plus DOI-less twins                  <- still refused

Only the first case changes. The third is deliberately left alone: a record
with no DOI names no competing answer, but it also cannot confirm that it is
the same work, and grounding is this project's highest bar. That is a
judgement call about someone's library, not a fact about the code, so it stays
with the person who owns the library.
"""

from __future__ import annotations


from figcite import zotero


def _item(key, title, doi=""):
    return {
        "key": key,
        "title": title,
        "doi": doi,
        "creators": ["Someone"],
        "date": "2020",
        "doi_source": "doi-field" if doi else "",
    }


TITLE = "A Paper About Something"
DOI = "10.1111/nph.71477"


def _library(monkeypatch, items):
    monkeypatch.setattr(zotero, "library", lambda max_age_hours=None: items)


def test_one_record_still_grounds(monkeypatch):
    """The existing behaviour, unchanged."""
    _library(monkeypatch, [_item("AAA", TITLE, DOI)])

    out = zotero.resolve_title(TITLE)

    assert out["grounded"] is True
    assert out["doi"] == DOI


def test_duplicate_records_naming_one_doi_ground(monkeypatch):
    """The change. Three saves of one paper are three records and one answer."""
    _library(
        monkeypatch,
        [_item("AAA", TITLE, DOI), _item("BBB", TITLE, DOI), _item("CCC", TITLE, DOI)],
    )

    out = zotero.resolve_title(TITLE)

    assert out["grounded"] is True, (
        f"three records naming the same DOI were treated as ambiguous: {out}"
    )
    assert out["doi"] == DOI


def test_the_doi_is_compared_canonically(monkeypatch):
    """Same work, recorded with different surface forms of its DOI."""
    _library(
        monkeypatch,
        [
            _item("AAA", TITLE, DOI),
            _item("BBB", TITLE, f"https://doi.org/{DOI}"),
            _item("CCC", TITLE, DOI.upper()),
        ],
    )

    out = zotero.resolve_title(TITLE)

    assert out["grounded"] is True, out
    assert zotero.normalize_doi(out["doi"]).lower() == DOI.lower()


def test_records_naming_different_dois_stay_ambiguous(monkeypatch):
    """The bar that must NOT move. Two works sharing a title is exactly when a
    guess is least defensible, and this is the case the original rule was for."""
    _library(
        monkeypatch,
        [_item("AAA", TITLE, DOI), _item("BBB", TITLE, "10.1038/s41586-020-2649-2")],
    )

    out = zotero.resolve_title(TITLE)

    assert out["grounded"] is False, (
        f"two different DOIs under one title were grounded anyway: {out}"
    )
    assert out["doi"] is None
    assert len(out["candidates"]) == 2, out
    assert "ambiguous" in out["evidence"] or "share this title" in out["evidence"]


def test_a_doi_less_twin_still_blocks_grounding(monkeypatch):
    """Deliberately unchanged, and the reason is worth stating.

    A record with no DOI names no competing answer -- but neither does it
    confirm it is the same work, and it might be a different paper this library
    simply has no DOI for. Grounding is the highest bar here, so this stays
    refused until a human decides otherwise about their own library.
    """
    _library(monkeypatch, [_item("AAA", TITLE, DOI), _item("BBB", TITLE, "")])

    out = zotero.resolve_title(TITLE)

    assert out["grounded"] is False, (
        "a DOI-less twin stopped blocking; that is a judgement call about "
        "someone's library, not a code fix"
    )
    assert out["candidates"], out


def test_no_doi_anywhere_is_not_grounded(monkeypatch):
    """Positive control for the whole file: 'ground whenever the DOIs agree'
    must not ground when there are no DOIs to agree."""
    _library(monkeypatch, [_item("AAA", TITLE, ""), _item("BBB", TITLE, "")])

    out = zotero.resolve_title(TITLE)

    assert out["grounded"] is False
    assert out["doi"] is None
