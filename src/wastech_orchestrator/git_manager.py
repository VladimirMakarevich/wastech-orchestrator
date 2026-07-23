"""Git Manager (.agents/rules/git-workflow.md).

The **only** component that commits, pushes, or opens pull requests — agents never do. Every git
and ``gh`` invocation goes through the P2 safe process runner as an **argv list** (no shell string,
no user-string interpolation), with an allowlisted environment.

Responsibilities:

* branch flow: ``fetch`` → checkout ``base_branch`` → ``pull`` → create the task branch;
* **scoped staging**: stage only the agent's code paths via an explicit pathspec plus a
  belt-and-braces ``:(exclude)tasks/`` guard — **never** ``git add .``;
* the canonical footprint: the orchestrator's runtime files live under the gitignored
  ``<repo>/.worc/`` home, and a separate orchestrator-made audit commit captures the task file plus
  its ``<id>.summary.md`` at the repo root under ``tasks/``;
* idempotent commit/push/PR via an operation fingerprint + remote-state check;
* terminal cleanup back to ``repo.base_branch`` when provably safe;
* the :class:`~wastech_orchestrator.routing.snapshots.SnapshotHook` for partial-change capture.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.config.schema import (
    AuditBranch,
    BranchMode,
    MergeStrategy,
    OrchestratorConfig,
)
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.observability.progress import run_with_heartbeat
from wastech_orchestrator.providers.artifacts import sha256_file, task_artifact_dir
from wastech_orchestrator.providers.process import ProcessResult, run_process
from wastech_orchestrator.providers.redaction import read_denied_secrets, redact_text
from wastech_orchestrator.routing.snapshots import PartialChange, WorkingTreeSnapshot
from wastech_orchestrator.runtime_layout import (
    CONTROL_HOME_DIRNAME,
    EXCHANGE_HOME_DIRNAME,
    ProviderWriteGuardPolicy,
)
from wastech_orchestrator.security.env import build_child_env
from wastech_orchestrator.state_store import PublishOpRow, StateStore
from wastech_orchestrator.task.model import BRANCH_NAME_MAX_LEN
from wastech_orchestrator.task.parser import slugify_bounded

# Git/gh operations are bounded but slower than a trivial command (network fetch/push allowed).
GIT_TIMEOUT_SECONDS = 300

# F13 defense-in-depth: a push can fail for a genuinely transient reason (a network blip, or
# `.git/index.lock` contention with a concurrent git process) that a short retry resolves. Only the
# failures whose stderr matches one of these markers are retried — a deterministic failure (a
# rejected non-fast-forward, a bad pathspec, an auth error) must still fail loudly and immediately,
# so F12 surfaces it, never be masked by retries. Matched case-insensitively against the (redacted)
# git stderr.
_TRANSIENT_GIT_STDERR_MARKERS = (
    "index.lock",
    "another git process",
    "could not resolve host",
    "couldn't resolve host",
    "connection timed out",
    "connection reset",
    "connection refused",
    "operation timed out",
    "failed to connect",
    "temporary failure",
    "the remote end hung up",
    "rpc failed",
    "early eof",
    "unable to access",
    "ssh: connect to host",
)
# Bounded: at most this many extra attempts after the first, with a short fixed backoff each.
_PUSH_MAX_RETRIES = 2
_PUSH_RETRY_BACKOFF_SECONDS = 1.5

# The gitignored runtime home that must never enter a code commit (state.db, logs/, workspace/,
# checks/, config.yaml, orchestrator.pid, …). The configured task lifecycle directory
# (`paths.tasks_dir`, default "tasks") is also excluded from the code commit — it is tracked but
# rides the separate audit commit — but that name is per-config, so it is added per instance (see
# `__init__`). Together they form `self._excluded_dirs`.
RUNTIME_EXCLUDED_DIRS = (CONTROL_HOME_DIRNAME, EXCHANGE_HOME_DIRNAME)

_RUNTIME_IGNORE_COMMENT = (
    "# wastech-orchestrator runtime home + exchange (auto-appended by `worc install`)"
)

# Each runtime root as (probe path, ignore line). The probe is a representative path under the root
# (a directory has no ignorable entry of its own); the ignore line is appended only when that probe
# is not already ignored, so each root is handled independently — a repo that already ignores
# `.worc/` (e.g. an operator's `.worc/*` + `!.worc/flows/` scheme) still gets the `.worc-io/`
# exchange line without a blanket `.worc/` re-append that would defeat the operator's negation.
# `.worc-io/` (WRI-001) is the provider-readable exchange — a sibling runtime root that must also
# never enter a commit. `tasks/` is intentionally NOT ignored — it holds the committed audit trail.
_RUNTIME_IGNORE_ROOTS: tuple[tuple[str, str], ...] = (
    (f"{CONTROL_HOME_DIRNAME}/state.db", f"{CONTROL_HOME_DIRNAME}/"),
    (f"{EXCHANGE_HOME_DIRNAME}/probe", f"{EXCHANGE_HOME_DIRNAME}/"),
)

# The full ignore block (comment + both roots), kept as the public constant install/docs reference.
RUNTIME_GITIGNORE_LINES: tuple[str, ...] = (
    _RUNTIME_IGNORE_COMMENT,
    f"{CONTROL_HOME_DIRNAME}/",
    f"{EXCHANGE_HOME_DIRNAME}/",
)

# WRI-009: the private, empty hooks directory every orchestrator git command points at via
# `-c core.hooksPath`, so a target-repo hook (or an agent-set `core.hooksPath`) can never execute in
# an orchestrator-owned git process. It lives under the private home (a provider-denied root) and
# is created empty once per manager. An *absolute path to a real empty dir* is required — an empty
# value would make git resolve hooks relative to the agent-writable working tree.
GIT_NULL_HOOKS_DIRNAME = "git-null-hooks"

# Orchestrator-injected fixed `-c` overrides applied to *every* git subcommand (a command-line `-c`
# beats repo/global/system config). They neutralize agent-editable, program-launching config knobs:
# the fsmonitor hook/daemon; the editor and sequence editor (we always use `-m`/`--no-edit`, and
# `false` fails closed rather than launching a repo-selected editor); and commit/tag signing (an
# agent-set `gpg.program` never runs because signing never happens — matching the orchestrator's
# current unsigned-commit behavior). `core.hooksPath` is added per-instance (needs the dir path).
_GIT_HARDENING_CONFIG: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.editor", "false"),
    ("sequence.editor", "false"),
    ("commit.gpgsign", "false"),
    ("tag.gpgsign", "false"),
)

# Orchestrator-injected git environment (unconditional — not agent-influenced, so not gated by the
# `security.allowed_environment` allowlist). No credential/host prompt, no editor, no GUI credential
# dialog, no opportunistic index.lock. `GIT_CONFIG_NOSYSTEM` is deliberately NOT set: operator
# system/global config is trusted and holds the credentials push/fetch/gh need; the agent-writable
# surface is repo-local config + `.gitattributes`, handled by the fingerprint and the filter gate.
_GIT_HARDENING_ENV: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
    "GIT_EDITOR": "false",
    "GIT_SEQUENCE_EDITOR": "false",
    "GCM_INTERACTIVE": "Never",
}

# WRI-009: repo-local/worktree config keys whose *value is a program* git would execute during a
# filter (clean/smudge/process), an external/textconv diff, or a fetch/push. A command-line `-c`
# cannot blanket a filter/diff driver — its name comes from `.gitattributes` — so the agent-writable
# config surface is inventoried and any such key is refused. `git config --list` lowercases the
# section/name (subsection preserved), so keys are matched lowercased. Operator global/system config
# is trusted and is not inventoried, so a global `git lfs install` keeps working with zero config.
_FILTER_DRIVER_KEY_RE = re.compile(
    r"^(filter|diff)\.[^.]+\.(clean|smudge|process|command|textconv)$"
)
_PROGRAM_CONFIG_KEYS = frozenset({"core.sshcommand", "credential.helper"})


def _append_missing_lines(target: Path, lines: Sequence[str]) -> list[str]:
    """Idempotently append the ``lines`` not already present in ``target`` (one entry per line).

    Creates parent dirs and the file as needed, preserves existing content, and separates the
    appended block with a blank line when the file does not already end on one. Returns the lines
    actually appended (empty when all were present).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    present = {line.strip() for line in existing}
    additions = [line for line in lines if line.strip() not in present]
    if additions:
        with target.open("a", encoding="utf-8") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("\n".join(additions) + "\n")
    return additions


def _git_path_ignored(repo_root: str | Path, probe: str) -> bool:
    """Whether ``git check-ignore`` reports ``probe`` as ignored in ``repo_root`` (exit code 0)."""
    with tempfile.TemporaryDirectory() as scratch:
        stdout_path = Path(scratch) / "stdout"
        result = run_process(
            ["git", "check-ignore", "-q", probe],
            cwd=repo_root,
            env=dict(os.environ),
            timeout_seconds=30,
            stdout_path=str(stdout_path),
        )
    return result.exit_code == 0


def _missing_runtime_ignore_lines(is_ignored: Callable[[str], bool]) -> list[str]:
    """The ignore lines for the runtime roots not yet covered, per root (empty when all covered).

    Each root (``.worc/``, ``.worc-io/``) is decided independently against its own probe, so an
    operator's own ``.worc/*`` + ``!.worc/flows/`` scheme (see ``docs/how-to.md``) is never stomped:
    when ``.worc/`` is already ignored, the blanket ``.worc/`` line is skipped (re-appending it
    would silently re-exclude ``.worc/flows/`` — a parent-dir exclusion from any source blocks
    re-inclusion of its children), while the ``.worc-io/`` exchange line is still added if missing
    (WRI-001). The comment header rides along only when at least one root line is added.
    """
    lines = [line for probe, line in _RUNTIME_IGNORE_ROOTS if not is_ignored(probe)]
    return [_RUNTIME_IGNORE_COMMENT, *lines] if lines else []


def append_runtime_excludes(repo_root: str | Path) -> list[str]:
    """Idempotently add the ``.worc/`` + ``.worc-io/`` ignore lines to the tracked ``.gitignore``.

    Adds only the runtime roots not already covered by an existing rule (per root — see
    :func:`_missing_runtime_ignore_lines`). Returns the lines actually appended — empty when every
    root was already ignored.
    """
    lines = _missing_runtime_ignore_lines(lambda probe: _git_path_ignored(repo_root, probe))
    if not lines:
        return []
    return _append_missing_lines(Path(repo_root) / ".gitignore", lines)


# publish_operations.kind values (idempotency keys).
KIND_CODE_COMMIT = "code_commit"
KIND_SUBTASK_COMMIT = "subtask_commit"
KIND_AUDIT_COMMIT = "audit_commit"
KIND_MERGE_COMMIT = "merge_commit"
KIND_PUSH = "push"
KIND_PR = "pr"
KIND_PR_MERGE = "pr_merge"

# Substrings in a (redacted) ``gh pr merge`` failure that mean the PR is already merged/closed — an
# idempotent success (a crash dropped the op row after a real merge, or a human merged out of band),
# never a re-merge. Conflict/branch-protection failures deliberately do NOT match.
_ALREADY_MERGED_MARKERS = ("already merged", "already been merged", "not open", "was merged")

_STATUS_STARTED = "started"
_STATUS_COMPLETED = "completed"
_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class GitResult:
    """Raw outcome of one git/gh invocation."""

    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    launch_error: str | None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.launch_error is None


@dataclass(frozen=True)
class CleanupOutcome:
    """The terminal-cleanup decision."""

    safe: bool
    target_branch: str
    error: str | None = None


@dataclass(frozen=True)
class ChangedPath:
    """One repository path changed against HEAD, normalized from Git's name-status output."""

    status: str
    path: str
    previous_path: str | None = None


# --- machine-safe Git path parsing (NUL-delimited `-z` output) ------------------------------
#
# Git's default text output C-quotes any path with a non-ASCII/space/quote/control byte (e.g. a
# Cyrillic filename becomes `"\320\274..."`), so trimming lines assumes an invariant Git does not
# hold. `-z` output is NUL-delimited and never quotes paths, so every path-bearing command below is
# run with `-z` and parsed by one of these three helpers instead of `str.splitlines()`.


