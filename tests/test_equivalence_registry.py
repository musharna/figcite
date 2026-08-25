"""Equivalence claims, as assertions instead of prose.

A mutation survivor gets dealt with two ways: a test that kills it, or a proof
that it is unobservable. The second was being recorded in a form that cannot
fail, and it failed silently.

The audit memo listed `k != "record"` among the mutants proved equivalent by
construction. Commit c5f1ba7 -- INSIDE the same batch that memo describes --
had already added the two tests that kill it. Nothing noticed, because a prose
proof has no link to the suite it is a claim about. The error surfaced only
when the claim was sent to an outside reviewer, which is not a mechanism.

So the claims live here and are checked by running them.

## What a green run here does and does not mean

It does NOT mean a claim is true. Equivalence is established by the
construction argument recorded in `Claim.proof`; what this file provides is a
STALE-PROOF DETECTOR. Green means the suite has not falsified the proof. Red
means it has, and the proof is obsolete -- which is good news, and the fix is
to delete the claim, never to weaken the test that killed it.

## Three outcomes, not two

The first version of this file returned a bool: exit code 0 meant survived,
anything else meant killed. That collapses three distinguishable states --

    SURVIVED        nothing in the suite kills this mutant
    KILLED          a specific test kills it, and that has been attributed
    HARNESS_ERROR   the run did not produce evidence either way

-- into two, which is precisely the failure figcite exists to prevent: an
outage folded into an absence. A collection error, a timeout, or a usage
mistake is not a test disagreeing with a proof. It stops certification, but it
must never satisfy the kill control, and it must never be reported as "a test
now kills this mutant".

## Attribution, because an unattributed kill is not evidence

A red suite names the mutant only if the redness came from the mutant. So when
a claim's run fails, the failing nodeids are re-run twice: alone against a
clean round-tripped tree (they must PASS) and alone against the mutated tree
(they must FAIL). Anything else is HARNESS_ERROR -- including a failure that
only reproduces inside the full serial prefix, which is an order-dependent
test sequence rather than an attributable kill.

Failing nodeids are collected by an in-process plugin, not by parsing terminal
prose or guessing them from JUnit's dotted `classname`, which carries no file
attribute.

## One canonical execution profile

The profile is fixed: a pinned worker count, no plugin autoloading, no
randomised order.

Runs were forced SERIAL for most of this file's life, and the reason is kept
because it was retired rather than overruled. conftest built one
`FIGCITE_HOME` at import and conftest is imported once per PROCESS, so under
xdist each worker accumulated its own store instead of sharing one. This
suite had real coupling through that store -- two tests written during the
audit passed alone and failed only in the full suite, because both matchers
fall back to `store.find_similar` over the global store whatever manifest they
are handed. A mutant killed only via accumulated state could be scheduled into
a worker that never accumulated it, and the registry would certify a claim
that is false serially.

conftest now gives every TEST its own store, corpus and library, so there is
no accumulation to partition and that argument no longer has a premise.
Parallelism is used both inside a run and across runs.

Note what did NOT justify the change: all 12 claims plus the control gave
identical verdicts serially and under `-n 8`, 0 disagreements -- and it was 0
disagreements BEFORE the isolation too, which was correctly refused as a
reason at the time. Agreement across a fixed set of mutants shows those
mutants agreed in that run, never that the topology is sound. The mechanism
going away is the reason; the measurement is corroboration.

Scope, narrower than "nothing is shared": the store axis is gone by
construction. `crossref.MAILTO` is a constant and `crossref._last_call` /
`pmc._last_call` are rate-limiter timestamps no test reads across tests.
Those are the only per-process leftovers.

The profile also sanitises `PYTEST_ADDOPTS`, `PYTHONPATH` and `FIGCITE_*`,
pins `PYTHONHASHSEED`, and disables plugin autoloading so an unrelated
installed plugin cannot reorder collection. That removes the differences this
file can control; it does NOT make a verdict machine-independent, because
Python version, optional dependencies and environment-dependent skips still
vary and nothing here pins those.

The gate deliberately runs a DIFFERENT profile: it may randomise order, which
is how isolation bugs get found. These two answer different questions -- the
gate is a stress diagnostic, this is a qualification oracle that has to give
the same answer twice -- so the profiles differing is a design choice, not
drift.

Scope is stated rather than implied: survival is measured under
`-m "not live"`, the selection the push gate uses. A claim here means "no test
in the unit suite kills this mutant", not "no test anywhere".
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SELF = Path(__file__).name
TIMEOUT = 1800
# Fixed, not `auto`, so a verdict does not depend on the core count of the
# machine that produced it. Four inside a run x four runs at a time = 16.
INNER_WORKERS = 4


class Outcome(str, Enum):
    SURVIVED = "SURVIVED"
    KILLED = "KILLED"
    HARNESS_ERROR = "HARNESS_ERROR"


# --------------------------------------------------------------- the claims


@dataclass(frozen=True)
class Mutation:
    """One mutant, anchored on the AST rather than on a line number.

    Line numbers drift with every edit above them, and a registry that
    silently mutated the wrong line would report a meaningless verdict. A
    target is named by its enclosing top-level function plus the exact
    `ast.unparse` text of the node, with the match count pinned. If the code
    changes shape the count stops matching and this fails LOUDLY rather than
    mutating something else.
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

