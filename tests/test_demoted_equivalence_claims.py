"""Three guards I had certified as equivalent-by-construction, demoted.

The GUARD tier of the mutation audit ended with a handful of survivors I did
not test but PROVED unobservable. Two of those proofs I falsified myself
(702753f, 38bebf2); a peer review of the rest killed one more and split a
second. The failures all share one shape, and it is worth naming because it
is what this file exists to stop happening again:

  An equivalence argument that quantifies over INPUTS is a hypothesis.
  One that quantifies over the CODE PATH is a proof.

`desc is None or not kp` was the input-quantified kind wearing a proof's
clothes. I argued the two operands can never disagree because OpenCV's
detectAndCompute returns `desc=None` exactly when `kp` is empty, and my
evidence was eight probed images. But that is a claim about a DEPENDENCY, not
about this function -- it would have to hold for every supported OpenCV
version -- and `orb` is an injected parameter, so nothing in figcite's own
code makes the operands agree. A caller passing a double separates them
trivially.

The zotero case is different and is a note about the INSTRUMENT rather than
the code. `len(exact) > 1` is genuinely equivalent to `>= 1` on the reachable
domain, and that is the only mutant my generator emits for it, because its
comparison table only swaps an operator for its adjacent one or its exact
negation. `> 1 -> != 1` is outside the table and is NOT equivalent: with
`len(exact) == 0` -- the ordinary no-match path, reached because both
preceding `len(exact) == 1` branches are false -- it announces "0 Zotero
items share this title, ambiguous" about an empty list. A reachable,
user-visible wrong answer sitting behind an operator the tool cannot produce
is the reason "the tier is closed" has to mean "closed under the mutations I
generate", and the reason this test is here even though no mutant of mine
demands it.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from figcite import corpus, match, zotero


# ------------------------------------------------------------- the doubles


class _KP:
    """The one attribute `_target_features` reads off a keypoint."""

    pt = (1.0, 2.0)


class _Orb:
    """An ORB whose detectAndCompute returns exactly what it is told to.

    This is the whole point of the demotion: `orb` is a PARAMETER, so the
    agreement between `desc` and `kp` that my proof rested on is a property
    of one implementation, not of this code.
    """

    def __init__(self, result):
        self._result = result

    def detectAndCompute(self, img, mask):
        return self._result


def _png(colour=(7, 90, 200), size=(40, 30)):
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


class _Row:
    def __init__(self, image_path):
        self.image_path = image_path


# ---------------------------------------------- match._target_features


@pytest.mark.parametrize(
    "result,operand",
    [
        (([_KP()], None), "desc is None"),
        (([], np.zeros((0, 32), dtype=np.uint8)), "not kp"),
    ],
    ids=[
        "descriptors-missing-but-keypoints-found",
        "descriptors-found-but-no-keypoints",
    ],
)
def test_each_operand_of_the_feature_guard_is_separately_load_bearing(
    result, operand, tmp_path
):
    """`desc is None or not kp` survived dropping EITHER operand.

    Under the mutant that drops `desc is None`, the first case returns
    `(None, pts)` -- a row that claims keypoint coordinates while carrying no
    descriptors to match them with. Under the mutant that drops `not kp`, the
    second returns a zero-row descriptor array paired with empty coordinates.

    Both are contract violations of this function, whose entire job is to
    answer "usable features, or nothing at all" -- and the docstring says so:
    every way a row can fail has to arrive at CouldNotDecide.
    """
    (tmp_path / "fig.png").write_bytes(_png())

    desc, pts = match._target_features(
        _Row("fig.png"), str(tmp_path), None, _Orb(result)
    )

    # Element-wise, NOT `== (None, None)`. A tuple comparison against a numpy
    # array returns an array, and `assert` on it raises "truth value ... is
    # ambiguous" BEFORE reaching the message -- so the first version of this
    # test failed on the mutant for a reason that had nothing to do with the
    # mutant. It would have read as a kill in any summary that only counts
    # failures.
    assert desc is None, f"dropping `{operand}` returns descriptors {desc!r}"
    assert pts is None, f"dropping `{operand}` returns keypoints {pts!r}"


def test_the_caller_absorbs_both_mutants_which_is_why_the_contract_is_the_test(
    tmp_path,
):
    """Why the assertion above is on the RETURN VALUE and not on a query.

    `by_orb` re-checks `if d is None or len(d) < 10: continue`, and that guard
    happens to skip both mutated results too -- `(None, pts)` fails `d is
    None`, and a zero-row array fails `len(d) < 10`. So an end-to-end query
    cannot distinguish them TODAY.

    That is a coincidence of the current caller, not a property of the
    function, and it is exactly the reasoning that produced the bad proof in
    the first place: checking one consumer and generalising. Pinning the
    contract here means the next caller -- one that does not happen to
    re-check -- inherits a guard that already holds.
    """
    from figcite.match import MIN_KEYPOINTS

    assert MIN_KEYPOINTS > 0, "a zero floor would make the caller's guard vacuous"

    # The narrower point, stated as code: a zero-row descriptor array has a
    # length, so `len(d) < 10` is what absorbs it -- not a None check.
    empty = np.zeros((0, 32), dtype=np.uint8)
    assert empty is not None and len(empty) == 0


# ---------------------------------------------- corpus.write_descriptors


@pytest.mark.parametrize(
    "result,operand",
    [
        (([_KP()], None), "desc is None"),
        (([], np.zeros((0, 32), dtype=np.uint8)), "not kp"),
    ],
    ids=[
        "descriptors-missing-but-keypoints-found",
        "descriptors-found-but-no-keypoints",
    ],
)
def test_a_refused_image_never_leaves_a_cache_behind(
    result, operand, monkeypatch, tmp_path
):
    """The same guard in corpus.py, where the mutant is worse than a bad return.

    `np.savez` accepts `None` -- it writes an object array -- so under the
    mutant this function reports success AND leaves a descriptor cache on
    disk that no query can use. `match._target_features` prefers that cache
    over decoding the image, and reading it back raises (object arrays need
    allow_pickle), which the caller swallows as "a corrupt cache entry is a
    slow path, not a failure".

    So the figure is not merely unmatched: it is permanently slow AND
    permanently unmatchable, and the corpus reports itself as built.
    """
    import cv2

    monkeypatch.setattr(cv2, "ORB_create", lambda *a, **kw: _Orb(result))

    rel = f"demoted/{operand.replace(' ', '-')}.png"
    dest = corpus.descriptor_path(rel)

    assert corpus.write_descriptors(rel, _png()) is False, (
        f"dropping `{operand}` reports a descriptor cache that is not usable"
    )
    assert not dest.exists(), f"dropping `{operand}` writes {dest} anyway"


def test_the_positive_control_a_real_image_still_caches(monkeypatch, tmp_path):
    """The negative assertions above need this or a broken stub reads as a pass.

    If `_png()` produced something OpenCV refused, or `descriptor_path` pointed
    somewhere unwritable, every assertion above would hold for the wrong
    reason. This drives the same function with the REAL ORB and requires the
    cache to appear.
    """
    if not match.opencv_available():
        pytest.skip("opencv not installed")

    # Deliberately the same texture generator the matcher's own tests use. My
    # first attempt here was a strip of flat rectangles, and real ORB found
    # ZERO keypoints in it -- so the control passed the negative assertions
    # for exactly the reason they were meant to rule out.
    import random

    from PIL import ImageDraw

    rnd = random.Random(11)
    img = Image.new("RGB", (320, 320), "white")
    d = ImageDraw.Draw(img)
    for _ in range(90):
        x, y = rnd.randrange(280), rnd.randrange(280)
        w, h = rnd.randrange(8, 38), rnd.randrange(8, 38)
        col = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        (d.rectangle if rnd.random() < 0.5 else d.ellipse)(
            [x, y, x + w, y + h], fill=col
        )
    buf = io.BytesIO()
    img.save(buf, "PNG")

    rel = "demoted/positive-control.png"
    assert corpus.write_descriptors(rel, buf.getvalue()) is True
    assert corpus.descriptor_path(rel).exists()


# ---------------------------------------------- zotero.resolve_title


def test_an_empty_exact_match_list_is_not_an_ambiguous_one(monkeypatch):
    """`len(exact) > 1` is reached with len 0, and must stay false there.

    Both `len(exact) == 1` branches above it return, so line 499 is reachable
    only when the count is 0 or 2+. My generator emits `>= 1` for it, which IS
    equivalent on that domain. `!= 1` is not: it turns the ordinary "nothing in
    the library looks like this" outcome into "0 Zotero items share this title
    -- ambiguous, so which one the figure came from is a guess".

    That is the project's cardinal failure in miniature. Ambiguity means
    CouldNotDecide-among-candidates; nothing found means NoMatch. Collapsing
    them tells the user to go disambiguate an empty list.
    """
    monkeypatch.setattr(
        zotero,
        "library",
        lambda *a, **kw: [
            {
                "key": "Q1",
                "title": "Quantum gravity in black holes",
                "doi": "10.1/qg",
                "url": "",
                "itemType": "journalArticle",
                "creators": ["Doe"],
                "date": "2024",
                "doi_source": "doi-field",
            }
        ],
    )

    out = zotero.resolve_title("A study of orchids")

    assert not out.get("doi"), out
    assert "ambiguous" not in out["evidence"].lower(), (
        f"an empty exact-match list was reported as ambiguous: {out['evidence']!r}"
    )
    assert "0 Zotero items" not in out["evidence"], out["evidence"]


def test_two_sharing_a_title_really_is_ambiguous(monkeypatch):
    """The positive control for the assertion above.

    A test that only asserts "not ambiguous" passes just as well against a
    build where the ambiguity branch is dead. Two items with one title must
    still reach it.
    """

    def _it(key):
        return {
            "key": key,
            "title": "A study of orchids",
            "doi": "",
            "url": "",
            "itemType": "journalArticle",
            "creators": ["Doe"],
            "date": "2024",
            "doi_source": "doi-field",
        }

    monkeypatch.setattr(zotero, "library", lambda *a, **kw: [_it("A"), _it("B")])

    out = zotero.resolve_title("A study of orchids")

    assert "ambiguous" in out["evidence"].lower(), out["evidence"]
    assert "2 Zotero items" in out["evidence"], out["evidence"]
