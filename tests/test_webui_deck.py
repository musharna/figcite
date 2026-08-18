"""Task 8: the deck screen, where licence verdicts finally become visible.

Rule: a verdict with no badge renders as blank, and blank reads as "fine" --
the opposite of what an unknown/restricted licence means. The badge map must
therefore cover every verdict `crossref.classify_reuse` can emit, and the
covering test must not itself be a second list someone can forget to update:
it re-derives the verdict set from `classify_reuse`'s own source rather than
hard-coding it, so a verdict added to the classifier tomorrow fails this test
until PAGE grows a badge for it.
"""

import inspect
import re

from figcite import crossref
from figcite.provenance import Record
from figcite.webui import PAGE


def test_the_never_classified_sentinel_also_has_a_badge():
    """Not a classify_reuse output -- `Record.reuse`'s own dataclass default
    (see provenance.py), reachable whenever a Record is built without ever
    calling classify_reuse (own-work confirmations, clipboard/matplotlib
    captures: figcite/_actions.py, figcite/clipboard.py, figcite/mplhook.py
    all construct Record() directly). Verified live against
    demo/auxin_talk-COPY.pptx: every one of its rows carries this exact
    string as `reuse`. A badge map keyed only to classify_reuse's seven
    verdicts renders this row's badge as an empty box -- the same
    blank-reads-as-fine failure, reached from provenance.py's default
    instead of crossref.py's classifier. Derived from Record's own default,
    not retyped, for the same reason `_reuse_verdicts()` above is derived."""
    assert Record().reuse in PAGE


def _reuse_verdicts() -> set[str]:
    """Every string `classify_reuse` can return as its verdict.

    `classify_reuse` has no enum -- it's a chain of `if` branches, each
    ending `return <url-or-None>, "<verdict>"`. Parsing those literals out
    of the function's own source (rather than re-typing them here) is what
    keeps this test tied to the classifier: a new branch added to
    `classify_reuse` shows up here automatically, with no second list to
    remember to update.
    """
    src = inspect.getsource(crossref.classify_reuse)
    verdicts = re.findall(r'return\s+(?:None|urls\[0\])\s*,\s*"([a-z0-9-]+)"', src)
    assert verdicts, "could not parse any verdict out of classify_reuse's source"
    return set(verdicts)


def test_every_reuse_verdict_the_classifier_can_emit_has_a_badge():
    """A verdict with no badge renders as blank, which reads as 'fine'."""
    for verdict in _reuse_verdicts():
        assert verdict in PAGE, f"no badge for {verdict}"


def test_the_derived_verdict_set_still_matches_the_seven_the_brief_named():
    """Belt and suspenders, not the coverage guard above: pins that the
    *shape* of classify_reuse's verdicts hasn't silently changed (e.g. a
    typo'd rename that both classify_reuse and PAGE happen to agree on).
    If this fails, the derivation regex above needs updating, not PAGE."""
    assert _reuse_verdicts() == {
        "public-domain",
        "reuse-ok-attribution-required",
        "reuse-ok-share-alike-attribution-required",
        "noncommercial-only",
        "restricted-no-derivatives",
        "publisher-terms-check-required",
        "unknown-ask-publisher",
    }


def test_retraction_is_rendered_loudly():
    assert "RETRACTED" in PAGE


def test_the_headline_number_is_the_unsourced_count():
    assert "untagged_substantive" in PAGE