def _parse_name_only_z(output: str) -> list[str]:
    """Paths from a ``--name-only -z`` record stream (also fits plain ``ls-files -z``)."""
    return [item for item in output.split("\0") if item]


def _parse_name_status_z(output: str) -> list[tuple[str, str, str | None]]:
    """``(status, path, previous_path)`` records from ``git diff --name-status -z`` output.

    Each record is one status token followed by one path token, except a rename/copy (a status
    starting with ``R``/``C``, e.g. ``R100``) which carries two path tokens — the original path,
    then the new one — the same field order as the tab-separated non-``-z`` form.
    """
    tokens = [t for t in output.split("\0") if t]
    entries: list[tuple[str, str, str | None]] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if status[:1] in ("R", "C") and i + 2 < len(tokens):
            entries.append((status, tokens[i + 2], tokens[i + 1]))
            i += 3
        elif i + 1 < len(tokens):
            entries.append((status, tokens[i + 1], None))
            i += 2
        else:  # malformed trailing token — nothing to pair it with
            break
    return entries


def _parse_porcelain_status_z(output: str) -> list[tuple[str, str]]:
    """``(code, path)`` records from ``git status --porcelain -z`` output.

    Each record is ``"XY <path>"`` NUL-terminated, except a rename/copy (``X`` or ``Y`` is ``R``/
    ``C``) which is followed by one extra NUL-terminated token carrying the *original* path. Per
    Git's own docs, the ``-z`` field order for a rename is reversed versus the line-based ``"old ->
    new"`` form — the destination path is the one in the ``"XY <path>"`` record, the source trails
    it — so the destination is read directly and the trailing source token is skipped: no current
    caller needs the rename source.
    """
    tokens = [t for t in output.split("\0") if t]
    entries: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens):
        record = tokens[i]
        code, path = record[:2], record[3:]
        i += 1
        if code[0] in ("R", "C") or code[1] in ("R", "C"):
            if i >= len(tokens):  # a rename/copy record truncated mid-stream — drop it, don't crash
                break
            i += 1  # skip the rename/copy source path
        entries.append((code, path))
    return entries


# --- Git control-state fingerprint (WRI-009) ------------------------------------------------
#
# A provider with workspace write can mutate the clone's Git *control* state — the index, HEAD/refs,
# repo-local config, hooks, and operation markers — not just ordinary working-tree files (which are
# the point of the run). The orchestrator fingerprints that control state immediately before a
# workspace-write attempt and compares it after WRI-012 proves the provider process tree quiescent;
# any change is a non-fallback `manual_action_required` policy violation. The fingerprint keeps only
# identities and content/value *hashes* (never raw config values or hook bytes), so it carries no
# secret, and the drift evidence is redacted path/key/name level only.

# Operation-control markers under the gitdir/common-dir; presence means a merge/rebase/etc. is live.
_CONTROL_MARKERS: tuple[str, ...] = (
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
)
# Cap on the number of individual drift items rendered into a manual-action reason (avoid an
# unbounded reason string when, e.g., many index entries changed at once).
_DRIFT_EVIDENCE_CAP = 20


def _parse_ls_files_stage_z(output: str) -> dict[str, tuple[str, str, str]]:
    """``{path: (mode, blob_sha, stage)}`` from ``git ls-files --stage -z`` output.

    Each record is ``<mode> <sha> <stage>\\t<path>`` NUL-terminated. An intent-to-add entry carries
    the all-zero blob sha, so a provider ``git add -N`` or ``git add -f`` surfaces as a new/changed
    index entry — this is the most complete index probe (add/modify/delete *and* intent-to-add).
    """
    entries: dict[str, tuple[str, str, str]] = {}
    for record in output.split("\0"):
        if not record or "\t" not in record:
            continue
        meta, path = record.split("\t", 1)
        parts = meta.split()
        if len(parts) != 3:  # malformed line — skip rather than crash
            continue
        mode, blob_sha, stage = parts
        entries[path] = (mode, blob_sha, stage)
    return entries


@dataclass(frozen=True)
class HookFacts:
    """Identity of one entry in the effective git hooks directory (WRI-009)."""

    kind: str  # "file" | "symlink" | "dir" | "other"
    target: str | None  # POSIX symlink target when kind == "symlink"
    content_sha: str | None  # sha256 of bytes when kind == "file"
    executable: bool


@dataclass(frozen=True)
class GitControlState:
    """Fingerprint of the Git control surfaces a provider attempt must not mutate (WRI-009).

    ``config`` maps each repo-local (+worktree) key to the sha256 of each of its values — hashes,
    never raw values, since a value can be secret-bearing (e.g. a remote URL or a signing-key path).
    ``index`` blob shas and ``hooks`` content shas are already content-derived. Nothing here is a
    raw secret, so the object is safe to hold in parent memory for the attempt window.
    """

    head_symref: str | None  # `symbolic-ref HEAD` (None == detached)
    head_commit: str  # `rev-parse HEAD` ("" when unborn)
    task_ref: str | None  # active branch refname, e.g. "refs/heads/worc/x"
    task_ref_commit: str | None  # value of task_ref (None when missing)
    index: dict[str, tuple[str, str, str]]  # path -> (mode, blob-sha, stage)
    config: dict[str, tuple[str, ...]]  # repo-local(+worktree) key -> value sha256s
    hooks_path: str | None  # `core.hooksPath` (None when unset)
    hooks: dict[str, HookFacts]  # entry name -> facts, for the effective hooks dir
    markers: frozenset[str]  # present operation-control markers


@dataclass(frozen=True)
class GitControlDriftItem:
    """One redacted control-state change (path/key/name level only — never a value or content)."""

    aspect: str  # index | head | task_ref | config | hooks | markers
    detail: str


@dataclass(frozen=True)
class GitControlDrift:
    """One or more control-state changes detected across a provider attempt (WRI-009)."""

    items: tuple[GitControlDriftItem, ...]

    def summary(self) -> str:
        """A bounded, redacted one-line reason for the manual-action result."""
        shown = self.items[:_DRIFT_EVIDENCE_CAP]
        text = "; ".join(f"{it.aspect}: {it.detail}" for it in shown)
        if len(self.items) > _DRIFT_EVIDENCE_CAP:
            text += f"; (+{len(self.items) - _DRIFT_EVIDENCE_CAP} more)"
        return text


class GitCommandError(Exception):
    """A git/gh command exited non-zero (or failed to launch) where success was required."""


