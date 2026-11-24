# Copyright (c) 2026- Paschalis Bizopoulos
"""Check Nix parsing and literal decoding through the shared library."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import nix_syntax


@given(st.binary(max_size=4096))
def test_parsing_preserves_source_bytes_in_the_document_or_raises_a_nix_syntax_error(
    source: bytes,
) -> None:
    """Arbitrary bytes produce a document or the documented syntax error."""
    try:
        document = nix_syntax.parse(source)
    except nix_syntax.NixSyntaxError:
        return
    if document.source != source or document.tree.root_node.has_error:
        raise AssertionError(document)


@pytest.mark.parametrize(
    "expression", [b"{}", b"[ 1 2 ]", b"let a = 1; in a", b'"${toString 1}"']
)
def test_unclosed_nix_expressions_report_the_file_line_and_column_of_the_syntax_error(
    expression: bytes,
) -> None:
    """Keep valid source bytes and locate a syntax error in its incomplete version."""
    if nix_syntax.parse(expression).source != expression:
        raise AssertionError(expression)
    with pytest.raises(
        nix_syntax.NixSyntaxError, match=r"incomplete\.nix:\d+:\d+: parse error"
    ):
        nix_syntax.parse(b"(" + expression, path="incomplete.nix")


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        (r'"café\r\n\${name}"', "café\r\n${name}"),
        ('"a\rb\r\nc\\\r\n"', "a\nb\nc\r\n"),
        (r'"\q\\\t\"$$"', 'q\\\t"$$'),
        ('""', ""),
    ],
)
def test_quoted_strings_decode_escapes_and_newlines_at_the_selected_source_position(
    literal: str, expected: str
) -> None:
    """Read the literal at its UTF-8 byte offset using Nix escape and newline rules."""
    document = nix_syntax.parse(f'["préfixe" {literal} undefined]')
    value = nix_syntax.string_value(document, document.root.named_children[1])
    if value != expected:
        raise AssertionError(value)


@pytest.mark.parametrize("source", ['"${undefined}"', "42", "''indented''"])
def test_literal_inspection_reads_only_double_quoted_strings_without_interpolation(
    source: str,
) -> None:
    """Leave interpolations and other expression types unresolved."""
    document = nix_syntax.parse(source)
    if nix_syntax.string_value(document, document.root) is not None:
        raise AssertionError(source)


@pytest.mark.parametrize("value", ['a"b', "a\\b", "\r\n\t", "café", "${name}", ""])
def test_quoting_a_value_preserves_its_text_and_keeps_interpolation_literal(
    value: str,
) -> None:
    """Encode a string as Nix source and read the same literal back."""
    document = nix_syntax.parse(nix_syntax.quote_string(value))
    actual = nix_syntax.string_value(document, document.root)
    if actual != value:
        raise AssertionError(actual)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('let a = b; b = "value"; in (a)', '"value"'),
        ('let a = "outer"; in args: let a = "inner"; in a', '"inner"'),
        ('let a = "outer"; in a: a', None),
        ('let a = "outer"; in { a }: a', None),
        ("let a = b; b = a; in a", None),
    ],
)
def test_package_aliases_resolve_locally_while_parameters_and_cycles_stay_unresolved(
    source: str, expected: str | None
) -> None:
    """Inspect a package result without evaluating its Nix expression."""
    document = nix_syntax.parse(source)
    body = nix_syntax.call_package_body(document)
    actual = None if body is None else document.text(body)
    if actual != expected:
        raise AssertionError((source, actual))