# --- broadened exception handlers, from the exception-routing tier ----------
#
# Each of these survived `except X` -> `except Exception`. They are here rather
# than in a memo because that is the lesson of this file: a prose equivalence
# claim has no link to the suite and goes stale silently.
#
# The shared argument is FILTER vs SWALLOW. A handler that RE-RAISES what it
# does not recognise passes the unexpected through either way, so widening
# what it catches changes nothing. A handler that RETURNS or PRINTS is a
# swallow, and widening one launders a defect into a diagnosis -- those are
# not here, they got tests (tests/test_error_routing.py).
_BROADENINGS: tuple[tuple[str, str, str, str], ...] = (
    (
        "zotero.py",
        "configured",
        "NotConfigured",
        "The try is `credentials()`, which calls `_from_file()` -- itself "
        "wrapped in `except Exception: raise NotConfigured(...)` -- then only "
        "os.environ.get and .lower() on a str, then raises NotConfigured. "
        "NotConfigured is the only thing that can leave it.",
    ),
    (
        "crossref.py",
        "throttled_get",
        "ValueError",
        "`float(r.headers.get('retry-after', 2))`. The .get supplies a default "
        "so the argument is never None, and float() on a str or int raises "
        "only ValueError.",
    ),
    (
        "corpus.py",
        "can_compare_dhash",
        "ValueError",
        "`if not dh: return False` precedes the try, so int(None, 16) is "
        "unreachable and int(str, 16) raises only ValueError. The guard above "
        "the try is what makes this a code-path argument rather than a guess.",
    ),
    (
        "service.py",
        "_library_path_for",
        "(OSError, ValueError)",
        "`json.loads(sidecar.read_text(...))`. read_text raises OSError or "
        "UnicodeDecodeError, which subclasses ValueError; json.loads raises "
        "JSONDecodeError, which also subclasses ValueError. The caught set IS "
        "the reachable set.",
    ),
    # NOT here on purpose: `match.py:_target_features` `cv2.error ->
    # Exception` also survived, but `orb` is an INJECTED parameter, so the
    # argument that only cv2 can fail there holds for production and not for
    # the code. That is an input-quantified claim wearing a proof's clothes --
    # the exact shape this file was built after getting wrong twice -- so it
    # stays an open survivor rather than a certified equivalence.
    (
        "match.py",
        "_decode",
        "cv2.error",
        "`cv2.imdecode(np.frombuffer(data, np.uint8), ...)`. np.frombuffer on "
        "bytes with a one-byte dtype cannot raise for any length, including "
        "zero, so cv2 is the only source of failure.",
    ),
    (
        "pmc.py",
        "_get",
        "requests.exceptions.ConnectionError",
        "The handler raises DnsUnreachable for a DNS-shaped failure and then "
        "ends in a bare `raise`. Anything it does not recognise is re-raised "
        "unchanged, so it is a FILTER: widening what reaches it changes what "
        "is inspected, not what escapes.",
    ),
    (
        "pmc.py",
        "_get_with_retry",
        "requests.exceptions.HTTPError",
        "`code = getattr(e.response, 'status_code', None)` then `if code not "
        "in RETRY_CODES or attempt == MAX_ATTEMPTS: raise`. Anything without a "
        "usable .response yields code=None, which is not in RETRY_CODES, so it "
        "re-raises. A FILTER again.",
    ),
)

