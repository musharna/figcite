"""Zotero-first resolution.

The numbers quoted in these tests were measured against the real library
(group 6532713) on 2026-08-16 and are what justify the design; they are
recorded here so a future change that guts coverage is visible as a claim
someone has to re-measure, not just a passing test.
"""

from __future__ import annotations

import pytest

from figcite import zotero
from figcite.crossref import LookupUnavailable


# ------------------------------------------------------------------ fixtures


def _item(
    key,
    title,
    doi="",
    url="",
    itype="journalArticle",
    creators=("Doe",),
    date="2024",
    doi_source="doi-field",
):
    return {
        "key": key,
        "title": title,
        "doi": doi,
        "doi_source": doi_source if doi else "",
        "url": url,
        "date": date,
        "itemType": itype,
        "creators": list(creators),
    }


LIB = [
    _item(
        "AAA",
        "A conserved ARF-DNA interface underlies auxin response",
        doi="10.1073/pnas.2501915122",
        creators=("Rienstra", "Smith", "Jones"),
        date="2025-06-01",
    ),
    # The real library has 24 items titled "Redirecting" -- publisher landing
    # pages Zotero captured before the redirect resolved.
    _item("R1", "Redirecting", doi="10.1016/j.cub.2023.05.033", itype="webpage"),
    _item("R2", "Redirecting", doi="10.1016/j.csda.2006.11.025", itype="webpage"),
    _item("NOD", "A paper with no DOI recorded anywhere"),
    _item(
        "LONG",
        "Modelling herbivory impacts on vegetation structure and productivity",
        doi="10.5194/gmd-18-9633-2025",
        itype="webpage",
    ),
]


@pytest.fixture
def lib(monkeypatch):
    monkeypatch.setattr(zotero, "library", lambda **kw: LIB)
    return LIB


# ------------------------------------------------------------------ normalizing


def test_normalize_title_folds_typography():
    """A title copied out of a PDF carries dashes and case a title from the API
    does not; comparing raw makes an exact match look like a miss."""
    a = zotero.normalize_title("A conserved ARF–DNA interface")
    b = zotero.normalize_title("a conserved arf-dna   interface!")
    assert a == b == "a conserved arf dna interface"


def test_normalize_title_strips_accents():
    assert zotero.normalize_title("Café Genomique") == zotero.normalize_title(
        "Cafe Genomique"
    )


def test_title_from_zotero_filename():
    """Zotero names attachments '<creators> - <year> - <title>.pdf'. Searching
    the whole filename matches nothing: no title contains its own author list."""
    assert (
        zotero.title_from_pdf_name(
            "Shiragaki et al. - 2020 - Phylogenetic Analysis.pdf"
        )
        == "Phylogenetic Analysis"
    )
    assert zotero.title_from_pdf_name("Harris - 2020 - Array programming.pdf") == (
        "Array programming"
    )


def test_title_from_pdf_name_passes_through_a_plain_name():
    assert zotero.title_from_pdf_name("some-figure.pdf") == "some-figure"


# ------------------------------------------------------------------- resolving


def test_exact_unique_title_with_doi_is_grounded(lib):
    r = zotero.resolve("A conserved ARF–DNA interface underlies auxin response")
    assert r["doi"] == "10.1073/pnas.2501915122"
    assert r["grounded"] is True
    assert "AAA" in r["evidence"]


def test_ambiguous_title_is_never_grounded(lib):
    """Measured on the real library: 24 items are titled 'Redirecting'.

    This is the case where guessing is worst, so it must produce candidates and
    ground nothing. A resolver that returned the first match would look correct
    on every unique title and be silently wrong here.
    """
    r = zotero.resolve("Redirecting")
    assert r["grounded"] is False
    assert r["doi"] is None
    assert len(r["candidates"]) == 2
    assert "ambiguous" in r["evidence"]


def test_exact_match_without_a_doi_grounds_nothing(lib):
    r = zotero.resolve("A paper with no DOI recorded anywhere")
    assert r["grounded"] is False
    assert r["doi"] is None
    assert "no DOI recorded" in r["evidence"]


def test_credentials_are_readable_without_any_environment(monkeypatch, tmp_path):
    """The gap this closes, measured 2026-08-16.

    The clipboard watcher is started by a Windows launcher that runs
    `wsl.exe ... bash -lc`, and that shell inherits none of an interactive
    shell's exports -- every ZOTERO_* variable came back UNSET there. An
    env-only credential would have passed every test in this file and then
    silently never fired in the one path the feature exists for.
    """
    for var in (
        "FIGCITE_ZOTERO_API_KEY",
        "ZOTERO_API_KEY",
        "FIGCITE_ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_ID",
        "FIGCITE_ZOTERO_LIBRARY_TYPE",
        "ZOTERO_LIBRARY_TYPE",
    ):
        monkeypatch.delenv(var, raising=False)
    cfg = tmp_path / "zotero.json"
    monkeypatch.setattr(zotero, "CONFIG_FILE", cfg)
    assert zotero.configured() is False, "control: nothing configured yet"

    zotero.save_credentials("secret-key", "6532713", "group")
    assert zotero.credentials() == ("secret-key", "6532713", "group")


