"""A score means nothing without its unit, and the two matchers disagree.

`by_dhash` scores a HAMMING DISTANCE, where 0 is a perfect hit. `by_orb`
scores an INLIER COUNT, where higher is better. Rendered as a bare number
next to a DOI -- which is what both front ends did -- the same digit means
"exact match" or "barely anything" depending on which matcher produced it,
and the reader is given nothing to tell them apart.

Caught by looking at the real screen: a perfect dhash hit displayed `0`.

The fix labels the number where the polarity is known, in the matcher. The
guard below is deliberately STRUCTURAL rather than a list of matcher names:
`{"by_dhash", "by_orb"}` is a closed set standing in for an open one, and the
whole point is that a matcher added later must not be able to inherit
whichever unit a front end happened to hard-code.
"""

import ast
import inspect
from pathlib import Path

from figcite import match


def _match_constructions():
    """Every `Match(...)` call in match.py, by AST rather than by regex."""
    tree = ast.parse(Path(inspect.getfile(match)).read_text())
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Match"
    ]


def test_every_matcher_labels_its_own_score():
    calls = _match_constructions()
    # Without this the test passes vacuously on a file that builds no Match at
    # all -- e.g. after a rename this test did not follow.
    assert len(calls) >= 2, f"expected both matchers to build a Match, found {len(calls)}"

    for call in calls:
        kwargs = {k.arg for k in call.keywords}
        line = call.lineno
        assert "score_label" in kwargs, (
            f"match.py:{line} builds a Match without saying what its score "
            f"measures; a bare number is unreadable next to the other matcher's"
        )


def test_the_dhash_label_says_hamming_and_survives_a_perfect_hit(tmp_path):
    """The label has to be right, not merely present -- and the value that
    broke the screen (a perfect 0) is the one asserted."""
    from PIL import Image

    from figcite import corpus, provenance

    png = tmp_path / "fig.png"
    Image.new("RGB", (64, 48), "white").save(png)
    # NON-MONOTONIC texture, so the dhash is not the all-zero hash that
    # `can_compare_dhash` refuses. A left-to-right RAMP is not enough: every
    # dhash bit is "is this pixel brighter than the one to its right", so a
    # smooth ramp answers "no" 64 times and hashes to all zeros exactly like a
    # flat fill. This fixture asserted a gradient was sufficient and it was not.
    im = Image.new("RGB", (64, 48))
    for x in range(64):
        for y in range(48):
            v = (x * 37 + y * 101) % 256
            im.putpixel((x, y), (v, (v * 3) % 256, 0))
    im.save(png)
    blob = png.read_bytes()
    dh = provenance.dhash_bytes(blob)

    row = corpus.FigureRow(
        pmcid="PMC1",
        doi="10.1/x",
        label="Figure 1",
        caption="",
        licence="",
        source_url="",
        dhash=dh,
        width=64,
        height=48,
        image_path="a.png",
    )

    verdict = match.by_dhash(blob, [row])

    assert isinstance(verdict, match.Match), verdict
    assert verdict.score == 0.0, "premise: an identical image scores hamming 0"
    assert verdict.score_label == "hamming 0", verdict.score_label
