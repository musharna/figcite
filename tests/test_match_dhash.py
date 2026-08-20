"""dhash stage: exact and rescaled hits only.

Measured on a real figure against threshold 6: re-save 0, rescale-to-50% 0,
crop-5%-each-edge 7, crop-10% 16. So dhash MUST NOT report NoMatch on a miss
-- a crop is invisible to it, and calling that "no match" asserts an absence
it cannot see.
"""

import io
import random

from PIL import Image

from figcite import match
from figcite.provenance import dhash_bytes


def _png(img):
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


class Row:
    def __init__(self, dh, doi):
        self.dhash, self.doi, self.pmcid, self.label = dh, doi, "PMC1", "Figure 1"


def test_an_identical_image_is_matched():
    img = _noise(1)
    rows = [
        Row(dhash_bytes(_png(img)), "10.1/right"),
        Row(dhash_bytes(_png(_noise(2))), "10.1/wrong"),
    ]
    v = match.by_dhash(_png(img), rows)
    assert isinstance(v, match.Match)
    assert v.doi == "10.1/right"
    assert v.method == "dhash"


def test_a_rescaled_image_is_matched():
    img = _noise(3, (128, 128))
    rows = [Row(dhash_bytes(_png(img)), "10.1/right")]
    small = img.resize((64, 64))
    v = match.by_dhash(_png(small), rows)
    assert isinstance(v, match.Match), "dhash is scale-invariant; this must hit"


def test_a_miss_is_could_not_decide_not_no_match():
    """The rule this stage exists to respect."""
    rows = [Row(dhash_bytes(_png(_noise(4))), "10.1/other")]
    v = match.by_dhash(_png(_noise(5)), rows)
    assert isinstance(v, match.CouldNotDecide)
    assert not isinstance(v, match.NoMatch)
    assert "crop" in v.reason.lower()


def test_an_empty_corpus_is_could_not_decide():
    v = match.by_dhash(_png(_noise(6)), [])
    assert isinstance(v, match.CouldNotDecide)
