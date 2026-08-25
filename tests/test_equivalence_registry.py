"""Equivalence claims, as assertions instead of prose.

A mutation survivor can be dealt with two ways: write a test that kills it, or
prove it unobservable and record the proof. This file exists because the second
option was being recorded in a form that cannot fail.

The failure it prevents actually happened. The audit memo listed
`k != "record"` among the mutants proved equivalent by construction. Commit
c5f1ba7 -- INSIDE the same batch that memo describes -- had already added the
two tests that kill it. Nothing noticed, because a prose proof has no link to
the suite it is a claim about. The error surfaced only when the claim was sent
to an outside reviewer, which is not a mechanism.

The point: **an equivalence claim is a falsifiable prediction.** "No test in
this suite kills this mutant" is not a philosophical position, it is a
statement about an executable artifact, and it is either true right now or it
is not. So the claims live here and are checked by running them. When someone
adds a test that kills one, this file goes red and says which claim died --
instead of the claim quietly becoming false and staying on the books.

A claim going red is NOT a bug. It means a proof became obsolete because the
suite got stronger, which is good news. The fix is to delete the claim, not to
delete the test.

## What is checked, and the controls

Three things run, and the two controls matter as much as the claims:

1. `test_the_round_trip_alone_changes_nothing` -- the machinery rewrites a
   module by unparsing its AST. If that transform were itself lossy, every
   claim would look "killed" and the registry would cry wolf. So the transform
   is applied with NO mutation and the suite must stay green.

2. `test_each_claim_still_holds` -- the claims. Each mutant is applied and the
   suite must stay GREEN, i.e. the mutant survives, i.e. the proof still holds.

3. `test_the_registry_can_detect_a_kill` -- the control that makes the whole
   file worth having. `k != "record"` is a mutation this suite DOES kill, and
   the registry must report it killed. Without this, a registry whose runner
   was silently broken -- wrong path, swallowed exit code, a pytest that never
   collected anything -- would report every claim as holding, forever. A green
   result from a harness that cannot go red is not evidence.

## Scope of the claim, stated rather than implied

Survival is measured against `-m "not live"`: the same selection the push gate
uses. Live tests need a Zotero key, Windows, PowerPoint, Ghostscript and the
network, so they cannot run here. A claim in this file therefore means "no
test in the unit suite kills this mutant", not "no test anywhere". That is a
narrower statement than the bare word "equivalent" suggests, which is exactly
why it is written down.

The registry is also excluded from the runs it launches -- it does not kill
mutants, and a registry that ran itself would not terminate.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SELF = Path(__file__).name


# --------------------------------------------------------------- the claims


@dataclass(frozen=True)
class Mutation:
    """One mutant, anchored on the AST rather than on a line number.

    Line numbers drift with every edit above them, and a registry that
    silently mutated the wrong line would report a meaningless verdict. So a
    target is named by its enclosing function plus the exact `ast.unparse`
    text of the node, and `expect_occurrences` pins how many nodes should
    match. If the code changes shape, the count stops matching and this fails
    LOUDLY rather than mutating something else.
    """

    module: str
    function: str
    target: str
    occurrence: int
    expect_occurrences: int
    op: str  # "gt_to_gte" | "bump_last_arg" | "ne_to_eq"


@dataclass(frozen=True)
class Claim:
    claim_id: str
    mutation: Mutation
    proof: str


CLAIMS: tuple[Claim, ...] = (
    Claim(
        claim_id="zotero-exact-count-boundary",
        mutation=Mutation(
            module="zotero.py",
            function="resolve_title",
            target="len(exact) > 1",
            occurrence=0,
            expect_occurrences=1,
            op="gt_to_gte",
        ),
        proof=(
            "Both `len(exact) == 1` branches above it return, so this line is "
            "reachable only when the count is 0 or 2+. Over that domain `> 1` "
            "and `>= 1` agree. NOTE the scope: this certifies the boundary "
            "shift ONLY. `!= 1` is a different mutant and is NOT equivalent -- "
            "at len 0 it calls an empty list ambiguous -- which is pinned by "
            "test_demoted_equivalence_claims.py. The generator that produced "
            "this survivor cannot emit `!= 1`."
        ),
    ),
    *[
        Claim(
            claim_id=f"{mod.removesuffix('.py')}-entry-counts-default-{i}",
            mutation=Mutation(
                module=mod,
                function="apply",
                target="entry_counts.get(i + 1, 1)",
                occurrence=i,
                expect_occurrences=2,
                op="bump_last_arg",
            ),
            proof=(
                "`entries.append` and `entry_counts[n] = ...` are populated "
                "together in one branch, and n is assigned 1..len(entries) in "
                "order, so entry_counts.keys() == {1..len(entries)} and the "
                "default is unreachable for every index enumerate() yields."
            ),
        )
        for mod in ("deck.py", "pdfdeck.py")
        for i in (0, 1)
    ],
)


# The control. This mutation IS killed by the suite; the registry must say so.
KILL_CONTROL = Claim(
    claim_id="control-manifest-record-filter",
    mutation=Mutation(
        module="deck.py",
        function="_write_manifest",
        target="k != 'record'",
        occurrence=0,
        expect_occurrences=1,
        op="ne_to_eq",
    ),
    proof="NOT a claim. Known killed -- proves this file can report a kill.",
)


# ------------------------------------------------------------- the mutator


def _enclosing(tree: ast.Module, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ):
            return node
    raise AssertionError(f"no function named {name!r}")


def _mutate_source(src: str, m: Mutation) -> str:
    tree = ast.parse(src)
    fn = _enclosing(tree, m.function)

    hits = [n for n in ast.walk(fn) if ast.unparse(n) == m.target]
    # ast.walk is breadth-first, not source order. Sort so `occurrence` means
    # what a reader assumes it means.
    hits.sort(key=lambda n: (n.lineno, n.col_offset))

    assert len(hits) == m.expect_occurrences, (
        f"{m.module}:{m.function} has {len(hits)} nodes matching {m.target!r}, "
        f"expected {m.expect_occurrences}. The code changed shape -- re-derive "
        f"this claim rather than adjusting the count."
    )

    node = hits[m.occurrence]
    if m.op == "gt_to_gte":
        assert isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Gt)
        node.ops[0] = ast.GtE()
    elif m.op == "ne_to_eq":
        assert isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.NotEq)
        node.ops[0] = ast.Eq()
    elif m.op == "bump_last_arg":
        assert isinstance(node, ast.Call)
        const = node.args[-1]
        assert isinstance(const, ast.Constant) and isinstance(const.value, int)
        node.args[-1] = ast.Constant(value=const.value + 1)
    else:  # pragma: no cover - a typo in the registry, not a runtime path
        raise AssertionError(f"unknown op {m.op!r}")

    return ast.unparse(ast.fix_missing_locations(tree))


# --------------------------------------------------------------- the runner


def _worktree(dest: Path) -> Path:
    """A private copy of the repo. Per-call, deliberately.

    An earlier version of this audit's throwaway harness used one fixed
    scratch directory, and two runs that overlapped each wiped the tree the
    other was mutating -- producing a mutant reported as surviving that dies
    immediately when run alone. A mutation harness is an instrument; it gets
    the isolation demanded of the tests it measures.
    """
    dest.mkdir(parents=True, exist_ok=True)
    for item in ("figcite", "tests", "pyproject.toml", "pytest.ini"):
        src = REPO / item
        if not src.exists():
            continue
        target = dest / item
        if src.is_dir():
            shutil.copytree(
                src, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
            )
        else:
            shutil.copy2(src, target)
    # The registry does not kill mutants, and running itself would not
    # terminate.
    (dest / "tests" / SELF).unlink(missing_ok=True)
    return dest


def _run_suite(tree: Path, stop_early: bool) -> tuple[bool, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("FIGCITE_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    cmd = [sys.executable, "-m", "pytest", "-q", "-m", "not live", "-p", "no:randomly"]
    try:  # opportunistic only -- xdist is not a dependency of this project
        import xdist  # noqa: F401

        cmd += ["-n", "auto", "-p", "no:cacheprovider"]
    except ImportError:
        pass
    if stop_early:
        cmd.append("-x")
    proc = subprocess.run(
        cmd, cwd=tree, env=env, capture_output=True, text=True, timeout=1800
    )
    tail = "\n".join((proc.stdout + proc.stderr).strip().splitlines()[-12:])
    # Exit 0 = every test passed = nothing killed the mutant. Any other code is
    # treated as a kill, INCLUDING a collection error: a suite that cannot run
    # is not evidence that a mutant survives it.
    return proc.returncode == 0, tail


def _verify(
    claim: Claim, tmp: Path, mutate: bool, stop_early: bool
) -> tuple[bool, str]:
    tree = _worktree(tmp / claim.claim_id)
    path = tree / "figcite" / claim.mutation.module
    src = path.read_text(encoding="utf-8")
    if mutate:
        out = _mutate_source(src, claim.mutation)
        assert out != ast.unparse(ast.parse(src)), "the mutation changed nothing"
    else:
        out = ast.unparse(ast.parse(src))
    path.write_text(out, encoding="utf-8")
    return _run_suite(tree, stop_early)


# ----------------------------------------------------------------- the tests


@pytest.mark.slow
def test_the_round_trip_alone_changes_nothing(tmp_path):
    """Control for the instrument: unparsing a module must not break it.

    Every claim below is measured on a module that has been round-tripped
    through `ast.unparse`. If that transform were lossy, the suite would fail
    for reasons having nothing to do with the mutant and every claim would
    read as killed. This is checked on the module carrying the most claims.
    """
    survived, tail = _verify(CLAIMS[1], tmp_path, mutate=False, stop_early=False)
    assert survived, (
        "unparsing deck.py with NO mutation applied changed the suite's "
        f"outcome, so no verdict below is trustworthy:\n{tail}"
    )


@pytest.mark.slow
@pytest.mark.parametrize("claim", CLAIMS, ids=lambda c: c.claim_id)
def test_each_claim_still_holds(claim, tmp_path):
    """The mutant must still survive. If it does not, the proof is obsolete."""
    survived, tail = _verify(claim, tmp_path, mutate=True, stop_early=False)
    assert survived, (
        f"EQUIVALENCE CLAIM {claim.claim_id!r} IS NO LONGER TRUE.\n\n"
        f"The recorded proof was:\n  {claim.proof}\n\n"
        "A test now kills this mutant, which means the suite got stronger and "
        "the proof is obsolete. Delete the claim from CLAIMS -- do not weaken "
        "the test that killed it.\n\n"
        f"{tail}"
    )


@pytest.mark.slow
def test_the_registry_can_detect_a_kill(tmp_path):
    """The control that makes this file evidence rather than decoration.

    Without it, a runner pointed at the wrong directory, or one whose exit
    code was swallowed, would report every claim as holding forever -- a green
    result from a harness that cannot go red. `k != "record"` is killed by
    tests/test_deck_guards.py and tests/test_pdfdeck_layout.py, so the
    registry must report it killed.
    """
    survived, tail = _verify(KILL_CONTROL, tmp_path, mutate=True, stop_early=True)
    assert not survived, (
        "the registry reported a KNOWN-KILLED mutant as surviving, so it "
        f"cannot detect a kill and no claim above means anything:\n{tail}"
    )
