# Copyright (c) 2026- Paschalis Bizopoulos
"""Check project layout and source validation."""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import nix_syntax
import project_layout

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
def test_source_validation_skips_nested_assets_without_running_package_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, exported: bool
) -> None:
    """Validate resource files without executing code or traversing nested assets."""
    repository = _repository(tmp_path)
    _add_example(repository, python=True)
    (repository / "example/main.py").write_text(
        "raise RuntimeError('must not execute')\n"
    )
    assets = repository / "example/assets"
    assets.mkdir()
    (assets / "default.nix").write_text("invalid nested declaration")
    _git(repository, "add", "--", "example")
    local = repository / "experiments"
    local.mkdir()
    (local / "main.py").write_text("invalid untracked source")
    if exported:
        shutil.rmtree(repository / ".git")
    original = os.scandir

    def guarded(path: str | os.PathLike[str]) -> Iterator[os.DirEntry[str]]:
        if Path(path).is_relative_to(assets) or (
            not exported and Path(path).is_relative_to(local)
        ):
            message = f"traversed excluded directory {path}"
            raise AssertionError(message)
        return original(path)

    monkeypatch.setattr(os, "scandir", guarded)
    sources = project_layout.source_paths(repository)
    _expect(Path("example/default.nix") in sources, sources)
    _expect(Path("example/assets/default.nix") not in sources, sources)
    project_layout.validate_flake_source(repository)


def _expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an invariant failure with its observed state."""
    if not condition:
        raise AssertionError(detail)


def _repository(root: Path) -> Path:
    """Index a minimal flake in an existing temporary directory."""
    _git(root, "init", "--quiet")
    (root / "flake.nix").write_text("{}\n")
    (root / "flake.lock").touch()
    _git(root, "add", ".")
    return root


@pytest.fixture(name="root")
def repository_root_fixture(tmp_path: Path) -> Path:
    """Index a flake alongside the test's other temporary files."""
    root = tmp_path / "repo"
    root.mkdir()
    return _repository(root)


def _snapshot(root: Path) -> dict[Path, tuple[int, bytes]]:
    """Observe contents and modes, including Git metadata and symbolic links."""
    files = {}
    for path in root.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            contents = os.fsencode(path.readlink())
        elif stat.S_ISREG(mode):
            contents = path.read_bytes()
        else:
            contents = b""
        files[path.relative_to(root)] = (mode, contents)
    return files


def test_source_validation_rejects_a_tracked_python_package_initializer(
    root: Path,
) -> None:
    """Reject a selected initializer without changing source or installing over main."""
    _add_example(root, python=True)
    initializer = root / "example/__init__.py"
    initializer.write_text('"""Personal initialization."""\n')
    _git(root, "add", "example/__init__.py")
    before = _snapshot(root)
    with pytest.raises(ValueError, match=r"__init__\.py: reserved"):
        project_layout.validate_flake_source(root)
    _expect(_snapshot(root) == before, "rejection changed sources")


@pytest.mark.parametrize(
    "relative",
    ["flake.nix", "example/default.nix", "example/main.nix", "example/test_main.nix"],
)
def test_source_validation_identifies_the_file_with_invalid_nix_syntax(
    root: Path, relative: str
) -> None:
    """Report syntax errors in tracked flake, package, source, and test files."""
    _add_example(root, python=False)
    (root / relative).write_text("{ inputs, pkgs, ... }: (\n")
    _git(root, "add", "--", relative)
    with pytest.raises(
        nix_syntax.NixSyntaxError, match=rf"{re.escape(relative)}:\d+:\d+: parse error"
    ):
        project_layout.validate_flake_source(root)
