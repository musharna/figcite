"""What is open in the browser RIGHT NOW.

History answers "what did I navigate to". That is a different question from
"what was on screen when I hit the snip key", and the gap between them is not
academic: a paper opened yesterday and left in a tab has no recent visit row at
all, which is the commonest way anyone reads one. Measured on a real profile,
the paper a figure was snipped from appeared in **zero** history rows and was
sitting in the session store the whole time.

Firefox writes its open tabs to `sessionstore-backups/recovery.jsonlz4` -- JSON
in Mozilla's own "mozlz4" container, which is an 8-byte magic, a little-endian
uint32 of the decompressed size, and then a raw LZ4 block. The block format is
small enough to decode here rather than take a dependency on `lz4` for it.

Nothing in this module confirms anything. An open tab is evidence that a page
was on screen recently, not evidence of which tab was showing at the instant of
a snip, so everything it produces is a candidate for a human to pick from.
"""

from __future__ import annotations

import ipaddress
import json
import struct
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

from .browser import doi_from_publisher_pattern, doi_from_url_text, firefox_profiles

MAGIC = b"mozLz40\0"


def lz4_block_decompress(src: bytes, expected: int) -> bytes:
    """Decode one raw LZ4 block.

    A sequence is: a token byte, then that many literal bytes, then a 2-byte
    little-endian back-offset and a match length. Both lengths escape at 15 and
    continue in 255-chunks. The match is copied **byte at a time on purpose**:
    an LZ4 match may overlap its own output (offset 1, length 40 = "repeat this
    byte 40 times"), which a slice copy silently gets wrong.
    """
    out = bytearray()
    pos = 0
    n = len(src)
    while pos < n:
        token = src[pos]
        pos += 1

        lit_len = token >> 4
        if lit_len == 15:
            while True:
                b = src[pos]
                pos += 1
                lit_len += b
                if b != 255:
                    break
        out += src[pos : pos + lit_len]
        pos += lit_len

        # The final sequence of a block is literals with no match following.
        if pos >= n:
            break

        offset = src[pos] | (src[pos + 1] << 8)
        pos += 2
        if offset == 0 or offset > len(out):
            raise ValueError(f"corrupt LZ4 back-offset {offset} at byte {pos}")

        match_len = token & 0x0F
        if match_len == 15:
            while True:
                b = src[pos]
                pos += 1
                match_len += b
                if b != 255:
                    break
        match_len += 4  # the minimum encodable match

        start = len(out) - offset
        for i in range(match_len):
            out.append(out[start + i])

    return bytes(out[:expected])


def read_mozlz4(path) -> bytes:
    """Decompressed bytes of a mozlz4 file."""
    raw = Path(path).read_bytes()
    if raw[:8] != MAGIC:
        raise ValueError(f"{path} is not a mozLz4 file (magic was {raw[:8]!r}, expected {MAGIC!r})")
    (size,) = struct.unpack("<I", raw[8:12])
    return lz4_block_decompress(raw[12:], size)


def session_store_path() -> Optional[Path]:
    """The newest profile's live session store, or None."""
    for profile in firefox_profiles():
        p = profile / "sessionstore-backups" / "recovery.jsonlz4"
        if p.exists():
            return p
    return None


def open_tabs(path=None) -> list[dict[str, str]]:
    """[{'url', 'title'}] for every tab currently open, across all windows."""
    path = path or session_store_path()
    if path is None:
        return []
    try:
        session = json.loads(read_mozlz4(path))
    except Exception:
        # A session store being rewritten under us is normal, not exceptional.
        return []

    tabs: list[dict[str, str]] = []
    for window in session.get("windows", []) or []:
        for tab in window.get("tabs", []) or []:
            entries = tab.get("entries") or []
            # `index` is 1-based into the tab's back/forward list. A tab sent
            # Back is showing an EARLIER entry, so entries[-1] would report a
            # page the user navigated away from.
            i = int(tab.get("index", len(entries))) - 1
            if 0 <= i < len(entries):
                entry = entries[i]
                url = entry.get("url") or ""
                if url:
                    tabs.append({"url": url, "title": entry.get("title") or ""})
    return tabs


def _is_loopback(url: str) -> bool:
    """Structurally, not by name-matching.

    figcite's own UI is a web page and it is open exactly when figcite is in
    use, so it is a standing candidate for "the page I was looking at". It is
    never the source of a figure.
    """
    host = urlsplit(url).hostname
    if not host:
        return False
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def tab_candidates(path=None) -> list[dict[str, Any]]:
    """Open tabs whose URL yields a DOI, shaped as pending-list candidates.

    Offline only. This runs on every render of the pending list, and
    `browser.url_to_doi` verifies each candidate against CrossRef -- one
    throttled network round trip per open tab, which on a real profile (39
    http tabs) is most of a minute. The two extractors used here are pure
    regex over the URL. Verification happens once, later, when a human picks
    one and `record_for` looks it up.
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tab in open_tabs(path):
        url = tab["url"]
        if not url.startswith(("http://", "https://")) or _is_loopback(url):
            continue
        doi = doi_from_url_text(url) or doi_from_publisher_pattern(url)
        if not doi or doi in seen:
            continue
        seen.add(doi)
        out.append(
            {
                "source": "open-tab",
                "score": "",
                "doi": doi,
                "title": tab["title"],
                "container": urlsplit(url).hostname or "",
                "year": "",
                "type": "",
                "url": url,
            }
        )
    return out
