"""An image figcite cannot read is CouldNotDecide, never NoMatch, never a crash.

Found by the PROVENANCE-STATE mutation tier -- verdict-constructor replacement,
which mutates `Match` <-> `NoMatch` <-> `CouldNotDecide` at every return site.
Neither comparison nor boolean mutation can synthesize this class: the guards
are all correct and the tests all pass, and the wrong CONSTRUCTOR still goes
unnoticed. Of 28 such mutants, 27 died. The survivor was

    match.py:126  CouldNotDecide -> NoMatch

on `by_orb`'s undecodable-query guard, and chasing it found a live crash in the
sibling matcher.

## Why the distinction is the whole product

"No match" means figcite searched your corpus and your figure is not in it --
you should go find the source another way. "Could not decide" means figcite
never managed to look. Reporting the second as the first tells a user their
figure is absent on the strength of a search that never happened, which is the
one failure this tool exists to prevent.

## What was actually broken

`by_orb` handled it (returning CouldNotDecide) but nothing asserted so, hence
the surviving mutant. `by_dhash` did not handle it at all: `dhash_bytes` hands
bytes to PIL, PIL RAISES on a file it cannot read, and `service.whereis` calls
`by_dhash` first with no guard. Measured against a real corpus before the fix:

    $ figcite whereis corrupt.png
    OSError: Truncated File Read        <- raw traceback, exit 1

`cli.main` did not soften it either: it catches only `(LookupError,
RuntimeError, ValueError)`, and OSError is in none of those.

Root cause was the ERROR BOUNDARY, not the file: `by_dhash` commits to three
outcomes and left decoding outside them. Same root cause as the cv2 crash
this audit already fixed in `_target_features` -- and the same twin asymmetry,
where one of a pair of matchers gets the lesson and the other does not.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from figcite import corpus, match, provenance, service, session_tabs

# Each is a real way a user hands over something that is not a readable image.
UNREADABLE = {
    "not-an-image-at-all": b"this is plain text, not an image",
    "truncated-png": b"\x89PNG\r\n\x1a\n" + b"chopped off here",
    "empty-file": b"",
    "pdf-handed-over-by-mistake": b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n",
}


def _row(dhash="0f0f0f0f0f0f0f0f", image_path="fig.png"):
    return corpus.FigureRow(
        pmcid="PMC1",
        doi="10.1/x",
        label="Figure 1",
        caption="c",
        licence="cc-by",
        source_url="http://example.invalid/x",
        dhash=dhash,
        width=64,
        height=64,
        image_path=image_path,
    )


def _readable_png(colour=(10, 120, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), colour).save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------- the matchers


@pytest.mark.parametrize("blob", UNREADABLE.values(), ids=list(UNREADABLE))
def test_dhash_answers_instead_of_raising(blob):
    """`by_dhash` used to let PIL's exception escape.

    Not a verdict at all -- neither Match, nor NoMatch, nor CouldNotDecide --
    so `whereis` crashed rather than answering.
    """
    verdict = match.by_dhash(blob, [_row()])

    assert isinstance(verdict, match.CouldNotDecide), verdict
    assert "could not be decoded" in verdict.reason, verdict.reason


@pytest.mark.parametrize("blob", UNREADABLE.values(), ids=list(UNREADABLE))
def test_orb_says_could_not_decide_and_not_no_match(blob):
    """The surviving mutant: `CouldNotDecide -> NoMatch` at match.py:126.

    Under it, a file figcite cannot open reports "your figure is not in the
    corpus" off the back of a search that never ran.
    """
    if not match.opencv_available():
        pytest.skip("opencv not installed")

    verdict = match.by_orb(blob, [_row()], ".")

    assert isinstance(verdict, match.CouldNotDecide), verdict
    assert not isinstance(verdict, match.NoMatch)
    assert "could not be decoded" in verdict.reason, verdict.reason


def test_a_readable_image_is_not_swallowed_by_the_new_guard():
    """Positive control, and it has to be here.

    Every assertion above is satisfied by a `by_dhash` that returns
    CouldNotDecide unconditionally -- including one whose try/except is far too
    wide and eats a working query. This drives a genuinely readable image and
    requires the guard NOT to fire.
    """
    blob = _readable_png()
    verdict = match.by_dhash(blob, [_row(dhash=provenance.dhash_bytes(blob))])

    assert isinstance(verdict, match.Match), verdict
    assert verdict.doi == "10.1/x"


# ---------------------------------------------------------------- end to end


@pytest.fixture
def one_figure_corpus(tmp_path, monkeypatch):
    """A corpus with exactly one real figure, so `rows` is non-empty.

    The empty-corpus branch returns before either matcher touches the query,
    so it hides this bug completely -- which is why the crash needs a real row
    to reproduce and why a fixture that forgot one would prove nothing.
    """
    blob = _readable_png()
    img = tmp_path / "fig.png"
    img.write_bytes(blob)
    monkeypatch.setattr(corpus, "IMAGE_DIR", tmp_path)

    conn = corpus.connect()
    corpus.upsert(conn, _row(dhash=provenance.dhash_bytes(blob)))
    conn.commit()
    assert corpus.all_rows(conn), "the fixture must leave a non-empty corpus"

    # Open tabs are a garnish on the answer and reading them shells out to
    # PowerShell, which conftest blocks in a non-live test. Stubbed so the
    # assertions below are about the MATCHER, not about Windows interop.
    monkeypatch.setattr(session_tabs, "tab_candidates", lambda path=None: [])
    return tmp_path


def test_whereis_on_an_unreadable_file_answers_could_not_decide(
    one_figure_corpus, tmp_path
):
    """The user-visible end of it: `figcite whereis corrupt.png`.

    Before the fix this raised `OSError: Truncated File Read` straight through
    `cli.main`, whose handler covers only LookupError/RuntimeError/ValueError.
    """
    bad = tmp_path / "corrupt.png"
    bad.write_bytes(UNREADABLE["truncated-png"])

    out = service.whereis(str(bad))

    assert out["verdict"] == "could-not-decide", out
    assert out["matches"] == [], out
    assert "decoded" in out["reason"], out["reason"]


def test_whereis_still_finds_a_figure_it_can_read(one_figure_corpus, tmp_path):
    """Positive control for the end-to-end assertion above.

    A `whereis` that returned could-not-decide for everything would satisfy
    the previous test perfectly.
    """
    good = tmp_path / "query.png"
    good.write_bytes((one_figure_corpus / "fig.png").read_bytes())

    out = service.whereis(str(good))

    assert out["verdict"] == "match", out
    assert [m["doi"] for m in out["matches"]] == ["10.1/x"], out
