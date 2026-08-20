"""Descriptors are computed once at build time, not on every query.

`by_orb` decoded and re-processed every corpus image per query. At the measured
corpus size (~1,200 figures) that is tens of seconds per lookup, which makes the
pending card unusable.

Caching descriptors alone is not enough: RANSAC needs the keypoint COORDINATES
to fit a homography, and those are not recoverable from descriptors. So the
cache stores both, and the equivalence test below is what proves the cached
path and the decode path agree.
"""

import io
import random

import pytest
from PIL import Image, ImageDraw

from figcite import corpus, match


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


def _png_bytes(img):
    b = io.BytesIO()
    img.save(b, "PNG")
    return b.getvalue()


class Row:
    def __init__(self, image_path, doi):
        self.image_path, self.doi = image_path, doi
        self.pmcid, self.label = "PMC1", "Figure 1"


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "c")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "c" / "images")
    monkeypatch.setattr(corpus, "DESCRIPTOR_DIR", tmp_path / "c" / "descriptors")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "c" / "figures.sqlite")
    return tmp_path


needs_cv2 = pytest.mark.skipif(
    not match.opencv_available(), reason="opencv not installed"
)


@needs_cv2
def test_descriptors_are_written_for_a_figure(wired):
    assert corpus.write_descriptors("PMC1/f1.png", _png_bytes(_textured(1))) is True
    assert corpus.descriptor_path("PMC1/f1.png").exists()


def test_write_descriptors_is_a_no_op_without_opencv(wired, monkeypatch):
    """Absent opencv is a valid state, not an error."""
    monkeypatch.setattr(match, "opencv_available", lambda: False)
    assert corpus.write_descriptors("PMC1/f1.png", _png_bytes(_textured(1))) is False
    assert not corpus.descriptor_path("PMC1/f1.png").exists()


@needs_cv2
def test_a_featureless_image_caches_nothing_and_says_so(wired):
    """Positive control on the return value: it must be able to be False."""
    blank = Image.new("RGB", (300, 300), "white")
    assert corpus.write_descriptors("PMC1/blank.png", _png_bytes(blank)) is False


@needs_cv2
def test_the_cached_path_and_the_decode_path_agree(wired, tmp_path):
    """The correctness test for the whole optimisation.

    A cache that returns a different answer from the code it replaces is worse
    than no cache: every query would be wrong in a way no unit test of either
    path alone could see.
    """
    images = tmp_path / "img"
    images.mkdir()
    rows = []
    truth = _textured(11)
    for name, img in [("truth.png", truth)] + [
        (f"decoy{i}.png", _textured(100 + i)) for i in range(4)
    ]:
        img.save(images / name)
        rows.append(Row(name, f"10.1/{name}"))

    w, h = truth.size
    query = _png_bytes(truth.crop((0, 0, int(w * 0.55), int(h * 0.55))))

    uncached = match.by_orb(query, rows, images)

    for row in rows:
        corpus.write_descriptors(row.image_path, (images / row.image_path).read_bytes())
    cached = match.by_orb(query, rows, images, corpus.DESCRIPTOR_DIR)

    assert type(uncached) is type(cached), f"{uncached!r} != {cached!r}"
    assert isinstance(cached, match.Match)
    assert cached.doi == uncached.doi


@needs_cv2
def test_the_cache_is_actually_used(wired, tmp_path):
    """Delete the images after caching; retrieval must still work.

    Without this, a cache that silently fell back to decoding every time would
    pass every other test here while delivering none of the speed it exists for.
    """
    images = tmp_path / "img2"
    images.mkdir()
    rows = []
    truth = _textured(21)
    for name, img in [("truth.png", truth)] + [
        (f"decoy{i}.png", _textured(200 + i)) for i in range(3)
    ]:
        img.save(images / name)
        rows.append(Row(name, f"10.1/{name}"))
    for row in rows:
        assert corpus.write_descriptors(
            row.image_path, (images / row.image_path).read_bytes()
        )

    for row in rows:  # the images are gone; only the cache remains
        (images / row.image_path).unlink()

    w, h = truth.size
    query = _png_bytes(truth.crop((0, 0, int(w * 0.55), int(h * 0.55))))
    v = match.by_orb(query, rows, images, corpus.DESCRIPTOR_DIR)
    assert isinstance(v, match.Match), v
    assert v.doi == "10.1/truth.png"
