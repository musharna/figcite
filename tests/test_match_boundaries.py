"""Every threshold in the matcher, driven at its exact boundary.

`match.py` is where the tool decides whether it KNOWS something. It carries
six tuned numbers -- DHASH_THRESHOLD, MIN_KEYPOINTS, MIN_INLIERS, MIN_MARGIN,
the 0.75 ratio test and the 8-point minimum for homography -- and the full
mutation sweep left 15 survivors here, more than any module of its size. The
existing tests drive the middle of each range (an identical image, a blank
image, an unrelated image) and nothing sits on an edge, so every comparison
could be loosened or tightened by one and no test noticed.

That matters more here than anywhere else in the codebase. These comparisons
are the three-outcome discipline itself: shift one and a `CouldNotDecide`
becomes a `NoMatch`, which is the tool asserting a figure is absent from a
corpus it never actually searched.

Boundaries are hit by SELF-CALIBRATION rather than by hard-coded magic
numbers: the test measures what the real detector produced and then moves the
threshold onto that value. Hard-coding "this image has 21 keypoints" would
rot on the next OpenCV release and, worse, would turn into a test that skips
rather than fails. The constant is fixture setup; the assertion is always
about which VERDICT comes back.
"""

from __future__ import annotations

import io
import random

import numpy as np
import pytest
from PIL import Image, ImageDraw

from figcite import match
from figcite.provenance import dhash_bytes

pytestmark = pytest.mark.skipif(
    not match.opencv_available(), reason="opencv not installed"
)


# ------------------------------------------------------------------ helpers


def _png(img) -> bytes:
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


