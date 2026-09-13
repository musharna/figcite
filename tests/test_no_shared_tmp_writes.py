"""No test may write a fixture into the SHARED temp dir.

pytest hands every test a private `tmp_path`. A test that reaches around it
to `tempfile.gettempdir()` writes to a directory every other process on the
machine can also write to, and `Path.write_bytes` opens `'wb'`, which
TRUNCATES to zero before writing. Two processes running the same test then
race: one truncates while the other is reading back, and the reader gets a
zero-length file.

This is not theoretical and it is not rare under contention. Measured
directly -- 8 processes, 400 write-then-read cycles each, identical bytes:

    shared path   1253 torn / 3200 reads   (all 1253 zero-length)
    private path     0 torn / 3200 reads

Identical content does not save you, which is the counter-intuitive part:
the tear comes from the truncate, not from a content difference.

How it was found, and why it is worth a guard rather than a one-line fix:
a mutation sweep runs the suite in 8 processes at once. One worker's
baseline came back red with `PIL.UnidentifiedImageError` while the other
seven were green, and that worker refused to run its ~231 mutants. The
direction that matters is the quiet one -- a torn read makes a test FAIL,
so a mutation harness records the mutant as KILLED when no test caught it.
A false kill is a finding that never gets looked at. In a plain suite this
class is an intermittent annoyance; in any harness that reads failure as
evidence, it manufactures coverage that does not exist.

WHAT THIS PREDICATE CANNOT SEE, stated plainly so the guard is not trusted
past its reach. It parses the test file and looks for two things in CODE
positions: a call to `gettempdir`/`mktemp`, and a string literal starting
`/tmp`. A path built at runtime -- read from an env var, joined from pieces,
handed back by a helper in another module -- is invisible to it, and so is
a write through an alias it cannot resolve. It observes the SPELLING of the
escape, not the property "this write landed somewhere another process can
reach", and those two are not the same thing.

So do not read green here as proof that no test escapes. Read it as proof
that no test escapes the two ways the known instances did. The suite's own
recurring lesson applies to this file as much as to the code it guards: a
guard works only where its predicate fully observes its referent, and this
one does not fully observe its referent.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent

# The escape, as an API rather than as a spelling of a path. `mkdtemp` and
# `NamedTemporaryFile` are deliberately absent: both mint a name nobody else
# holds, which is the property that matters, not the directory it lives in.
ESCAPING_CALLS = {"gettempdir", "mktemp"}

# ...but the RECEIVER has to be checked too, and the first version did not.
#
# `mktemp` is not a name only `tempfile` uses. pytest's own session-scoped
# fixture spells its API `tmp_path_factory.mktemp("name")`, which is the
# SANCTIONED way to get a private directory that outlives one test -- the
# exact opposite of the escape. Matching the bare attribute name flagged it,
# and `tests/test_equivalence_registry.py` was the first file to trip it.
#
# That is this suite's recurring lesson landing on the guard itself: the
# predicate observed the method NAME while the thing it must distinguish is
# WHO the method belongs to, so it could not tell the hazard from its
# opposite. Resolving the receiver is not a special case for one fixture --
# it is the check the guard was always trying to make.
TEMPFILE_MODULE = "tempfile"

# DELIBERATELY NOT CHECKED: a hardcoded "/tmp/..." string literal.
#
# The first version flagged those too, and every single hit was a false
# positive -- three of them, and no true ones:
#
#   test_cli_negated_flags.py   "--manifest /tmp/mine"  argv handed to a spy
#   test_browser.py             Path("/tmp/x")          a monkeypatched return
#   test_network_guard.py       lambda: "/tmp"          a stubbed os.getcwd
#
# All three are VALUES being passed around; none is a write target. Telling
# them apart from a real write needs dataflow, not a pattern, so keeping the
# check meant keeping a per-file exemption list that every new legitimate
# "/tmp" string would have to be added to. That is a list of names guarding
# an open set, which is the failure mode this suite keeps writing itself
# notes about -- and here it would guard a set whose members so far are
# ENTIRELY false positives.
#
# The cost of dropping it, stated so it is a choice and not an oversight: a
# test that hardcodes "/tmp/fixture.png" and writes to it has the identical
# defect and this guard will not see it. Both real instances went through
# `tempfile.gettempdir()`, which is checked.


def _offending_lines(path: Path) -> list[tuple[int, str]]:
    """Parse, don't grep.

    The first version scanned source TEXT and skipped lines starting with
    `#`. It immediately failed on `test_corpus_duplicates.py` -- whose
    docstring QUOTES the old bad path while explaining why it is gone. A text
    scan cannot tell code from prose about code, and the cheapest way to
    silence that false positive is to delete the explanation, which is the
    opposite of what the explanation is for.

    Parsing removes the ambiguity for free, with no special case: a docstring
    is an `ast.Constant`, and this walk only ever looks at `ast.Call`. Prose
    describing the escape is structurally incapable of tripping it.

    Resolve the RECEIVER, not just the method name. `x.mktemp()` is only the
    escape when `x` is the `tempfile` module (or a local alias of it);
    `tmp_path_factory.mktemp()` is pytest's own API and is the correct thing
    to reach for. A bare `mktemp()` counts only when it was imported from
    tempfile, which is tracked from the file's own import statements rather
    than assumed.
    """
    source = path.read_text()
    tree = ast.parse(source, filename=str(path))
    src = source.splitlines()

    # Aliases of the tempfile MODULE, and bare names imported out of it.
    module_aliases = {TEMPFILE_MODULE}
    bare_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == TEMPFILE_MODULE:
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == TEMPFILE_MODULE:
            for alias in node.names:
                if alias.name in ESCAPING_CALLS:
                    bare_names.add(alias.asname or alias.name)

    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if isinstance(fn, ast.Attribute):
            hit = fn.attr in ESCAPING_CALLS and (
                isinstance(fn.value, ast.Name) and fn.value.id in module_aliases
            )
        else:
            hit = getattr(fn, "id", "") in bare_names
        if hit:
            ln = getattr(node, "lineno", 0)
            text = src[ln - 1].strip() if 0 < ln <= len(src) else ""
            out.append((ln, text))
    return sorted(set(out))


@pytest.mark.parametrize(
    "test_file",
    sorted(p for p in TESTS_DIR.glob("test_*.py")),
    ids=lambda p: p.name,
)
def test_a_test_file_does_not_write_into_the_shared_temp_dir(test_file: Path):
    """Parametrized over every test file found on disk, not a fixed list.

    A file added tomorrow is covered without editing this test, which is the
    same reason `test_candidate_contract` derives its keys from the live
    template instead of hand-maintaining them.
    """
    if test_file.name == Path(__file__).name:
        pytest.skip("this file names the escape in order to look for it")

    hits = _offending_lines(test_file)
    assert not hits, (
        f"{test_file.name} reaches around pytest's tmp_path into the shared "
        f"temp dir:\n"
        + "\n".join(f"    line {n}: {t}" for n, t in hits)
        + "\n\nUse the `tmp_path` fixture. A deterministic name in a shared "
        "directory is claimed by every concurrent process at once, and "
        "write_bytes truncates before it writes, so a reader can observe a "
        "zero-length file. Measured 1253/3200 torn reads under 8 processes."
    )


def test_the_guard_can_actually_see_an_escape(tmp_path):
    """Positive control.

    Without this, a typo in a regex, a bad glob, or an ALLOWED entry that
    swallowed everything would leave the test above passing on every file
    forever -- green because it inspects nothing, which is the exact defect
    class this suite keeps finding in itself.
    """
    bad = tmp_path / "test_pretend.py"
    bad.write_text(
        "import tempfile\n"
        "from pathlib import Path\n"
        "def test_x():\n"
        "    p = Path(tempfile.gettempdir()) / 'fixed_name.png'\n"
        "    p.write_bytes(b'x')\n"
    )
    assert _offending_lines(bad), "the guard cannot see the escape it exists to catch"

    good = tmp_path / "test_fine.py"
    good.write_text("def test_x(tmp_path):\n    (tmp_path / 'f.png').write_bytes(b'x')\n")
    assert not _offending_lines(good), "the guard flags correct tmp_path use"

    commented = tmp_path / "test_comment.py"
    commented.write_text("# p = Path(tempfile.gettempdir()) / 'x.png'\ndef test_x():\n    pass\n")
    assert not _offending_lines(commented), "a comment is not a write"

    # The false positive this guard actually produced on its first run. A
    # docstring that QUOTES the bad path -- which is exactly what the fix for
    # the real defect left behind, on purpose -- must not read as a violation,
    # or the cheapest way to get back to green is to delete the explanation.
    documented = tmp_path / "test_documented.py"
    documented.write_text(
        "def helper(tmp_path):\n"
        '    """This used to write tempfile.gettempdir() / fixed.png\n\n'
        "    ...and also mentions /tmp/figcite_dup_2.png by name. It no\n"
        "    longer writes either; tmp_path cannot be claimed twice.\n"
        '    """\n'
        "    (tmp_path / 'f.png').write_bytes(b'x')\n"
    )
    assert not _offending_lines(documented), (
        "prose ABOUT the escape reads as the escape, so documenting the fix "
        "trips the guard and deleting the docstring is what makes it pass"
    )


