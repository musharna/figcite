"""Round-2 review fixes for the pending screen.

Static/textual checks only, matching this repo's existing convention for
`PAGE` (tests/test_webui_page.py): there is no JS runtime in this test
suite, and adding one to exercise the script would be a new test-time
dependency this task's constraints rule out. So these tests pin the
generated markup/script TEXT in ways that fail against the pre-fix source
for the stated reason, and pass once the branch/handler actually exists.
"""

import re

from figcite.webui import PAGE


# ---------------------------------------------------------------------------
# C1 -- an item with doi set and grounded=False must not render as
# "no source inferred". It must be shown as an unconfirmed DOI, distinct
# from both the grounded-DOI branch and the true-negative branch.
# ---------------------------------------------------------------------------


def test_an_ungrounded_doi_gets_its_own_branch_before_the_catch_all():
    assert "else if (item.doi) {" in PAGE, (
        "no branch keyed on item.doi alone (grounded or not) -- an "
        "ungrounded DOI falls through to the final else and renders as "
        "'no source inferred', which is rule 1's defect, inverted"
    )
    # Branch order matters: JS evaluates if/else-if top to bottom, so the
    # grounded-DOI branch must come first, the new ungrounded-DOI branch
    # second, candidates third, and the true-negative catch-all last --
    # otherwise an ungrounded DOI could be shadowed by (or shadow) a
    # candidate list, or by the catch-all itself.
    order = [
        "item.doi && item.grounded",
        "else if (item.doi) {",
        "else if (item.candidates.length)",
        '<p class="nosrc">no source inferred</p>',  # the actual catch-all
        # render, not this test file's own vocabulary echoed back in a comment.
        # The state and its reason were split onto two lines of differing
        # weight; the branch ORDER this test exists to pin is unchanged.
    ]
    positions = [PAGE.index(s) for s in order]
    assert positions == sorted(positions), (
        f"branch order is wrong: expected grounded-doi -> ungrounded-doi -> "
        f"candidates -> catch-all, got positions {positions}"
    )


def test_the_ungrounded_doi_branch_shows_the_doi_and_its_evidence():
    start = PAGE.index("else if (item.doi) {")
    end = PAGE.index("else if (item.candidates.length)")
    branch = PAGE[start:end]
    assert "doi_evidence" in branch, "ungrounded-DOI branch drops the evidence string"
    assert "esc(item.doi)" in branch, "ungrounded-DOI branch never shows the DOI itself"


# ---------------------------------------------------------------------------
# I1 -- a grounded card must be confirmable by clicking Confirm, sending
# {ref} with no selector (service.confirm's own "use this item's grounded
# DOI" path), not by retyping a DOI the server already has.
# ---------------------------------------------------------------------------


def test_a_grounded_card_can_be_confirmed_with_no_selector():
    assert re.search(r"body\s*=\s*\{\s*ref\s*\}", PAGE), (
        "no code path sends {ref} alone -- a grounded card can only be "
        "confirmed by retyping the DOI figcite already grounded"
    )
    assert "canConfirm" in PAGE, (
        "the Confirm button's initial disabled state must be computed from "
        "whether the card is already confirmable (grounded, or a "
        "pre-filled DOI), not hardcoded disabled"
    )


# ---------------------------------------------------------------------------
# I2 -- picking a candidate must clear a typed DOI and vice versa, so the
# card never visibly shows one decision while the API call sends another.
# ---------------------------------------------------------------------------


def test_picking_a_radio_clears_the_typed_doi_and_vice_versa():
    assert 'doi-input").value = ""' in PAGE, (
        "no handler clears the typed DOI box when a candidate is picked"
    )
    assert "picked.checked = false" in PAGE, (
        "no handler clears a checked radio when the DOI box is typed into"
    )


# ---------------------------------------------------------------------------
# I3 -- "This is my own work" always 400s on a filed capture
# (service._confirm_filed only accepts doi=). Must not render there.
# ---------------------------------------------------------------------------


def test_own_work_button_is_hidden_on_filed_captures():
    assert 'item.kind !== "filed"' in PAGE, (
        "'This is my own work' renders unconditionally -- it always 400s "
        "on a filed:<sha256> item, whose only resolution path is doi="
    )


