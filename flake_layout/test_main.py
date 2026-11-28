# Copyright (c) 2026- Paschalis Bizopoulos
"""Test flake layout validation and static Python test discovery."""

import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import flake_layout
import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Iterator


pytest_plugins = ("pytester",)


def test_python_names_match_the_functions_and_methods_collected_by_pytest(
    pytester: pytest.Pytester,
) -> None:
    """Compare the static inventory with the node IDs collected by pytest."""
    path = pytester.makepyfile("""
import unittest as unit
from unittest import IsolatedAsyncioTestCase as AsyncCase
from unittest import TestCase
from unittest import TestCase as Case
def testCamelCase(): pass
def test(): pass
def helper():
    def test_nested(): pass
class TestArguments:
    def testCamelCase(self): pass
    async def test_missing_input(self): pass
class Arguments(Case):
    def test_valid_input(self): pass
class Derived(Arguments):
    def test_other_input(self): pass
class Helper:
    def test_not_collected(self): pass
class Reports(TestCase):
    def testCamelCase(self): pass
class Cases:
    def test_result(self): pass
class Intermediate(Cases): pass
class TestInherited(Intermediate): pass
class TestOverride(Cases):
    test_result = None
    def test_visible(self): pass
class TestDisabled(Cases):
    __test__ = False
    __test__: bool
class TestEnabled(Cases):
    __test__ = False
    del __test__
class Qualified(unit.TestCase):
    def test_qualified(self): pass
class AsyncBase(AsyncCase): pass
class AsyncReports(AsyncBase):
    async def test_result(self): pass
""")
    collected = {item.nodeid.split("::", 1)[1] for item in pytester.getitems(path)}
    declared = flake_layout.python_test_names(path)
    if declared != collected:
        raise AssertionError((declared, collected))


def _add_example(root: Path, *, python: bool) -> None:
    """Stage a minimal Python package or generic Nix declaration for validation."""
    package = root / "example"
    package.mkdir()
    if python:
        (package / "default.nix").write_text(
            "{ inputs, pkgs, ... }: inputs.self.lib.mkPythonPackage "
            "{ inherit pkgs; src = ./.; executable = true; }"
        )
        (package / "main.py").write_text("def main(): pass\n")
    else:
        (package / "default.nix").write_text("{ pkgs, ... }: pkgs.writeTextFile {}")
    flake_layout.git(root, "add", "--", "example")


@pytest.mark.parametrize("exported", [False, True], ids=["git", "export"])
def test_layout_check_skips_nested_assets_without_running_package_code(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, exported: bool
) -> None:
    """Check package files without executing code or traversing nested assets."""
    _add_example(root, python=True)
    (root / "example/main.py").write_text("raise RuntimeError('must not execute')\n")
    assets = root / "example/assets"
    assets.mkdir()
    (assets / "default.nix").write_text("invalid nested declaration")
    (assets / "nested.rs").touch()
    flake_layout.git(root, "add", "--", "example")
    local = root / "experiments"
    local.mkdir()
    (local / "main.py").write_text("invalid untracked source")
    if exported:
        shutil.rmtree(root / ".git")
    original = os.scandir

    def guarded(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        if Path(path).is_relative_to(assets) or (
            not exported and Path(path).is_relative_to(local)
        ):
            message = f"traversed excluded directory {path}"
            raise AssertionError(message)
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)
    sources = flake_layout.source_paths(root)
    if Path("example/default.nix") not in sources:
        raise AssertionError(sources)
    if any(path.is_relative_to("example/assets") for path in sources):
        raise AssertionError(sources)
    flake_layout.validate_flake_source(root)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Stage a minimal flake in an isolated Git repository."""
    flake_layout.git(tmp_path, "init", "--quiet")
    (tmp_path / "flake.nix").write_text("{}\n")
    (tmp_path / "flake.lock").touch()
    flake_layout.git(tmp_path, "add", ".")
    return tmp_path


def test_layout_check_rejects_a_tracked_python_package_initializer(
    root: Path,
) -> None:
    """Report a tracked initializer that conflicts with the installed main.py."""
    _add_example(root, python=True)
    initializer = root / "example/__init__.py"
    initializer.write_text('"""Personal initialization."""\n')
    flake_layout.git(root, "add", "example/__init__.py")
    with pytest.raises(ValueError, match=r"__init__\.py: reserved"):
        flake_layout.validate_flake_source(root)


@pytest.mark.parametrize(
    "relative",
    ["flake.nix", "example/default.nix", "example/main.nix", "example/test_main.nix"],
)
def test_layout_check_identifies_the_file_with_invalid_nix_syntax(
    root: Path, relative: str
) -> None:
    """Report syntax errors in tracked flake, package, source, and test files."""
    _add_example(root, python=False)
    (root / relative).write_text("{ inputs, pkgs, ... }: (\n")
    flake_layout.git(root, "add", "--", relative)
    with pytest.raises(
        nix_syntax.NixSyntaxError, match=rf"{re.escape(relative)}:\d+:\d+: parse error"
    ):
        flake_layout.validate_flake_source(root)
