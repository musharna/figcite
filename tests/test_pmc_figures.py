"""Enumerating figures, and finding the URL that actually serves them.

The article page was the original plan and it is a dead end: PMC answers a
non-browser HTTP client with a 21 KB "Checking your browser - reCAPTCHA"
interstitial, and no header set clears it. Scraping the human-facing site was
the wrong idea anyway.

NCBI publishes the open-access subset through the AWS Open Data programme, one
prefix per article version, holding the figure images beside the XML:

    PMC5383700.1/fpls-08-00491-g0001.jpg

Those filenames are exactly the `xlink:href` values fullTextXML already gives,
the URLs are constructible, and a plain `requests` GET returns the same bytes
(verified: identical length and identical dhash to the CDN copy).
"""

from figcite import pmc

XML = b"""<article>
  <fig id="F1"><label>Figure 1</label>
    <caption><p>Evolution of the thing.</p></caption>
    <graphic xmlns:xlink="http://www.w3.org/1999/xlink" content-type="image"
             xlink:href="fpls-08-00491-g0001.jpg"/>
    <graphic xmlns:xlink="http://www.w3.org/1999/xlink" content-type="thumb"
             xlink:href="fpls-08-00491-g0001.gif"/>
  </fig>
  <fig id="F2"><label>Figure 2</label>
    <caption><p>Another thing.</p></caption>
    <graphic xmlns:xlink="http://www.w3.org/1999/xlink" content-type="image"
             xlink:href="fpls-08-00491-g0002.jpg"/>
  </fig>
</article>"""

# Captured shapes of the two S3 list responses.
PREFIX_LIST = b"""<ListBucketResult>
  <CommonPrefixes><Prefix>PMC5383700.</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>PMC5383700.1/</Prefix></CommonPrefixes>
</ListBucketResult>"""

PREFIX_LIST_V2 = b"""<ListBucketResult>
  <CommonPrefixes><Prefix>PMC5383700.1/</Prefix></CommonPrefixes>
  <CommonPrefixes><Prefix>PMC5383700.2/</Prefix></CommonPrefixes>
</ListBucketResult>"""

KEY_LIST = b"""<ListBucketResult>
  <Contents><Key>PMC5383700.1/DataSheet1.XLS</Key></Contents>
  <Contents><Key>PMC5383700.1/PMC5383700.1.pdf</Key></Contents>
  <Contents><Key>PMC5383700.1/PMC5383700.1.xml</Key></Contents>
  <Contents><Key>PMC5383700.1/fpls-08-00491-g0001.jpg</Key></Contents>
  <Contents><Key>PMC5383700.1/fpls-08-00491-g0002.jpg</Key></Contents>
</ListBucketResult>"""


def _router(**by_substring):
    def fake_get(url, **kw):
        for needle, payload in by_substring.items():
            if needle in url:
                return payload
        raise AssertionError(f"unexpected url: {url}")

    return fake_get


def test_figures_are_enumerated_with_labels_and_captions(monkeypatch):
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: XML)
    figs = pmc.figures_of("PMC5383700")
    assert [f.label for f in figs] == ["Figure 1", "Figure 2"]
    assert figs[0].filename == "fpls-08-00491-g0001.jpg"
    assert "Evolution" in figs[0].caption


def test_thumbnails_are_not_treated_as_figures(monkeypatch):
    """content-type="thumb" is a preview of the same figure, not another one."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: XML)
    names = [f.filename for f in pmc.figures_of("PMC5383700")]
    assert not any(n.endswith(".gif") for n in names), names


def test_the_newest_article_version_is_used(monkeypatch):
    """Articles are revised. Indexing .1 when .2 exists silently uses stale figures."""
    monkeypatch.setattr(pmc, "_get", _router(delimiter=PREFIX_LIST_V2))
    assert pmc.s3_prefix("PMC5383700") == "PMC5383700.2/"


def test_image_urls_are_constructed_and_keyed_by_filename(monkeypatch):
    calls = {"n": 0}

    def fake_get(url, **kw):
        calls["n"] += 1
        return PREFIX_LIST if "delimiter" in url else KEY_LIST

    monkeypatch.setattr(pmc, "_get", fake_get)
    urls = pmc.image_urls("PMC5383700")
    assert set(urls) == {
        "fpls-08-00491-g0001.jpg",
        "fpls-08-00491-g0002.jpg",
    }, "non-image objects (xml, pdf, spreadsheets) must not be collected"
    assert urls["fpls-08-00491-g0001.jpg"].startswith("https://")
    assert urls["fpls-08-00491-g0001.jpg"].endswith("PMC5383700.1/fpls-08-00491-g0001.jpg")


def test_an_article_not_in_the_open_access_bucket_returns_empty(monkeypatch):
    """Positive control: absence is data. A closed article must not raise."""
    monkeypatch.setattr(pmc, "_get", lambda url, **kw: b"<ListBucketResult></ListBucketResult>")
    assert pmc.s3_prefix("PMC0") is None
    assert pmc.image_urls("PMC0") == {}
