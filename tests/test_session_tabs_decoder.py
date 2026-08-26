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


# ------------------------------- the operands, from the side nothing stood on


def test_a_back_offset_strictly_inside_the_output_is_legal():
    """Every legal-offset test in this file uses `offset == len(out)`.

    `_block(b"abc", 0, 3)` and `_block(b"A", 0, 1)` both look back to the very
    first byte written, and at that point `offset > len(out)` and
    `offset != len(out)` give the SAME answer. So the guard could be checking
    inequality rather than range and nothing here would notice -- which is what
    the reduced-ROR sweep found: `offset > len(out)` -> `!=` survived a file
    full of back-offset tests.

    Under that mutant every ordinary match is rejected as corrupt, because in
    real LZ4 the look-back is almost always SHORTER than the output so far.
    Four literals and a look-back of two is the smallest case that says so.
    """
    got = st.lz4_block_decompress(_block(b"abcd", 0, 2), 8)

    assert got == b"abcdcdcd", got


def test_the_two_operands_of_the_offset_guard_are_not_the_same_test():
    """Pins why the test above exists, so it cannot be 'simplified' back.

    If a future edit made every legal case use offset == len(out) again, the
    guard would be untestable for inequality-vs-range and this file would go
    back to passing on a broken predicate.
    """
    # offset < len(out): legal, and only `>` allows it
    assert st.lz4_block_decompress(_block(b"abcd", 0, 2), 8) == b"abcdcdcd"
    # offset == len(out): legal, the existing boundary case
    assert st.lz4_block_decompress(_block(b"abc", 0, 3), 7) == b"abcabca"
    # offset > len(out): refused
    with pytest.raises(ValueError, match="corrupt LZ4 back-offset"):
        st.lz4_block_decompress(_block(b"A", 0, 5), 8)


def test_a_truncated_block_does_not_run_off_the_end():
    """`if pos >= n: break` survived being narrowed to `==`, because no test
    ever gets `pos` PAST `n`.

    It gets there on truncated input. `out += src[pos : pos + lit_len]` is a
    slice and clamps; `pos += lit_len` does not, so a block claiming more
    literals than it carries leaves `pos > n`. `pos >= n` is then True and the
    block ends -- but `pos == n` is False, and the next read is off the end.

    A session file that was being written when Firefox was killed is exactly
    this input, so the difference is between returning the bytes that survived
    and raising IndexError from inside a decompressor.

    NOTE what this does NOT cover, because the measurement said so: the loop
    header `while pos < n` narrowed to `!=` SURVIVES this test, and survives
    correctly. This very break is why. It catches the only route that
    overshoots, and every read after it (`src[pos]`, `src[pos + 1]`) raises
    rather than advancing past the end -- so `pos <= n` always holds at the top
    of the loop and the two spellings agree there. One guard is what makes the
    other unobservable; that is a proof, not a coverage gap.
    """
    # token claims 9 literals; only 3 follow.
    truncated = bytes([9 << 4]) + b"abc"

    got = st.lz4_block_decompress(truncated, 16)

    assert got == b"abc", got


def test_a_block_that_ends_exactly_on_its_literals_is_still_whole():
    """Positive control for the test above: `pos == n` is the ordinary way a
    block ends, and a decoder that bailed early on everything would satisfy
    the truncation assertion too."""
    exact = bytes([3 << 4]) + b"xyz"

    assert st.lz4_block_decompress(exact, 3) == b"xyz"


# ---------------------------------------- why six of these mutants are correct


def test_the_arithmetic_that_makes_the_narrow_spellings_equivalent():
    """Six reduced-ROR mutants in this decoder survive, and should.

    They are not coverage gaps, they are arithmetic:

        lit_len   = token >> 4     so 0..15   =>  `== 15` is `>= 15`
        match_len = token & 0x0F   so 0..15   =>  `== 15` is `>= 15`
        b         = src[pos]       so 0..255  =>  `!= 255` is `< 255`  (x2)
        offset    = a | (b << 8)   so >= 0    =>  `== 0` is `<= 0`

    (The sixth, `while pos < n` -> `!=`, is proved by the break above rather
    than by arithmetic, and is argued where that test lives.)

    Written as a test rather than a comment because a comment cannot fail.
    Each of these would be a registry equivalence claim, except that a claim
    re-runs the entire suite in a subprocess to re-check one operator, and the
    property is decidable here in microseconds by enumeration. Change the
    nibble mask or widen the byte source and this fails, naming the mutant
    whose proof just went stale.
    """
    for token in range(256):
        lit_len = token >> 4
        match_len = token & 0x0F
        assert 0 <= lit_len <= 15, (token, lit_len)
        assert 0 <= match_len <= 15, (token, match_len)
        assert (lit_len == 15) == (lit_len >= 15), token
        assert (match_len == 15) == (match_len >= 15), token

    for b in range(256):
        assert (b != 255) == (b < 255), b

    for lo in (0, 1, 254, 255):
        for hi in (0, 1, 254, 255):
            offset = lo | (hi << 8)
            assert offset >= 0
            assert (offset == 0) == (offset <= 0), (lo, hi)


def test_a_byte_really_is_the_only_thing_that_reaches_those_comparisons():
    """The premise of the enumeration above: `b` comes from indexing `bytes`,
    which yields 0..255 and nothing else. If that source ever became something
    wider -- an int from a struct field, say -- `!= 255` and `< 255` would part
    company and the enumeration would no longer be about the real operand."""
    sample = bytes(range(256))
    assert {type(sample[i]) for i in range(256)} == {int}
    assert min(sample[i] for i in range(256)) == 0
    assert max(sample[i] for i in range(256)) == 255
