# Copyright (c) 2026- Paschalis Bizopoulos
"""Discover package declarations and check the layout of a flake source tree."""

# ruff: noqa: INP001 - mkPythonPackage installs this module as __init__.py.

import ast
import keyword
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import nix_syntax

if TYPE_CHECKING:
    from tree_sitter import Node

BUILDER_KINDS = {
    "mkBashPackage": "bash",
    "mkHtmlPackage": "html",
    "mkLatexPackage": "latex",
    "mkLean4Package": "lean4",
    "mkPythonPackage": "python",
}
PACKAGE_MAIN_FILES = {
    "bash": "main.sh",
    "generic": "main.nix",
    "html": "index.html",
    "latex": "ms.tex",
    "lean4": "main.lean",
    "python": "main.py",
}
PACKAGE_TEST_FILES = {
    "bash": "test_main.bats",
    "generic": "test_main.nix",
    "lean4": "test_main.lean",
    "python": "test_main.py",
}
ROOT_FILES = ("flake.lock", "flake.nix")


@dataclass(frozen=True)
class Package:
    """A repository package and its parsed declaration."""

    root: Path
    kind: str
    declaration: nix_syntax.Document


def git(root: Path, *arguments: str) -> str:
    """Read Git output without optional writes to the index."""
    completed = subprocess.run(  # noqa: S603 - Git arguments are passed without a shell.
        ["git", "--no-optional-locks", "-C", str(root), *arguments],  # noqa: S607
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if completed.returncode:
        raise ValueError(completed.stderr.strip())
    return completed.stdout


def read_regular(path: Path) -> bytes | None:
    """Read an optional regular file, rejecting other filesystem entries."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        msg = f"{path}: must be a regular file"
        raise ValueError(msg)
    return path.read_bytes()


def source_paths(root: Path) -> set[Path]:
    """Select top-level package files and indexed Rust sources for module discovery."""
    if (root / ".git").exists():
        entries = git(root, "ls-files", "-z")
        return {
            Path(item)
            for item in entries.split("\0")
            if item and (item.count("/") <= 1 or item.endswith(".rs"))
        }
    paths: set[Path] = set()
    for directory, directories, filenames in root.walk():
        if directory != root:
            directories.clear()
        relative = directory.relative_to(root)
        paths.update(relative / name for name in filenames)
    return paths


def _builder_field(document: nix_syntax.Document, field: str) -> Node | None:
    """Resolve the function or argument of the returned builder call."""
    body = nix_syntax.call_package_body(document)
    if body is None or body.type != "apply_expression":
        return None
    return nix_syntax.resolve_expression(document, body.child_by_field_name(field))


def _package_kind(document: nix_syntax.Document) -> str:
    """Select conventions only from recognizable builders without evaluating Nix."""
    function = _builder_field(document, "function")
    if function is not None and function.type == "select_expression":
        attrs = function.child_by_field_name("attrpath")
        if attrs is not None and (parts := nix_syntax.static_attrpath(document, attrs)):
            return BUILDER_KINDS.get(parts[-1], "generic")
    return "generic"


def _find_declarations(root: Path, paths: set[Path], filename: str) -> list[Path]:
    """Find declarations with the given filename in top-level directories."""
    declarations = []
    for path in paths:
        if path.name != filename or len(path.parent.parts) != 1:
            continue
        declaration = root / path
        if not declaration.parent.is_symlink() and declaration.parent.is_dir():
            declarations.append(declaration)
    return sorted(declarations)


def detect_packages(root: Path, paths: set[Path]) -> tuple[list[Package], list[Path]]:
    """Read package declarations and identify tracked declarations missing on disk."""
    result = []
    missing = []
    for path in _find_declarations(root, paths, "default.nix"):
        source = read_regular(path)
        relative = path.relative_to(root)
        if source is None:
            missing.append(relative)
            continue
        document = nix_syntax.parse(source, str(relative))
        result.append(Package(path.parent, _package_kind(document), document))
    return result, missing


def validate_python_package_names(packages: list[Package]) -> None:
    """Reject invalid or conflicting installed Python module names."""
    modules: dict[str, str] = {}
    for package in packages:
        if package.kind != "python":
            continue
        name = package.root.name
        module = name.replace("-", "_")
        if not module.isidentifier() or keyword.iskeyword(module):
            msg = (
                f"{name}: installed Python module must be "
                "a valid non-keyword identifier"
            )
            raise ValueError(msg)
        if module in modules:
            msg = (
                f"{name} and {modules[module]}: "
                f"installed Python module names collide: {module}"
            )
            raise ValueError(msg)
        modules[module] = name


def _layout_issues(root: Path, packages: list[Package], paths: set[Path]) -> list[str]:
    """Check tracked package and host files, including Python entry points."""
    issues: list[str] = []
    conventional = {Path(item) for item in ROOT_FILES}
    for config in _find_declarations(root, paths, "configuration.nix"):
        if config.is_file():
            relative = config.relative_to(root)
            conventional.update(
                (relative, relative.with_name("hardware-configuration.nix"))
            )
    for package in packages:
        issues.extend(_python_source_issues(package, paths))
        directory = package.root.relative_to(root)
        main = directory / PACKAGE_MAIN_FILES[package.kind]
        conventional.add(main)
        if suite := PACKAGE_TEST_FILES.get(package.kind):
            conventional.add(directory / suite)
        if package.kind != "generic" and (
            main not in paths or not (root / main).is_file()
        ):
            issues.append(f"{main}: missing required tracked regular file")
    for relative in sorted(conventional & paths):
        path = root / relative
        if path.is_symlink():
            issues.append(
                f"{relative}: expected regular file or directory, found symbolic link",
            )
    return issues


def _python_class_mro(
    classes: dict[str, ast.ClassDef],
    name: str,
    seen: set[str],
) -> list[str]:
    """Compute Python's class precedence without executing base declarations."""
    if name in seen:
        msg = f"cyclic class inheritance: {name}"
        raise ValueError(msg)
    if name not in classes:
        return [name]
    bases = [ast.unparse(base) for base in classes[name].bases]
    sequences = [_python_class_mro(classes, base, seen | {name}) for base in bases] + [
        bases.copy(),
    ]
    result = [name]
    while sequences := [sequence for sequence in sequences if sequence]:
        candidate = next(
            (
                sequence[0]
                for sequence in sequences
                if all(sequence[0] not in other[1:] for other in sequences)
            ),
            None,
        )
        if candidate is None:
            msg = f"inconsistent class inheritance: {name}"
            raise ValueError(msg)
        result.append(candidate)
        for sequence in sequences:
            if sequence[0] == candidate:
                sequence.pop(0)
    return result


def _test_names_from_ast(module: ast.Module) -> set[str]:
    """Collect pytest identifiers, including methods inherited from local bases."""
    classes = {
        node.name: node for node in module.body if isinstance(node, ast.ClassDef)
    }
    case_classes = _unittest_classes(module)
    names = {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    }
    for name in classes:
        if not (name.startswith("Test") or name in case_classes):
            continue
        methods: set[str] = set()
        for ancestor in reversed(_python_class_mro(classes, name, set())):
            if ancestor not in classes:
                continue
            for statement in classes[ancestor].body:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if statement.name.startswith("test"):
                        methods.add(statement.name)
                elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        statement.targets
                        if isinstance(statement, ast.Assign)
                        else [statement.target]
                    )
                    methods.difference_update(
                        target.id for target in targets if isinstance(target, ast.Name)
                    )
        names.update(f"{name}::{method}" for method in methods)
    return names


