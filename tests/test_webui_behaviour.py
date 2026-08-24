"""Run the page's own JavaScript and assert what it RENDERS.

`webui.py` is 596 lines of which 99.9% of the characters sit inside one
string literal, so the mutation sweep generated exactly ZERO mutants for it.
That was recorded as a blind spot, and the wording mattered: the harness did
not fail to see the Python: there IS no Python. The file is a docstring and
`PAGE = \"\"\"...\"\"\"`.

Inside that string are 16 named functions, 30 `if` statements, 13
comparisons and 6 ternaries -- a real program, and the half of the tool a
user actually looks at. The existing tests assert SUBSTRINGS of the string
(`"LOOKUP FAILED" in PAGE`), which shows the text exists somewhere but not
which branch produces it, nor that the other branch does not. That is the
same shape as the `inspect.getsource` test this audit found in
`test_autostart.py`.

The script is extracted from the real `PAGE` at test time and executed under
node with a minimal `document` stub -- only one line of the page touches the
DOM at load. So the functions exercised are the shipped ones and cannot
drift from what is served.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from figcite.webui import PAGE

NODE = shutil.which("node") or shutil.which("nodejs")
CHECKS = Path(__file__).parent / "webui_checks.mjs"

# The whole page loads one line of DOM at import: a click delegate on <nav>.
# Everything else this file exercises is pure string-building.
DOM_STUB = """
const __handlers = [];
globalThis.document = {
  querySelector: () => ({
    addEventListener: (t, fn) => __handlers.push([t, fn]),
    setAttribute: () => {},
    hidden: false,
    value: "",
    innerHTML: "",
    dataset: {},
  }),
};
globalThis.window = globalThis;
"""


def _script_from_page() -> str:
    """The <script> body of the page actually shipped."""
    start = PAGE.index("<script")
    body = PAGE.index(">", start) + 1
    end = PAGE.index("</script>", body)
    return PAGE[body:end]


def test_the_page_contains_exactly_one_script_block():
    """The extractor assumes one. If a second is ever added, everything below
    would silently test only the first -- so this fails instead."""
    assert PAGE.count("<script") == 1, (
        f"{PAGE.count('<script')} script blocks; the extractor reads only the first"
    )


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_pages_javascript_behaves(tmp_path):
    """Every check in webui_checks.mjs, against the real shipped script."""
    bundle = tmp_path / "bundle.mjs"
    bundle.write_text(
        DOM_STUB + _script_from_page() + "\n" + CHECKS.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    r = subprocess.run([NODE, str(bundle)], capture_output=True, text=True, timeout=120)

    assert r.returncode == 0, (
        f"the page's own JavaScript failed its behaviour checks:\n"
        f"{r.stdout}\n{r.stderr}"
    )


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_javascript_checks_can_actually_fail(tmp_path):
    """The control, and the reason this file is worth anything.

    A harness that cannot fail reports every page as correct -- which is
    precisely the defect this whole audit exists to find, and it would be
    especially easy to build here, where a `node` invocation that never
    reaches an assertion still exits 0.

    So the shipped script is MUTATED and the checks must go red. The mutation
    is a real one: `badgeFor` returns the reuse verdict for an unrecognised
    value rather than a blank badge, and that fallback was added by review
    fix C2. Removing it is exactly the regression this file must catch.
    """
    script = _script_from_page()
    original = 'return REUSE[r.reuse] || [r.reuse, "warn"];'
    assert original in script, "the mutation target moved; update this control"
    mutated = script.replace(original, 'return REUSE[r.reuse] || ["", ""];')

    bundle = tmp_path / "mutant.mjs"
    bundle.write_text(
        DOM_STUB + mutated + "\n" + CHECKS.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    r = subprocess.run([NODE, str(bundle)], capture_output=True, text=True, timeout=120)

    assert r.returncode != 0, (
        "the checks passed against a page whose unknown-verdict badge was "
        "blanked -- they cannot fail, so they measure nothing"
    )
    assert "unknown verdict" in r.stderr, (
        f"the checks failed, but not for the reason this control names:\n{r.stderr}"
    )
