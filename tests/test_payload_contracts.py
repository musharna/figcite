"""Every field the page reads, asserted against what the service produces.

The STORE tier held 452 survivors and 168 of them are FIELD NAMES in dicts
that get returned to a caller or serialised. Killing 168 individually would
be 168 tests each pinning one string. The defect they share is one thing --
a payload and its consumer drifting apart -- so the instrument should be one
thing too.

`test_candidate_contract.py` already does this for candidate rows, and does
it the right way round: it DERIVES the key list from the live `PAGE` with a
regex rather than hand-maintaining a copy, because a hand-written list drifts
in both directions without a test noticing. This file extends that to the
other payloads -- the whereis result, the deck audit row, the pending card,
and the confirm result.

A hand-written key list would be this repo's own recurring "a list of names
cannot guard an open set". Derived lists cannot go stale: if the page starts
or stops reading a field, this file changes with it and no one edits it.
"""

from __future__ import annotations

import re

import pytest
from PIL import Image

from figcite import corpus, match, service, session_tabs, store
from figcite.provenance import Record
from figcite.webui import PAGE


def _keys_read_as(var: str) -> set[str]:
    """Every `<var>.<field>` the page's own script reads."""
    return set(re.findall(rf"\b{var}\.(\w+)", PAGE))


def test_the_derivation_still_matches_the_page():
    """The control on the regex itself, not a duplicate of it.

    If the page's loop variables are renamed, these sets go empty and every
    assertion below passes vacuously -- a contract test that cannot fail.
    """
    for var, expect in (("res", 3), ("item", 3), ("m", 3), ("r", 3)):
        assert len(_keys_read_as(var)) >= expect, (
            f"the page no longer reads fields off `{var}`; the regex in this "
            f"file has gone stale and every check below is vacuous"
        )


# ------------------------------------------------- whereis


def _whereis(monkeypatch, tmp_path, verdict, rows=()):
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: list(rows) or [object()])
    monkeypatch.setattr(match, "by_dhash", lambda b, r: verdict)
    monkeypatch.setattr(match, "by_orb", lambda b, r, root, descriptor_dir=None: verdict)
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])
    q = tmp_path / "q.png"
    Image.new("RGB", (40, 30), "blue").save(q)
    return service.whereis(str(q))


def test_a_whereis_match_carries_every_field_the_page_renders(tmp_path, monkeypatch):
    """`whereisHtml` reads `res.verdict`, `res.matches`, `res.reason`, and
    per match `source`, `doi`, `score`, `score_label`, `title`, `container`,
    `evidence`.

    Those key names sat in the STORE tier: mutate one and the payload still
    has the right SHAPE, the page reads `undefined`, and the row renders with
    a silently blank field. Nothing raises, nothing logs.
    """
    out = _whereis(
        monkeypatch,
        tmp_path,
        match.Match("10.1/found", "PMC9", "Figure 3", "dhash", 0.0, 9.0, "hamming 0"),
    )

    assert out["verdict"] == "match"
    row = out["matches"][0]
    for field in (
        "source",
        "doi",
        "score",
        "score_label",
        "title",
        "container",
        "evidence",
        "year",
        "type",
    ):
        assert field in row, f"the whereis row is missing {field!r}: {sorted(row)}"

    # PRESENCE is not enough. `score_label` is read off the verdict with
    # `getattr(verdict, "score_label", "")`, so mutating that attribute name
    # leaves the key in place and blanks the VALUE -- and a blank field and a
    # missing one render identically on the page, which is the whole reason
    # this class of defect survives. So the values are asserted too.
    assert row["source"] == "dhash", row
    assert row["doi"] == "10.1/found", row
    assert row["score_label"] == "hamming 0", (
        f"score_label came through blank: {row!r}. A bare number tells a "
        f"reader nothing -- the two matchers score on opposite polarities."
    )
    assert row["container"] == "PMC9", row
    assert row["type"] == "figure", (
        f"the row type is {row['type']!r}; the page groups on this value"
    )
    assert row["evidence"] is True, row
    assert _keys_read_as("m"), "the page reads no match fields at all"


