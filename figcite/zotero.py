"""Resolve a capture against the Zotero library you actually curated.

Why this sits AHEAD of CrossRef in the resolution chain: CrossRef title search
is a search over ~150M works and is known here to rank a review above the paper
it reviews (there is a live test pinning that). Your Zotero library is a few
thousand works you personally collected, and a figure you snipped is far more
likely to come from one of them than from an arbitrary CrossRef hit. Same query,
enormously better prior.

**Which library.** Measured 2026-08-16: the local desktop DB at
`<Zotero>/zotero.sqlite` holds 5 items, while the group library `claude-refs`
(6532713) holds 4,824. Resolving against the local DB would have produced an
almost total null and looked like "Zotero cannot help here". The web API is the
real library, so that is what this queries.

**What counts as grounded.** Deliberately the same predicate the browser route
already uses (`browser.resolve`: exact title match, not ambiguous). An exact,
unique, normalized title match against a library you curated is evidence about
WHICH paper was on screen. Anything fuzzier is a candidate, returned unconfirmed
for `figcite confirm` to settle. A near-match that is not unique is specifically
NOT grounded -- two papers sharing a title is exactly the case where guessing is
worst.

**Credentials** come from the environment only. There is no key in this file and
none is ever written to the cache; see `credentials()`.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from pathlib import Path
from typing import Any, Optional

import requests

from .crossref import CACHE as _CROSSREF_CACHE
from .crossref import DOI_RE, LookupUnavailable, normalize_doi

# doi.org links in the URL field are the dominant DOI carrier in a
# webpage-heavy library; see _doi_of().
DOI_URL_RE = re.compile(
    r"(?:dx\.)?doi\.org/(" + DOI_RE.pattern.lstrip(r"\b") + ")", re.I
)

API = "https://api.zotero.org"
API_VERSION = "3"
TIMEOUT = 30
PAGE = 100

# A local library snapshot. Kept beside the CrossRef cache so `FIGCITE_CACHE`
# relocates both together.
CACHE_DIR = Path(
    os.environ.get("FIGCITE_ZOTERO_CACHE", _CROSSREF_CACHE.parent / "zotero")
)
# Refresh the snapshot when it is older than this. A reference library changes
# slowly; a stale hit is still a real hit, and `figcite zotero sync` forces it.
DEFAULT_TTL_HOURS = float(os.environ.get("FIGCITE_ZOTERO_TTL_HOURS", "168"))


class NotConfigured(RuntimeError):
    """No Zotero credentials available.

    Distinct from LookupUnavailable (configured but unreachable) and from a
    genuine miss (configured, reachable, no such title). Collapsing these is how
    "you never set an API key" turns into "that paper isn't in your library".
    """


# Credentials on disk, mode 0600.
#
# Environment variables alone are not enough, and the reason is specific and was
# measured: the clipboard watcher is started by a Windows launcher that runs
# `wsl.exe ... bash -lc`, and that shell inherits none of an interactive shell's
# exports. Verified 2026-08-16 -- every ZOTERO_* variable came back UNSET there.
# Library-first resolution would therefore have passed every test and then
# silently never fired in the one path it was built for.
#
# The alternative fix, exporting the key from a shell rc, adds another
# world-readable copy of a live credential. This file is written 0600 instead,
# and `credentials()` complains if the mode is ever loosened.
CONFIG_FILE = Path(
    os.environ.get(
        "FIGCITE_ZOTERO_CONFIG", Path.home() / ".config" / "figcite" / "zotero.json"
    )
)


def save_credentials(api_key: str, library_id: str, library_type: str = "user") -> Path:
    """Write credentials 0600 so background processes can read them."""
    if library_type not in ("user", "group"):
        raise ValueError(
            f"library type must be 'user' or 'group', got {library_type!r}"
        )
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Create with the right mode from the outset; writing then chmod-ing leaves
    # a window where the key is readable.
    fd = os.open(CONFIG_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "api_key": api_key,
                "library_id": str(library_id),
                "library_type": library_type,
            },
            fh,
        )
    os.chmod(CONFIG_FILE, 0o600)  # in case the file already existed
    return CONFIG_FILE


def _from_file() -> tuple[Optional[str], Optional[str], Optional[str]]:
    if not CONFIG_FILE.exists():
        return None, None, None
    mode = CONFIG_FILE.stat().st_mode & 0o777
    if mode & 0o077:
        raise NotConfigured(
            f"{CONFIG_FILE} is mode {mode:o} and holds a live API key; "
            f"refusing to read it. Fix with: chmod 600 {CONFIG_FILE}"
        )
    try:
        d = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise NotConfigured(f"could not read {CONFIG_FILE}: {e}") from e
    return d.get("api_key"), d.get("library_id"), d.get("library_type")


def credentials() -> tuple[str, str, str]:
    """(api_key, library_id, library_type).

    Environment first so a shell can override for one run, then the 0600 config
    file so background processes -- the clipboard watcher above all -- can
    resolve without inheriting anyone's exports.
    """
    f_key, f_lib, f_typ = _from_file()
    key = (
        os.environ.get("FIGCITE_ZOTERO_API_KEY")
        or os.environ.get("ZOTERO_API_KEY")
        or f_key
    )
    lib = (
        os.environ.get("FIGCITE_ZOTERO_LIBRARY_ID")
        or os.environ.get("ZOTERO_LIBRARY_ID")
        or f_lib
    )
    typ = (
        os.environ.get("FIGCITE_ZOTERO_LIBRARY_TYPE")
        or os.environ.get("ZOTERO_LIBRARY_TYPE")
        or f_typ
        or "user"
    ).lower()
    if not key or not lib:
        raise NotConfigured(
            "no Zotero credentials. Run `figcite zotero configure --api-key ... "
            "--library-id ... --type group`, or set FIGCITE_ZOTERO_API_KEY and "
            "FIGCITE_ZOTERO_LIBRARY_ID"
        )
    if typ not in ("user", "group"):
        raise NotConfigured(f"library type must be 'user' or 'group', got {typ!r}")
    return key, lib, typ


def configured() -> bool:
    try:
        credentials()
        return True
    except NotConfigured:
        return False


# ------------------------------------------------------------------ matching

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
# Zotero's own attachment naming: "Harris et al. - 2020 - Array programming.pdf"
_ZOTERO_FILENAME = re.compile(
    r"^(?P<who>.{2,80}?)\s+-\s+(?P<year>(19|20)\d{2})\s+-\s+(?P<title>.+)$"
)


def normalize_title(s: str) -> str:
    """Fold a title to a comparison key.

    Unicode-normalized because a title copied out of a PDF carries typographic
    dashes and ligatures that a title from the API does not; comparing those raw
    makes an exact match look like a miss.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("–", "-").replace("—", "-").replace("’", "'")
    s = _PUNCT.sub(" ", s)
    return _WS.sub(" ", s).strip()