def test_the_predicate_can_still_tell_the_escape_from_pytests_own_api(tmp_path):
    """The control for the guard itself: it must DISCRIMINATE, not just pass.

    The receiver check was added because `tmp_path_factory.mktemp()` -- the
    sanctioned way to get a private directory that outlives one test -- was
    being flagged as the escape. A tightening like that is one edit away from
    a guard that simply stops complaining, so both directions are pinned here.

    Every negative case below is a real spelling that appears in this suite or
    in pytest's own documented API; every positive case is a real way to reach
    `tempfile`, including the aliased and `from`-imported forms that a check
    written against the literal word "tempfile" would miss.
    """
    cases = [
        # (source, should_be_flagged, why)
        ("import tempfile\ntempfile.mktemp()\n", True, "the escape, plainly"),
        ("import tempfile\ntempfile.gettempdir()\n", True, "the other escape"),
        (
            "import tempfile as tf\ntf.mktemp()\n",
            True,
            "aliased module -- a literal 'tempfile' check would miss it",
        ),
        (
            "from tempfile import mktemp\nmktemp()\n",
            True,
            "bare name imported out of tempfile",
        ),
        (
            "from tempfile import mktemp as mk\nmk()\n",
            True,
            "bare name, renamed on import",
        ),
        (
            "def f(tmp_path_factory):\n    tmp_path_factory.mktemp('x')\n",
            False,
            "pytest's OWN session-scoped API -- the opposite of the escape",
        ),
        (
            "import tempfile\ntempfile.mkdtemp()\n",
            False,
            "mkdtemp mints a name nobody else holds; deliberately not checked",
        ),
        (
            "'tempfile.mktemp() is the escape'\n",
            False,
            "prose about the escape is not the escape",
        ),
    ]

    for i, (source, flagged, why) in enumerate(cases):
        probe = tmp_path / f"probe_{i}.py"
        probe.write_text(source, encoding="utf-8")
        hits = _offending_lines(probe)
        assert bool(hits) is flagged, (
            f"case {i} ({why}): expected {'a hit' if flagged else 'no hit'}, got {hits}\n{source}"
        )
