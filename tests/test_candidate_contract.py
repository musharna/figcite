r"""Producer/candidate-template contract, pinned at the seam.

Round-1 version of this test hard-imported the two producers
(`zotero._candidate`, `crossref.search_bibliographic`) and wrote one test
per name -- which is this repo's own recurring "a list of names cannot
guard an open set": a third producer added later would be invisible to it.
It also hand-maintained `TEMPLATE_KEYS` as a comment-level claim about what
`figcite/webui.py`'s candidate-row template reads, and that claim was
already wrong (`doi` and `type` are not read by `PAGE`; the candidate
template only ever touches `container`, `score`, `source`, `title`,
`year`) -- an empty field and an absent field render identically at the
template, so a hand-written key list can silently drift in either
direction without a test noticing.

This version fixes both: `TEMPLATE_KEYS` is derived directly from the live
`PAGE` string via `re.findall(r"c\.(\w+)", PAGE)`, so it maintains itself
-- if the template starts (or stops) reading a key, this file changes with
it, with no hand-edit required. And the assertion runs against
`service.pending_items()`, the single choke point every candidate --
Zotero's, CrossRef's, or a future third source's -- passes through on its
way to the API and the page, rather than against each producer function
individually.
"""

import json
import re

from figcite import service
from figcite.crossref import search_bibliographic
from figcite.webui import PAGE
from figcite.zotero import _candidate

TEMPLATE_KEYS = set(re.findall(r"c\.(\w+)", PAGE))


def test_template_keys_were_actually_derived_from_the_live_page():
    """Sanity control on the derivation itself, not a hand duplicate of it.

    If this fails, the regex above stopped matching PAGE's actual candidate
    template (e.g. the loop variable was renamed from `c`) -- fix the
    regex, don't hand-edit the set back to what you expect it to be.
    """
    assert TEMPLATE_KEYS == {"container", "score", "source", "title", "year"}


def test_every_candidate_pending_items_surfaces_carries_every_template_key(tmp_path, monkeypatch):
    """Stage one candidate from each real producer, run them both through
    `service.pending_items()` -- the seam -- and check every key the page
    template reads survives the trip. A third producer added later needs no
    new test here: anything `pending_items()` returns gets checked the same
    way.
    """
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
                            "title": ["A CrossRef Title"],
                            "container-title": ["Some Journal"],
                            "type": "journal-article",
                            "issued": {"date-parts": [[2020]]},
                        }
                    ]
                }
            }

    # No network: the conftest socket block stays intact and would refuse
    # an unstubbed HTTP attempt outright -- that's the point of stubbing
    # throttled_get rather than weakening the guard.
    monkeypatch.setattr(C, "throttled_get", lambda *a, **kw: _FakeResponse())
    crossref_candidates = search_bibliographic("irrelevant query", rows=1)

    zotero_item = {
        "doi": "10.1000/xyz123",
        "title": "A Zotero-held Paper",
        "date": "2021-03-01",
        "itemType": "journalArticle",
        "key": "ABCD1234",
        "creators": ["Someone"],
    }
    zotero_candidates = [_candidate(zotero_item, "exact-title")]

    staging = tmp_path / "staging"
    staging.mkdir()
    png = staging / "clip-1.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    (staging / "clip-1.pending.json").write_text(
        json.dumps(
            {
                "png": str(png),
                "capture": {"process": "firefox", "title": "A paper"},
                "inference": {
                    "kind": "browser",
                    "candidates": crossref_candidates + zotero_candidates,
                    "error": None,
                },
            }
        )
    )
    monkeypatch.setattr(service.clipboard, "staging_dirs", lambda: (None, staging))

    it = [i for i in service.pending_items() if i.kind == "staged"][0]
    assert len(it.candidates) == 2, "both producers' candidates must survive the seam"
    for cand in it.candidates:
        missing = TEMPLATE_KEYS - set(cand)
        assert not missing, f"candidate is missing template keys {missing}: {cand}"