CLAIMS = CLAIMS + tuple(
    Claim(
        claim_id=f"{mod.removesuffix('.py')}-{fn}-broadening",
        mutation=Mutation(
            module=mod,
            function=fn,
            target=caught,
            occurrence=0,
            expect_occurrences=1,
            op="broaden_except",
        ),
        proof=proof,
    )
    for mod, fn, caught, proof in _BROADENINGS
)

# Every module a claim rewrites. Each needs its OWN round-trip baseline: a
# lossy rewrite of zotero.py is not cleared by deck.py round-tripping safely.
MODULES: tuple[str, ...] = tuple(sorted({c.mutation.module for c in CLAIMS}))

# The control. This mutation IS killed, by a test named here rather than by
# "something went red somewhere".
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
KILL_CONTROL_NODE = (
    "tests/test_deck_guards.py::test_the_pptx_manifest_json_carries_the_record_as_data"
)


# ------------------------------------------------------------- the mutator


def _enclosing(tree: ast.Module, name: str) -> ast.AST:
    """The single top-level function called `name`.

    `ast.walk` would return the first match anywhere, so a nested helper or a
    method that later borrowed the name could silently move the target. These
    claims all live in top-level functions, so that is what is required, and
    ambiguity is an error rather than a coin flip.
    """
    hits = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name
    ]
    assert len(hits) == 1, (
        f"expected exactly one top-level function named {name!r}, found {len(hits)}"
    )
    return hits[0]


def _mutate_source(src: str, m: Mutation) -> str:
    tree = ast.parse(src)
    fn = _enclosing(tree, m.function)

    if m.op == "broaden_except":
        # An ExceptHandler unparses to the whole `except X:` plus its body, so
        # it is matched on the caught TYPE alone -- which is the thing the
        # claim is about, and is stable when the body is edited.
        hits = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.ExceptHandler)
            and n.type is not None
            and ast.unparse(n.type) == m.target
        ]
    else:
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
    elif m.op == "broaden_except":
        assert isinstance(node, ast.ExceptHandler)
        node.type = ast.Name(id="Exception", ctx=ast.Load())
    else:  # pragma: no cover - a typo in the registry, not a runtime path
        raise AssertionError(f"unknown op {m.op!r}")

    return ast.unparse(ast.fix_missing_locations(tree))


def _roundtrip(src: str) -> str:
    return ast.unparse(ast.parse(src))


# --------------------------------------------------------------- the runner

# Collected in-process. Reconstructing nodeids from JUnit's dotted `classname`
# is a guess (it carries no file attribute), and parsing the terminal summary
# is parsing prose. A plugin reports exactly what failed.
_PLUGIN = """
import json, os
_failed = []
_errored = []

def pytest_runtest_logreport(report):
    if report.failed:
        # A "call" failure is a test disagreeing with the code. A setup or
        # teardown failure is pytest's ERROR: the test never got to run, so it
        # is infrastructure, not evidence. Kept apart deliberately -- see the
        # runner, which will not read the second as a kill.
        (_failed if report.when == "call" else _errored).append(report.nodeid)

def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["FIGCITE_REGISTRY_REPORT"], "w", encoding="utf-8") as fh:
        json.dump(
            {
                "failed": sorted(set(_failed)),
                "errored": sorted(set(_errored)),
                "exitstatus": int(exitstatus),
            },
            fh,
        )
"""


@dataclass
class Run:
    outcome: Outcome
    exit_code: int | None
    failed: list[str] = field(default_factory=list)
    detail: str = ""


