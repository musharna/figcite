"""Comparing one image against corpus figures.

Two stages. dhash is free and exact-ish; ORB handles crops. Neither ever
returns a bare boolean, because the caller must be able to tell "searched and
found nothing" from "could not look" -- they license different next actions.
The first means the figure is not in your corpus. The second means you learned
nothing at all, and treating it as the first is how a tool ends up asserting
an absence it never observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from .provenance import dhash_bytes, hamming

DHASH_THRESHOLD = 6


@dataclass
class Match:
    doi: str
    pmcid: str
    label: str
    method: str
    score: float
    margin: float


@dataclass
class NoMatch:
    pass


@dataclass
class CouldNotDecide:
    reason: str


Verdict = Union[Match, NoMatch, CouldNotDecide]


def by_dhash(query_bytes: bytes, rows) -> Verdict:
    """Nearest corpus figure by perceptual hash.

    A miss is CouldNotDecide, never NoMatch: dhash cannot see a crop at all
    (measured -- hamming 7 for 5% off each edge, against a threshold of 6), so
    its silence is not evidence the figure is absent from the corpus.
    """
    if not rows:
        return CouldNotDecide("the corpus is empty -- run `figcite corpus build`")

    dh = dhash_bytes(query_bytes)
    scored = sorted(((hamming(dh, r.dhash), r) for r in rows), key=lambda t: t[0])
    best_d, best = scored[0]
    if best_d > DHASH_THRESHOLD:
        return CouldNotDecide(
            "no perceptual-hash hit; dhash cannot see a crop, so this is not "
            "evidence the figure is absent"
        )
    # 64 stands in for "as far away as an 8x8 dhash can be" when there is only
    # one row, so a lone corpus figure reports a wide margin rather than none.
    runner_up = scored[1][0] if len(scored) > 1 else 64
    return Match(
        doi=best.doi,
        pmcid=best.pmcid,
        label=best.label,
        method="dhash",
        score=float(best_d),
        margin=float(runner_up - best_d),
    )
