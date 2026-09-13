import pytest

from figcite import service


def test_audit_normalizes_the_pptx_and_pdf_report_shapes(monkeypatch):
    monkeypatch.setattr(
        service.deck,
        "audit",
        lambda p, min_inches=1.0: {
            "pptx": "/x/deck.pptx",
            "pictures": 1,
            "tagged": 0,
            "unconfirmed": 0,
            "untagged_substantive": 1,
            "rows": [
                {
                    "slide": 3,
                    "shape": "Picture 4",
                    "size_in": [2.0, 2.0],
                    "decorative": False,
                    "matched_by": "none",
                    "record": None,
                    "alt_text": "",
                }
            ],
        },
    )
    rep = service.audit("/x/deck.pptx")
    assert rep["path"] == "/x/deck.pptx"
    assert rep["kind"] == "pptx"
    assert rep["rows"][0]["location"] == "slide 3"
    assert rep["rows"][0]["status"] == "no-source"


def test_audit_reports_the_licensing_verdict_for_a_matched_row(monkeypatch):
    from figcite.provenance import Record

    rec = Record(
        sha256="b" * 64,
        doi="10.1/x",
        short_cite="Band et al. 2014",
        confirmed=True,
        license_url="https://creativecommons.org/licenses/by/4.0/",
        reuse="reuse-ok-attribution-required",
        retracted=False,
    )
    monkeypatch.setattr(
        service.deck,
        "audit",
        lambda p, min_inches=1.0: {
            "pptx": "/x/deck.pptx",
            "pictures": 1,
            "tagged": 1,
            "unconfirmed": 0,
            "untagged_substantive": 0,
            "rows": [
                {
                    "slide": 7,
                    "shape": "Picture 1",
                    "size_in": [3.0, 3.0],
                    "decorative": False,
                    "matched_by": "manifest-sha256",
                    "record": rec,
                    "alt_text": "",
                }
            ],
        },
    )
    row = service.audit("/x/deck.pptx")["rows"][0]
    assert row["status"] == "ok"
    assert row["reuse"] == "reuse-ok-attribution-required"
    assert row["retracted"] is False
    assert row["ref"] == f"sha:{'b' * 64}"


def test_apply_refuses_to_overwrite_its_input(tmp_path, monkeypatch):
    """apply() must compare RESOLVED paths and raise rather than clobber.

    A caller passing an `out` that resolves to the same file as `path` --
    even spelled differently, via a "sub/.." detour -- must never reach
    deck.apply/pdfdeck.apply. A "." component doesn't discriminate here:
    pathlib already normalizes "." away when the Path is *constructed*, so a
    raw string compare would catch that case too and the test would pass
    even with the resolve() guard downgraded to strings. A ".." component is
    NOT normalized at construction (pathlib can't collapse it without
    touching the filesystem, in case an intermediate is a symlink) -- so
    str(path) != str(out) here even though they name the same file, and only
    .resolve() sees they're identical. That's what makes this test able to
    fail: with the guard downgraded to `str(out) == str(path)`, this passes
    straight through to the deck.apply monkeypatch below and raises nothing.
    """
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"not a real pptx")

    called = []
    monkeypatch.setattr(service.deck, "apply", lambda *a, **kw: called.append((a, kw)) or {})

    # Same file, spelled differently: a "sub/.." detour a raw string
    # compare cannot see through, but Path.resolve() collapses.
    same_path_spelled_differently = tmp_path / "sub" / ".." / "deck.pptx"
    assert str(same_path_spelled_differently) != str(pptx)

    with pytest.raises(ValueError):
        service.apply(str(pptx), out=str(same_path_spelled_differently))

    assert called == [], "deck.apply must never be reached when out == path"


def test_apply_never_honors_a_caller_supplied_allow_unconfirmed(tmp_path, monkeypatch):
    """No path from the web UI (or any caller) to allow_unconfirmed=True.

    Even if a caller passes allow_unconfirmed=True in **opts, apply() must
    pop it and always call deck.apply with allow_unconfirmed=False.
    """
    pptx = tmp_path / "deck.pptx"
    pptx.write_bytes(b"not a real pptx")
    out = tmp_path / "deck.cited.pptx"

    captured = {}

    def _fake_apply(path, out_path, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(service.deck, "apply", _fake_apply)

    service.apply(str(pptx), out=str(out), allow_unconfirmed=True)

    assert captured.get("allow_unconfirmed") is False
