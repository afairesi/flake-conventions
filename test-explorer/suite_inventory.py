# Copyright (c) 2026- Paschalis Bizopoulos
"""Collect and compare package names and the test names declared by each package."""

import io
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict

from test_explorer.node_tests import is_node_suite, node_test_names
from test_explorer.rust import rust_test_names
from test_explorer.suite_readers import (
    bats_test_names,
    cpp_test_names,
    lean_test_names,
    nix_test_names,
)

import flake_layout
import nix_syntax

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from tree_sitter import Node

TEST_READERS = {
    "bash": bats_test_names,
    "generic": nix_test_names,
    "lean4": lean_test_names,
    "python": flake_layout.python_test_names,
}


type PackageStatus = Literal["added", "removed", "modified"]


class PackageInfo(TypedDict):
    """Display metadata and statically discovered tests for a package."""

    description: str
    tests: list[str]


class PackageChange(TypedDict):
    """Changes to one package and its named tests between the index and worktree."""

    package: str
    description: str
    status: PackageStatus
    added: list[str]
    removed: list[str]


class RepositoryDiff(TypedDict):
    """Changes to the repository description and package rows shown in the tree."""

    description_changed: bool
    packages: list[PackageChange]


def _description(
    document: nix_syntax.Document, node: Node | None, path: tuple[str, ...]
) -> str:
    """Read a one-line description from dotted or nested Nix bindings."""
    if node is None:
        return ""
    if not path:
        if node.type == "indented_string_expression" and all(
            child.type == "string_fragment" for child in node.named_children
        ):
            value = document.text(node)[2:-2]
        else:
            value = nix_syntax.string_value(document, node) or ""
        return " ".join(value.split())
    if node.type not in {"attrset_expression", "rec_attrset_expression"}:
        return ""
    bindings = nix_syntax.binding_set(node)
    for binding in bindings.named_children if bindings else ():
        attrpath = binding.child_by_field_name("attrpath")
        if attrpath is None:
            continue
        parts = nix_syntax.static_attrpath(document, attrpath)
        if parts and path[: len(parts)] == parts:
            value_node = nix_syntax.resolve_expression(
                document, binding.child_by_field_name("expression")
            )
            return _description(document, value_node, path[len(parts) :])
    return ""


def flake_description(root: Path) -> str:
    """Read optional metadata even when the home checkout has no flake."""
    path = root / "flake.nix"
    try:
        if not path.is_file():
            return ""
        document = nix_syntax.parse(path.read_bytes(), str(path))
    except OSError, ValueError:
        return ""
    return _description(
        document,
        nix_syntax.resolve_expression(document, document.root),
        ("description",),
    )


def _package_description(document: nix_syntax.Document) -> str:
    """Read meta.description from the returned set or the builder's arguments."""
    node = nix_syntax.call_package_body(document)
    if node is not None and node.type == "apply_expression":
        node = nix_syntax.resolve_expression(
            document, node.child_by_field_name("argument")
        )
        if node is not None and node.type == "function_expression":
            node = nix_syntax.resolve_expression(
                document, node.child_by_field_name("body")
            )
    return _description(document, node, ("meta", "description"))


def _suite_reader(path: Path) -> Callable[[Path], set[str]] | None:
    """Recognize suite sources referenced directly by a package build."""
    if path.suffix == ".py" and (
        path.stem.startswith("test_") or path.stem.endswith("_test")
    ):
        return flake_layout.python_test_names
    if path.suffix in {".cc", ".cpp", ".cxx"} and path.stem.endswith(
        ("-test", "_test")
    ):
        return cpp_test_names
    if path.suffix == ".bats":
        return bats_test_names
    if is_node_suite(path):
        return node_test_names
    return None


_LOCAL_PATH = re.compile(r"\$\{(\./[^{}]*)\}([^\s]*)")
_PYTEST = re.compile(
    r"^\s*(?:python(?:3(?:\.\d+)?)?(?:\s+-[A-Za-z]+)*\s+-m\s+)?pytest\s+(.+)$"
)
_PYTHON_FILE_FILTER = re.compile(
    r'\(\s*(?P<file>\w+)\s*:\s*(?P=file)\.hasExt\s+"py"\s*&&\s*'
    r'(?:[\w.]+\.)?hasPrefix\s+"test_"\s+(?P=file)\.name\s*\)'
)


