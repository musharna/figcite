"""Open-tab reading.

Firefox's session store is a different question from its history. History
records navigations; a paper left open in a tab since yesterday has no recent
visit row at all. Every test here builds its own mozlz4 payload rather than
shipping a copy of a real profile, so it runs anywhere -- with one `live` test
that reads the actual profile, because a hand-built fixture cannot prove we
parse what Firefox actually writes.
"""

import json
import struct

import pytest

from figcite import session_tabs as st


# --------------------------------------------------------------- lz4 fixtures


def _lz4_literals(payload: bytes) -> bytes:
    """A valid LZ4 block that is one literal run and no match."""
    n = len(payload)
    out = bytearray()
    if n < 15:
        out.append(n << 4)
    else:
        out.append(15 << 4)
        rem = n - 15
        while rem >= 255:
            out.append(255)
            rem -= 255
        out.append(rem)
    out += payload
    return bytes(out)


def _mozlz4(block: bytes, decompressed_len: int) -> bytes:
    return b"mozLz40\0" + struct.pack("<I", decompressed_len) + block


def test_decoder_roundtrips_a_literal_only_block():
    payload = b'{"windows":[]}' * 40  # >15 bytes, exercises the length escape
    got = st.lz4_block_decompress(_lz4_literals(payload), len(payload))
    assert got == payload


def test_decoder_expands_a_back_reference():
    """A literals-only test cannot fail if the match path is broken.

    This block is literals 'abc' followed by a 4-byte match at offset 3, whose
    copy overlaps its own output -- the case a naive slice-copy gets wrong.
    """
    block = bytes([(3 << 4) | 0]) + b"abc" + struct.pack("<H", 3)
    # Literals "abc", then copy 4 bytes from 3 back. The 4th byte copied is one
    # this very match just wrote, so a slice copy -- which reads only the
    # pre-match buffer -- yields "abcabc" and comes up a byte short.
    assert st.lz4_block_decompress(block, 7) == b"abcabca"


MAGIC_BYTES = st.MAGIC


def test_bad_magic_is_refused_and_a_good_one_is_not(tmp_path):
    payload = b'{"windows":[]}'
    good = tmp_path / "good.jsonlz4"
    good.write_bytes(_mozlz4(_lz4_literals(payload), len(payload)))
    # Positive control in the same test: if the reader were broken outright,
    # the negative assertion below would pass for the wrong reason.
    assert st.read_mozlz4(good) == payload

    # Two bad magics, one on each side of MAGIC in byte order.
    #
    # `raw[:8] != MAGIC` compares for INEQUALITY, and a single counter-example
    # cannot show that: b"NOTMOZLZ" sorts BELOW b"mozLz40\0" ('N' < 'm'), so a
    # guard narrowed to `raw[:8] < MAGIC` refuses it too and the test passes on
    # the narrowed guard. The reduced-ROR sweep found exactly that survivor.
    #
    # Anything sorting ABOVE separates them, and such a file is not
    # far-fetched -- any header beginning with a byte above 'm' qualifies.
    for label, magic in (("below", b"NOTMOZLZ"), ("above", b"zzzzzzzz")):
        assert (magic < MAGIC_BYTES) == (label == "below"), (
            f"{magic!r} no longer sorts {label} the real magic; this test has "
            f"stopped bracketing it"
        )
        bad = tmp_path / f"bad-{label}.jsonlz4"
        bad.write_bytes(
            magic + struct.pack("<I", len(payload)) + _lz4_literals(payload)
        )
        with pytest.raises(ValueError, match="mozLz4"):
            st.read_mozlz4(bad)


# --------------------------------------------------------------- tab reading


def _session_file(tmp_path, session: dict):
    raw = json.dumps(session).encode()
    p = tmp_path / "recovery.jsonlz4"
    p.write_bytes(_mozlz4(_lz4_literals(raw), len(raw)))
    return p


def test_open_tabs_takes_the_entry_the_tab_is_actually_showing(tmp_path):
    """`index` is 1-based and points into the tab's back/forward list.

    A tab that navigated A -> B -> C and was then sent Back twice is SHOWING A.
    Taking entries[-1] would report C: a page the user navigated away from.
    """
    p = _session_file(
        tmp_path,
        {
            "windows": [
                {
                    "tabs": [
                        {
                            "index": 1,
                            "entries": [
                                {"url": "https://example.org/a", "title": "A"},
                                {"url": "https://example.org/b", "title": "B"},
                                {"url": "https://example.org/c", "title": "C"},
                            ],
                        }
                    ]
                }
            ]
        },
    )
    tabs = st.open_tabs(p)
    assert [t["url"] for t in tabs] == ["https://example.org/a"]
    assert tabs[0]["title"] == "A"


