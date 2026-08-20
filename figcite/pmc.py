"""Fetching from Europe PMC and PMC. No index knowledge, no matching.

NCBI asks for no more than 3 requests/second without an API key, so every
request in this module goes through one throttle. It is deliberately separate
from `crossref.throttled_get`: that one is tuned to CrossRef's 1/s, and sharing
it would make NCBI politeness a side effect of an unrelated rate limit.
"""

from __future__ import annotations

import json
import re
import socket
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
OA_SERVICE = "https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
XLINK = "{http://www.w3.org/1999/xlink}href"

# NCBI publishes the open-access subset through the AWS Open Data programme,
# one prefix per article VERSION, holding the figure images beside the XML:
#   PMC5383700.1/fpls-08-00491-g0001.jpg
#
# The article page was tried first and is a dead end: PMC answers a non-browser
# client with a "Checking your browser - reCAPTCHA" interstitial that no header
# set clears. Scraping the human-facing site was the wrong idea anyway; this is
# the channel NCBI publishes FOR programmatic use.
S3_BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff")
USER_AGENT = "figcite/0.1 (https://github.com/musharna/figcite)"
MIN_INTERVAL = 1.0 / 3.0
_last_call = 0.0


class DnsUnreachable(RuntimeError):
    """A hostname did not resolve.

    Named, rather than left as a bare ConnectionError, because it has a real
    precedent: `cdn.ncbi.nlm.nih.gov` failed to resolve on the author's machine
    while every other NCBI host worked, and the resulting failure read as "this
    article has no figures". A DNS fault and an absence of figures must not
    look the same to the caller.
    """


@dataclass
class PmcRecord:
    doi: str
    pmcid: str
    title: str
    year: str
    is_open_access: bool


def _get(url: str, **kw) -> bytes:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        r = requests.get(url, timeout=kw.pop("timeout", 45), **kw)
    except requests.exceptions.ConnectionError as e:
        if isinstance(e.__cause__, socket.gaierror) or "NameResolution" in str(e):
            host = urllib.parse.urlsplit(url).hostname or url
            raise DnsUnreachable(
                f"{host} did not resolve; the corpus will be incomplete. "
                "This is a DNS failure, not an absence of figures."
            ) from e
        raise
    finally:
        _last_call = time.monotonic()
    r.raise_for_status()
    return r.content


MAX_ATTEMPTS = 3
_RETRY_WAIT = 2.0  # seconds, doubled per attempt
RETRY_CODES = {429, 500, 502, 503, 504}


