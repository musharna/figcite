"""The same figure carrying a DIFFERENT DOI than the one credited.

The same index as reverse sourcing, queried the other way round. It reports;
it never rewrites a record. A hit is a question for the user ("you credited
this to A, it also appears in B"), not a correction to apply.

Note the fixtures are TEXTURED, not flat fills. A solid-colour image has no
gradients at all, so its dhash is all zeros -- red and blue hash identically.
That is a property of the hash, and it is why `can_compare` exists.
"""

import io
import random

import pytest
from PIL import Image, ImageDraw

from figcite import corpus
from figcite.provenance import dhash_bytes


def _textured(seed, size=(96, 96)):
    rnd = random.Random(seed)
    im = Image.new("RGB", size, "white")
    d = ImageDraw.Draw(im)
    for _ in range(40):
        x, y = rnd.randrange(size[0] - 20), rnd.randrange(size[1] - 20)
        col = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        d.rectangle([x, y, x + rnd.randrange(4, 18), y + rnd.randrange(4, 18)], fill=col)
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def _flat(color="red"):
    b = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(b, "PNG")
    return b.getvalue()


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    monkeypatch.setattr(corpus, "CORPUS_DIR", tmp_path / "c")
    monkeypatch.setattr(corpus, "DB_PATH", tmp_path / "c" / "figures.sqlite")
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path / "c" / "images")
    monkeypatch.setattr(corpus, "DESCRIPTOR_DIR", tmp_path / "c" / "descriptors")

    conn = corpus.connect()
    blob = _textured(1)
    for pmcid, doi in (("PMC1", "10.1/credited"), ("PMC2", "10.1/elsewhere")):
        corpus.upsert(
            conn,
            corpus.FigureRow(
                pmcid=pmcid,
                doi=doi,
                label="Figure 1",
                caption="",
                licence="CC BY",
                source_url="",
                dhash=dhash_bytes(blob),
                width=96,
                height=96,
                image_path=f"{pmcid}/f1.png",
            ),
        )
    conn.close()
    return blob


def test_the_same_figure_under_another_doi_is_reported(wired):
    others = corpus.duplicates_of(wired, credited_doi="10.1/credited")
    assert [o.doi for o in others] == ["10.1/elsewhere"]


def test_the_credited_paper_is_not_reported_as_a_duplicate_of_itself(wired):
    others = corpus.duplicates_of(wired, credited_doi="10.1/credited")
    assert all(o.doi != "10.1/credited" for o in others)


def test_a_different_figure_has_no_duplicates(wired):
    """Positive control: the query must be able to return nothing."""
    assert corpus.duplicates_of(_textured(999), credited_doi="10.1/credited") == []


def test_the_doi_comparison_ignores_case(wired):
    """DOIs are case-insensitive; a case difference must not fake a duplicate."""
    others = corpus.duplicates_of(wired, credited_doi="10.1/CREDITED")
    assert [o.doi for o in others] == ["10.1/elsewhere"]


def test_no_credited_doi_reports_every_copy(wired):
    """An uncredited figure has nothing to exclude, so both papers are news."""
    others = corpus.duplicates_of(wired, credited_doi="")
    assert sorted(o.doi for o in others) == ["10.1/credited", "10.1/elsewhere"]


# ------------------------------------------------------- featureless images


def test_a_flat_image_cannot_be_compared_at_all():
    """Two different solid colours hash identically -- dhash sees no gradient.

    Reporting those as duplicates of each other would be a confident, wrong
    accusation about someone's citation. `can_compare` is the same rule as the
    ORB keypoint floor: refuse rather than guess.
    """
    assert dhash_bytes(_flat("red")) == dhash_bytes(_flat("blue")), (
        "premise of this test: flat fills collide"
    )
    assert corpus.can_compare(_flat("red")) is False


def test_a_textured_image_can_be_compared(wired):
    """Positive control: can_compare must not simply refuse everything."""
    assert corpus.can_compare(_textured(1)) is True


def test_a_flat_query_reports_no_duplicates_rather_than_false_ones(wired):
    conn = corpus.connect()
    corpus.upsert(
        conn,
        corpus.FigureRow(
            pmcid="PMC3",
            doi="10.1/flat",
            label="Figure 1",
            caption="",
            licence="",
            source_url="",
            dhash=dhash_bytes(_flat("red")),
            width=64,
            height=64,
            image_path="PMC3/f1.png",
        ),
    )
    conn.close()
    assert corpus.duplicates_of(_flat("blue"), credited_doi="10.1/other") == []


# ------------------------------------------------------------ service layer


def test_the_service_reports_other_dois_with_their_licence(wired, tmp_path):
    from figcite import service

    out = service.duplicates(_write_tmp(tmp_path, wired), credited_doi="10.1/credited")
    assert [o["doi"] for o in out["others"]] == ["10.1/elsewhere"]
    assert out["others"][0]["licence"] == "CC BY"
    assert out["reason"] == ""


def test_the_service_says_WHY_when_it_could_not_compare(wired, tmp_path):
    """ "No duplicates" and "I could not look" must not read the same."""
    from figcite import service

    out = service.duplicates(_write_tmp(tmp_path, _flat("blue")), credited_doi="10.1/x")
    assert out["others"] == []
    assert "compare" in out["reason"].lower() or "feature" in out["reason"].lower()


def test_the_service_distinguishes_a_genuine_absence(wired, tmp_path):
    """Positive control: a real 'nothing found' must not borrow the other reason."""
    from figcite import service

    out = service.duplicates(_write_tmp(tmp_path, _textured(999)), credited_doi="10.1/x")
    assert out["others"] == []
    assert "compare" not in out["reason"].lower()


_TMP_N = [0]


def _write_tmp(tmp_path, blob):
    """Write a query image where PYTEST says scratch space lives.

    This used to write `tempfile.gettempdir()/figcite_dup_{N}.png`, and the
    counter is a module global that restarts at 0 in every process -- so any
    two concurrent pytest runs both claim `/tmp/figcite_dup_1.png`. One writes
    a flat blue PNG while the other is reading back a textured one, and the
    reader gets a torn file: `PIL.UnidentifiedImageError: cannot identify image
    file`. It fails in whichever process lost the race, on whichever test got
    there first, which is why it reads as a flake.

    That is not hypothetical -- it is how this was found. A mutation sweep runs
    the suite in 8 processes at once; one worker's baseline came back red here
    while the other seven were green.

    The collision direction that matters is not the red one. A torn read makes
    a test FAIL, so under a mutation sweep it reports a mutant as KILLED that
    no test actually caught, and a killed mutant is a finding that never gets
    looked at. A shared-path flake in a suite is noise; in a harness that reads
    failure as evidence, it manufactures false coverage.

    `tmp_path` is per-test, unique per process, and pytest cleans it up, so the
    name cannot be claimed twice. It was available all along -- `wired` already
    takes it; this helper just could not reach it from module scope.
    """
    _TMP_N[0] += 1
    p = tmp_path / f"figcite_dup_{_TMP_N[0]}.png"
    p.write_bytes(blob)
    return str(p)
