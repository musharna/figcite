"""The real Europe PMC and AWS Open Data boundary. No fixtures.

Marked `live` so it stays out of the push gate. Every other pmc test feeds
canned XML to the parsers, which proves the parsers are self-consistent and
nothing else. This is the only test that would notice PMC republishing an
article under a new version prefix, renaming a figure file, or changing the
listing format -- the joints SP2 actually rests on.

The load-bearing assertion is the OVERLAP one. `corpus.build` joins figures to
images with a bare `urls.get(fig.filename)`, so if fullTextXML's `xlink:href`
ever stops matching the S3 key basename, every build silently indexes zero
figures while reporting success. Nothing offline can see that.
"""

import pytest

from figcite import pmc

DOI = "10.3389/fpls.2017.00491"
PMCID = "PMC5383700"  # Frontiers in Plant Science, CC BY, 6 figures

pytestmark = pytest.mark.live


def test_a_real_doi_resolves_to_a_pmcid():
    recs = pmc.lookup_dois([DOI]).records
    assert recs, "Europe PMC returned nothing for a known indexed DOI"
    assert recs[0].pmcid == PMCID
    assert recs[0].is_open_access is True


def test_a_nonexistent_doi_comes_back_absent_not_invented():
    """Negative control: lookup must be able to report nothing.

    Without this, a lookup that returned a hit for literally any input would
    pass the test above and quietly attach the wrong paper to every figure.
    """
    recs = pmc.lookup_dois(["10.9999/this-doi-does-not-exist-figcite"]).records
    invented = [r.pmcid for r in recs if r.pmcid]
    assert invented == [], f"a made-up DOI resolved to {invented}"


def test_figure_names_still_line_up_with_the_published_image_keys():
    figs = pmc.figures_of(PMCID)
    assert len(figs) >= 6, f"expected >=6 figures, got {[f.label for f in figs]}"
    assert all(f.filename for f in figs), "a figure came back with no href"

    prefix = pmc.s3_prefix(PMCID)
    assert prefix and prefix.startswith(PMCID + "."), (
        f"no versioned key prefix for {PMCID}; the bucket layout may have changed"
    )

    urls = pmc.image_urls(PMCID)
    assert urls, "the article's key listing yielded no image files"

    missing = [f.filename for f in figs if f.filename not in urls]
    assert not missing, (
        f"{len(missing)} figure href(s) have no matching S3 key: {missing[:3]} -- "
        f"corpus.build joins on this exact name, so builds would index nothing. "
        f"Published keys look like: {sorted(urls)[:3]}"
    )


def test_a_figure_image_actually_downloads():
    urls = pmc.image_urls(PMCID)
    name = sorted(urls)[0]
    blob = pmc._get(urls[name])
    assert len(blob) > 5000, f"suspiciously small image for {name}: {len(blob)} bytes"
    assert blob[:3] == b"\xff\xd8\xff" or blob[:8] == b"\x89PNG\r\n\x1a\n", (
        f"{name} did not arrive as a JPEG or PNG: {blob[:16]!r}"
    )


def test_an_unknown_article_yields_no_prefix_rather_than_a_wrong_one():
    """Positive control on discrimination: the listing can come back empty.

    An `s3_prefix` that matched too loosely would return SOME prefix for this
    nonsense id, and then every article would index another article's figures.
    """
    bogus = pmc.s3_prefix("PMC000000000")
    assert bogus is None, f"a nonsense id matched the prefix {bogus!r}"


def test_the_licence_comes_from_pmc_not_from_a_guess():
    lic = pmc.licence_of(PMCID)
    assert lic.upper().startswith("CC"), (
        f"expected a Creative Commons licence for {PMCID}, got {lic!r} -- an "
        f"empty string means the OA service did not answer, which is not the "
        f"same as the article being unlicensed"
    )