def _python_source_issues(package: Package, paths: set[Path]) -> list[str]:
    """Check source contracts without importing or executing package code."""
    if package.kind != "python":
        return []
    issues = []
    name = package.root.name
    if Path(name) / "__init__.py" in paths:
        issues.append(
            f"{name}/__init__.py: reserved for the installed main.py; "
            "move initialization into main.py"
        )
    source = read_regular(package.root / "main.py")
    if source is not None:
        module = ast.parse(source, filename=str(package.root / "main.py"))
        if _test_names_from_ast(module):
            issues.append(
                f"{name}/main.py: move test definitions to test_main.py",
            )
        entrypoint = (
            None if _python_library(package.declaration) else _python_entrypoint(module)
        )
        prefix = f"{name}/main.py: "
        if isinstance(entrypoint, ast.AsyncFunctionDef):
            issues.append(
                prefix + "main must be synchronous; the executable calls main()",
            )
        elif isinstance(entrypoint, (ast.FunctionDef, ast.Lambda)) and (
            len(entrypoint.args.posonlyargs) + len(entrypoint.args.args)
            > len(entrypoint.args.defaults)
            or any(default is None for default in entrypoint.args.kw_defaults)
        ):
            issues.append(prefix + "main must accept a call without arguments")
        elif isinstance(
            entrypoint, (ast.Constant, ast.List, ast.Tuple, ast.Set, ast.Dict)
        ):
            issues.append(prefix + "main must be callable; the executable calls main()")
    tests = package.root / "test_main.py"
    if tests.relative_to(package.root.parent) in paths and tests.is_file():
        _source(tests)
    return issues