# ---------------------------------------------------------------------------
# I4 -- a failed /api/pending fetch must render the real error, not leave
# the section blank forever (indistinguishable from "nothing pending").
# ---------------------------------------------------------------------------


def test_loadpending_has_a_catch_that_renders_the_real_error():
    start = PAGE.index("async function loadPending")
    end = PAGE.index("function card(item)")
    body = PAGE[start:end]
    assert "catch" in body, "loadPending has no failure path"
    assert "e.message" in body or "String(e)" in body, (
        "loadPending's catch must render the actual error, not a generic message"
    )
    assert '"nothing pending' not in body[body.index("catch") :], (
        "the catch branch must not reuse the empty-state message"
    )


# ---------------------------------------------------------------------------
# I5 -- ref must never be interpolated into a single-quoted JS string
# literal inside an HTML attribute (the escaper doesn't cover ' or `).
# ---------------------------------------------------------------------------


def test_ref_is_never_interpolated_into_an_inline_js_string_literal():
    assert not re.search(r"on\w+=\"[a-zA-Z_]+\('\$\{esc\(item\.ref\)\}", PAGE), (
        "a ref is still being built into a single-quoted JS string literal "
        "inside an onclick/oninput/onchange attribute"
    )
    # No id should be built from ref either -- that was the mechanism that
    # required CSS.escape() at every call site; dataset/delegation removes
    # the need for it entirely.
    assert 'id="ok-' not in PAGE
    assert 'id="doi-' not in PAGE


# ---------------------------------------------------------------------------
# I7 -- --line (card/nav/pill borders) must clear WCAG 1.4.11 non-text
# contrast (3:1) against --bg, in both color schemes. color-scheme stays.
# ---------------------------------------------------------------------------


def _expand_hex(h):
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return h


def _luminance(hex_color):
    h = _expand_hex(hex_color)
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def chan(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = chan(r), chan(g), chan(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(hex_a, hex_b):
    la, lb = _luminance(hex_a), _luminance(hex_b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def test_line_color_clears_3to1_nontext_contrast_light_and_dark():
    root_block = re.search(r":root\s*\{([^}]*)\}", PAGE).group(1)
    bg_light = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,6})", root_block).group(1)
    line_light = re.search(r"--line:\s*(#[0-9a-fA-F]{3,6})", root_block).group(1)

    dark_block = re.search(
        r"prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}", PAGE
    ).group(1)
    bg_dark = re.search(r"--bg:\s*(#[0-9a-fA-F]{3,6})", dark_block).group(1)
    line_dark = re.search(r"--line:\s*(#[0-9a-fA-F]{3,6})", dark_block).group(1)

    ratio_light = _contrast(bg_light, line_light)
    ratio_dark = _contrast(bg_dark, line_dark)
    assert ratio_light >= 3.0, f"light --line contrast {ratio_light:.2f}:1 < 3:1"
    assert ratio_dark >= 3.0, f"dark --line contrast {ratio_dark:.2f}:1 < 3:1"


def test_color_scheme_light_dark_is_still_declared():
    assert "color-scheme: light dark" in PAGE, (
        "buttons and the DOI input carry no CSS of their own -- their "
        "dark-mode legibility depends entirely on this"
    )


# ---------------------------------------------------------------------------
# M2 / M3 / M6, folded in.
# ---------------------------------------------------------------------------


def test_the_note_field_is_rendered():
    assert "item.note" in PAGE, (
        "PendingItem.note ('why is this unresolved') is serialized but "
        "never shown -- rule 3's own hide-the-reason failure mode"
    )


def test_card_info_column_can_wrap_a_long_title_instead_of_overflowing():
    assert "min-width:0" in PAGE


def test_post_checks_response_ok_before_parsing_json():
    start = PAGE.index("async function post(")
    end = PAGE.index("async function confirmRef")
    body = PAGE[start:end]
    ok_check = re.search(r"if\s*\(\s*!r\.ok\s*\)", body)
    assert ok_check, "post() never checks r.ok at all"
    first_json_call = body.index("r.json()")
    assert ok_check.start() < first_json_call, (
        "r.ok must be checked BEFORE r.json() is awaited -- send_error() "
        "emits an HTML body that r.json() cannot parse, so parsing it "
        "unconditionally first throws before the failure is ever reported"
    )