def _local_path(text: str) -> Path | None:
    path = Path(text)
    return path if text.startswith("./") and ".." not in path.parts else None


def _pytest_suites(script: str, paths: set[Path]) -> tuple[set[Path], set[Path]]:
    """Collect files and literal ignores from each supported pytest invocation."""
    suites = set()
    ignored_paths = set()
    for line in script.replace("\\\n", " ").splitlines():
        match = _PYTEST.fullmatch(line)
        if match is None:
            continue
        try:
            arguments = iter(shlex.split(match[1], comments=True))
        except ValueError:
            continue
        roots = set()
        ignores = set()
        for argument in arguments:
            option, separator, value = argument.partition("=")
            ignored = option == "--ignore"
            if ignored and not separator:
                value = next(arguments, "")
            elif not ignored:
                value = argument
            if (local := _LOCAL_PATH.fullmatch(value)) and (
                path := _local_path(local[1] + local[2])
            ) is not None:
                (ignores if ignored else roots).add(path)
        ignored_paths.update(ignores)
        suites.update(
            path
            for path in paths
            if any(path.is_relative_to(root) for root in roots)
            and not any(path.is_relative_to(ignored) for ignored in ignores)
        )
    return suites, ignored_paths


def _file_filter_root(document: nix_syntax.Document, node: Node) -> Path | None:
    """Recognize a Nix fileset selecting Python files whose names start with test_."""
    function = node.child_by_field_name("function")
    directory = node.child_by_field_name("argument")
    if function is None or function.type != "apply_expression" or directory is None:
        return None
    selector = function.child_by_field_name("function")
    predicate = function.child_by_field_name("argument")
    if selector is None or predicate is None or directory.type != "path_expression":
        return None
    attrs = selector.child_by_field_name("attrpath")
    if (
        attrs is not None
        and (parts := nix_syntax.static_attrpath(document, attrs))
        and parts[-1] == "fileFilter"
        and _PYTHON_FILE_FILTER.fullmatch(document.text(predicate))
    ):
        return _local_path(document.text(directory))
    return None


def _suite_children(
    document: nix_syntax.Document, node: Node, *, in_passthru: bool
) -> tuple[list[Node], bool]:
    """Follow live expressions and only visit test bindings within passthru."""
    if node.type in {"let_expression", "variable_expression"}:
        resolved = nix_syntax.resolve_expression(document, node)
        return ([resolved] if resolved is not None else []), in_passthru
    if node.type != "binding":
        return node.named_children, in_passthru
    attrpath = node.child_by_field_name("attrpath")
    parts = (nix_syntax.static_attrpath(document, attrpath) if attrpath else None) or ()
    if (
        parts[:1] == ("meta",)
        or (in_passthru and parts[:1] != ("tests",))
        or (parts[:1] == ("passthru",) and len(parts) > 1 and parts[1] != "tests")
    ):
        return [], False
    if parts[:1] == ("passthru",):
        value = node.child_by_field_name("expression")
        return ([value] if value is not None else []), len(parts) == 1
    return node.named_children, False


def _declared_suites(
    package: flake_layout.Package, paths: set[Path]
) -> dict[Path, Callable[[Path], set[str]]]:
    """Find explicit and directory-based suites without evaluating Nix."""
    document = package.declaration
    suites = {}
    python_paths = {
        path
        for path in paths
        if path.suffix == ".py" and _suite_reader(path) is not None
    }
    pending = [(document.root, False)]
    seen = set()
    while pending:
        node, in_passthru = pending.pop()
        if (node.id, in_passthru) in seen:
            continue
        seen.add((node.id, in_passthru))
        children, in_passthru = _suite_children(document, node, in_passthru=in_passthru)
        if node.type == "path_expression" and all(
            child.type == "path_fragment" for child in node.named_children
        ):
            path = _local_path(document.text(node))
            if path is not None and (reader := _suite_reader(path)):
                suites[path] = reader
        elif node.type == "indented_string_expression":
            selected, ignored = _pytest_suites(document.text(node)[2:-2], python_paths)
            suites.update(dict.fromkeys(selected, flake_layout.python_test_names))
            if ignored:
                children = [
                    child
                    for child in children
                    if child.type != "interpolation"
                    or (value := child.child_by_field_name("expression")) is None
                    or value.type != "path_expression"
                    or _local_path(document.text(value)) not in ignored
                ]
        elif (
            node.type == "apply_expression"
            and (directory := _file_filter_root(document, node)) is not None
        ):
            suites.update(
                dict.fromkeys(
                    (
                        path
                        for path in python_paths
                        if path.name.startswith("test_")
                        and path.is_relative_to(directory)
                    ),
                    flake_layout.python_test_names,
                )
            )
        pending.extend((child, in_passthru) for child in children)
    return suites