def _worktree(dest: Path, module: str | None, source: str | None) -> Path:
    """A private copy of the repo, one per run, deliberately.

    An earlier throwaway harness in this audit used one fixed scratch
    directory, and two runs that overlapped each wiped the tree the other was
    mutating -- producing a mutant reported as surviving that dies immediately
    when run alone. A mutation harness is an instrument; it gets the isolation
    demanded of the tests it measures.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
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
    (dest / "_registry_plugin.py").write_text(_PLUGIN, encoding="utf-8")
    if module is not None:
        assert source is not None
        (dest / "figcite" / module).write_text(source, encoding="utf-8")
    return dest


def _run(tree: Path, selection: list[str]) -> Run:
    """One pytest run under the canonical profile.

    These runs used to be forced serial, and the reason is worth keeping
    because it is now GONE rather than overruled. conftest built one
    `FIGCITE_HOME` at import, conftest is imported once per PROCESS, so under
    xdist each worker accumulated its own store instead of sharing one. A
    mutant killed only via accumulated cross-test state could then be
    scheduled into a worker that never accumulated it, and the registry would
    certify a claim that is false serially.

    conftest now gives every TEST its own store, corpus and library. There is
    no accumulation left to partition, so the premise of that argument is
    false, not merely unlikely -- which is the difference between retiring a
    constraint and ignoring one.

    Measured after the change, all 12 claims plus the control, serial against
    `-n 8`: 0 disagreements. Recorded as corroboration, NOT as the reason. It
    was 0 disagreements before the isolation too, and that was correctly
    refused as a justification: agreement across a fixed set shows those
    mutants agreed in that run, never that the topology is sound.

    Scope, stated because it is narrower than "nothing is shared": the store
    axis is gone by construction. `crossref.MAILTO` is a constant, and
    `crossref._last_call` / `pmc._last_call` are rate-limiter timestamps that
    no test reads across tests. Those are the only per-process leftovers.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("FIGCITE_") and k not in ("PYTEST_ADDOPTS", "PYTHONPATH")
    }
    report = tree / "registry-report.json"
    env["FIGCITE_REGISTRY_REPORT"] = str(report)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    # Disabling `randomly` and `cacheprovider` by name only covers the plugins
    # known about today; any other installed third-party plugin could still
    # change collection or execution order. The oracle loads nothing it did
    # not ask for, and asks for the reporting plugin explicitly below.
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--tb=no",
        "-p",
        "no:randomly",
        "-p",
        "no:cacheprovider",
        "-p",
        "_registry_plugin",
        *selection,
    ]
    try:  # xdist is not a declared dependency of this project
        import xdist  # noqa: F401

        # A FIXED count, never `auto`: the same commit should not be certified
        # under four workers here and sixteen elsewhere. Four inside each run,
        # four runs at a time, is one worker per core on this machine.
        cmd += ["-p", "xdist", "-n", str(INNER_WORKERS)]
    except ImportError:
        pass
    # Its own process group, so a timeout can reap DESCENDANTS too. pytest
    # here can spawn children (the suite shells out), and `subprocess.run`'s
    # timeout kills only the direct child -- leaving orphans holding the
    # scratch tree and contaminating later runs of this same registry.
    proc = subprocess.Popen(
        cmd,
        cwd=tree,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            # `start_new_session=True` makes the child the group leader, so
            # its pid IS the pgid. Looking it up with `os.getpgid` instead can
            # fail once the pytest leader has exited while a descendant still
            # holds the pipes open -- exactly the case a timeout implies --
            # and would then leave the surviving group unkilled.
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):  # already gone
            pass
        proc.communicate()
        return Run(Outcome.HARNESS_ERROR, None, [], f"timed out after {TIMEOUT}s")

    tail = "\n".join((out + err).strip().splitlines()[-12:])
    failed: list[str] = []
    errored: list[str] = []
    if report.exists():
        try:
            data = json.loads(report.read_text(encoding="utf-8"))
            failed = data["failed"]
            errored = data.get("errored", [])
        except (ValueError, KeyError):
            failed, errored = [], []

    # pytest: 0 all passed, 1 tests failed, 2 interrupted, 3 internal error,
    # 4 usage error, 5 nothing collected. Only 0 and 1 are evidence.
    if proc.returncode == 0:
        return Run(Outcome.SURVIVED, 0, [], tail)

    # An ERROR is a test that never ran -- a fixture blew up, a browser could
    # not reach the page, a port was taken. That is the environment
    # misbehaving, not a test disagreeing with the code, and reading it as a
    # kill is the "outage folded into an absence" mistake this repo is about.
    #
    # Observed, which is why this is here: running the mutation tier alongside
    # the push gate put enough load on the machine that a Playwright test
    # errored with net::ERR_NETWORK_CHANGED. The registry called that a KILL,
    # the round-trip control reported pdfdeck.py's transform as lossy, and a
    # push was blocked over a browser hiccup. The test passes alone.
    if errored:
        return Run(
            Outcome.HARNESS_ERROR,
            proc.returncode,
            failed,
            f"{len(errored)} test(s) ERRORED rather than failed, so this run is "
            f"not evidence either way -- first: {errored[0]}\n{tail}",
        )
    if proc.returncode == 1 and failed:
        return Run(Outcome.KILLED, 1, failed, tail)
    return Run(
        Outcome.HARNESS_ERROR,
        proc.returncode,
        failed,
        f"exit {proc.returncode} produced no attributable failure\n{tail}",
    )