class ManualActionRequired(Exception):
    """A condition the Core must surface as ``manual_action_required`` (e.g. preflight)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


CommandRunner = Callable[[Sequence[str]], GitResult]


@dataclass
class _ActiveTask:
    task_id: str
    slug: str
    branch: str
    partial_counter: int = 0
    # F32: the commit the working branch sat at when THIS task started. Set only for
    # ``existing``/``current`` (chain-continuation) modes, where the branch already carries prior
    # tasks' commits; ``None`` for ``new`` (the branch is cut fresh from ``base_branch``, so the
    # config base is exactly the task's start). Diffs use it so review/docs see only this task's
    # change, not the whole unmerged chain.
    base_ref: str | None = None


class GitManager:
    """Drives all git/gh operations for the target clone. Also satisfies ``SnapshotHook``."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        store: StateStore,
        artifacts_root: str | Path,
        gh_runner: CommandRunner | None = None,
        run_process: Callable[..., ProcessResult] = run_process,
        heartbeat_seconds: float = 30.0,
    ) -> None:
        self._config = config
        self._store = store
        self._artifacts_root = artifacts_root
        self._clone = config.repo.local_path
        # The configured task lifecycle dir is tracked but rides the audit commit, so it is excluded
        # from the code commit alongside the gitignored `.worc/` runtime home.
        self._tasks_dir = config.paths.tasks_dir
        self._excluded_dirs = (*RUNTIME_EXCLUDED_DIRS, self._tasks_dir)
        # WRI-009: orchestrator-injected no-prompt/no-editor git env on top of the security
        # allowlist; applies to both `git` (`_run`) and `gh` (`_gh`), which shells out to git.
        self._env = {
            **build_child_env(config.security.allowed_environment),
            **_GIT_HARDENING_ENV,
        }
        # WRI-009: an empty hooks dir every git command points at (see `_harden_git_argv`), so no
        # target-repo hook runs in an orchestrator git process. Absolute, real, empty.
        self._null_hooks_dir = Path(self._artifacts_root) / GIT_NULL_HOOKS_DIRNAME
        self._null_hooks_dir.mkdir(parents=True, exist_ok=True)
        self._run_process = run_process
        self._gh_runner = gh_runner
        self._heartbeat_seconds = heartbeat_seconds
        self._active: _ActiveTask | None = None
        # Whether the task lifecycle dir is gitignored (cached; drives the code-commit pathspec).
        self._tasks_dir_ignored_cache: bool | None = None
        # Injectable backoff for the transient-push retry (F13); real time in production, patched in
        # tests so the bounded retry never actually sleeps.
        self._sleep: Callable[[float], None] = time.sleep

    # --- low-level command execution ------------------------------------------------------

    def _run(self, argv: Sequence[str]) -> GitResult:
        """Run an argv list in the clone via the safe process runner; capture stdout + stderr.

        Every ``git`` invocation is hardened first (WRI-009, :meth:`_harden_git_argv`) so a
        target-repo hook/filter/editor/pager/signing program can never execute in an
        orchestrator-owned git process. ``gh`` argv is left unchanged (env-only hardening).
        """
        argv = list(argv)
        hardened = self._harden_git_argv(argv)
        with tempfile.TemporaryDirectory() as scratch:
            stdout_path = Path(scratch) / "stdout"
            context: dict[str, object] = {"component": argv[0] if argv else "process"}
            if self._active is not None:
                context["task_id"] = self._active.task_id
            log = bind(_LOG, **context)
            operation = argv[1] if len(argv) > 1 else "launch"  # original argv → real subcommand
            result = run_with_heartbeat(
                lambda: self._run_process(
                    hardened,
                    cwd=self._clone,
                    env=self._env,
                    timeout_seconds=GIT_TIMEOUT_SECONDS,
                    stdout_path=str(stdout_path),
                ),
                logger=log,
                message="git operation heartbeat",
                interval_seconds=self._heartbeat_seconds,
                fields={"operation": operation, "timeout_seconds": GIT_TIMEOUT_SECONDS},
            )
            stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        return GitResult(
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=redact_text(result.stderr_text or ""),
            timed_out=result.timed_out,
            launch_error=result.launch_error,
        )

    def _harden_git_argv(self, argv: list[str]) -> list[str]:
        """Insert the WRI-009 hardening prefix into a ``git`` argv (leaves ``gh``/other argv as-is).

        Produces ``git --no-pager -c core.hooksPath=<empty> -c ... <subcommand> ...``. A
        command-line ``-c`` overrides repo/global/system config, beating a repo that sets its own
        hooks path or a program-launching config key. Every patch/textconv-capable ``diff`` also
        gets ``--no-textconv --no-ext-diff`` so a repo-selected textconv/external-diff driver never
        runs (harmless on ``--name-only``/``--name-status``/``--stat``/``--cached --check``).
        """
        if not argv or argv[0] != "git":
            return argv
        prefix = ["--no-pager", "-c", f"core.hooksPath={self._null_hooks_dir.as_posix()}"]
        for key, value in _GIT_HARDENING_CONFIG:
            prefix += ["-c", f"{key}={value}"]
        rest = argv[1:]
        if rest and rest[0] == "diff":
            rest = ["diff", "--no-textconv", "--no-ext-diff", *rest[1:]]
        return [argv[0], *prefix, *rest]

    def _git(self, *args: str) -> GitResult:
        return self._run(["git", *args])

    def _git_checked(self, *args: str) -> str:
        result = self._git(*args)
        if not result.ok:
            raise GitCommandError(
                f"git {' '.join(args)} failed (exit={result.exit_code}): {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _git_checked_retryable(self, *args: str) -> str:
        """Like :meth:`_git_checked` but retries a *transient* failure with a short bounded backoff
        (F13). A deterministic failure (stderr not matching :data:`_TRANSIENT_GIT_STDERR_MARKERS`)
        re-raises on the first attempt — retries must never mask a real bug (F12 still surfaces it).
        """
        for attempt in range(_PUSH_MAX_RETRIES + 1):
            try:
                return self._git_checked(*args)
            except GitCommandError as exc:
                lowered = str(exc).lower()
                transient = any(marker in lowered for marker in _TRANSIENT_GIT_STDERR_MARKERS)
                if not transient or attempt == _PUSH_MAX_RETRIES:
                    raise
                context: dict[str, object] = {"operation": args[0] if args else "git"}
                if self._active is not None:
                    context["task_id"] = self._active.task_id
                bind(_LOG, **context).warning(
                    "transient git failure; retrying",
                    extra={"attempt": attempt + 1, "error": str(exc)},
                )
                self._sleep(_PUSH_RETRY_BACKOFF_SECONDS)
        raise AssertionError("unreachable")  # pragma: no cover — the loop always returns or raises

    def _gh(self, args: Sequence[str]) -> GitResult:
        """Run GitHub CLI arguments, adding the ``gh`` executable exactly once."""
        if self._gh_runner is not None:
            return self._gh_runner(args)
        return self._run(["gh", *args])

    def list_tracked_skill_files(self) -> tuple[str, ...]:
        """Repo-relative POSIX paths of every tracked ``SKILL.md`` (whole-repo skill discovery).

        Enumerates tracked files via ``git ls-files`` (ignore-aware and bounded for free — untracked
        ``node_modules``/build/vendor trees never appear) and keeps the ``SKILL.md`` basenames,
        wherever they sit in the tree. ``ls-files`` emits forward-slash paths on every platform.
        Best-effort: a working copy with no git data (some tests) yields ``()`` so the inventory is
        simply empty rather than failing the task.
        """
        result = self._git("ls-files", "-z")
        if not result.ok:
            return ()
        return tuple(
            item for item in result.stdout.split("\0") if item and item.split("/")[-1] == "SKILL.md"
        )

    def list_tracked_files(self, *pathspecs: str) -> tuple[str, ...]:
        """Repo-relative POSIX paths of every tracked file, optionally under ``pathspecs``.

        Used by WRI-011 to discover which root repository-instruction files (``AGENTS.md`` etc.) are
        tracked and to enumerate a selected skill's package closure (``ls-files -- <package-dir>``).
        ``ls-files`` is ignore-aware, bounded (untracked build/vendor trees never appear), and emits
        forward-slash paths on every platform. Best-effort: a working copy with no git data yields
        ``()`` so a caller degrades to "nothing tracked" rather than failing the task.
        """
        args = ["ls-files", "-z"]
        if pathspecs:
            args += ["--", *pathspecs]
        result = self._git(*args)
        if not result.ok:
            return ()
        return tuple(item for item in result.stdout.split("\0") if item)

    # --- branch flow ----------------------------------------------------------------------

    def branch_name(
        self, task_id: str, slug: str, *, epoch: int, override: str | None = None
    ) -> str:
        if override:
            return override
        fixed = f"{self._config.repo.branch_prefix}/{epoch}-{task_id}"
        # -1 reserves the dash that joins {fixed} and the slug; slugify_bounded returns "" (slug
        # segment omitted) once {fixed} alone fills the BRANCH_NAME_MAX_LEN budget.
        bounded = slugify_bounded(slug, BRANCH_NAME_MAX_LEN - len(fixed) - 1)
        return f"{fixed}-{bounded}" if bounded else fixed

    def prepare_branch(
        self,
        task_id: str,
        slug: str,
        *,
        epoch: int,
        branch_name: str | None = None,
        mode: BranchMode = BranchMode.NEW,
        branch_ref: str | None = None,
    ) -> str:
        """Attach the task's working branch per :class:`BranchMode`. Returns its name.

        ``new`` (owned): once the auto-named task branch already exists (a restart or a
        ``--continue`` resume), this reattaches to it directly — never by way of ``base_branch`` —
        so a legitimate in-progress WIP on it is never at risk of a checkout conflict; already being
        on it is a no-op. Only the first creation (branch does not exist yet) fetches, checks out
        ``base_branch``, ``pull --ff-only``s, then branches off it. ``existing`` / ``current`` are
        operator-owned — this never fast-forwards, resets, or aborts a merge on them; it only
        performs a plain checkout (``existing``) or nothing at all (``current``), so the operator's
        local state is preserved.
        """
        # WRI-009: no orchestrator git (incl. the checkout below, which runs smudge filters) may run
        # a repo-local program-launching driver.
        self._assert_no_untrusted_filters()
        if mode is BranchMode.EXISTING:
            branch = self._prepare_existing(task_id, slug, branch_ref)
        elif mode is BranchMode.CURRENT:
            branch = self._prepare_current(task_id, slug)
        else:
            branch = self._prepare_new(task_id, slug, epoch=epoch, branch_name=branch_name)
        # WRI-009: with the branch attached, refuse to start if a non-artifact entry is already
        # staged — a bare ``git commit`` would sweep it into the task's scoped commit. existing/
        # current never reset, so this is a fail-closed refusal; unstaged edits are left untouched.
        self.assert_index_clean_at_start()
        return branch

    def _prepare_new(self, task_id: str, slug: str, *, epoch: int, branch_name: str | None) -> str:
        """``new`` (owned) mode: reattach to the existing auto-named branch, or first-create it."""
        branch = self.branch_name(task_id, slug, epoch=epoch, override=branch_name)
        self._active = _ActiveTask(task_id=task_id, slug=slug, branch=branch)

        # Clear a stale in-progress merge (e.g. a killed ``merge-task``) so a checkout below cannot
        # wedge on "you need to resolve your current index first". No-op in the normal case.
        self.merge_abort()
        if self._branch_exists(branch):
            # Reuse on restart/continue, never recreate — and never by way of base_branch, so any
            # uncommitted WIP already on the branch is left untouched. Already there is a no-op.
            if self.current_branch() != branch:
                self._git_checked("checkout", branch)
            return branch

        base = self._config.repo.base_branch
        # Fetch is best-effort: a repo without a remote (some tests) still proceeds locally.
        self._git("fetch", "origin")
        self._git_checked("checkout", base)
        self._git("pull", "--ff-only")
        self._git_checked("checkout", "-b", branch)
        return branch

    def _prepare_existing(self, task_id: str, slug: str, branch_ref: str | None) -> str:
        """``existing`` mode: check out an operator-owned branch (creating a local tracking branch
        from ``origin/<ref>`` when only the remote exists). Never ff/reset — a plain checkout."""
        if not branch_ref:  # defensive: the gate requires branch_ref for `existing`
            raise GitCommandError("branch_mode 'existing' requires branch_ref")
        self._active = _ActiveTask(task_id=task_id, slug=slug, branch=branch_ref)
        self._git("fetch", "origin")
        if self._branch_exists(branch_ref):
            self._git_checked("checkout", branch_ref)
        elif self._remote_branch_exists(branch_ref):
            # Only a remote ref exists → create a local branch tracking it (no reset of anything).
            self._git_checked("checkout", "-b", branch_ref, f"origin/{branch_ref}")
        else:  # defensive: the pre-branch preflight already verified existence
            raise GitCommandError(f"branch_ref {branch_ref!r} does not exist locally or on origin")
        self._active.base_ref = self._head_sha()  # F32: chain start = this branch's current tip
        return branch_ref

    def _prepare_current(self, task_id: str, slug: str) -> str:
        """``current`` mode: use the working tree's current branch as-is — no switch, no fetch, no
        ``pull``, no clean-tree requirement. A detached HEAD has no branch and is rejected."""
        branch = self.current_branch()
        if branch is None:  # defensive: the preflight rejects a detached HEAD for `current`
            raise GitCommandError("branch_mode 'current' requires a branch (HEAD is detached)")
        self._active = _ActiveTask(task_id=task_id, slug=slug, branch=branch)
        self._active.base_ref = self._head_sha()  # F32: chain start = this branch's current tip
        return branch

    def _head_sha(self) -> str | None:
        """The current ``HEAD`` commit SHA, or ``None`` when it cannot be resolved (empty repo)."""
        return self._git("rev-parse", "HEAD").stdout.strip() or None

    def _diff_base(self) -> str:
        """The ref the task's change is diffed against: the per-task chain start (F32) when set,
        else the config ``base_branch``. Equal to ``base_branch`` for ``new`` mode, so a
        non-chained run's diffs are unchanged; for ``existing``/``current`` it is the branch tip at
        task start, so review/docs/PR body see only this task's change, not the whole chain."""
        if self._active is not None and self._active.base_ref:
            return self._active.base_ref
        return self._config.repo.base_branch

    def current_branch(self) -> str | None:
        """The working tree's symbolic branch name, or ``None`` when HEAD is detached."""
        name = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if not name or name == "HEAD":  # empty (no commits) or detached HEAD → no branch
            return None
        return name

    def local_or_remote_branch_exists(self, branch: str) -> bool:
        """Whether ``branch`` exists locally or on ``origin`` (fetch first). The ``existing``-mode
        preflight probe: fetch is best-effort so an offline repo still checks the local ref."""
        self._git("fetch", "origin")
        return self._branch_exists(branch) or self._remote_branch_exists(branch)

    def reset_branch_to_base(
        self,
        task_id: str,
        slug: str,
        *,
        branch_name: str | None = None,
        force_reset_remote: bool = False,
    ) -> str:
        """Delete the stale task branch so a fresh ``rerun`` rebuilds it from the current base.

        The complement of ``prepare_branch``'s deliberate *reuse*: a failed attempt's commits would
        otherwise be reused (and stack on a base that moved on). The caller has already verified the
        tree is clean (fail-closed), so ``checkout base`` is safe; deleting the branch while on base
        lets the subsequent ``prepare_branch`` take its ``checkout -b`` arm from current base. Force
        (``-D``) because a failed attempt's commits are unmerged. Idempotent: a missing branch is a
        no-op, so re-running ``rerun`` after an interruption is safe. Returns the branch name.
        """
        base = self._config.repo.base_branch
        # The caller (rerun) always supplies the stored ``branch_name``, so the epoch here is
        # shadowed by that override; it only mints a name in the degenerate "no stored branch" case,
        # where ``delete_branch`` below is a no-op anyway.
        branch = self.branch_name(task_id, slug, epoch=int(time.time()), override=branch_name)
        self._git("fetch", "origin")
        self._git_checked("checkout", base)
        self._git("pull", "--ff-only")
        if force_reset_remote and self._remote_branch_exists(branch):
            # Best-effort: deleting the remote branch makes GitHub auto-close any open PR on it.
            self._git("push", "origin", "--delete", branch)
        self.delete_branch(branch)
        return branch

    def delete_branch(self, branch: str) -> bool:
        """Force-delete a local branch if it exists (idempotent). Returns whether it deleted.

        Used by ``finalize`` to tidy the now-unneeded agent branch (opt-in) and by
        ``reset_branch_to_base`` for the ``rerun`` reset. ``-D`` because a terminal task's commits
        may be unmerged; the caller must already be on another branch (``checkout base`` first).
        """
        if not self._branch_exists(branch):
            return False
        self._git_checked("branch", "-D", branch)
        return True

    def unaccounted_dirty_paths(self) -> set[str]:
        """Public read probe for the ``rerun`` fail-closed dirty-tree gate (no mutation)."""
        return self._unaccounted_dirty_paths()

    def remote_branch_exists(self, branch: str) -> bool:
        """Public read probe: does ``origin`` still carry this branch? (``rerun`` refuse-gate)."""
        return self._remote_branch_exists(branch)

    def recorded_pr_url(self, task_id: str) -> str | None:
        """The PR URL a prior attempt recorded (completed ``pr`` publish op), or ``None``."""
        existing = self._store.get_publish_op(task_id, KIND_PR, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref
        return None

    def verify_pr_state(self, pr_url: str) -> str | None:
        """Read-only PR state for ``finalize``'s merge check: ``MERGED``/``OPEN``/``CLOSED``/None.

        Runs `gh pr view <url> --json state` — strictly **read-only** (never creates/pushes/merges),
        so it does not weaken the security policy. Best-effort: returns ``None`` when ``gh`` is
        missing / unauthenticated / offline or the PR is gone, so the caller can skip the check.
        """
        result = self._gh(["pr", "view", pr_url, "--json", "state", "-q", ".state"])
        if not result.ok:
            return None
        return result.stdout.strip() or None

    def pr_merge_state(self, pr_url: str) -> tuple[str | None, str | None]:
        """Read-only ``(state, merge_commit_sha)`` for a PR: the dependency-readiness probe.

        Runs a single `gh pr view <url> --json state,mergeCommit` — strictly **read-only** (no
        ``--admin``, never creates/pushes/merges), so it does not weaken the security policy. The
        SHA is present only once the PR is ``MERGED`` (``None`` otherwise). Best-effort: returns
        ``(None, None)`` when ``gh`` is missing / unauthenticated / offline or the PR is gone, so
        the caller can treat an unconfirmable merge as "not yet" and skip.
        """
        result = self._gh(["pr", "view", pr_url, "--json", "state,mergeCommit"])
        if not result.ok:
            return None, None
        try:
            data = json.loads(result.stdout or "{}")
        except ValueError:
            return None, None
        state = data.get("state") or None
        merge_commit = data.get("mergeCommit") or {}
        sha = (merge_commit.get("oid") if isinstance(merge_commit, dict) else None) or None
        return state, sha

    def backfill_merge_sha(self, task_id: str, sha: str) -> None:
        """Replace an armed ``pr_merge`` outcome with the real merge SHA, once observed merged.

        Closes the "armed PR never records its real SHA" gap for any task that has a dependent: the
        readiness probe (:meth:`pr_merge_state`) observed ``MERGED`` and hands the merge oid here.
        Updates **only** the SQLite ``pr_merge`` publish op (the authoritative merge-outcome store);
        the append-only ledger keeps its point-in-time ``"armed"`` record untouched. Idempotent: a
        no-op when there is no recorded merge op or its ``result_ref`` is already ``sha``.
        """
        if not sha:
            return
        existing = self._store.get_publish_op(task_id, KIND_PR_MERGE, None)
        if existing is None or existing.status != _STATUS_COMPLETED or existing.result_ref == sha:
            return
        self._record_completed(task_id, KIND_PR_MERGE, existing.fingerprint, sha)

    def refresh_base(self) -> None:
        """Best-effort fetch + ff-only pull of ``base_branch`` so git-pushed tasks become visible.

        Periodic discovery for the ``watch`` loop. A no-op unless HEAD is already on
        ``base_branch`` (i.e. the slot is free after terminal cleanup), so it never disturbs an
        active task branch. Both git calls are best-effort: a repo without a remote or a
        non-fast-forwardable base simply leaves the working copy untouched.
        """
        base = self._config.repo.base_branch
        current = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        if current != base:
            return
        self._git("fetch", "origin")
        self._git("pull", "--ff-only")

    def _branch_exists(self, branch: str) -> bool:
        return self._git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").ok

    # --- operator-driven merge (worc merge-task) ------------------------------------------

    def update_branch_with_base(self, branch: str, base: str) -> bool:
        """Merge ``origin/<base>`` into the task ``branch`` in the clone; True iff it conflicts.

        Checks out the branch, fetches, then ``git merge --no-commit origin/<base>`` — a **merge
        commit**, not a rebase (no history rewrite of reviewed commits, no force-push). WRI-009:
        ``--no-commit`` means a clean 3-way merge is left *staged* with ``MERGE_HEAD`` live rather
        than auto-committed, so the caller finalizes it through the gated
        :meth:`commit_merge_resolution` (which proves the staged set) — an auto-commit would bypass
        that gate and could sweep a pre-staged foreign entry. A fast-forward moves the ref with no
        commit; a conflicting merge stops with ``MERGE_HEAD`` live and markers in the tree for the
        caller to resolve + commit (or abort). Only the operator merge routine calls this.
        """
        # WRI-009: the merge checks out/merges files, running smudge filters — refuse first.
        self._assert_no_untrusted_filters()
        self._git_checked("checkout", branch)
        self._git("fetch", "origin")
        result = self._git("merge", "--no-commit", "--no-edit", f"origin/{base}")
        if result.ok:
            return False  # clean (staged with MERGE_HEAD / fast-forward / already up to date)
        if self.merge_in_progress():
            return True  # the expected, recoverable conflict: MERGE_HEAD live, markers in the tree
        # Non-zero without MERGE_HEAD: the merge failed for another reason (bad ref / no remote).
        raise GitCommandError(f"git merge origin/{base} failed: {result.stderr.strip()}")

    def merge_in_progress(self) -> bool:
        """True iff a merge is mid-flight in the clone (``MERGE_HEAD`` exists)."""
        return self._git("rev-parse", "-q", "--verify", "MERGE_HEAD").ok

    def merge_abort(self) -> None:
        """Abort an in-progress merge to restore the working tree. No-op when none is in flight.

        Best-effort (never raises): it runs in the merge routine's cleanup/``finally`` and on entry
        to clear a stale merge left by a crash.
        """
        if self.merge_in_progress():
            self._git("merge", "--abort")

    def push_branch_update(self, branch: str) -> None:
        """Fast-forward the remote task branch with its new local commits (the base-merge commit).

        Unlike :meth:`push`, this is NOT gated by the one-shot ``push`` publish op: ``merge-task``
        runs after the task's PR already exists (its branch is on the remote) and has advanced by
        the merge commit, so a plain ``git push`` fast-forwards the remote — and a re-run after the
        commit is already pushed is a git no-op. Without this the completed original ``push`` op
        would skip publishing the merge commit and ``gh pr merge`` would merge the pre-merge branch.
        """
        self._git_checked("push", "origin", branch)

    def commit_on_branch(self, sha: str, branch: str) -> bool:
        """True iff ``sha`` is an ancestor of ``branch`` (recovery subtask verification)."""
        if not sha:
            return False
        return self._git("merge-base", "--is-ancestor", sha, branch).ok

    # --- footprint ------------------------------------------------------------------------

    def ensure_runtime_excludes(self) -> None:
        """Ensure this clone ignores ``.worc/`` + ``.worc-io/`` via its LOCAL ``.git/info/exclude``.

        ``install`` writes the *tracked* ``.gitignore`` (operator-facing, committed as part of
        setup). This per-run fallback uses the clone-local, untracked exclude instead — so the
        runtime-home ignore is guaranteed however the clone was scaffolded, yet never rides into a
        task's code commit or PR diff (the tracked ``.gitignore`` is left untouched). Keeps
        ``.worc/`` (state.db, logs/, workspace/, checks/, config.yaml, …) out of the operator's
        ``git status``; ``tasks/`` stays trackable — it carries the audit trail. Idempotent.
        Resolved via ``rev-parse --git-path`` so it is correct for clones and linked worktrees.

        Adds only the runtime roots not already covered per root (see
        :func:`_missing_runtime_ignore_lines`), so an operator's own ``.worc/*`` + ``!.worc/flows/``
        scheme that deliberately un-ignores ``.worc/flows/`` to track flows in git (see
        ``docs/how-to.md``) is never stomped — the blanket ``.worc/`` line is not re-appended —
        while the newer ``.worc-io/`` exchange line is still added when missing (WRI-001).
        """
        lines = _missing_runtime_ignore_lines(
            lambda probe: self._git("check-ignore", "-q", probe).ok
        )
        if not lines:
            return
        rel = self._git("rev-parse", "--git-path", "info/exclude").stdout.strip()
        if not rel:
            return
        exclude_path = Path(rel) if Path(rel).is_absolute() else Path(self._clone) / rel
        _append_missing_lines(exclude_path, lines)

    # --- SnapshotHook --------------------------------------------------------------

    def capture(self) -> WorkingTreeSnapshot:
        commit_sha = self._git("rev-parse", "HEAD").stdout.strip()
        porcelain = self._git("status", "--porcelain").stdout
        diff = self._git("diff", "HEAD").stdout
        checksum = hashlib.sha256((porcelain + "\n" + diff).encode("utf-8")).hexdigest()
        return WorkingTreeSnapshot(
            commit_sha=commit_sha, porcelain_status=porcelain, diff_checksum=checksum, artifacts=()
        )

    def partial_change_since(self, before: WorkingTreeSnapshot) -> PartialChange | None:
        after = self.capture()
        if after.diff_checksum == before.diff_checksum:
            return None
        diff_text = self._git("diff", "HEAD").stdout
        diff_path = self._write_partial_diff(diff_text)
        return PartialChange(
            before=before,
            after=after,
            diff_path=diff_path,
            note="partial attempt from a prior provider; build on it rather than restart",
        )

    def _write_partial_diff(self, diff_text: str) -> str:
        if self._active is not None:
            self._active.partial_counter += 1
            base = task_artifact_dir(self._artifacts_root, self._active.task_id) / "partial"
            name = f"{self._active.partial_counter:03d}.diff"
        else:
            base = Path(self._artifacts_root) / "logs" / "_partial"
            name = "partial.diff"
        base.mkdir(parents=True, exist_ok=True)
        path = base / name
        path.write_text(diff_text, encoding="utf-8")
        return str(path)

    # --- Git control-state fingerprint (WRI-009) -----------------------------------

    def capture_git_control_state(self) -> GitControlState:
        """Fingerprint the Git control state a provider attempt must not mutate.

        Captured immediately before a workspace-write attempt and compared afterwards via
        :meth:`compare_git_control_state`. Read-only: none of these probes runs a content filter, so
        the capture itself cannot execute an agent-selected driver.
        """
        symref = self._git("symbolic-ref", "-q", "HEAD")
        head_symref = (symref.stdout.strip() or None) if symref.ok else None
        head_commit = self._git("rev-parse", "HEAD").stdout.strip()
        branch = self._active.branch if self._active is not None else self.current_branch()
        task_ref = f"refs/heads/{branch}" if branch and branch != "HEAD" else None
        task_ref_commit: str | None = None
        if task_ref is not None:
            ref = self._git("rev-parse", "--verify", "-q", task_ref)
            task_ref_commit = (ref.stdout.strip() or None) if ref.ok else None
        index = _parse_ls_files_stage_z(self._git("ls-files", "--stage", "-z").stdout)
        return GitControlState(
            head_symref=head_symref,
            head_commit=head_commit,
            task_ref=task_ref,
            task_ref_commit=task_ref_commit,
            index=index,
            config=self._capture_local_config(),
            hooks_path=self._repo_local_config_value("core.hooksPath"),
            hooks=self._capture_hooks(),
            markers=frozenset(m for m in _CONTROL_MARKERS if self._marker_present(m)),
        )

    def compare_git_control_state(self, before: GitControlState) -> GitControlDrift | None:
        """Recapture, diff against ``before``, and return the redacted drift (``None`` if none)."""
        after = self.capture_git_control_state()
        items: list[GitControlDriftItem] = []
        if before.head_symref != after.head_symref:
            items.append(GitControlDriftItem("head", "HEAD symbolic identity changed"))
        if before.head_commit != after.head_commit:
            items.append(GitControlDriftItem("head", "HEAD commit moved"))
        if (before.task_ref, before.task_ref_commit) != (after.task_ref, after.task_ref_commit):
            items.append(GitControlDriftItem("task_ref", "task branch ref moved"))
        items.extend(self._diff_index(before.index, after.index))
        items.extend(self._diff_config(before.config, after.config))
        if before.hooks_path != after.hooks_path:
            items.append(GitControlDriftItem("hooks", "core.hooksPath changed"))
        items.extend(self._diff_hooks(before.hooks, after.hooks))
        if before.markers != after.markers:
            changed = ", ".join(sorted(before.markers ^ after.markers))
            items.append(GitControlDriftItem("markers", f"operation markers changed: {changed}"))
        return GitControlDrift(tuple(items)) if items else None

    def _capture_local_config(self) -> dict[str, tuple[str, ...]]:
        """Repo-local (+worktree) config as ``{key: (value-sha256, ...)}`` — hashes, never values.

        Scoped to ``--local``/``--worktree`` deliberately: it is exactly the agent-writable config
        surface, and it excludes the command-line ``-c`` hardening prefix (never written to a config
        file), so neutralization is never seen as drift.
        """
        config: dict[str, list[str]] = {}
        for scope in ("--local", "--worktree"):
            res = self._git("config", scope, "--list", "-z")
            if not res.ok:  # --worktree errors unless extensions.worktreeConfig is set
                continue
            for record in res.stdout.split("\0"):
                if not record:
                    continue
                key, _, value = record.partition("\n")
                config.setdefault(key, []).append(hashlib.sha256(value.encode("utf-8")).hexdigest())
        return {key: tuple(values) for key, values in config.items()}

    def _repo_local_config_value(self, key: str) -> str | None:
        """A single repo-local (or worktree) config value, ignoring the command-line ``-c`` prefix.

        ``git config --local --get`` reads the config *file*, so the orchestrator's own
        ``-c core.hooksPath`` (and peers) — applied to every git call — do not mask the
        agent-controllable value the control-state fingerprint must watch.
        """
        for scope in ("--local", "--worktree"):
            res = self._git("config", scope, "--get", key)
            if res.ok and res.stdout.strip():
                return res.stdout.strip()
        return None

    def _hooks_dir(self) -> Path:
        """The hooks dir the *repository* would run — its ``core.hooksPath`` or ``<common>/hooks``.

        Resolved from ``--local``/``--worktree`` config and ``--git-common-dir`` (never
        ``--git-path hooks``/``config --get``, which honor the orchestrator's own ``-c
        core.hooksPath`` hardening) so the agent-controllable hooks the fingerprint must watch are
        not masked. ``--git-common-dir`` gives the shared hooks dir for a linked worktree too.
        """
        configured = self._repo_local_config_value("core.hooksPath")
        if configured:
            path = Path(configured)
            return path if path.is_absolute() else Path(self._clone) / path
        common = self._git("rev-parse", "--git-common-dir").stdout.strip() or ".git"
        base = Path(common)
        if not base.is_absolute():
            base = Path(self._clone) / base
        return base / "hooks"

    def resolve_control_paths(
        self, exchange_root: str | Path | None = None
    ) -> ProviderWriteGuardPolicy:
        """Absolute Git-control + lifecycle roots a workspace-write attempt must Write/Edit-deny.

        Provider-neutral (WRI-002/003): the node runner resolves this fresh for each workspace-write
        attempt — the gitdir/common-dir are only final after branch prep and differ for a linked
        worktree — and threads it onto ``AgentRunRequest.write_guard``; each adapter renders it into
        its own tool-deny / OS-sandbox ``denyWrite`` syntax (the Core never learns that syntax).
        Read-only git plumbing, reusing the same resolution as the WRI-009 control-state fingerprint
        (``rev-parse`` + :meth:`_hooks_dir`). ``git_dir`` and ``git_common_dir`` are returned
        separately because a linked worktree's per-worktree gitdir differs from the shared common
        dir and both must be denied. Fails closed (:class:`GitCommandError`) on a git-resolution
        failure so an unguarded run is impossible.
        """
        git_dir = Path(self._git_checked("rev-parse", "--absolute-git-dir"))
        git_common_dir = Path(self._git_checked("rev-parse", "--git-common-dir"))
        if not git_common_dir.is_absolute():
            git_common_dir = Path(self._clone) / git_common_dir
        return ProviderWriteGuardPolicy(
            exchange_root=Path(exchange_root) if exchange_root else None,
            git_dir=git_dir,
            git_common_dir=git_common_dir,
            hooks_dir=self._hooks_dir(),
            tasks_dir=Path(self._clone) / self._tasks_dir,
        )

    def _capture_hooks(self) -> dict[str, HookFacts]:
        """Identity facts for each entry in the effective hooks dir (empty when absent)."""
        facts: dict[str, HookFacts] = {}
        hooks_dir = self._hooks_dir()
        try:
            entries = sorted(hooks_dir.iterdir())
        except (
            OSError
        ):  # no hooks dir (our neutralized empty dir, or none) — nothing to fingerprint
            return facts
        for entry in entries:
            try:
                st = entry.lstat()
            except OSError:
                continue
            if stat.S_ISLNK(st.st_mode):
                try:
                    target: str | None = entry.readlink().as_posix()
                except OSError:
                    target = None
                facts[entry.name] = HookFacts("symlink", target, None, False)
            elif stat.S_ISREG(st.st_mode):
                try:
                    content_sha: str | None = hashlib.sha256(entry.read_bytes()).hexdigest()
                except OSError:
                    content_sha = None
                facts[entry.name] = HookFacts("file", None, content_sha, bool(st.st_mode & 0o111))
            elif stat.S_ISDIR(st.st_mode):
                facts[entry.name] = HookFacts("dir", None, None, False)
            else:
                facts[entry.name] = HookFacts("other", None, None, False)
        return facts

    def _marker_present(self, name: str) -> bool:
        rel = self._git("rev-parse", "--git-path", name)
        if not rel.ok:
            return False
        path = Path(rel.stdout.strip())
        if not path.is_absolute():
            path = Path(self._clone) / path
        return path.exists()

    @staticmethod
    def _diff_index(
        before: dict[str, tuple[str, str, str]], after: dict[str, tuple[str, str, str]]
    ) -> list[GitControlDriftItem]:
        return [
            GitControlDriftItem("index", f"staged entry changed: {redact_text(path)}")
            for path in sorted(set(before) | set(after))
            if before.get(path) != after.get(path)
        ]

    @staticmethod
    def _diff_config(
        before: dict[str, tuple[str, ...]], after: dict[str, tuple[str, ...]]
    ) -> list[GitControlDriftItem]:
        # key names only — never the (hashed) values
        return [
            GitControlDriftItem("config", f"repo config key changed: {key}")
            for key in sorted(set(before) | set(after))
            if before.get(key) != after.get(key)
        ]

    @staticmethod
    def _diff_hooks(
        before: dict[str, HookFacts], after: dict[str, HookFacts]
    ) -> list[GitControlDriftItem]:
        items: list[GitControlDriftItem] = []
        for name in sorted(set(before) | set(after)):
            b, a = before.get(name), after.get(name)
            if b == a:
                continue
            if b is None:
                reason = "added"
            elif a is None:
                reason = "removed"
            elif b.target != a.target:
                reason = "symlink retargeted"
            elif b.content_sha != a.content_sha:
                reason = "content changed"
            else:
                reason = "changed"
            items.append(GitControlDriftItem("hooks", f"hook {name!r} {reason}"))
        return items

    def _untrusted_config_programs(self) -> list[str]:
        """Agent-writable config keys whose value is a program git could execute (WRI-009).

        A filter/diff clean/smudge/process/command/textconv driver, or repo-local
        ``core.sshCommand``/``credential.helper``. Operator global/system config is trusted and not
        read. Keys are compared lowercased (``git config --list`` lowercases section/name).
        """
        keys: list[str] = []
        for scope in ("--local", "--worktree"):
            res = self._git("config", scope, "--list", "-z")
            if not res.ok:  # --worktree errors unless extensions.worktreeConfig is set
                continue
            for record in res.stdout.split("\0"):
                if not record:
                    continue
                key = record.partition("\n")[0]
                if _FILTER_DRIVER_KEY_RE.match(key) or key in _PROGRAM_CONFIG_KEYS:
                    keys.append(key)
        return keys

    def _assert_no_untrusted_filters(self) -> None:
        """Refuse (manual action) before staging/checkout if the agent-writable config defines a
        program-launching driver.

        The operator-authorized-filter allowlist is a deferred WRI-009 follow-up; under the current
        contract any untrusted repo-local driver stops the run in manual action rather than letting
        an orchestrator git command execute agent-selected code. An agent ``.gitattributes`` edit
        cannot authorize a new process because a *repo-local* driver program is refused outright
        (binding a trusted operator-global driver is not a new process and is unaffected).
        """
        programs = self._untrusted_config_programs()
        if programs:
            names = ", ".join(sorted(set(programs)))
            raise ManualActionRequired(
                "target-repo local git config defines an untrusted program-launching driver "
                f"({names}); refusing to run git that could execute it. Move it to operator global "
                "config or remove it from the repository."
            )

    def files_in_commit(self, sha: str) -> list[str]:
        """Repo-relative paths changed by commit ``sha`` (git-posix separators; empty on any error).

        Used by the subtask handoff to name a predecessor subtask's changed files (deterministic
        ground-truth floor). Routes through the same safe argv runner as every git call — no shell,
        mandatory timeout — and is best-effort: a bad/missing sha yields ``[]`` rather than raising,
        so the handoff degrades to the rest of the floor.
        """
        result = self._git("diff-tree", "--no-commit-id", "--name-only", "-r", "-z", sha)
        if not result.ok:
            return []
        return _parse_name_only_z(result.stdout)

    # --- staging + commit ---------------------------------------------------------

    def changed_code_paths(self) -> list[str]:
        """The changed paths that are *not* orchestration artifacts (the code staging set)."""
        porcelain = self._git("status", "--porcelain", "-z").stdout
        paths: list[str] = []
        for _code, path in _parse_porcelain_status_z(porcelain):
            if self._is_artifact_path(path):
                continue
            paths.append(path)
        return paths

    def changed_code_entries(self) -> tuple[ChangedPath, ...]:
        """Return tracked and untracked code changes for deterministic output guardrails."""
        entries: list[ChangedPath] = []
        tracked = self._git("diff", "--name-status", "-z", "HEAD", "--").stdout
        for status, path, previous in _parse_name_status_z(tracked):
            if self._is_artifact_path(path):
                continue
            entries.append(ChangedPath(status=status, path=path, previous_path=previous))

        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
        entries.extend(
            ChangedPath(status="??", path=path)
            for path in _parse_name_only_z(untracked)
            if not self._is_artifact_path(path)
        )
        return tuple(entries)

    def changed_code_paths_since_base(self) -> list[str]:
        """Code paths changed vs ``base_branch`` (committed-since-base + uncommitted), deduped.

        The path-list analog of :meth:`diff_stat`: diffs ``base_branch`` against the working tree
        (``git diff --name-only <base>``, two-dot — committed *and* uncommitted), plus untracked
        files (``git ls-files --others``, which ``git diff`` never reports). Used **only** for
        check-set selection (the ``testing`` node), never for staging: :meth:`changed_code_paths`
        stays working-tree-only because the commit pathspec must add just the uncommitted change.

        Selection from ``git status --porcelain`` alone missed a decomposed subtask whose code was
        already committed on the task branch — the working tree was clean, so the checks node saw an
        empty diff and passed vacuously without running any command set. Diffing against the base
        catches that case. In a non-decomposed run nothing is committed until publish, so this
        equals the working-tree change (``base == HEAD``); in a decomposed run it also includes
        earlier subtasks' committed paths, so a later subtask re-runs their sets too — redundant but
        fail-safe, matching ``checks.selection``'s bias toward running more, never fewer.
        """
        return self._changed_code_paths_from(self._config.repo.base_branch)

    def changed_code_paths_since_task_base(self) -> list[str]:
        """Code paths changed vs the per-task chain base — this task's files only (F48).

        Same shape as :meth:`changed_code_paths_since_base`, but diffs against :meth:`_diff_base`
        (the branch tip at task start, F32) instead of the coarse ``base_branch``. On a shared/chain
        branch (``existing``/``current`` mode) that means it returns only THIS task's changed paths,
        not the whole chain's — so the memory packet's path-overlap ranking stays relevant to the
        current task instead of saturating on every prior task's files. Equals
        :meth:`changed_code_paths_since_base` in ``new`` mode (base == chain start).
        """
        return self._changed_code_paths_from(self._diff_base())

    def _changed_code_paths_from(self, base: str) -> list[str]:
        """Deduped, artifact-filtered code paths changed vs ``base`` (committed + uncommitted +
        untracked). Shared by the ``base_branch`` (check-set) and per-task (packet) variants."""
        paths: list[str] = []
        seen: set[str] = set()
        tracked = self._git("diff", "--name-only", "-z", base, "--").stdout
        for path in _parse_name_only_z(tracked):
            if path not in seen and not self._is_artifact_path(path):
                seen.add(path)
                paths.append(path)
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
        for path in _parse_name_only_z(untracked):
            if path not in seen and not self._is_artifact_path(path):
                seen.add(path)
                paths.append(path)
        return paths

    def _is_artifact_path(self, path: str) -> bool:
        normalized = path.replace("\\", "/")
        return any(normalized == d or normalized.startswith(f"{d}/") for d in self._excluded_dirs)

    def _fully_staged_deletions(self) -> set[str]:
        """Code paths whose deletion is already fully staged (porcelain ``D `` — staged in the
        index, absent from the working tree).

        ``git add -- <path>`` fails on these with ``fatal: pathspec '<path>' did not match any
        files`` (exit 128): the path exists neither in the working tree nor as an unstaged index
        difference, so there is nothing for ``git add`` to match. The deletion is already captured
        in the index, so the commit picks it up without re-adding — they are simply dropped from the
        ``git add`` pathspec. This happens when the agent itself stages a delete/move (``git rm`` /
        ``git mv`` that git did not record as a rename); the porcelain X column is ``D`` and the Y
        (working-tree) column is blank (F18).
        """
        porcelain = self._git("status", "--porcelain", "-z").stdout
        deletions: set[str] = set()
        for code, path in _parse_porcelain_status_z(porcelain):
            if code == "D " and path and not self._is_artifact_path(path):
                deletions.add(path)
        return deletions

    def staged_pathspec(self, paths: Sequence[str]) -> list[str]:
        """Build the scoped ``git add`` pathspec: the code paths, plus a ``:(exclude)`` guard for
        the task lifecycle dir **only when that dir is tracked**.

        ``.worc/`` is gitignored, so ``git add`` skips it without a guard. The task lifecycle dir
        (``paths.tasks_dir``) rides the separate audit commit; when it is *tracked* it is guarded
        with ``:(exclude)`` so it never slips into the *code* commit. When it is *gitignored* (what
        ``worc install`` seeds by default), that guard is redundant — git already skips it — **and**
        it breaks ``git add`` for repo-root paths: git aborts with "The following paths are ignored
        … use -f" (exit 1) whenever a positive pathspec sits beside the ignored dir at the root, so
        a root-level code change (``package.json``/``tsconfig.json``/…) could not be committed at
        all. So the guard is dropped when the dir is ignored (the explicit ``changed_code_paths``
        never include an ignored path anyway).

        Fully-staged deletions are dropped from the positive pathspec: ``git add`` cannot match a
        path that is absent from the working tree with no unstaged difference, and the deletion is
        already in the index (F18). The returned list may therefore hold only the ``:(exclude)``
        guard (or be empty); :meth:`_commit` skips ``git add`` entirely when no positive path
        remains and commits the pre-staged index.
        """
        staged_deletions = self._fully_staged_deletions()
        stageable = [p for p in paths if p not in staged_deletions]
        if self._tasks_dir_ignored():
            return stageable
        return [*stageable, f":(exclude){self._tasks_dir}/"]

    def _tasks_dir_ignored(self) -> bool:
        """Whether the task lifecycle dir is gitignored (cached ``git check-ignore``).

        Probes with a trailing slash (``tasks/``) so a directory-only ignore pattern matches whether
        or not the dir currently exists on disk (a bare ``tasks`` probe only matches an existing
        directory).
        """
        if self._tasks_dir_ignored_cache is None:
            probe = f"{self._tasks_dir}/"
            self._tasks_dir_ignored_cache = self._git("check-ignore", "-q", probe).ok
        return self._tasks_dir_ignored_cache

    # --- staged-set / index gates (WRI-009) ----------------------------------------

    def assert_index_clean_at_start(self) -> None:
        """Refuse to start (manual action) if a non-artifact entry is already staged (WRI-009).

        A bare ``git commit`` commits the whole index, so an operator/agent pre-staged baseline is
        swept into the orchestrator's scoped commit. Under the one documented contract WRI-009
        adopts we refuse with actionable guidance rather than reset (``existing``/``current`` never
        reset); an *unstaged* dirty working tree is preserved and never flagged.
        """
        staged = self._git("diff", "--cached", "--name-status", "-z").stdout
        offenders = [
            candidate
            for _status, path, previous in _parse_name_status_z(staged)
            for candidate in (path, previous)
            if candidate is not None and not self._is_artifact_path(candidate)
        ]
        if offenders:
            names = ", ".join(redact_text(p) for p in sorted(set(offenders))[:_DRIFT_EVIDENCE_CAP])
            raise ManualActionRequired(
                f"refusing to start: {len(set(offenders))} path(s) already staged in the index "
                f"({names}); unstage or commit them before running — the task's commit would "
                "otherwise sweep them in."
            )

    def assert_exchange_never_staged(self) -> None:
        """Fail closed (manual action) if this commit would touch a runtime-artifact path (WRI-009).

        An ignore rule is not a commit boundary — a provider can ``git add -f`` an ignored path — so
        this checks the index directly, in two ways: the transient exchange ``.worc-io`` must never
        be *tracked* at all (``git ls-files --cached``); and no *staged change* (add/modify/delete
        vs HEAD) under ``.worc``/``.worc-io`` may ride this commit (``git diff --cached``). The
        control home ``.worc`` may be legitimately tracked by an operator, so only a staged change
        under it is a violation — a historically-tracked, unchanged control file is not this
        commit's concern.
        """
        exchange_tracked = _parse_name_only_z(
            self._git("ls-files", "--cached", "-z", "--", EXCHANGE_HOME_DIRNAME).stdout
        )
        staged_changes = _parse_name_only_z(
            self._git("diff", "--cached", "--name-only", "-z", "--", *RUNTIME_EXCLUDED_DIRS).stdout
        )
        offenders = sorted(set(exchange_tracked) | set(staged_changes))
        if offenders:
            names = ", ".join(redact_text(p) for p in offenders[:_DRIFT_EVIDENCE_CAP])
            raise ManualActionRequired(
                f"refusing to commit: runtime artifact path(s) would be committed ({names}); the "
                "exchange/private home must never enter a commit."
            )

    @staticmethod
    def _within_allowlist(candidate: str, allowed: Collection[str]) -> bool:
        """Whether ``candidate`` is in ``allowed`` or under an allowed directory prefix.

        ``git status --porcelain`` reports a wholly-untracked directory as one ``dir/`` entry while
        ``git diff --cached`` reports the individual files under it, so an allowlist built from the
        porcelain paths must match a staged file against its directory entry.
        """
        return candidate in allowed or any(
            candidate.startswith(d) for d in allowed if d.endswith("/")
        )

    def assert_staged_allowed(self, allowed: Collection[str] | None) -> None:
        """Prove the whole staged set is within this operation's allowlist before commit (WRI-009).

        A bare ``git commit`` commits the entire index, not only what the scoped ``git add`` staged,
        so every commit path proves the staged set first. ``allowed`` as a collection is a positive
        allowlist: any staged added/surviving path not in it — a force-added artifact, a foreign
        ``tasks/`` file, an unrelated code path — is a violation even when its own ``git add``
        succeeded. A rename *source* is exempt from the allowlist (it is being moved out, and its
        destination is validated) unless it is a runtime-artifact path outside the allowlist — a
        rename FROM ``.worc-io``/``.worc``/``tasks`` would exfiltrate it. ``allowed=None`` is the
        merge exclude-mode: a base merge stages arbitrary base code, so only staged *artifact* paths
        (either endpoint of a rename) are rejected. An ignore rule never makes a staged entry safe.
        """
        self.assert_exchange_never_staged()
        offenders: list[str] = []
        staged = self._git("diff", "--cached", "--name-status", "-z").stdout
        for _status, path, previous in _parse_name_status_z(staged):
            if allowed is None:  # merge exclude-mode: reject only a staged artifact endpoint
                offenders += [
                    p for p in (path, previous) if p is not None and self._is_artifact_path(p)
                ]
                continue
            if not self._within_allowlist(path, allowed):  # added/surviving path must be allowed
                offenders.append(path)
            # a rename source is exempt unless it is a forbidden artifact not itself allowlisted
            if (
                previous is not None
                and self._is_artifact_path(previous)
                and not self._within_allowlist(previous, allowed)
            ):
                offenders.append(previous)
        if offenders:
            names = ", ".join(redact_text(p) for p in sorted(set(offenders))[:_DRIFT_EVIDENCE_CAP])
            raise ManualActionRequired(
                f"refusing to commit: {len(set(offenders))} staged path(s) outside this op's "
                f"allowlist ({names}); the provider may have staged files it must not."
            )

    def commit_code(self, task_id: str, message: str) -> str | None:
        """Stage the agent's code paths and make one commit. Idempotent. Returns the commit SHA.

        Returns the current HEAD when there is nothing to commit (e.g. a decomposed task whose code
        was already committed per subtask).
        """
        paths = self.changed_code_paths()
        if not paths:
            return self._git("rev-parse", "HEAD").stdout.strip() or None
        return self._commit(task_id, KIND_CODE_COMMIT, None, message, paths)

    def commit_subtask(self, task_id: str, order: int, slug: str, message: str) -> str:
        """Make the single local commit for a completed subtask on the task branch."""
        paths = self.changed_code_paths()
        sha = self._commit(task_id, KIND_SUBTASK_COMMIT, order, message, paths)
        if sha is None:  # nothing changed — fall back to HEAD so the marker is always set
            sha = self._git("rev-parse", "HEAD").stdout.strip()
        return sha

    def _commit(
        self,
        task_id: str,
        kind: str,
        subtask: int | None,
        message: str,
        paths: Sequence[str],
    ) -> str | None:
        existing = self._store.get_publish_op(task_id, kind, subtask)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref  # already committed (restart) — never double-commit

        head_before = self._git("rev-parse", "HEAD").stdout.strip()
        fingerprint = self._fingerprint(task_id, kind, subtask, head_before, paths)
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=kind,
                subtask_order=subtask,
                fingerprint=fingerprint,
                status=_STATUS_STARTED,
            )
        )
        if not paths:
            return None
        self._assert_no_untrusted_filters()  # `git add` runs clean filters
        pathspec = self.staged_pathspec(paths)
        positive = [p for p in pathspec if not p.startswith(":(exclude)")]
        if positive:  # skip when only fully-staged deletions remain — the index already has them
            self._git_checked("add", "--", *pathspec)
        # WRI-009: the bare `git commit` commits the whole index — prove it holds only `paths`.
        self.assert_staged_allowed(set(paths))
        self._git_checked("commit", "-m", message)
        sha = self._git_checked("rev-parse", "HEAD")
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=kind,
                subtask_order=subtask,
                fingerprint=fingerprint,
                status=_STATUS_COMPLETED,
                result_ref=sha,
            )
        )
        return sha

    def commit_merge_resolution(self, task_id: str, message: str) -> str | None:
        """Finalize the in-progress base-merge as one commit (after its conflicts are resolved).

        Distinct from :meth:`commit_code`: a merge also brings in base's incoming changes (not just
        the agent's edits), so it stages the whole tree (``git add -A``; ``.worc/`` stays ignored)
        and commits with ``MERGE_HEAD`` as the second parent. Idempotent via the ``merge_commit``
        publish op; returns the merge commit SHA, or current HEAD when there is nothing to finalize
        (no merge in flight — e.g. an already-current branch).
        """
        existing = self._store.get_publish_op(task_id, KIND_MERGE_COMMIT, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref  # already finalized (restart) — never double-commit
        if not self.merge_in_progress():
            return self._git("rev-parse", "HEAD").stdout.strip() or None
        head_before = self._git("rev-parse", "HEAD").stdout.strip()
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=KIND_MERGE_COMMIT,
                fingerprint=self._fingerprint(task_id, KIND_MERGE_COMMIT, None, head_before, ()),
                status=_STATUS_STARTED,
            )
        )
        self._assert_no_untrusted_filters()  # `git add -A` runs clean filters
        self._git_checked("add", "-A")
        # Belt-and-braces: never commit a half-resolved merge. ``git diff --cached --check`` reports
        # "leftover conflict marker" lines; refuse if any remain (catches the case where no checks
        # are configured, so the flow's testing node could not catch the markers itself).
        if "leftover conflict marker" in self._git("diff", "--cached", "--check").stdout.lower():
            raise GitCommandError("merge resolution left conflict markers; refusing to commit")
        # WRI-009: a base merge stages arbitrary base code (add -A) — exclude-mode rejects only a
        # staged runtime-artifact path (e.g. a pre-merge force-added `.worc-io/*`).
        self.assert_staged_allowed(None)
        self._git_checked("commit", "-m", message)
        sha = self._git_checked("rev-parse", "HEAD")
        self._record_completed(task_id, KIND_MERGE_COMMIT, head_before, sha)
        return sha

    def _assert_lifecycle_matches_packet(
        self, task_id: str, stageable: Sequence[str], task_packet_digest: str
    ) -> None:
        """Verify the lifecycle ``<id>.md`` is byte-identical to the frozen task packet (WRI-009).

        The audit commit publishes ``tasks/{done,failed}/<id>.md``; its content must still match the
        task the run was authorized from (the WRI-011 frozen packet digest). A mismatch means the
        task file was rewritten under the running task — a security violation, never a commit input.
        Only the task packet is checked; ``<id>.summary.md`` is orchestrator-authored.
        """
        for state in ("done", "failed"):
            rel = f"{self._tasks_dir}/{state}/{task_id}.md"
            path = Path(self._clone) / rel
            if rel not in stageable or not path.exists():
                continue
            if sha256_file(path) != task_packet_digest:
                raise ManualActionRequired(
                    f"refusing to commit: task lifecycle file {rel!r} does not match the frozen "
                    "task packet (it was rewritten under the running task); a security violation, "
                    "not a commit input."
                )

    def commit_audit(self, task_id: str, *, task_packet_digest: str | None = None) -> str | None:
        """Make the orchestrator-only commit of the task lifecycle.

        Stages **only this task's** moved task file plus its `<id>.summary.md` (in ``tasks/done`` or
        ``tasks/failed``) — never the whole ``tasks/`` tree, so a concurrently-pending task is never
        swept into this commit. Working artifacts (plan, review, stage logs, diffs, summary.json)
        live under the gitignored ``.worc/`` home and are never committed. The code change rides in
        the separate scoped code commit, so this never touches code paths.

        WRI-009: the lifecycle ``<id>.md`` is verified byte-identical to the WRI-011 frozen task
        packet (``task_packet_digest``) before staging — a rewritten task file is a security
        violation, not a commit input. ``None`` skips it (a merge/synthetic run has no packet).
        """
        footprint = self._config.git.footprint
        existing = self._store.get_publish_op(task_id, KIND_AUDIT_COMMIT, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref
        self._assert_no_untrusted_filters()  # `git add -A` runs clean filters

        code_branch = self._git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        audit_branch = code_branch
        if footprint.audit_on_branch is AuditBranch.SIBLING:
            audit_branch = f"{code_branch}-audit"
            if not self._branch_exists(audit_branch):
                self._git_checked("checkout", "-b", audit_branch)
            else:
                self._git_checked("checkout", audit_branch)

        message = footprint.audit_commit_message.format(task_id=task_id)
        audit_files = [
            f"{self._tasks_dir}/{state}/{task_id}{suffix}"
            # Destination states (``done``/``failed``) stage the file's *appearance*; the source
            # state (``pending``) stages its *removal* on a lifecycle move — without the source
            # path a ``pending→failed`` / ``pending→done`` move of a base-tracked task file leaves
            # a dangling ``D`` on the base branch after terminal cleanup.
            for state in ("done", "failed", "pending")
            for suffix in (".md", ".summary.md")
        ]
        # Stage the task file's *appearance* in its new lifecycle folder AND its *removal* from the
        # old one. A lifecycle move (failed -> done) deletes the source path; a plain `git add` of
        # only the files that still exist would miss that deletion when the source is tracked in the
        # branch's base (e.g. committed to base by hand) — leaving the base working tree dirty (a
        # dangling `D`) after terminal cleanup. `git add -A` over the candidate pathspecs stages
        # both adds and deletes. Pass only paths that exist on disk or are tracked, so an unmatched
        # pathspec can never abort the whole add.
        tracked = set(self._git("ls-files", "--", *audit_files).stdout.splitlines())
        stageable = [
            rel for rel in audit_files if (Path(self._clone) / rel).exists() or rel in tracked
        ]
        sha: str | None = None
        if task_packet_digest is not None:
            self._assert_lifecycle_matches_packet(task_id, stageable, task_packet_digest)
        if stageable and self._git("add", "-A", "--", *stageable).ok:
            # WRI-009: only this task's lifecycle files may be in the index at the audit commit.
            self.assert_staged_allowed(set(stageable))
            commit = self._git("commit", "-m", message)
            if commit.ok:
                sha = self._git_checked("rev-parse", "HEAD")

        if footprint.audit_on_branch is AuditBranch.SIBLING:
            self._git_checked("checkout", code_branch)

        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=KIND_AUDIT_COMMIT,
                fingerprint=sha or "noop",
                status=_STATUS_COMPLETED,
                result_ref=sha,
            )
        )
        return sha

    # --- publish (idempotent) --------------------------------------------------------

    def push(self, task_id: str, branch: str, *, mode: BranchMode = BranchMode.NEW) -> bool:
        """Push the task branch to ``origin``. Idempotent via the publish op + remote check.

        In ``new`` mode the task branch must never be ``base_branch`` — a push targeting the base
        signals a corrupted branch state (publishing is PR-only), so it is refused. In
        ``existing``/``current`` mode the operator chose the branch, so pushing to it — even one
        that happens to be the base — is legitimate (the head==base publish path, where
        ``create_pr`` then skips the PR). Branch protection on the remote, if any, is the real gate.
        """
        base = self._config.repo.base_branch
        if branch == base and mode is BranchMode.NEW:
            raise GitCommandError(
                f"refusing to push directly to base branch {base!r}; publishing is PR-only"
            )
        existing = self._store.get_publish_op(task_id, KIND_PUSH, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return True
        # The remote-existence shortcut is a ``new``-mode restart optimisation: a fresh unique task
        # branch is on the remote only if a prior attempt pushed it. In ``existing``/``current`` the
        # branch pre-exists on the remote regardless of our commit, so its presence proves nothing —
        # push for real (``git push`` is itself a no-op when already up to date).
        if mode is BranchMode.NEW and self._remote_branch_exists(branch):
            self._record_completed(task_id, KIND_PUSH, branch, branch)
            return True
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id, kind=KIND_PUSH, fingerprint=branch, status=_STATUS_STARTED
            )
        )
        self._git_checked_retryable("push", "--set-upstream", "origin", branch)
        self._record_completed(task_id, KIND_PUSH, branch, branch)
        return True

    def _remote_branch_exists(self, branch: str) -> bool:
        result = self._git("ls-remote", "--heads", "origin", branch)
        return result.ok and bool(result.stdout.strip())

    def create_pr(self, task_id: str, branch: str, *, title: str, body_path: str) -> str | None:
        """Open a PR with ``summary.md`` as the body. Idempotent. None when PRs are disabled.

        Two branch-mode short-circuits (both return ``None`` — no PR, auto-merge then no-ops):
        head==base — when the working branch is the PR base (e.g. ``current`` on ``main``), a PR is
        impossible, so the commit+push already stand and the PR step is skipped; and PR reuse — a
        chain of tasks on one branch converges on one PR, so an already-open ``head→base`` PR is
        reused rather than re-created (``gh pr create`` would otherwise fail on the duplicate).
        """
        if not self._config.git.create_pull_request:
            return None
        pr_base = self._config.git.pr_base
        if branch == pr_base:
            self._log_pr(task_id).info(
                "PR skipped: head equals base (%r); the commit/push stands, no PR to open", branch
            )
            return None
        existing = self._store.get_publish_op(task_id, KIND_PR, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref
        reused = self._find_open_pr(task_id, branch, pr_base)
        if reused is not None:
            self._append_reused_pr_body(task_id, reused, title=title, body_path=body_path)
            self._record_completed(task_id, KIND_PR, branch, reused)
            return reused

        self._store.record_publish_op(
            PublishOpRow(task_id=task_id, kind=KIND_PR, fingerprint=branch, status=_STATUS_STARTED)
        )
        result = self._gh(
            [
                "pr",
                "create",
                "--base",
                pr_base,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                body_path,
            ]
        )
        if not result.ok:
            raise GitCommandError(f"gh pr create failed: {result.stderr.strip()}")
        pr_url = result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""
        self._record_completed(task_id, KIND_PR, branch, pr_url)
        return pr_url

    def _append_reused_pr_body(
        self, task_id: str, pr_url: str, *, title: str, body_path: str
    ) -> None:
        """Append this task's section to a reused chain PR's body, keyed by task id (F27).

        A chain of tasks on one branch converges on a single reused PR, which otherwise keeps the
        FIRST task's title/body — a reviewer reading a 7-task chain PR sees only task 1's scope.
        Append ``## <title>`` (with the task's summary) under the existing body, guarded by a
        ``<!-- worc-task:<id> -->`` marker so re-running the same task never duplicates its section.
        Best-effort: the reuse already succeeded, so any ``gh`` failure is logged and swallowed —
        never block publish for a cosmetic body update. Redaction rides ``body_path`` (already the
        redacted summary the create path uses)."""
        marker = f"<!-- worc-task:{task_id} -->"
        current = self._pr_body(pr_url)
        if current is None or marker in current:
            return  # unreadable body, or this task's section is already present (idempotent)
        try:
            summary = Path(body_path).read_text(encoding="utf-8").strip()
        except OSError:
            summary = ""
        section = f"{current.rstrip()}\n\n---\n\n{marker}\n\n## {title}\n\n{summary}\n"
        body_file = task_artifact_dir(self._artifacts_root, task_id) / "pr_body_appended.md"
        body_file.parent.mkdir(parents=True, exist_ok=True)
        body_file.write_text(section, encoding="utf-8")
        result = self._gh(["pr", "edit", pr_url, "--body-file", str(body_file)])
        if not result.ok:
            self._log_pr(task_id).warning(
                "could not append task section to reused PR body: %s", result.stderr.strip()
            )

    def _pr_body(self, pr_url: str) -> str | None:
        """The current PR body text, or ``None`` when ``gh`` cannot read it (best-effort)."""
        result = self._gh(["pr", "view", pr_url, "--json", "body", "-q", ".body"])
        if not result.ok:
            return None
        return result.stdout.rstrip("\n")

    def _find_open_pr(self, task_id: str, branch: str, pr_base: str) -> str | None:
        """The URL of an already-open ``branch→pr_base`` PR to reuse, or ``None`` (create a new PR).

        Only an **open** PR (a ``draft`` counts as open) is reusable; a ``closed``/``merged`` PR is
        not (``gh pr list --state open`` filters them out for us). Multiple open matches → reuse the
        most recent and warn (the operator likely opened an extra by hand). Best-effort: a ``gh``
        error / offline / unparseable response returns ``None`` so publishing falls through to
        ``gh pr create`` (which surfaces a real duplicate-PR error rather than masking it).
        """
        result = self._gh(
            [
                "pr",
                "list",
                "--head",
                branch,
                "--base",
                pr_base,
                "--state",
                "open",
                "--json",
                "url,updatedAt",
            ]
        )
        if not result.ok:
            return None
        try:
            rows = json.loads(result.stdout or "[]")
        except ValueError:
            return None
        if not isinstance(rows, list) or not rows:
            return None
        rows = sorted(rows, key=lambda r: str(r.get("updatedAt") or ""), reverse=True)
        if len(rows) > 1:
            self._log_pr(task_id).warning(
                "multiple open PRs for %s→%s; reusing the most recent", branch, pr_base
            )
        url = rows[0].get("url")
        return str(url) if url else None

    def _log_pr(self, task_id: str) -> logging.LoggerAdapter[logging.Logger]:
        return bind(_LOG, task_id=task_id, component="gh")

    def merge_pr(
        self, task_id: str, pr_url: str, *, strategy: MergeStrategy, wait_for_checks: bool
    ) -> str | None:
        """Merge an open PR via ``gh pr merge``. Idempotent via the publish op.

        Returns a merge-outcome marker: the merge commit SHA (immediate mode), ``"merged"`` when the
        SHA is unreadable, or ``"armed"`` when GitHub-native auto-merge was armed (``--auto``);
        ``None`` when there is no PR. Reached only when ``git.auto_merge`` resolves true.

        DANGER: this bypasses the human review gate. It never weakens safety — **no** ``--admin``
        (branch protection is respected), no force-push, exactly one attempt (no retry). A blocked
        merge raises :class:`GitCommandError`; the Core surfaces that as ``manual_action_required``
        and leaves the PR open for a human to merge.
        """
        if not pr_url:
            return None
        existing = self._store.get_publish_op(task_id, KIND_PR_MERGE, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return existing.result_ref
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id, kind=KIND_PR_MERGE, fingerprint=pr_url, status=_STATUS_STARTED
            )
        )
        # Fixed argv (no shell, no interpolation); strategy comes from the validated MergeStrategy
        # enum. ``--admin`` is never emitted, so a protected branch's checks remain the real gate.
        args = ["pr", "merge", pr_url, f"--{strategy.value}"]
        if wait_for_checks:
            args.append("--auto")
        result = self._gh(args)
        if not result.ok:
            haystack = f"{result.stderr}\n{result.stdout}".lower()
            if any(marker in haystack for marker in _ALREADY_MERGED_MARKERS):
                self._record_completed(task_id, KIND_PR_MERGE, pr_url, "merged")
                return "merged"
            # ``result.stderr`` is already redacted by ``_run`` — never surface raw process output.
            raise GitCommandError(f"gh pr merge failed: {result.stderr.strip()}")
        outcome = "armed" if wait_for_checks else (self._merge_commit_sha(pr_url) or "merged")
        self._record_completed(task_id, KIND_PR_MERGE, pr_url, outcome)
        return outcome

    def record_external_merge(self, task_id: str, pr_url: str) -> None:
        """Record a ``pr_merge`` publish op for a PR merged out of band (the ``prs --sync`` path).

        ``verify_pr_state`` has already confirmed the PR is MERGED on GitHub, so this writes the
        idempotency/audit op (which unblocks ``depends_on`` dependents) without any ``gh`` call — no
        network, no re-merge. Idempotent: a task that already has a completed ``pr_merge`` op is a
        no-op.
        """
        existing = self._store.get_publish_op(task_id, KIND_PR_MERGE, None)
        if existing is not None and existing.status == _STATUS_COMPLETED:
            return
        self._record_completed(task_id, KIND_PR_MERGE, pr_url, "merged")

    def _merge_commit_sha(self, pr_url: str) -> str | None:
        """Best-effort merge commit SHA after an immediate merge; ``None`` when unavailable."""
        result = self._gh(["pr", "view", pr_url, "--json", "mergeCommit", "-q", ".mergeCommit.oid"])
        if not result.ok:
            return None
        return result.stdout.strip() or None

    def _record_completed(self, task_id: str, kind: str, fingerprint: str, result_ref: str) -> None:
        self._store.record_publish_op(
            PublishOpRow(
                task_id=task_id,
                kind=kind,
                fingerprint=fingerprint,
                status=_STATUS_COMPLETED,
                result_ref=result_ref,
            )
        )

    # --- diffs ----------------------------------------------------------------------------

    def write_current_diff(self, task_id: str) -> str:
        """Write ``logs/<task-id>/current.diff`` (this task's change vs its base) and return it.

        Diffs :meth:`_diff_base` against the **working tree** (``git diff <base>``, not ``git diff
        HEAD``), so it captures the task's net change whether or not it is committed yet — the same
        base-vs-worktree coverage as :meth:`diff_stat`. ``git diff HEAD`` only showed uncommitted
        working-tree edits, so in a decomposed run (where each subtask is committed) it collapsed to
        just the trailing uncommitted hunk and badly understated the change in ``current.diff`` /
        ``{diff_path}`` / the PR body / the failure report. For ``new`` mode the base is
        ``base_branch`` (the branch is cut from it and it does not advance), so a non-decomposed run
        equals ``git diff HEAD``; for a ``existing``/``current`` chain branch the base is the branch
        tip at task start (F32), so review/docs see only this task's change, not the whole unmerged
        chain (which previously showed every prior task — e.g. 35 files for ~5 changed). The
        dangerous-diff guard classifies from :meth:`changed_code_entries` (HEAD-relative), not this
        artifact, so the base here does not change what the guard gates.

        Two completeness fixes (F20): plain ``git diff`` never reports untracked files, so a brand
        new file was silently missing from the artifact — bracket the diff with a transient
        intent-to-add (staged, then immediately reset back to untracked; no persistent index
        change, so :meth:`changed_code_entries`/:meth:`changed_code_paths` still see them as
        ``??``) so their full content is included. ``--text`` forces a textual diff even for a file
        Git's heuristic misclassifies as binary (e.g. a NUL-delimited fixture) — without it such a
        file rendered as an opaque "Binary files differ", hiding the actual change.

        The diff is redacted before writing: the failure report reads it back, so this is
        the single place that keeps a leaked secret out of both ``current.diff`` and the report.
        """
        untracked = self._git("ls-files", "--others", "--exclude-standard", "-z").stdout
        untracked_paths = [p for p in untracked.split("\0") if p]
        if untracked_paths:
            self._git("add", "--intent-to-add", "--", *untracked_paths)
        try:
            diff = self._git("diff", "--text", self._diff_base()).stdout
        finally:
            if untracked_paths:
                self._git("reset", "--", *untracked_paths)
        task_dir = task_artifact_dir(self._artifacts_root, task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        path = task_dir / "current.diff"
        path.write_text(redact_text(diff, extra_secrets=self._diff_secrets()), encoding="utf-8")
        offenders = self.control_byte_paths()
        if offenders:
            bind(_LOG, task_id=task_id, component="diff").warning(
                "committed control bytes (NUL) in %d file(s) — invisible to git diff/review even "
                "with --text (F35): %s",
                len(offenders),
                ", ".join(offenders),
            )
        return str(path)

    def control_byte_paths(self) -> list[str]:
        """Repo-relative POSIX paths of this task's changed files that contain a NUL byte (F35).

        A committed NUL delimiter makes a file git-**binary** — invisible in ``git diff``/GitHub
        review even with ``--text`` (F20) — so a NUL that slips into source escapes human review.
        Best-effort scan of the task's changed files (committed-since-base + uncommitted); a file
        that cannot be read (e.g. deleted) is skipped. Returns ``[]`` when clean. Orchestrator-side
        and repo-agnostic, so the F23→F35 recurrence surfaces in the run logs instead of only via a
        manual ``git show``."""
        offenders: list[str] = []
        for rel in self.changed_code_paths_since_base():
            try:
                if b"\x00" in (Path(self._clone) / rel).read_bytes():
                    offenders.append(rel)
            except OSError:
                continue
        return sorted(offenders)

    def _diff_secrets(self) -> tuple[str, ...]:
        """Denied-file secret values present in the clone, to redact from written diffs."""
        return read_denied_secrets(self._clone, self._config.security.denied_read_paths)

    def diff_stat(self) -> str:
        """``git diff --stat`` of the task change vs ``base_branch`` — files + line counts only.

        Diffs ``base_branch`` against the **working tree** (not ``base...HEAD``), so it reflects the
        pending change whether or not it is committed yet. The deterministic minimal summary is
        built during ``finalize`` — *before* the publish node's ``commit_code`` — so a
        committed-only diff (``base...HEAD``) is still empty there and renders a misleading "(no
        changes)". The task branch is cut from ``base_branch`` and ``base_branch`` does not advance
        during a task, so this equals the task's net change (tracked files; same coverage as the
        redacted ``current.diff``). ``--stat`` carries only file paths and counts (never patch
        content), so there is nothing secret to redact.
        """
        return self._git("diff", "--stat", self._diff_base()).stdout

    # --- terminal cleanup ----------------------------------------------------------

    def returns_to_base(self, mode: BranchMode) -> bool:
        """Whether terminal cleanup should check out ``base_branch`` for this branch mode.

        Resolves ``repo.checkout_base_on_cleanup`` (branch-mode ADR): ``current`` never returns
        (the operator owns its tree); otherwise an explicit flag wins, and when unset the default
        is per-mode — ``new`` returns to base, ``existing`` stays on the branch.
        """
        if mode is BranchMode.CURRENT:
            return False
        flag = self._config.repo.checkout_base_on_cleanup
        return mode is BranchMode.NEW if flag is None else flag

    def terminal_cleanup(
        self, task_id: str, *, mode: BranchMode = BranchMode.NEW
    ) -> CleanupOutcome:
        """Free the single processing slot after a terminal outcome, or report why it is unsafe.

        When :meth:`returns_to_base` is true (``new`` by default, or as forced by
        ``repo.checkout_base_on_cleanup``): check out ``base_branch`` once the tree is clean, else
        **fail closed**. Otherwise (``current`` and ``existing`` by default, or the flag disabled):
        leave HEAD on the working branch as-is and report safe — the operator owns that branch and
        its (possibly dirty) tree, so this must not force-checkout away. A subsequent ``new``-mode
        prep still checks out base, so the slot is freed either way without losing operator state.
        """
        if not self.returns_to_base(mode):
            branch = self.current_branch() or self._config.repo.base_branch
            outcome = CleanupOutcome(safe=True, target_branch=branch)
            self._write_cleanup_artifact(task_id, outcome, completed=True)
            self._active = None
            return outcome
        base = self._config.repo.base_branch
        dirty = self._unaccounted_dirty_paths()
        if dirty:
            outcome = CleanupOutcome(
                safe=False,
                target_branch=base,
                error=f"working tree has unaccounted changes: {', '.join(sorted(dirty))}",
            )
            self._write_cleanup_artifact(task_id, outcome, completed=False)
            return outcome

        checkout = self._git("checkout", base)
        if not checkout.ok:
            outcome = CleanupOutcome(
                safe=False, target_branch=base, error=f"checkout failed: {checkout.stderr.strip()}"
            )
            self._write_cleanup_artifact(task_id, outcome, completed=False)
            return outcome

        outcome = CleanupOutcome(safe=True, target_branch=base)
        self._write_cleanup_artifact(task_id, outcome, completed=True)
        self._active = None
        return outcome

    def _unaccounted_dirty_paths(self) -> set[str]:
        """Tracked, uncommitted changes (artifact dirs are expected and so are ignored)."""
        porcelain = self._git("status", "--porcelain", "-z").stdout
        dirty: set[str] = set()
        for code, path in _parse_porcelain_status_z(porcelain):
            if self._is_artifact_path(path):
                continue
            # An untracked non-artifact file (``??``) is unexpected; any tracked change is dirty.
            if code == "??" or code.strip():
                dirty.add(path)
        return dirty

    def _write_cleanup_artifact(
        self, task_id: str, outcome: CleanupOutcome, *, completed: bool
    ) -> str:
        publish_dir = task_artifact_dir(self._artifacts_root, task_id) / "publish"
        publish_dir.mkdir(parents=True, exist_ok=True)
        path = publish_dir / "terminal-cleanup.json"
        path.write_text(
            json.dumps(
                {
                    "target_branch": outcome.target_branch,
                    "completed": completed,
                    "safe": outcome.safe,
                    "error": outcome.error,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return str(path)

    def _fingerprint(
        self, task_id: str, kind: str, subtask: int | None, head: str, paths: Sequence[str]
    ) -> str:
        material = "|".join([task_id, kind, str(subtask), head, ",".join(sorted(paths))])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
