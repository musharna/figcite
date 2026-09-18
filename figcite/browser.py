"""Ground a clipboard snip taken from a browser in the page it came from.

The chain is: foreground window title (already recorded at snip time)
  -> exact row in the browser's history  -> URL  -> DOI  -> CrossRef.

Why a URL may be auto-confirmed when a title guess may not: the URL is the
address of the document that was on screen, and the DOI is then either written
in that address or declared by the page itself via <meta name="citation_doi">.
A CrossRef title search, by contrast, is an inference about which paper someone
meant -- and it demonstrably returns reviews of a paper above the paper.

The nearest-visit-in-time fallback IS a guess about which tab, so anything
resolved that way stays unconfirmed.
"""

from __future__ import annotations

import os
import re
from urllib.parse import unquote
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

from .crossref import LookupUnavailable, fetch_work, normalize_doi, throttled_get

FIREFOX_SUFFIX = re.compile(r"\s+[-—–]\s+Mozilla Firefox(\s+Private Browsing)?\s*$")
BROWSER_SUFFIX = re.compile(
    r"\s+[-—–]\s+(Google Chrome|Microsoft\s*Edge|Brave|Opera|Vivaldi|Chromium)\s*$"
)
# Which browsers' history this module can actually open. The suffix regex above
# normalises six browsers' window titles, and clipboard.BROWSERS lists six, but
# only one of them has a reader here -- so a capture from any of the other five
# had its title looked up in FIREFOX's database, and the miss was reported as
# "no history entry titled X (private-browsing windows leave no history)",
# naming a cause that was not the real one.
#
# These two sets are reconciled against clipboard.BROWSERS by a test, so adding
# a browser there without deciding what its history costs fails loudly instead
# of silently searching the wrong database.
HISTORY_BACKENDS = {"firefox"}
BROWSERS_WITHOUT_HISTORY_BACKEND = {"msedge", "chrome", "brave", "opera", "vivaldi"}

DOI_IN_URL = re.compile(r"10\.\d{4,9}/[^\s?&#]+")
CITATION_DOI_META = re.compile(
    r"""<meta[^>]+(?:name|property)\s*=\s*["'](?:citation_doi|DC\.Identifier|dc\.identifier)["'][^>]*>""",
    re.I,
)
CONTENT_ATTR = re.compile(r"""content\s*=\s*["']([^"']+)["']""", re.I)

# Suffixes publishers bolt onto a DOI inside a URL path.
URL_DOI_TAIL = re.compile(
    r"(?:/(?:full|abstract|pdf|epdf|meta|full-text|article-info|supplementary)|"
    r"\.(?:pdf|full|long|abstract)|v\d+)+$",
    re.I,
)

FETCH_TIMEOUT = 20
UA = "figcite/0.1 (+local provenance tool)"


# --------------------------------------------------------------- history


def _win_userprofile() -> Optional[str]:
    from .clipboard import _win_userprofile as w

    return w()


def firefox_profiles() -> list[Path]:
    """Firefox profile dirs holding a places.sqlite, newest history first."""
    up = _win_userprofile()
    if not up:
        return []
    from .clipboard import win_to_wsl

    root = Path(win_to_wsl(up)) / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles"
    if not root.exists():
        return []
    profs = [p for p in root.iterdir() if (p / "places.sqlite").exists()]
    return sorted(profs, key=lambda p: (p / "places.sqlite").stat().st_mtime, reverse=True)


def snapshot_history(profile: Path) -> Optional[Path]:
    """Copy places.sqlite *and its WAL* so very recent visits are visible.

    Firefox holds the DB open; without the -wal the newest visits -- exactly the
    ones a just-taken snip needs -- are missing.
    """
    src = profile / "places.sqlite"
    if not src.exists():
        return None
    tmp = Path(tempfile.mkdtemp(prefix="figcite-hist-"))
    dst = tmp / "places.sqlite"
    try:
        shutil.copy2(src, dst)
        for ext in ("-wal", "-shm"):
            s = profile / f"places.sqlite{ext}"
            if s.exists():
                shutil.copy2(s, tmp / f"places.sqlite{ext}")
    except Exception:
        return None
    return dst