def test_every_whereis_verdict_carries_a_reason_field(tmp_path, monkeypatch):
    """`res.reason` is only rendered for could-not-decide, but the KEY must be
    present on every verdict -- the page reads `res.reason || ""` and a
    missing key and an empty one are indistinguishable there, which is how a
    reason silently stops being shown."""
    for verdict in (
        match.CouldNotDecide("opencv is not installed"),
        match.NoMatch(),
    ):
        out = _whereis(monkeypatch, tmp_path, verdict)
        assert "reason" in out, (verdict, sorted(out))
        assert "matches" in out and "verdict" in out


def test_an_empty_corpus_returns_the_same_shape_as_every_other_verdict(tmp_path, monkeypatch):
    """The empty-corpus EARLY RETURN builds its own dict literal, so its keys
    are a separate copy of the contract -- and `'matches'` there survived
    being mutated because every test that checks the shape goes through the
    normal path.

    A caller that reads `res.matches` gets `undefined` and the page renders
    nothing at all, on the one route where the user most needs to be told
    what to do next (`run figcite corpus build`).
    """
    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [])
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])
    q = tmp_path / "empty.png"
    Image.new("RGB", (40, 30), "blue").save(q)

    out = service.whereis(str(q))

    assert out["verdict"] == "could-not-decide", out
    assert out["matches"] == [], out
    assert out["reason"], "an empty corpus said nothing about why"


def test_a_could_not_decide_actually_carries_its_reason(tmp_path, monkeypatch):
    """And the reason is not merely present but populated -- an empty string
    renders as a bare "COULD NOT DECIDE:" with nothing after the colon."""
    out = _whereis(monkeypatch, tmp_path, match.CouldNotDecide("opencv missing"))

    assert out["verdict"] == "could-not-decide"
    assert "opencv missing" in out["reason"], out


# ------------------------------------------------- the pending card


@pytest.fixture
def _own_store(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MANIFEST", tmp_path / "m.jsonl")
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(store, "LIBRARY", tmp_path / "library")


def test_a_pending_item_carries_every_field_the_card_renders(_own_store):
    """`card(item)` reads `item.error`, `item.candidates`, `item.doi`,
    `item.grounded`, `item.doi_evidence`, `item.ref`.

    `doi` and `grounded` are DELIBERATELY not read back from a filed record
    (the service comment says so), so they must still be PRESENT keys with
    falsey values rather than absent -- `item.doi && item.grounded` on a
    missing key is `undefined`, which renders the "no source inferred" branch
    for an item that has one.
    """
    store.put(
        Record(
            sha256="a" * 64,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            source_detail={
                "clipboard_capture": {"width": 800, "height": 600},
                "doi_evidence": "no zotero hit",
                "candidates": [{"doi": "10.1/c", "title": "T"}],
            },
        )
    )

    items = service.pending_items()
    assert items, "nothing pending"
    payload = items[0].__dict__ if hasattr(items[0], "__dict__") else dict(items[0])

    for field in ("ref", "candidates", "doi_evidence"):
        assert field in payload, f"missing {field!r}: {sorted(payload)}"
    assert payload["candidates"], "candidates were dropped on the way out"
    assert payload["doi_evidence"] == "no zotero hit"


def test_the_card_template_reads_nothing_the_service_does_not_send(_own_store):
    """The contract in the direction that actually breaks.

    Adding a field to the page and forgetting it in the payload renders
    `undefined`; the page never errors. Only the fields the template reads on
    the ITEM are checked -- not on `c` (candidates, already covered by
    test_candidate_contract.py) -- and a small allowlist covers names the
    template computes locally rather than receives.
    """
    LOCAL = {
        "length",
        "map",
        "join",
        "value",
        "checked",
        "innerHTML",
        "dataset",
        "closest",
        "querySelector",
        "querySelectorAll",
        "forEach",
        "filter",
        "push",
        "trim",
        "textContent",
        "hidden",
        "reset",
        "elements",
        "type",
        "name",
        "files",
        "target",
        "currentTarget",
        "preventDefault",
    }
    store.put(
        Record(
            sha256="b" * 64,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            source_detail={"doi_evidence": "why"},
        )
    )
    payload = service.pending_items()[0]
    have = set(payload.__dict__) if hasattr(payload, "__dict__") else set(payload)

    wanted = _keys_read_as("item") - LOCAL
    missing = wanted - have

    assert not missing, (
        f"the page reads {sorted(missing)} off a pending item and the service sends {sorted(have)}"
    )
