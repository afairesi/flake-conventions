# Copyright (c) 2026- Paschalis Bizopoulos
"""Collect and compare package names and the test names declared by each package."""

# ruff: noqa: INP001 - mkPythonPackage installs this file inside the package.

import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from test_explorer.rust import rust_test_names
from test_explorer.suite_readers import (
    bats_test_names,
    cpp_test_names,
    lean_test_names,
    nix_test_names,
)

import flake_layout

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

TEST_READERS = {
    "bash": bats_test_names,
    "generic": nix_test_names,
    "lean4": lean_test_names,
    "python": flake_layout.python_test_names,
}


type PackageStatus = Literal["added", "removed", "modified"]


class PackageChange(TypedDict):
    """Changes to one package and its named tests between the index and worktree."""

    package: str
    status: PackageStatus
    added: list[str]
    removed: list[str]


def _suite_reader(path: Path) -> Callable[[Path], set[str]] | None:
    """Recognize suite sources referenced directly by a package build."""
    if path.suffix == ".py" and path.name.startswith("test_"):
        return flake_layout.python_test_names
    if path.suffix in {".cc", ".cpp", ".cxx"} and path.stem.endswith(
        ("-test", "_test")
    ):
        return cpp_test_names
    if path.suffix == ".bats":
        return bats_test_names
    return None


def _declared_suites(
    package: flake_layout.Package,
) -> Iterator[tuple[Path, Callable[[Path], set[str]]]]:
    """Follow literal suite paths inside this package, including Nix interpolations."""
    pending = [package.declaration.root]
    while pending:
        node = pending.pop()
        if node.type == "path_expression" and all(
            child.type == "path_fragment" for child in node.named_children
        ):
            value = package.declaration.text(node)
            path = Path(value)
            if (
                value.startswith("./")
                and ".." not in path.parts
                and (reader := _suite_reader(path))
            ):
                yield package.root / path, reader
        else:
            pending.extend(node.named_children)


def inspect_repository(root: Path) -> dict[str, list[str]]:
    """Map each declared package to its sorted, statically named tests."""
    if not (root / "flake.nix").is_file():
        message = f"{root}: missing flake.nix"
        raise ValueError(message)
    tracked = (
        {
            Path(name)
            for name in flake_layout.git(root, "ls-files", "-z").split("\0")
            if name
        }
        if (root / ".git").exists()
        else None
    )
    paths = tracked if tracked is not None else flake_layout.source_paths(root)
    packages, _ = flake_layout.detect_packages(root, paths)
    inventory: dict[str, list[str]] = {}
    for package in packages:
        name = package.root.name
        names: set[str] = set()
        suites = dict(_declared_suites(package))
        if reader := TEST_READERS.get(package.kind):
            suite = package.root / flake_layout.PACKAGE_TEST_FILES[package.kind]
            suites[suite] = reader
        for suite, read_names in suites.items():
            if (
                tracked is None or suite.relative_to(root) in tracked
            ) and suite.is_file():
                names.update(read_names(suite))
        if (
            package.kind == "generic"
            and Path(name) / "Cargo.toml" in paths
            and (package.root / "Cargo.toml").is_file()
        ):
            tracked_rust = (
                None
                if tracked is None
                else {root / path for path in tracked if path.suffix == ".rs"}
            )
            names.update(rust_test_names(package.root, tracked_rust))
        inventory[name] = sorted(names)
    return inventory


def index_entries(root: Path) -> Iterator[tuple[str, str, str, str]]:
    """Yield the mode, object ID, stage, and path of each Git index entry."""
    for entry in flake_layout.git(root, "ls-files", "--stage", "-z").split("\0"):
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
        *flake_layout.PACKAGE_TEST_FILES.values(),
    }
    inputs = [
        (mode, oid, path)
        for mode, oid, path in files
        if path == Path("flake.nix")
        or (len(path.parent.parts) == 1 and path.name in filenames)
        or _suite_reader(path) is not None
        or (path.suffix == ".rs" and Path(path.parts[0]) in manifests)
    ]
    if any(mode == "120000" or (root / path).is_symlink() for mode, _, path in inputs):
        return files
    return inputs


def _snapshot_inventory(
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
    before = _snapshot_inventory(root, files)
    after = _snapshot_inventory(root, files, working_tree=True)
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
