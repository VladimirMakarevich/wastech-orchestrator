"""The change overview the Git Manager writes beside ``current.diff`` — on a real temporary repo.

``current.diff`` is the core's artifact and must not move: the dangerous-diff guard, the PR body,
the failure report and recovery all read it. The overview is written in the same call, from the
same snapshot, so the two always describe one change.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from wastech_orchestrator.config.schema import BranchMode
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.core.supervisor_packet import summarize_diff
from wastech_orchestrator.diff_overview import LAST_COMMIT_TITLE, TASK_BASE_TITLE, overview_base
from wastech_orchestrator.git_manager import (
    RUNTIME_EXCLUDED_DIRS,
    GitCommandError,
    GitManager,
    GitResult,
)
from wastech_orchestrator.providers.redaction import REDACTED
from wastech_orchestrator.state_store import StateStore, TaskRow

pytestmark = pytest.mark.slow

GitRunner = Callable[[Sequence[str], Path], str]
ConfigFactory = Callable[..., object]

_EPOCH = 1700000000


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore.open(tmp_path / "state.db")


def _offline_gh(argv: Sequence[str]) -> GitResult:
    """A ``gh`` that answers the fingerprint's PR probe and refuses everything else."""
    if list(argv[:2]) == ["pr", "list"]:
        return GitResult(exit_code=0, stdout="[]", stderr="", timed_out=False, launch_error=None)
    return GitResult(exit_code=1, stdout="", stderr="no gh", timed_out=False, launch_error=None)


def _manager(git_repo, store: StateStore, art: Path, make_git_config: ConfigFactory) -> GitManager:
    store.insert_task(TaskRow(task_id="task-001", title="t", status=Status.NEW))
    config = make_git_config(git_repo.clone)
    return GitManager(config, store=store, artifacts_root=str(art), gh_runner=_offline_gh)


def _write(path: Path, text: str) -> None:
    # Bytes, not text mode: a Windows text write would put CRLF in the tree and change every blob
    # hash the pinned diff below names.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _fixture_tree(git_repo, git_run: GitRunner) -> None:
    """Seed ``main`` with files to modify, delete and rename, plus a tracked runtime-home file."""
    git_run(["config", "core.autocrlf", "false"], git_repo.clone)
    _write(git_repo.clone / "mod.txt", "a\nb\nc\n")
    _write(git_repo.clone / "gone.txt", "x\ny\n")
    _write(git_repo.clone / "old.txt", "r1\nr2\nr3\nr4\nr5\n")
    _write(git_repo.clone / RUNTIME_EXCLUDED_DIRS[0] / "config.yaml", "poll: 60\n")
    git_run(["add", "-f", "."], git_repo.clone)
    git_run(["commit", "-m", "seed"], git_repo.clone)


def _edit_tree(clone: Path) -> None:
    """What an editing node leaves behind: every kind of change, one untracked file included."""
    _write(clone / "mod.txt", "a\nB\nc\nd\n")
    (clone / "gone.txt").unlink()
    (clone / "old.txt").rename(clone / "new.txt")
    _write(clone / "added.txt", "-- dashes\n")
    _write(clone / RUNTIME_EXCLUDED_DIRS[0] / "config.yaml", "poll: 30\n")


# `current.diff` as the Git Manager writes it for the fixture tree. Pinned so that writing the
# overview in the same call can never change a byte of the artifact every core reader relies on.
_PINNED_DIFF = """\
diff --git a/added.txt b/added.txt
new file mode 100644
index 0000000..895bfc4
--- /dev/null
+++ b/added.txt
@@ -0,0 +1 @@
+-- dashes
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
index b77b4eb..0000000
--- a/gone.txt
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
diff --git a/mod.txt b/mod.txt
index de98044..a7bc997 100644
--- a/mod.txt
+++ b/mod.txt
@@ -1,3 +1,4 @@
 a
-b
+B
 c
+d
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
"""