def _selection() -> list[str]:
    return ["-m", "not live"]


def _attribute(root: Path, claim: Claim, failed: list[str]) -> Run:
    """A red suite names the mutant only if the redness came from the mutant.

    Each failing node is re-run ONE AT A TIME, each in its own fresh pair of
    trees: alone on a clean round-tripped tree it must pass, alone on the
    mutant it must fail. Only nodes that clear both are reported as killers.

    One at a time matters, and the first version got it wrong by re-running
    the whole failing set together. If the suite reports A and B, A can seed
    the state that makes B fail; the reduced A+B sequence still reproduces,
    and every node in it gets reported as a verified killer even though
    neither kills the mutant on its own. That is the same order-dependence
    this function exists to screen out, surviving inside the screen.

    A failure that reproduces only in a longer sequence is an order-dependent
    test sequence, not an attributable kill: the fixture should install the
    state it needs explicitly before it counts as evidence.
    """
    src = (REPO / "figcite" / claim.mutation.module).read_text(encoding="utf-8")
    mutated = _mutate_source(src, claim.mutation)
    verified: list[str] = []
    notes: list[str] = []

    for i, node in enumerate(failed):
        clean = _worktree(
            root / f"{claim.claim_id}-attr-{i}-clean",
            claim.mutation.module,
            _roundtrip(src),
        )
        mutant = _worktree(
            root / f"{claim.claim_id}-attr-{i}-mutant", claim.mutation.module, mutated
        )
        on_clean = _run(clean, [node])
        if on_clean.outcome is not Outcome.SURVIVED:
            notes.append(f"{node}: also fails on a CLEAN tree, so it is not the mutant")
            continue
        on_mutant = _run(mutant, [node])
        if on_mutant.outcome is not Outcome.KILLED:
            notes.append(f"{node}: PASSES alone on the mutant (order-dependent)")
            continue
        verified.append(node)

    if verified:
        # One independently verified killer falsifies the proof, whatever else
        # in the suite happened to be red at the same time.
        return Run(Outcome.KILLED, 1, verified, "\n".join(notes))
    return Run(
        Outcome.HARNESS_ERROR,
        1,
        [],
        "the suite went red, but NO failing test kills the mutant on its own. "
        "That is an order-dependent sequence or unrelated breakage, not an "
        "attributable kill -- make the required state explicit in the fixture "
        "before counting it as evidence.\n" + "\n".join(notes),
    )


# ------------------------------------------------------------- the workload


def _roundtrip_job(root: Path, module: str) -> Run:
    src = (REPO / "figcite" / module).read_text(encoding="utf-8")
    tree = _worktree(root / f"roundtrip-{module}", module, _roundtrip(src))
    return _run(tree, _selection())


def _claim_job(root: Path, claim: Claim) -> Run:
    src = (REPO / "figcite" / claim.mutation.module).read_text(encoding="utf-8")
    mutated = _mutate_source(src, claim.mutation)
    assert mutated != _roundtrip(src), "the mutation changed nothing"
    tree = _worktree(root / claim.claim_id, claim.mutation.module, mutated)
    run = _run(tree, _selection())
    if run.outcome is Outcome.KILLED:
        return _attribute(root, claim, run.failed)
    return run


def _kill_control_job(root: Path) -> tuple[Run, Run]:
    """Run the ONE node that is supposed to do the killing, both ways.

    A full-suite `-x` run cannot tell "killed by the oracle I named" from
    "something unrelated went red first". This repo already learned that a
    kill you cannot attribute is not evidence; the control has to name its
    killer and drive it directly.
    """
    src = (REPO / "figcite" / KILL_CONTROL.mutation.module).read_text(encoding="utf-8")
    clean = _worktree(
        root / "control-clean", KILL_CONTROL.mutation.module, _roundtrip(src)
    )
    mutant = _worktree(
        root / "control-mutant",
        KILL_CONTROL.mutation.module,
        _mutate_source(src, KILL_CONTROL.mutation),
    )
    return _run(clean, [KILL_CONTROL_NODE]), _run(mutant, [KILL_CONTROL_NODE])


