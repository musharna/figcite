"""BibTeX handoff to ghostcite.

The central design claim -- that the handoff must carry a claimed byline, not
just identifiers -- was measured against the real ghostcite on 2026-08-16:

    two real DOIs, as a bare DOI list  -> 0 findings
    the same two as BibTeX, one with a
    fabricated first author            -> 1 finding, on the fabricated one

A DOI list gives a byline checker nothing to disagree with, so it reports clean
no matter what. `test_entry_carries_the_fields_ghostcite_compares` is what keeps
that from regressing silently.
"""

from __future__ import annotations

import shutil

import pytest

from figcite import bibtex
from figcite.provenance import Record


def _rec(**kw) -> Record:
    base = dict(
        sha256="a" * 64,
        doi="10.1038/s41586-020-2649-2",
        citation="Harris, Charles R. et al. (2020). Array programming with NumPy.",
        short_cite="Harris et al. 2020",
        authors=["Harris, Charles R.", "Millman, K. Jarrod"],
        year=2020,
        title="Array programming with NumPy",
        container="Nature",
        source_kind="pdf-crop",
        confirmed=True,
    )
    base.update(kw)
    return Record(**base)


# ------------------------------------------------------- what ghostcite needs


def test_entry_carries_the_fields_ghostcite_compares():
    """ghostcite compares the CLAIMED first author and year against CrossRef.

    Drop those and the check still runs, still exits 0, and can no longer catch
    anything -- the inert-DOI-list failure, reintroduced one field at a time.
    """
    txt = bibtex.entry(_rec(), "harris2020")
    assert "author = {Harris, Charles R. and Millman, K. Jarrod}" in txt
    assert "year = {2020}" in txt
    assert "title = {Array programming with NumPy}" in txt
    assert "doi = {10.1038/s41586-020-2649-2}" in txt


def test_first_author_surname_is_not_reformatted():
    """ghostcite compares the first author's surname; every reformat is a chance
    to corrupt exactly the field under test."""
    txt = bibtex.entry(_rec(authors=["van der Waals, Johannes D."]), "k")
    assert "author = {van der Waals, Johannes D.}" in txt


# -------------------------------------------------------------- escaping


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Genes & Development", r"Genes \& Development"),
        ("50% of loci", r"50\% of loci"),
        ("cost in $ terms", r"cost in \$ terms"),
        ("snake_case_gene", r"snake\_case\_gene"),
        ("a #hashtag", r"a \#hashtag"),
    ],
)
def test_special_characters_are_escaped(raw, expected):
    """A bare & in a title silently breaks the .bib for whatever reads it next,
    and usually surfaces as a missing reference rather than an error."""
    assert bibtex.escape(raw) == expected


def test_escaped_title_reaches_the_entry():
    txt = bibtex.entry(_rec(title="Genes & Development in 50% of loci"), "k")
    assert r"Genes \& Development in 50\% of loci" in txt


# ------------------------------------------------------------- cite keys


def test_cite_key_is_author_and_year():
    assert bibtex.cite_key(_rec()) == "harris2020"


def test_cite_keys_are_disambiguated():
    taken: set[str] = set()
    keys = [bibtex.cite_key(_rec(), taken) for _ in range(3)]
    assert keys == ["harris2020", "harris2020a", "harris2020b"]
    assert len(set(keys)) == 3, "duplicate keys make BibTeX drop entries"


def test_cite_key_survives_a_missing_author():
    assert bibtex.cite_key(_rec(authors=[], year=None)) == "anonnd"


# ------------------------------------------------------- inclusion policy


def test_unconfirmed_records_are_excluded_by_default():
    """An unconfirmed record is a machine's guess about WHICH paper a figure came
    from. ghostcite cannot catch that error -- the byline matches the DOI
    perfectly, because both came from CrossRef. It is the DOI-to-figure link that
    is unverified, and no bibliography checker can see it.
    """
    rep = bibtex.records_to_bibtex([_rec(confirmed=False)])
    assert rep["included"] == 0
    assert rep["skipped_unconfirmed"] == 1


def test_unconfirmed_can_be_opted_into_and_is_labelled():
    rep = bibtex.records_to_bibtex([_rec(confirmed=False)], include_unconfirmed=True)
    assert rep["included"] == 1
    assert "UNCONFIRMED" in rep["bibtex"]