def test_current_diff_bytes_are_pinned(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _fixture_tree(git_repo, git_run)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    _edit_tree(git_repo.clone)
    diff = Path(gm.write_current_diff("task-001").diff_path)
    # Written in text mode, so the platform's own line separator is part of the pinned bytes.
    assert diff.read_bytes() == _PINNED_DIFF.replace("\n", os.linesep).encode("utf-8")


def _overview(written) -> str:
    raw = Path(written.overview_path).read_bytes()
    # Written as bytes with `\n` on every platform, so the file is the same everywhere.
    assert b"\r" not in raw
    return raw.decode("utf-8")


def test_the_overview_lists_exactly_what_the_diff_holds(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    _fixture_tree(git_repo, git_run)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    _edit_tree(git_repo.clone)
    written = gm.write_current_diff("task-001")
    overview = _overview(written)
    summary = summarize_diff(Path(written.diff_path).read_text(encoding="utf-8"))
    base = git_run(["rev-parse", "main"], git_repo.clone)
    assert Path(written.overview_path).parent == Path(written.diff_path).parent
    assert written.base_sha == base
    assert overview == (
        f"{TASK_BASE_TITLE} {base} — 4 files, +{summary.insertions} −{summary.deletions}, "
        f"full diff {len(_PINNED_DIFF.encode())} B\n"
        "\n"
        "A  added.txt  +1  −0\n"
        "D  gone.txt  +0  −2\n"
        "M  mod.txt  +2  −1\n"
        "R  old.txt → new.txt  +0  −0\n"
    )
    # The untracked file is listed, the runtime home is not — the same set the diff carries.
    listed = [line.split("  ")[1].split(" → ")[-1] for line in overview.splitlines()[2:]]
    assert listed == list(summary.paths)
    assert RUNTIME_EXCLUDED_DIRS[0] not in overview
    # The transient intent-to-add left the untracked file untracked.
    assert any(
        e.status == "??" and e.path == "added.txt" for e in gm.changed_code_entries("task-001")
    )


def test_the_base_is_the_recorded_start_commit_on_an_existing_branch(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # On a chain branch the change is measured from the branch tip at task start, not from
    # `main`, so the base the overview names is that commit — a full SHA, never a branch name.
    git_run(["checkout", "-b", "feature/chain"], git_repo.clone)
    _write(git_repo.clone / "prior.py", "prior = 1\n")
    git_run(["add", "prior.py"], git_repo.clone)
    git_run(["commit", "-m", "prior task"], git_repo.clone)
    start = git_run(["rev-parse", "HEAD"], git_repo.clone)
    git_run(["checkout", "main"], git_repo.clone)
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch(
        "task-001", "x", epoch=_EPOCH, mode=BranchMode.EXISTING, branch_ref="feature/chain"
    )
    _write(git_repo.clone / "this.py", "this = 2\n")
    written = gm.write_current_diff("task-001")
    assert written.base_sha == start and re.fullmatch(r"[0-9a-f]{40}", start)
    overview = _overview(written)
    assert overview_base(overview) == start
    assert "this.py" in overview and "prior.py" not in overview


def test_a_subtask_commit_adds_a_section_for_what_changed_since(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    _write(git_repo.clone / "first.py", "one = 1\n")
    # Before the orchestrator commits anything there is one scope, and one section.
    assert LAST_COMMIT_TITLE not in _overview(gm.write_current_diff("task-001"))

    sha = gm.commit_subtask("task-001", 1, "first", "feat: subtask 1")
    _write(git_repo.clone / "second.py", "two = 2\n")
    _write(git_repo.clone / "README.md", "# project\nmore\n")
    overview = _overview(gm.write_current_diff("task-001"))
    whole, since = overview.split(f"{LAST_COMMIT_TITLE} ")
    assert since.startswith(f"{sha} — 2 files, +2 −0")
    since_paths = {line.split("  ")[1] for line in since.splitlines()[2:]}
    whole_paths = {line.split("  ")[1] for line in whole.splitlines()[2:] if line}
    assert since_paths == {"README.md", "second.py"}
    assert whole_paths == {"README.md", "first.py", "second.py"}
    assert since_paths <= whole_paths


def test_a_denied_path_is_marked_and_a_secret_in_a_path_is_redacted(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    secret = "zq81Xv0pLmN4tRw7"
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    _write(git_repo.clone / ".env", f"K={secret}\n")
    _write(git_repo.clone / "notes" / f"{secret}.txt", "n\n")
    written = gm.write_current_diff("task-001")
    overview = _overview(written)
    assert "A  .env  +1  −0  [denied]" in overview
    assert secret not in overview
    assert f"notes/{REDACTED}.txt" in overview


def test_an_empty_change_still_writes_an_overview(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory
) -> None:
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    written = gm.write_current_diff("task-001")
    assert "— 0 files, +0 −0, full diff 0 B\n" in _overview(written)


def test_a_base_that_does_not_resolve_writes_nothing(
    git_repo, store: StateStore, tmp_path: Path, make_git_config: ConfigFactory, git_run: GitRunner
) -> None:
    # A change measured from nowhere is not handed to anyone: the call raises the Git Manager's own
    # error, before the index is touched, and leaves the previous diff/overview pair as it was.
    gm = _manager(git_repo, store, tmp_path / "art", make_git_config)
    gm.prepare_branch("task-001", "x", epoch=_EPOCH)
    _write(git_repo.clone / "a.py", "a = 1\n")
    first = gm.write_current_diff("task-001")
    before = (Path(first.diff_path).read_bytes(), Path(first.overview_path).read_bytes())
    _write(git_repo.clone / "b.py", "b = 1\n")
    store.set_gate_reference("task-001", "f" * 40)
    with pytest.raises(GitCommandError):
        gm.write_current_diff("task-001")
    assert (Path(first.diff_path).read_bytes(), Path(first.overview_path).read_bytes()) == before
    # Nothing was left staged by an intent-to-add that never got its reset.
    assert git_run(["diff", "--cached", "--name-only"], git_repo.clone) == ""