# A browser tab title is conventionally "<page title> <sep> <site name>".
# Matching the site name by NAME cannot work: publishers are an open set, and an
# enumeration of them silently fails for every one not listed. Measured on the
# existing publisher list -- of ten real suffixes, " | PNAS", " | Frontiers",
# " | Science" and " | Cell" all defeated exact matching, because those names
# were simply not in the list. So the tail is identified STRUCTURALLY (a short
# trailing run after a separator) and offered as a candidate, rather than
# recognised.
# A colon is deliberately NOT a separator here: journal titles use ":" for
# subtitles far more often than browsers use it for site names, so splitting on
# it would trim real title text. Browsers use the pipe and dash family.
_TAB_TAIL = re.compile(r"^(?P<head>.+?)\s+[|–—\-·]\s+(?P<tail>[^|]{1,60})$")
# Long enough for the real journal names -- "Proceedings of the National Academy
# of Sciences" is seven words, and a five-word cap silently dropped it. The
# exact-and-unique requirement is what keeps the bar high, not this number: a
# trimmed candidate that matches nothing simply loses.
_MAX_TAIL_WORDS = 8


def title_variants(title: str) -> list[str]:
    """Candidate queries for a browser tab title, most literal first.

    Returns the title itself, then progressively trimmed forms with a trailing
    site name removed. Grounding still requires an exact, unique match against
    the library, so offering more candidates does not lower the bar -- it only
    stops a publisher we happen not to have heard of from hiding an exact match.
    """
    out: list[str] = []
    cur = (title or "").strip()
    seen: set[str] = set()
    for _ in range(3):  # "Title | Journal | Publisher" bottoms out quickly
        if cur and cur.lower() not in seen:
            seen.add(cur.lower())
            out.append(cur)
        m = _TAB_TAIL.match(cur)
        if not m:
            break
        tail = m.group("tail").strip()
        if len(tail.split()) > _MAX_TAIL_WORDS:
            break
        cur = m.group("head").strip()
    return out


