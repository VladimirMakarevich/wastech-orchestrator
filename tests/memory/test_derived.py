"""DerivedIndex (04.4): path/symbol existence + rebuildable cache, no real git repo needed."""

from __future__ import annotations

import json
from pathlib import Path

from wastech_orchestrator.memory.derived import DerivedIndex


def _index(repo: Path, tracked: set[str], *, derived: Path | None = None) -> DerivedIndex:
    return DerivedIndex(
        repo, derived_dir=derived, tracked_paths_provider=lambda _root: frozenset(tracked)
    )


def test_path_exists_tracked_then_removed(tmp_path: Path) -> None:
    idx = _index(tmp_path, {"src/a.py", "src/b.py"})
    assert idx.path_exists("src/a.py") is True  # tracked
    assert idx.path_exists("src/gone.py") is False  # neither tracked nor on disk


def test_path_exists_untracked_but_on_disk(tmp_path: Path) -> None:
    # A present-but-untracked path (e.g. generated) still counts as existing.
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "out.js").write_text("x", encoding="utf-8")
    idx = _index(tmp_path, set())
    assert idx.path_exists("build/out.js") is True


def test_symbol_exists_present_and_absent(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("def handle_event():\n    ...\n", encoding="utf-8")
    idx = _index(tmp_path, {"src/a.py"})
    assert idx.symbol_exists("handle_event", paths=["src/a.py"]) is True
    assert idx.symbol_exists("vanished_symbol", paths=["src/a.py"]) is False


def test_symbol_without_scope_is_conservatively_present(tmp_path: Path) -> None:
    # No paths to grep → never treated as stale (fail-closed; cleanup must not drop on a non-check).
    idx = _index(tmp_path, set())
    assert idx.symbol_exists("anything", paths=[]) is True


def test_symbol_all_paths_gone_is_absent(tmp_path: Path) -> None:
    idx = _index(tmp_path, set())  # the scoped path is neither tracked nor on disk
    assert idx.symbol_exists("foo", paths=["src/gone.py"]) is False


def test_find_by_basename_excludes_self_and_sorts(tmp_path: Path) -> None:
    idx = _index(tmp_path, {"src/old/foo.py", "src/new/foo.py", "src/bar.py"})
    assert idx.find_by_basename("src/old/foo.py") == ("src/new/foo.py",)
    assert idx.find_by_basename("src/bar.py") == ()  # unique basename → no remap candidate


def test_cache_is_rebuildable_and_carries_no_audit(tmp_path: Path) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    idx = _index(tmp_path, {"src/b.py", "src/a.py"}, derived=derived)
    cache = idx.write_cache(generated_at="2026-06-30T00:00:00Z")
    assert cache is not None and cache.name == "repo_map.json"
    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["tracked_paths"] == ["src/a.py", "src/b.py"]  # sorted, deterministic
    # Recompute from a fresh index → identical answer (the cache is derived, not truth).
    assert _index(tmp_path, {"src/b.py", "src/a.py"}).tracked_paths() == idx.tracked_paths()


def test_tracked_paths_are_posix_normalized(tmp_path: Path) -> None:
    # Stored/compared paths are the POSIX form (AC-X1): forward-slash paths round-trip unchanged and
    # membership is exact, so a record's POSIX scope path always matches the tracked set.
    idx = _index(tmp_path, {"src/pkg/mod.py"})
    assert "src/pkg/mod.py" in idx.tracked_paths()
    assert idx.path_exists("src/pkg/mod.py") is True
