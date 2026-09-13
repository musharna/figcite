"""Task 8: the deck screen, where licence verdicts finally become visible.

Round-2 review (task-8-report.md's "Concerns" section, addressed here):
C1/C2 found the round-1 guards inert by mutation. C2's fix is structural
(`badgeFor()` in webui.py always falls back to `[r.reuse, "warn"]`, so a
blank badge is impossible by construction, not merely covered by a list);
these tests check the STRUCTURE that guarantees that, plus the coverage
niceties layered on top of it. I3/I4/I5/I6 are the same review round's
"important" findings, each with a test that distinguishes the pre-fix code
from the post-fix code.
"""

import re

from figcite import crossref
from figcite.provenance import Record
from figcite.webui import PAGE


def _reuse_badge_map_keys(page: str) -> set[str]:
    """The literal keys of the JS `REUSE` badge map, parsed out of PAGE's
    own script text.

    C1's defect was a substring check (`Record().reuse in PAGE`): the
    string "unknown" is a substring of the already-mapped
    "unknown-ask-publisher", so deleting the real "unknown" entry left the
    test green. "Is X covered" has to mean "is X an actual map key", which
    means parsing the map's keys, not searching the page's raw text.
    """
    start = page.index("const REUSE = {")
    end = page.index("\n};", start)
    block = page[start:end]
    pairs = re.findall(r'(?:"([a-z0-9-]+)"|^\s*([a-zA-Z_]\w*))\s*:', block, re.MULTILINE)
    return {a or b for a, b in pairs}


def test_every_classifier_verdict_is_an_explicit_badge_map_key():
    """C2 fix: reads `crossref.REUSE_VERDICTS` -- a real shared constant,
    not a regex parse of `classify_reuse`'s source. The round-1 version of
    this test parsed `return <None|urls[0]>, "<verdict>"` out of the
    function's source text, which is really an allowlist of two return
    SHAPES: a verdict returned via a variable (`return chosen,
    "copyleft-software-license"`) or a module-level constant (`return None,
    EMBARGO`) both parsed as zero verdicts, and `assert verdicts` only
    checks non-emptiness, so a partial parse was invisible. `REUSE_VERDICTS`
    can't be fooled that way because nothing here parses source code -- and
    `classify_reuse` itself asserts every verdict it returns is a member of
    it (crossref.py), so the two can't drift apart un-noisily either.
    """
    keys = _reuse_badge_map_keys(PAGE)
    for verdict in crossref.REUSE_VERDICTS:
        assert verdict in keys, f"no badge map entry for {verdict}"


def test_reuse_verdicts_constant_is_the_seven_named_in_the_brief():
    """Sanity pin on content, not the coverage guard above: confirms
    `REUSE_VERDICTS` is still exactly the seven verdicts this task's brief
    was written against."""
    assert crossref.REUSE_VERDICTS == {
        "public-domain",
        "reuse-ok-attribution-required",
        "reuse-ok-share-alike-attribution-required",
        "noncommercial-only",
        "restricted-no-derivatives",
        "publisher-terms-check-required",
        "unknown-ask-publisher",
    }


def test_the_never_classified_sentinel_is_an_explicit_badge_map_key():
    """C1, fixed: `Record.reuse`'s own dataclass default (see
    provenance.py) is reachable whenever a Record is built without ever
    calling classify_reuse (own-work confirmations, clipboard/matplotlib
    captures -- figcite/_actions.py, figcite/clipboard.py, figcite/mplhook.py
    all construct Record() directly). Checks the PAIRING (an actual map
    key), not `in PAGE` -- the substring check that made C1 pass with the
    badge deleted. This is now a niceties-level test: even with this map
    entry gone, `badgeFor()`'s fallback (tested separately below) still
    renders the raw "unknown" string rather than a blank badge -- deleting
    this entry degrades the label, it no longer produces a safety failure.
    """
    assert Record().reuse in _reuse_badge_map_keys(PAGE)


