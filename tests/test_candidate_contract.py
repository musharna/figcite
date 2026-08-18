"""Producer/consumer contract for candidate dicts.

The pending-screen template (`figcite/webui.py`) reads a fixed set of keys
off every candidate dict: `doi`, `score`, `title`, `container`, `year`,
`type`, `source`. Nothing enforced that either producer -- `zotero.py`'s
`_candidate()` or `crossref.py`'s `search_bibliographic()` -- actually emits
all of them, and an absent key and an empty-string key render identically at
the template (`c.source || ""`), so a producer silently regressing (or, as
happened here, being written to a spec the producer never matched) never
surfaced as a failure anywhere. This test pins the agreement directly, so
the next mismatch goes red at the source instead of rendering blank.

Deliberately a superset check against a NAMED set, not a length/count check
-- a producer that starts returning extra keys is fine; one that drops a key
the template reads is not, and a later producer being added must not make a
count-based assertion silently pass.
"""

from figcite.zotero import _candidate
from figcite.crossref import search_bibliographic

# The exact keys figcite/webui.py's candidate-row template reads off `c`.
TEMPLATE_KEYS = {"doi", "score", "title", "container", "year", "type", "source"}


def test_zotero_candidate_carries_every_key_the_template_reads():
    item = {
        "doi": "10.1000/xyz123",
        "title": "A Paper About Something",
        "date": "2021-03-01",
        "itemType": "journalArticle",
        "key": "ABCD1234",
        "creators": ["Someone"],
    }
    cand = _candidate(item, "exact-title")
    missing = TEMPLATE_KEYS - set(cand)
    assert not missing, f"zotero candidate is missing keys the page reads: {missing}"


def test_crossref_candidate_carries_every_key_the_template_reads(monkeypatch):
    """Stubs the HTTP call -- the conftest socket block stays intact and
    would otherwise refuse this test outright, which is the point."""
    import figcite.crossref as C

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "message": {
                    "items": [
                        {
                            "DOI": "10.1000/abc999",
                            "score": 87.5,
                            "title": ["Some Title"],
                            "container-title": ["Some Journal"],
                            "type": "journal-article",
                            "issued": {"date-parts": [[2020]]},
                        }
                    ]
                }
            }

    monkeypatch.setattr(C, "throttled_get", lambda *a, **kw: _FakeResponse())

    hits = search_bibliographic("irrelevant query", rows=1)
    assert hits, "stubbed search returned nothing"
    missing = TEMPLATE_KEYS - set(hits[0])
    assert not missing, f"crossref candidate is missing keys the page reads: {missing}"