def inspect_packages(root: Path) -> dict[str, PackageInfo]:
    """Read descriptions and test names from the same package declarations."""
    if not (root / "flake.nix").is_file():
        message = f"{root}: missing flake.nix"
        raise ValueError(message)
    indexed = (root / ".git").exists()
    paths = (
        {
            Path(name)
            for name in flake_layout.git(root, "ls-files", "-z").split("\0")
            if name
        }
        if indexed
        else {
            directory.relative_to(root) / name
            for directory, _, filenames in root.walk()
            for name in filenames
        }
    )
    tracked_rust = (
        {root / path for path in paths if path.suffix == ".rs"} if indexed else None
    )
    packages, _ = flake_layout.detect_packages(root, paths)
    inventory: dict[str, PackageInfo] = {}
    for package in packages:
        name = package.root.name
        names: set[str] = set()
        relative_paths = {
            path.relative_to(name) for path in paths if path.is_relative_to(name)
        }
        primary: dict[Path, Callable[[Path], set[str]]] = {}
        if reader := TEST_READERS.get(package.kind):
            primary[Path(flake_layout.PACKAGE_TEST_FILES[package.kind])] = reader
        if (
            package.kind == "generic"
            and Path("package.json") in relative_paths
            and (package.root / "package.json").is_file()
        ):
            primary.update(
                dict.fromkeys(filter(is_node_suite, relative_paths), node_test_names)
            )
        suites = _declared_suites(package, relative_paths) | primary
        for suite, read_names in suites.items():
            source = package.root / suite
            if suite in relative_paths and source.is_file():
                prefix = "" if suite in primary else f"{suite.as_posix()}::"
                names.update(prefix + test for test in read_names(source))
        if (
            package.kind == "generic"
            and Path("Cargo.toml") in relative_paths
            and (package.root / "Cargo.toml").is_file()
        ):
            names.update(rust_test_names(package.root, tracked_rust))
        inventory[name] = PackageInfo(
            description=_package_description(package.declaration), tests=sorted(names)
        )
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
        "package.json",
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
) -> tuple[str, dict[str, PackageInfo]]:
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
            objects = flake_layout.git_bytes(
                root,
                "cat-file",
                "--batch",
                stdin="".join(f"{oid}\n" for _mode, oid, _path in files).encode(),
            )
            content = io.BytesIO(objects)
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
        if not (snapshot / "flake.nix").is_file():
            return "", {}
        return flake_description(snapshot), inspect_packages(snapshot)


def compare_repository(root: Path) -> RepositoryDiff:
    """Compare the displayed inventory from the index to the working tree."""
    files = diff_inputs(root)
    if not files:
        return RepositoryDiff(description_changed=False, packages=[])
    old_description, before = _snapshot_inventory(root, files)
    new_description, after = _snapshot_inventory(root, files, working_tree=True)
    changes: list[PackageChange] = []
    for package, info in sorted((before | after).items()):
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
        old_names = set(previous["tests"] if previous else ())
        new_names = set(current["tests"] if current else ())
        changes.append(
            PackageChange(
                package=package,
                description=info["description"],
                status=status,
                added=sorted(new_names - old_names),
                removed=sorted(old_names - new_names),
            )
        )
    return RepositoryDiff(
        description_changed=new_description != old_description, packages=changes
    )