def test_saved_credentials_are_not_world_readable(monkeypatch, tmp_path):
    """It is a live API key. The privacy audit already found five in plaintext."""
    import stat

    cfg = tmp_path / "zotero.json"
    monkeypatch.setattr(zotero, "CONFIG_FILE", cfg)
    zotero.save_credentials("secret-key", "1", "user")
    mode = stat.S_IMODE(cfg.stat().st_mode)
    assert mode == 0o600, f"credential file is mode {mode:o}"


def test_a_loosened_credential_file_is_refused(monkeypatch, tmp_path):
    """Failing loud beats reading a key that anyone on the box can also read."""
    cfg = tmp_path / "zotero.json"
    monkeypatch.setattr(zotero, "CONFIG_FILE", cfg)
    zotero.save_credentials("secret-key", "1", "user")
    cfg.chmod(0o644)
    for var in ("FIGCITE_ZOTERO_API_KEY", "ZOTERO_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(zotero.NotConfigured, match="chmod 600"):
        zotero.credentials()


def test_environment_overrides_the_file(monkeypatch, tmp_path):
    cfg = tmp_path / "zotero.json"
    monkeypatch.setattr(zotero, "CONFIG_FILE", cfg)
    zotero.save_credentials("file-key", "111", "user")
    monkeypatch.setenv("FIGCITE_ZOTERO_API_KEY", "env-key")
    monkeypatch.setenv("FIGCITE_ZOTERO_LIBRARY_ID", "222")
    assert zotero.credentials()[:2] == ("env-key", "222")


def test_save_credentials_rejects_a_bad_library_type(monkeypatch, tmp_path):
    monkeypatch.setattr(zotero, "CONFIG_FILE", tmp_path / "z.json")
    with pytest.raises(ValueError):
        zotero.save_credentials("k", "1", "team")


def test_partial_match_is_a_candidate_not_an_answer(lib):
    r = zotero.resolve("Modelling herbivory impacts on vegetation structure")
    assert r["grounded"] is False
    assert r["doi"] is None
    assert r["candidates"] and r["candidates"][0]["doi"] == "10.5194/gmd-18-9633-2025"


def test_a_miss_is_reported_as_a_miss(lib):
    r = zotero.resolve("Quantum badger husbandry in zero gravity")
    assert r["grounded"] is False and r["doi"] is None
    assert r["available"] is True, (
        "a reachable library that has no such paper IS available"
    )


def test_short_query_is_refused_rather_than_matched(lib):
    r = zotero.resolve("fig")
    assert r["doi"] is None
    assert "too short" in r["evidence"]


# --------------------------------------------- unavailable is not the same as absent


def test_missing_credentials_is_not_a_miss(monkeypatch):
    """'You never set an API key' must never read as 'that paper isn't yours'."""
    for var in (
        "FIGCITE_ZOTERO_API_KEY",
        "ZOTERO_API_KEY",
        "FIGCITE_ZOTERO_LIBRARY_ID",
        "ZOTERO_LIBRARY_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    assert zotero.configured() is False
    r = zotero.resolve("anything at all here")
    assert r["available"] is False
    assert "not configured" in r["evidence"].lower()


def test_unreachable_library_is_not_a_miss(monkeypatch):
    def boom(*_a, **_kw):
        raise LookupUnavailable("network is down")

    monkeypatch.setattr(zotero, "library", boom)
    monkeypatch.setenv("FIGCITE_ZOTERO_API_KEY", "k")
    monkeypatch.setenv("FIGCITE_ZOTERO_LIBRARY_ID", "1")
    r = zotero.resolve("a perfectly reasonable paper title")
    assert r["available"] is False
    assert "NOT a miss" in r["evidence"]


def test_credentials_reject_a_bad_library_type(monkeypatch):
    monkeypatch.setenv("FIGCITE_ZOTERO_API_KEY", "k")
    monkeypatch.setenv("FIGCITE_ZOTERO_LIBRARY_ID", "1")
    monkeypatch.setenv("FIGCITE_ZOTERO_LIBRARY_TYPE", "team")
    with pytest.raises(zotero.NotConfigured):
        zotero.credentials()


# ----------------------------------------------------------- DOI harvesting


def test_doi_is_harvested_from_a_doi_org_url():
    """The measurement this encodes: on the real library the DOI *field* is
    populated for 147 items, but 540 carry a doi.org URL, because 4,677 of
    4,824 items are `webpage` -- an item type that has no DOI field at all.
    Reading only the DOI field discards most of the coverage that exists.
    """
    doi, how = zotero._doi_of(
        {"DOI": "", "url": "https://doi.org/10.1038/s41586-020-2649-2"}
    )
    assert doi == "10.1038/s41586-020-2649-2"
    assert how == "url"


def test_doi_field_wins_over_the_url():
    doi, how = zotero._doi_of(
        {"DOI": "10.1111/aaa", "url": "https://doi.org/10.2222/bbb"}
    )
    assert doi == "10.1111/aaa" and how == "doi-field"


def test_non_doi_url_yields_nothing():
    doi, how = zotero._doi_of({"DOI": "", "url": "https://example.com/figure.png"})
    assert doi == "" and how == ""


def test_dx_doi_org_is_also_harvested():
    doi, _ = zotero._doi_of(
        {"DOI": "", "url": "http://dx.doi.org/10.5194/gmd-18-9633-2025"}
    )
    assert doi == "10.5194/gmd-18-9633-2025"


# ------------------------------------------------- ordering inside the chain


def test_zotero_is_consulted_before_crossref(monkeypatch, lib):
    """The whole point of the feature: the curated library wins over a search of
    ~150M works. If CrossRef is queried when Zotero already grounded the answer,
    the ordering has silently inverted and the tool is slower AND less accurate.
    """
    from figcite import clipboard

    called = []
    monkeypatch.setattr(
        clipboard,
        "search_bibliographic",
        lambda *a, **k: called.append(a) or [],
    )
    monkeypatch.setattr(
        clipboard,
        "browser_resolve",
        lambda cap: {"doi": None, "url": None, "grounded": False, "evidence": ""},
    )
    out = clipboard.infer_source(
        {
            "process": "firefox",
            "title": "A conserved ARF–DNA interface underlies auxin response - Mozilla Firefox",
        }
    )
    assert out["doi"] == "10.1073/pnas.2501915122"
    assert out["grounded"] is True
    assert not called, "CrossRef was queried even though Zotero had already grounded it"


def test_crossref_still_runs_when_zotero_has_nothing(monkeypatch, lib):
    """The positive control for the test above: if Zotero misses, the CrossRef
    fallback must still fire. Otherwise 'Zotero first' would quietly mean
    'Zotero only', and that test would pass on a broken chain."""
    from figcite import clipboard

    called = []
    monkeypatch.setattr(
        clipboard,
        "search_bibliographic",
        lambda *a, **k: (called.append(a), [])[1],
    )
    monkeypatch.setattr(
        clipboard,
        "browser_resolve",
        lambda cap: {"doi": None, "url": None, "grounded": False, "evidence": ""},
    )
    out = clipboard.infer_source(
        {"process": "firefox", "title": "Quantum badger husbandry in zero gravity"}
    )
    assert called, "CrossRef fallback never ran on a Zotero miss"
    assert out["doi"] is None


def test_pdf_not_on_disk_falls_through_to_the_library(monkeypatch, lib):
    """This path used to dead-end: a window title naming a PDF we could not
    locate produced no DOI and no candidates at all."""
    from figcite import clipboard

    monkeypatch.setattr(clipboard, "find_pdf_on_disk", lambda name: None)
    out = clipboard.infer_source(
        {
            "process": "acrord32",
            "title": "Rienstra et al. - 2025 - A conserved ARF-DNA interface "
            "underlies auxin response.pdf",
        }
    )
    assert out["doi"] == "10.1073/pnas.2501915122"
    assert out["grounded"] is True
    assert "no such file was found on disk" in out["doi_evidence"]


def test_process_name_matches_with_or_without_exe(monkeypatch, lib):
    """PowerShell's ProcessName has no '.exe'. If a capture ever arrives with
    one, every app-specific branch stops matching and the only symptom is worse
    inference -- a failure with no observable."""
    from figcite import clipboard

    monkeypatch.setattr(
        clipboard,
        "browser_resolve",
        lambda cap: {"doi": None, "url": None, "grounded": False, "evidence": ""},
    )
    title = "A conserved ARF-DNA interface underlies auxin response"
    bare = clipboard.infer_source({"process": "firefox", "title": title})
    dotted = clipboard.infer_source({"process": "firefox.exe", "title": title})
    assert bare["kind"] == dotted["kind"] == "clipboard-from-web"
    assert bare["doi"] == dotted["doi"] == "10.1073/pnas.2501915122"
