"""ORB stage: the one that handles crops.

Measured on real figures with decoys from three unrelated papers: 7/7 correct
at a median 38.9x margin. Measured WITHOUT decoys the same technique scored
283x on a two-candidate test and 1.5x on a real retrieval task -- so every
test here carries decoys and asserts the margin, not just the top hit.
"""

import io
import random

import pytest
from PIL import Image, ImageDraw

from figcite import match


def _textured(seed, size=(320, 320)):
    """A high-feature synthetic figure. ORB needs corners; flat fills give none."""
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


def _write(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)


def _png(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


class Row:
    def __init__(self, image_path, doi):
        self.image_path, self.doi = image_path, doi
        self.pmcid, self.label = "PMC1", "Figure 1"


pytestmark = pytest.mark.skipif(
    not match.opencv_available(), reason="opencv not installed"
)


def test_a_panel_crop_finds_its_source_among_decoys(tmp_path):
    truth = _textured(11)
    _write(truth, tmp_path / "truth.png")
    rows = [Row("truth.png", "10.1/right")]
    for i in range(4):
        _write(_textured(100 + i), tmp_path / f"decoy{i}.png")
        rows.append(Row(f"decoy{i}.png", f"10.1/decoy{i}"))

    w, h = truth.size
    crop = truth.crop((0, 0, int(w * 0.55), int(h * 0.55)))

    v = match.by_orb(_png(crop), rows, tmp_path)
    assert isinstance(v, match.Match), v
    assert v.doi == "10.1/right"
    assert v.margin >= match.MIN_MARGIN, (
        f"margin {v.margin} too thin to assert a source"
    )


def test_a_figure_that_is_in_no_paper_is_a_real_no_match(tmp_path):
    """The positive control for the test above: NoMatch must be reachable."""
    rows = []
    for i in range(4):
        _write(_textured(200 + i), tmp_path / f"decoy{i}.png")
        rows.append(Row(f"decoy{i}.png", f"10.1/decoy{i}"))

    v = match.by_orb(_png(_textured(999)), rows, tmp_path)
    assert isinstance(v, match.NoMatch), v


def test_a_featureless_query_is_could_not_decide(tmp_path):
    """Measured on a real figure: a smooth panel yielded 2 keypoints.

    Reporting that as NoMatch would assert an absence nothing observed.
    """
    rows = []
    for i in range(3):
        _write(_textured(300 + i), tmp_path / f"decoy{i}.png")
        rows.append(Row(f"decoy{i}.png", f"10.1/d{i}"))

    blank = Image.new("RGB", (300, 300), "white")
    v = match.by_orb(_png(blank), rows, tmp_path)
    assert isinstance(v, match.CouldNotDecide)
    assert "feature" in v.reason.lower()


def test_without_opencv_the_verdict_is_could_not_decide(tmp_path, monkeypatch):
    """A silent degrade to NoMatch is indistinguishable from a real negative."""
    monkeypatch.setattr(match, "opencv_available", lambda: False)
    v = match.by_orb(_png(_textured(1)), [], tmp_path)
    assert isinstance(v, match.CouldNotDecide)
    assert "opencv" in v.reason.lower()
