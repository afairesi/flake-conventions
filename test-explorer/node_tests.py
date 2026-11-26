# Copyright (c) 2026- Paschalis Bizopoulos
"""Discover named JavaScript cases declared with Node's built-in test runner."""

from functools import cache
from typing import TYPE_CHECKING

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from tree_sitter import Node


def is_node_suite(path: Path) -> bool:
    """Match JavaScript files using Node's default test discovery conventions."""
    return (
        path.suffix in {".js", ".mjs", ".cjs"}
        and "node_modules" not in path.parts
        and (
            "test" in path.parts[:-1]
            or path.stem == "test"
            or path.stem.startswith("test-")
            or path.stem.endswith((".test", "-test", "_test"))
        )
    )


def node_suites(package: Path, tracked: set[Path] | None) -> Iterator[Path]:
    """List default suites inside an npm package or its Git index snapshot."""
    if tracked is not None:
        for path in tracked:
            if path.parts[0] == package.name and is_node_suite(
                path.relative_to(package.name)
            ):
                yield package.parent / path
        return
    for directory, directories, filenames in package.walk():
        if "node_modules" in directories:
            directories.remove("node_modules")
        for filename in filenames:
            suite = directory / filename
            if is_node_suite(suite.relative_to(package)):
                yield suite


@cache
def _query(pattern: str) -> Query:
    return Query(get_language("javascript"), pattern)


def _matches(root: Node, source: bytes, pattern: str) -> Iterator[dict[str, str]]:
    for _, captures in QueryCursor(_query(pattern)).matches(root):
        yield {
            key: source[node.start_byte : node.end_byte].decode()
            for key, (node, *_) in captures.items()
        }


_IMPORTS = """
(import_statement
  (import_clause (identifier) @runner)
  source: (string (string_fragment) @module))
(import_statement
  (import_clause (namespace_import (identifier) @namespace))
  source: (string (string_fragment) @module))
(import_statement
  (import_clause (named_imports
    (import_specifier name: (identifier) @name alias: (identifier)? @alias)))
  source: (string (string_fragment) @module))
"""
_REQUIRE = """
value: (call_expression
  function: (identifier) @require
  arguments: (arguments . (string (string_fragment) @module) .))
"""
_BINDINGS = _IMPORTS + "\n".join(
    f"(variable_declarator name: {binding} {_REQUIRE})"
    for binding in (
        "(identifier) @runner",
        "(object_pattern (shorthand_property_identifier_pattern) @name)",
        """(object_pattern (pair_pattern
              key: (property_identifier) @name value: (identifier) @alias))""",
    )
)


def node_test_names(path: Path) -> set[str]:
    """Read static test and it titles without executing the package."""
    source = path.read_bytes()
    root = get_parser("javascript").parse(source).root_node
    functions: set[str] = set()
    for binding in _matches(root, source, _BINDINGS):
        if (
            binding["module"] != "node:test"
            or binding.get("require", "require") != "require"
        ):
            continue
        if runner := binding.get("runner"):
            functions.add(runner)
        if namespace := binding.get("namespace") or binding.get("runner"):
            functions.update(f"{namespace}.{method}" for method in ("test", "it"))
        if binding.get("name") in {"test", "it"}:
            functions.add(binding.get("alias", binding["name"]))
    callees = functions | {
        f"{name}.{modifier}"
        for name in functions
        for modifier in ("only", "skip", "todo")
    }
    calls = """
    (call_expression function: [(identifier) (member_expression)] @callee
      arguments: (arguments . [(string) (template_string)] @title))
    """
    names = set()
    for call in _matches(root, source, calls):
        title = call["title"]
        if call["callee"] in callees and not (title.startswith("`") and "${" in title):
            names.add(title[1:-1])
    return names
