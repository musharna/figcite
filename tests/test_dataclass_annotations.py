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
    for m in pkgutil.walk_packages(figcite.__path__, figcite.__name__ + "."):
        mod = importlib.import_module(m.name)
        for name, obj in vars(mod).items():
            if (
                isinstance(obj, type)
                and dataclasses.is_dataclass(obj)
                and obj.__module__ == mod.__name__
            ):
                found.append(pytest.param(obj, id=f"{obj.__module__}.{name}"))
    return found


_FOUND = _dataclasses_in_package()


def test_the_sweep_actually_found_dataclasses():
    """Positive control. An import error or a renamed package would make the
    parametrized test below collect zero cases and report green."""
    assert len(_FOUND) >= 3, f"expected figcite's dataclasses, found {_FOUND}"


@pytest.mark.parametrize("cls", _FOUND)
def test_every_dataclass_annotation_resolves(cls):
    """Guard the class of defect, not the instance."""
    typing.get_type_hints(cls)
