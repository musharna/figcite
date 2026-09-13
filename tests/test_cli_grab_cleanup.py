"""`figcite grab` deletes its scratch crop. It must not delete your output.

`cmd_grab` ends with:

    if not a.out and tmp.exists() and tmp != dest:
        tmp.unlink()

Every one of those three conjuncts is load-bearing and none was tested. The
mutation sweep flipped `and` to `or` and `!=` to `==`; both survived the whole
suite. The `or` form is the one that costs you something: when `--out` IS
given, `tmp` IS the path you asked for (`tmp = Path(a.out) if a.out`), so the
unlink deletes the file the command was run to produce -- after reporting
success.

The guard exists to clean up a scratch file. Getting it wrong turns it into a
delete of the user's own output, which is why it gets a test with a real PDF
and a real file on disk rather than an assertion about a boolean.
"""

import fitz
import pytest

from figcite import cli
from figcite.provenance import Record

DOI = "10.1234/demo-doi"


@pytest.fixture
def pdf(tmp_path):
    """A one-page PDF with a DOI in its text and something to crop."""
    path = tmp_path / "paper.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), f"https://doi.org/{DOI}")
    page.draw_rect(fitz.Rect(72, 200, 300, 400), color=(0, 0, 1), fill=(0.2, 0.4, 0.9))
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(autouse=True)
def _no_crossref(monkeypatch):
    """`grab` resolves the DOI through CrossRef; that is not what is under
    test here, and a non-live test must not reach the network anyway."""
    monkeypatch.setattr(
        cli,
        "_record_for",
        lambda *a, **kw: Record(
            doi=DOI,
            citation="Demo et al. 2024",
            short_cite="Demo 2024",
            confirmed=True,
            source_kind="pdf-crop",
        ),
    )


def test_the_out_file_you_asked_for_still_exists(tmp_path, pdf):
    """THE finding. With `--out`, tmp IS dest, and an `or` in that guard
    unlinks it -- after the command has printed that it filed the figure."""
    out = tmp_path / "my-figure.png"

    rc = cli.main(
        [
            "grab",
            str(pdf),
            "--page",
            "1",
            "--out",
            str(out),
            "--rect",
            "0.1,0.3,0.9,0.9",
            "--frac",
        ]
    )

    assert rc == 0, "grab did not succeed, so the assertion below proves nothing"
    assert out.exists(), (
        "figcite grab reported success and then deleted the --out file it was asked to produce"
    )
    assert out.stat().st_size > 0, f"the output file is empty: {out}"


def test_the_scratch_crop_is_cleaned_up_when_no_out_is_given(tmp_path, pdf, monkeypatch):
    """The other half, and the reason the guard exists at all.

    Without this, 'never unlink anything' would pass the test above while
    leaving a tmp-crop.png behind on every run.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(cli.store, "DATA_DIR", home)

    rc = cli.main(["grab", str(pdf), "--page", "1", "--rect", "0.1,0.3,0.9,0.9", "--frac"])

    assert rc == 0
    assert not (home / "tmp-crop.png").exists(), (
        "the scratch crop was left behind; the cleanup no longer runs"
    )


def test_a_missing_pdf_exits_nonzero(tmp_path):
    """Exit codes for `grab` were untested in both directions -- `return 2`
    survived becoming `return 3`, and the success `return 0` survived becoming
    `1`. A CLI that exits 0 on failure is silently ignored by any script that
    runs it."""
    rc = cli.main(["grab", str(tmp_path / "nope.pdf"), "--page", "1"])
    assert rc == 2, f"a missing PDF exited {rc}"


def test_a_malformed_rect_exits_nonzero(pdf):
    rc = cli.main(["grab", str(pdf), "--page", "1", "--rect", "1,2,3"])
    assert rc == 2, f"a 3-value --rect exited {rc}"

    # Five, not just three. `len(parts) != 4` compares for INEQUALITY, and a
    # 3-value rect sorts BELOW 4, so a guard narrowed to `len(parts) < 4`
    # rejects it too and this test passes on the narrowed guard -- which is
    # what the reduced-ROR sweep found. Too MANY coordinates is the half it
    # lets through, and `tuple(parts)` then hands crop() a 5-tuple where it
    # expects x0,y0,x1,y1.
    rc = cli.main(["grab", str(pdf), "--page", "1", "--rect", "1,2,3,4,5"])
    assert rc == 2, f"a 5-value --rect exited {rc}"

    # And the premise, so the pair keeps bracketing 4 if anyone edits it.
    assert len("1,2,3".split(",")) < 4 < len("1,2,3,4,5".split(","))