@pytest.fixture(scope="session")
def verdicts(tmp_path_factory) -> dict:
    """Every run, computed once.

    Runs are serial INTERNALLY -- that is the soundness requirement -- but
    they are independent whole-suite processes sharing no state, so they are
    executed concurrently with each other. That is a different thing from
    xdist, which splits ONE suite's state across workers.
    """
    root = tmp_path_factory.mktemp("equivalence-registry")
    out: dict = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            ("roundtrip", m): pool.submit(_roundtrip_job, root, m) for m in MODULES
        }
        futures.update(
            {("claim", c.claim_id): pool.submit(_claim_job, root, c) for c in CLAIMS}
        )
        futures[("control", "kill")] = pool.submit(_kill_control_job, root)
        for key, fut in futures.items():
            try:
                out[key] = fut.result()
            except Exception as exc:  # a broken harness is not a verdict
                out[key] = Run(
                    Outcome.HARNESS_ERROR, None, [], f"{type(exc).__name__}: {exc}"
                )
    return out


# ----------------------------------------------------------------- the tests


@pytest.mark.slow
@pytest.mark.parametrize("module", MODULES)
def test_the_round_trip_alone_changes_nothing(module, verdicts):
    """Control for the instrument, once per module a claim rewrites.

    Every claim is measured on a module that has been round-tripped through
    `ast.unparse`. If that transform were lossy the suite would fail for
    reasons having nothing to do with any mutant, and every claim in that
    module would read as killed.
    """
    run = verdicts[("roundtrip", module)]
    assert run.outcome is Outcome.SURVIVED, (
        f"unparsing figcite/{module} with NO mutation applied changed the "
        f"suite's outcome ({run.outcome.value}), so no verdict for that "
        f"module is trustworthy:\n{run.detail}"
    )


@pytest.mark.slow
@pytest.mark.parametrize("claim", CLAIMS, ids=lambda c: c.claim_id)
def test_each_claim_still_holds(claim, verdicts):
    """The mutant must still survive. If it does not, the proof is obsolete."""
    run = verdicts[("claim", claim.claim_id)]

    if run.outcome is Outcome.HARNESS_ERROR:
        pytest.fail(
            f"claim {claim.claim_id!r} COULD NOT BE CERTIFIED -- this is not a "
            f"statement about the proof, the run produced no evidence either "
            f"way:\n{run.detail}"
        )

    assert run.outcome is Outcome.SURVIVED, (
        f"EQUIVALENCE CLAIM {claim.claim_id!r} HAS BEEN FALSIFIED.\n\n"
        f"The recorded proof was:\n  {claim.proof}\n\n"
        f"These tests kill the mutant, and were verified to pass on a clean "
        f"round-tripped tree and fail alone on the mutant:\n  "
        + "\n  ".join(run.failed)
        + "\n\nThe suite got stronger and the proof is obsolete. Delete the "
        "claim from CLAIMS -- do not weaken the test that killed it."
    )


@pytest.mark.slow
def test_the_registry_can_detect_a_kill(verdicts):
    """The control that makes this file evidence rather than decoration.

    Without it, a runner pointed at the wrong directory, or one swallowing its
    exit code, would report every claim as holding forever -- a green result
    from a harness that cannot go red. The named oracle must PASS on a clean
    round-tripped tree and FAIL on the mutant, so the redness is attributable
    to the mutation and not to anything else in the suite.
    """
    on_clean, on_mutant = verdicts[("control", "kill")]

    assert on_clean.outcome is Outcome.SURVIVED, (
        f"the control's oracle {KILL_CONTROL_NODE} does not pass on a clean "
        f"tree, so its failure on the mutant would prove nothing "
        f"({on_clean.outcome.value}):\n{on_clean.detail}"
    )
    assert on_mutant.outcome is Outcome.KILLED, (
        f"the registry did not detect a KNOWN-KILLED mutant via its named "
        f"oracle ({on_mutant.outcome.value}), so it cannot report a kill and "
        f"no claim above means anything:\n{on_mutant.detail}"
    )
    assert KILL_CONTROL_NODE in on_mutant.failed, (
        f"the mutant run went red, but not at {KILL_CONTROL_NODE}. An "
        f"unattributed kill is not evidence. Failed: {on_mutant.failed}"
    )