def test_an_unmapped_verdict_still_renders_visibly_not_blank():
    """C2's actual structural fix, and what dissolves C1's severity:
    `badgeFor()` must fall back to `[r.reuse, "warn"]` on a map miss, not
    `["", ""]`. An invented verdict like "totally-made-up-verdict" -- the
    same shape a genuinely new classify_reuse verdict takes before anyone
    adds it a friendly label -- must still render AS ITSELF, in warn
    styling, rather than as an empty badge that reads as fine. There is no
    JS runtime in this test suite (matches this file's own convention, see
    tests/test_webui_page_round2.py's docstring) so this is a structural
    check on the fallback expression itself, not an executed one.
    """
    assert re.search(r'REUSE\[r\.reuse\]\s*\|\|\s*\[r\.reuse,\s*"warn"\]', PAGE), (
        "badgeFor()'s REUSE lookup has no non-blank fallback -- an unmapped "
        "verdict would render as an empty badge, indistinguishable from "
        "'fine'"
    )


def test_retraction_is_rendered_loudly():
    assert "RETRACTED" in PAGE


def test_the_headline_number_is_the_unsourced_count():
    assert "untagged_substantive" in PAGE


# ---------------------------------------------------------------------------
# I3b -- an image whose ref is non-empty but unresolvable (not empty, but
# no library file backs it -- reproduced live with an EXIF-credit JPEG)
# still 404s at request time. The empty-ref guard in deckRow() is necessary
# but not sufficient; this is the "sufficient" half.
# ---------------------------------------------------------------------------


def test_thumbnail_load_errors_render_a_placeholder_not_a_broken_image_icon():
    assert re.search(r'addEventListener\(\s*"error"', PAGE), (
        "no error listener on #deckrows -- an unresolvable ref 404s silently"
    )
    assert "image unavailable" in PAGE
    # Bounded to "from the error listener up to the next top-level async
    # function" -- not up to the first "):" after `start`, which lands
    # inside the callback body itself (e.g. `createElement("span");`
    # contains a `);` long before the real close of addEventListener(...)).
    start = PAGE.index('addEventListener(\n  "error"')
    end = PAGE.index("async function auditDeck", start)
    block = PAGE[start:end]
    # <img> "error" doesn't bubble -- capture phase (a trailing `true`
    # argument to addEventListener) is the only way a listener above the
    # <img> itself can ever see it.
    assert re.search(r"\btrue\s*,?\s*\)\s*;", block), (
        "the error listener isn't registered for the capture phase, so it "
        "can never see a non-bubbling <img> error event"
    )


# ---------------------------------------------------------------------------
# I4 -- decorative rows are excluded from untagged_substantive (deck.py)
# but rendered identically to substantive no-source rows, so the headline
# and the table disagreed.
# ---------------------------------------------------------------------------


def test_decorative_rows_are_marked_so_the_table_agrees_with_the_headline():
    start = PAGE.index("function deckRow(r)")
    end = PAGE.index("function ", start + 1)
    body = PAGE[start:end]
    assert "r.decorative" in body, "deckRow never reads r.decorative at all"
    assert "decorative" in body.split("r.decorative", 1)[1][:80], (
        "r.decorative is read but never turned into a visible marker"
    )


# ---------------------------------------------------------------------------
# I5 -- own work (source_kind == "generated") has no third-party licence to
# ask about; badging it "warn" the same as a genuinely unknown licence
# trains a user to ignore red (measured: 15/15 rows on the project's own
# demo deck are own work).
# ---------------------------------------------------------------------------


def test_own_work_gets_a_neutral_badge_not_a_warn_colored_one():
    start = PAGE.index("function badgeFor(r)")
    end = PAGE.index("function ", start + 1)
    body = PAGE[start:end]
    assert 'source_kind === "generated"' in body, (
        "badgeFor never branches on source_kind -- own-work figures fall "
        "through to the same warn-colored badge as a genuinely unclassified "
        "third-party figure"
    )
    # The own-work branch must come BEFORE the REUSE fallback, and must not
    # itself use the "warn" class.
    own_work_pos = body.index('source_kind === "generated"')
    fallback_pos = body.index("REUSE[r.reuse]")
    assert own_work_pos < fallback_pos
    own_work_line = body[own_work_pos : body.index("\n", own_work_pos)]
    assert '"warn"' not in own_work_line