def test_open_tabs_survives_a_tab_with_no_entries(tmp_path):
    """A pending/lazy tab has an empty entry list. It must not kill the scan."""
    p = _session_file(
        tmp_path,
        {
            "windows": [
                {
                    "tabs": [
                        {"index": 0, "entries": []},
                        {
                            "index": 1,
                            "entries": [
                                {"url": "https://example.org/real", "title": "Real"}
                            ],
                        },
                    ]
                }
            ]
        },
    )
    assert [t["url"] for t in st.open_tabs(p)] == ["https://example.org/real"]


# --------------------------------------------------------------- candidates


def test_loopback_tabs_are_never_offered_as_a_source(tmp_path):
    """figcite's own UI is a web page, and it is open while you use figcite.

    Measured on the real profile: the nearest-visit-in-time fallback returned
    `http://127.0.0.1:8765/` -- figcite proposing itself as the source of the
    figure. A tab list has the same hazard, so loopback is excluded here.
    """
    p = _session_file(
        tmp_path,
        {
            "windows": [
                {
                    "tabs": [
                        # Crafted to LOOK resolvable: if the exclusion is keyed on "has no
                        # DOI" rather than "is loopback", this tab slips through.
                        {
                            "index": 1,
                            "entries": [
                                {
                                    "url": "http://127.0.0.1:8765/doi/10.1111/nph.71477",
                                    "title": "figcite",
                                }
                            ],
                        },
                        {
                            "index": 1,
                            "entries": [
                                {
                                    "url": "http://localhost:8765/doi/10.1111/nph.99999",
                                    "title": "figcite",
                                }
                            ],
                        },
                        # Positive control: a real publisher tab in the SAME fixture must
                        # still come through, or a broken scan reads as "correctly excluded".
                        {
                            "index": 1,
                            "entries": [
                                {
                                    "url": "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477",
                                    "title": "AUXIN RESPONSE FACTORs",
                                }
                            ],
                        },
                    ]
                }
            ]
        },
    )
    cands = st.tab_candidates(p)
    dois = [c["doi"] for c in cands]
    assert dois == ["10.1111/nph.71477"]
    assert all("127.0.0.1" not in c.get("url", "") for c in cands)
    assert all("localhost" not in c.get("url", "") for c in cands)


def test_candidates_are_shaped_the_way_the_ui_renders_them(tmp_path):
    p = _session_file(
        tmp_path,
        {
            "windows": [
                {
                    "tabs": [
                        {
                            "index": 1,
                            "entries": [
                                {
                                    "url": "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477",
                                    "title": "AUXIN RESPONSE FACTORs",
                                }
                            ],
                        }
                    ]
                }
            ]
        },
    )
    c = st.tab_candidates(p)[0]
    # webui.py renders c.source, c.score, c.title, c.container, c.year.
    for field in ("source", "score", "doi", "title", "container", "year", "type"):
        assert field in c, f"UI reads {field}; candidate has {sorted(c)}"
    assert c["source"] == "open-tab"


def test_a_tab_with_no_resolvable_doi_is_dropped_not_offered_blank(tmp_path):
    p = _session_file(
        tmp_path,
        {
            "windows": [
                {
                    "tabs": [
                        {
                            "index": 1,
                            "entries": [
                                {
                                    "url": "https://www.google.com/search?q=arf",
                                    "title": "arf - Google Search",
                                }
                            ],
                        },
                        {
                            "index": 1,
                            "entries": [
                                {
                                    "url": "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477",
                                    "title": "AUXIN RESPONSE FACTORs",
                                }
                            ],
                        },
                    ]
                }
            ]
        },
    )
    cands = st.tab_candidates(p)
    assert [c["doi"] for c in cands] == ["10.1111/nph.71477"]


def test_scanning_tabs_touches_no_network():
    """The scan runs on every pending-list render, so it must stay offline.

    conftest's autouse `_block_network` turns any non-loopback connect into an
    error, so a CrossRef verification added to this path would fail here rather
    than silently costing a second per tab.
    """
    # Read the names the compiled function actually references, not its source
    # text -- the source names `url_to_doi` in the comment explaining why it is
    # avoided, so a substring check over source fails on the explanation.
    called = st.tab_candidates.__code__.co_names
    assert "url_to_doi" not in called, (
        "url_to_doi verifies every candidate against CrossRef -- one throttled "
        "network round trip per open tab. Use the offline extractors."
    )
    # Positive control: if the scan stopped extracting DOIs altogether, the
    # assertion above would pass for the wrong reason.
    assert "doi_from_url_text" in called


@pytest.mark.live
def test_the_real_firefox_profile_parses():
    """A hand-built fixture cannot prove we parse what Firefox actually writes."""
    p = st.session_store_path()
    if p is None:
        pytest.skip("no Firefox profile with a session store on this machine")
    tabs = st.open_tabs(p)
    assert tabs, "session store parsed but reported zero open tabs"
    assert all(t.get("url") for t in tabs)
