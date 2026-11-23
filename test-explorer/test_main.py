# Copyright (c) 2026- Paschalis Bizopoulos
"""Exercise home discovery, directory grouping, and test-name diffs."""

import curses
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING
from unittest.mock import Mock, call

import pytest
import test_explorer as subject
from test_explorer import test_inventory

import flake_layout

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


_ENTRYPOINTS = {
    "bash": ("main.sh", "echo hello\n"),
    "html": ("index.html", "<!doctype html><html><body></body></html>\n"),
    "latex": (
        "ms.tex",
        "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n",
    ),
    "lean4": ("main.lean", "def main : IO Unit := pure ()\n"),
    "python": ("main.py", "def main():\n    pass\n"),
}


def expect(condition: bool, detail: object) -> None:  # noqa: FBT001
    """Report an unexpected result with its observed value."""
    if not condition:
        raise AssertionError(detail)


def labels(node: subject.Node, *path: str) -> list[str]:
    """Read the child labels at a displayed tree path."""
    for name in path:
        node = next(child for child in node.children if child.label == name)
    return [child.label for child in node.children]


def git(root: Path, *arguments: str) -> str:
    """Run Git only against isolated test repositories."""
    return subprocess.run(  # noqa: S603 - Test-owned argument vectors.
        ["git", "-C", str(root), *arguments],  # noqa: S607
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    ).stdout.strip()


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


