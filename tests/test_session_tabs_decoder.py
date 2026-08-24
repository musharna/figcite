"""The LZ4 decoder's escape paths and corruption guard, and the URL filters.

`session_tabs` hand-decodes Mozilla's mozlz4 container rather than take a
dependency for it, which means figcite owns an LZ4 implementation. The
existing tests decode a literals-only block and one 4-byte overlapping match
-- the happy path twice. The sweep left the whole variable-length machinery
alive: the 15-escape for match lengths, the 255-continuation chain, and the
back-offset validity check.

Those are not cosmetic. A decoder that mis-parses a length does not fail; it
returns DIFFERENT JSON, and the caller parses whatever came out. The failure
mode is silently reading someone's tab list wrong, and the corruption guard
is the only thing standing between a malformed block and an unbounded copy
loop.

Blocks here are built byte by byte rather than round-tripped through a
compressor, because the point is to place a value exactly ON an escape
boundary -- something a compressor will not do on request.
"""

from __future__ import annotations

import struct

import pytest

from figcite import session_tabs as st


def _block(lit: bytes, match_nibble: int, offset: int, cont: bytes = b"") -> bytes:
    """One LZ4 sequence: token, literals, back-offset, match continuation.

    Literal length is kept under 15 so the LITERAL escape never fires and the
    match machinery is the only variable under test.
    """
    assert len(lit) < 15, "keep literals short so only the match path varies"
    token = (len(lit) << 4) | match_nibble
    return bytes([token]) + lit + struct.pack("<H", offset) + cont


# ------------------------------------------------- the corruption guard


def test_a_zero_back_offset_is_refused():
    """`if offset == 0 or offset > len(out)` survived `Or -> And`.

    Offset zero means "copy starting from the byte after the end of output",
    which is not a location. Under `and` the guard can NEVER fire -- an offset
    of 0 is not greater than a length, so the two operands cannot both hold --
    and a corrupt block runs `out.append(out[start + i])` with `start` at the
    end of the buffer instead of raising.
    """
    with pytest.raises(ValueError, match="corrupt LZ4 back-offset"):
        st.lz4_block_decompress(_block(b"A", 0, 0), 8)


def test_a_back_offset_past_the_start_of_output_is_refused():
    """The other operand, alone. One byte of output cannot be the target of a
    five-byte look-back; the reference points before the block began."""
    with pytest.raises(ValueError, match="corrupt LZ4 back-offset"):
        st.lz4_block_decompress(_block(b"A", 0, 5), 8)


def test_a_back_offset_of_one_is_legal_and_is_a_run(self_check=None):
    """`offset == 0` survived `0 -> 1`, which outlaws offset 1.

    Offset 1 is the single most common match in real LZ4 output: it means
    "repeat the previous byte", and the module's own docstring calls it out
    as the overlapping case a slice copy gets wrong. Rejecting it as corrupt
    would make ordinary session files unreadable.
    """
    got = st.lz4_block_decompress(_block(b"A", 0, 1), 5)

    assert got == b"AAAAA", got


def test_a_back_offset_exactly_at_the_output_length_is_legal():
    """The boundary of the second operand: a look-back to the very first byte
    written is in range, and `>` -- not `>=` -- is what allows it."""
    got = st.lz4_block_decompress(_block(b"abc", 0, 3), 7)

    assert got == b"abcabca", got


# ------------------------------------------------- the match-length escape


def test_a_match_length_of_fifteen_reads_its_continuation_byte():
    """`if match_len == 15` survived `15 -> 16`, and its `while True` survived
    `True -> False`.

    Fifteen is the escape value: the nibble is saturated, so the real length
    continues in the bytes that follow. Both mutants skip the continuation --
    the length stays 15 and, worse, the continuation byte is never consumed,
    so the decoder resumes reading a length byte as though it were the next
    token and every sequence after this one is garbage.
    """
    got = st.lz4_block_decompress(_block(b"A", 0xF, 1, cont=bytes([0])), 20)

    assert got == b"A" * 20, f"{len(got)} bytes: {got[:40]!r}"


def test_a_match_length_of_fourteen_does_not_read_a_continuation():
    """Positive control: "always read a continuation byte" passes the test
    above and eats the next token on every short match."""
    got = st.lz4_block_decompress(_block(b"A", 0xE, 1), 19)

    assert got == b"A" * 19, f"{len(got)} bytes: {got[:40]!r}"


