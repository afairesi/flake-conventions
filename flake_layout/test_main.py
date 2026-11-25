# Copyright (c) 2026- Paschalis Bizopoulos
"""Test the flake layout check."""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import flake_layout
import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Iterator


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
    _git(root, "add", "--", "example")


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(  # noqa: S603 - Fixed Git executable with test-controlled arguments.
        ["git", *arguments],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout


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
    _git(root, "add", "--", "example")
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
    _expect(Path("example/default.nix") in sources, sources)
    _expect(not any(path.is_relative_to("example/assets") for path in sources), sources)
    flake_layout.validate_flake_source(root)


def _expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Stage a minimal flake in an isolated Git repository."""
    _git(tmp_path, "init", "--quiet")
    (tmp_path / "flake.nix").write_text("{}\n")
    (tmp_path / "flake.lock").touch()
    _git(tmp_path, "add", ".")
    return tmp_path


def test_layout_check_rejects_a_tracked_python_package_initializer(
    root: Path,
) -> None:
    """Report the reserved initializer and preserve the index and working tree."""
    _add_example(root, python=True)
    initializer = root / "example/__init__.py"
    initializer.write_text('"""Personal initialization."""\n')
    _git(root, "add", "example/__init__.py")
    staged = _git(root, "write-tree").strip()
    with pytest.raises(ValueError, match=r"__init__\.py: reserved"):
        flake_layout.validate_flake_source(root)
    _git(root, "diff", "--cached", "--exit-code", staged)
    _git(root, "diff", "--exit-code", staged)
    _expect(
        not _git(root, "ls-files", "--others"), "validation created untracked files"
    )


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
    _git(root, "add", "--", relative)
    with pytest.raises(
        nix_syntax.NixSyntaxError, match=rf"{re.escape(relative)}:\d+:\d+: parse error"
    ):
        flake_layout.validate_flake_source(root)
