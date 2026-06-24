"""Unit tests for deterministic diff-based command-set selection (Р3) and the glob matcher."""

from __future__ import annotations

from wastech_orchestrator.checks.model import ResolvedCheck, ResolvedCheckSet
from wastech_orchestrator.checks.selection import _compile_glob, select_check_sets


def _set(name: str, *paths: str) -> ResolvedCheckSet:
    return ResolvedCheckSet(name=name, paths=tuple(paths), checks=(ResolvedCheck(name, (name,)),))


def _names(sets: tuple[ResolvedCheckSet, ...]) -> list[str]:
    return [s.name for s in sets]


# -- glob matcher -------------------------------------------------------------


def _m(pattern: str, path: str) -> bool:
    return _compile_glob(pattern).fullmatch(path) is not None


def test_extension_glob_matches_anywhere() -> None:
    # `**/*.md` runs a Markdown linter only when .md files change (operator's MD-lint set).
    assert _m("**/*.md", "README.md")
    assert _m("**/*.md", "docs/a/b.md")
    assert not _m("**/*.md", "src/x.py")


def test_subtree_glob() -> None:
    assert _m("backend/**", "backend/src/x.cs")
    assert not _m("backend/**", "mobile/x")


def test_single_segment_star_does_not_cross_slash() -> None:
    assert _m("mobile/*.lock", "mobile/yarn.lock")
    assert not _m("mobile/*.lock", "mobile/a/b.lock")


# -- selection rules ----------------------------------------------------------

_BE = _set("backend", "backend/**")
_FE = _set("frontend", "mobile/**")
_DOCS = _set("docs", "**/*.md")
_LINT = _set("lint")  # no paths → always runs (on a non-empty diff)
_SETS = (_BE, _FE, _DOCS, _LINT)


def test_none_diff_runs_all() -> None:
    # Diff indeterminate (git not wired) → conservative: run everything.
    assert _names(select_check_sets(_SETS, None)) == ["backend", "frontend", "docs", "lint"]


def test_empty_diff_runs_nothing() -> None:
    # Correction: a task that changed nothing runs no checks (the node passes vacuously).
    assert select_check_sets(_SETS, []) == ()


def test_matched_subtree_plus_always_on() -> None:
    assert _names(select_check_sets(_SETS, ["backend/src/x.cs"])) == ["backend", "lint"]


def test_markdown_only_change_runs_only_docs_and_always_on() -> None:
    # An MD-only change is "covered" by the docs set → no fail-safe; backend/frontend stay idle.
    assert _names(select_check_sets(_SETS, ["README.md", "docs/x.md"])) == ["docs", "lint"]


def test_unmatched_path_runs_only_always_on_sets() -> None:
    # A changed path claimed by no path-bearing set runs no set on its account: only the always-on
    # set (no `paths`) runs. (Previously this fell back to running ALL sets — removed because on a
    # real monorepo any unclaimed root/docs edit then triggered a full-repo run.)
    assert _names(select_check_sets(_SETS, ["build/codegen.py"])) == ["lint"]


def test_unmatched_path_with_no_always_on_set_runs_nothing() -> None:
    # No catch-all set + an unclaimed path → nothing runs (the node passes vacuously). The operator
    # covers shared/root paths by adding a no-`paths` set or listing them in a set's `paths`.
    assert select_check_sets((_BE, _FE, _DOCS), ["build/codegen.py"]) == ()


def test_union_of_multiple_matched_sets() -> None:
    out = select_check_sets(_SETS, ["backend/x.cs", "mobile/y.ts"])
    assert _names(out) == ["backend", "frontend", "lint"]


def test_no_sets_configured_runs_nothing() -> None:
    assert select_check_sets((), ["backend/x"]) == ()