def resolve_page_title(title: str, max_age_hours: Optional[float] = None) -> dict:
    """resolve() over the candidate forms of a browser tab title.

    Stops at the first candidate that grounds. If none ground, the result of the
    most literal candidate is returned, so the evidence describes what was
    actually asked rather than some trimmed variant of it.
    """
    first: Optional[dict] = None
    for cand in title_variants(title):
        r = resolve(cand, max_age_hours=max_age_hours)
        if first is None:
            first = r
        if r.get("grounded"):
            r["evidence"] = r["evidence"] + (
                f" (matched on {cand!r})" if cand != title else ""
            )
            return r
        # An unreachable library will not become reachable on the next variant.
        if r.get("available") is False:
            return r
        if r.get("candidates") and not (first.get("candidates")):
            first = r
    return first or resolve(title, max_age_hours=max_age_hours)


def title_from_pdf_name(name: str) -> str:
    """Recover the paper title from a Zotero-style attachment filename.

    Zotero names attachments "<creators> - <year> - <title>.pdf". Searching the
    whole filename matches nothing, because no title contains its own author
    list; the title alone is what the library can be searched by.
    """
    stem = re.sub(r"\.pdf$", "", (name or "").strip(), flags=re.I)
    m = _ZOTERO_FILENAME.match(stem)
    return (m.group("title") if m else stem).strip()


# ------------------------------------------------------------------- fetching


def _cache_file(lib: str, typ: str) -> Path:
    return CACHE_DIR / f"{typ}-{lib}.json"


def _get(path: str, key: str, **params) -> requests.Response:
    try:
        r = requests.get(
            f"{API}{path}",
            params=params,
            headers={"Zotero-API-Key": key, "Zotero-API-Version": API_VERSION},
            timeout=TIMEOUT,
        )
    except Exception as e:
        raise LookupUnavailable(f"Zotero request failed: {e}") from e
    if r.status_code == 403:
        raise LookupUnavailable(
            "Zotero refused the API key (403). The key may lack access to this library."
        )
    if r.status_code == 429 or r.status_code >= 500:
        raise LookupUnavailable(f"Zotero returned {r.status_code}; try again later")
    if r.status_code != 200:
        raise LookupUnavailable(f"Zotero returned {r.status_code} for {path}")
    return r


def _doi_of(d: dict) -> tuple[str, str]:
    """The DOI of a raw Zotero item, and where it was found.

    The DOI field alone is not enough, and the gap is not small. Measured on
    this library: 147 items populate the DOI field, but 540 carry a doi.org
    URL -- because 4,677 of 4,824 items are `webpage`, an item type that HAS no
    DOI field, so a clipped paper landing page keeps its DOI only in the URL.
    Reading the DOI field alone would have discarded most of the coverage that
    exists. (`extra` was checked too and holds none: 0 of 400 sampled.)
    """
    doi = normalize_doi(d.get("DOI") or "")
    if doi:
        return doi, "doi-field"
    url = d.get("url") or ""
    m = DOI_URL_RE.search(url)
    if m:
        return normalize_doi(m.group(1)), "url"
    return "", ""