def _get_with_retry(url: str, **kw) -> bytes:
    """`_get` with bounded retries on the codes that mean "ask again".

    A 504 is by definition transient -- it is the gateway saying the backend
    was slow, not that the answer is no. Failing on the first one throws away
    an answer the server was willing to give; one turned up within 25 requests
    of a real library sweep.

    Only transient codes. A 404 is a definite answer and retrying it just
    spends the rate limit to arrive at the same place. DnsUnreachable is not
    retried either: nothing about a name that does not resolve improves by
    asking three times.
    """
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return _get(url, **kw)
        except requests.exceptions.HTTPError as e:
            code = getattr(e.response, "status_code", None)
            if code not in RETRY_CODES or attempt == MAX_ATTEMPTS:
                raise
            time.sleep(_RETRY_WAIT * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")  # pragma: no cover


@dataclass
class Lookup:
    """What a sweep found, AND what it could not look at.

    Two channels, deliberately. Folding an unreachable DOI into "no record"
    would make an outage indistinguishable from a paper Europe PMC has never
    heard of, and only the second of those says anything about the DOI.

    A dataclass rather than a NamedTuple on purpose: a tuple is iterable and
    sized, so a caller still written against the old `list[PmcRecord]` would
    quietly iterate two fields or measure a length of 2 instead of failing.
    This way a stale caller raises immediately.
    """

    records: list[PmcRecord]
    unreachable: dict[str, str]  # doi -> why the lookup did not happen


def lookup_dois(dois: list[str], batch: int = 8) -> Lookup:
    """Resolve DOIs to PMC records, several per request.

    A library has hundreds of DOIs and Europe PMC accepts an OR query, so
    one-at-a-time would be hundreds of round trips at 3/s for no reason.
    """
    out: list[PmcRecord] = []
    unreachable: dict[str, str] = {}
    for i in range(0, len(dois), batch):
        chunk = dois[i : i + batch]
        query = " OR ".join(f'DOI:"{d}"' for d in chunk)
        url = (
            EUROPE_PMC
            + "?"
            + urllib.parse.urlencode(
                {
                    "query": query,
                    "format": "json",
                    "pageSize": "25",
                    "resultType": "core",
                }
            )
        )
        # The boundary belongs HERE, around one request, because one request
        # is the unit of independent failure. Wrapping the whole loop (which
        # is what `build` used to do) means a single blip on batch 3 of 67
        # discards the 66 batches that already succeeded. DnsUnreachable is
        # allowed through: it is total, and retrying every remaining batch
        # against a name that does not resolve only wastes minutes.
        try:
            payload = json.loads(_get_with_retry(url))
        except DnsUnreachable:
            raise
        except Exception as e:
            for d in chunk:
                unreachable[d.lower()] = f"lookup did not complete: {e}"
            continue
        for it in payload.get("resultList", {}).get("result", []) or []:
            # Records WITHOUT a pmcid are kept, with pmcid="". Dropping them
            # made "Europe PMC knows this paper but there is no PMC copy"
            # indistinguishable from "Europe PMC has never heard of this DOI",
            # and only the second of those suggests the DOI might be wrong.
            out.append(
                PmcRecord(
                    doi=(it.get("doi") or "").lower(),
                    pmcid=it.get("pmcid") or "",
                    title=it.get("title", ""),
                    year=str(it.get("pubYear", "")),
                    is_open_access=it.get("isOpenAccess") == "Y",
                )
            )
    return Lookup(records=out, unreachable=unreachable)


@dataclass
class FigureRef:
    label: str
    filename: str
    caption: str


def figures_of(pmcid: str) -> list[FigureRef]:
    """Every figure of an article, with its label and caption."""
    root = ET.fromstring(_get(FULLTEXT.format(pmcid=pmcid)))
    out: list[FigureRef] = []
    for fig in root.iter("fig"):
        label = (fig.findtext("label") or "").strip()
        caption = " ".join(t.strip() for t in fig.itertext() if t.strip())
        for g in fig.iter("graphic"):
            # A <graphic content-type="thumb"> is a preview of the SAME figure.
            # Counting it would double every row and index a downsampled copy.
            if g.get("content-type") == "thumb":
                continue
            href = g.get(XLINK)
            if href:
                out.append(FigureRef(label=label, filename=href, caption=caption))
                break
    return out


def s3_prefix(pmcid: str) -> str | None:
    """The newest versioned key prefix for an article, e.g. "PMC5383700.1/".

    Articles get revised. Pinning .1 when .2 exists would silently index the
    figures of a superseded version, which is exactly the sort of quiet
    wrongness this project exists to avoid.
    """
    url = f"{S3_BUCKET}/?list-type=2&delimiter=/&prefix={urllib.parse.quote(pmcid)}."
    xml = _get(url).decode("utf8", "replace")
    versioned = []
    for pfx in re.findall(r"<Prefix>([^<]+)</Prefix>", xml):
        tail = pfx.rstrip("/").rsplit(".", 1)[-1]
        if tail.isdigit():
            versioned.append((int(tail), pfx))
    if not versioned:
        return None
    return max(versioned)[1]


def image_urls(pmcid: str) -> dict[str, str]:
    """filename -> servable URL for every figure image of an article.

    Keyed by bare filename because that is exactly what fullTextXML's
    `xlink:href` gives, so the two line up without any mapping.
    """
    prefix = s3_prefix(pmcid)
    if prefix is None:
        return {}
    url = f"{S3_BUCKET}/?list-type=2&prefix={urllib.parse.quote(prefix)}"
    xml = _get(url).decode("utf8", "replace")
    out: dict[str, str] = {}
    for key in re.findall(r"<Key>([^<]+)</Key>", xml):
        name = key.rsplit("/", 1)[-1]
        if name.lower().endswith(IMAGE_SUFFIXES):
            out[name] = f"{S3_BUCKET}/{urllib.parse.quote(key)}"
    return out


def licence_of(pmcid: str) -> str:
    """The reuse licence PMC records for an article, or "" if unavailable.

    Worth storing with each figure: a reverse-sourced match then arrives with
    its reuse terms already attached, and feeds the badges figcite renders.
    """
    try:
        xml = _get(OA_SERVICE.format(pmcid=pmcid)).decode("utf8", "replace")
    except Exception:
        return ""
    m = re.search(r'license="([^"]+)"', xml)
    return m.group(1) if m else ""
