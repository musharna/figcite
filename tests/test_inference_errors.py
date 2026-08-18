
from figcite import clipboard


def test_a_failed_browser_lookup_sets_error(monkeypatch):
    """'Could not look' must be readable as such, not as prose in doi_evidence."""

    def boom(capture):
        raise RuntimeError("Network is unreachable")

    monkeypatch.setattr(clipboard, "browser_resolve", boom)

    out = clipboard.infer_source({"process": "firefox", "title": "Some paper"})
    assert out["error"] is not None
    assert "Network is unreachable" in out["error"]


def test_a_successful_inference_leaves_error_none(monkeypatch):
    """Positive control. Without this, a function that set error unconditionally
    -- or one that crashed on every path -- would pass the test above."""
    monkeypatch.setattr(
        clipboard,
        "browser_resolve",
        lambda capture: {
            "doi": "10.1/real",
            "url": "https://example.org/10.1/real",
            "grounded": True,
            "evidence": "DOI in URL",
        },
    )
    out = clipboard.infer_source({"process": "firefox", "title": "Some paper"})
    assert out["doi"] == "10.1/real"
    assert out["error"] is None


def test_error_and_doi_evidence_are_independent_signals(monkeypatch):
    """The regression this task prevents: collapsing the two back into one."""

    def boom(capture):
        raise RuntimeError("Network is unreachable")

    monkeypatch.setattr(clipboard, "browser_resolve", boom)

    out = clipboard.infer_source({"process": "firefox", "title": "Some paper"})
    assert out["error"], "the failure must be its own field"
    assert out["doi_evidence"] is not None, "existing evidence text must survive"