@pytest.fixture(name="checkout")
def checkout_fixture(tmp_path: Path) -> Path:
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
        f"160000,{git(child, 'rev-parse', 'HEAD')},{path}",
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
    tree = subject.build_tree(home, cache=home.parent / "cache")
    expect(labels(tree) == ["broken", "github.com", "gitlab.com", "missing"], tree)
    project = ("github.com", "owner", "project")
    expect(labels(tree, *project, "example") == ["- staged", "+ working"], tree)
    expect(labels(tree, *project) == ["example"], tree)
    viewer = subject.Viewer(tree)
    expect(viewer.search("test_working"), tree)
    colors = {text.strip(): color for text, _, color in viewer.frame(30)}
    expect(
        all(
            colors["▾ " + label] == "~"
            for label in (str(home), *project, "example (1)")
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
    """Stage Bats, Nix, Lean and Rust suites in one checkout."""
    checkout = home / "github.com/owner/project"
    suites = {
        "shell": (
            "mkBashPackage",
            "test_main.bats",
            '@test "preserves spaces" { true; }',
        ),
        "nix": (
            "",
            "test_main.nix",
            "{ testHTTPRequestsUseAHeader = { expr = 1; expected = 1; }; }",
        ),
        "proof": (
            "mkLean4Package",
            "test_main.lean",
            "namespace Output\ntheorem gardeÉtiquette : True := trivial\nend Output",
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
    git(checkout, "add", "--", *suites)
    return checkout


@pytest.mark.usefixtures("mixed_language_checkout")
def test_bats_nix_lean_and_rust_test_names_are_searchable_under_their_packages(
    home: Path,
) -> None:
    """Search each suite's displayed name and select it under its package."""
    cache = home.parent / "cache"
    tree = subject.build_tree(home, cache=cache)
    viewer = subject.Viewer(tree)
    project = ("github.com", "owner", "project")
    for package, name in {
        "shell": "preserves spaces",
        "nix": "HTTP requests use a header",
        "proof": "Output.garde étiquette",
        "crate": "output::keeps name",
    }.items():
        expect(labels(tree, *project, package) == [name], package)
        expect(viewer.search(name), name)
        frame = viewer.frame(40)
        expect(
            any(selected and row.strip() == name for row, selected, _ in frame), frame
        )
        expect(
            any(row.strip() == f"▾ {package} (1)" for row, _, _ in frame),
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
    cache = home.parent / "cache"
    project = ("github.com", "owner", "project")
    subject.cached_output(checkout, "diff", cache, refresh=False)
    (checkout / "shell/test_main.bats").write_text(
        '@test "preserves tabs" { true; }\n@test "accepts blanks" { true; }\n'
    )
    for package in ("crate", "proof", "untested"):
        (checkout / package / "default.nix").unlink()
    changes = subject.build_tree(home, view="diff", cache=cache)
    expect("gitlab.com" not in labels(changes), changes)
    expect(labels(changes, *project, "- crate") == ["- output::keeps name"], changes)
    expect(
        labels(changes, *project, "shell")
        == ["- preserves spaces", "+ accepts blanks", "+ preserves tabs"],
        changes,
    )
    expect(
        labels(changes, *project, "- proof") == ["- Output.garde étiquette"], changes
    )
    expect("- untested" in labels(changes, *project), changes)
    tests = subject.Viewer(subject.build_tree(home, cache=cache))
    expect(tests.search("+ preserves tabs"), tests.root)
    rows = {row.strip(): color for row, _, color in tests.frame(40)}
    expect(rows["▾ shell (2)"] == "~" and rows["▸ - crate (0)"] == "-", rows)
    expect(rows["- preserves spaces"] == "-" and rows["+ preserves tabs"] == "+", rows)


def test_home_tree_reports_a_timeout_and_shows_current_status_for_a_cached_repository(
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show cached test names and live Git status alongside a checkout timeout."""
    cache = home.parent / "cache"
    cached = home / "gitlab.com/owner/project"
    subject.cached_output(cached, "tests", cache, refresh=False)
    (cached / "notes.txt").touch()
    git(cached, "add", "notes.txt")
    original = test_inventory.inspect_repository

    def timeout(root: Path) -> dict[str, list[str]]:
        if root == home / "github.com/owner/project":
            raise subprocess.TimeoutExpired(["git", "ls-files"], 30)
        return original(root)

    monkeypatch.setattr(test_inventory, "inspect_repository", timeout)
    tree = subject.build_tree(
        home, cache=cache, progress=subject.write_startup_progress
    )
    expect(
        any(
            "timed out" in label
            for label in labels(tree, "github.com", "owner", "project")
        ),
        tree,
    )
    expect(
        labels(tree, "gitlab.com", "owner", "project", "example") == ["default output"],
        tree,
    )
    viewer = subject.Viewer(tree)
    expect(viewer.search("gitlab.com"), tree)
    expect(
        any(selected and color == "~" for _, selected, color in viewer.frame(8)), tree
    )
    progress = capsys.readouterr().err
    expect("2 direct submodules" in progress and "[2/2]" in progress, progress)
    expect("gitlab.com/owner/project: cached" in progress, progress)


def test_home_tree_requires_a_git_root_and_accepts_one_without_submodules(
    tmp_path: Path,
) -> None:
    """Show an empty valid home, but reject a nested directory or a non-repository."""
    cache = tmp_path / "cache"
    with pytest.raises(ValueError, match="not a git repository"):
        subject.build_tree(tmp_path, cache=cache)
    git(tmp_path, "init", "--quiet")
    tree = subject.build_tree(tmp_path, cache=cache)
    expect(
        tree.label == str(tmp_path)
        and labels(tree) == ["No direct registered submodules."],
        tree,
    )
    differences = subject.build_tree(tmp_path, cache=cache, view="diff")
    expect(labels(differences) == ["No package or test-name changes"], differences)
    child = tmp_path / "directory"
    child.mkdir()
    with pytest.raises(ValueError, match="repository root"):
        subject.build_tree(child, cache=cache)


@pytest.mark.parametrize("view", ["tests", "diff"])
def test_both_views_reuse_cached_results_until_their_test_inputs_change(
    checkout: Path,
    view: subject.View,
) -> None:
    """Use working names for the tests view and staged names as the diff baseline."""
    cache = checkout.parent / "cache"
    nested = checkout / "nested"
    add_submodule(checkout, nested)
    source = checkout / "example/test_main.py"
    source.write_text("def test_working(): ...\n")
    first, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(bool(first) and not hit, first)
    (checkout / "example/main.py").write_text("irrelevant")
    git(checkout, "add", "example/main.py")
    (nested / "example/test_main.py").write_text("def test_nested(): ...\n")
    again, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(hit and first == again, again)
    metadata = source.stat()
    source.write_text(source.read_text().replace("test_working", "test_updated"))
    os.utime(source, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
    changed, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(not hit and changed != first, changed)
    git(checkout, "add", "example/test_main.py")
    staged, hit = subject.cached_output(checkout, view, cache, refresh=False)
    if view == "tests":
        expect(hit and staged == changed == {"example": ["test_updated"]}, staged)
    else:
        expect(not hit and staged == [], staged)
    commit(checkout, "updated")
    committed, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(hit and committed == staged, committed)
    _, hit = subject.cached_output(checkout, view, cache, refresh=True)
    expect(not hit, "explicit refresh used cached data")
    git(checkout, "rm", "--cached", "example/test_main.py")
    untracked, _ = subject.cached_output(checkout, view, cache, refresh=False)
    expect(untracked == ({"example": []} if view == "tests" else []), untracked)


@pytest.mark.parametrize("view", ["tests", "diff"])
def test_views_are_reloaded_when_the_cache_is_damaged_or_unavailable(
    checkout: Path,
    view: subject.View,
) -> None:
    """Reload the view when cached JSON is malformed or storage is unavailable."""
    cache = checkout.parent / "cache"
    (checkout / "example/test_main.py").write_text("def test_updated(): ...\n")
    expected, _ = subject.cached_output(checkout, view, cache, refresh=False)
    entry = next(cache.glob("*.json"))
    saved = json.loads(entry.read_text())
    invalid = {"example": [None]} if view == "tests" else [{}]
    entry.write_text(json.dumps({**saved, "value": invalid}))
    actual, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(not hit and actual == expected, actual)
    entry.write_text("partial {")
    actual, hit = subject.cached_output(checkout, view, cache, refresh=False)
    expect(not hit and actual == expected, actual)
    blocked = checkout.parent / "blocked-cache"
    blocked.write_text("not a directory")
    actual, hit = subject.cached_output(checkout, view, blocked, refresh=False)
    expect(not hit and actual == expected, actual)


def test_the_next_inspection_retries_failed_or_unstable_results(
    checkout: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry errors and avoid persisting a result read while the source was changing."""
    cache = checkout.parent / "cache"
    flake = checkout / "flake.nix"
    flake.unlink()
    with pytest.raises(ValueError, match=r"flake\.nix"):
        subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not cache.exists(), "failed inspection was cached")
    flake.write_text("{}")
    original = test_inventory.inspect_repository

    def changing(path: Path) -> dict[str, list[str]]:
        value = original(path)
        (path / "example/test_main.py").write_text("def test_changed(): ...\n")
        return value

    monkeypatch.setattr(test_inventory, "inspect_repository", changing)
    subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not cache.exists(), "changing source was cached")
    monkeypatch.setattr(test_inventory, "inspect_repository", original)
    current, hit = subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not hit and current == {"example": ["test_changed"]}, current)


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
    subject.cached_output(checkout, "tests", cache, refresh=False)
    target.write_text("def test_other(): ...\n")
    value, hit = subject.cached_output(checkout, "tests", cache, refresh=False)
    expect(not hit and value == {"example": ["test_other"]}, value)


def test_directories_start_collapsed_and_can_be_expanded_with_the_keyboard() -> None:
    """Show meaningful labels and let h/l fold and unfold details without JSON noise."""
    root = subject.Node("/home/test", expanded=True)
    project = root.add("github.com").add("owner").add("project")
    subject.add_tests(project, {"hello": ["test_greets_the_reader"]})
    viewer = subject.Viewer(root)
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
    root = subject.Node("home", expanded=True)
    for number in range(12):
        root.add(f"repository {number}")
    viewer = subject.Viewer(root)
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
    viewer = subject.Viewer(subject.Node("hello\nworld\x1b[31m"))
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


@pytest.fixture(name="screen")
def screen_fixture(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Provide a terminal window for browser interactions."""
    screen = Mock(spec=curses.window)
    screen.getmaxyx.return_value = (24, 80)
    monkeypatch.setattr(curses, "has_colors", lambda: False)
    return screen


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
    tree = subject.Node("home", expanded=True)
    subject.add_tests(
        tree,
        None,
        [
            {"package": "new", "status": "added", "added": [], "removed": []},
            {"package": "old", "status": "removed", "added": [], "removed": []},
        ],
    )
    subject.browse(screen, tree, "diff", Mock())
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
    tree = subject.Node("home", expanded=True)
    tree.add("package").add_test("test_result")
    updated = subject.Node("home", expanded=True)
    updated.add("another package")
    package = updated.add("package")
    package.add_test("testResult")
    package.add_test("test_result")
    tests = subject.Node("current tests", expanded=True)
    tests.add("obsolete test")
    empty = subject.Node("no remaining tests")
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
    subject.browse(
        screen,
        subject.Node("initial tests"),
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
        view: subject.View, report: Callable[[subject.Progress], None]
    ) -> subject.Node:
        expect(frames[-1] == "Loading diff…", frames)
        return subject.build_tree(
            home, view=view, cache=home / "cache", progress=report
        )

    subject.browse(screen, subject.Node("home"), "tests", load)
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

    subject.main()

    rows = screen.addnstr.call_args_list
    labels = [row.args[2].strip() for row in rows]
    expect("▾ github.com" in labels and "▾ example (1)" in labels, labels)
    selected = [
        row.args[2].strip() for row in rows[:-1] if row.args[4] & curses.A_REVERSE
    ]
    expect(selected == ["keeps saved notes"], selected)


def _fixture_add(
    root: Path,
    name: str,
    kind: str,
) -> None:
    """Write the minimal tracked source needed by a test."""
    sources = {}
    if kind in _ENTRYPOINTS:
        filename, content = _ENTRYPOINTS[kind]
        sources[filename] = content
    if kind == "host":
        sources["configuration.nix"] = "{ ... }: {}\n"
    else:
        builder = {
            "bash": "inputs.afairesi.lib.mkBashPackage",
            "python": "inputs.self.lib.mkPythonPackage",
            "html": "inputs.self.lib.mkHtmlPackage",
            "latex": "inputs.self.lib.mkLatexPackage",
            "lean4": "inputs.self.lib.mkLean4Package",
            "generic": "pkgs.writeTextFile",
        }[kind]
        settings = (
            '  name = baseNameOf ./.;\n  text = "";\n'
            if kind == "generic"
            else "  inherit pkgs;\n  src = ./.;\n"
        )
        if kind in {"python", "lean4"}:
            settings += "  executable = true;\n"
        sources["default.nix"] = (
            f"{{ inputs, pkgs, ... }}:\n{builder} {{\n{settings}}}\n"
        )
    package = root / name
    package.mkdir()
    for filename, source in sources.items():
        (package / filename).write_text(source)
    git(root, "add", "--force", "--", name)


@pytest.fixture(name="root")
def repository_root_fixture(tmp_path: Path) -> Path:
    """Index a flake alongside the test's other temporary files."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "flake.nix").write_text("{}\n")
    (root / "flake.lock").touch()
    git(root, "add", ".")
    return root


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


def test_inspection_lists_test_names_in_order_even_when_the_implementation_is_missing(
    root: Path,
) -> None:
    """List declarations without importing the suite or requiring an implementation."""
    _fixture_add(root, "inbox", "python")
    (root / "inbox/main.py").unlink()
    (root / "inbox/test_main.py").write_text(
        "import pytest\n"
        "raise RuntimeError('inspection must not import this suite')\n"
        "@pytest.mark.parametrize('message', ['abc', 'ABC'])\n"
        "def test_preserves_message_case(message): assert deliver(message) == message\n"
        "def test_delivers_messages(): assert deliver('hello') == 'hello'\n"
    )
    git(root, "add", "--", "inbox/test_main.py")
    inventory = test_inventory.inspect_repository(root)
    expect(
        inventory
        == {"inbox": ["test_delivers_messages", "test_preserves_message_case"]},
        inventory,
    )


def test_each_package_lists_a_shared_test_name_once(root: Path) -> None:
    """Keep equal identifiers distinct across packages and unique within a suite."""
    for name in ("mail_box", "mail-box"):
        _fixture_add(root, name, "python")
        tests = root / name / "test_main.py"
        tests.write_text("def test_delivers(): ...\ndef test_delivers(): ...\n")
        git(root, "add", "--", f"{name}/test_main.py")
    inventory = test_inventory.inspect_repository(root)
    expect(list(inventory) == ["mail-box", "mail_box"], inventory)
    expect(
        inventory == {"mail-box": ["test_delivers"], "mail_box": ["test_delivers"]},
        inventory,
    )
    with pytest.raises(ValueError, match="module names collide"):
        flake_layout.validate_flake_source(root)


@pytest.mark.parametrize(
    ("kind", "constructor"),
    [
        ("bash", "inputs.afairesi.lib.mkBashPackage"),
        ("html", "inputs.self.lib.mkHtmlPackage"),
        ("latex", "inputs.afairesi.lib.mkLatexPackage"),
        ("lean4", "(inputs.self).lib.mkLean4Package"),
        ("python", "let build = inputs.self.lib.mkPythonPackage; in (build)"),
    ],
)
def test_declared_package_kind_determines_the_required_entrypoint(
    root: Path,
    kind: str,
    constructor: str,
) -> None:
    """Keep the declared build kind when supporting files resemble entrypoints."""
    package = root / "example"
    package.mkdir()
    executable = "executable = true;" if kind in {"lean4", "python"} else ""
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: let dependency = inputs.self.lib.mkHtmlPackage {}; in "
        f"{constructor} {{ inherit pkgs; src = ./.; {executable} }}",
    )
    if kind != "python":
        (package / "test_main.py").write_text("def test_unrelated(): pass")
    for filename, content in _ENTRYPOINTS.values():
        (package / filename).write_text(content)
    (package / "main.nix").touch()
    git(root, "add", "--", "example")
    inventory = test_inventory.inspect_repository(root)
    expect(inventory == {"example": []}, inventory)
    flake_layout.validate_flake_source(root)
    entrypoint, _ = _ENTRYPOINTS[kind]
    (package / entrypoint).unlink()
    with pytest.raises(ValueError, match=re.escape(entrypoint)):
        flake_layout.validate_flake_source(root)


def test_packages_declared_in_a_with_scope_can_be_listed_without_rewriting_the_source(
    root: Path,
) -> None:
    """Validate a package without rewriting its Nix scope."""
    _fixture_add(root, "example", "python")
    declaration = root / "example/default.nix"
    declaration.write_text(
        "{ inputs, pkgs, ... }: with inputs.self.lib; "
        "mkPythonPackage { inherit pkgs; src = ./.; executable = true; }\n"
    )
    git(root, "add", ".")
    before = _snapshot(root)
    flake_layout.validate_flake_source(root)
    packages = test_inventory.inspect_repository(root)
    expect(list(packages) == ["example"], packages)
    expect(_snapshot(root) == before, "inspection changed repository")


def test_packages_with_custom_builders_stay_generic_regardless_of_source_files(
    root: Path,
) -> None:
    """Discover Nix suites for native and opaque builders with mixed source files."""
    builders = {
        "class": "pkgs.python3Packages.buildPythonPackage",
        "dash-name": "pkgs.rustPlatform.buildRustPackage",
        "dash_name": 'pkgs.haskellPackages.callCabal2nix "example" ./.',
        "custom": "import ./builder.nix",
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
    inventory = test_inventory.inspect_repository(root)
    expect(inventory == {name: ["testBuilds"] for name in builders}, inventory)


def test_python_names_match_the_functions_and_methods_collected_by_pytest(
    tmp_path: Path,
) -> None:
    """Use pytest's collection as the oracle for ordinary and unittest test names."""
    _inspection_repository(tmp_path)
    path = tmp_path / "example/test_main.py"
    path.write_text("""
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
class Qualified(unit.TestCase):
    def test_qualified(self): pass
class AsyncBase(AsyncCase): pass
class AsyncReports(AsyncBase):
    async def test_result(self): pass
""")
    result = subprocess.run(  # noqa: S603 - Use this interpreter with test-authored code and arguments.
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
            "--collect-only",
            "-q",
            str(path),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    collected = sorted(
        line.split("::", 1)[1] for line in result.stdout.splitlines() if "::" in line
    )
    declared = test_inventory.inspect_repository(tmp_path)["example"]
    expect(declared == collected, (declared, collected))


def test_the_package_list_preserves_declared_names_and_excludes_standalone_hosts(
    root: Path,
) -> None:
    """Keep each package's exact spelling in a mixed repository."""
    packages = [
        ("python", "Mixed_Name-v2"),
        ("python", "double--dash"),
        ("python", "_Private"),
        ("html", "Web.Site+v2"),
        ("latex", "Report.v2"),
        ("lean4", "Proof_Library-v2"),
        ("generic", "Package Name"),
        ("generic", "δοκιμή"),
    ]
    for kind, name in packages:
        _fixture_add(root, name, kind)
    _fixture_add(root, "workLaptop", "host")
    flake_layout.validate_flake_source(root)
    inventory = test_inventory.inspect_repository(root)
    expect(inventory == {name: [] for _, name in packages}, inventory)


def _inspection_repository(root: Path) -> None:
    (root / "flake.nix").write_text("{}\n")
    package = root / "example"
    package.mkdir(parents=True)
    (package / "default.nix").write_text(
        "{ inputs, pkgs, ... }: inputs.self.lib.mkPythonPackage { "
        "inherit pkgs; src = ./.; executable = true; "
        'meta.description = "Example"; }',
    )
    (package / "main.py").write_text("def main(): pass\n")
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
        test_inventory.inspect_repository(root) == {},
        "untracked declaration was listed",
    )
    git(root, "add", "--", "draft/default.nix")
    expect(
        test_inventory.inspect_repository(root) == {"draft": ["testDelivers"]},
        package,
    )
    git(root, "mv", "--", "draft", "inbox")
    expect(test_inventory.inspect_repository(root) == {"inbox": ["testDelivers"]}, root)


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
    names = test_inventory.inspect_repository(tmp_path)["example"]
    expect(names == ["test_default_output"], names)
    declaration.write_text(
        declaration.read_text().replace("executable = false", "executable = true")
    )
    with pytest.raises(
        ValueError,
        match="main must accept a call without arguments",
    ):
        flake_layout.validate_flake_source(tmp_path)


def test_inspection_reports_a_missing_flake_or_invalid_python_test_source(
    tmp_path: Path,
) -> None:
    """Report invalid repositories and malformed source with useful errors."""
    with pytest.raises(ValueError, match=r"flake\.nix"):
        test_inventory.inspect_repository(tmp_path)
    _inspection_repository(tmp_path)
    (tmp_path / "example" / "test_main.py").write_text("def invalid(")
    with pytest.raises(
        ValueError,
        match="could not parse Python source",
    ):
        test_inventory.inspect_repository(tmp_path)


def _indexed_repository(root: Path) -> None:
    """Stage the sources used as the diff baseline."""
    root.mkdir(parents=True, exist_ok=True)
    _inspection_repository(root)
    git(root, "init", "--quiet")
    git(root, "add", "--force", ".")


def _write_test(root: Path, name: str) -> None:
    """Give the fixture's sole Python test a new name."""
    (root / "example/test_main.py").write_text(f"def test_{name}(): pass\n")


def test_diff_ignores_implementation_edits_and_untracked_packages(
    tmp_path: Path,
) -> None:
    """Only changes to the package-to-test-name mapping produce output."""
    _inspection_repository(tmp_path)
    git(tmp_path, "init", "--quiet")
    expect(not test_inventory.diff_repository(tmp_path), "untracked files in diff")
    git(tmp_path, "add", ".")
    source = tmp_path / "example/main.py"
    source.write_text("def unfinished(")
    declaration = tmp_path / "example/default.nix"
    declaration.write_text(
        declaration.read_text().replace("Example", "Updated description")
    )
    tests = tmp_path / "example/test_main.py"
    tests.write_text(
        'def test_default_output():\n    """Updated notes."""\n'
        '    raise RuntimeError("changed")\n',
    )
    (tmp_path / "untracked").mkdir()
    (tmp_path / "untracked/default.nix").write_text("{}")
    before = _snapshot(tmp_path)
    difference = test_inventory.diff_repository(tmp_path)
    expect(not difference, difference)
    expect(_snapshot(tmp_path) == before, "empty diff changed state")


def test_diff_follows_tracked_symlink_targets_of_test_suites(tmp_path: Path) -> None:
    """Keep deeply nested symlink targets in both snapshots."""
    _indexed_repository(tmp_path)
    source = tmp_path / "example/test_main.py"
    target = tmp_path / "assets/deep/entry"
    target.parent.mkdir(parents=True)
    source.rename(target)
    source.symlink_to("../assets/deep/entry")
    git(tmp_path, "add", ".")
    target.write_text("def test_unicode_input(): ...\ndef test_empty_input(): ...\n")
    changes = test_inventory.diff_repository(tmp_path)
    expect(
        changes
        == [
            {
                "package": "example",
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
    _fixture_add(root, "shared", "host")
    (root / "shared/default.nix").write_text("{}")
    git(root, "add", "--", "shared/default.nix")
    (root / "shared/default.nix").unlink()
    changes = test_inventory.diff_repository(root)
    expect(
        changes
        == [{"package": "shared", "status": "removed", "added": [], "removed": []}],
        changes,
    )
    with pytest.raises(
        ValueError,
        match=r"shared/default\.nix: missing required tracked regular file",
    ):
        flake_layout.validate_flake_source(root)


def test_a_failed_test_name_comparison_removes_its_temporary_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clean failed snapshots when a tracked source cannot be inspected."""
    root = tmp_path / "repository"
    _indexed_repository(root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    (root / "example/test_main.py").write_text("def invalid(")
    before = _snapshot(root)
    with pytest.raises(ValueError, match="could not parse Python source"):
        test_inventory.diff_repository(root)
    expect(not list(scratch.iterdir()), "failed diff left temporary sources")
    expect(_snapshot(root) == before, "failed diff changed state")


def test_diff_reports_unresolved_merge_conflicts_and_leaves_the_index_untouched(
    tmp_path: Path,
) -> None:
    """Report the conflicting index before producing a test-name diff."""
    _indexed_repository(tmp_path)
    blob = git(tmp_path, "rev-parse", ":example/main.py")
    subprocess.run(
        ["git", "update-index", "--index-info"],  # noqa: S607
        cwd=tmp_path,
        input=(
            "0 "
            + "0" * len(blob)
            + "\texample/main.py\n"
            + f"100644 {blob} 1\texample/main.py\n"
            + f"100644 {blob} 2\texample/main.py\n"
            + f"100644 {blob} 3\texample/main.py\n"
        ),
        text=True,
        check=True,
        timeout=10,
    )
    before = _snapshot(tmp_path)
    with pytest.raises(ValueError, match="unmerged index"):
        test_inventory.diff_repository(tmp_path)
    expect(
        _snapshot(tmp_path) == before,
        "conflict diff changed state",
    )


def test_diff_reads_the_selected_linked_worktree_without_modifying_it(
    tmp_path: Path,
) -> None:
    """Read the selected worktree's index while preserving shared Git metadata."""
    root, linked = tmp_path / "repository", tmp_path / "linked"
    _indexed_repository(root)
    commit(root, "initial")
    git(root, "worktree", "add", "--quiet", "--detach", str(linked), "HEAD")
    _write_test(linked, "staged")
    git(linked, "add", "example/test_main.py")
    _write_test(linked, "working")
    before = _snapshot(tmp_path)
    changes = test_inventory.diff_repository(linked)
    expect(
        changes
        == [
            {
                "package": "example",
                "status": "modified",
                "added": ["test_working"],
                "removed": ["test_staged"],
            }
        ],
        changes,
    )
    expect(
        _snapshot(tmp_path) == before,
        "diff changed worktree state",
    )


def test_layout_check_ignores_untracked_packages_and_test_files(
    root: Path,
) -> None:
    """Skip malformed local declarations and suites until they are tracked."""
    _fixture_add(root, "example", "python")
    _fixture_add(root, "library", "generic")
    (root / "example/test_main.py").write_text("def broken(:")
    (root / "library/test_main.nix").write_text("{ invalid")
    local = root / "experiments"
    local.mkdir()
    (local / "default.nix").write_text("{ invalid")
    before = _snapshot(root)
    flake_layout.validate_flake_source(root)
    inventory = test_inventory.inspect_repository(root)
    expect(inventory == {"example": [], "library": []}, inventory)
    expect(
        _snapshot(root) == before, "reading untracked sources changed the repository"
    )


def test_staging_bats_nix_and_lean_suites_lists_their_declared_test_names(
    root: Path,
) -> None:
    """Read Bats, Nix, and Lean declarations once their suites are staged."""
    expected = {}
    for kind, suite, source, names in [
        (
            "bash",
            "test_main.bats",
            """exit 99
cat <<'TEXT'
@test "not a declaration" {
TEXT
@test "preserves spaces" { true; }
check_status() { # @test
  true
}
helper() { true; }
""",
            ["check_status", "preserves spaces"],
        ),
        (
            "generic",
            "test_main.nix",
            """{ ... }: let
              selection = [ "preservesSpaces" ];
              suite = {
                tests = selection;
                preservesSpaces = { expr = throw "never evaluate"; expected = true; };
                testExcluded = { expr = false; expected = true; };
              };
            in (suite)""",
            ["preservesSpaces"],
        ),
        (
            "lean4",
            "test_main.lean",
            """namespace Résultats
section
/- theorem ignored : True := trivial -/
theorem preservesCafé : True := trivial
example : True := trivial
end
end Résultats
""",
            ["Résultats.preservesCafé"],
        ),
    ]:
        _fixture_add(root, kind, kind)
        (root / kind / suite).write_text(source)
        expected[kind] = names
    untracked = test_inventory.inspect_repository(root)
    expect(untracked == {kind: [] for kind in expected}, untracked)
    git(root, "add", "--", *expected)
    inventory = test_inventory.inspect_repository(root)
    expect(inventory == expected, inventory)


def test_cargo_lists_tests_from_enabled_targets_and_their_module_trees(
    root: Path,
) -> None:
    """List declared unit and integration tests without building the crate."""
    _fixture_add(root, "example", "generic")
    package = root / "example"
    sources = {
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
    before = test_inventory.inspect_repository(root)
    expect(
        before
        == {
            "example": [
                "prints_output",
                "tests::accepts_input",
                "tests::shared::nested::preserves_spaces",
            ]
        },
        before,
    )
    suite = package / "src/shared.rs"
    suite.write_text(suite.read_text().replace("preserves", "retains"))
    changes = test_inventory.diff_repository(root)
    expect(
        changes
        == [
            {
                "package": "example",
                "status": "modified",
                "added": ["tests::shared::nested::retains_spaces"],
                "removed": ["tests::shared::nested::preserves_spaces"],
            }
        ],
        changes,
    )