def strip_browser_suffix(title: str) -> str:
    t = FIREFOX_SUFFIX.sub("", title or "")
    return BROWSER_SUFFIX.sub("", t).strip()


def _visit_dt(us: int) -> str:
    try:
        return datetime.fromtimestamp(us / 1e6).isoformat(timespec="seconds")
    except Exception:
        return ""


def lookup_by_title(db: Path, page_title: str) -> Optional[dict[str, Any]]:
    """Exact title match -> most recently visited URL with that title."""
    if not page_title:
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "select url, title, last_visit_date from moz_places "
            "where title = ? and url like 'http%' "
            "order by last_visit_date desc limit 5",
            (page_title,),
        ).fetchall()
        con.close()
    except Exception:
        return None
    if not rows:
        return None
    url, title, ts = rows[0]
    return {
        "url": url,
        "title": title,
        "visited": _visit_dt(ts or 0),
        "match": "exact-title",
        "ambiguous": len(rows) > 1,
    }


def lookup_by_time(db: Path, when_epoch: float, window_s: int = 180) -> Optional[dict[str, Any]]:
    """Nearest visit to the capture time. A GUESS about which tab was showing."""
    lo = int((when_epoch - window_s) * 1e6)
    hi = int((when_epoch + window_s) * 1e6)
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = con.execute(
            "select p.url, p.title, v.visit_date from moz_historyvisits v "
            "join moz_places p on p.id = v.place_id "
            "where v.visit_date between ? and ? and p.url like 'http%' "
            "order by abs(v.visit_date - ?) limit 1",
            (lo, hi, int(when_epoch * 1e6)),
        ).fetchall()
        con.close()
    except Exception:
        return None
    if not rows:
        return None
    url, title, ts = rows[0]
    return {
        "url": url,
        "title": title,
        "visited": _visit_dt(ts or 0),
        "match": "nearest-visit",
        "ambiguous": True,
    }


# --------------------------------------------------------------- url -> doi


def _clean_doi_from_url(raw: str) -> str:
    d = unquote(raw)
    d = URL_DOI_TAIL.sub("", d)
    return normalize_doi(d.rstrip("/.,;"))


def doi_from_url_text(url: str) -> Optional[str]:
    m = DOI_IN_URL.search(url or "")
    return _clean_doi_from_url(m.group(0)) if m else None


PUBLISHER_PATTERNS: list[tuple[re.Pattern, str]] = [
    # nature.com/articles/s41598-019-42976-3 -> 10.1038/s41598-019-42976-3
    (
        re.compile(r"nature\.com/articles/([a-z0-9][\w.\-]+?)(?:\.pdf)?/?$", re.I),
        "10.1038/{0}",
    ),
]


def doi_from_publisher_pattern(url: str) -> Optional[str]:
    for pat, tmpl in PUBLISHER_PATTERNS:
        m = pat.search(url or "")
        if m:
            return normalize_doi(tmpl.format(*m.groups()))
    return None


PII_IN_URL = re.compile(r"/(?:pii|fulltext)/(S[0-9A-Z()\-]{10,30})", re.I)
# Elsevier's own assets -- the high-resolution figure behind "Download
# high-res image", the PDF -- are named after the PII: 1-s2.0-<PII>-gr1_lrg.jpg.
# An unpunctuated PII is exactly 17 characters, S plus digits with an optional
# X check character; fixing the length keeps "-gr1" out of the identifier.
PII_IN_ASSET = re.compile(r"/1-s2\.0-(S[0-9X]{16})(?![0-9A-Z])", re.I)
NCBI_PMID = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{4,10})")
NCBI_PMC = re.compile(r"(?:pmc\.ncbi\.nlm\.nih\.gov/articles|/pmc/articles)/(PMC\d+)", re.I)


def _normalize_pii(raw: str) -> str:
    """Cell Press punctuates the PII; Elsevier and CrossRef do not."""
    return re.sub(r"[^0-9A-Za-z]", "", raw).upper()