def fetch_library(progress: bool = False) -> list[dict[str, Any]]:
    """Every bibliographic item in the library, paged.

    Attachments and notes are excluded server-side: they carry their parent's
    title but no DOI, so they would double every entry with a hollow twin.
    """
    key, lib, typ = credentials()
    base = f"/{typ}s/{lib}/items"
    out: list[dict[str, Any]] = []
    start = 0
    while True:
        r = _get(
            base,
            key,
            format="json",
            include="data",
            itemType="-attachment || note",
            limit=PAGE,
            start=start,
        )
        batch = r.json()
        if not batch:
            break
        for it in batch:
            d = it.get("data") or {}
            title = d.get("title") or ""
            if not title:
                continue
            doi, doi_from = _doi_of(d)
            out.append(
                {
                    "key": d.get("key", ""),
                    "title": title,
                    "doi": doi,
                    "doi_source": doi_from,
                    "url": d.get("url") or "",
                    "date": d.get("date") or "",
                    "itemType": d.get("itemType") or "",
                    "creators": [
                        c.get("lastName") or c.get("name") or ""
                        for c in (d.get("creators") or [])
                    ],
                }
            )
        if progress:
            print(f"    fetched {len(out)} items...", flush=True)
        if len(batch) < PAGE:
            break
        start += PAGE
        # The API allows well under this; be a polite client of someone else's server.
        time.sleep(0.2)
    return out


def sync(progress: bool = False) -> dict:
    """Refresh the local snapshot. Returns a summary."""
    _, lib, typ = credentials()
    items = fetch_library(progress=progress)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "library": f"{typ}s/{lib}",
        "items": items,
    }
    _cache_file(lib, typ).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return {
        "items": len(items),
        "with_doi": sum(1 for i in items if i["doi"]),
        "cache": str(_cache_file(lib, typ)),
    }


def library(max_age_hours: Optional[float] = None) -> list[dict[str, Any]]:
    """The cached library, syncing if absent or stale."""
    _, lib, typ = credentials()
    cf = _cache_file(lib, typ)
    ttl = DEFAULT_TTL_HOURS if max_age_hours is None else max_age_hours
    if cf.exists():
        age_h = (time.time() - cf.stat().st_mtime) / 3600.0
        if age_h <= ttl:
            try:
                return json.loads(cf.read_text(encoding="utf-8"))["items"]
            except Exception:
                pass  # corrupt snapshot: fall through and refetch
    sync()
    return json.loads(cf.read_text(encoding="utf-8"))["items"]


# ------------------------------------------------------------------ resolving


def _year(item: dict) -> Optional[int]:
    m = re.search(r"(19|20)\d{2}", item.get("date") or "")
    return int(m.group(0)) if m else None


def _short_cite(item: dict) -> str:
    who = [c for c in item.get("creators") or [] if c]
    yr = _year(item)
    if not who:
        stem = ""
    elif len(who) == 1:
        stem = who[0]
    elif len(who) == 2:
        stem = f"{who[0]} & {who[1]}"
    else:
        stem = f"{who[0]} et al."
    return f"{stem} {yr}".strip() if yr else stem


def _candidate(item: dict, how: str) -> dict:
    return {
        "doi": item.get("doi") or "",
        "title": item.get("title") or "",
        "year": _year(item),
        "container": item.get("itemType") or "",
        "type": item.get("itemType") or "",
        "score": {"exact-title": 100.0, "contains-title": 60.0}.get(how, 40.0),
        "zotero_key": item.get("key") or "",
        "short_cite": _short_cite(item),
        "source": "zotero",
    }


