"""Fetching from Europe PMC and PMC. No index knowledge, no matching.

NCBI asks for no more than 3 requests/second without an API key, so every
request in this module goes through one throttle. It is deliberately separate
from `crossref.throttled_get`: that one is tuned to CrossRef's 1/s, and sharing
it would make NCBI politeness a side effect of an unrelated rate limit.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.parse
from dataclasses import dataclass

import requests

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
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


def lookup_dois(dois: list[str], batch: int = 8) -> list[PmcRecord]:
    """Resolve DOIs to PMC records, several per request.

    A library has hundreds of DOIs and Europe PMC accepts an OR query, so
    one-at-a-time would be hundreds of round trips at 3/s for no reason.
    """
    out: list[PmcRecord] = []
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
        payload = json.loads(_get(url))
        for it in payload.get("resultList", {}).get("result", []) or []:
            if not it.get("pmcid"):
                continue
            out.append(
                PmcRecord(
                    doi=(it.get("doi") or "").lower(),
                    pmcid=it["pmcid"],
                    title=it.get("title", ""),
                    year=str(it.get("pubYear", "")),
                    is_open_access=it.get("isOpenAccess") == "Y",
                )
            )
    return out