def doi_from_ncbi_id(url: str, timeout: int = FETCH_TIMEOUT) -> Optional[str]:
    """PubMed/PMC pages carry a PMID or PMCID; NCBI maps those to a DOI."""
    pmc = NCBI_PMC.search(url or "")
    pmid = NCBI_PMID.search(url or "")
    if pmc is None and pmid is None:
        return None
    if pmc:
        ident = pmc.group(1)
        try:
            r = requests.get(
                "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/",
                params={
                    "ids": ident,
                    "format": "json",
                    "tool": "figcite",
                    "email": os.environ.get("FIGCITE_MAILTO", ""),
                },
                headers={"User-Agent": UA},
                timeout=timeout,
            )
            r.raise_for_status()
            recs = r.json().get("records", [])
        except Exception as e:
            raise LookupUnavailable(f"NCBI id converter failed for {ident}: {e}") from e
        for rec in recs:
            if rec.get("doi"):
                return normalize_doi(rec["doi"])
        return None

    # A PMID need not be in PMC at all, so idconv is the wrong endpoint for it;
    # esummary carries the DOI in articleids.
    assert pmid is not None  # the early return above covers the other case
    ident = pmid.group(1)
    try:
        r = requests.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params={
                "db": "pubmed",
                "id": ident,
                "retmode": "json",
                "tool": "figcite",
                "email": os.environ.get("FIGCITE_MAILTO", ""),
            },
            headers={"User-Agent": UA},
            timeout=timeout,
        )
        r.raise_for_status()
        rec = r.json()["result"][ident]
    except Exception as e:
        raise LookupUnavailable(f"NCBI esummary failed for PMID {ident}: {e}") from e
    for a in rec.get("articleids", []):
        if a.get("idtype") == "doi" and a.get("value"):
            return normalize_doi(a["value"])
    return None


def doi_from_alternative_id(url: str, timeout: int = FETCH_TIMEOUT) -> Optional[str]:
    """Resolve a publisher article ID in the URL via CrossRef's alternative-id.

    Elsevier/ScienceDirect URLs carry a PII rather than a DOI, and those
    publishers bot-wall the citation_doi fetch. But CrossRef indexes the PII as
    an alternative-id, so the lookup is exact: one hit means the identifier in
    the address IS this work. Anything other than exactly one hit is refused --
    an ambiguous identifier is not evidence.
    """
    piis: list[str] = []
    for pat in (PII_IN_URL, PII_IN_ASSET):
        for m in pat.finditer(url or ""):
            pii = _normalize_pii(m.group(1))
            if pii not in piis:
                piis.append(pii)
    # Every identifier is tried, in order: an asset path can name the ISSUE
    # ahead of the article, and the issue has no work of its own to resolve to.
    for pii in piis:
        try:
            r = throttled_get(
                "https://api.crossref.org/works",
                params={
                    "filter": f"alternative-id:{pii}",
                    "rows": 3,
                    "select": "DOI,alternative-id",
                },
                headers={"User-Agent": UA},
                timeout=timeout,
            )
            r.raise_for_status()
            items = r.json()["message"]["items"]
        except Exception as e:
            # A throttled or failed lookup is NOT the same as "this URL has no DOI".
            raise LookupUnavailable(f"CrossRef alternative-id lookup failed for {pii}: {e}") from e
        if len(items) == 1:
            return normalize_doi(items[0].get("DOI", ""))
    return None