def test_a_two_five_five_continuation_byte_means_the_length_continues():
    """`if b != 255` survived both `NotEq -> Eq` and `255 -> 256`.

    255 is the only value that does not terminate the chain, because it is the
    largest a byte can carry and so cannot be distinguished from "there is
    more". Under `255 -> 256` the chain stops at the first byte, the remaining
    length bytes are re-read as tokens, and the output is silently short.
    Under `NotEq -> Eq` the chain never stops.

    15 + 255 + 3 + 4 = 277 copied bytes on top of one literal.
    """
    got = st.lz4_block_decompress(_block(b"A", 0xF, 1, cont=bytes([255, 3])), 278)

    assert got == b"A" * 278, f"{len(got)} bytes"


def test_a_continuation_chain_of_two_full_bytes_keeps_going():
    """Two 255s in a row: the loop has to survive more than one iteration.
    15 + 255 + 255 + 1 + 4 = 530."""
    got = st.lz4_block_decompress(_block(b"A", 0xF, 1, cont=bytes([255, 255, 1])), 531)

    assert got == b"A" * 531, f"{len(got)} bytes"


# ---------------------------------------------------------- the URL filters


def _session(tmp_path, urls):
    """A session file holding one tab per URL, each with a resolvable DOI."""
    import json

    payload = json.dumps(
        {
            "windows": [
                {
                    "tabs": [
                        {"index": 1, "entries": [{"url": u, "title": "t"}]}
                        for u in urls
                    ]
                }
            ]
        }
    ).encode()
    lit = bytearray()
    n = len(payload)
    if n < 15:
        lit.append(n << 4)
    else:
        lit.append(0xF0)
        rem = n - 15
        while rem >= 255:
            lit.append(255)
            rem -= 255
        lit.append(rem)
    block = bytes(lit) + payload
    p = tmp_path / "recovery.jsonlz4"
    p.write_bytes(b"mozLz40\0" + struct.pack("<I", n) + block)
    return p


def test_a_plain_http_tab_is_still_offered(tmp_path):
    """`url.startswith(("http://", "https://"))` survived mutating "http://".

    Every existing test that expects a tab to COME THROUGH uses https, and
    every http fixture in the suite is a loopback one that is meant to be
    dropped -- so replacing the "http://" literal changed nothing observable:
    the http tabs were excluded either way, just for the wrong reason.

    Plenty of real reading happens over plain http (institutional proxies,
    local mirrors), and dropping those tabs loses the source silently.
    """
    p = _session(tmp_path, ["http://nph.onlinelibrary.wiley.com/doi/10.1111/nph.71477"])

    cands = st.tab_candidates(p)

    assert [c["doi"] for c in cands] == ["10.1111/nph.71477"], cands


def test_a_non_http_scheme_is_not_offered(tmp_path):
    """Positive control: "accept every scheme" passes the test above.
    `file://` and `about:` tabs are not pages a figure was read from."""
    p = _session(
        tmp_path,
        [
            "file:///home/me/doi/10.1111/nph.71477",
            "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.99999",
        ],
    )

    cands = st.tab_candidates(p)

    assert [c["doi"] for c in cands] == ["10.1111/nph.99999"], cands


def test_the_fully_qualified_localhost_name_is_also_loopback(tmp_path):
    """`{"localhost", "localhost.localdomain"}` survived mutating the second
    name.

    `localhost.localdomain` is what a default /etc/hosts calls the loopback
    interface, and it is what some browsers show in the URL bar. It is not an
    ip_address, so the `ipaddress` fallback below cannot catch it -- the name
    in this set is the only thing that excludes it, and figcite proposing its
    own UI as the source of a figure is the exact failure the loopback filter
    exists to prevent.
    """
    p = _session(
        tmp_path,
        [
            "http://localhost.localdomain:8765/doi/10.1111/nph.71477",
            "https://nph.onlinelibrary.wiley.com/doi/10.1111/nph.99999",
        ],
    )

    cands = st.tab_candidates(p)

    assert [c["doi"] for c in cands] == ["10.1111/nph.99999"], (
        f"figcite's own UI was offered as the source of a figure: {cands}"
    )
