"""Lazy annotations are invisible to every other test in this suite.

`figcite/service.py` carries `from __future__ import annotations`, so every
annotation is stored as a string and never evaluated. `ConfirmResult.record`
shipped annotated `Record` with no import of `Record`: the dataclass
constructed fine, all 151 tests passed, and only `typing.get_type_hints`
could see the NameError.

The cause is worth recording, because it will recur: the import WAS written,
and a post-edit lint pass removed it as unused -- correctly, at that instant,
because the import landed a moment before the dataclass that uses it. Any
edit that adds an import ahead of its first use is exposed to this.
"""

import dataclasses
import importlib
import pkgutil
import typing

import pytest

import figcite


def test_confirm_result_annotations_resolve():
    """The instance the review caught."""
    from figcite import service

    hints = typing.get_type_hints(service.ConfirmResult)
    assert hints["record"].__name__ == "Record"


def _dataclasses_in_package():
    """Every dataclass figcite defines, discovered rather than listed.

    A hand-written list of class names cannot guard an open set: the next
    dataclass added to the package would not be on it, which is precisely how
    this defect got in.
    """
    found = []
    walk_errors = []
    # M4. walk_packages swallows ImportError while recursing unless onerror is
    # given, so a sub-package that stopped importing would drop its dataclasses
    # out of the sweep silently -- the sweep would go green having looked at
    # less than it did yesterday.
    for m in pkgutil.walk_packages(
        figcite.__path__, figcite.__name__ + ".", onerror=walk_errors.append
    ):
        mod = importlib.import_module(m.name)
        for name, obj in vars(mod).items():
            if (
                isinstance(obj, type)
                and dataclasses.is_dataclass(obj)
                and obj.__module__ == mod.__name__
            ):
                found.append(pytest.param(obj, id=f"{obj.__module__}.{name}"))
    return found, walk_errors


_FOUND, _WALK_ERRORS = _dataclasses_in_package()
_FOUND_IDS = {p.id for p in _FOUND}

# The classes this sweep exists for. Naming them is NOT the guard -- the sweep
# below is, and it covers whatever else the package defines. This is the
# positive control on the sweep's reach: a count cannot serve, because once a
# fourth dataclass lands anywhere, a targeted discovery failure of
# figcite.service -- the one module that actually had the defect -- keeps the
# count at 3 and the sweep goes green with ConfirmResult never looked at.
_MUST_REACH = {
    "figcite.service.ConfirmResult",
    "figcite.service.PendingItem",
    "figcite.provenance.Record",
}


def test_the_sweep_reached_the_classes_it_exists_for():
    """Positive control on reach, by identity rather than by count."""
    assert not _WALK_ERRORS, f"walk_packages could not import: {_WALK_ERRORS}"
    missing = _MUST_REACH - _FOUND_IDS
    assert not missing, f"sweep never looked at {missing}; it saw {_FOUND_IDS}"


@pytest.mark.parametrize("cls", _FOUND)
def test_every_dataclass_annotation_resolves(cls):
    """Guard the class of defect, not the instance."""
    typing.get_type_hints(cls)