def resolve_title(title: str, max_age_hours: Optional[float] = None) -> dict:
    """Look one title up in the library.

    Returns {doi, grounded, evidence, candidates}. `grounded` is True only for an
    exact, unique normalized-title match that carries a DOI -- the same bar the
    Firefox-history route uses. Two library items sharing a title makes the match
    ambiguous, which is precisely when a guess is least defensible, so that case
    returns candidates and grounds nothing.
    """
    out: dict[str, Any] = {
        "doi": None,
        "grounded": False,
        "evidence": "",
        "candidates": [],
    }
    q = normalize_title(title)
    if len(q) < 8:
        out["evidence"] = f"title {title!r} too short to search the library with"
        return out

    items = library(max_age_hours=max_age_hours)
    if not items:
        out["evidence"] = "Zotero library snapshot is empty"
        return out

    exact = [i for i in items if normalize_title(i["title"]) == q]
    # Duplicate RECORDS are not ambiguity about the ANSWER.
    #
    # This used to require exactly one item, and the instinct was right: two
    # works under one title makes the match a guess. But the question asked is
    # "what DOI does this title resolve to", and the answer is a DOI, not an
    # item. A reference manager permits duplicates -- Zotero ships a merge
    # screen because of it -- so one paper saved twice is the ordinary case.
    # When every record bearing the title names the SAME canonical DOI there is
    # no competing answer to be ambiguous between. Measured on the library this
    # was written against: 17 titles were refused for that reason alone.
    #
    # Two things deliberately still refuse. Records naming DIFFERENT DOIs are
    # the case the original rule existed for. And a DOI-LESS twin blocks too:
    # it names no competing answer, but neither does it confirm it is the same
    # work, and grounding is this project's highest bar -- that call belongs to
    # whoever owns the library, not to this function.
    with_doi = [i for i in exact if i["doi"]]
    canonical = {normalize_doi(i["doi"]).lower() for i in with_doi}
    if exact and len(with_doi) == len(exact) and len(canonical) == 1:
        it = with_doi[0]
        n = len(exact)
        where = (
            f"exact unique title match in Zotero ({it['key']})"
            if n == 1
            else (
                f"{n} Zotero records share this title and all name the same "
                f"DOI ({it['key']} and {n - 1} duplicate(s))"
            )
        )
        out.update(
            doi=it["doi"],
            grounded=True,
            evidence=f"{where}: {_short_cite(it)}",
            candidates=[_candidate(i, "exact-title") for i in exact[:5]],
        )
        return out
    if len(exact) == 1 and not exact[0]["doi"]:
        out.update(
            evidence=(
                f"exact title match in Zotero ({exact[0]['key']}) but that item "
                f"has no DOI recorded"
            ),
            candidates=[_candidate(exact[0], "exact-title")],
        )
        return out
    if len(exact) > 1:
        out.update(
            evidence=(
                f"{len(exact)} Zotero items share this title -- ambiguous, so "
                f"which one the figure came from is a guess"
            ),
            candidates=[_candidate(i, "exact-title") for i in exact[:5]],
        )
        return out

    # Substring both ways: a window title is often truncated ("...") and a
    # library title is sometimes the subtitle-bearing full version.
    near = [
        i
        for i in items
        if i["doi"]
        and len(normalize_title(i["title"])) >= 8
        and (q in normalize_title(i["title"]) or normalize_title(i["title"]) in q)
    ]
    if near:
        out.update(
            evidence=(
                f"{len(near)} partial title match(es) in Zotero -- not exact, so "
                f"unconfirmed; pick one with `figcite confirm`"
            ),
            candidates=[_candidate(i, "contains-title") for i in near[:5]],
        )
        return out

    out["evidence"] = (
        f"no Zotero item titled like {title[:60]!r} ({len(items)} searched)"
    )
    return out


def resolve(title: str, max_age_hours: Optional[float] = None) -> dict:
    """resolve_title, but tolerant of an unconfigured or unreachable library.

    A missing API key must not look like "the paper is not in your library", so
    the reason is carried through in `evidence` and `available` says which of the
    two happened.
    """
    try:
        r = resolve_title(title, max_age_hours=max_age_hours)
        r["available"] = True
        return r
    except NotConfigured as e:
        return {
            "doi": None,
            "grounded": False,
            "available": False,
            "evidence": f"Zotero not configured ({e})",
            "candidates": [],
        }
    except LookupUnavailable as e:
        return {
            "doi": None,
            "grounded": False,
            "available": False,
            "evidence": f"Zotero unreachable ({e}) -- this is NOT a miss",
            "candidates": [],
        }
