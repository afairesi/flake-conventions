# Copyright (c) 2026- Paschalis Bizopoulos
"""Inspect and compare the named tests in tracked project packages."""

# ruff: noqa: INP001 - mkPythonPackage installs this file inside the package.

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from test_explorer.rust import cargo_names
from test_explorer.specification import bats_names, lean_names, nix_names

import project_layout

if TYPE_CHECKING:
    from collections.abc import Iterator

TEST_READERS = {
    "bash": bats_names,
    "generic": nix_names,
    "lean4": lean_names,
    "python": project_layout.python_suite_names,
}


type PackageStatus = Literal["added", "removed", "modified"]


class PackageChange(TypedDict):
    """Changes to one package and its named tests between the index and worktree."""

    package: str
    status: PackageStatus
    added: list[str]
    removed: list[str]


def inspect_repository(root: Path) -> dict[str, list[str]]:
    """Map each declared package to its sorted, statically named tests."""
    if not (root / "flake.nix").is_file():
        message = f"{root}: expected an Afairesi repository with flake.nix"
        raise ValueError(message)
    paths = project_layout.source_paths(root)
    packages, _ = project_layout.detect_packages(root, paths)
    report: dict[str, list[str]] = {}
    tracked_rust = None
    for package in packages:
        name = package.root.name
        names: set[str] = set()
        if extract := TEST_READERS.get(package.kind):
            suite = package.root / project_layout.PACKAGE_TEST_FILES[package.kind]
            if suite.relative_to(root) in paths and suite.is_file():
                names.update(extract(suite))
        if (
            package.kind == "generic"
            and Path(name) / "Cargo.toml" in paths
            and (package.root / "Cargo.toml").is_file()
        ):
            if tracked_rust is None and (root / ".git").exists():
                tracked_rust = {
                    root / source for source in paths if source.suffix == ".rs"
                }
            names.update(cargo_names(package.root, tracked_rust))
        report[name] = sorted(names)
    return report


def index_entries(root: Path) -> Iterator[tuple[str, str, str, str]]:
    """Yield the mode, object ID, stage, and path of each Git index entry."""
    for entry in project_layout.git(root, "ls-files", "--stage", "-z").split("\0"):
        if entry:
            metadata, name = entry.split("\t", 1)
            mode, oid, stage = metadata.split()
            yield mode, oid, stage, name


def diff_inputs(root: Path) -> list[tuple[str, str, Path]]:
    """Select inspection inputs, retaining possible targets of source symlinks."""
    files = []
    for mode, oid, stage, name in index_entries(root):
        if stage != "0":
            message = "cannot inspect an unmerged index; resolve conflicts first"
            raise ValueError(message)
        if mode != "160000":
            files.append((mode, oid, Path(name)))
    manifests = {
        path.parent
        for _, _, path in files
        if path.name == "Cargo.toml" and len(path.parent.parts) == 1
    }
    filenames = {
        "default.nix",
        "Cargo.toml",
        *project_layout.PACKAGE_TEST_FILES.values(),
    }
    inputs = [
        (mode, oid, path)
        for mode, oid, path in files
        if path == Path("flake.nix")
        or (len(path.parent.parts) == 1 and path.name in filenames)
        or (path.suffix == ".rs" and Path(path.parts[0]) in manifests)
    ]
    if any(mode == "120000" or (root / path).is_symlink() for mode, _, path in inputs):
        return files
    return inputs


def _inspection_git_report(
    root: Path,
    files: list[tuple[str, str, Path]],
    *,
    working_tree: bool = False,
) -> dict[str, list[str]]:
    """Inspect tracked sources in a temporary directory without checkout filters."""
    with tempfile.TemporaryDirectory(prefix="test-explorer-diff-") as directory:
        snapshot = Path(directory)
        if working_tree:
            for _mode, _oid, relative in files:
                source = root / relative
                if any((root / parent).is_symlink() for parent in relative.parents):
                    continue
                if source.is_file() or source.is_symlink():
                    target = snapshot / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, target, follow_symlinks=False)
        else:
            completed = subprocess.run(  # noqa: S603 - Fixed Git command; object IDs are passed on stdin.
                ["git", "-C", str(root), "cat-file", "--batch"],  # noqa: S607
                input="".join(f"{oid}\n" for _mode, oid, _path in files).encode(),
                capture_output=True,
                check=False,
                timeout=30,
            )
            if completed.returncode:
                raise ValueError(completed.stderr.decode(errors="replace").strip())
            content = io.BytesIO(completed.stdout)
            for mode, _oid, relative in files:
                _object, separator, size = content.readline().partition(b" blob ")
                if not separator:
                    message = f"{relative}: could not read Git blob"
                    raise ValueError(message)
                source_bytes = content.read(int(size))
                content.read(1)
                target = snapshot / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if mode == "120000":
                    target.symlink_to(os.fsdecode(source_bytes))
                else:
                    target.write_bytes(source_bytes)
        return (
            inspect_repository(snapshot) if (snapshot / "flake.nix").is_file() else {}
        )


def diff_repository(root: Path) -> list[PackageChange]:
    """List package and test-name changes from the index to the working tree."""
    files = diff_inputs(root)
    if not files:
        return []
    before = _inspection_git_report(root, files)
    after = _inspection_git_report(root, files, working_tree=True)
    changes: list[PackageChange] = []
    for package in sorted(before.keys() | after.keys()):
        previous = before.get(package)
        current = after.get(package)
        if previous is None:
            status: PackageStatus = "added"
        elif current is None:
            status = "removed"
        elif current != previous:
            status = "modified"
        else:
            continue
        old_names = set(previous or ())
        new_names = set(current or ())
        changes.append(
            PackageChange(
                package=package,
                status=status,
                added=sorted(new_names - old_names),
                removed=sorted(old_names - new_names),
            )
        )
    return changes
