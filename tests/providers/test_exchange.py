"""Tests for the exchange publication boundary (WRI-001).

The POSIX filesystem branches run natively; the native-Windows branches (reparse points, hard-link
count, NTFS alternate data streams) are driven by an injected fake :data:`FileInspector` returning
simulated Windows :class:`FileFacts`, so every fail-closed path is exercised on this host too. The
real Windows fact extraction is covered on a Windows host by the WRI-006 gate.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from wastech_orchestrator.providers.artifacts import (
    EXCHANGE_HOME,
    exchange_task_dir,
    sha256_file,
)
from wastech_orchestrator.providers.base import AgentRunRequest
from wastech_orchestrator.providers.exchange import (
    ExchangeError,
    FileFacts,
    assert_exchange_current_task_only,
    assert_orchestration_paths_contained,
    build_exchange_manifest,
    clear_exchange_task_dir,
    posix_file_facts,
    publish_to_exchange,
)

GH_TOKEN = "ghp_" + "A" * 24
SK_KEY = "sk-" + "B" * 24
ASSIGN = "PASSWORD=hunter2-very-secret-value"
LITERAL = "supersecretliteral123"


@pytest.fixture
def task_dir(tmp_path: Path) -> Path:
    return exchange_task_dir(tmp_path / EXCHANGE_HOME, "add-http-retry")


def _fake_inspector(overrides: dict[Path, FileFacts]) -> Callable[[Path], FileFacts]:
    """A :data:`FileInspector` returning simulated facts for registered paths, else real facts."""
    resolved = {key.resolve(): facts for key, facts in overrides.items()}

    def inspect(path: Path) -> FileFacts:
        return resolved.get(path.resolve(), posix_file_facts(path))

    return inspect


# --- publisher happy path ------------------------------------------------------------------------


def test_publish_returns_posix_path_and_writes_under_task_dir(task_dir: Path) -> None:
    result = publish_to_exchange(task_dir, "plan.md", "plan body\n")
    assert result == (task_dir / "plan.md").as_posix()
    assert Path(result).read_text(encoding="utf-8") == "plan body\n"
    assert "\\" not in result  # POSIX display form even on Windows


def test_publish_bytes_are_decoded_and_written(task_dir: Path) -> None:
    result = publish_to_exchange(task_dir, "current.diff", b"diff --git a b\n")
    assert Path(result).read_text(encoding="utf-8") == "diff --git a b\n"


def test_publish_is_lf_byte_stable(task_dir: Path) -> None:
    # A lone \n is never platform-translated to \r\n; existing bytes are preserved verbatim.
    result = publish_to_exchange(task_dir, "f.txt", "a\nb\nc\n")
    assert Path(result).read_bytes() == b"a\nb\nc\n"


def test_publish_leaves_no_temp_file(task_dir: Path) -> None:
    publish_to_exchange(task_dir, "plan.md", "x")
    assert sorted(p.name for p in task_dir.iterdir()) == ["plan.md"]


def test_publish_overwrites_an_existing_regular_file(task_dir: Path) -> None:
    publish_to_exchange(task_dir, "plan.md", "first")
    result = publish_to_exchange(task_dir, "plan.md", "second")
    assert Path(result).read_text(encoding="utf-8") == "second"


# --- redaction matrix (one per content shape a real writer produces) -----------------------------


@pytest.mark.parametrize(
    ("relpath", "content"),
    [
        ("plan.md", f"# Plan\nrun with {GH_TOKEN} and {LITERAL}\n"),
        ("stages/review/run-000001/findings.json", f'{{"note": "{SK_KEY}", "{ASSIGN}": 1}}'),
        ("checks/run-000004.log", f"FAILED\nstdout leaked {GH_TOKEN}\n{ASSIGN}\n"),
        ("current.diff", f"+api_key = {SK_KEY}\n+literal {LITERAL}\n"),
        ("hitl/fixing.answer.json", f'{{"answer": "use {GH_TOKEN}"}}'),
        ("memory/fixing.md", f"lesson: avoid leaking {SK_KEY} / {LITERAL}"),
        ("subtasks/01-do-thing.md", f"spec references {ASSIGN}"),
    ],
)
def test_publish_redacts_every_content_shape(task_dir: Path, relpath: str, content: str) -> None:
    result = publish_to_exchange(task_dir, relpath, content, extra_secrets=(LITERAL,))
    on_disk = Path(result).read_text(encoding="utf-8")
    for secret in (GH_TOKEN, SK_KEY, "hunter2-very-secret-value", LITERAL):
        assert secret not in on_disk, f"{secret!r} leaked into {relpath}"
    assert "[REDACTED]" in on_disk


# --- relpath / containment fail-closed -----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../escape",
        "/abs/path",
        "a\\b",
        "stdout.txt:evil",
        "..",
        "",
        "  ",
        "a/../b",
        # Non-portable segments now rejected via the shared is_portable_path_segment grammar:
        "con",  # Windows device name
        "nul.txt",  # device stem + extension
        "COM1",  # device name
        "plan.",  # trailing dot
        "trailing ",  # trailing space
        "stages/con/plan.md",  # device name in an inner segment
    ],
)
def test_publish_rejects_unsafe_relpath(task_dir: Path, bad: str) -> None:
    with pytest.raises(ExchangeError):
        publish_to_exchange(task_dir, bad, "x")


def test_publish_rejects_symlinked_path_component(task_dir: Path, tmp_path: Path) -> None:
    task_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (task_dir / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ExchangeError):
        publish_to_exchange(task_dir, "sub/f.txt", "x")


def test_publish_rejects_symlinked_target(task_dir: Path, tmp_path: Path) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "plan.md").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(ExchangeError):
        publish_to_exchange(task_dir, "plan.md", "x")


# --- manifest ------------------------------------------------------------------------------------


def test_manifest_fingerprints_regular_files(task_dir: Path) -> None:
    p1 = publish_to_exchange(task_dir, "plan.md", "plan")
    publish_to_exchange(task_dir, "stages/impl/run-000001/impl.out.md", "out")
    manifest = build_exchange_manifest(task_dir, "add-http-retry")
    assert manifest.task_id == "add-http-retry"
    by_name = {e.relname: e for e in manifest.entries}
    assert set(by_name) == {"plan.md", "stages/impl/run-000001/impl.out.md"}
    plan = by_name["plan.md"]
    assert plan.is_regular and plan.link_count == 1
    assert plan.size == len("plan")
    assert plan.sha256 == sha256_file(p1)


def test_manifest_rejects_real_symlink(task_dir: Path, tmp_path: Path) -> None:
    task_dir.mkdir(parents=True)
    (task_dir / "link.md").symlink_to(tmp_path / "target")
    with pytest.raises(ExchangeError):
        build_exchange_manifest(task_dir, "t")


def test_manifest_rejects_real_hard_link(task_dir: Path) -> None:
    publish_to_exchange(task_dir, "a.md", "body")
    os.link(task_dir / "a.md", task_dir / "b.md")  # a hard link → link_count 2
    with pytest.raises(ExchangeError):
        build_exchange_manifest(task_dir, "t")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX fifo unavailable")
def test_manifest_rejects_special_file(task_dir: Path) -> None:
    task_dir.mkdir(parents=True)
    os.mkfifo(task_dir / "pipe")  # type: ignore[attr-defined]
    with pytest.raises(ExchangeError):
        build_exchange_manifest(task_dir, "t")


def test_manifest_rejects_simulated_windows_reparse_point(task_dir: Path) -> None:
    publish_to_exchange(task_dir, "plan.md", "x")
    target = task_dir / "plan.md"
    facts = FileFacts(
        is_symlink=True, is_dir=False, is_regular=False, link_count=1, alt_streams=(), size=0
    )
    with pytest.raises(ExchangeError):
        build_exchange_manifest(task_dir, "t", inspect=_fake_inspector({target: facts}))


def test_manifest_rejects_simulated_windows_hard_link(task_dir: Path) -> None:
    publish_to_exchange(task_dir, "plan.md", "x")
    target = task_dir / "plan.md"
    facts = FileFacts(
        is_symlink=False, is_dir=False, is_regular=True, link_count=2, alt_streams=(), size=1
    )
    with pytest.raises(ExchangeError):
        build_exchange_manifest(task_dir, "t", inspect=_fake_inspector({target: facts}))


def test_manifest_rejects_simulated_ntfs_alternate_data_stream(task_dir: Path) -> None:
    publish_to_exchange(task_dir, "plan.md", "x")
    target = task_dir / "plan.md"
    facts = FileFacts(
        is_symlink=False, is_dir=False, is_regular=True, link_count=1, alt_streams=("evil",), size=1
    )
    with pytest.raises(ExchangeError):
        build_exchange_manifest(task_dir, "t", inspect=_fake_inspector({target: facts}))


def _case_sensitive(directory: Path) -> bool:
    probe = directory / "CaseProbe"
    probe.write_text("x", encoding="utf-8")
    try:
        return not (directory / "caseprobe").exists()
    finally:
        probe.unlink()


def test_manifest_rejects_case_fold_collision(task_dir: Path) -> None:
    task_dir.mkdir(parents=True)
    if not _case_sensitive(task_dir):
        pytest.skip("case-insensitive filesystem cannot hold both names")
    (task_dir / "Plan.md").write_text("a", encoding="utf-8")
    (task_dir / "plan.md").write_text("b", encoding="utf-8")
    with pytest.raises(ExchangeError):
        build_exchange_manifest(task_dir, "t")


# --- pre-launch invariants -----------------------------------------------------------------------


def test_current_task_only_accepts_absent_empty_and_single(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    assert_exchange_current_task_only(exchange_root, "t")  # absent → ok
    exchange_root.mkdir()
    assert_exchange_current_task_only(exchange_root, "t")  # empty → ok
    (exchange_root / "t").mkdir()
    assert_exchange_current_task_only(exchange_root, "t")  # only the current task → ok


def test_current_task_only_rejects_foreign_dir_and_stray_file(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    (exchange_root / "other-task").mkdir(parents=True)
    with pytest.raises(ExchangeError):
        assert_exchange_current_task_only(exchange_root, "t")
    (exchange_root / "stray.txt").write_text("x", encoding="utf-8")
    with pytest.raises(ExchangeError):
        assert_exchange_current_task_only(exchange_root, "other-task")


def test_current_task_only_rejects_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    exchange_root = tmp_path / EXCHANGE_HOME
    exchange_root.symlink_to(real, target_is_directory=True)
    with pytest.raises(ExchangeError):
        assert_exchange_current_task_only(exchange_root, "t")


def test_current_task_only_rejects_simulated_windows_reparse_root(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    exchange_root.mkdir()
    facts = FileFacts(
        is_symlink=True, is_dir=False, is_regular=False, link_count=1, alt_streams=(), size=0
    )
    with pytest.raises(ExchangeError):
        assert_exchange_current_task_only(
            exchange_root, "t", inspect=_fake_inspector({exchange_root: facts})
        )


# --- request-path containment --------------------------------------------------------------------


def _request(exchange_root: Path, repo: Path, **paths: str | tuple[str, ...]) -> AgentRunRequest:
    return AgentRunRequest(
        task_id="t",
        node_id="n",
        working_directory=str(repo),
        prompt="p",
        permission_profile="read-only",
        timeout_seconds=60,
        attempt=1,
        node_run_id=1,
        **paths,  # type: ignore[arg-type]
    )


def test_paths_contained_accepts_exchange_paths(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    td = exchange_task_dir(exchange_root, "t")
    req = _request(
        exchange_root,
        tmp_path / "repo",
        task_path=str(td / "task.md"),
        plan_path=str(td / "plan.md"),
        skill_reference_paths=(str(td / "skills" / "s" / "SKILL.md"),),
    )
    assert_orchestration_paths_contained(req, exchange_root)  # does not raise


def test_paths_contained_rejects_a_path_outside_the_exchange(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    req = _request(
        exchange_root,
        tmp_path / "repo",
        task_path=str(tmp_path / "repo" / "tasks" / "t.md"),  # live workspace path → rejected
    )
    with pytest.raises(ExchangeError):
        assert_orchestration_paths_contained(req, exchange_root)


def test_paths_contained_ignores_working_directory(tmp_path: Path) -> None:
    exchange_root = tmp_path / EXCHANGE_HOME
    req = _request(exchange_root, tmp_path / "repo")  # only working_directory set
    assert_orchestration_paths_contained(req, exchange_root)  # does not raise


# --- lifecycle -----------------------------------------------------------------------------------


def test_clear_exchange_task_dir_removes_the_tree(task_dir: Path) -> None:
    publish_to_exchange(task_dir, "plan.md", "x")
    assert task_dir.exists()
    clear_exchange_task_dir(task_dir.parent, "add-http-retry")
    assert not task_dir.exists()
    # Idempotent when already absent.
    clear_exchange_task_dir(task_dir.parent, "add-http-retry")
