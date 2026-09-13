"""The card must show HOW a candidate was found, not merely that it was.

An open-tab lead ("you had this paper open") sitting beside a pixel match
("this crop came out of Figure 3") invites being read as equal evidence, and
the only thing separating them on screen is the `.src` badge. It already
renders; this task is the guard that keeps it rendering, because losing it is
silent -- the card still looks fine, it just quietly promotes a guess to the
same standing as a measurement.

Pinned at both ends: the producers must emit distinguishable labels, and the
template must still show them.
"""

import re

from figcite import service, session_tabs
from figcite.webui import PAGE

CANDIDATE_BLOCK = re.compile(r"item\.candidates\.map\((.*?)\.join\(", re.S)


def test_the_source_badge_renders_inside_the_candidate_list():
    """Scoped to the candidate block, not the whole page.

    `"c.source" in PAGE` would keep passing if the badge were moved somewhere
    the reader never looks, so the assertion has to be about where it renders.
    """
    m = CANDIDATE_BLOCK.search(PAGE)
    assert m, "the candidate list is no longer rendered by mapping over candidates"
    block = m.group(1)
    assert "c.source" in block, (
        "the candidate rows stopped rendering their source badge; an open-tab "
        "lead and a pixel match would then look identical"
    )
    assert 'class="src"' in block, "the badge lost the class that styles it apart"


def test_a_could_not_decide_reason_has_somewhere_to_land():
    assert "doi_evidence" in PAGE, (
        "the card has no slot for WHY nothing was inferred, so 'could not "
        "look' and 'looked and found nothing' would read the same"
    )


def test_the_two_kinds_of_candidate_carry_different_labels(monkeypatch, tmp_path):
    """The badge is only worth rendering if the values it shows differ.

    Positive control on the whole guard: if every candidate reported the same
    source, the template test above would still pass while the distinction it
    exists to preserve had already been lost upstream.
    """
    monkeypatch.setattr(
        session_tabs,
        "tab_candidates",
        lambda: [
            {
                "source": "open-tab",
                "doi": "10.1/tab",
                "title": "t",
                "container": "",
                "year": "",
                "score": "",
            }
        ],
    )
    labels = {c["source"] for c in _candidates_with_a_pixel_match(monkeypatch, tmp_path)}
    assert "open-tab" in labels
    assert labels - {"open-tab"}, "a pixel match reported the same source as a tab"


def _candidates_with_a_pixel_match(monkeypatch, tmp_path):
    from figcite import corpus, match

    monkeypatch.setattr(corpus, "connect", lambda: None)
    monkeypatch.setattr(corpus, "all_rows", lambda conn: [_Row()])
    monkeypatch.setattr(
        match,
        "by_dhash",
        lambda b, rows: match.Match("10.1/pixel", "PMC1", "Figure 1", "dhash", 1.0, 9.0),
    )
    # `tempfile.gettempdir()/figcite_badge_probe.png` -- a FIXED name in the
    # shared temp dir -- is the same escape that made test_corpus_duplicates
    # tear under concurrent runs. Identical content makes a torn read less
    # likely here, not impossible, and "less likely" is the property that
    # turns a defect into an intermittent one. pytest's tmp_path cannot be
    # claimed by another process.
    p = tmp_path / "figcite_badge_probe.png"
    p.write_bytes(b"not really a png")
    return service.whereis(str(p))["matches"]


class _Row:
    pmcid, label, doi, caption = "PMC1", "Figure 1", "10.1/pixel", "A caption"
    dhash, image_path = "0" * 16, "PMC1/f1.png"
