"""Unit tests for the shared repo-relative glob matcher (``globmatch``)."""

from __future__ import annotations

from wastech_orchestrator.globmatch import compile_glob, path_matches_any


def _m(pattern: str, path: str) -> bool:
    return compile_glob(pattern).fullmatch(path) is not None


def test_extension_glob_matches_anywhere() -> None:
    # `**/*.md` matches a .md file at any depth.
    assert _m("**/*.md", "README.md")
    assert _m("**/*.md", "docs/a/b.md")
    assert not _m("**/*.md", "src/x.py")


def test_subtree_glob() -> None:
    assert _m("backend/**", "backend/src/x.cs")
    assert not _m("backend/**", "mobile/x")


def test_single_segment_star_does_not_cross_slash() -> None:
    assert _m("mobile/*.lock", "mobile/yarn.lock")
    assert not _m("mobile/*.lock", "mobile/a/b.lock")


def test_top_level_star_is_single_segment() -> None:
    # `*.md` is top-level only; `**/*.md` is "anywhere".
    assert _m("*.md", "README.md")
    assert not _m("*.md", "docs/a.md")


def test_path_matches_any_normalizes_backslashes() -> None:
    assert path_matches_any("docs\\a\\b.md", ("**/*.md",))
    assert not path_matches_any("src/x.py", ("**/*.md", "docs/**"))
    assert path_matches_any("docs/x.txt", ("**/*.md", "docs/**"))


def test_path_matches_any_empty_patterns() -> None:
    assert not path_matches_any("anything.md", ())