def _python_entrypoint(
    module: ast.Module,
) -> ast.stmt | ast.expr | None:
    """Find the last direct binding or deletion of the installed main name."""
    for node in reversed(module.body):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == "main"
        ):
            return node
        if isinstance(node, ast.Delete) and any(
            isinstance(target, ast.Name) and target.id == "main"
            for target in node.targets
        ):
            return node
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            (alias.asname or alias.name) == "main" for alias in node.names
        ):
            return None
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(target, ast.Name) and target.id == "main"
                for target in targets
            ):
                return node.value
    return None


def validate_flake_source(root: Path) -> None:
    """Check the flake layout in a Git checkout or an exported source tree."""
    paths = source_paths(root)
    packages, missing = detect_packages(root, paths)
    validate_python_package_names(packages)
    issues = _layout_issues(root, packages, paths)
    issues.extend(f"{path}: missing required tracked regular file" for path in missing)
    flake = read_regular(root / "flake.nix")
    if flake is not None:
        nix_syntax.parse(flake, "flake.nix")
    for required in ROOT_FILES:
        if not (root / required).is_file():
            issues.append(f"{required}: missing required regular file")
    if issues:
        formatted_issues = "\n".join(f"  - {issue}" for issue in issues)
        msg = f"repository layout validation failed:\n{formatted_issues}"
        raise ValueError(msg)
    for package in packages:
        if package.kind == "generic":
            for filename in (
                PACKAGE_MAIN_FILES["generic"],
                PACKAGE_TEST_FILES["generic"],
            ):
                source = package.root / filename
                if source.relative_to(root) in paths and source.is_file():
                    nix_syntax.parse(source.read_bytes(), str(source))


def _unittest_classes(module: ast.Module) -> set[str]:
    """Recognize unittest subclasses regardless of their class names."""
    bases: set[str] = set()
    case_types = {"TestCase", "IsolatedAsyncioTestCase"}
    for node in module.body:
        if isinstance(node, ast.Import):
            bases.update(
                (alias.asname or alias.name) + "." + case
                for alias in node.names
                if alias.name == "unittest"
                for case in case_types
            )
        elif isinstance(node, ast.ImportFrom) and node.module == "unittest":
            bases.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name in case_types
            )
    classes = [node for node in module.body if isinstance(node, ast.ClassDef)]
    found: set[str] = set()
    while additions := {
        node.name
        for node in classes
        if node.name not in found
        and any(ast.unparse(base) in bases | found for base in node.bases)
    }:
        found.update(additions)
    return found


def _source(path: Path) -> ast.Module:
    """Parse source without importing it or executing its declarations."""
    try:
        return ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, UnicodeError) as error:
        message = f"{path}: could not parse Python source: {error}"
        raise ValueError(message) from error


def _python_library(document: nix_syntax.Document) -> bool:
    """Read the executable setting from the Python builder's arguments."""
    argument = _builder_field(document, "argument")
    if argument is None or argument.type not in {
        "attrset_expression",
        "rec_attrset_expression",
    }:
        return False
    bindings = nix_syntax.binding_set(argument)
    return [
        document.text(value)
        for binding in (bindings.named_children if bindings is not None else [])
        if (attrpath := binding.child_by_field_name("attrpath")) is not None
        and nix_syntax.static_attrpath(document, attrpath) == ("executable",)
        and (value := binding.child_by_field_name("expression")) is not None
    ] == ["false"]


def python_test_names(path: Path) -> set[str]:
    """Read the pytest identifiers declared in a suite."""
    return _test_names_from_ast(_source(path))
