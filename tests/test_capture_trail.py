"""Every capture leaves a record, even when no citation can be established.

"At least some track" -- a screenshot with no resolvable DOI still knows which
app was in front, what the window said, and when.
"""
import json

from PIL import Image

from figcite import clipboard as C
from figcite import store


def _staged(tmp_path, capture):
    png = tmp_path / "clip-test.png"
    Image.new("RGB", (200, 140), (30, 90, 140)).save(png)
    (tmp_path / "clip-test.capture.json").write_text(json.dumps(capture))
    return png


def test_ungrounded_capture_is_filed_with_its_context(tmp_path):
    capture = {"title": "Some Random Page - Mozilla Firefox", "process": "firefox",
               "captured_local": "2026-08-13T23:59:00-04:00", "width": 200, "height": 140}
    png = _staged(tmp_path, capture)
    pending = {"png": str(png), "capture": capture,
               "inference": {"kind": "clipboard-from-web", "doi": None, "grounded": False,
                             "url": "https://example.org/some-page",
                             "doi_evidence": "no history entry matched"}}

    dest = C.auto_finalize(png, pending)
    assert dest is not None and dest.exists(), "an ungrounded capture was not filed at all"

    rec = store.get(__import__("figcite").provenance.sha256_file(dest))
    assert rec is not None, "nothing reached the manifest"
    assert rec.confirmed is False, "an unresolved capture must not claim confirmation"
    assert not rec.doi and not rec.citation, "no citation should be invented"

    ctx = rec.context_line()
    assert "firefox" in ctx
    assert "Some Random Page" in ctx
    assert "example.org" in ctx
    assert "2026-08-13 23:59" in ctx
    assert "no history entry matched" in rec.note, "the reason for the gap was dropped"


def test_grounded_capture_is_still_confirmed(monkeypatch, tmp_path):
    """Positive control: filing everything must not stop grounded ones confirming."""
    from figcite.provenance import Record

    capture = {"title": "A Paper - Mozilla Firefox", "process": "firefox",
               "captured_local": "2026-08-13T23:59:00-04:00"}
    png = _staged(tmp_path, capture)
    pending = {"png": str(png), "capture": capture,
               "inference": {"kind": "clipboard-from-web", "doi": "10.1111/nph.71477",
                             "grounded": True, "url": "https://doi.org/10.1111/nph.71477",
                             "doi_evidence": "exact-title in Firefox history"}}

    import figcite.crossref as X

    def fake_record_from_doi(doi, *, confirmed=True, source_kind="manual",
                             source_detail=None):
        return Record(doi=doi, citation="Someone et al. (2026).",
                      short_cite="Someone et al. 2026", confirmed=confirmed,
                      source_kind=source_kind, source_detail=source_detail or {})

    monkeypatch.setattr(X, "record_from_doi", fake_record_from_doi)

    dest = C.auto_finalize(png, pending)
    rec = store.get(__import__("figcite").provenance.sha256_file(dest))
    assert rec.confirmed is True
    assert rec.doi == "10.1111/nph.71477"