def test_own_work_is_excluded():
    rep = bibtex.records_to_bibtex([_rec(source_kind="generated")])
    assert rep["included"] == 0 and rep["skipped_own_work"] == 1


def test_records_without_a_doi_are_excluded_and_counted():
    rep = bibtex.records_to_bibtex([_rec(doi=None)])
    assert rep["included"] == 0 and rep["skipped_no_doi"] == 1


def test_every_skip_is_counted():
    """A bibliography that is short because entries vanished looks identical to
    one that is short because the deck was small."""
    rep = bibtex.records_to_bibtex(
        [
            _rec(),
            _rec(confirmed=False),
            _rec(doi=None),
            _rec(source_kind="generated"),
        ]
    )
    assert rep["included"] == 1
    assert rep["skipped_unconfirmed"] == 1
    assert rep["skipped_no_doi"] == 1
    assert rep["skipped_own_work"] == 1


def test_one_work_used_twice_yields_one_entry():
    rep = bibtex.records_to_bibtex([_rec(), _rec(), _rec()])
    assert rep["included"] == 1, "one entry per work, however many figures use it"


def test_retraction_is_carried_into_the_entry():
    rep = bibtex.records_to_bibtex([_rec(retracted=True)])
    assert "RETRACTED" in rep["bibtex"]


# ------------------------------------------------------------- the checker


def test_missing_ghostcite_raises_rather_than_returning_clean(monkeypatch, tmp_path):
    """'No findings' and 'the checker never ran' must not look the same."""
    monkeypatch.setattr(bibtex, "ghostcite_available", lambda: None)
    bib = tmp_path / "x.bib"
    bib.write_text("@article{a, doi={10.1/2}}")
    with pytest.raises(RuntimeError, match="not on PATH"):
        bibtex.run_ghostcite(bib)


def test_unparseable_ghostcite_output_raises(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(bibtex, "ghostcite_available", lambda: "/bin/true")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 3, "not json", "boom"),
    )
    bib = tmp_path / "x.bib"
    bib.write_text("@article{a}")
    with pytest.raises(RuntimeError, match="could not parse"):
        bibtex.run_ghostcite(bib)


# ------------------------------------------------------------------- CLI


def test_cli_refuses_to_check_an_empty_bibliography(tmp_path, capsys):
    """Running a checker over an empty file returns 'clean', which is the most
    dangerous possible answer here."""
    from figcite.cli import main

    out = tmp_path / "empty.bib"
    src = tmp_path / "m.json"
    src.write_text("[]")
    rc = main(["bib", str(src), "-o", str(out), "--check"])
    assert rc == 1
    assert "empty" in capsys.readouterr().err


def test_cli_writes_a_bib_from_an_apply_manifest(tmp_path):
    import json

    from figcite.cli import main

    src = tmp_path / "m.json"
    src.write_text(
        json.dumps([{"n": 1, "slide": 1, "record": _rec().__dict__}]),
        encoding="utf-8",
    )
    out = tmp_path / "o.bib"
    assert main(["bib", str(src), "-o", str(out)]) == 0
    assert "harris2020" in out.read_text()


# ------------------------------------------------------------------ live


@pytest.mark.live
@pytest.mark.skipif(not shutil.which("ghostcite"), reason="ghostcite not installed")
def test_ghostcite_catches_a_fabricated_byline(tmp_path):
    """The real check, against the real ghostcite and live CrossRef.

    Both directions in one test: the correct entry must pass and the fabricated
    one must be caught. Asserting only the catch would pass on a checker that
    flags everything.
    """
    good = bibtex.entry(_rec(), "good")
    bad = bibtex.entry(_rec(authors=["Fabricated, Nobody Q."]), "bad")
    bib = tmp_path / "both.bib"
    bib.write_text(good + "\n\n" + bad + "\n", encoding="utf-8")

    res = bibtex.run_ghostcite(bib)
    assert res["summary"]["total"] == 2
    assert res["summary"]["findings"] == 1, (
        f"expected exactly the fabricated entry to be caught, got "
        f"{res['summary']['findings']}: {res['findings']}"
    )
    assert "Harris" in str(res["findings"][0])
