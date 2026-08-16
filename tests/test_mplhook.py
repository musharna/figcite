"""Auto-registration of figures you generate yourself.

`mplhook.savefig()` has always existed, but it only helps if you remember to
call it. These cover `install()`, which makes the ordinary `fig.savefig("x.png")`
self-register with no call-site change.
"""

from __future__ import annotations

import io
import threading

import pytest

matplotlib = pytest.importorskip("matplotlib")
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from figcite import mplhook, store  # noqa: E402


@pytest.fixture
def fig():
    f, ax = plt.subplots()
    ax.plot([1, 2, 3], [3, 1, 2])
    yield f
    plt.close(f)


@pytest.fixture(autouse=True)
def _always_uninstall():
    """A leaked patch would silently register figures for every later test."""
    yield
    mplhook.uninstall()


def _count() -> int:
    return len(list(store.iter_records()))


# --------------------------------------------------------------- install


def test_plain_savefig_registers_nothing_before_install(fig, tmp_path):
    before = _count()
    fig.savefig(tmp_path / "a.png")
    assert _count() == before


def test_install_makes_plain_savefig_register(fig, tmp_path):
    mplhook.install()
    before = _count()
    fig.savefig(tmp_path / "b.png")
    assert _count() == before + 1


def test_pyplot_savefig_registers_exactly_once(fig, tmp_path):
    """plt.savefig is a thin wrapper that calls fig.savefig, so patching both
    would file every pyplot figure twice."""
    mplhook.install()
    before = _count()
    plt.savefig(tmp_path / "c.png")
    assert _count() == before + 1


def test_install_is_idempotent(fig, tmp_path):
    """Stacked wrappers would double-write AND make uninstall restore a wrapper
    instead of matplotlib's own function."""
    mplhook.install()
    mplhook.install()
    mplhook.install()
    before = _count()
    fig.savefig(tmp_path / "d.png")
    assert _count() == before + 1


def test_uninstall_restores_the_original_function():
    from matplotlib.figure import Figure

    pristine = Figure.savefig
    mplhook.install()
    assert Figure.savefig is not pristine
    mplhook.uninstall()
    assert Figure.savefig is pristine
    assert mplhook.installed() is False


def test_uninstall_stops_registration(fig, tmp_path):
    mplhook.install()
    mplhook.uninstall()
    before = _count()
    fig.savefig(tmp_path / "e.png")
    assert _count() == before


# ------------------------------------------------------- what is NOT registered


def test_non_raster_output_passes_through(fig, tmp_path):
    """A PDF has no pixels to perceptually hash, so it could never be matched
    back to a slide; registering one would put an unmatchable row in the store."""
    mplhook.install()
    before = _count()
    fig.savefig(tmp_path / "f.pdf")
    assert _count() == before
    assert (tmp_path / "f.pdf").exists(), "the figure must still be written"


def test_file_like_target_passes_through(fig):
    """savefig accepts a stream. A BytesIO has no path to hash later and no
    sidecar location, so it is passed through rather than half-registered."""
    mplhook.install()
    before = _count()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    assert _count() == before
    assert buf.getvalue(), "the figure must still be written to the stream"


# ---------------------------------------------------------- double registration


def test_explicit_helper_does_not_double_register(fig, tmp_path):
    """The bug this pins, measured: with install() active, mplhook.savefig()
    filed the same figure TWICE -- once from its own store.put and once from the
    patched Figure.savefig it calls internally. The re-entrancy flag has to be
    claimed by the helper, not only inside the wrapper.
    """
    mplhook.install()
    before = _count()
    mplhook.savefig(fig, str(tmp_path / "g.png"), cite="This work")
    assert _count() == before + 1


def test_helper_still_registers_when_not_installed(fig, tmp_path):
    """Positive control for the test above: if the guard were simply always on,
    the helper would register nothing and that test would still pass."""
    before = _count()
    mplhook.savefig(fig, str(tmp_path / "h.png"), cite="This work")
    assert _count() == before + 1


def test_concurrent_saves_each_register(fig, tmp_path):
    """The guard is thread-local. A plain global set by one thread would be seen
    by another, and that race is silent in the losing direction: the other
    thread's figure is passed through UNREGISTERED, which is indistinguishable
    from a figure nobody saved.
    """
    mplhook.install()
    before = _count()
    threads = [
        threading.Thread(target=lambda i=i: fig.savefig(tmp_path / f"t{i}.png"))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _count() == before + 4


def test_guard_is_thread_local_not_a_module_global():
    assert isinstance(mplhook._state, threading.local)


# ------------------------------------------------------------------ failure


def test_registration_failure_is_loud(fig, tmp_path, monkeypatch):
    """A provenance tool that silently stops recording is the exact failure this
    project exists to prevent."""
    mplhook.install(strict=True)

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(store, "register_existing", boom)
    out = tmp_path / "i.png"
    with pytest.raises(mplhook.RegistrationError) as ei:
        fig.savefig(out)
    assert "disk full" in str(ei.value)
    assert str(out) in str(ei.value), "the error must name the unregistered file"
    assert out.exists(), (
        "the figure is written before registration is attempted, so a "
        "provenance failure must not cost you the plot"
    )


def test_non_strict_downgrades_to_a_warning(fig, tmp_path, monkeypatch):
    mplhook.install(strict=False)

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(store, "register_existing", boom)
    with pytest.warns(UserWarning, match="could NOT record its provenance"):
        fig.savefig(tmp_path / "j.png")


# ------------------------------------------------------------------- content


def test_auto_registered_record_carries_the_build_context(fig, tmp_path):
    mplhook.install(dataset="rnaseq_v3", cite="This work", note="panel A")
    fig.savefig(tmp_path / "k.png")
    rec = [
        r
        for r in store.iter_records()
        if r.source_detail.get("auto_registered")
        and r.source_detail.get("path", "").endswith("k.png")
    ][-1]
    assert rec.source_kind == "generated"
    assert rec.confirmed is True, "your own figure is not a guess"
    assert rec.source_detail["dataset"] == "rnaseq_v3"
    assert rec.source_detail["cwd"]
    assert rec.note == "panel A"
    assert rec.sha256 and rec.dhash, "must be matchable by hash and perceptually"


def test_registration_does_not_rewrite_the_figure(fig, tmp_path):
    """register_existing hashes bytes as written; the file must be untouched."""
    mplhook.install()
    out = tmp_path / "l.png"
    fig.savefig(out)
    blob = out.read_bytes()
    from figcite.provenance import sha256_bytes

    rec = [
        r
        for r in store.iter_records()
        if r.source_detail.get("path", "").endswith("l.png")
    ][-1]
    assert rec.sha256 == sha256_bytes(blob)
