"""Picking a candidate: which failures are refusals, and which are bugs.

Two guards do the same job in the two confirm paths -- `confirm` for a staged
capture and `_confirm_filed` for one already in the manifest:

    try:
        doi = candidates[pick]["doi"]
    except (KeyError, IndexError, TypeError):
        raise KeyError(f"no candidate {pick} on {ref}")

The exception-routing mutation tier left six mutants alive across the pair:
`drop KeyError` and `drop TypeError` at each site, plus each broadening to
`except Exception`. Only the IndexError arm was exercised, at both. So five
sixths of a guard that turns a user's bad selection into a clean refusal was
resting on nothing.

## Why all three types are justified, checked rather than assumed

Each is reachable by a different real route, which is why this file tests them
instead of narrowing the tuple the way `by_dhash`'s guard was narrowed:

- **IndexError** -- `--pick 7` against two candidates. Already covered.
- **KeyError** -- two ways. `inf = raw.get("inference", {}) or {}`, so
  `inf["candidates"]` raises outright on a capture whose inference never ran;
  and a candidate dict that carries no `doi` key raises on the last step.
- **TypeError** -- `"candidates": null` in the JSON gives `None[pick]`. And
  `pick` itself arrives from an untrusted client: `/api/confirm` splats the
  request body through `CONFIRM_FIELDS` into `confirm()`, and while the
  shipped page sends `Number(picked.value)`, nothing makes a caller do that.
  `list["0"]` is a TypeError.

## What the broadening mutant is for

`except Exception` still refuses, so every "bad pick is refused" assertion
passes under it. What changes is that a DEFECT inside the lookup then gets
reported as "no candidate 3" -- a message about the user's selection, for a
failure that has nothing to do with their selection. The pair of assertions
is the point: the bad pick must be refused AND the unrelated fault must
escape.
"""

from __future__ import annotations

import pytest

from figcite import service, store
from figcite.provenance import Record, now_stamps
from figcite.service import PendingItem

CAND = {"source": "open-tab", "doi": "10.1/real", "title": "A paper"}


class _Exploding:
    """Indexing this raises something the guard has no business catching."""

    def __getitem__(self, key):
        raise RuntimeError("the candidate store is broken")


# ------------------------------------------------------- the staged path


def _staged(monkeypatch, inference):
    """Drive `confirm`'s staged branch with a chosen inference dict."""
    item = PendingItem(ref="staged:q.png", kind="staged", context="ctx")
    monkeypatch.setattr(service, "_item_for", lambda ref: item)
    monkeypatch.setattr(service, "_raw_staged", lambda ref: {"inference": inference})
    return item


@pytest.mark.parametrize(
    "inference,pick,arm",
    [
        ({}, 0, "KeyError: no 'candidates' key at all"),
        ({"candidates": [{"title": "no doi"}]}, 0, "KeyError: candidate has no doi"),
        ({"candidates": None}, 0, "TypeError: candidates is null"),
        ({"candidates": [CAND]}, 5, "IndexError: index past the end"),
    ],
    ids=[
        "no-candidates-key",
        "candidate-without-doi",
        "candidates-null",
        "out-of-range",
    ],
)
def test_a_pick_that_cannot_be_resolved_is_refused_not_raised(
    inference, pick, arm, monkeypatch
):
    """Every one of the three caught types, reached by its own real route.

    `pick` is a parameter, not derived from `arm`. The first draft computed it
    as `5 if "out of range" in arm else 0` against a label reading "index past
    the end" -- so the out-of-range case silently ran with pick=0, resolved
    candidate 0 and went off to CrossRef, testing nothing it was named for.
    Deriving control flow from prose invites exactly that.
    """
    _staged(monkeypatch, inference)

    with pytest.raises(KeyError, match="no candidate"):
        service.confirm("staged:q.png", pick=pick)


def test_a_string_pick_from_an_untrusted_client_is_refused(monkeypatch):
    """The TypeError arm as the API actually exposes it.

    `/api/confirm` splats the client's JSON body into `confirm()`. The shipped
    page coerces with `Number(...)`, but the route is a trust boundary and a
    caller need not. `candidates["0"]` is a TypeError, and it has to come back
    as a refusal rather than a 500.
    """
    _staged(monkeypatch, {"candidates": [CAND]})

    with pytest.raises(KeyError, match="no candidate"):
        service.confirm("staged:q.png", pick="0")


def test_a_broken_candidate_store_is_not_reported_as_a_bad_pick(monkeypatch):
    """The broadening mutant: `except Exception` swallows our own defect.

    Under it a RuntimeError from the lookup is answered with "no candidate 0",
    telling the user their selection was wrong when their selection was fine.
    """
    _staged(monkeypatch, {"candidates": _Exploding()})

    with pytest.raises(RuntimeError, match="candidate store is broken"):
        service.confirm("staged:q.png", pick=0)


def test_a_good_pick_still_resolves(monkeypatch):
    """Positive control for the whole staged group.

    Every assertion above is satisfied by a `confirm` that refuses every pick.
    """
    _staged(monkeypatch, {"candidates": [CAND]})
    seen = {}

    def fake_record_for(doi, cite, _x, **kw):
        seen["doi"] = doi
        raise RuntimeError("stop here -- resolution is not what this test measures")

    monkeypatch.setattr(service, "record_for", fake_record_for)

    with pytest.raises(RuntimeError, match="stop here"):
        service.confirm("staged:q.png", pick=0)
    assert seen["doi"] == CAND["doi"], seen


# -------------------------------------------------------- the filed path


def _filed(sha, candidates):
    u, loc = now_stamps()
    store.put(
        Record(
            sha256=sha,
            dhash="0" * 16,
            source_kind="clipboard",
            confirmed=False,
            captured_utc=u,
            captured_local=loc,
            source_detail={"candidates": candidates},
        )
    )
    return PendingItem(
        ref=f"filed:{sha}",
        kind="filed",
        context="ctx",
        candidates=candidates,
    )


@pytest.mark.parametrize(
    "candidates,pick",
    [
        ([{"title": "no doi here"}], 0),
        (None, 0),
        ([CAND], 9),
        ([CAND], "0"),
    ],
    ids=["candidate-without-doi", "candidates-null", "out-of-range", "string-pick"],
)
def test_the_filed_twin_refuses_the_same_four_ways(candidates, pick):
    """The identical guard in `_confirm_filed`, which had the identical gap.

    Two copies of one rule need two observations -- this suite has written
    itself that note before, and the tier proved it again by leaving the same
    two arms alive at both sites.
    """
    item = _filed("a" * 64, candidates)

    with pytest.raises(KeyError, match="no candidate"):
        service._confirm_filed(item, doi=None, pick=pick, adapted_from=None, note="")


def test_the_filed_twin_also_lets_a_real_fault_escape():
    """Broadening control for the filed copy."""
    item = _filed("b" * 64, [])
    item.candidates = _Exploding()

    with pytest.raises(RuntimeError, match="candidate store is broken"):
        service._confirm_filed(item, doi=None, pick=0, adapted_from=None, note="")
