# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise home discovery, directory grouping, and test-name diffs."""

import curses
import json
import os
import re
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

import pytest
import test_explorer
from test_explorer import suite_inventory

import flake_layout
from flake_layout import git

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


def expect(condition: object, detail: object) -> None:
    """Require a truthy result and report the observed value on failure."""
    if not condition:
        raise AssertionError(detail)


def labels(node: test_explorer.Node, *path: str) -> list[str]:
    """Read the child labels at a displayed tree path."""
    for name in path:
        node = next(child for child in node.children if child.label == name)
    return [child.label for child in node.children]


def commit(root: Path, message: str) -> None:
    """Commit the staged fixture files with a test identity."""
    git(
        root,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "maintenance.auto=false",
        "commit",
        "--quiet",
        "-m",
        message,
    )


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """Create one tracked package for cache tests."""
    root = tmp_path / "project"
    _indexed_repository(root)
    return root


def add_submodule(home: Path, child: Path) -> None:
    """Create a fixture checkout and register its commit in the home index."""
    _indexed_repository(child)
    commit(child, "baseline")
    path = child.relative_to(home).as_posix()
    git(home, "config", "set", "--file", ".gitmodules", f"submodule.{path}.path", path)
    git(
        home,
        "config",
        "set",
        "--file",
        ".gitmodules",
        f"submodule.{path}.url",
        "https://example.invalid/repo",
    )
    git(home, "add", "--force", ".gitmodules")
    git(
        home,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{git(child, 'rev-parse', 'HEAD').strip()},{path}",
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """Register two clean checkouts with the same basename."""
    root = tmp_path / "home"
    root.mkdir()
    git(root, "init", "--quiet")
    for name in ("github.com/owner/project", "gitlab.com/owner/project"):
        add_submodule(root, root / name)
    return root


def test_home_tree_groups_direct_submodules_by_path_without_modifying_them(
    home: Path,
) -> None:
    """Discover direct checkouts and read diffs only for unstaged changes."""
    for name in ("missing", "broken"):
        add_submodule(home, home / name)
    (home / "missing").rename(home / "unregistered")
    (home / "broken/flake.nix").unlink()
    first = home / "github.com/owner/project"
    source = first / "example/test_main.py"
    source.write_text("def test_staged(): ...\n")
    git(first, "add", "example/test_main.py")
    source.write_text("def test_working(): ...\n")
    second = home / "gitlab.com/owner/project"
    nested = second / "nested"
    add_submodule(second, nested)
    commit(second, "Register nested checkout")
    (nested / "example/test_main.py").write_text("def test_nested_edit(): ...\n")
    (second / "untracked.txt").touch()
    paths = [home / ".git/index", first / ".git/index", source]
    before = [path.read_bytes() for path in paths]
    tree = test_explorer.build_tree(home, cache=home.parent / "cache")
    expect(labels(tree) == ["broken", "github.com", "gitlab.com", "missing"], tree)
    project = ("github.com", "owner", "project")
    expect(labels(tree, *project, "example") == ["- staged", "+ working"], tree)
    expect(labels(tree, *project) == ["example"], tree)
    viewer = test_explorer.Viewer(tree)
    expect(viewer.search("test_working"), tree)
    colors = {text.strip(): color for text, _, color in viewer.frame(30)}
    expect(
        all(
            colors["▾ " + label] == "~"
            for label in (str(home), *project, "example (1): Example")
        ),
        colors,
    )
    expect(colors["+ working"] == "+" and colors["- staged"] == "-", colors)
    expect(colors["▸ gitlab.com"] == "", colors)
    other = ("gitlab.com", "owner", "project")
    expect(labels(tree, *other, "example") == ["default output"], tree)
    expect(labels(tree, *other) == ["example"], tree)
    expect(
        labels(tree, "missing") == ["error: This submodule is not initialized."], tree
    )
    expect(any("flake.nix" in label for label in labels(tree, "broken")), tree)
    expect([path.read_bytes() for path in paths] == before, "inspection modified files")


@pytest.fixture
def mixed_language_checkout(home: Path) -> Path:
    """Track package declarations and leave their suite sources ready to stage."""
    checkout = home / "github.com/owner/project"
    suites = {
        "shell": (
            "mkBashPackage",
            "test_main.bats",
            """exit 99
cat <<'TEXT'
@test "not a declaration" {
TEXT
@test "preserves spaces" { true; }
handles_tabs() { # @test
  true
}
helper() { true; }
""",
        ),
        "nix": (
            "",
            "test_main.nix",
            """{ ... }: let
              selection = [ "testHTTPRequestsUseAHeader" "keepsSpaces" ];
              suite = {
                tests = selection;
                keepsSpaces = { expr = throw "never evaluate"; expected = true; };
                testHTTPRequestsUseAHeader = { expr = 1; expected = 1; };
                testExcluded = { expr = false; expected = true; };
              };
            in (suite)""",
        ),
        "proof": (
            "mkLean4Package",
            "test_main.lean",
            """namespace Résultats
section
/- theorem ignored : True := trivial -/
theorem gardeÉtiquette : True := trivial
example : True := trivial
end
end Résultats
""",
        ),
        "crate": (
            "",
            "tests/output.rs",
            "mod output { #[test] fn test_keeps_name() {} }",
        ),
        "untested": ("", "test_main.nix", "{}"),
    }
    for package, (builder, filename, source) in suites.items():
        directory = checkout / package
        path = directory / filename
        path.parent.mkdir(parents=True)
        (directory / "default.nix").write_text(
            f"{{ inputs, ... }}: inputs.self.lib.{builder} {{}}" if builder else "{}"
        )
        path.write_text(source + "\n")
    (checkout / "crate/Cargo.toml").write_text(
        '[package]\nname = "crate"\nversion = "0.1.0"\n'
    )
    git(checkout, "add", "--", "*/default.nix", "crate/Cargo.toml")
    return checkout


def test_tracked_bats_nix_lean_and_rust_tests_are_searchable_under_their_packages(
    home: Path,
    mixed_language_checkout: Path,
) -> None:
    """Search each suite's displayed name and select it under its package."""
    cache = home.parent / "cache"
    expected = {
        "shell": ["handles tabs", "preserves spaces"],
        "nix": ["keeps spaces", "HTTP requests use a header"],
        "proof": ["Résultats.garde étiquette"],
        "crate": ["output::keeps name"],
    }
    before = suite_inventory.inspect_packages(mixed_language_checkout)
    expect(all(not before[package]["tests"] for package in expected), before)
    git(mixed_language_checkout, "add", ".")
    tree = test_explorer.build_tree(home, cache=cache)
    viewer = test_explorer.Viewer(tree)
    project = ("github.com", "owner", "project")
    for package, names in expected.items():
        expect(sorted(labels(tree, *project, package)) == sorted(names), package)
        for name in names:
            expect(viewer.search(name), name)
            frame = viewer.frame(40)
            expect(
                any(selected and row.strip() == name for row, selected, _ in frame),
                frame,
            )
            expect(
                any(
                    row.strip() == f"▾ {package} ({len(names)})" for row, _, _ in frame
                ),
                frame,
            )
    expect(viewer.search("testHTTPRequestsUseAHeader"), tree)
    expect(
        any(
            selected and row.strip() == "HTTP requests use a header"
            for row, selected, _ in viewer.frame(40)
        ),
        tree,
    )
    expect(viewer.search("untested"), tree)
    frame = viewer.frame(40)
    expect(
        any(selected and row.strip() == "untested (0)" for row, selected, _ in frame),
        frame,
    )


def test_added_and_removed_test_names_remain_visible_under_their_packages(
    home: Path, mixed_language_checkout: Path
) -> None:
    """Keep additions and removals searchable even when a whole package disappears."""
    checkout = mixed_language_checkout
    git(checkout, "add", ".")
    cache = home.parent / "cache"
    project = ("github.com", "owner", "project")
    test_explorer.cached_output(checkout, "diff", cache, refresh=False)
    (checkout / "shell/test_main.bats").write_text(
        '@test "preserves tabs" { true; }\n@test "accepts blanks" { true; }\n'
    )
    for package in ("crate", "proof", "untested"):
        (checkout / package / "default.nix").unlink()
    changes = test_explorer.build_tree(home, view="diff", cache=cache)
    expect("gitlab.com" not in labels(changes), changes)
    expect(labels(changes, *project, "- crate") == ["- output::keeps name"], changes)
    expect(
        labels(changes, *project, "shell")
        == [
            "- handles tabs",
            "- preserves spaces",
            "+ accepts blanks",
            "+ preserves tabs",
        ],
        changes,
    )
    expect(
        labels(changes, *project, "- proof") == ["- Résultats.garde étiquette"], changes
    )
    expect("- untested" in labels(changes, *project), changes)
    tests = test_explorer.Viewer(test_explorer.build_tree(home, cache=cache))
    expect(tests.search("+ preserves tabs"), tests.root)
    rows = {row.strip(): color for row, _, color in tests.frame(40)}
    expect(rows["▾ shell (2)"] == "~" and rows["▸ - crate (0)"] == "-", rows)
    expect(rows["- preserves spaces"] == "-" and rows["+ preserves tabs"] == "+", rows)


def test_a_timed_out_checkout_reports_an_inspection_error(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Show the repository whose inspection timed out."""
    cache = home.parent / "cache"
    original = suite_inventory.inspect_packages

    def timeout(root: Path) -> dict[str, suite_inventory.PackageInfo]:
        if root == home / "github.com/owner/project":
            raise subprocess.TimeoutExpired(["git", "ls-files"], 30)
        return original(root)

    monkeypatch.setattr(suite_inventory, "inspect_packages", timeout)
    tree = test_explorer.build_tree(
        home, cache=cache, progress=test_explorer.write_startup_progress
    )
    expect(
        any(
            "timed out" in label
            for label in labels(tree, "github.com", "owner", "project")
        ),
        tree,
    )


def test_unrelated_edits_leave_cached_packages_and_their_ancestors_uncolored(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Staged and unstaged implementation edits leave an unchanged inventory plain."""
    cache = home.parent / "cache"
    cached = home / "gitlab.com/owner/project"
    test_explorer.cached_output(cached, "tests", cache, refresh=False)
    (cached / "notes.txt").touch()
    git(cached, "add", "notes.txt")
    working = home / "github.com/owner/project"
    (working / "example/main.py").write_text("def unfinished(")
    (working / "example/test_main.py").write_text(
        'def test_default_output():\n    raise RuntimeError("edited test body")\n'
    )
    (working / "untracked").mkdir()
    (working / "untracked/default.nix").write_text("{}")
    tree = test_explorer.build_tree(
        home, cache=cache, progress=test_explorer.write_startup_progress
    )
    expect(
        labels(tree, "gitlab.com", "owner", "project", "example") == ["default output"],
        tree,
    )
    viewer = test_explorer.Viewer(tree)
    for _ in range(2):
        expect(viewer.search("test_default_output"), tree)
    rows = viewer.frame(30)
    expect(all(not color for _, _, color in rows), rows)
    progress = capsys.readouterr().err
    expect("2 direct submodules" in progress and "[2/2]" in progress, progress)
    expect("gitlab.com/owner/project: cached" in progress, progress)
    diff = test_explorer.build_tree(home, "diff", cache=cache)
    expect(labels(diff) == ["No inventory changes"], diff)


def test_home_tree_requires_a_git_root_and_accepts_one_without_submodules(
    tmp_path: Path,
) -> None:
    """Show an empty valid home, but reject a nested directory or a non-repository."""
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="not a git repository"):
        test_explorer.build_tree(tmp_path, cache=cache)
    git(tmp_path, "init", "--quiet")
    tree = test_explorer.build_tree(tmp_path, cache=cache)
    expect(
        tree.label == str(tmp_path)
        and labels(tree) == ["No direct registered submodules."],
        tree,
    )
    differences = test_explorer.build_tree(tmp_path, cache=cache, view="diff")
    expect(labels(differences) == ["No inventory changes"], differences)
    child = tmp_path / "directory"
    child.mkdir()
    with pytest.raises(ValueError, match="repository root"):
        test_explorer.build_tree(child, cache=cache)


@pytest.mark.parametrize("view", ["tests", "diff"])
def test_both_views_reuse_cached_results_until_their_test_inputs_change(
    checkout: Path,
    view: test_explorer.View,
) -> None:
    """Use working names for the tests view and staged names as the diff baseline."""
    cache = checkout.parent / "cache"
    nested = checkout / "nested"
    add_submodule(checkout, nested)
    source = checkout / "example/test_main.py"
    source.write_text("def test_working(): ...\n")
    first, hit = test_explorer.cached_output(checkout, view, cache, refresh=False)
    expect(not hit, "first inspection used a cached result")
    (checkout / "example/main.py").write_text("irrelevant")
    git(checkout, "add", "example/main.py")
    (nested / "example/test_main.py").write_text("def test_nested(): ...\n")
    again, hit = test_explorer.cached_output(checkout, view, cache, refresh=False)
    expect(hit and first == again, again)
    metadata = source.stat()
    source.write_text(source.read_text().replace("test_working", "test_updated"))
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    changed, hit = test_explorer.cached_output(checkout, view, cache, refresh=False)
    expect(not hit and changed != first, changed)
    git(checkout, "add", "example/test_main.py")
    staged, hit = test_explorer.cached_output(checkout, view, cache, refresh=False)
    if view == "tests":
        expect(
            hit
            and staged
            == changed
            == {"example": {"description": "Example", "tests": ["test_updated"]}},
            staged,
        )
    else:
        expect(
            not hit and staged == {"description_changed": False, "packages": []},
            staged,
        )
    commit(checkout, "updated")
    committed, hit = test_explorer.cached_output(checkout, view, cache, refresh=False)
    expect(hit and committed == staged, committed)
    _, hit = test_explorer.cached_output(checkout, view, cache, refresh=True)
    expect(not hit, "explicit refresh used cached data")
    git(checkout, "rm", "--cached", "example/test_main.py")
    untracked, _ = test_explorer.cached_output(checkout, view, cache, refresh=False)
    expect(
        untracked
        == (
            {"example": {"description": "Example", "tests": []}}
            if view == "tests"
            else {"description_changed": False, "packages": []}
        ),
        untracked,
    )


@pytest.mark.parametrize("view", ["tests", "diff"])
def test_views_are_reloaded_when_the_cache_is_damaged_or_unavailable(
    checkout: Path,
    view: test_explorer.View,
) -> None:
    """Reload the view when cached JSON is malformed or storage is unavailable."""
    cache = checkout.parent / "cache"
    (checkout / "example/test_main.py").write_text("def test_updated(): ...\n")
    expected, _ = test_explorer.cached_output(checkout, view, cache, refresh=False)
    entry = next(cache.glob("*.json"))
    saved = json.loads(entry.read_text())
    invalid = (
        {"example": [None]}
        if view == "tests"
        else {"description_changed": False, "packages": [{}]}
    )
    entry.write_text(json.dumps({**saved, "value": invalid}))
    actual, hit = test_explorer.cached_output(checkout, view, cache, refresh=False)
    expect(not hit and actual == expected, actual)
    entry.write_text("partial {")
    actual, hit = test_explorer.cached_output(checkout, view, cache, refresh=False)
    expect(not hit and actual == expected, actual)
    blocked = checkout.parent / "blocked-cache"
    blocked.write_text("not a directory")
    actual, hit = test_explorer.cached_output(checkout, view, blocked, refresh=False)
    expect(not hit and actual == expected, actual)


def test_the_next_inspection_retries_failed_or_unstable_results(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry errors and avoid persisting a result read while the source was changing."""
    cache = checkout.parent / "cache"
    flake = checkout / "flake.nix"
    flake.unlink()
    with pytest.raises(ValueError, match=r"flake\.nix"):
        test_explorer.cached_output(checkout, "tests", cache, refresh=False)
    expect(not cache.exists(), "failed inspection was cached")
    flake.write_text("{}")
    original = suite_inventory.inspect_packages

    def changing(path: Path) -> dict[str, suite_inventory.PackageInfo]:
        value = original(path)
        (path / "example/test_main.py").write_text("def test_changed(): ...\n")
        return value

    monkeypatch.setattr(suite_inventory, "inspect_packages", changing)
    test_explorer.cached_output(checkout, "tests", cache, refresh=False)
    expect(not cache.exists(), "changing source was cached")
    monkeypatch.setattr(suite_inventory, "inspect_packages", original)
    current, hit = test_explorer.cached_output(checkout, "tests", cache, refresh=False)
    expect(
        not hit
        and current
        == {"example": {"description": "Example", "tests": ["test_changed"]}},
        current,
    )


def test_test_name_cache_refreshes_when_a_symlink_target_changes(
    checkout: Path,
) -> None:
    """Read new test names through an unchanged suite symlink."""
    cache = checkout.parent / "cache"
    target = checkout.parent / "external.py"
    tests = checkout / "example/test_main.py"
    tests.rename(target)
    tests.symlink_to(target)
    git(checkout, "add", "example/test_main.py")
    test_explorer.cached_output(checkout, "tests", cache, refresh=False)
    target.write_text("def test_other(): ...\n")
    value, hit = test_explorer.cached_output(checkout, "tests", cache, refresh=False)
    expect(
        not hit
        and value == {"example": {"description": "Example", "tests": ["test_other"]}},
        value,
    )


def test_directories_start_collapsed_and_can_be_expanded_with_the_keyboard() -> None:
    """Show meaningful labels and let h/l fold and unfold details without JSON noise."""
    root = test_explorer.Node("/home/test", expanded=True)
    project = root.add("github.com").add("owner").add("project")
    test_explorer.add_tests(
        project, {"hello": {"description": "", "tests": ["test_greets_the_reader"]}}
    )
    viewer = test_explorer.Viewer(root)
    expect(
        viewer.frame(30) == [("▾ /home/test", True, ""), ("  ▸ github.com", False, "")],
        viewer.frame(30),
    )
    for key in "lllll":
        viewer.handle(key, 30)
    repository = list(viewer.root.visible())[viewer.selected][0]
    expect(repository.label == "project" and not repository.expanded, repository)
    expect(
        [node.label for node in repository.children] == ["hello"],
        repository.children,
    )
    for key in "ljl":
        viewer.handle(key, 30)
    package = list(viewer.root.visible())[viewer.selected][0]
    expect(package.label == "hello" and package.expanded, package)
    for key in "hhh":
        viewer.handle(key, 30)
    expect(
        list(viewer.root.visible())[viewer.selected][0] is repository
        and not repository.expanded,
        viewer.frame(30),
    )
    viewer.handle(" ", 30)
    expect(repository.expanded, "Space did not expand repository")
    viewer.handle("\n", 30)
    expect(not repository.expanded, "Enter did not fold repository")
    expect(not viewer.handle("q", 30), "q did not quit")


def test_viewer_keeps_the_selection_visible_when_paging_or_resizing() -> None:
    """Keep navigation visible across viewport changes, paging, and collapsed roots."""
    root = test_explorer.Node("home", expanded=True)
    for number in range(12):
        root.add(f"repository {number}")
    viewer = test_explorer.Viewer(root)
    viewer.handle("G", 4)
    expect(viewer.frame(4)[-1] == ("    repository 11", True, ""), viewer.frame(4))
    expect(
        viewer.frame(2)
        == [("    repository 10", False, ""), ("    repository 11", True, "")],
        viewer.frame(2),
    )
    viewer.handle(curses.KEY_PPAGE, 2)
    expect(viewer.frame(2)[0] == ("    repository 9", True, ""), viewer.frame(2))
    viewer.handle(curses.KEY_DOWN, 2)
    expect(viewer.frame(2)[-1][1], viewer.frame(2))
    viewer.handle(curses.KEY_UP, 2)
    expect(viewer.frame(2)[0] == ("    repository 9", True, ""), viewer.frame(2))
    viewer.handle("\x06", 2)
    viewer.handle("j", 2)
    expect(viewer.frame(2)[-1] == ("    repository 11", True, ""), viewer.frame(2))
    viewer.handle("h", 2)
    expect(viewer.selected == 0, viewer.selected)
    viewer.handle(curses.KEY_LEFT, 2)
    expect(viewer.frame(2) == [("▸ home", True, "")], viewer.frame(2))
    viewer.handle(curses.KEY_RIGHT, 2)
    viewer.handle("g", 2)
    viewer.handle("k", 2)
    expect(viewer.selected == 0, viewer.selected)


def test_viewer_pans_and_displays_control_characters_as_text() -> None:
    """Long labels remain accessible and source text cannot inject terminal controls."""
    viewer = test_explorer.Viewer(test_explorer.Node("hello\nworld\x1b[31m"))
    label = "  hello\\nworld\\x1b[31m"
    expect(viewer.frame(1) == [(label, True, "")], viewer.frame(1))
    viewer.handle("L", 1)
    viewer.handle("L", 1)
    expect(viewer.frame(1) == [(label[8:], True, "")], viewer.frame(1))
    viewer.handle("H", 1)
    expect(viewer.frame(1) == [(label[4:], True, "")], viewer.frame(1))
    viewer.handle("0", 1)
    viewer.handle("H", 1)
    expect(viewer.frame(1) == [(label, True, "")], viewer.frame(1))


@pytest.fixture
def screen(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Provide a terminal window for browser interactions."""
    window = Mock(spec=curses.window)
    window.getmaxyx.return_value = (24, 80)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    return window


@pytest.mark.parametrize(
    ("palette", "modified"), [(1 << 24, 0xFF8700), (256, 208), (8, curses.COLOR_YELLOW)]
)
def test_selected_additions_and_removals_keep_their_colors_when_highlighted(
    screen: Mock, monkeypatch: pytest.MonkeyPatch, palette: int, modified: int
) -> None:
    """Keep addition and removal colors visible when either row is selected."""
    screen.get_wch.side_effect = ["j", "j", "q"]
    monkeypatch.setattr(curses, "has_colors", lambda: True)
    monkeypatch.setattr(curses, "use_default_colors", lambda: None)
    monkeypatch.setattr(curses, "COLORS", palette, raising=False)
    pairs = Mock()
    monkeypatch.setattr(curses, "init_pair", pairs)
    monkeypatch.setattr(curses, "color_pair", lambda pair: pair << 8)
    tree = test_explorer.Node("home", expanded=True)
    test_explorer.add_tests(
        tree,
        None,
        {
            "description_changed": False,
            "packages": [
                {
                    "package": "new",
                    "description": "",
                    "status": "added",
                    "added": [],
                    "removed": [],
                },
                {
                    "package": "old",
                    "description": "",
                    "status": "removed",
                    "added": [],
                    "removed": [],
                },
            ],
        },
    )
    test_explorer.browse(screen, tree, "diff", Mock())
    pairs.assert_has_calls(
        [
            call(1, curses.COLOR_GREEN, -1),
            call(2, curses.COLOR_RED, -1),
            call(3, modified, -1),
        ]
    )
    for label, pair in (("+ new", 1), ("- old", 2)):
        styles = {
            args.args[-1]
            for args in screen.addnstr.call_args_list
            if args.args[2].strip() == label
        }
        color = curses.color_pair(pair)
        expect(styles == {color, color | curses.A_BOLD | curses.A_UNDERLINE}, styles)


def test_refresh_shows_current_tests_and_diffs_and_keeps_the_selected_test(
    screen: Mock,
) -> None:
    """Follow the highlight as rows move, then remove the selected test."""
    tree = test_explorer.Node("home", expanded=True)
    tree.add("package").add_test("test_result")
    updated = test_explorer.Node("home", expanded=True)
    updated.add("another package")
    package = updated.add("package")
    package.add_test("testResult")
    package.add_test("test_result")
    tests = test_explorer.Node("current tests", expanded=True)
    tests.add("obsolete test")
    empty = test_explorer.Node("no remaining tests")
    results = {"diff": iter([tree, updated]), "tests": iter([tests, empty])}
    screen.erase.side_effect = screen.addnstr.reset_mock

    def selected_label() -> str:
        rows = screen.addnstr.call_args_list[:-1]
        (label,) = [
            str(row.args[2]).strip() for row in rows if row.args[4] & curses.A_REVERSE
        ]
        return label

    def keys() -> Iterator[str]:
        footer = screen.addnstr.call_args.args[2]
        expect("r refresh" in footer, footer)
        yield "d"
        yield "/"
        yield "r"
        expect(screen.addnstr.call_args.args[2] == "/r", screen.addnstr.call_args)
        yield "\n"
        yield "r"
        rows = screen.addnstr.call_args_list
        expect(any(row.args[2].strip() == "another package" for row in rows), rows)
        expect(
            [
                bool(row.args[4] & curses.A_REVERSE)
                for row in rows
                if row.args[2].strip() == "result"
            ]
            == [False, True],
            rows,
        )
        yield "d"
        expect(selected_label() == "▾ current tests", screen.addnstr.call_args_list)
        yield "j"
        expect(selected_label() == "obsolete test", screen.addnstr.call_args_list)
        yield "r"
        expect(selected_label() == "no remaining tests", screen.addnstr.call_args_list)
        yield "q"

    screen.get_wch.side_effect = keys()
    test_explorer.browse(
        screen,
        test_explorer.Node("initial tests"),
        "tests",
        lambda view, _report: next(results[view]),
    )


def test_switching_to_diff_shows_repository_progress_until_the_view_is_ready(
    home: Path, screen: Mock
) -> None:
    """Show progress before discovery and after each repository, then reuse the view."""
    screen.get_wch.side_effect = ["d", "d", "d", "q"]
    frames: list[str] = []
    screen.refresh.side_effect = lambda: frames.append(screen.addnstr.call_args.args[2])

    def load(
        view: test_explorer.View, report: Callable[[test_explorer.Progress], None]
    ) -> test_explorer.Node:
        expect(frames[-1] == "Loading diff…", frames)
        return test_explorer.build_tree(
            home, view=view, cache=home / "cache", progress=report
        )

    test_explorer.browse(screen, test_explorer.Node("home"), "tests", load)
    expect(
        frames[1:-3]
        == [
            "Loading diff…",
            "Loading diff… 0/2",
            "Loading diff… 1/2",
            "Loading diff… 2/2",
        ],
        frames,
    )
    expect(
        [line.split()[0] for line in frames[-3:]] == ["diff", "tests", "diff"],
        frames,
    )


def test_opening_a_home_checkout_lets_the_reader_find_a_test_by_its_words(
    home: Path, screen: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Open the command, type a search and read the selected rule under its package."""
    checkout = home / "github.com/owner/project"
    (home / "flake.nix").write_text('{ description = "Development checkouts"; }')
    (checkout / "flake.nix").write_text(
        '{ inputs.fake.description = "Dependency"; description = "Project tools"; }'
    )
    (checkout / "example/test_main.py").write_text(
        "def test_keeps_saved_notes(): pass\n"
    )
    git(checkout, "add", "example/test_main.py")
    monkeypatch.setenv("XDG_CACHE_HOME", str(home.parent / "cache"))
    monkeypatch.setattr(sys, "argv", ["test-explorer", "--home", str(home)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(curses, "wrapper", lambda browse, *args: browse(screen, *args))
    screen.erase.side_effect = screen.addnstr.reset_mock
    screen.get_wch.side_effect = [*"/saved notes\n", "q"]

    test_explorer.main()

    rows = screen.addnstr.call_args_list
    labels = [row.args[2].strip() for row in rows]
    expect(f"▾ {home}: Development checkouts" in labels, labels)
    expect(
        "▾ project: Project tools" in labels and "▾ example (1): Example" in labels,
        labels,
    )
    selected = [
        row.args[2].strip() for row in rows[:-1] if row.args[4] & curses.A_REVERSE
    ]
    expect(selected == ["keeps saved notes"], selected)


@pytest.mark.parametrize("view", ["tests", "diff"])
def test_a_changed_flake_description_highlights_only_its_repository_and_ancestors(
    home: Path, view: test_explorer.View
) -> None:
    """Compare the displayed description even when every package stays the same."""
    checkout = home / "github.com/owner/project"
    flake = checkout / "flake.nix"
    cache = home.parent / "cache"
    flake.write_text('{ description = "Project tools"; }')
    tree = test_explorer.build_tree(home, view=view, cache=cache)
    viewer = test_explorer.Viewer(tree)
    expect(viewer.search("Project tools"), tree)
    rows = {row.strip(): color for row, _, color in viewer.frame(20)}
    expect(
        rows["▾ " + str(home)] == rows["▾ github.com"] == rows["▾ owner"] == "~",
        rows,
    )
    expect(
        any(
            selected and row.endswith("project: Project tools") and color == "~"
            for row, selected, color in viewer.frame(20)
        ),
        rows,
    )
    if view == "tests":
        expect(viewer.search("example"), tree)
        expect(
            any(
                selected and not color and "example (1): Example" in row
                for row, selected, color in viewer.frame(20)
            ),
            tree,
        )
    git(checkout, "add", "flake.nix")
    flake.write_text('{ description = "Project tools"; } # a comment\n')
    refreshed = test_explorer.build_tree(home, view=view, cache=cache)
    expect(not refreshed.change, refreshed)


def test_package_descriptions_follow_edits_and_removals_in_the_explorer(
    home: Path,
) -> None:
    """Refresh a selected test and show the staged description for a removed package."""
    checkout = home / "github.com/owner/project"
    declaration = checkout / "example/default.nix"
    cache = home.parent / "cache"
    viewer = test_explorer.Viewer(test_explorer.build_tree(home, cache=cache))
    expect(viewer.search("test_default_output"), viewer.root)
    declaration.write_text(
        "{ inputs, ... }: let summary = ''Read stored\n  notes''; in "
        "inputs.self.lib.mkPythonPackage (_: { meta = { description = summary; }; })"
    )
    viewer.refresh(test_explorer.build_tree(home, cache=cache))
    rows = viewer.frame(20)
    expect(
        any(selected and row.strip() == "default output" for row, selected, _ in rows),
        rows,
    )
    expect(
        any(
            row.strip() == "▾ example (1): Read stored notes" and color == "~"
            for row, _, color in rows
        ),
        rows,
    )
    expect(viewer.search("read stored"), rows)
    expect(
        any(
            selected and "example (1): Read stored notes" in row
            for row, selected, _ in viewer.frame(20)
        ),
        viewer.root,
    )
    diff = test_explorer.build_tree(home, view="diff", cache=cache)
    description_diff = test_explorer.Viewer(diff)
    expect(description_diff.search("Read stored notes"), diff)
    expect(
        any(
            selected and "example: Read stored notes" in row and color == "~"
            for row, selected, color in description_diff.frame(20)
        ),
        diff,
    )
    git(checkout, "add", "example/default.nix")
    declaration.unlink()
    diff_viewer = test_explorer.Viewer(
        test_explorer.build_tree(home, view="diff", cache=cache)
    )
    expect(diff_viewer.search("Read stored notes"), diff_viewer.root)
    expect(
        any(
            selected
            and row.strip() == "▸ - example: Read stored notes"
            and color == "-"
            for row, selected, color in diff_viewer.frame(20)
        ),
        diff_viewer.root,
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Stage a flake in the test's temporary repository."""
    git(tmp_path, "init", "--quiet")
    (tmp_path / "flake.nix").write_text("{}\n")
    (tmp_path / "flake.lock").touch()
    git(tmp_path, "add", ".")
    return tmp_path


def test_inspection_lists_test_names_in_order_even_when_the_implementation_is_missing(
    checkout: Path,
) -> None:
    """List declarations without importing the suite or requiring an implementation."""
    package = checkout / "example"
    (package / "main.py").unlink()
    (package / "test_main.py").write_text(
        "import pytest\n"
        "raise RuntimeError('inspection must not import this suite')\n"
        "@pytest.mark.parametrize('message', ['abc', 'ABC'])\n"
        "def test_preserves_message_case(message): assert deliver(message) == message\n"
        "def test_delivers_messages(): assert deliver('hello') == 'hello'\n"
    )
    names = suite_inventory.inspect_packages(checkout)["example"]["tests"]
    expect(names == ["test_delivers_messages", "test_preserves_message_case"], names)


def test_each_package_lists_a_shared_test_name_once(root: Path) -> None:
    """Keep equal identifiers distinct across packages and unique within a suite."""
    for name in ("Mail_Box-v2", "Mail-Box_v2"):
        tests = _python_package(root, name) / "test_main.py"
        tests.write_text("def test_delivers(): ...\ndef test_delivers(): ...\n")
    git(root, "add", ".")
    inventory = suite_inventory.inspect_packages(root)
    expect(list(inventory) == ["Mail-Box_v2", "Mail_Box-v2"], inventory)
    expect(
        all(info["tests"] == ["test_delivers"] for info in inventory.values()),
        inventory,
    )
    with pytest.raises(ValueError, match="module names collide"):
        flake_layout.validate_flake_source(root)


@pytest.mark.parametrize(
    ("entrypoint", "constructor"),
    [
        ("main.sh", "inputs.afairesi.lib.mkBashPackage"),
        ("index.html", "inputs.self.lib.mkHtmlPackage"),
        ("ms.tex", "inputs.afairesi.lib.mkLatexPackage"),
        ("main.lean", "(inputs.self).lib.mkLean4Package"),
        ("main.py", "let build = inputs.self.lib.mkPythonPackage; in (build)"),
    ],
)
def test_declared_package_kind_determines_the_required_entrypoint(
    root: Path,
    entrypoint: str,
    constructor: str,
) -> None:
    """Keep the declared build kind when supporting files resemble entrypoints."""
    package = root / "example"
    package.mkdir()
    executable = "executable = true;" if entrypoint in {"main.lean", "main.py"} else ""
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: let dependency = inputs.self.lib.mkHtmlPackage {}; in "
        f"{constructor} {{ inherit pkgs; src = ./.; {executable} }}",
    )
    if entrypoint != "main.py":
        (package / "test_main.py").write_text("def test_unrelated(): pass")
    for filename in ("main.sh", "index.html", "ms.tex", "main.lean", "main.nix"):
        (package / filename).touch()
    (package / "main.py").write_text("def main(): pass\n")
    git(root, "add", "--", "example")
    names = suite_inventory.inspect_packages(root)["example"]["tests"]
    expect(not names, names)
    flake_layout.validate_flake_source(root)
    (package / entrypoint).unlink()
    with pytest.raises(ValueError, match=re.escape(entrypoint)):
        flake_layout.validate_flake_source(root)


def test_packages_with_custom_builders_stay_generic_regardless_of_source_files(
    root: Path,
) -> None:
    """Discover Nix suites for native and opaque builders with mixed source files."""
    builders = {
        "class": "pkgs.python3Packages.buildPythonPackage",
        "dash-name": "pkgs.rustPlatform.buildRustPackage",
        "dash_name": 'pkgs.haskellPackages.callCabal2nix "example" ./.',
        "custom": "import ./builder.nix",
        "scoped": "with pkgs; writeTextFile",
    }
    for name, builder in builders.items():
        package = root / name
        package.mkdir()
        (package / "default.nix").write_text(
            f"{{ pkgs, ... }}: {builder} {{}}\n",
        )
        (package / "test_main.nix").write_text(
            "{ testBuilds = { expr = 1; expected = 1; }; }"
        )
        (package / "__init__.py").write_text("VALUE = 42\n")
        (package / "main.py").write_text("not valid Python!\n")
        (package / "test_main.py").write_text("not valid Python tests!\n")
        for marker in ("main.lean", "ms.tex", "index.html"):
            (package / marker).write_text("Supporting source\n")
    git(root, "add", ".")
    flake_layout.validate_flake_source(root)
    inventory = suite_inventory.inspect_packages(root)
    expect(
        inventory
        == {name: {"description": "", "tests": ["testBuilds"]} for name in builders},
        inventory,
    )


def _python_package(root: Path, name: str) -> Path:
    package = root / name
    package.mkdir()
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: inputs.self.lib.mkPythonPackage { "
        "inherit pkgs; src = ./.; executable = true; "
        'meta.description = "Example"; }',
    )
    (package / "main.py").write_text("def main(): pass\n")
    return package


def _inspection_repository(root: Path) -> None:
    (root / "flake.nix").write_text("{}\n")
    _python_package(root, "example")
    _write_test(root, "default_output")


def test_package_names_follow_their_tracked_declarations(
    root: Path,
) -> None:
    """Publish a tracked package and carry its test names through a rename."""
    package = root / "draft"
    package.mkdir()
    (package / "default.nix").write_text("{}")
    (package / "test_main.nix").write_text(
        "{ testDelivers = { expr = 1; expected = 1; }; }"
    )
    git(root, "add", "--", "draft/test_main.nix")
    expect(
        suite_inventory.inspect_packages(root) == {},
        "untracked declaration was listed",
    )
    git(root, "add", "--", "draft/default.nix")
    expect(
        suite_inventory.inspect_packages(root)
        == {"draft": {"description": "", "tests": ["testDelivers"]}},
        package,
    )
    git(root, "mv", "--", "draft", "δοκιμή.v2 + inbox")
    expect(
        suite_inventory.inspect_packages(root)
        == {"δοκιμή.v2 + inbox": {"description": "", "tests": ["testDelivers"]}},
        root,
    )


def test_library_tests_are_listed_even_when_main_requires_arguments(
    tmp_path: Path,
) -> None:
    """Only executables require a main function callable without arguments."""
    _inspection_repository(tmp_path)
    (tmp_path / "flake.lock").touch()
    package = tmp_path / "example"
    declaration = package / "default.nix"
    declaration.write_text(
        """
{ inputs, pkgs, ... }:
let options = {
    inherit pkgs;
    executable = false;
    src = ./.;
}; in inputs.self.lib.mkPythonPackage options
""",
    )
    source = package / "main.py"
    source.write_text(source.read_text().replace("def main():", "def main(values):"))
    flake_layout.validate_flake_source(tmp_path)
    names = suite_inventory.inspect_packages(tmp_path)["example"]["tests"]
    expect(names == ["test_default_output"], names)
    declaration.write_text(
        declaration.read_text().replace("executable = false", "executable = true")
    )
    with pytest.raises(
        ValueError,
        match="main must accept a call without arguments",
    ):
        flake_layout.validate_flake_source(tmp_path)


def _indexed_repository(root: Path) -> None:
    """Stage the sources used as the diff baseline."""
    root.mkdir(parents=True, exist_ok=True)
    _inspection_repository(root)
    git(root, "init", "--quiet")
    git(root, "add", "--force", ".")


def _write_test(root: Path, name: str) -> None:
    """Give the fixture's sole Python test a new name."""
    (root / "example/test_main.py").write_text(f"def test_{name}(): pass\n")


def test_declared_auxiliary_suites_list_their_tracked_and_registered_behaviors(
    checkout: Path,
) -> None:
    """Follow declared suite sources through inspection, cache refresh and diffs."""
    package = checkout / "example"
    nested = package / "nested"
    nested.mkdir()
    python = nested / "test_rules.py"
    python.write_text("def test_pdf_output(): pass\n")
    cpp = package / "count-test.cc"
    cpp.write_text(
        "static void test_unused() {}\nstatic void test_cpu_limit() {}\n"
        "int main() { test_cpu_limit(); }\n"
    )
    (package / "default.nix").write_text(
        "{ checks = [ ./nested/test_rules.py ./count-test.cc ./test_local.py ]; }"
    )
    (package / "test_local.py").write_text("def test_untracked(): pass\n")
    (nested / "test_fixture.py").write_text("def test_fixture(): pass\n")
    git(
        checkout,
        "add",
        "example/default.nix",
        "example/nested",
        "example/count-test.cc",
    )
    cache = checkout.parent / "cache"
    expected = {
        "example": {
            "description": "",
            "tests": [
                "count-test.cc::test_cpu_limit",
                "nested/test_rules.py::test_pdf_output",
            ],
        }
    }
    first, _ = test_explorer.cached_output(checkout, "tests", cache, refresh=False)
    expect(first == expected, first)
    diff = suite_inventory.compare_repository(checkout)
    expect(diff == {"description_changed": False, "packages": []}, diff)
    python.write_text("def test_openapi_schema(): pass\n")
    current, hit = test_explorer.cached_output(checkout, "tests", cache, refresh=False)
    expect(
        not hit
        and current
        == {
            "example": {
                "description": "",
                "tests": [
                    "count-test.cc::test_cpu_limit",
                    "nested/test_rules.py::test_openapi_schema",
                ],
            }
        },
        current,
    )
    display = test_explorer.Node("example")
    display.add_test("test_openapi_schema")
    expect(labels(display) == ["OpenAPI schema"], display)
    changes = suite_inventory.compare_repository(checkout)["packages"]
    expect(
        changes
        == [
            {
                "package": "example",
                "description": "",
                "status": "modified",
                "removed": ["nested/test_rules.py::test_pdf_output"],
                "added": ["nested/test_rules.py::test_openapi_schema"],
            }
        ],
        changes,
    )


def test_diff_follows_tracked_symlink_targets_of_test_suites(checkout: Path) -> None:
    """Keep deeply nested symlink targets in both snapshots."""
    source = checkout / "example/test_main.py"
    target = checkout / "assets/deep/entry"
    target.parent.mkdir(parents=True)
    source.rename(target)
    source.symlink_to("../assets/deep/entry")
    (target.parent / "image.bin").write_bytes(b"\xff\x00\r\n")
    git(checkout, "add", ".")
    target.write_text("def test_unicode_input(): ...\ndef test_empty_input(): ...\n")
    changes = suite_inventory.compare_repository(checkout)["packages"]
    expect(
        changes
        == [
            {
                "package": "example",
                "description": "Example",
                "status": "modified",
                "added": ["test_empty_input", "test_unicode_input"],
                "removed": ["test_default_output"],
            }
        ],
        changes,
    )


def test_removing_a_package_drops_its_entry_even_when_it_shares_a_host(
    root: Path,
) -> None:
    """Drop deleted declarations and keep diagnosing them in layout validation."""
    shared = root / "shared"
    shared.mkdir()
    (shared / "configuration.nix").write_text("{ ... }: {}")
    (shared / "default.nix").write_text("{}")
    git(root, "add", "--", "shared")
    (shared / "default.nix").unlink()
    expect(suite_inventory.inspect_packages(root) == {}, shared)
    changes = suite_inventory.compare_repository(root)["packages"]
    expect(
        changes
        == [
            {
                "package": "shared",
                "description": "",
                "status": "removed",
                "added": [],
                "removed": [],
            }
        ],
        changes,
    )
    with pytest.raises(
        ValueError,
        match=r"shared/default\.nix: missing required tracked regular file",
    ):
        flake_layout.validate_flake_source(root)


def test_a_failed_test_name_comparison_removes_its_temporary_snapshots(
    checkout: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean failed snapshots when a tracked source cannot be inspected."""
    scratch = checkout.parent / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    (checkout / "example/test_main.py").write_text("def invalid(")
    with pytest.raises(ValueError, match="could not parse Python source"):
        suite_inventory.compare_repository(checkout)
    expect(not list(scratch.iterdir()), "failed diff left temporary sources")


def test_diff_reports_unresolved_merge_conflicts_and_leaves_the_index_untouched(
    checkout: Path,
) -> None:
    """Report the conflicting index before producing a test-name diff."""
    blob = git(checkout, "rev-parse", ":example/main.py").strip()
    flake_layout.git_bytes(
        checkout,
        "update-index",
        "--index-info",
        stdin=(
            "0 "
            + "0" * len(blob)
            + "\texample/main.py\n"
            + f"100644 {blob} 1\texample/main.py\n"
            + f"100644 {blob} 2\texample/main.py\n"
            + f"100644 {blob} 3\texample/main.py\n"
        ).encode(),
    )
    index = checkout / ".git/index"
    before = index.read_bytes()
    with pytest.raises(ValueError, match="unmerged index"):
        suite_inventory.compare_repository(checkout)
    expect(index.read_bytes() == before, "diff modified the conflicted index")


def test_diff_reads_the_selected_linked_worktree_without_modifying_it(
    checkout: Path,
) -> None:
    """Compare the linked worktree's staged and working sources without edits."""
    linked = checkout.parent / "linked"
    commit(checkout, "initial")
    git(checkout, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    _write_test(linked, "staged")
    git(linked, "add", "example/test_main.py")
    _write_test(linked, "working")
    paths = [
        checkout / ".git/index",
        linked / git(linked, "rev-parse", "--git-path", "index").strip(),
        checkout / "example/test_main.py",
        linked / "example/test_main.py",
    ]
    before = [path.read_bytes() for path in paths]
    changes = suite_inventory.compare_repository(linked)["packages"]
    expect(
        changes
        == [
            {
                "package": "example",
                "description": "Example",
                "status": "modified",
                "added": ["test_working"],
                "removed": ["test_staged"],
            }
        ],
        changes,
    )
    expect([path.read_bytes() for path in paths] == before, "diff modified files")


def test_inspection_omits_untracked_packages_and_test_files(tmp_path: Path) -> None:
    """Skip malformed local declarations and suites until they are tracked."""
    git(tmp_path, "init", "--quiet")
    (tmp_path / "flake.nix").write_text("{}")
    (tmp_path / "flake.lock").touch()
    _python_package(tmp_path, "example")
    (tmp_path / "library").mkdir()
    (tmp_path / "library/default.nix").write_text("{}")
    diff = suite_inventory.compare_repository(tmp_path)
    expect(diff == {"description_changed": False, "packages": []}, diff)
    git(tmp_path, "add", ".")
    (tmp_path / "example/test_main.py").write_text("def broken(:")
    (tmp_path / "library/test_main.nix").write_text("{ invalid")
    local = tmp_path / "experiments"
    local.mkdir()
    (local / "default.nix").write_text("{ invalid")
    flake_layout.validate_flake_source(tmp_path)
    inventory = suite_inventory.inspect_packages(tmp_path)
    expect(
        inventory
        == {
            "example": {"description": "Example", "tests": []},
            "library": {"description": "", "tests": []},
        },
        inventory,
    )


def test_rust_inventory_follows_enabled_cargo_targets_and_tracked_modules(
    root: Path,
) -> None:
    """List declared unit and integration tests without building the crate."""
    package = root / "example"
    sources = {
        "default.nix": "{}",
        "Cargo.toml": """[package]
name = "example"
edition = "2024"
autotests = false
[[test]]
name = "behavior"
path = "cases/output.rs"
[[bin]]
name = "tool"
test = false
""",
        "src/lib.rs": (
            '#[cfg(test)]\nmod tests;\nconst TEXT: &str = "#[test] fn fake() {}";\n'
        ),
        "src/tests.rs": (
            '#[path = "shared.rs"] mod shared;\n#[test]\nfn accepts_input() {}\n'
        ),
        "src/shared.rs": "mod nested { #[test] fn preserves_spaces() {} }\n",
        "src/bin/tool.rs": "#[test] fn disabled() {}\n",
        "cases/output.rs": "#[test] /* a comment */ #[ignore] fn prints_output() {}\n",
        "tests/unused.rs": "#[test] fn ignored_target() {}\n",
        "src/orphan.rs": "#[test] fn unreachable() {}\n",
        "build.rs": 'fn main() { panic!("do not build"); }\n',
    }
    for filename, source in sources.items():
        path = package / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    git(root, "add", "--", "example")
    (package / "src/bin/untracked.rs").write_text("#[test] fn untracked() {}\n")
    names = suite_inventory.inspect_packages(root)["example"]["tests"]
    expect(
        names
        == [
            "prints_output",
            "tests::accepts_input",
            "tests::shared::nested::preserves_spaces",
        ],
        names,
    )
    suite = package / "src/shared.rs"
    suite.write_text(suite.read_text().replace("preserves", "retains"))
    changes = suite_inventory.compare_repository(root)["packages"]
    expect(
        changes
        == [
            {
                "package": "example",
                "description": "",
                "status": "modified",
                "added": ["tests::shared::nested::retains_spaces"],
                "removed": ["tests::shared::nested::preserves_spaces"],
            }
        ],
        changes,
    )


def test_node_packages_list_and_diff_tracked_builtin_runner_cases(root: Path) -> None:
    """Discover ESM and CommonJS bindings, modifiers and nested test files."""
    package = root / "registry"
    nested = package / "test"
    nested.mkdir(parents=True)
    (package / "default.nix").write_text(
        "{ pkgs, ... }: pkgs.buildNpmPackage { "
        "checkPhase = ''node --test ${./test.mjs}''; }"
    )
    (package / "package.json").write_text("{}")
    suite = package / "test.mjs"
    suite.write_text(
        """import run, {test as check, it, before} from 'node:test';
import * as suite from 'node:test';
import {test} from 'other';
run("default", () => {});
check.only('aliased', () => {});
it.skip(`skipped`, () => {});
suite.test.todo('pending');
suite.it('nested', () => {});
before('hook'); test('unrelated'); check(variable); it(`dynamic ${value}`);
"""
    )
    (nested / "api.cjs").write_text(
        """const {test, it: example} = require("node:test");
const runner = require("node:test");
const {it: unrelated} = other("node:test");
test("commonjs"); example("destructured"); runner.it("method"); unrelated("ignored");
"""
    )
    dependency = package / "node_modules/dependency/test.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("import t from 'node:test'; t('dependency case')")
    git(root, "add", "--force", str(dependency))
    git(root, "add", ".")
    (package / "untracked.test.js").write_text("import t from 'node:test'; t('local')")
    expected = sorted(
        [
            "default",
            "aliased",
            "skipped",
            "pending",
            "nested",
            "commonjs",
            "destructured",
            "method",
        ]
    )
    inventory = suite_inventory.inspect_packages(root)
    expect(inventory["registry"]["tests"] == expected, inventory)
    suite.write_text(suite.read_text().replace("aliased", "renamed"))
    changes = suite_inventory.compare_repository(root)["packages"]
    expect(len(changes) == 1, changes)
    expect(
        changes[0]["added"] == ["renamed"] and changes[0]["removed"] == ["aliased"],
        changes,
    )


@pytest.mark.parametrize(
    "discovery",
    [
        """check = ''
          python -B -m pytest --import-mode=importlib \\
            ${./nested}
        '';""",
        """fileset = lib.fileset.fileFilter
          (file: file.hasExt "py" && lib.hasPrefix "test_" file.name) ./nested;""",
    ],
)
def test_directory_checks_discover_tracked_suites_and_report_removed_tests(
    checkout: Path, discovery: str
) -> None:
    """Discover tracked suites in directory-based checks and keep their identities."""
    package = checkout / "example"
    nested = package / "nested"
    nested.mkdir()
    for filename in ("test_first.py", "test_second.py"):
        (nested / filename).write_text("def test_shared(): pass\n")
    (nested / "test_disabled.py").write_text(
        "__test__ = False\ndef test_hidden(): pass\n"
    )
    (package / "test_metadata.py").write_text("def test_metadata(): pass\n")
    (package / "default.nix").write_text(
        f"let unused = ./test_metadata.py; in {{ {discovery} "
        "meta.example = ./test_metadata.py; passthru.example = ./test_metadata.py; }"
    )
    git(checkout, "add", "example")
    (nested / "test_untracked.py").write_text("def test_untracked(): pass\n")
    names = ["nested/test_first.py::test_shared", "nested/test_second.py::test_shared"]
    expect(
        suite_inventory.inspect_packages(checkout)["example"]["tests"] == names, names
    )
    (nested / "test_first.py").unlink()
    changes = suite_inventory.compare_repository(checkout)["packages"]
    expect(len(changes) == 1 and changes[0]["removed"] == names[:1], changes)
    expect(changes[0]["added"] == [], changes)


@pytest.mark.parametrize("ignore", ["--ignore=", "--ignore "])
def test_pytest_directory_ignores_and_alternative_names(
    checkout: Path, ignore: str
) -> None:
    """Respect explicit pytest ignores and recognize both default Python patterns."""
    package = checkout / "example"
    nested = package / "nested"
    nested.mkdir()
    (nested / "views_test.py").write_text("def test_visible(): pass\n")
    (nested / "test_ignored.py").write_text("def test_ignored(): pass\n")
    (package / "default.nix").write_text(
        "{ check = ''pytest " + ignore + "${./nested/test_ignored.py} ${./nested}''; }"
    )
    git(checkout, "add", "example")
    names = suite_inventory.inspect_packages(checkout)["example"]["tests"]
    expect(names == ["nested/views_test.py::test_visible"], names)