def doi_from_page_meta(url: str, timeout: int = FETCH_TIMEOUT) -> Optional[str]:
    """Read <meta name="citation_doi"> -- the page declaring its own DOI."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
    except Exception:
        return None
    head = r.text[:400_000]
    for tag in CITATION_DOI_META.findall(head):
        cm = CONTENT_ATTR.search(tag)
        if cm:
            d = normalize_doi(cm.group(1))
            if d.startswith("10."):
                return d
    return None


def url_to_doi(url: str, *, allow_fetch: bool = True) -> tuple[Optional[str], str]:
    """(doi, evidence). Every candidate is checked against CrossRef before use."""
    tried: list[str] = []
    for label, cand in (
        ("the URL itself", doi_from_url_text(url)),
        ("a known publisher URL pattern", doi_from_publisher_pattern(url)),
    ):
        if cand:
            tried.append(f"{cand} from {label}")
            try:
                if fetch_work(cand) is not None:
                    return cand, f"{label} (verified in CrossRef)"
            except Exception:
                pass
    # CrossRef-only lookup (no publisher fetch), so it works even where the
    # publisher blocks bots -- which is exactly where it is needed.
    unavailable: list[str] = []
    for fn, label in (
        (doi_from_alternative_id, "a publisher article ID (PII)"),
        (doi_from_ncbi_id, "a PubMed/PMC identifier"),
    ):
        try:
            cand = fn(url)
        except LookupUnavailable as e:
            unavailable.append(str(e))
            continue
        if cand:
            tried.append(f"{cand} from {label}")
            try:
                if fetch_work(cand) is not None:
                    return (
                        cand,
                        f"{label} in the URL, resolved and verified in CrossRef",
                    )
            except Exception as e:
                unavailable.append(f"CrossRef verification failed for {cand}: {e}")

    if allow_fetch:
        cand = doi_from_page_meta(url)
        if cand:
            tried.append(f"{cand} from citation_doi")
            try:
                if fetch_work(cand) is not None:
                    return (
                        cand,
                        "the page's own citation_doi meta tag (verified in CrossRef)",
                    )
            except Exception:
                pass
    if unavailable:
        return None, (
            "LOOKUP FAILED (not an absence of provenance -- retry): " + "; ".join(unavailable)
        )
    if tried:
        return None, "candidate DOIs found but none resolved in CrossRef: " + "; ".join(tried)
    return None, (
        "no DOI in the URL, no publisher pattern matched, no PubMed/PMC id, "
        "no citation_doi meta tag"
    )


# --------------------------------------------------------------- top level


def resolve_from_capture(capture: dict, *, allow_fetch: bool = True) -> dict[str, Any]:
    """Ground a clipboard capture taken from a browser.

    grounded=True means the DOI came from the address of the page that was on
    screen (matched by exact title), which is evidence -- not an inference about
    which paper was meant.
    """
    out: dict[str, Any] = {
        "url": None,
        "doi": None,
        "grounded": False,
        "evidence": "",
        "history_match": None,
    }
    title = capture.get("title") or ""
    page_title = strip_browser_suffix(title)
    if not page_title:
        out["evidence"] = "no window title recorded at capture time"
        return out

    # Say which browser this came from when we cannot read its history, rather
    # than searching Firefox's database for another browser's page and blaming
    # the miss on private browsing. This does not make the other five readable;
    # it stops the diagnosis being wrong about why.
    proc = re.sub(r"\.exe$", "", (capture.get("process") or "").lower().strip())
    foreign = (
        f"the capture came from '{proc}', whose history figcite cannot read "
        f"(only {', '.join(sorted(HISTORY_BACKENDS))} is supported); "
        if proc in BROWSERS_WITHOUT_HISTORY_BACKEND
        else ""
    )

    profiles = firefox_profiles()
    if not profiles:
        out["evidence"] = foreign + "no Firefox profile with a places.sqlite was found"
        return out
    db = snapshot_history(profiles[0])
    if db is None:
        out["evidence"] = f"could not snapshot history from {profiles[0].name}"
        return out

    hit = lookup_by_title(db, page_title)
    if hit is None and capture.get("captured_local"):
        try:
            ts = datetime.fromisoformat(capture["captured_local"]).timestamp()
            hit = lookup_by_time(db, ts)
        except Exception:
            hit = None
    if hit is None:
        # The commonest shape of this failure: a Firefox profile exists, so the
        # lookup runs, but the page was open in a browser whose history we
        # never opened. Blaming private browsing there is a wrong diagnosis of
        # a real miss.
        out["evidence"] = (
            foreign + f"no history entry titled {page_title[:60]!r} "
            "(private-browsing windows leave no history)"
        )
        return out

    out["history_match"] = hit
    out["url"] = hit["url"]
    doi, why = url_to_doi(hit["url"], allow_fetch=allow_fetch)
    out["doi"] = doi
    if doi is None:
        out["evidence"] = f"matched {hit['match']} -> {hit['url'][:80]} but {why}"
        return out

    # Only an exact, unambiguous title match is evidence of WHICH page was shown.
    out["grounded"] = hit["match"] == "exact-title" and not hit["ambiguous"]
    out["evidence"] = f"{hit['match']} in Firefox history -> {hit['url'][:80]} -> DOI from {why}"
    if not out["grounded"]:
        out["evidence"] += "  [which tab was showing is a guess -- confirm before citing]"
    return out