def test_service_audit_carries_source_kind_so_the_ui_can_tell_own_work_apart():
    """Python-level companion to the JS test above: the front end can only
    branch on source_kind if service.audit() actually puts it on the row."""
    from figcite import service

    rec = Record(
        sha256="c" * 64,
        citation="This work",
        confirmed=True,
        source_kind="generated",
    )
    import figcite.service as _svc

    orig = _svc.deck.audit
    try:
        _svc.deck.audit = lambda p, min_inches=1.0: {
            "pptx": "/x/deck.pptx",
            "pictures": 1,
            "tagged": 1,
            "unconfirmed": 0,
            "untagged_substantive": 0,
            "rows": [
                {
                    "slide": 1,
                    "shape": "Picture 1",
                    "decorative": False,
                    "matched_by": "manifest-sha256",
                    "record": rec,
                }
            ],
        }
        row = service.audit("/x/deck.pptx")["rows"][0]
    finally:
        _svc.deck.audit = orig
    assert row["source_kind"] == "generated"


def test_service_audit_falls_back_to_the_full_citation_field():
    """I3a: figcite/provenance.py's JPEG-EXIF recovery path
    (`read_embedded`) sets ONLY `Record.citation` -- no `short_cite`, no
    `doi`. `service.audit()` used to compute
    `rec.short_cite or rec.doi or ""`, which never falls back to
    `rec.citation` at all, so a figure carrying a genuine embedded credit
    rendered an empty citation cell. Fix B's own defect class, one column
    over."""
    from figcite import service

    rec = Record(
        sha256="d" * 64,
        citation="Recovered Credit, EXIF (2020).",
        confirmed=False,
        note="recovered from JPEG EXIF; full record not embeddable in JPEG",
    )
    import figcite.service as _svc

    orig = _svc.deck.audit
    try:
        _svc.deck.audit = lambda p, min_inches=1.0: {
            "pptx": "/x/deck.pptx",
            "pictures": 1,
            "tagged": 0,
            "unconfirmed": 1,
            "untagged_substantive": 0,
            "rows": [
                {
                    "slide": 1,
                    "shape": "Picture 1",
                    "decorative": False,
                    "matched_by": "manifest-sha256",
                    "record": rec,
                }
            ],
        }
        row = service.audit("/x/deck.pptx")["rows"][0]
    finally:
        _svc.deck.audit = orig
    assert row["citation"] == "Recovered Credit, EXIF (2020)."


# ---------------------------------------------------------------------------
# I6 -- Apply was a dead end after one use: no `out` field, no force
# control, even though APPLY_FIELDS already allows both.
# ---------------------------------------------------------------------------


def test_apply_screen_offers_an_out_path_and_a_force_control():
    assert 'id="deckout"' in PAGE
    assert 'id="deckforce"' in PAGE


def test_apply_deck_sends_out_and_force_in_its_payload():
    start = PAGE.index("async function applyDeck")
    end = (
        PAGE.index("async function", start + 1)
        if "async function" in PAGE[start + 1 :]
        else len(PAGE)
    )
    body = PAGE[start:end]
    assert '"/api/apply"' in body
    assert "out" in body and "force" in body
    assert "deckout" in body and "deckforce" in body


# ---------------------------------------------------------------------------
# Fold-ins from the review: applyDeck's missing try (auditDeck already had
# one), and the two nav onclick="show(...)" literals.
# ---------------------------------------------------------------------------


def test_apply_deck_has_a_catch_like_audit_deck_does():
    start = PAGE.index("async function applyDeck")
    end = PAGE.index("\n}\n", start)
    body = PAGE[start:end]
    assert "catch" in body, (
        "applyDeck has no catch -- a rejected post() surfaces as an "
        "unhandled promise rejection in the delegated click listener"
    )


def test_nav_tabs_no_longer_use_inline_onclick():
    assert 'onclick="show(' not in PAGE, (
        "nav tab buttons still carry a static onclick literal -- the "
        "report claims every interactive element goes through delegation"
    )
    assert 'data-tab="pending"' in PAGE
    assert 'data-tab="deck"' in PAGE