def _noise(seed, size=(64, 64)):
    rnd = random.Random(seed)
    im = Image.new("RGB", size)
    im.putdata(
        [
            (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
            for _ in range(size[0] * size[1])
        ]
    )
    return im


def _textured(seed, size=(320, 320)):
    rnd = random.Random(seed)
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    for _ in range(90):
        x, y = rnd.randrange(size[0] - 40), rnd.randrange(size[1] - 40)
        w, h = rnd.randrange(8, 38), rnd.randrange(8, 38)
        col = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        (d.rectangle if rnd.random() < 0.5 else d.ellipse)(
            [x, y, x + w, y + h], fill=col
        )
    return im


def _sparse(n_squares, size=(300, 300)):
    """A near-blank image with a few corners: FEW keypoints, but not zero.

    The distinction the `or` at match.py:130 makes and the existing blank-image
    test cannot: a blank page yields `dq is None` AND `len(kq) < MIN`, so both
    operands hold and `and` satisfies it too.
    """
    im = Image.new("RGB", size, "white")
    dr = ImageDraw.Draw(im)
    for i in range(n_squares):
        x, y = 40 + i * 30, 40 + (i % 3) * 60
        dr.rectangle([x, y, x + 9, y + 9], fill="black")
    return im


def _keypoints(img):
    """What the REAL detector finds, with the real settings, on this image."""
    import cv2

    g = cv2.imdecode(np.frombuffer(_png(img), np.uint8), cv2.IMREAD_GRAYSCALE)
    orb = cv2.ORB_create(nfeatures=match.ORB_FEATURES)
    kp, desc = orb.detectAndCompute(g, None)
    return kp, desc


class Row:
    def __init__(self, image_path, doi="10.1/x", dh=""):
        self.image_path, self.doi, self.dhash = image_path, doi, dh
        self.pmcid, self.label = "PMC1", "Figure 1"


def _write(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _flip_bits(hex_hash: str, n: int) -> str:
    """A hash exactly `n` bits away from this one.

    dhash is hex over a fixed-width bit string and `hamming` is a popcount of
    the XOR, so flipping n distinct bits produces a distance of exactly n --
    no image needs to be found by search.
    """
    v = int(hex_hash, 16)
    for i in range(n):
        v ^= 1 << i
    return format(v, f"0{len(hex_hash)}x")


# ---------------------------------------------- dhash: the threshold itself


def test_a_hit_exactly_at_the_dhash_threshold_is_a_match():
    """`if best_d > DHASH_THRESHOLD` survived `Gt -> GtE`.

    At exactly the threshold the figure IS matched -- the constant is the
    furthest distance still considered the same image, not the first distance
    rejected. Tightened by one, a legitimately-matched figure silently becomes
    "no perceptual-hash hit", and because that path returns CouldNotDecide the
    user is told the tool could not look rather than that it looked and found
    their figure.
    """
    query = _png(_noise(1))
    at_edge = _flip_bits(dhash_bytes(query), match.DHASH_THRESHOLD)

    v = match.by_dhash(query, [Row("f.png", "10.1/edge", at_edge)])

    assert isinstance(v, match.Match), (
        f"a figure exactly at hamming {match.DHASH_THRESHOLD} was rejected: {v}"
    )
    assert v.doi == "10.1/edge"
    assert v.score == float(match.DHASH_THRESHOLD)


def test_one_bit_past_the_dhash_threshold_is_not_a_match():
    """The positive control. Without it "match everything" passes the test
    above, and every query returns whichever figure happens to sort first."""
    query = _png(_noise(1))
    past = _flip_bits(dhash_bytes(query), match.DHASH_THRESHOLD + 1)

    v = match.by_dhash(query, [Row("f.png", "10.1/past", past)])

    assert isinstance(v, match.CouldNotDecide), (
        f"a figure past the threshold was asserted as a match: {v}"
    )


def test_the_dhash_margin_is_measured_against_the_runner_up():
    """`runner_up = scored[1][0] if len(scored) > 1 else 64` survived `1 -> 2`.

    Under the mutant a two-row corpus takes the `else 64` branch, so the
    margin is reported against "as far away as a dhash can be" instead of
    against the figure that actually came second. The margin is the number a
    reader uses to judge whether to trust the hit; inflating it to 64 makes
    every match look unambiguous, including the ambiguous ones.
    """
    query = _png(_noise(2))
    dh = dhash_bytes(query)
    rows = [
        Row("best.png", "10.1/best", dh),  # distance 0
        Row("near.png", "10.1/near", _flip_bits(dh, 20)),  # distance 20
    ]

    v = match.by_dhash(query, rows)

    assert isinstance(v, match.Match) and v.doi == "10.1/best", v
    assert v.margin == 20.0, (
        f"margin {v.margin} is not the distance to the runner-up; 64 would mean "
        "the lone-row fallback fired with two rows present"
    )


def test_a_lone_corpus_figure_reports_the_widest_possible_margin():
    """The `else 64` half, which the test above must not be able to cover for.
    One row means there is no runner-up, and the documented stand-in is the
    maximum an 8x8 dhash can express."""
    query = _png(_noise(3))
    v = match.by_dhash(query, [Row("only.png", "10.1/only", dhash_bytes(query))])

    assert isinstance(v, match.Match)
    assert v.margin == 64.0, v.margin


# ------------------------------------------- ORB: too smooth to identify


def test_a_query_with_few_but_nonzero_features_is_still_too_smooth(tmp_path):
    """`if dq is None or len(kq) < MIN_KEYPOINTS` survived `Or -> And`.

    The existing blank-page test cannot reach this: a blank page produces
    `dq is None` AND zero keypoints, so BOTH operands hold and `and` is
    satisfied too. A fixture that satisfies every operand of a boolean cannot
    observe which operand carries it.

    A few black squares give a handful of real descriptors -- `dq` is not
    None, and the count is still under the floor. Under `and` that image sails
    past the guard and gets matched on five features, which is how a matcher
    starts confidently returning whichever corpus figure is nearest noise.
    """
    kp, desc = _keypoints(_sparse(1))
    assert desc is not None and 0 < len(kp) < match.MIN_KEYPOINTS, (
        f"fixture no longer sits between the operands: {len(kp)} keypoints, "
        f"desc={None if desc is None else desc.shape}"
    )
    _write(_textured(11), tmp_path / "c.png")

    v = match.by_orb(_png(_sparse(1)), [Row("c.png")], tmp_path)

    assert isinstance(v, match.CouldNotDecide), v
    assert "too smooth" in v.reason, v.reason


def test_the_too_smooth_message_reports_the_real_feature_count(tmp_path):
    """`{0 if dq is None else len(kq)}` survived `Is -> IsNot`.

    Inverted, an image with five features is reported as having zero and a
    featureless one reports its (nonexistent) count. The number is the whole
    actionable content of the message -- "0 features" reads as a broken file,
    "5 features (need 25)" reads as "crop a busier region".
    """
    kp, _ = _keypoints(_sparse(1))
    _write(_textured(12), tmp_path / "c.png")

    v = match.by_orb(_png(_sparse(1)), [Row("c.png")], tmp_path)

    assert isinstance(v, match.CouldNotDecide)
    assert f"only {len(kp)} visual features" in v.reason, (
        f"the message did not name the {len(kp)} features actually found: {v.reason!r}"
    )


def test_a_query_exactly_at_the_keypoint_floor_is_accepted(tmp_path, monkeypatch):
    """`len(kq) < MIN_KEYPOINTS` survived `Lt -> LtE`.

    MIN_KEYPOINTS is the fewest features still workable, not the first count
    refused. The threshold is moved onto the measured count rather than
    hunting for an image with exactly 25 features, so this stays true whatever
    the detector does next release.

    The corpus is unreadable on purpose, so passing the guard lands on a
    DIFFERENT CouldNotDecide -- "no corpus figure could be read" -- and the
    two reasons tell the two paths apart. Asserting the verdict type alone
    would pass either way.
    """
    kp, _ = _keypoints(_sparse(2))
    monkeypatch.setattr(match, "MIN_KEYPOINTS", len(kp))

    v = match.by_orb(_png(_sparse(2)), [Row("missing.png")], tmp_path)

    assert isinstance(v, match.CouldNotDecide)
    assert "too smooth" not in v.reason, (
        f"a query exactly at the floor was refused as too smooth: {v.reason!r}"
    )
    assert "no corpus figure" in v.reason, v.reason


def test_one_feature_below_the_floor_is_refused(monkeypatch, tmp_path):
    """Positive control for the boundary above: "accept everything" passes it."""
    kp, _ = _keypoints(_sparse(2))
    monkeypatch.setattr(match, "MIN_KEYPOINTS", len(kp) + 1)
    _write(_textured(13), tmp_path / "c.png")

    v = match.by_orb(_png(_sparse(2)), [Row("c.png")], tmp_path)

    assert isinstance(v, match.CouldNotDecide)
    assert "too smooth" in v.reason, v.reason


# ------------------------------------- ORB: a corpus figure that cannot be read


def test_a_corpus_of_unusable_figures_is_could_not_decide_not_no_match(tmp_path):
    """`if d is None or len(d) < 10` survived `Or -> And`.

    This is the cardinal distinction in one line. If every corpus figure is
    too sparse to compare, the tool has searched NOTHING and must say so.
    Under `and` those rows are no longer skipped: they enter the tally with
    zero inliers, `scored` is non-empty, and the function returns NoMatch --
    an assertion that the figure is absent from a corpus it never read.
    """
    _write(_sparse(1), tmp_path / "sparse.png")
    kp, desc = _keypoints(_sparse(1))
    assert desc is not None and len(desc) < 10, (
        f"fixture must have real-but-too-few descriptors: {desc.shape}"
    )

    v = match.by_orb(_png(_textured(21)), [Row("sparse.png")], tmp_path)

    assert isinstance(v, match.CouldNotDecide), (
        f"an unreadable corpus was reported as a searched-and-empty one: {v}"
    )
    assert "no corpus figure could be read" in v.reason, v.reason


def _cache(dirpath, name, desc, pts):
    """Write the build-time descriptor cache entry `_target_features` prefers.

    Going through the cache is what makes an EXACT descriptor count possible:
    no image has exactly ten ORB features on request, but a cache entry can.
    """
    dirpath.mkdir(parents=True, exist_ok=True)
    np.savez(str(dirpath / (name + ".npz")), desc=desc, pts=pts)


def test_a_corpus_figure_with_exactly_ten_descriptors_is_usable(tmp_path):
    """`len(d) < 10` survived both `Lt -> LtE` and `10 -> 11`.

    Ten is the fewest descriptors still worth comparing, not the first count
    discarded. Both mutants drop the row, which empties `scored` and turns the
    verdict from NoMatch into CouldNotDecide -- the same collapse as the test
    above, one row at a time.
    """
    rnd = np.random.RandomState(4)
    desc = rnd.randint(0, 256, size=(10, 32), dtype=np.uint8)
    pts = rnd.rand(10, 2).astype(np.float32) * 100
    _cache(tmp_path / "desc", "c.png", desc, pts)

    v = match.by_orb(_png(_textured(22)), [Row("c.png")], tmp_path, tmp_path / "desc")

    assert isinstance(v, match.NoMatch), (
        f"a corpus figure with exactly 10 descriptors was discarded as unreadable: {v}"
    )


def test_a_corpus_figure_with_nine_descriptors_is_discarded(tmp_path):
    """Positive control: "keep every row" passes the test above."""
    rnd = np.random.RandomState(5)
    desc = rnd.randint(0, 256, size=(9, 32), dtype=np.uint8)
    pts = rnd.rand(9, 2).astype(np.float32) * 100
    _cache(tmp_path / "desc", "c.png", desc, pts)

    v = match.by_orb(_png(_textured(23)), [Row("c.png")], tmp_path, tmp_path / "desc")

    assert isinstance(v, match.CouldNotDecide), v
    assert "no corpus figure could be read" in v.reason, v.reason


# ------------------------------------------- ORB: inliers, margin, runner-up


def _two_similar_corpus_figures(tmp_path):
    """Two corpus rows that both match the query about equally well.

    Same source texture saved twice under different names: whatever the query
    scores against one it scores against the other, so the margin collapses
    toward 1.0 and the "too close to call" branch becomes reachable.
    """
    img = _textured(31)
    _write(img, tmp_path / "a.png")
    _write(img, tmp_path / "b.png")
    return [Row("a.png", "10.1/a"), Row("b.png", "10.1/b")]


def test_two_indistinguishable_candidates_are_too_close_to_call(tmp_path):
    """`second = scored[1][0] if len(scored) > 1 else 0` survived `1 -> 2`.

    Under the mutant a TWO-row corpus takes the `else 0` branch, so the
    runner-up scores zero, the margin becomes `best_n / 1` -- a large number
    by construction -- and two figures the matcher genuinely cannot separate
    are reported as a confident hit on whichever sorted first. Picking between
    two identical candidates by sort order is the worst available outcome:
    it is wrong half the time and never says so.
    """
    rows = _two_similar_corpus_figures(tmp_path)
    crop = _textured(31).crop((0, 0, 200, 200))

    v = match.by_orb(_png(crop), rows, tmp_path)

    assert isinstance(v, match.CouldNotDecide), (
        f"two identical corpus figures produced a confident verdict: {v}"
    )
    assert "too close to call" in v.reason, v.reason


def test_a_score_exactly_at_the_inlier_floor_is_a_match(tmp_path, monkeypatch):
    """`if best_n < MIN_INLIERS` survived `Lt -> LtE`.

    Self-calibrated: run once with the floor dropped to learn what this crop
    actually scores, then put the floor exactly on that score. MIN_INLIERS is
    the lowest score still assertable, so at the floor the answer is a Match.
    Under `<=` it becomes NoMatch -- a real, correctly-identified figure
    reported as absent from the corpus it is sitting in.
    """
    truth = _textured(41)
    _write(truth, tmp_path / "truth.png")
    rows = [Row("truth.png", "10.1/truth")]
    for i in range(3):
        _write(_textured(50 + i), tmp_path / f"d{i}.png")
        rows.append(Row(f"d{i}.png", f"10.1/d{i}"))
    crop = _png(truth.crop((0, 0, 180, 180)))

    monkeypatch.setattr(match, "MIN_INLIERS", 0)
    monkeypatch.setattr(match, "MIN_MARGIN", 0.0)
    probe = match.by_orb(crop, rows, tmp_path)
    assert isinstance(probe, match.Match), f"probe run did not match: {probe}"

    monkeypatch.setattr(match, "MIN_INLIERS", int(probe.score))
    v = match.by_orb(crop, rows, tmp_path)

    assert isinstance(v, match.Match), (
        f"a score of exactly {probe.score} was refused by a floor of the same "
        f"value: {v}"
    )
    assert v.doi == "10.1/truth"


def test_a_score_one_below_the_inlier_floor_is_no_match(tmp_path, monkeypatch):
    """Positive control for the floor: "assert everything" passes the above."""
    truth = _textured(41)
    _write(truth, tmp_path / "truth.png")
    rows = [Row("truth.png", "10.1/truth")]
    for i in range(3):
        _write(_textured(50 + i), tmp_path / f"d{i}.png")
        rows.append(Row(f"d{i}.png", f"10.1/d{i}"))
    crop = _png(truth.crop((0, 0, 180, 180)))

    monkeypatch.setattr(match, "MIN_INLIERS", 0)
    monkeypatch.setattr(match, "MIN_MARGIN", 0.0)
    probe = match.by_orb(crop, rows, tmp_path)

    monkeypatch.setattr(match, "MIN_INLIERS", int(probe.score) + 1)
    v = match.by_orb(crop, rows, tmp_path)

    assert isinstance(v, match.NoMatch), v


def test_a_margin_exactly_at_the_floor_is_asserted(tmp_path, monkeypatch):
    """`if margin < MIN_MARGIN` survived `Lt -> LtE`.

    Same self-calibration one threshold along. At exactly the required margin
    the hit is asserted; under `<=` it degrades to "too close to call", so the
    tool declines on the very cases the constant was tuned to accept.
    """
    truth = _textured(61)
    _write(truth, tmp_path / "truth.png")
    rows = [Row("truth.png", "10.1/truth")]
    for i in range(3):
        _write(_textured(70 + i), tmp_path / f"d{i}.png")
        rows.append(Row(f"d{i}.png", f"10.1/d{i}"))
    crop = _png(truth.crop((0, 0, 190, 190)))

    monkeypatch.setattr(match, "MIN_MARGIN", 0.0)
    probe = match.by_orb(crop, rows, tmp_path)
    assert isinstance(probe, match.Match), probe

    monkeypatch.setattr(match, "MIN_MARGIN", probe.margin)
    v = match.by_orb(crop, rows, tmp_path)

    assert isinstance(v, match.Match), (
        f"a margin of exactly {probe.margin} was refused by a floor of the "
        f"same value: {v}"
    )


def test_a_margin_just_under_the_floor_is_declined(tmp_path, monkeypatch):
    """Positive control for the margin floor."""
    truth = _textured(61)
    _write(truth, tmp_path / "truth.png")
    rows = [Row("truth.png", "10.1/truth")]
    for i in range(3):
        _write(_textured(70 + i), tmp_path / f"d{i}.png")
        rows.append(Row(f"d{i}.png", f"10.1/d{i}"))
    crop = _png(truth.crop((0, 0, 190, 190)))

    monkeypatch.setattr(match, "MIN_MARGIN", 0.0)
    probe = match.by_orb(crop, rows, tmp_path)

    monkeypatch.setattr(match, "MIN_MARGIN", probe.margin * 1.01)
    v = match.by_orb(crop, rows, tmp_path)

    assert isinstance(v, match.CouldNotDecide), v
    assert "too close to call" in v.reason, v.reason


# ------------------------------- ORB: the ratio test and the homography floor
#
# The two guards below sit BETWEEN the detector and the verdict, and no image
# can be commissioned to produce an exact number of good matches. Here the
# matcher is stubbed so the pair distances are exactly what the boundary
# needs, while the query keypoints, the descriptor cache and the homography
# are all real. The real-detector tests above are what keep this file honest
# about the stub: if the stubbed geometry stopped resembling the real thing,
# those tests -- not these -- are the ones that would notice.


class _Pair:
    """One cv2.DMatch, with only the three fields `by_orb` reads."""

    def __init__(self, q, t, distance):
        self.queryIdx, self.trainIdx, self.distance = q, t, distance


def _stub_matcher(monkeypatch, n_pairs, m_dist, s_dist):
    """Make knnMatch return exactly `n_pairs` (best, second) pairs.

    Indices are the identity, and the cached corpus points are written as a
    copy of the query keypoints, so any pair that survives the ratio test maps
    a point onto itself: the homography is the identity and every good match
    is an inlier. That makes `len(good)` the ONLY thing that varies.
    """
    import cv2

    pairs = [
        [_Pair(i, i, m_dist), _Pair(i, (i + 1) % n_pairs, s_dist)]
        for i in range(n_pairs)
    ]

    class _BF:
        def knnMatch(self, a, b, k=2):
            return pairs

    monkeypatch.setattr(cv2, "BFMatcher", lambda *a, **kw: _BF())


def _identity_corpus(tmp_path, query_img, n=16):
    """A cache entry whose points ARE the query's first `n` keypoints."""
    kp, _ = _keypoints(query_img)
    assert len(kp) >= n, f"query only has {len(kp)} keypoints"
    pts = np.float32([kp[i].pt for i in range(n)])
    desc = np.random.RandomState(9).randint(0, 256, size=(n, 32), dtype=np.uint8)
    _cache(tmp_path / "desc", "c.png", desc, pts)
    return [Row("c.png", "10.1/craft")]


@pytest.fixture
def _low_floors(monkeypatch):
    """Eight inliers is below the shipped floor of 15, so the floor is lowered
    to let the 8-point guard be the thing under test rather than the one that
    masks it."""
    monkeypatch.setattr(match, "MIN_INLIERS", 5)
    monkeypatch.setattr(match, "MIN_MARGIN", 1.0)


def test_exactly_eight_good_matches_are_enough_for_a_homography(
    tmp_path, monkeypatch, _low_floors
):
    """`if len(good) >= 8` survived both `8 -> 9` and `GtE -> Gt`.

    Eight is the minimum a homography can be fitted from, so at exactly eight
    the fit must run. Under either mutant it is skipped, `inliers` stays 0,
    and a figure that could have been identified returns NoMatch -- the tool
    declining to look at the very cases the constant was chosen to admit.
    """
    q = _textured(81)
    rows = _identity_corpus(tmp_path, q)
    _stub_matcher(monkeypatch, 8, 0, 100)

    v = match.by_orb(_png(q), rows, tmp_path, tmp_path / "desc")

    assert isinstance(v, match.Match), (
        f"exactly 8 good matches did not reach the homography: {v}"
    )
    assert v.score == 8.0, v.score


def test_seven_good_matches_are_not_enough(tmp_path, monkeypatch, _low_floors):
    """Positive control: "always fit" passes the test above, and a homography
    from fewer than eight points is not determined."""
    q = _textured(81)
    rows = _identity_corpus(tmp_path, q)
    _stub_matcher(monkeypatch, 7, 0, 100)

    v = match.by_orb(_png(q), rows, tmp_path, tmp_path / "desc")

    assert isinstance(v, match.NoMatch), v


def test_a_pair_exactly_at_the_ratio_is_not_a_good_match(
    tmp_path, monkeypatch, _low_floors
):
    """`m.distance < 0.75 * s.distance` survived `Lt -> LtE`.

    Lowe's ratio test keeps a match only when the best candidate is CLEARLY
    better than the second -- exactly at the ratio it is not clearly better,
    so it is discarded. Loosening to `<=` admits the ambiguous pairs the test
    exists to remove, which is how spurious geometry starts accumulating
    inliers on unrelated figures.
    """
    q = _textured(82)
    rows = _identity_corpus(tmp_path, q)
    _stub_matcher(monkeypatch, 8, 75, 100)  # 75 == 0.75 * 100, exactly

    v = match.by_orb(_png(q), rows, tmp_path, tmp_path / "desc")

    assert isinstance(v, match.NoMatch), (
        f"pairs exactly at the ratio were kept as good matches: {v}"
    )


def test_a_pair_just_inside_the_ratio_is_a_good_match(
    tmp_path, monkeypatch, _low_floors
):
    """Positive control: "discard everything" passes the test above."""
    q = _textured(82)
    rows = _identity_corpus(tmp_path, q)
    _stub_matcher(monkeypatch, 8, 74, 100)  # just inside 0.75

    v = match.by_orb(_png(q), rows, tmp_path, tmp_path / "desc")

    assert isinstance(v, match.Match), v
    assert v.score == 8.0, v.score


def test_a_featureless_query_reports_zero_features_not_one(tmp_path):
    """The `0` in `{0 if dq is None else len(kq)}` survived `0 -> 1`.

    When the detector returns nothing there is nothing to count, and the
    message has to say zero. "only 1 visual feature" describes a state that
    cannot occur and sends the reader looking for the one feature that was
    found.
    """
    _write(_textured(91), tmp_path / "c.png")
    blank = Image.new("RGB", (300, 300), "white")

    v = match.by_orb(_png(blank), [Row("c.png")], tmp_path)

    assert isinstance(v, match.CouldNotDecide)
    assert "only 0 visual features" in v.reason, v.reason


# ---------------------------- a corpus figure OpenCV refuses to look at


def test_a_degenerate_corpus_figure_does_not_sink_the_whole_query(tmp_path):
    """Found by this file's own probe, not by a mutant.

    While checking whether the two operands of `desc is None or not kp` can
    ever disagree, a 1x1 PNG turned out to make `detectAndCompute` RAISE --
    OpenCV refuses images smaller than its patch size instead of reporting no
    features. The exception escaped `_target_features`, escaped `by_orb`, and
    reached the caller: a query against a corpus containing one bad figure
    returned no verdict at all, which is a fourth outcome this tool does not
    have and no caller handles.

    One unusable row must cost that row, not the search.
    """
    truth = _textured(101)
    _write(truth, tmp_path / "truth.png")
    Image.new("RGB", (1, 1), "white").save(tmp_path / "degenerate.png")
    rows = [Row("degenerate.png", "10.1/bad"), Row("truth.png", "10.1/truth")]

    v = match.by_orb(_png(truth.crop((0, 0, 200, 200))), rows, tmp_path)

    assert isinstance(v, match.Match), (
        f"a good figure was not found because another row was unreadable: {v}"
    )
    assert v.doi == "10.1/truth"


def test_a_corpus_of_only_unreadable_figures_is_could_not_decide(tmp_path):
    """And when EVERY row is refused, the answer is the third outcome.

    NoMatch here would assert that the query is absent from a corpus not one
    figure of which was ever compared against it.
    """
    for i in range(3):
        Image.new("RGB", (1, 1), "white").save(tmp_path / f"bad{i}.png")
    rows = [Row(f"bad{i}.png", f"10.1/b{i}") for i in range(3)]

    v = match.by_orb(_png(_textured(102)), rows, tmp_path)

    assert isinstance(v, match.CouldNotDecide), v
    assert "no corpus figure could be read" in v.reason, v.reason


def test_a_lone_candidate_too_close_to_call_reports_a_runner_up_of_zero(
    tmp_path, monkeypatch
):
    """The second equivalence proof I got wrong.

    `second = scored[1][0] if len(scored) > 1 else 0` -- mutated to `else 1`.
    I argued this was equivalent because the value only feeds
    `max(second, 1)`, and max(0,1) == max(1,1).

    It does not only feed that. Four lines down `second` is interpolated INTO
    the CouldNotDecide message: "the two best candidates are too close to call
    ({best_n} vs {second})". With one corpus row the mutant makes that read
    "(8 vs 1)" -- naming a runner-up that does not exist.

    Reaching it needs `MIN_INLIERS <= best_n < MIN_MARGIN`, which the shipped
    constants (15 and 3.0) make impossible -- so the proof was true of
    PRODUCTION and false of the code. A test may set those constants, and
    several in this file already do. "No test can ever kill it" was a claim
    about the input space, not a proof.
    """
    monkeypatch.setattr(match, "MIN_INLIERS", 1)
    monkeypatch.setattr(match, "MIN_MARGIN", 100.0)

    q = _textured(83)
    rows = _identity_corpus(tmp_path, q)
    _stub_matcher(monkeypatch, 8, 0, 100)

    v = match.by_orb(_png(q), rows, tmp_path, tmp_path / "desc")

    assert isinstance(v, match.CouldNotDecide), v
    assert "(8 vs 0)" in v.reason, (
        f"a lone candidate reported a runner-up that does not exist: {v.reason!r}"
    )
