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
from pathlib import Path
from typing import Union

from .provenance import dhash_bytes, hamming

DHASH_THRESHOLD = 6

# ORB tuning. All three exist to make the matcher decline rather than guess.
MIN_KEYPOINTS = 25  # below this the query is too smooth to identify at all
MIN_INLIERS = 15  # below this nothing in the corpus is a real hit
MIN_MARGIN = 3.0  # top must beat the runner-up by this factor to be asserted


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


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401
    except Exception:
        return False
    return True


def _decode(data: bytes):
    import cv2
    import numpy as np

    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)


def by_orb(query_bytes: bytes, rows, image_root) -> Verdict:
    """Sub-image search: find the corpus figure this crop came out of.

    Scored by RANSAC INLIERS, not raw match counts. Raw counts do not
    discriminate -- measured on text-heavy inputs they ranked an unrelated
    document level with the truth (1.5x). Inliers separated the same cases by a
    median 38.9x, because spurious matches are not geometrically consistent
    with any single transform.
    """
    if not opencv_available():
        return CouldNotDecide(
            "opencv is not installed, so a cropped figure cannot be matched; "
            "install figcite[match] to enable it"
        )

    import cv2
    import numpy as np

    query = _decode(query_bytes)
    if query is None:
        return CouldNotDecide("the query image could not be decoded")

    orb = cv2.ORB_create(nfeatures=1500)
    kq, dq = orb.detectAndCompute(query, None)
    if dq is None or len(kq) < MIN_KEYPOINTS:
        return CouldNotDecide(
            f"only {0 if dq is None else len(kq)} visual features in this image "
            f"(need {MIN_KEYPOINTS}); it is too smooth to identify"
        )

    bf = cv2.BFMatcher(cv2.NORM_HAMMING)
    scored = []
    for row in rows:
        img = cv2.imread(str(Path(image_root) / row.image_path), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        k, d = orb.detectAndCompute(img, None)
        if d is None or len(d) < 10:
            continue
        pairs = [p for p in bf.knnMatch(dq, d, k=2) if len(p) == 2]
        good = [m for m, s in pairs if m.distance < 0.75 * s.distance]
        inliers = 0
        if len(good) >= 8:
            src = np.float32([kq[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
            dst = np.float32([k[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
            _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
            inliers = int(mask.sum()) if mask is not None else 0
        scored.append((inliers, row))

    if not scored:
        return CouldNotDecide("no corpus figure could be read for comparison")

    scored.sort(key=lambda t: t[0], reverse=True)
    best_n, best = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0
    if best_n < MIN_INLIERS:
        return NoMatch()
    margin = best_n / max(second, 1)
    if margin < MIN_MARGIN:
        return CouldNotDecide(
            f"the two best candidates are too close to call ({best_n} vs {second})"
        )
    return Match(
        doi=best.doi,
        pmcid=best.pmcid,
        label=best.label,
        method="orb",
        score=float(best_n),
        margin=float(margin),
    )
