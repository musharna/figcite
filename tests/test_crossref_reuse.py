"""What you are allowed to do with a figure, tested.

`classify_reuse` turns a paper's licence URLs into the verdict that becomes
the badge on the deck screen. It is the only thing in figcite that answers
"may I put this in my talk", and before this file **no test called it at all**
-- the existing references to it in `test_webui_deck.py` are about the badge
map, not the classifier.

The mutation sweep found it the same way: the alternative-spelling branches
(`"/by-nc-nd" in joined or "/by-nd" in joined`) survived `or -> and`.

That mutation matters because the chain is an ordered `elif` and real CC URLs
are nested substrings of one another. A BY-NC-ND url contains `/by-nc` and
`/licenses/by` as well, so when the ND branch stops firing the url does not
become unclassified -- it falls through to **noncommercial-only**, quietly
dropping the no-derivatives term. The user is then told they may adapt a
figure they may not adapt. Under-restriction is the direction that costs
something, so it gets its own test rather than only a table row.
"""

import pytest

from figcite.crossref import REUSE_VERDICTS, classify_reuse

CC = "https://creativecommons.org/licenses"

CASES = [
    ([], "unknown-ask-publisher"),
    ([""], "unknown-ask-publisher"),
    (["https://creativecommons.org/publicdomain/zero/1.0/"], "public-domain"),
    (["https://spdx.org/licenses/CC0-1.0.html"], "public-domain"),
    ([f"{CC}/by-nc-nd/4.0/"], "restricted-no-derivatives"),
    ([f"{CC}/by-nd/4.0/"], "restricted-no-derivatives"),
    ([f"{CC}/by-nc/4.0/"], "noncommercial-only"),
    ([f"{CC}/by-sa/4.0/"], "reuse-ok-share-alike-attribution-required"),
    ([f"{CC}/by/4.0/"], "reuse-ok-attribution-required"),
    (
        ["http://www.elsevier.com/tdm/userlicense/1.0/"],
        "publisher-terms-check-required",
    ),
]


@pytest.mark.parametrize("urls,expected", CASES)
def test_each_licence_gets_its_verdict(urls, expected):
    lic, verdict = classify_reuse(urls)
    assert verdict == expected, f"{urls} -> {verdict}"
    assert verdict in REUSE_VERDICTS
    if urls and any(urls):
        assert lic, "a licence was classified but none was reported back"


def test_no_derivatives_is_not_downgraded_to_merely_noncommercial():
    """THE finding, stated as the harm rather than as a branch.

    `/by-nc-nd/` contains `/by-nc`. If the ND branch stops matching, the URL
    does not fall out of the chain -- it lands on the NEXT one, and the user
    is told "noncommercial only" for a licence that also forbids adaptation.
    Cropping a panel out of a figure is an adaptation, and cropping is what
    this tool is for.
    """
    _lic, verdict = classify_reuse([f"{CC}/by-nc-nd/4.0/"])

    assert verdict == "restricted-no-derivatives", verdict
    assert verdict != "noncommercial-only", (
        "a no-derivatives licence was reported as merely noncommercial"
    )
    assert verdict != "reuse-ok-attribution-required", (
        "a no-derivatives licence was reported as freely reusable"
    )


def test_noncommercial_is_not_downgraded_to_plain_attribution():
    """The same failure one rung down: `/by-nc/` also contains
    `/licenses/by`, so a broken NC branch reads as plain CC-BY."""
    _lic, verdict = classify_reuse([f"{CC}/by-nc/4.0/"])

    assert verdict == "noncommercial-only", verdict
    assert verdict != "reuse-ok-attribution-required", (
        "a noncommercial licence was reported as freely reusable"
    )


def test_share_alike_is_not_downgraded_to_plain_attribution():
    _lic, verdict = classify_reuse([f"{CC}/by-sa/4.0/"])
    assert verdict == "reuse-ok-share-alike-attribution-required", verdict


def test_the_most_restrictive_url_wins_when_a_work_lists_several():
    """Publishers list more than one licence (a TDM licence beside the CC
    one). The verdict must not depend on which happens to come first."""
    permissive_first = classify_reuse([f"{CC}/by/4.0/", f"{CC}/by-nc-nd/4.0/"])[1]
    restrictive_first = classify_reuse([f"{CC}/by-nc-nd/4.0/", f"{CC}/by/4.0/"])[1]

    assert permissive_first == restrictive_first == "restricted-no-derivatives", (
        f"order changed the verdict: {permissive_first} vs {restrictive_first}"
    )


def test_an_unrecognised_licence_asks_rather_than_assumes():
    """The default has to be the cautious one: an unknown licence is not a
    permission. This is the positive control for the whole file -- if the
    classifier returned 'publisher-terms-check-required' for everything, every
    assertion above would fail, and if it returned CC-BY for everything this
    one would."""
    _lic, verdict = classify_reuse(["https://example.test/some-bespoke-licence"])
    assert verdict == "publisher-terms-check-required", verdict


def test_case_is_not_load_bearing():
    """CrossRef returns whatever the publisher deposited; a capitalised URL
    must not read as an unknown licence."""
    _lic, verdict = classify_reuse(
        ["HTTPS://CREATIVECOMMONS.ORG/LICENSES/BY-NC-ND/4.0/"]
    )
    assert verdict == "restricted-no-derivatives", verdict
