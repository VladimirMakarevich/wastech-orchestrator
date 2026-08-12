"""Cross-platform round-trip coverage for the memory store.

These tests are OS-agnostic — they live in the shared suite and pass everywhere — but their point is
to be exercised on **real Windows**, where the audit left the POSIX-path guarantees as "storage is
tested … but there is no Windows-form round-trip test". They pin the two cross-platform invariants:

* **Paths round-trip as POSIX.** Every stored/compared path string is the ``Path.as_posix()`` form,
  so a record written on one OS resolves against the native filesystem of another. A path is never
  stored with backslashes, and ``git ls-files`` output is consumed as forward slashes.
* **Bytes are deterministic.** Tier files are written UTF-8 with an explicit ``\n`` (never ``\r\n``)
  and key-sorted JSON, so the audit hash-chain and snapshot/restore stay byte-stable across OSes.

The Windows-only backslash-normalization assertions are guarded by ``skipif`` — everywhere else the
POSIX/native forms coincide and there is nothing OS-specific to prove.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from wastech_orchestrator.memory import (
    AuditContext,
    CandidateDelta,
    CandidateEntity,
    DerivedIndex,
    EpisodeRecord,
    MemoryLayout,
    MemoryService,
    TrustLevel,
    _io,
)
from wastech_orchestrator.memory.derived import git_tracked_paths
from wastech_orchestrator.memory.service import WriteSource

_TS = "2026-07-01T00:00:00Z"


def _episode(task_id: str = "t1") -> EpisodeRecord:
    return EpisodeRecord(
        id=f"ep_{task_id}", task_id=task_id, created_at=_TS, trust_level=TrustLevel.ARTIFACT_BACKED
    )


def _indexed_service(repo: Path) -> MemoryService:
    """A service whose write funnel validates entity paths against the *live* repo tree (no git).

    The provider returns an empty tracked set, so ``path_exists`` must fall back to a native
    filesystem stat — which is exactly the Windows behavior this needs to exercise.
    """
    index = DerivedIndex(repo, tracked_paths_provider=lambda _root: frozenset())
    return MemoryService(MemoryLayout(repo / ".worc"), index=index)


# --- (a) a POSIX-stored path resolves against the native (Windows) filesystem ----------------


def test_posix_path_resolves_against_native_filesystem(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('hi')\n", encoding="utf-8")
    # Empty tracked set → path_exists can only answer via a native filesystem stat of "src/a.py".
    index = DerivedIndex(tmp_path, tracked_paths_provider=lambda _root: frozenset())
    assert index.path_exists("src/a.py") is True
    assert index.path_exists("src/gone.py") is False

    # find_by_basename returns tracked candidates as forward-slash POSIX strings.
    idx = DerivedIndex(
        tmp_path, tracked_paths_provider=lambda _root: frozenset({"src/old/a.py", "src/new/a.py"})
    )
    candidates = idx.find_by_basename("src/old/a.py")
    assert candidates == ("src/new/a.py",)
    assert all("\\" not in candidate for candidate in candidates)


# --- (b) end-to-end: entity path validated against the native FS, stored forward-slashed -------


def test_entity_card_end_to_end_stores_posix_path(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    layout = MemoryLayout(tmp_path / ".worc")
    service = _indexed_service(tmp_path)
    delta = CandidateDelta(
        entities=(
            CandidateEntity(entity_id="module:a", entity_type="module", paths=("src/a.py",)),
            CandidateEntity(entity_id="module:gone", entity_type="module", paths=("src/gone.py",)),
        )
    )
    service.apply_delta(
        delta, episode=_episode(), source=WriteSource.SUCCESS, audit=AuditContext(timestamp=_TS)
    )

    entities = service.read_entities()
    assert [row["entity_id"] for row in entities] == ["module:a"]  # verified path → stored
    assert entities[0]["trust_level"] == "repo-observed"
    assert entities[0]["paths"] == ["src/a.py"]
    quarantined = {row.get("entity_id") for row in service.read_quarantine()}
    assert quarantined == {"module:gone"}  # missing path → quarantine, never a silent drop

    # The persisted bytes carry the POSIX path — no backslash anywhere in the tier file.
    entities_text = (layout.entities / "entities.jsonl").read_text(encoding="utf-8")
    assert '"src/a.py"' in entities_text
    assert "\\" not in entities_text


# --- (c) tier files are written LF (never CRLF) and are byte-deterministic (sort_keys) ---------


def test_tier_file_is_lf_and_deterministic(tmp_path: Path) -> None:
    layout = MemoryLayout(tmp_path / ".worc")
    service = _indexed_service(tmp_path)
    service.apply_delta(
        None, episode=_episode(), source=WriteSource.SUCCESS, audit=AuditContext(timestamp=_TS)
    )

    recent = layout.short_term / "recent.jsonl"
    raw = recent.read_bytes()
    assert b"\n" in raw
    assert b"\r\n" not in raw  # explicit newline="\n": no platform CRLF translation on Windows

    rows = _io.read_jsonl(recent)
    assert rows and rows[0]["id"] == "ep_t1"  # JSON reads back identically
    # Keys are sorted on disk (canonical form), so content hashes match across OSes.
    line = recent.read_text(encoding="utf-8").splitlines()[0]
    assert line == json.dumps(json.loads(line), ensure_ascii=False, sort_keys=True)

    # A second identical write produces byte-identical output (determinism, not clock/random).
    scratch = tmp_path / "scratch.jsonl"
    unordered = {"b": 2, "a": 1, "nested": {"y": 1, "x": 2}}
    _io.atomic_write_jsonl(scratch, [unordered])
    first = scratch.read_bytes()
    _io.atomic_write_jsonl(scratch, [unordered])
    assert scratch.read_bytes() == first
    assert b"\r\n" not in first
    assert first.startswith(b'{"a": 1,')  # sorted keys


# --- (d) git_tracked_paths over a real Windows git repo returns forward-slash POSIX paths -------


def test_git_tracked_paths_are_posix_on_real_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "src" / "pkg" / "mod.py").write_text("y = 2\n", encoding="utf-8")

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, check=True)

    _git("init", "-b", "main", ".")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("config", "commit.gpgsign", "false")
    _git("add", ".")
    _git("commit", "-m", "initial")

    tracked = git_tracked_paths(repo)
    # git ls-files emits forward slashes on every OS; the nested path is POSIX, not "src\\pkg\\...".
    assert "src/pkg/mod.py" in tracked
    assert all("\\" not in path for path in tracked)

    index = DerivedIndex(repo)  # default provider → shells out to real git
    assert index.path_exists("src/pkg/mod.py") is True
    assert index.find_by_basename("src/pkg/mod.py") == ()  # unique basename → no remap candidate


# --- (e) Windows-only: a backslash path normalizes via as_posix() and still resolves -----------


@pytest.mark.skipif(sys.platform != "win32", reason="backslash paths are Windows-native only")
def test_backslash_path_normalizes_and_resolves_on_windows(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("z = 3\n", encoding="utf-8")

    # A tracked entry supplied in native Windows form is normalized to POSIX on ingest…
    index = DerivedIndex(tmp_path, tracked_paths_provider=lambda _root: frozenset({"src\\a.py"}))
    assert "src/a.py" in index.tracked_paths()
    assert all("\\" not in path for path in index.tracked_paths())

    # …and a backslash query path resolves against it (path_exists as_posix-normalizes its arg).
    assert index.path_exists("src\\a.py") is True
    assert index.path_exists("src/a.py") is True
    assert Path("src\\a.py").as_posix() == "src/a.py"
