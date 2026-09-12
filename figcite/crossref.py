"""CrossRef lookups.

Design rule enforced here: a DOI *given* by a human, or read out of the PDF the
figure was cropped from, is grounded. A DOI *guessed* from a window title or a
figure caption is not, and is returned unconfirmed. This is not paranoia --
querying CrossRef for the exact title "Array programming with NumPy" returns a
*review of* that paper as the top hit, not the paper.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests

from .provenance import Record, now_stamps

CACHE = Path(
    os.environ.get("FIGCITE_CACHE", Path.home() / ".cache" / "figcite" / "crossref")
)
MAILTO = os.environ.get("FIGCITE_MAILTO", "advertisingemailhaha@gmail.com")
UA = f"figcite/0.1 (https://github.com/; mailto:{MAILTO})"
TIMEOUT = 25


class LookupUnavailable(RuntimeError):
    """The lookup could not be performed. NOT the same as 'no such record'.

    Collapsing these two into None is how a rate-limited request silently
    becomes an image with no recorded source.
    """


# CrossRef advertises x-rate-limit-limit=1 per 1s. A tight loop over a reading
# list blows straight through it and every 429 would read as "no DOI".
_MIN_INTERVAL = float(os.environ.get("FIGCITE_CROSSREF_MIN_INTERVAL", "1.05"))
_last_call = 0.0


def throttled_get(url: str, **kw):
    global _last_call
    timeout = kw.pop("timeout", TIMEOUT)  # a hung CrossRef socket must not hang the watcher
    wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    r = requests.get(url, timeout=timeout, **kw)
    _last_call = time.monotonic()
    if r.status_code == 429:
        delay = 2.0
        try:
            delay = float(r.headers.get("retry-after", 2))
        except ValueError:
            pass
        time.sleep(min(max(delay, 1.0), 15.0))
        r = requests.get(url, timeout=timeout, **kw)
        _last_call = time.monotonic()
    return r


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9<>\[\]]+", re.I)


def normalize_doi(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"^\s*(doi:\s*|https?://(dx\.)?doi\.org/)", "", s, flags=re.I)
    return s.rstrip(" .,;)]}>").strip()


def find_dois(text: str) -> list[str]:
    """All DOIs in a blob of text, de-duped, order preserved."""
    out, seen = [], set()
    for m in DOI_RE.finditer(text or ""):
        d = normalize_doi(m.group(0))
        # A trailing ')' is far more often prose punctuation than part of the DOI.
        while d and d[-1] in ".,;:":
            d = d[:-1]
        if d.lower() not in seen:
            seen.add(d.lower())
            out.append(d)
    return out


def _cache_file(doi: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doi.lower())[:180]
    return CACHE / f"{safe}.json"


def fetch_work(doi: str, *, use_cache: bool = True) -> Optional[dict[str, Any]]:
    doi = normalize_doi(doi)
    if not doi:
        return None
    cf = _cache_file(doi)
    if use_cache and cf.exists():
        try:
            return json.loads(cf.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        r = throttled_get(
            f"https://api.crossref.org/works/{doi}",
            headers={"User-Agent": UA},
            timeout=TIMEOUT,
        )
    except Exception as e:
        raise RuntimeError(f"CrossRef request failed for {doi}: {e}") from e
    if r.status_code == 404:
        return None
    r.raise_for_status()
    msg = r.json()["message"]
    CACHE.mkdir(parents=True, exist_ok=True)
    cf.write_text(json.dumps(msg, ensure_ascii=False), encoding="utf-8")
    return msg


def search_bibliographic(query: str, rows: int = 5) -> list[dict[str, Any]]:
    """Title/citation search. Results are CANDIDATES, never answers."""
    try:
        r = throttled_get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": query, "rows": rows},
            headers={"User-Agent": UA},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"CrossRef search failed: {e}") from e
    out = []
    for it in r.json()["message"]["items"]:
        out.append(
            {
                "doi": it.get("DOI", ""),
                "score": round(float(it.get("score", 0)), 1),
                "title": (it.get("title") or [""])[0],
                "year": _year(it),
                "container": (it.get("container-title") or [""])[0],
                "type": it.get("type", ""),
                "source": "crossref",
            }
        )
    return out


def _year(msg: dict) -> Optional[int]:
    for k in ("issued", "published-print", "published-online", "created"):
        try:
            return int(msg[k]["date-parts"][0][0])
        except Exception:
            continue
    return None


def _authors(msg: dict) -> list[str]:
    out = []
    for a in msg.get("author", []) or []:
        fam, giv = a.get("family"), a.get("given")
        if fam and giv:
            out.append(f"{fam}, {giv}")
        elif fam:
            out.append(fam)
        elif a.get("name"):
            out.append(a["name"])
    return out


# Named so `classify_reuse` cannot drift from `REUSE_VERDICTS` by typo --
# every branch below returns one of these names, never a bare string
# literal, and the assertion at the bottom of `classify_reuse` makes that a
# runtime-enforced contract rather than a convention someone can forget.
# Task-8 review (C2): the web UI's badge map (figcite/webui.py's `REUSE`)
# and this test suite's coverage check both read `REUSE_VERDICTS` directly
# now -- one source of truth, no regex parsing of this function's source
# (which a review found could be fooled by a verdict returned via a
# variable or a differently-named constant instead of a bare string).
_PUBLIC_DOMAIN = "public-domain"
_ATTRIBUTION_REQUIRED = "reuse-ok-attribution-required"
_SHARE_ALIKE_ATTRIBUTION_REQUIRED = "reuse-ok-share-alike-attribution-required"
_NONCOMMERCIAL_ONLY = "noncommercial-only"
_RESTRICTED_NO_DERIVATIVES = "restricted-no-derivatives"
_PUBLISHER_TERMS_CHECK_REQUIRED = "publisher-terms-check-required"
_UNKNOWN_ASK_PUBLISHER = "unknown-ask-publisher"

REUSE_VERDICTS: frozenset[str] = frozenset(
    {
        _PUBLIC_DOMAIN,
        _ATTRIBUTION_REQUIRED,
        _SHARE_ALIKE_ATTRIBUTION_REQUIRED,
        _NONCOMMERCIAL_ONLY,
        _RESTRICTED_NO_DERIVATIVES,
        _PUBLISHER_TERMS_CHECK_REQUIRED,
        _UNKNOWN_ASK_PUBLISHER,
    }
)


def classify_reuse(license_urls: list[str]) -> tuple[Optional[str], str]:
    """(chosen license url, reuse verdict). Deliberately conservative.

    The verdict half of the return is always a `REUSE_VERDICTS` member --
    the assertion below enforces that at the one place a new verdict could
    ever be introduced, so a future branch added with a raw string that was
    never added to `REUSE_VERDICTS` (and therefore never given a badge in
    `webui.py`) fails loudly here instead of silently reaching the UI as an
    unmapped verdict.
    """
    urls = [u for u in license_urls if u]
    joined = " ".join(urls).lower()
    if not urls:
        lic, verdict = None, _UNKNOWN_ASK_PUBLISHER
    elif "creativecommons.org/publicdomain" in joined or "/cc0" in joined:
        lic, verdict = urls[0], _PUBLIC_DOMAIN
    elif "/by-nc-nd" in joined or "/by-nd" in joined:
        lic, verdict = urls[0], _RESTRICTED_NO_DERIVATIVES
    elif "/by-nc" in joined:
        lic, verdict = urls[0], _NONCOMMERCIAL_ONLY
    elif "/by-sa" in joined:
        lic, verdict = urls[0], _SHARE_ALIKE_ATTRIBUTION_REQUIRED
    elif "/licenses/by" in joined:
        lic, verdict = urls[0], _ATTRIBUTION_REQUIRED
    else:
        lic, verdict = urls[0], _PUBLISHER_TERMS_CHECK_REQUIRED
    assert verdict in REUSE_VERDICTS, f"{verdict!r} is not a REUSE_VERDICTS member"
    return lic, verdict


def format_citation(msg: dict) -> tuple[str, str]:
    """(full citation, short cite)."""
    auth = _authors(msg)
    yr = _year(msg)
    if not auth:
        who = msg.get("publisher") or "Anon."
        short_who = who
    elif len(auth) == 1:
        who = auth[0]
        short_who = auth[0].split(",")[0]
    elif len(auth) == 2:
        who = f"{auth[0]} & {auth[1]}"
        short_who = f"{auth[0].split(',')[0]} & {auth[1].split(',')[0]}"
    else:
        who = f"{auth[0]} et al."
        short_who = f"{auth[0].split(',')[0]} et al."
    title = (msg.get("title") or [""])[0]
    cont = (msg.get("container-title") or [""])[0]
    vol = msg.get("volume")
    pages = msg.get("page")
    bits = [f"{who} ({yr})." if yr else f"{who}."]
    if title:
        bits.append(f"{title}.")
    tail = cont or ""
    if vol:
        tail += f" {vol}"
    if pages:
        tail += f": {pages}"
    if tail.strip():
        bits.append(tail.strip() + ".")
    full = " ".join(bits)
    short = f"{short_who} {yr}" if yr else short_who
    return full, short


def is_retracted(msg: dict) -> bool:
    for u in msg.get("update-to", []) or []:
        if "retract" in str(u.get("type", "")).lower():
            return True
    return str(msg.get("type", "")).lower() == "retraction"


def record_from_doi(
    doi: str,
    *,
    confirmed: bool,
    source_kind: str = "manual",
    source_detail: Optional[dict] = None,
) -> Record:
    """Build a Record from a DOI, with the citation coming from CrossRef itself.

    Because the byline is read from CrossRef rather than recalled, the
    wrong-author-for-right-DOI failure cannot occur here. The residual risk is
    the wrong DOI for the figure -- which is what `confirmed` tracks.
    """
    doi = normalize_doi(doi)
    msg = fetch_work(doi)
    if msg is None:
        raise LookupError(f"DOI not found in CrossRef: {doi}")
    full, short = format_citation(msg)
    lic_urls = [entry.get("URL", "") for entry in msg.get("license", []) or []]
    lic, reuse = classify_reuse(lic_urls)
    u, loc = now_stamps()
    return Record(
        doi=doi,
        url=f"https://doi.org/{doi}",
        citation=full,
        short_cite=short,
        authors=_authors(msg),
        year=_year(msg),
        title=(msg.get("title") or [""])[0],
        container=(msg.get("container-title") or [""])[0],
        license_url=lic,
        reuse=reuse,
        retracted=is_retracted(msg),
        source_kind=source_kind,
        source_detail=source_detail or {},
        captured_utc=u,
        captured_local=loc,
        confirmed=confirmed,
    )
