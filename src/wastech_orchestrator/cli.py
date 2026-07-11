"""CLI entry point.

``install`` scaffolds a project layout under ``.worc/`` and writes config; ``run`` processes one
task end to end through the Orchestrator Core; ``watch`` resumes any in-flight task and then
processes pending tasks (one at a time, continuing only when ``orchestrator.auto_mode.enabled``);
``status`` reads persisted progress without starting work.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import NamedTuple, TextIO

from wastech_orchestrator import __version__, preflight, process_control
from wastech_orchestrator.config import upgrade as config_upgrade
from wastech_orchestrator.config.loader import ConfigError, load_config, loads_config
from wastech_orchestrator.config.schema import (
    CONFIG_SCHEMA_VERSION,
    MergeStrategy,
    OrchestratorConfig,
)
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.hitl import iter_task_interactions
from wastech_orchestrator.core.orchestrator import (
    Eligibility,
    FinalizePlan,
    MergePlan,
    Orchestrator,
    PipelineFailed,
    PipelineResult,
    RerunPlan,
    build_orchestrator,
    build_providers,
)
from wastech_orchestrator.core.state_machine import TERMINAL, Status
from wastech_orchestrator.env_file import count_env_file, load_env_file
from wastech_orchestrator.git_manager import (
    KIND_PR,
    GitCommandError,
    GitManager,
    append_runtime_excludes,
)
from wastech_orchestrator.install import config_writer, detect, wizard
from wastech_orchestrator.ledger import Ledger
from wastech_orchestrator.memory import (
    AuditActor,
    AuditContext,
    CleanupJob,
    DerivedIndex,
    LongTermKind,
    MemoryLayout,
    MemoryService,
    MemoryTier,
)
from wastech_orchestrator.notify import build_notifier
from wastech_orchestrator.notify.telegram import check_telegram_preflight
from wastech_orchestrator.observability.logging import configure_logging, set_log_level
from wastech_orchestrator.preflight import preflight_gh
from wastech_orchestrator.providers import process as agent_process
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.security.isolation import check_isolation
from wastech_orchestrator.state_store import IncompatibleStateError, StateStore, TaskRow
from wastech_orchestrator.task.model import DEFAULT_QUEUE, priority_rank
from wastech_orchestrator.task.parser import read_task_source, split_frontmatter

_LOG = logging.getLogger(__name__)

# --log-level names → stdlib logging levels for the structured operator trace.
_LOG_LEVELS: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

# Exit codes for a pipeline outcome. ``RUNNING`` is the B-lite soft pause: not a terminal failure,
# the task stays resumable and the next watch tick / run continues it — a distinct code so an
# operator (or CI) can tell "paused, provider down" from "failed".
_EXIT_BY_STATUS: dict[Status, int] = {
    Status.DONE: 0,
    Status.FAILED: 1,
    Status.MANUAL_ACTION_REQUIRED: 2,
    Status.RUNNING: 3,
}

# Default count for `list --recent` (and the "recent" section of the default overview).
_LIST_RECENT_DEFAULT = 10

# The orchestrator's runtime home inside the target repo. Everything the orchestrator
# generates or installs lives under `<repo>/.worc/` — gitignored as a whole — except the audit
# trail: the task lifecycle dirs below sit at the repo root and are audit-committed.
WORC_HOME = ".worc"

# Task lifecycle dirs created at the repo root by `install` (tracked; the audit commit captures the
# task file + its `<id>.summary.md` in done/failed). `tasks/rejected` is the quarantine and
# lives under `.worc/` instead, so rejected tasks are never swept into the audit commit.
# These are the install-time *default* layout (`paths.tasks_dir` defaults to "tasks"); the runtime
# reads `config.paths.tasks_dir` (see `pending_dir`). An operator who configures a different
# directory creates its lifecycle subfolders themselves.
REPO_TASK_DIRS: tuple[str, ...] = (
    "tasks/pending",
    "tasks/done",
    "tasks/failed",
)

# Runtime dirs created under `<repo>/.worc/` by `install` (all gitignored).
WORC_RUNTIME_DIRS: tuple[str, ...] = ("logs", "workspace", "tasks/rejected")

# `install` drops this commented template (never real values) so the operator knows which secrets
# the orchestrator reads from the environment. Copy it to `.worc/.env` and fill it in; the whole
# `.worc/` home is gitignored, so the real `.env` is never committed. Real exported env vars win.
ENV_EXAMPLE_FILENAME = ".env.example"
_ENV_EXAMPLE_TEMPLATE = """\
# wastech-orchestrator secrets — copy this file to `.env` (i.e. `.worc/.env`) and fill in values.
# The orchestrator auto-loads `<repo>/.worc/.env` at startup; an already-exported env var always
# wins over the file. This whole `.worc/` directory is gitignored, so `.worc/.env` is never
# committed. Never put secret VALUES in config.yaml or task files — keep them here (or export them).

# Telegram notifications / HITL (only used when telegram.enabled: true). The names must match
# telegram.bot_token_env / telegram.chat_id_env in config.yaml.
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Add any other variables the orchestrator process needs. A variable reaches a child process
# (codex / claude / git / gh / checks) only if its name is also in security.allowed_environment.
# GH_TOKEN=
"""


def _add_stop_force_flags(parser: argparse.ArgumentParser) -> None:
    """The stop-ladder force flags shared by ``stop`` and ``restart`` (mutually exclusive).

    No flag → idle stops with no prompt; a busy daemon refuses (interactive: confirm ``YES``).
    ``--force`` → soft stop (finish the current step). ``--force-full`` → hard stop: kill the active
    agent's process group now (POSIX; Windows degrades to soft). See ``_resolve_stop_level``.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--force",
        action="store_true",
        help="stop even while a task is active: soft (finish the current step, then exit)",
    )
    group.add_argument(
        "--force-full",
        dest="force_full",
        action="store_true",
        help="hard stop: kill the active agent's process group now (POSIX; Windows: soft)",
    )
    parser.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="never prompt: a busy daemon with no --force/--force-full is refused (exit 1), not "
        "confirmed. Used by scripts/CI and by 'worc shell' (a prompt would fight the REPL's stdin)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wastech-orchestrator",
        description="Orchestrator for coding agents (Codex / Claude Code) on top of Git.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        default=None,
        help="path to config.yaml (default: <repo-root>/.worc/config.yaml, discovered from cwd)",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="load environment variables from this file (default: the .env beside config.yaml, "
        "i.e. <repo-root>/.worc/.env); existing env vars are never overridden",
    )
    parser.add_argument(
        "--log-level",
        choices=sorted(_LOG_LEVELS),
        default=None,
        help="operator log level; overrides logging.level (default: logging.level, else info)",
    )
    parser.add_argument(
        "--log-format",
        choices=("logfmt", "json"),
        default="logfmt",
        help="operator log format for terminal and --log-file (default: logfmt)",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="also write rotating operator logs to this file (10 MB, 5 backups)",
    )
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=30.0,
        help="heartbeat interval for long provider/check/git operations; 0 disables (default: 30)",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    install_cmd = sub.add_parser(
        "install", help="set up the orchestrator in the current repo under .worc/ and write config"
    )
    install_cmd.add_argument(
        "repo_path", nargs="?", default=".", help="repository path (default: current directory)"
    )
    install_cmd.add_argument(
        "--provider",
        choices=("auto", "codex", "claude", "both"),
        default="auto",
        help="which providers to route to (default: auto-detect what is on PATH)",
    )
    install_cmd.add_argument(
        "--create-pr",
        dest="create_pr",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="create a pull request after push (default: yes when 'gh' is on PATH)",
    )
    install_cmd.add_argument(
        "--auto-mode",
        dest="auto_mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="process pending tasks back-to-back (default: no)",
    )
    install_cmd.add_argument(
        "--non-interactive",
        action="store_true",
        help="resolve everything from flags/detection; never prompt",
    )
    install_cmd.add_argument(
        "--reconfigure", action="store_true", help="back up and overwrite an existing config"
    )
    install_cmd.add_argument(
        "--skip-preflight", action="store_true", help="do not run preflight after writing config"
    )
    install_cmd.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")

    run_cmd = sub.add_parser("run", help="run a single task from a file")
    run_cmd.add_argument("task_file", help="path to the task file (.md or .json)")

    watch_cmd = sub.add_parser("watch", help="watch the tasks folder and run the tasks in it")
    watch_cmd.add_argument(
        "--poll-seconds",
        type=int,
        default=None,
        metavar="N",
        help="override orchestrator.poll_interval_seconds: fetch/pull base_branch and re-scan "
        "every N seconds (0 = single pass, no loop)",
    )
    watch_cmd.add_argument(
        "--queue",
        default=None,
        metavar="NAME",
        help="override orchestrator.queue: only pick pending tasks whose `queue` equals NAME "
        "(lets several worc instances share one task pool without colliding)",
    )

    stop_cmd = sub.add_parser("stop", help="stop a running 'watch' daemon gracefully")
    stop_cmd.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="seconds to wait for graceful shutdown before escalating (default: 30)",
    )
    _add_stop_force_flags(stop_cmd)

    restart_cmd = sub.add_parser(
        "restart", help="stop the running 'watch' daemon, then start a fresh one with these flags"
    )
    restart_cmd.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="seconds to wait for the previous watcher to exit (default: 30)",
    )
    _add_stop_force_flags(restart_cmd)
    restart_cmd.add_argument(
        "--poll-seconds",
        type=int,
        default=None,
        metavar="N",
        help="override orchestrator.poll_interval_seconds for the new loop (0 = single pass)",
    )
    restart_cmd.add_argument(
        "--queue",
        default=None,
        metavar="NAME",
        help="override orchestrator.queue for the new loop: only pick tasks whose `queue` is NAME",
    )

    sub.add_parser(
        "preflight", help="check both CLIs' health and the strict_isolation policy (read-only)"
    )
    telegram_test = sub.add_parser(
        "telegram-test",
        help="send a correlated Telegram prompt and wait for a reply",
    )
    telegram_test.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        metavar="N",
        help="maximum smoke-test wait, capped by telegram.ask_timeout_s (default: 60)",
    )
    status_cmd = sub.add_parser("status", help="show the active or latest persisted task status")
    status_cmd.add_argument("task_id", nargs="?", help="specific task id (default: active/latest)")

    top_cmd = sub.add_parser(
        "top", help="live read-only monitor: active task + queue + recent + daemon log (q to quit)"
    )
    top_cmd.add_argument(
        "--poll-seconds",
        type=float,
        default=None,
        metavar="SECONDS",
        help=f"refresh interval (default: {_TOP_DEFAULT_POLL_SECONDS:g})",
    )
    top_cmd.add_argument(
        "--queue",
        default=None,
        metavar="NAME",
        help="show only this queue's pending tasks (default: orchestrator.queue)",
    )
    top_cmd.add_argument(
        "--log-file",
        dest="tail_file",
        default=None,
        metavar="PATH",
        help="the daemon log file to tail (the path passed to 'watch --log-file')",
    )
    top_cmd.add_argument(
        "--recent",
        type=int,
        default=None,
        metavar="N",
        help=f"how many recent terminal tasks to show (default: {_LIST_RECENT_DEFAULT})",
    )

    shell_cmd = sub.add_parser(
        "shell",
        help="interactive operator console over the watch daemon (needs the [shell] extra)",
    )
    shell_cmd.add_argument(
        "--queue",
        default=None,
        metavar="NAME",
        help="serve and monitor this queue (default: orchestrator.queue)",
    )
    shell_cmd.add_argument(
        "--log-file",
        dest="tail_file",
        default=None,
        metavar="PATH",
        help="daemon log file to spawn-with and tail (default: .worc/logs/daemon.log)",
    )

    list_cmd = sub.add_parser(
        "list", help="enumerate the active / pending / recent tasks (read-only)"
    )
    list_view = list_cmd.add_mutually_exclusive_group()
    list_view.add_argument(
        "--pending", action="store_true", help="show only the tasks/pending queue"
    )
    list_view.add_argument(
        "--recent",
        nargs="?",
        type=int,
        const=_LIST_RECENT_DEFAULT,
        metavar="N",
        help=f"show only the last N terminal tasks (default {_LIST_RECENT_DEFAULT})",
    )
    list_view.add_argument(
        "--all", action="store_true", help="show every known task, across all statuses"
    )
    list_cmd.add_argument(
        "--format",
        choices=("table", "ids", "json"),
        default="table",
        help="output format: table (human, default), ids (one task id per line), or json",
    )
    list_cmd.add_argument(
        "--scope",
        choices=("rerun", "status", "finalize"),
        help="restrict ids to what the named command accepts (completion-facing; implies ids)",
    )

    completion_cmd = sub.add_parser(
        "completion", help="print a shell completion script (source it once) for bash or zsh"
    )
    completion_cmd.add_argument("shell", choices=("bash", "zsh"), help="the target shell")

    upgrade_cfg_cmd = sub.add_parser(
        "upgrade-config",
        help="add config keys introduced by the current version, preserving existing values",
    )
    upgrade_cfg_cmd.add_argument(
        "--dry-run", action="store_true", help="print what would change; write nothing"
    )

    upgrade_docs_cmd = sub.add_parser(
        "upgrade-docs",
        help="refresh the installed worc/ task-authoring docs to the packaged version (overwrite)",
    )
    upgrade_docs_cmd.add_argument(
        "--dry-run", action="store_true", help="print what would change; write nothing"
    )

    rerun_cmd = sub.add_parser(
        "rerun",
        help="re-attempt a terminal task: fresh from base, or --continue from the failed stage",
    )
    rerun_cmd.add_argument(
        "task_id", help="id of the failed / manual_action_required task to re-attempt"
    )
    rerun_cmd.add_argument(
        "--continue",
        dest="continue_",
        action="store_true",
        help="fix-and-continue: reuse the existing branch and re-enter at the stage it failed",
    )
    rerun_cmd.add_argument(
        "--reset-fix-budget",
        dest="reset_fix_budget",
        action="store_true",
        help="(--continue only) reset the consecutive fix-loop counters so an exhausted fix "
        "budget runs again; the global max_total_fix_iterations backstop is unchanged",
    )
    rerun_cmd.add_argument(
        "--from",
        dest="from_node",
        metavar="NODE",
        help="(--continue only) re-enter at NODE instead of the recorded checkpoint (must be a "
        "node in the checkpoint's flow)",
    )
    rerun_cmd.add_argument(
        "--force-reset-remote",
        action="store_true",
        help="(fresh mode) delete the prior attempt's remote branch, closing any open PR",
    )
    rerun_cmd.add_argument(
        "--dry-run", action="store_true", help="print the planned reconciliation; write nothing"
    )
    rerun_cmd.add_argument(
        "-y", "--yes", action="store_true", help="skip the interactive confirmation prompt"
    )

    finalize_cmd = sub.add_parser(
        "finalize",
        help="record + tidy a task the operator handled by hand (no pipeline, no commit/push/PR)",
    )
    finalize_cmd.add_argument("task_id", help="id of the task to finalize")
    finalize_cmd.add_argument(
        "--as",
        dest="as_",
        required=True,
        choices=("done", "failed", "abandoned"),
        help="the operator-declared terminal outcome",
    )
    finalize_cmd.add_argument("--pr-url", help="(--as done) the merged PR URL to record")
    finalize_cmd.add_argument("--note", help="a short reason recorded in the ledger")
    finalize_cmd.add_argument(
        "--delete-branch",
        action="store_true",
        help="delete the now-unneeded local agent branch (default: keep it)",
    )
    finalize_cmd.add_argument(
        "--no-verify-pr",
        action="store_true",
        help="(--as done) skip the read-only `gh pr view` merge check",
    )
    finalize_cmd.add_argument(
        "--dry-run", action="store_true", help="print the planned reconciliation; write nothing"
    )
    finalize_cmd.add_argument(
        "-y", "--yes", action="store_true", help="skip confirmation (incl. the WARNING prompts)"
    )

    prs_cmd = sub.add_parser(
        "prs", help="list orchestrator PRs that are open and awaiting merge (read-only)"
    )
    prs_cmd.add_argument(
        "--check",
        action="store_true",
        help="enrich each row with live GitHub state (needs gh; touches the network)",
    )
    prs_cmd.add_argument(
        "--sync",
        action="store_true",
        help="reconcile PRs merged externally on GitHub: record the merge (dry-run unless --yes)",
    )
    prs_cmd.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="(with --sync) write the reconciliation instead of a dry-run",
    )

    merge_cmd = sub.add_parser(
        "merge-task",
        help="merge a reviewed orchestrator PR: update branch w/ base, resolve conflicts, merge",
    )
    merge_cmd.add_argument("task_id", help="id of the task whose PR to merge")
    merge_cmd.add_argument(
        "--strategy",
        choices=("merge", "squash", "rebase"),
        help="gh pr merge strategy (default: git.auto_merge_strategy)",
    )
    merge_cmd.add_argument(
        "--wait-for-checks",
        dest="wait_for_checks",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="arm GitHub-native auto-merge (merge after required checks pass) instead of merging "
        "immediately (default: git.auto_merge_wait_for_checks)",
    )
    merge_cmd.add_argument(
        "--no-resolve",
        dest="resolve",
        action="store_false",
        help="on a conflict, abort instead of launching the merge flow (mechanical-only)",
    )
    merge_cmd.add_argument(
        "--dry-run", action="store_true", help="print the plan (PR, conflicts?); merge nothing"
    )
    merge_cmd.add_argument(
        "-y", "--yes", action="store_true", help="skip the confirmation (merging is consequential)"
    )

    tasks_cmd = sub.add_parser(
        "tasks", help="list all known tasks with status and branch (read-only)"
    )
    tasks_cmd.add_argument(
        "--status", help="show only tasks in this status (e.g. done, failed, running)"
    )

    logs_cmd = sub.add_parser("logs", help="manage task log artifacts under .worc/logs/")
    logs_sub = logs_cmd.add_subparsers(dest="logs_action", required=True)
    logs_clean = logs_sub.add_parser(
        "clean",
        help="remove task artifact directories under .worc/logs/ (the ledger is kept by default)",
    )
    logs_clean.add_argument(
        "--keep",
        type=int,
        metavar="N",
        help="keep the N most recently modified task dirs, remove the rest (no prompt unless N=0)",
    )
    logs_clean.add_argument(
        "--all",
        action="store_true",
        help="also remove the ledger (completed.jsonl); always confirms unless --yes",
    )
    logs_clean.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    memory_cmd = sub.add_parser(
        "memory", help="inspect and curate the persistent memory store (.worc/memory/)"
    )
    memory_sub = memory_cmd.add_subparsers(dest="memory_action", required=True)
    memory_sub.add_parser("show", help="summarize the store: tiers, counts, audit (read-only)")
    memory_sub.add_parser(
        "validate", help="report stale entities whose paths/symbols are gone (read-only)"
    )
    mem_compact = memory_sub.add_parser(
        "compact", help="run a fuller cleanup pass now: expire/remap/quarantine/merge (mutating)"
    )
    mem_compact.add_argument(
        "--dry-run", action="store_true", help="print the plan without writing anything"
    )
    mem_restore = memory_sub.add_parser(
        "restore", help="roll the store back to an audit snapshot (mutating)"
    )
    mem_restore.add_argument(
        "--snapshot",
        metavar="LABEL",
        help="snapshot dir under audit/snapshots/ to restore (default: the most recent)",
    )
    mem_restore.add_argument(
        "--dry-run", action="store_true", help="print the plan without writing anything"
    )
    mem_clear = memory_sub.add_parser(
        "clear",
        help="empty the store to zero: clear record tiers (reversible) or --purge the whole store",
    )
    mem_clear_scope = mem_clear.add_mutually_exclusive_group()
    mem_clear_scope.add_argument(
        "--kind",
        choices=[tier.value for tier in MemoryTier],
        metavar="TIER",
        help="clear only this record tier (default: all tiers): "
        + ", ".join(tier.value for tier in MemoryTier),
    )
    mem_clear_scope.add_argument(
        "--purge",
        action="store_true",
        help="hard reset: remove the entire .worc/memory/ store including the audit log and "
        "snapshots (irreversible)",
    )
    mem_clear.add_argument(
        "--dry-run", action="store_true", help="print what would be cleared, write nothing"
    )
    mem_clear.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    return parser


def _iter_template_files(root: Path) -> Iterator[Path]:
    """Yield template file paths relative to ``root``, sorted for deterministic output."""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path.relative_to(root)


def _worc_root() -> Traversable:
    """The packaged agent task-authoring docs (works from a source tree or a wheel).

    These ship as package data under ``packaged/guide/`` (the single aggregated home for everything
    shipped/seeded) and are copied into ``.worc/guide/`` by ``install`` so an AI agent can author
    tasks from a local, self-contained guide. Unlike the operator-editable flows, they are generated
    content with no operator edits — ``upgrade-docs`` overwrites them to the packaged version.
    """
    return resources.files("wastech_orchestrator").joinpath("packaged", "guide")


def _copy_worc_docs(dest_root: Path, *, overwrite: bool, dry: bool) -> tuple[list[str], list[str]]:
    """Copy the packaged ``worc/`` docs into ``dest_root/guide`` (the installed authoring guide).

    The packaged source dir is ``packaged/guide/``; it lands as ``guide/`` so the path reads
    ``.worc/guide/``. Existing files are skipped unless ``overwrite``;
    ``dry`` writes nothing. Returns ``(written, skipped)`` as ``guide/...`` relative paths.
    """
    written: list[str] = []
    skipped: list[str] = []
    with resources.as_file(_worc_root()) as wroot:
        for rel in _iter_template_files(Path(wroot)):
            label = str(Path("guide") / rel)
            dest = dest_root / "guide" / rel
            if dest.exists() and not overwrite:
                skipped.append(label)
                continue
            written.append(label)
            if not dry:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((Path(wroot) / rel).read_bytes())
    return written, skipped


def _flows_root() -> Traversable:
    """The packaged built-in flows (``implementation``/``deep_research``/``security_audit``) and
    their per-node role-prompt templates under each flow's own subdir (works from a source tree or a
    wheel).

    ``install`` copies this whole tree into ``.worc/flows/`` so the operator gets editable, *active*
    copies: the registry prefers ``<repo>/.worc/flows/<task_type>.yaml`` over the packaged built-in,
    and a node's ``role_file`` resolves under its flow-owned dir ``.worc/flows/<task_type>/``.
    Unlike the generated ``guide/``, these are operator-editable, so a plain re-run never clobbers
    them (see
    ``_copy_packaged_flows`` / ``_backup_flows_dir``).
    """
    return resources.files("wastech_orchestrator").joinpath("packaged", "flows")


def _copy_packaged_flows(
    dest_root: Path, *, overwrite: bool, dry: bool
) -> tuple[list[str], list[str]]:
    """Copy the packaged built-in flows + their ``roles/`` prompts into ``dest_root/flows``.

    Existing files are skipped unless ``overwrite`` (so a plain re-run preserves operator edits);
    ``dry`` writes nothing. Returns ``(written, skipped)`` as ``flows/...`` relative paths.
    """
    written: list[str] = []
    skipped: list[str] = []
    with resources.as_file(_flows_root()) as froot:
        for rel in _iter_template_files(Path(froot)):
            label = str(Path("flows") / rel)
            dest = dest_root / "flows" / rel
            if dest.exists() and not overwrite:
                skipped.append(label)
                continue
            written.append(label)
            if not dry:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((Path(froot) / rel).read_bytes())
    return written, skipped


def _backup_flows_dir(worc_home: Path) -> Path | None:
    """Snapshot an existing ``.worc/flows/`` to a timestamped sibling before ``--reconfigure``
    refreshes it, so operator edits (and any custom flows) stay recoverable. Returns the backup
    path, or ``None`` when there is nothing to back up. The backup lives under the gitignored
    ``.worc/`` home, so it never shows up in ``git status``.
    """
    flows = worc_home / "flows"
    if not flows.is_dir() or not any(flows.iterdir()):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = flows.with_name(f"flows.bak-{stamp}")
    shutil.copytree(flows, backup)
    return backup


def _tools_root() -> Traversable:
    """The packaged operator tools delivered into ``.worc/tools/`` (works from a source tree or a
    wheel).

    ``install`` copies this tree so a packaged flow's ``tool`` node resolves against a real
    executable on the install host: the shipped ``check_journey`` prose gate arrives as an
    extensionless ``+x`` script (POSIX) plus a ``.cmd`` wrapper (Windows). Like ``flows/`` these are
    delivered per machine (never committed — ``.worc/`` is gitignored), so the launcher always
    matches the install OS.
    """
    return resources.files("wastech_orchestrator").joinpath("packaged", "tools")


def _copy_packaged_tools(
    dest_root: Path, *, overwrite: bool, dry: bool
) -> tuple[list[str], list[str]]:
    """Copy the packaged operator tools into ``dest_root/tools`` (mirror of _copy_packaged_flows).

    Existing files are skipped unless ``overwrite`` (a plain re-run preserves operator edits);
    ``dry`` writes nothing. On POSIX every written file gets the ``+x`` bit: a wheel (and
    ``write_bytes``) drops the executable bit, yet the tool registry requires ``os.X_OK`` there — so
    without it the delivered tool would resolve as "not executable". On Windows executability is by
    suffix, so the chmod is skipped. Returns ``(written, skipped)`` as ``tools/...`` relative paths.
    """
    written: list[str] = []
    skipped: list[str] = []
    with resources.as_file(_tools_root()) as troot:
        for rel in _iter_template_files(Path(troot)):
            label = str(Path("tools") / rel)
            dest = dest_root / "tools" / rel
            if dest.exists() and not overwrite:
                skipped.append(label)
                continue
            written.append(label)
            if not dry:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes((Path(troot) / rel).read_bytes())
                if os.name != "nt":
                    dest.chmod(dest.stat().st_mode | 0o111)
    return written, skipped


def _backup_tools_dir(worc_home: Path) -> Path | None:
    """Snapshot an existing ``.worc/tools/`` to a timestamped sibling before ``--reconfigure``
    refreshes it (mirror of _backup_flows_dir), so any operator-added tools stay recoverable. The
    backup lives under the gitignored ``.worc/`` home. Returns the backup path, or ``None`` when
    there is nothing to back up.
    """
    tools = worc_home / "tools"
    if not tools.is_dir() or not any(tools.iterdir()):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = tools.with_name(f"tools.bak-{stamp}")
    shutil.copytree(tools, backup)
    return backup


def _config_example_root() -> Traversable:
    """The packaged commented ``config.example.yaml`` (works from a source tree or a wheel).

    ``install`` copies it verbatim into ``.worc/config.example.yaml`` so the operator has the
    field-by-field commentary beside the generated executable ``config.yaml``. It is a reference
    only — never read at runtime and never edited; --reconfigure refreshes it to the packaged copy.
    """
    return resources.files("wastech_orchestrator").joinpath("packaged", "config.example.yaml")


def _install_write_config_example(worc_home: Path, *, overwrite: bool) -> bool:
    """Copy the packaged ``config.example.yaml`` byte-for-byte into ``.worc/config.example.yaml``.

    Returns True iff a file was written. Existing files are skipped unless ``overwrite`` (a plain
    re-run leaves it; --reconfigure refreshes it). ``write_bytes`` keeps it byte-for-byte (LF).
    """
    dest = worc_home / "config.example.yaml"
    if dest.exists() and not overwrite:
        return False
    with resources.as_file(_config_example_root()) as src:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(Path(src).read_bytes())
    return True


def _load_config(path: str) -> OrchestratorConfig:
    """Load and semantically validate the config (fail-closed; non-fatal findings are logged)."""
    config = load_config(path).config
    for warning in validate_config(config):
        _LOG.warning("config warning: %s", warning)
    return config


def resolve_config_path(args: argparse.Namespace) -> str | None:
    """Find the config to load, in priority order:

    1. an explicit ``--config PATH``;
    2. ``<repo-root>/.worc/config.yaml`` — discovered by walking up from the cwd to the Git root, so
       any command works from any subdirectory of the repo;
    3. otherwise ``None`` (the caller prints an actionable hint).
    """
    explicit = getattr(args, "config", None)
    if explicit is not None:
        return str(explicit)
    info = detect.git_info(Path.cwd())
    if info is not None:
        candidate = info.root / WORC_HOME / "config.yaml"
        if candidate.is_file():
            return str(candidate)
    return None


def resolve_env_file_path(args: argparse.Namespace) -> tuple[Path | None, bool]:
    """Find the ``.env`` to load, returning ``(path, required)``:

    1. an explicit ``--env-file PATH`` — ``required=True`` (a missing file is an error);
    2. otherwise the ``.env`` beside the resolved ``config.yaml`` (``<repo-root>/.worc/.env``),
       falling back to ``<repo-root>/.worc/.env`` discovered from the Git root — ``required=False``
       (auto-discovery is best-effort: a missing file is a silent no-op);
    3. otherwise ``(None, False)``.
    """
    explicit = getattr(args, "env_file", None)
    if explicit is not None:
        return Path(explicit), True
    config_path = resolve_config_path(args)
    if config_path is not None:
        return Path(config_path).parent / ".env", False
    info = detect.git_info(Path.cwd())
    if info is not None:
        return info.root / WORC_HOME / ".env", False
    return None, False


def _load_env_file_for(args: argparse.Namespace) -> None:
    """Auto-load the orchestrator's ``.env`` before any command runs (real env vars always win).

    Loads **silently**: the ``.env`` status (path + variable count) is reported by ``preflight`` as
    a health line, not echoed on every command. A missing *explicit* ``--env-file`` is a
    fail-closed :class:`ConfigError` (exit 2); a missing auto-discovered ``.env`` is a silent no-op.
    """
    path, required = resolve_env_file_path(args)
    if path is None:
        return
    if not path.is_file():
        if required:
            raise ConfigError([f"--env-file not found: {path}"])
        return
    load_env_file(path)


def _env_preflight_line(env_file: Path | None) -> str:
    """One secret-free ``preflight`` health line describing the auto-loaded ``.env`` (path + count).

    Reports the same information the old per-command notice carried, but only where it belongs — as
    a health indicator. Never prints variable names or values.
    """
    if env_file is not None and env_file.is_file():
        count = count_env_file(env_file)
        if count:
            return f"env: OK — loaded {count} variable(s) from {env_file.as_posix()}"
        return f"env: OK — {env_file.as_posix()} defines no variables"
    return "env: OK — no .env file (using the process environment)"


def cmd_upgrade_config(args: argparse.Namespace) -> int:
    """Add config keys introduced by the current version, preserving existing operator values.

    The simple migration path (config/upgrade.py): merge the packaged template into the operator's
    config (add-missing-only), stamp the current ``schema_version``, back up the original, and write
    atomically. Idempotent — a config that is already current is left untouched (no rewrite, so its
    comments survive). Refuses a config that is unparsable or already newer than this orchestrator.
    """
    path_str = resolve_config_path(args)
    if path_str is None or not Path(path_str).is_file():
        target = f" ({path_str})" if path_str is not None else ""
        print(
            f"upgrade-config: no config.yaml found{target} — pass --config PATH, or run "
            "'install' in your repo (creates .worc/config.yaml)"
        )
        return 2
    path = Path(path_str).resolve()
    text = path.read_text(encoding="utf-8")
    # Fail-closed: a structural problem or a newer-than-supported schema_version raises ConfigError
    # (handled in main with a clean message + exit 2) — never upgrade a config we cannot read.
    load_config(path)

    operator = config_upgrade.parse_mapping(text)
    template = config_upgrade.packaged_template_mapping()
    merged, added, removed = config_upgrade.upgrade_config_mapping(template, operator)
    old_version = operator.get("schema_version", "absent")

    if merged == operator:
        print(f"upgrade-config: already up to date (schema_version {CONFIG_SCHEMA_VERSION})")
        return 0

    rendered = config_upgrade.render(merged)
    # Defensive: the regenerated config must load and pass before we touch the file.
    validate_config(loads_config(rendered, source="<upgraded config>").config)

    def _report(prefix: str) -> None:
        print(f"{prefix} {path}")
        print(f"  schema_version: {old_version} -> {CONFIG_SCHEMA_VERSION}")
        for key in added:
            print(f"  + {key}")
        for key in removed:
            print(f"  - {key} (removed in this schema version)")

    if args.dry_run:
        _report("upgrade-config (dry-run): would update")
        return 0

    backup = _install_backup_config(path)
    _install_atomic_write(path, rendered)
    _report("upgrade-config: updated")
    print(f"  backup: {backup}")
    return 0


def cmd_upgrade_docs(args: argparse.Namespace) -> int:
    """Refresh the installed ``.worc/guide/`` docs to the packaged version.

    The guide docs ship with the package, so an upgraded orchestrator carries newer docs than an
    already-installed copy. Unlike ``config.yaml`` they are generated content with no operator edits
    to preserve, so this is a straight overwrite-with-the-packaged-version (no backup): missing or
    differing files are written, files no longer in the package are removed. Idempotent — an
    already-current copy is a no-op — and ``--dry-run`` previews without writing. Fail-closed
    (exit 2) when no install location can be resolved, consistent with ``upgrade-config``.
    """
    path_str = resolve_config_path(args)
    if path_str is None or not Path(path_str).is_file():
        target = f" ({path_str})" if path_str is not None else ""
        print(
            f"upgrade-docs: no config.yaml found{target} — pass --config PATH, or run "
            "'install' in your repo (creates .worc/config.yaml)"
        )
        return 2
    worc_dir = Path(path_str).resolve().parent / "guide"

    packaged: dict[Path, bytes] = {}
    with resources.as_file(_worc_root()) as wroot:
        for rel in _iter_template_files(Path(wroot)):
            packaged[rel] = (Path(wroot) / rel).read_bytes()

    to_add: list[Path] = []
    to_update: list[Path] = []
    for rel, content in sorted(packaged.items()):
        dest = worc_dir / rel
        if not dest.is_file():
            to_add.append(rel)
        elif dest.read_bytes() != content:
            to_update.append(rel)
    installed = (
        {p.relative_to(worc_dir) for p in worc_dir.rglob("*") if p.is_file()}
        if worc_dir.is_dir()
        else set()
    )
    to_remove = sorted(installed - packaged.keys())

    if not (to_add or to_update or to_remove):
        print(f"upgrade-docs: already up to date ({len(packaged)} files in {worc_dir})")
        return 0

    def _report(prefix: str) -> None:
        print(f"{prefix} {worc_dir}")
        for rel in to_add:
            print(f"  + {Path('guide') / rel}")
        for rel in to_update:
            print(f"  ~ {Path('guide') / rel}")
        for rel in to_remove:
            print(f"  - {Path('guide') / rel}")

    if args.dry_run:
        _report("upgrade-docs (dry-run): would update")
        return 0

    for rel in to_add + to_update:
        _install_atomic_write(worc_dir / rel, packaged[rel].decode("utf-8"))
    for rel in to_remove:
        (worc_dir / rel).unlink()
    _report("upgrade-docs: updated")
    return 0


def load_config_for(args: argparse.Namespace) -> OrchestratorConfig | None:
    """Resolve + load a command's config; print an install hint and return ``None`` if not found."""
    path = resolve_config_path(args)
    if path is None:
        print(
            "no orchestrator config found. Run 'wastech-orchestrator install .' in your "
            "repository to set one up, or pass --config PATH."
        )
        return None
    config = _load_config(path)
    # Apply the persisted logging.level unless the operator passed --log-level (the flag wins).
    # Every command calls this right after _configure_runtime_logging, so this is the single seam.
    if getattr(args, "log_level", None) is None:
        set_log_level(_LOG_LEVELS[config.logging.level])
    return config


def worc_home_for(config: OrchestratorConfig) -> Path:
    """The orchestrator's gitignored runtime home: ``<repo>/.worc/``.

    Everything the orchestrator generates — ``state.db``, ``logs/``, ``orchestrator.pid``,
    ``workspace/``, ``checks/``, the resolved check profile, validation reports — lives here, plus
    the installed ``config.yaml``, ``templates/``, and ``guide/``. The whole dir is gitignored.
    """
    return Path(config.repo.local_path) / WORC_HOME


def tasks_root_for(config: OrchestratorConfig) -> Path:
    """The repo root that holds the tracked ``tasks/`` lifecycle dirs (the audit trail).

    Unlike :func:`worc_home_for`, ``tasks/`` stays at the repo root so the task file and its
    committed ``<id>.summary.md`` can be audit-committed into the repo's history.
    """
    return Path(config.repo.local_path)


def pending_dir(config: OrchestratorConfig) -> Path:
    """The folder ``watch`` scans for new tasks: ``<repo>/<paths.tasks_dir>/pending``."""
    return tasks_root_for(config) / config.paths.tasks_dir / "pending"


def has_active_task(config: OrchestratorConfig) -> bool:
    """True iff a task currently owns the processing slot (read-only ``state.db`` probe).

    An absent database is "idle" (``False``). Shared by the console's daemon shutdown and the stop
    ladder's idle/busy gate — the daemon may be alive but idle between ticks, which counts as idle.
    """
    db_path = Path(worc_home_for(config)) / "state.db"
    if not db_path.is_file():
        return False
    store = StateStore.open_readonly(db_path)
    try:
        return bool(store.find_active_tasks())
    finally:
        store.close()


def _configure_runtime_logging(args: argparse.Namespace) -> None:
    # The flag wins; absent (default None) we set up at INFO and let load_config_for re-apply the
    # persisted logging.level once the config is known.
    level = _LOG_LEVELS[args.log_level] if args.log_level else logging.INFO
    configure_logging(
        level=level,
        fmt=getattr(args, "log_format", "logfmt"),
        file_path=getattr(args, "log_file", None),
    )


def select_pending(folder: Path) -> list[Path]:
    """Pending task files (``.md`` / ``.json``), in a deterministic order."""
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in (".md", ".json"))


class _PendingScan(NamedTuple):
    """Lightweight scheduler view of a pending task file (no validation)."""

    task_id: str | None
    depends_on: tuple[str, ...]
    priority_rank: int
    queue: str
    # Front-matter ``title`` (or ``None``) — shown in the next-task confirmation prompt (idea 27),
    # alongside the id. Allowlisted for the prompt; never carries diff/prompt content.
    title: str | None = None


def _scan_pending_meta(task_file: Path) -> _PendingScan:
    """Lightweight front-matter read of ``id``/``depends_on``/``priority``/``queue``.

    Used by the scheduler for the cheap skip/eligibility/ordering/partitioning decision; full
    validation still happens in ``run_task``. A read/decode/parse problem (or a malformed
    ``depends_on``) yields no id and no deps, so the file falls through to the gate, which rejects
    it properly. ``priority`` and ``queue`` are fail-open here: an unrecognised priority sorts as
    ``mid`` (see :func:`priority_rank`) and a missing/malformed ``queue`` folds to the default
    queue, so a default instance still picks the file up and the gate then rejects a malformed queue
    fail-closed.
    """
    try:
        source = read_task_source(task_file)
        parse = split_frontmatter(source.raw_bytes.decode("utf-8"), source.suffix)
    except (OSError, UnicodeDecodeError):
        return _PendingScan(None, (), priority_rank(None), DEFAULT_QUEUE)
    if not parse.present or parse.malformed:
        return _PendingScan(None, (), priority_rank(None), DEFAULT_QUEUE)
    raw_id = parse.frontmatter.get("id")
    task_id = raw_id if isinstance(raw_id, str) else None
    raw_title = parse.frontmatter.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) and raw_title.strip() else None
    rank = priority_rank(parse.frontmatter.get("priority"))
    raw_queue = parse.frontmatter.get("queue")
    queue = raw_queue.strip() if isinstance(raw_queue, str) and raw_queue.strip() else DEFAULT_QUEUE
    raw_deps = parse.frontmatter.get("depends_on", [])
    if not isinstance(raw_deps, (list, tuple)) or not all(
        isinstance(d, str) and d.strip() for d in raw_deps
    ):
        return _PendingScan(task_id, (), rank, queue, title)
    return _PendingScan(task_id, tuple(d.strip() for d in raw_deps), rank, queue, title)


def scan_pending_sorted(folder: Path, selector: str) -> list[tuple[Path, _PendingScan]]:
    """Pending files for ``selector``'s queue, ranked exactly as :func:`watch_once` runs them.

    Keep only files whose ``queue`` equals ``selector`` (static partitioning across instances), then
    sort by ``(priority_rank, path)`` — :func:`select_pending` is already filename-sorted, so the
    path tie-break preserves the deterministic order within a priority. This is the single source of
    truth for "what order will the daemon actually run", shared by ``watch_once`` and the read-only
    monitor (``worc top`` / the console ``ps`` view) so the displayed order can never drift.
    """
    scans = [
        (p, s)
        for p, s in ((p, _scan_pending_meta(p)) for p in select_pending(folder))
        if s.queue == selector
    ]
    scans.sort(key=lambda item: (item[1].priority_rank, item[0]))
    return scans


def _confirm_next_task(
    orchestrator: Orchestrator,
    config: OrchestratorConfig,
    task_id: str | None,
    title: str | None,
) -> bool:
    """Ask the operator (Telegram) to approve claiming the next pending task (idea 27).

    Returns ``True`` only on an explicit approval; deny / timeout / no transport → ``False``
    (fail-closed STOP — the task stays pending, the operator decides later). Non-durable by design:
    a daemon restart mid-prompt simply re-asks next tick. Carries the task id + title only — never
    diff or prompt content. Preflight guarantees ``telegram.enabled`` when this gate is on.
    """
    label = task_id or "(unknown id)"
    context = f"Task {label}" + (f" — {title}" if title else "")
    result = orchestrator.notifier.ask_human(
        question="Start this task next?",
        context=context,
        task_id=task_id or "next-task",
        kind="approval",
        timeout_s=config.telegram.ask_timeout_s,
        interaction_id="next-task-" + uuid.uuid4().hex[:16],
    )
    approved = result.failure is None and result.answered and result.approved is True
    if not approved:
        _LOG.info(
            "next-task gate: not claiming %s (%s)",
            label,
            result.failure or ("denied" if result.answered else "no answer"),
        )
    return approved


def watch_once(
    orchestrator: Orchestrator,
    config: OrchestratorConfig,
    folder: Path,
    *,
    queue: str | None = None,
) -> list[PipelineResult]:
    """Resume any in-flight task, then process pending tasks per the auto-mode rule.

    Resumes the single active task first. Then picks pending tasks **only** when the slot is free;
    with auto mode off it processes exactly one, with auto mode on it continues to the next after a
    successful terminal cleanup. A ``manual_action_required`` outcome blocks further continuation.

    Only pending tasks whose ``queue`` equals this instance's selector are considered — plain
    string equality, static partitioning so several worc instances can share one git-distributed
    pool without colliding. ``queue`` defaults to ``config.orchestrator.queue`` (overridable per
    launch with ``worc watch --queue``). Out-of-queue tasks are invisible to this instance, so a
    cross-queue ``depends_on`` simply stays WAITING until the dependency is merged elsewhere.

    A task with unmerged ``depends_on`` dependencies is **skipped** (non-blocking) so an independent
    task can run instead — the slot never idles on CI; a dependency-broken task (cycle / unknown /
    self-ref) is terminally rejected. The skip does **not** consume the auto-mode-off "one task"
    budget, so the slot still runs one real eligible task per tick.

    Eligible tasks are ranked by ``priority`` (high → mid → low), ties broken by the filename order
    from :func:`select_pending`. ``depends_on`` is always stronger: a higher-priority but WAITING
    task is skipped, so a lower-priority eligible task still runs ahead of it.
    """
    results: list[PipelineResult] = []
    resumed = orchestrator.resume()
    if resumed is not None:
        results.append(resumed)
        if resumed.final_status is Status.MANUAL_ACTION_REQUIRED:
            return results
        if resumed.final_status not in TERMINAL:
            # B-lite soft pause (RUNNING): the active task is still parked on a provider outage and
            # holds the slot. Stop this tick — the between-tick poll sleep is the cool-off; the next
            # tick's resume() re-enters from the checkpoint (or fails it past max_blocked_s).
            return results

    auto = config.orchestrator.auto_mode.enabled
    selector = queue if queue is not None else config.orchestrator.queue
    # Partition + rank in one place (shared with the read-only monitor): drop other-queue tasks,
    # then order by (priority_rank, filename). pending_map is order-independent.
    scans = scan_pending_sorted(folder, selector)
    pending_map = {s.task_id: s.depends_on for _p, s in scans if s.task_id is not None}
    for task_file, scan in scans:
        task_id, depends_on = scan.task_id, scan.depends_on
        if not orchestrator.acquire_slot(""):
            break  # the slot is not free (an active task remains)
        if task_id is not None and depends_on:
            verdict = orchestrator.dependency_eligibility(task_id, depends_on, pending=pending_map)
            if verdict.state is Eligibility.WAITING:
                _LOG.info("task %s waiting: %s", task_id, verdict.detail)
                continue  # non-blocking skip — try the next eligible task
            if verdict.state is Eligibility.BROKEN:
                results.append(orchestrator.reject_dependency(str(task_file), verdict.detail))
                continue  # fail-closed terminal reject; the slot stays free
        if config.orchestrator.auto_mode.confirm_next_task and not _confirm_next_task(
            orchestrator, config, task_id, scan.title
        ):
            break  # operator denied / silent → leave pending, stop chaining this cycle (idea 27)
        result = orchestrator.run_task(str(task_file))
        results.append(result)
        if result.final_status is Status.MANUAL_ACTION_REQUIRED:
            break  # a manual task blocks automatic continuation
        if not auto:
            break  # auto mode off: process exactly one task
    return results


def _build_cleanup_hook(config: OrchestratorConfig) -> Callable[[], None] | None:
    """A rate-limited memory-cleanup callable for the ``watch_loop`` idle gap, or ``None``.

    Returns ``None`` when memory is disabled (Q10) — then no cleanup is ever scheduled. Otherwise a
    best-effort closure that runs one bounded :meth:`CleanupJob.run_once` at most every
    ``cleanup_min_interval_s`` (Q1), building a fresh store view + ``DerivedIndex`` each pass so the
    repo-introspection never goes stale across a long-lived daemon. A failure is logged and
    swallowed — cleanup must never crash the watcher or delay the next task pickup (AC-C2)."""
    if not config.memory.enabled:
        return None
    min_interval = float(config.memory.cleanup_min_interval_s)
    state: dict[str, float | None] = {"last": None}

    def _run() -> None:
        last = state["last"]
        now = time.monotonic()
        if last is not None and (now - last) < min_interval:
            return  # rate-limited: too soon since the last pass
        state["last"] = now
        try:
            layout = MemoryLayout.for_repo(config.repo.local_path)
            if not layout.root.exists():
                return  # nothing written yet — no work
            service = MemoryService(layout, config=config.memory)
            index = DerivedIndex(config.repo.local_path, derived_dir=layout.derived)
            job = CleanupJob(service, index, config.memory)
            report = job.run_once(audit=_memory_audit_context(AuditActor.CLEANUP))
            if report.ran and (
                report.expired or report.remapped or report.quarantined or report.merged
            ):
                _LOG.info(
                    "memory cleanup: expired %d, remapped %d, quarantined %d, merged %d",
                    report.expired,
                    report.remapped,
                    report.quarantined,
                    report.merged,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort; never crash the watcher
            _LOG.warning("memory cleanup failed (best-effort, ignored): %s", type(exc).__name__)

    return _run


def watch_loop(
    orchestrator: Orchestrator,
    config: OrchestratorConfig,
    folder: Path,
    *,
    poll_interval: int,
    queue: str | None = None,
    max_iterations: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
    stop_file: Path | None = None,
    cleanup_hook: Callable[[], None] | None = None,
) -> list[PipelineResult]:
    """Run ``watch_once`` on a loop, refreshing the repo each tick (periodic discovery).

    Each tick: ``refresh_repo`` (fetch + ff-only pull of ``base_branch`` when the slot is free, so a
    task pushed to git later becomes visible), then ``watch_once``, then sleep ``poll_interval``.
    ``poll_interval <= 0`` runs exactly one tick (single pass, no sleep). ``max_iterations`` bounds
    the loop for tests; in production the loop runs until interrupted.

    A stop is honored only *between* ticks, so an in-flight task run finishes its current stage
    rather than being interrupted. Two channels are checked: a ``stop_event`` (set by a ``SIGTERM``
    handler — POSIX) cuts the poll sleep short for a prompt shutdown, and a ``stop_file`` (the
    cross-platform sentinel ``stop`` writes) is polled at each tick so the daemon stops even where
    ``SIGTERM`` is undeliverable (Windows). The ``sleep_fn`` path is kept for callers without an
    event (existing tests).
    """

    def _stop_requested() -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        return stop_file is not None and process_control.stop_file_requested(stop_file)

    results: list[PipelineResult] = []
    iteration = 0
    while True:
        if _stop_requested():
            break
        orchestrator.refresh_repo()
        results.extend(watch_once(orchestrator, config, folder, queue=queue))
        iteration += 1
        # Idle-gap memory cleanup: the single-slot invariant guarantees no active task here, but
        # double-check (a RUNNING soft-pause still holds the slot) so cleanup never races a task or
        # delays the next pickup (AC-C2). Rate-limiting + bounds live inside the hook.
        if cleanup_hook is not None and not has_active_task(config):
            cleanup_hook()
        if poll_interval <= 0:
            break
        if max_iterations is not None and iteration >= max_iterations:
            break
        if stop_event is not None:
            if stop_event.wait(poll_interval):  # returns True the instant SIGTERM fires (POSIX)
                break
        else:
            sleep_fn(poll_interval)
        if _stop_requested():  # cross-platform stop-file, noticed between ticks
            break
    return results


def cmd_run(args: argparse.Namespace) -> int:
    """Process exactly one task file through the Core pipeline."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if config.git.create_pull_request:
        preflight.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish
        preflight.warn_if_gh_logged_out()  # non-blocking advisory if gh is present but logged out
    orchestrator = build_orchestrator(
        config,
        artifacts_root=worc_home_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
    )
    # Refuse an explicit run of a dependent whose dependencies are not merged — never build it on a
    # stale base. Unlike ``watch`` (which skips/retries), an explicit ``run`` of an ineligible task
    # is a controlled refusal with a non-zero exit; a malformed file falls through to the gate.
    scan = _scan_pending_meta(Path(args.task_file))
    task_id, depends_on = scan.task_id, scan.depends_on
    if task_id is not None and depends_on:
        verdict = orchestrator.dependency_eligibility(task_id, depends_on, pending={})
        if verdict.state is not Eligibility.ELIGIBLE:
            print(f"error: refusing to run {task_id}: {verdict.detail}", file=sys.stderr)
            return 2
    result = orchestrator.run_task(args.task_file)
    if result.final_status is Status.RUNNING:
        # B-lite soft pause: every provider was transiently unavailable. The task is left resumable;
        # the next `run`/`watch`/restart continues it from the checkpoint (until max_blocked_s).
        print(f"{result.task_id}: paused — provider unavailable, will resume")
        return _EXIT_BY_STATUS[Status.RUNNING]
    if result.validation_reason:
        # F5a: a gate reject prints the machine reason AND the field+cause detail, so the operator
        # sees WHICH front-matter field and WHY without opening the JSON validation report.
        detail = f" ({result.validation_detail})" if result.validation_detail else ""
        print(f"{result.task_id}: rejected — {result.validation_reason}{detail}", file=sys.stderr)
        return _EXIT_BY_STATUS.get(result.final_status, 1)
    suffix = f" → {result.pr_url}" if result.pr_url else ""
    print(f"{result.task_id}: {result.final_status.value}{suffix}")
    return _EXIT_BY_STATUS.get(result.final_status, 1)


def _confirm(prompt: str) -> bool:
    """Interactive y/N confirmation for a state-mutating operator command (default: no)."""
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def _confirm_yes(prompt: str) -> bool:
    """Require the literal uppercase ``YES`` (a deliberate, consequential confirmation; EOF → no).

    Used by the stop ladder when a task is active: typing ``YES`` maps to the *soft* stop only —
    destroying in-flight agent work needs the explicit ``--force-full`` flag, not a typed shortcut.
    """
    try:
        return input(prompt).strip() == "YES"
    except EOFError:
        return False


def _report_rerun_plan(plan: RerunPlan) -> None:
    """Print the planned reconciliation for ``rerun --dry-run``; writes nothing."""
    mode = "continue" if plan.continue_mode else "fresh"
    current = plan.current_status.value if plan.current_status else "unknown"
    print(f"rerun (dry-run): would re-attempt {plan.task_id} [{mode}]")
    print(f"  current status: {current}")
    if plan.continue_mode:
        node = plan.interrupted_node or "unknown"
        print(f"  branch:    reuse {plan.branch or '(none)'}")
        if plan.from_node:
            print(f"  re-enter:  {plan.from_node} (override; checkpoint was {node})")
        else:
            print(f"  re-enter:  {node}")
        print("  artifacts: kept; pending HITL prompt reset so the node re-asks")
        if plan.reset_fix_budget:
            print(
                "  state:     terminal markers cleared; consecutive fix budget reset "
                "(global backstop kept); subtasks/publish-ops kept"
            )
        else:
            print("  state:     terminal markers cleared; counters/subtasks/publish-ops kept")
    else:
        target = plan.branch or "worc/<id>-<slug>"
        archive = f"attempt-{max(plan.attempt - 1, 0)}"
        print(f"  branch:    reset {target} to base '{plan.base_branch}'")
        print(f"  artifacts: archived to logs/{plan.task_id}/{archive}/")
        print("  state:     counters, branch/slug, decomposition, subtasks, publish-ops cleared")
        if plan.has_remote_branch or plan.pr_url:
            print(f"  remote/PR: delete remote branch (closes PR {plan.pr_url or ''})")
    rerun_of = plan.task_id if plan.attempt > 1 else None
    print(
        f"  ledger:         append a record (attempt={plan.attempt}, rerun_of={rerun_of}); "
        "prior records kept"
    )
    for note in plan.notes:
        print(f"  note:      {note}")


def cmd_rerun(args: argparse.Namespace) -> int:
    """Re-attempt a terminal task: fresh from base, or ``--continue`` from the failed stage."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    root = worc_home_for(config)

    # Rerun drives the pipeline in the shared clone; refuse while a live watch daemon owns it.
    pid = process_control.running_daemon_pid(process_control.pid_file_path(root))
    if pid is not None:
        print(
            f"rerun: the watch daemon is running (pid {pid}); stop it first with "
            "'wastech-orchestrator stop'"
        )
        return 1

    if not (Path(root) / "state.db").is_file():
        print(f"rerun: no state database at {Path(root) / 'state.db'}")
        return 2

    target_id: str = args.task_id
    orchestrator = build_orchestrator(
        config,
        artifacts_root=root,
        heartbeat_seconds=args.heartbeat_seconds,
        is_recovery_rerun=lambda i: i == target_id,
    )
    plan = orchestrator.plan_rerun(
        args.task_id,
        continue_mode=args.continue_,
        force_reset_remote=args.force_reset_remote,
        reset_fix_budget=args.reset_fix_budget,
        from_node=args.from_node,
    )
    if plan.refusals:
        for reason in plan.refusals:
            print(f"rerun: {reason}")
        return 1

    if args.dry_run:
        _report_rerun_plan(plan)
        return 0

    if config.git.create_pull_request:
        preflight.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish
        preflight.warn_if_gh_logged_out()  # non-blocking advisory if gh is present but logged out

    for note in plan.notes:
        print(f"rerun: note: {note}")
    mode = "continue" if args.continue_ else "fresh"
    if not args.yes and not _confirm(
        f"Rerun {args.task_id} [{mode}] from base '{plan.base_branch}'? [y/N] "
    ):
        print("rerun: aborted")
        return 0

    if args.continue_:
        result = orchestrator.continue_task(
            args.task_id,
            reset_fix_budget=args.reset_fix_budget,
            from_node=args.from_node,
        )
        label = "rerun/continue"
    else:
        assert plan.source_path is not None  # guarded by plan_rerun refusals
        result = orchestrator.rerun_task(
            args.task_id,
            source_path=plan.source_path,
            force_reset_remote=args.force_reset_remote,
        )
        label = "rerun"
    suffix = f" → {result.pr_url}" if result.pr_url else ""
    print(f"{result.task_id}: {result.final_status.value}{suffix} ({label})")
    return _EXIT_BY_STATUS.get(result.final_status, 1)


_FINALIZE_STATUS: dict[str, Status] = {
    "done": Status.DONE,
    "failed": Status.FAILED,
    "abandoned": Status.MANUAL_ACTION_REQUIRED,  # variant A: manual + outcome="abandoned" in ledger
}


def _report_finalize_plan(plan: FinalizePlan, *, as_: str) -> None:
    """Print the planned reconciliation for ``finalize --dry-run``; writes nothing."""
    print(f"finalize (dry-run): would finalize {plan.task_id} as {as_}")
    print(
        f"  status:    {plan.current_status.value if plan.current_status else '?'} -> "
        f"{plan.declared.value}"
    )
    if plan.declared is Status.DONE:
        pr = plan.pr_url or "(none)"
        verify = f", verify={plan.verify_state}" if plan.verify_state else ""
        print(f"  pr url:    {pr} (source: {plan.pr_url_source}{verify})")
    print(f"  cleanup:   checkout base '{plan.base_branch}'")
    print(f"  branch:    {plan.branch or '(none)'}")
    abandoned = ", outcome=abandoned" if plan.declared is Status.MANUAL_ACTION_REQUIRED else ""
    print(f"  ledger:    append a manual record{abandoned}")
    for warning in plan.warnings:
        print(f"  WARNING:   {warning}")


def cmd_finalize(args: argparse.Namespace) -> int:
    """Record + tidy a task the operator handled out-of-band (no pipeline, no commit/push/PR)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    root = worc_home_for(config)

    # Finalize runs terminal cleanup (`git checkout base`) in the shared clone; refuse while a live
    # watch daemon owns it. An orphaned-active task (dead PID) is exactly what finalize reconciles.
    pid = process_control.running_daemon_pid(process_control.pid_file_path(root))
    if pid is not None:
        print(
            f"finalize: the watch daemon is running (pid {pid}); stop it first with "
            "'wastech-orchestrator stop'"
        )
        return 1

    if not (Path(root) / "state.db").is_file():
        print(f"finalize: no state database at {Path(root) / 'state.db'}")
        return 2

    declared = _FINALIZE_STATUS[args.as_]
    orchestrator = build_orchestrator(
        config, artifacts_root=root, heartbeat_seconds=args.heartbeat_seconds
    )
    plan = orchestrator.plan_finalize(
        args.task_id, declared=declared, pr_url=args.pr_url, verify=not args.no_verify_pr
    )
    if plan.refusals:
        for reason in plan.refusals:
            print(f"finalize: {reason}")
        return 1

    if args.dry_run:
        _report_finalize_plan(plan, as_=args.as_)
        return 0

    if not args.yes:
        for warning in plan.warnings:
            print(f"finalize: WARNING — {warning}")
        prompt = f"Finalize {args.task_id} as {args.as_}"
        prompt += " (unconfirmed)? [y/N] " if plan.warnings else "? [y/N] "
        if not _confirm(prompt):
            print("finalize: aborted")
            return 0

    result = orchestrator.finalize_task(
        args.task_id,
        declared=declared,
        pr_url=plan.pr_url,
        note=args.note,
        delete_branch=args.delete_branch,
    )
    suffix = f" → {result.pr_url}" if result.pr_url else ""
    print(f"{result.task_id}: {result.final_status.value}{suffix} (finalized)")
    return _EXIT_BY_STATUS.get(result.final_status, 1)


def _recorded_pr_url(store: StateStore, task_id: str) -> str | None:
    """The PR URL a task's completed ``pr`` publish op recorded (read straight off the store)."""
    op = store.get_publish_op(task_id, KIND_PR, None)
    return op.result_ref if op is not None else None


def cmd_prs(args: argparse.Namespace) -> int:
    """List orchestrator PRs that are open and awaiting merge.

    Default + ``--check`` never write: the listing is DB-only (``status``'s read-only path) and
    ``--check`` adds a live ``gh`` probe per row. ``--sync`` reconciles PRs merged externally on
    GitHub — dry-run unless ``-y/--yes``."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    root = worc_home_for(config)
    db_path = Path(root) / "state.db"
    if not db_path.is_file():
        print(f"prs: no state database at {db_path}")
        return 2
    if args.sync:
        return _cmd_prs_sync(args, config, root)

    store = StateStore.open_readonly(db_path)
    try:
        rows = store.find_open_pr_tasks()
        pr_urls = {r.task_id: _recorded_pr_url(store, r.task_id) for r in rows}
    finally:
        store.close()
    if not rows:
        print("prs: no open orchestrator PRs awaiting merge")
        return 0
    # ``--check`` adds a live gh probe; its read-only store is GitManager's (closed in finally).
    git_store = StateStore.open_readonly(db_path) if args.check else None
    git = (
        GitManager(config, store=git_store, artifacts_root=str(root))
        if git_store is not None
        else None
    )
    try:
        for row in rows:
            url = pr_urls.get(row.task_id)
            line = f"{row.status.value:<22} {row.task_id}  {row.title or ''}"
            if row.branch:
                line += f"  ({row.branch})"
            line += f"  {url or '(no url)'}"
            if git is not None and url:
                line += f"  [{git.verify_pr_state(url) or 'unknown'}]"
            print(line)
    finally:
        if git_store is not None:
            git_store.close()
    return 0


def _cmd_prs_sync(args: argparse.Namespace, config: OrchestratorConfig, root: Path) -> int:
    """The ``prs --sync`` reconcile path: dry-run by default, writes only with ``-y/--yes``."""
    # A write run touches the shared clone/DB; refuse while a live watch daemon owns it (like
    # finalize). The read-only dry-run is always safe.
    if args.yes:
        pid = process_control.running_daemon_pid(process_control.pid_file_path(root))
        if pid is not None:
            print(f"prs --sync: the watch daemon is running (pid {pid}); stop it first")
            return 1
    orchestrator = build_orchestrator(
        config, artifacts_root=root, heartbeat_seconds=args.heartbeat_seconds
    )
    entries = orchestrator.sync_external_merges(write=args.yes)
    if not entries:
        print("prs --sync: no open orchestrator PRs to reconcile")
        return 0
    prefix = "" if args.yes else "[dry-run] "
    for e in entries:
        if e.action == "record-merge":
            tail = " + finalized done" if e.finalized_done else ""
            verb = "recorded merge" if args.yes else "would record merge"
            print(f"{prefix}{e.task_id}: {verb} ({e.pr_url}){tail}")
        elif e.action == "closed-no-merge":
            print(f"{prefix}{e.task_id}: PR closed without merge ({e.pr_url}) — no change")
        elif e.action == "unverifiable":
            print(f"{prefix}{e.task_id}: PR state unverifiable (gh offline/unauth) — skipped")
        else:  # still-open
            print(f"{prefix}{e.task_id}: PR still open — skipped")
    if not args.yes and any(e.action == "record-merge" for e in entries):
        print("prs --sync: re-run with --yes to write the above")
    return 0


def _report_merge_plan(
    plan: MergePlan, *, strategy: MergeStrategy, wait_for_checks: bool, resolve: bool
) -> None:
    """Print the ``merge-task --dry-run`` plan (writes/merges nothing)."""
    print(f"merge-task plan for {plan.task_id}:")
    print(f"  status:   {plan.status.value if plan.status else '(unknown)'}")
    print(f"  branch:   {plan.branch or '(none)'}")
    print(f"  base:     {plan.base_branch}")
    print(f"  pr:       {plan.pr_url or '(none)'}")
    if plan.verify_state:
        print(f"  pr state: {plan.verify_state}")
    if plan.already_merged:
        print("  -> PR already merged; merge-task would just record it (idempotent)")
    else:
        wait = " (wait for checks)" if wait_for_checks else ""
        resolve_note = "" if resolve else "; --no-resolve: abort on conflict"
        print(f"  -> update branch w/ base, then merge via '{strategy.value}'{wait}{resolve_note}")
    for warning in plan.warnings:
        print(f"  WARNING — {warning}")


def cmd_merge_task(args: argparse.Namespace) -> int:
    """Operator go-ahead to merge a reviewed orchestrator PR (mirrors ``finalize``'s ergonomics)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    root = worc_home_for(config)
    # merge-task updates the branch + runs gh/merge in the shared clone; refuse while the daemon
    # owns it (like finalize). The merge flow + git ops need the idle slot.
    pid = process_control.running_daemon_pid(process_control.pid_file_path(root))
    if pid is not None:
        print(
            f"merge-task: the watch daemon is running (pid {pid}); stop it first with "
            "'wastech-orchestrator stop'"
        )
        return 1
    if not (Path(root) / "state.db").is_file():
        print(f"merge-task: no state database at {Path(root) / 'state.db'}")
        return 2

    orchestrator = build_orchestrator(
        config, artifacts_root=root, heartbeat_seconds=args.heartbeat_seconds
    )
    plan = orchestrator.plan_merge(args.task_id, verify=True)
    if plan.refusals:
        for reason in plan.refusals:
            print(f"merge-task: {reason}")
        return 1

    strategy = MergeStrategy(args.strategy) if args.strategy else config.git.auto_merge_strategy
    wait_for_checks = (
        config.git.auto_merge_wait_for_checks
        if args.wait_for_checks is None
        else args.wait_for_checks
    )
    if args.dry_run:
        _report_merge_plan(
            plan, strategy=strategy, wait_for_checks=wait_for_checks, resolve=args.resolve
        )
        return 0

    if not args.yes:
        for warning in plan.warnings:
            print(f"merge-task: WARNING — {warning}")
        verb = "Record already-merged" if plan.already_merged else f"Merge via {strategy.value}"
        if not _confirm(f"{verb} {args.task_id}? [y/N] "):
            print("merge-task: aborted")
            return 0

    try:
        result = orchestrator.merge_task(
            args.task_id,
            strategy=strategy,
            wait_for_checks=wait_for_checks,
            resolve=args.resolve,
        )
    except (PipelineFailed, GitCommandError) as exc:
        print(f"merge-task: {exc}")
        return 1
    suffix = f" → {result.pr_url}" if result.pr_url else ""
    print(f"{result.task_id}: {result.final_status.value}{suffix} (merged)")
    return _EXIT_BY_STATUS.get(result.final_status, 1)


def cmd_tasks(args: argparse.Namespace) -> int:
    """List every known task with its status and branch (read-only); ``--status`` filters."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    db_path = Path(worc_home_for(config)) / "state.db"
    if not db_path.is_file():
        print("tasks: no tasks")
        return 0
    store = StateStore.open_readonly(db_path)
    try:
        rows = store.all_tasks()
    finally:
        store.close()
    if args.status:
        want = args.status.strip().lower()
        rows = [r for r in rows if r.status.value == want]
    if not rows:
        suffix = f" with status '{args.status}'" if args.status else ""
        print(f"tasks: no tasks{suffix}")
        return 0
    for row in rows:
        print(_entry_line(_task_entry(row)))
    return 0


def _task_log_dirs(logs_root: Path) -> list[Path]:
    """Per-task artifact dirs under ``.worc/logs/`` (direct subdirectories), newest first.

    ``completed.jsonl`` is a file, so it is naturally excluded. Sorted by mtime descending so
    ``--keep N`` retains the most recently modified runs.
    """
    if not logs_root.is_dir():
        return []
    dirs = [p for p in logs_root.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)


def cmd_logs(args: argparse.Namespace) -> int:
    """Dispatch the ``logs`` subcommands (currently only ``clean``)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if args.logs_action == "clean":
        return _cmd_logs_clean(args, config)
    raise SystemExit(f"Unknown logs action '{args.logs_action}'.")


def _cmd_logs_clean(args: argparse.Namespace, config: OrchestratorConfig) -> int:
    """Remove task artifact dirs under ``.worc/logs/`` to reclaim disk.

    ``--keep N`` retains the N newest task dirs (no prompt unless N=0); bare ``clean`` removes every
    task dir; ``--all`` additionally removes the ledger. The ledger (``completed.jsonl``) is the
    audit trail and is preserved unless ``--all``. Running this while a task is active is
    unsupported.
    """
    logs_root = worc_home_for(config) / "logs"
    ledger_path = Ledger(logs_root).path
    task_dirs = _task_log_dirs(logs_root)
    if not task_dirs and not (args.all and ledger_path.exists()):
        print("logs clean: nothing to remove")
        return 0

    if args.keep is not None:
        if args.keep < 0:
            print("logs clean: --keep must be >= 0")
            return 2
        kept, doomed = task_dirs[: args.keep], task_dirs[args.keep :]
        # N=0 is equivalent to delete-all → confirm like the bare form.
        if (
            args.keep == 0
            and not args.yes
            and not _confirm(
                f"Remove all {len(doomed)} task log dir(s) under {logs_root.as_posix()}? [y/N] "
            )
        ):
            print("logs clean: aborted")
            return 0
        for path in doomed:
            shutil.rmtree(path, ignore_errors=True)
        print(f"logs clean: removed {len(doomed)} task dir(s); kept {len(kept)}")
        return 0

    # Bare clean (optionally --all): a full sweep — always confirm unless --yes.
    target = "all task logs and the ledger" if args.all else "all task logs"
    if not args.yes and not _confirm(f"Remove {target} under {logs_root.as_posix()}? [y/N] "):
        print("logs clean: aborted")
        return 0
    for path in task_dirs:
        shutil.rmtree(path, ignore_errors=True)
    removed_ledger = False
    if args.all and ledger_path.exists():
        ledger_path.unlink()
        removed_ledger = True
    suffix = " and the ledger" if removed_ledger else " (ledger kept)"
    print(f"logs clean: removed {len(task_dirs)} task dir(s){suffix}")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    """Dispatch the ``memory`` subcommands: show / validate (read-only) | compact / restore.

    Disabled memory (``memory.enabled: false`` or the block absent) is a clean no-op for every verb
    (Q10). The mutating verbs (compact / restore) refuse while a task is active and offer
    ``--dry-run`` to print their plan first (AC-C1)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if not config.memory.enabled:
        print("memory: disabled in config (memory.enabled: false) — nothing to do")
        return 0
    layout = MemoryLayout.for_repo(config.repo.local_path)
    action = args.memory_action
    if action == "show":
        return _cmd_memory_show(layout)
    if action == "validate":
        return _cmd_memory_validate(config, layout)
    if action == "compact":
        return _cmd_memory_compact(config, layout, dry_run=args.dry_run)
    if action == "restore":
        return _cmd_memory_restore(config, layout, snapshot=args.snapshot, dry_run=args.dry_run)
    if action == "clear":
        return _cmd_memory_clear(
            config, layout, kind=args.kind, purge=args.purge, dry_run=args.dry_run, yes=args.yes
        )
    raise SystemExit(f"Unknown memory action '{args.memory_action}'.")


def _memory_audit_context(actor: AuditActor) -> AuditContext:
    """An operator-actor audit context stamped with the current UTC time (the CLI's clock)."""
    return AuditContext(timestamp=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), actor=actor)


def _cmd_memory_show(layout: MemoryLayout) -> int:
    """Read-only summary of the store: per-tier counts, audit health, snapshot count."""
    if not layout.root.exists():
        print("memory: no store yet (.worc/memory/ has not been written)")
        return 0
    service = MemoryService(layout)
    long_term = {kind.value: len(service.read_long_term(kind)) for kind in LongTermKind}
    print(f"memory store: {layout.as_posix()}")
    print(f"  episodes (short-term): {len(service.read_episodes())}")
    print(f"  long-term: {sum(long_term.values())} ({_counts_line(long_term)})")
    print(f"  entities: {len(service.read_entities())}")
    print(f"  quarantine: {len(service.read_quarantine())}")
    rows = service.audit.rows()
    intact = "intact" if service.audit.verify_chain() else "BROKEN"
    print(f"  audit: {len(rows)} row(s), chain {intact}")
    for row in rows[-5:]:
        print(f"    {row.get('timestamp')} {row.get('action')} {row.get('affected_ids')}")
    snaps = _snapshot_labels(layout)
    print(f"  snapshots: {len(snaps)}" + (f" (latest {snaps[-1]})" if snaps else ""))
    return 0


def _cmd_memory_validate(config: OrchestratorConfig, layout: MemoryLayout) -> int:
    """Read-only staleness report: entities whose paths are gone, split into remap vs quarantine."""
    if not layout.root.exists():
        print("memory: no store yet — nothing to validate")
        return 0
    service = MemoryService(layout)
    index = DerivedIndex(config.repo.local_path, derived_dir=layout.derived)
    remap: list[str] = []
    stale: list[str] = []
    for row in service.read_entities():
        if str(row.get("status") or "active") != "active":
            continue
        paths = [p for p in (row.get("paths") or []) if isinstance(p, str)]
        missing = [p for p in paths if not index.path_exists(p)]
        if not missing:
            continue
        eid = str(row.get("entity_id"))
        if all(len(index.find_by_basename(p)) == 1 for p in missing):
            remap.append(f"{eid}: {missing} → same-basename move")
        else:
            stale.append(f"{eid}: {missing} gone")
    print(f"memory validate: {len(remap)} remappable, {len(stale)} stale entity card(s)")
    for line in (*remap, *stale):
        print(f"  - {line}")
    if not remap and not stale:
        print("  store is clean (no missing-path entities)")
    print("(read-only; run 'worc memory compact' to apply remaps/quarantines)")
    return 0


def _cmd_memory_compact(config: OrchestratorConfig, layout: MemoryLayout, *, dry_run: bool) -> int:
    """Run a fuller (uncapped) cleanup pass now — refused while a task is active (FR6/AC-C2)."""
    if has_active_task(config):
        print("memory compact: a task is active — refusing; run when the orchestrator is idle")
        return 1
    if not layout.root.exists():
        print("memory compact: no store yet — nothing to do")
        return 0
    service = MemoryService(layout, config=config.memory)
    index = DerivedIndex(config.repo.local_path, derived_dir=layout.derived)
    job = CleanupJob(service, index, config.memory)
    audit = _memory_audit_context(AuditActor.OPERATOR)
    report = job.run_once(audit=audit, full=True, dry_run=dry_run)
    verb = "would" if dry_run else "did"
    if not report.ran:
        print("memory compact: nothing on disk to act on")
        return 0
    print(
        f"memory compact ({'dry-run' if dry_run else 'done'}): scanned {report.scanned}; "
        f"{verb} expire {report.expired}, remap {report.remapped}, "
        f"quarantine {report.quarantined}, merge {report.merged}"
    )
    if not dry_run and report.snapshot is not None:
        print(f"  snapshot: {report.snapshot}")
    return 0


def _cmd_memory_restore(
    config: OrchestratorConfig, layout: MemoryLayout, *, snapshot: str | None, dry_run: bool
) -> int:
    """Roll the store back to an audit snapshot — refused while a task is active (AC-SF4)."""
    if has_active_task(config):
        print("memory restore: a task is active — refusing; run when the orchestrator is idle")
        return 1
    labels = _snapshot_labels(layout)
    if not labels:
        print("memory restore: no snapshots under audit/snapshots/")
        return 1
    chosen = snapshot if snapshot is not None else labels[-1]
    if chosen not in labels:
        print(f"memory restore: snapshot {chosen!r} not found; available: {', '.join(labels)}")
        return 2
    target = layout.snapshots / chosen
    files = sorted(p for p in target.rglob("*") if p.is_file())
    if dry_run:
        print(f"memory restore (dry-run): would restore {len(files)} file(s) from {chosen}")
        for path in files:
            print(f"  - {path.relative_to(target).as_posix()}")
        return 0
    service = MemoryService(layout, config=config.memory)
    restored = service.restore(target, audit=_memory_audit_context(AuditActor.OPERATOR))
    print(f"memory restore: restored {len(restored)} file(s) from {chosen}")
    return 0


def _cmd_memory_clear(
    config: OrchestratorConfig,
    layout: MemoryLayout,
    *,
    kind: str | None,
    purge: bool,
    dry_run: bool,
    yes: bool,
) -> int:
    """Empty the store to zero — refused while a task is active.

    Default (or ``--kind TIER``): a reversible content-clear — snapshot first, then empty the
    record tier(s) through the audited seam, so it is restore-able via ``worc memory restore``.
    ``--purge``: remove the whole ``.worc/memory/`` store, audit log and snapshots included
    (irreversible). ``--kind`` and ``--purge`` are mutually exclusive (enforced by argparse)."""
    if has_active_task(config):
        print("memory clear: a task is active — refusing; run when the orchestrator is idle")
        return 1
    if not layout.root.exists():
        print("memory clear: no store yet — nothing to clear")
        return 0

    if purge:
        if dry_run:
            print(
                f"memory clear (dry-run): would remove the entire store at {layout.as_posix()} "
                "(audit log and snapshots included)"
            )
            return 0
        if not yes and not _confirm_yes(
            f"PURGE the entire memory store at {layout.as_posix()} — audit log and snapshots "
            "included? This cannot be undone. Type YES to confirm: "
        ):
            print("memory clear: aborted")
            return 0
        shutil.rmtree(layout.root, ignore_errors=True)
        print(f"memory clear: purged the store at {layout.as_posix()}")
        return 0

    tiers = list(MemoryTier) if kind is None else [MemoryTier(kind)]
    scope = "all tiers" if kind is None else f"the {kind} tier"
    service = MemoryService(layout, config=config.memory)
    counts = service.tier_counts(tiers)
    total = sum(counts.values())
    if total == 0:
        print(f"memory clear: {scope} already empty — nothing to clear")
        return 0
    detail = _counts_line({tier.value: n for tier, n in counts.items()})
    if dry_run:
        print(
            f"memory clear (dry-run): would clear {total} record(s) from {scope} [{detail}]; "
            "a snapshot would be taken first (restore-able)"
        )
        return 0
    if not yes and not _confirm(
        f"Clear {total} record(s) from {scope} under {layout.as_posix()}? A snapshot is taken "
        "first, so this is reversible with 'worc memory restore'. [y/N] "
    ):
        print("memory clear: aborted")
        return 0
    report = service.clear(tiers=tiers, audit=_memory_audit_context(AuditActor.OPERATOR))
    cleared_detail = _counts_line({tier.value: n for tier, n in report.cleared.items()})
    print(f"memory clear: cleared {sum(report.cleared.values())} record(s) [{cleared_detail}]")
    if report.snapshot is not None:
        print(
            f"  snapshot: {report.snapshot.as_posix()} "
            f"(restore with: worc memory restore --snapshot {report.snapshot.name})"
        )
    return 0


def _counts_line(counts: dict[str, int]) -> str:
    return ", ".join(f"{name} {n}" for name, n in counts.items() if n)


def _snapshot_labels(layout: MemoryLayout) -> list[str]:
    """Snapshot dir names under ``audit/snapshots/``, sorted (timestamps sort chronologically)."""
    snaps = layout.snapshots
    if not snaps.is_dir():
        return []
    return sorted(p.name for p in snaps.iterdir() if p.is_dir())


def run_preflight(
    config: OrchestratorConfig, *, env_file: Path | None = None
) -> tuple[bool, list[str]]:
    """Compute the read-only preflight verdict + report lines; no task is processed.

    Runs every allowed provider's ``preflight()`` (``<cli> --version``) and the deterministic
    ``check_isolation`` policy check. Returns ``(ready, lines)`` where ``ready`` is true iff every
    provider is healthy and the required isolation can be enabled. Lines are secret-free by
    contract. Shared by ``cmd_preflight`` and the installer's post-write auto-preflight.
    ``env_file`` is the resolved ``.env`` path (already loaded at startup); its status is reported
    as a health line here — the only place the ``.env`` notice appears.
    """
    lines: list[str] = [_env_preflight_line(env_file)]
    providers = build_providers(config, artifacts_root=worc_home_for(config))
    ok = True
    for pid in config.agents.allowed:
        provider = providers.get(pid)
        if provider is None:
            lines.append(f"{pid.value}: FAIL — no provider adapter configured")
            ok = False
            continue
        health = provider.preflight()
        healthy = health.executable_found and health.supports_required_features
        ok = ok and healthy
        lines.append(
            f"{pid.value}: {'OK' if healthy else 'FAIL'} — {health.message} "
            f"(version={health.version or 'unknown'}, authenticated={health.authenticated})"
        )
        # Advisory degradations are fatal only when this provider has no fallback (it is the sole
        # allowed provider), else a warning — a fallback provider will cover the degraded nodes.
        has_fallback = any(other != pid for other in config.agents.allowed)
        for reason in health.degraded_reasons:
            if has_fallback:
                lines.append(f"{pid.value}: WARN — {reason} (a fallback provider will cover)")
            else:
                ok = False
                lines.append(f"{pid.value}: FAIL — {reason} (no fallback provider)")

    reasons = check_isolation(config)
    if reasons:
        ok = False
        lines.append("isolation: FAIL")
        lines.extend(f"  - {reason}" for reason in reasons)
    else:
        enforced = "enforced" if config.security.strict_isolation else "strict_isolation=false"
        lines.append(f"isolation: OK ({enforced})")

    lines.extend(_summarize_command_sets(config))

    # Every flow file — packaged built-ins and operator flows in ``.worc/flows/`` — must load and
    # pass the full fatal validator (graph + ceiling + config-consistency) before any task runs, so
    # a broken or unsafe operator flow is caught at install/preflight, not mid-run (P4.1).
    flow_registry = FlowRegistry(operator_flows_dir=worc_home_for(config) / "flows", config=config)
    for name, error in flow_registry.validate_all():
        if error is None:
            lines.append(f"flow {name}: OK")
        else:
            ok = False
            lines.append(f"flow {name}: FAIL — {error.splitlines()[0]}")
    # Non-fatal anti-drift lint: role prompts referencing an unknown ``{name}`` (a typo, or a
    # variable outside the flow-derived valid-set) render verbatim to the agent. Warn, never fail —
    # a verbatim render is the safe-renderer fallback (code/JSON braces must pass through).
    for name, messages in flow_registry.lint_all():
        for message in messages:
            lines.append(f"flow {name}: WARN — {message} (renders verbatim to the agent)")

    if config.git.create_pull_request:
        gh_ok, gh_line = preflight_gh()
        if not gh_ok:
            ok = False
        lines.append(gh_line)

    tg_ok, tg_line = check_telegram_preflight(config.telegram)
    if not tg_ok:
        ok = False
    lines.append(tg_line)

    lines.append(f"preflight: {'ready' if ok else 'NOT ready'}")
    return ok, lines


def cmd_preflight(args: argparse.Namespace) -> int:
    """Report each CLI's health and the strict_isolation verdict (read-only diagnostics)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    env_file, _ = resolve_env_file_path(args)
    ok, lines = run_preflight(config, env_file=env_file)
    for line in lines:
        print(line)
    return 0 if ok else 1


def cmd_telegram_test(args: argparse.Namespace) -> int:
    """Send a real question/reply round-trip without processing a task."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if args.timeout_seconds <= 0:
        print("error: --timeout-seconds must be > 0", file=sys.stderr)
        return 2
    if not config.telegram.enabled:
        print("telegram-test: FAIL (telegram.enabled is false)")
        return 1

    ok, line = check_telegram_preflight(config.telegram)
    print(line)
    if not ok:
        return 1

    notifier = build_notifier(config.telegram)
    result = notifier.ask_human(
        question="Reply to this message to confirm Telegram HITL is working.",
        context="This is an operator smoke test; no task or repository files will be changed.",
        task_id="telegram-test",
        kind="question",
        timeout_s=args.timeout_seconds,
        interaction_id="test-" + uuid.uuid4().hex[:24],
    )
    if result.failure is not None:
        print(f"telegram-test: FAIL ({result.failure})")
        return 1
    print("telegram-test: OK (correlated reply received)")
    return 0


def _summarize_watch(results: list[PipelineResult]) -> int:
    """Print one line per processed task and return the worst exit code (0 when nothing ran)."""
    if not results:
        print("watch: nothing to do (slot free, no pending tasks)")
        return 0
    for result in results:
        label = (
            "paused (provider unavailable)"
            if result.final_status is Status.RUNNING
            else result.final_status.value
        )
        print(f"{result.task_id}: {label}")
    return max(_EXIT_BY_STATUS.get(r.final_status, 1) for r in results)


def cmd_watch(args: argparse.Namespace) -> int:
    """Resume an in-flight task and process pending tasks (auto mode permitting).

    With ``poll_interval > 0`` (config ``orchestrator.poll_interval_seconds`` or ``--poll-seconds``)
    this runs as a daemon: each tick fetch/pulls ``base_branch`` to discover git-pushed tasks, then
    processes pending, then sleeps. ``0`` is a single pass. Stop the daemon with Ctrl-C, or from
    another shell with ``stop`` / ``restart``.

    The looping daemon writes ``<artifacts_root>/orchestrator.pid`` and shuts down gracefully
    between ticks when ``stop``/``restart`` ask it to: via a ``SIGTERM`` handler on POSIX and via an
    ``orchestrator.stop`` sentinel file it polls each tick (the cross-platform channel — ``SIGTERM``
    is undeliverable cross-process on Windows). It refuses to start when another watcher is already
    live for the same artifact root, and removes its PID file on exit (how ``stop`` confirms it).
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if config.git.create_pull_request:
        preflight.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish
        preflight.warn_if_gh_logged_out()  # non-blocking advisory if gh is present but logged out
    poll = (
        args.poll_seconds
        if args.poll_seconds is not None
        else config.orchestrator.poll_interval_seconds
    )
    folder = pending_dir(config)
    pid_path = process_control.pid_file_path(worc_home_for(config))
    stop_path = process_control.stop_file_path(worc_home_for(config))
    children_path = process_control.children_file_path(worc_home_for(config))
    cleanup_hook = _build_cleanup_hook(config)  # None when memory is disabled (Q10)

    # Single pass: no PID file, no signal handler, no stop wiring — and NO agent-handle recorder, so
    # it never clobbers a concurrent daemon's children file. A hung single-pass agent is still
    # reaped by run_process's own timeout/Ctrl-C subtree-kill.
    if poll <= 0:
        orchestrator = build_orchestrator(
            config,
            artifacts_root=worc_home_for(config),
            heartbeat_seconds=args.heartbeat_seconds,
        )
        return _summarize_watch(
            watch_loop(
                orchestrator,
                config,
                folder,
                poll_interval=poll,
                queue=args.queue,
                cleanup_hook=cleanup_hook,
            )
        )

    # Daemon: refuse a second watcher for the same artifact root. A stale PID file (process gone) is
    # overwritten on start.
    existing = process_control.running_daemon_pid(pid_path)
    if existing is not None:
        print(
            f"watch: already running (pid {existing}); stop it first with "
            f"'wastech-orchestrator stop', or use 'restart' ({pid_path})"
        )
        return 1

    controller = process_control.StopController()  # SIGTERM -> event, restored on exit
    # Record each launched agent's (pid, pgid) so a hard stop can reap its whole subtree, and tell
    # the Router a raised provider error is a stop-kill (never fall back to a fresh agent).
    recorder = agent_process.AgentHandleRecorder(
        on_spawn=lambda pid, pgid: process_control.write_children_file(
            children_path, pid=pid, pgid=pgid
        ),
        on_reap=lambda: process_control.clear_children_file(children_path),
    )
    orchestrator = build_orchestrator(
        config,
        artifacts_root=worc_home_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
        agent_handle_recorder=recorder,
        is_cancelled=lambda: (
            controller.event.is_set() or process_control.stop_file_requested(stop_path)
        ),
    )

    print(f"watch: polling every {poll}s for git-pushed tasks (Ctrl-C or 'stop' to exit)")
    results: list[PipelineResult] = []
    stopped = False
    try:
        with controller:
            # Lead our own process group (POSIX, best-effort) so `stop --force-full` can group-kill
            # the daemon without reaching an unrelated group. No-op if we already lead one
            # (foreground job control / console spawn) or on Windows.
            process_control.ensure_own_process_group()
            stop_path.unlink(missing_ok=True)  # clear a stale sentinel so it can't stop us on start
            process_control.clear_children_file(children_path)  # clear a stale handle from a crash
            process_control.write_pid_file(pid_path)
            results = watch_loop(
                orchestrator,
                config,
                folder,
                poll_interval=poll,
                queue=args.queue,
                stop_event=controller.event,
                stop_file=stop_path,
                cleanup_hook=cleanup_hook,
            )
            # Graceful stop arrived via SIGTERM (event) or the stop-file (Windows / cross-shell).
            stopped = controller.event.is_set() or process_control.stop_file_requested(stop_path)
    except KeyboardInterrupt:
        print("watch: stopped")
        return 0
    finally:
        # Reap the active agent's whole subtree before dropping the PID file — closes the main
        # orphan route (Ctrl-C / crash / clean exit). A soft stop lets the stage finish, so on_reap
        # already cleared the handle and this is a no-op; a --force-full from another shell already
        # reaped it.
        handle = process_control.read_children_record(children_path)
        if handle is not None:
            agent_process.kill_agent_subtree(handle.pid, handle.pgid)
        process_control.clear_children_file(children_path)
        pid_path.unlink(missing_ok=True)  # clean exit, Ctrl-C, SIGKILL-survivor, or error
        stop_path.unlink(missing_ok=True)  # reap our own sentinel
    if stopped:
        print("watch: stopped")  # graceful shutdown (SIGTERM or stop-file)
        return 0
    return _summarize_watch(results)


class _StopDecision(NamedTuple):
    """The stop ladder's verdict for one invocation (see :func:`_resolve_stop_level`)."""

    proceed: bool
    level: str = "soft"  # "soft" | "full" — passed to stop_process when proceeding
    message: str | None = None  # printed when not proceeding (refusal / abort)
    exit_code: int = 0  # exit code when not proceeding


def _resolve_stop_level(
    config: OrchestratorConfig, *, force: bool, force_full: bool, interactive: bool
) -> _StopDecision:
    """The stop ladder, keyed on whether a task is active (a read-only ``find_active_tasks`` probe).

    Idle → ordinary (soft) stop, no prompt, any form. Busy + no flag → refuse (interactive: confirm
    the literal ``YES`` → soft; non-interactive: exit non-zero, require a flag). Busy + ``--force``
    → soft; busy + ``--force-full`` → hard (full).
    """
    if not has_active_task(config):
        return _StopDecision(proceed=True, level="soft")  # nothing in flight: any form just stops
    if force_full:
        return _StopDecision(proceed=True, level="full")
    if force:
        return _StopDecision(proceed=True, level="soft")
    if interactive:
        if _confirm_yes(
            "a task is active. YES = soft stop (lets the current step finish, then exits); "
            "to interrupt the running agent NOW use --force-full: "
        ):
            return _StopDecision(proceed=True, level="soft")
        return _StopDecision(proceed=False, message="stop: aborted", exit_code=0)
    return _StopDecision(
        proceed=False,
        message=(
            "stop: a task is active; pass --force (soft: finishes the current step) or "
            "--force-full (hard: interrupts the running agent now and reaps its subtree)"
        ),
        exit_code=1,
    )


def _gated_stop(
    config: OrchestratorConfig, args: argparse.Namespace
) -> tuple[int, process_control.StopOutcome | None]:
    """Run the stop ladder. Returns ``(exit_code, outcome)``; ``outcome`` is ``None`` if refused."""
    decision = _resolve_stop_level(
        config,
        force=getattr(args, "force", False),
        force_full=getattr(args, "force_full", False),
        # --non-interactive forces the refuse-with-instructions path (no _confirm_yes/input()). The
        # console always passes it so a busy 'down'/'restart' never blocks on input() inside the
        # prompt_toolkit REPL (H1: the single-stdin-reader rule).
        interactive=sys.stdin.isatty() and not getattr(args, "non_interactive", False),
    )
    if not decision.proceed:
        print(decision.message)
        return decision.exit_code, None
    pid_path = process_control.pid_file_path(worc_home_for(config))
    stop_path = process_control.stop_file_path(worc_home_for(config))
    children_path = process_control.children_file_path(worc_home_for(config))
    outcome = process_control.stop_process(
        pid_path,
        timeout=args.timeout,
        stop_file=stop_path,
        children_file=children_path,
        level=decision.level,
        # POSIX hard rung: SIGKILL the daemon's group and reap the recorded agent's own subtree via
        # this seam (killpg + a descendant sweep). Windows: tree-kill the daemon (and the recorded
        # agent) via taskkill. Both injected seams keep process_control free of any direct
        # child-process launch (its no-shell-out rule).
        subtree_kill_fn=agent_process.kill_agent_subtree,
        hard_kill_fn=agent_process.hard_kill_tree,
    )
    return 0, outcome


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop a running ``watch`` daemon via the stop ladder (idle: no prompt; busy: confirm/force).

    Soft (default / ``--force`` / typed ``YES``) finishes the current step, then exits;
    ``--force-full`` interrupts the running agent now and reaps its whole subtree (POSIX: daemon
    group-kill + the recorded agent's group + a descendant sweep; Windows: ``taskkill /F /T``).
    Idempotent.
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    code, outcome = _gated_stop(config, args)
    if outcome is None:
        return code
    if outcome.degraded_to_soft:
        print("stop: hard stop (--force-full) is unavailable on Windows; doing a soft stop")
    if not outcome.found:
        print("stop: no running watcher (no PID file)")
    elif outcome.already_dead:
        print(f"stop: no running watcher (cleared stale PID {outcome.pid})")
    elif outcome.group_killed:
        print(
            f"stop: watcher {outcome.pid} hard-stopped (killed its process group); "
            "it resumes from its checkpoint on next start"
        )
    elif outcome.tree_killed:
        print(
            f"stop: watcher {outcome.pid} hard-stopped (killed its process tree); "
            "it resumes from its checkpoint on next start"
        )
    elif outcome.killed:
        print(f"stop: watcher {outcome.pid} did not exit in {args.timeout:g}s; sent SIGKILL")
    elif outcome.timed_out:
        print(
            f"stop: watcher {outcome.pid} did not confirm shutdown in {args.timeout:g}s; "
            "cleared its PID file (if it is still running, stop it via Task Manager)"
        )
    else:
        print(f"stop: watcher {outcome.pid} stopped")
    return 0


def cmd_restart(args: argparse.Namespace) -> int:
    """Stop the running watcher (via the stop ladder), then start a fresh ``watch`` with the flags.

    Targets the daemon recorded in the PID file (a different process), waits for it to exit, then
    runs its own loop in-process. A busy daemon confirms/forces exactly like ``stop``; a refusal
    does **not** start a new daemon.
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    code, outcome = _gated_stop(config, args)
    if outcome is None:
        return code  # refused (busy, no go-ahead) → do not start a new daemon
    if outcome.degraded_to_soft:
        print("restart: hard stop (--force-full) is unavailable on Windows; doing a soft stop")
    if not outcome.found or outcome.already_dead:
        print("restart: no previous watcher running")
    elif outcome.group_killed:
        print(f"restart: hard-stopped previous watcher {outcome.pid} (process group)")
    elif outcome.tree_killed:
        print(f"restart: hard-stopped previous watcher {outcome.pid} (process tree)")
    elif outcome.timed_out:
        print(f"restart: previous watcher {outcome.pid} did not confirm shutdown; starting anyway")
    else:
        suffix = " (SIGKILL)" if outcome.killed else ""
        print(f"restart: stopped previous watcher {outcome.pid}{suffix}")
    return cmd_watch(args)


def _summarize_command_sets(config: OrchestratorConfig) -> list[str]:
    """Read-only summary of the configured ``checks.command_sets`` (no resolution/probing/running).

    Shared by ``preflight`` and ``status``; an empty mapping is reported as "no quality gate".
    """
    sets = config.checks.command_sets
    if not sets:
        return ["checks: no command_sets configured (no quality gate)"]
    lines = [f"checks: {len(sets)} command set(s):"]
    for name, cset in sets.items():
        paths = ", ".join(cset.paths) if cset.paths else "always"
        cmds = "; ".join(" ".join(c.argv) for c in cset.commands)
        flags = []
        if cset.timeout_seconds is not None:
            flags.append(f"timeout={cset.timeout_seconds}s")
        if cset.skip_if_unavailable:
            flags.append("skip_if_unavailable")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        lines.append(f"  {name} (paths: {paths}){suffix}: {cmds}")
    return lines


def cmd_status(args: argparse.Namespace) -> int:
    """Show persisted progress without starting providers, checks, or git operations."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    db_path = Path(worc_home_for(config)) / "state.db"
    if not db_path.is_file():
        print(f"status: no state database at {db_path}")
        return 0

    store = StateStore.open_readonly(db_path)
    try:
        if args.task_id:
            task = store.get_task(args.task_id)
            tasks = [] if task is None else [task]
        else:
            tasks = store.find_active_tasks()
            if not tasks:
                latest = store.latest_task()
                tasks = [] if latest is None else [latest]
        current_nodes = {t.task_id: store.get_flow_checkpoint(t.task_id)[0] for t in tasks}
    finally:
        store.close()

    if not tasks:
        suffix = f" for task {args.task_id!r}" if args.task_id else ""
        print(f"status: no task found{suffix}")
        return 1 if args.task_id else 0

    now = datetime.now(UTC)
    for index, task in enumerate(tasks):
        if index:
            print()
        print(f"task_id={task.task_id}")
        print(f"title={task.title}")
        print(f"status={task.status.value}")
        node = current_nodes.get(task.task_id)
        if node:
            print(f"node={node}")  # the flow checkpoint: where the engine will resume
        if task.branch:
            print(f"branch={task.branch}")
        if task.active_subtask is not None and task.subtask_count is not None:
            print(f"subtask={task.active_subtask}/{task.subtask_count}")
        print(f"fix_iterations={task.fix_iterations}")
        if task.updated_at:
            print(f"updated_at={task.updated_at}")
            updated = datetime.fromisoformat(task.updated_at)
            elapsed = max(0.0, (now - updated).total_seconds())
            print(f"elapsed_since_update_seconds={elapsed:.1f}")
        if task.cleanup_last_error:
            print(f"last_error={task.cleanup_last_error}")

    # The configured check command sets, read-only (status never resolves, probes, or runs).
    print()
    for line in _summarize_command_sets(config):
        print(line)
    return 0


def _task_entry(row: TaskRow) -> dict[str, str | None]:
    # A RUNNING task with a blocked_since marker is parked on a provider outage (B-lite), not
    # actively executing — surface that so it does not read as a stuck/hung run.
    status = (
        f"{row.status.value} (paused)"
        if row.status is Status.RUNNING and row.blocked_since
        else row.status.value
    )
    return {
        "task_id": row.task_id,
        "status": status,
        "title": row.title,
        "branch": row.branch,
    }


def _pending_entry(path: Path, task_id: str | None) -> dict[str, str | None]:
    # A queued file has no DB row yet, so this view is file-derived; the id (if any) comes from the
    # cheap front-matter scan and an unparseable file is shown by filename instead.
    return {
        "task_id": task_id,
        "status": "pending",
        "title": None,
        "branch": None,
        "file": path.name,
    }


def _entry_line(entry: dict[str, str | None]) -> str:
    status = entry["status"] or ""
    label = entry["task_id"] or entry.get("file") or "(unknown)"
    line = f"{status:<22} {label}"
    title = entry.get("title")
    if title:
        line += f"  {title}"
    branch = entry.get("branch")
    if branch:
        line += f"  ({branch})"
    return line


# Reverse of task.model._PRIORITY_RANK for display (the scheduler keeps only the rank).
_PRIORITY_LABEL: dict[int, str] = {0: "high", 1: "mid", 2: "low"}


def tail_lines(path: Path | None, n: int) -> list[str]:
    """The last ``n`` lines of ``path`` (oldest first), or ``[]`` when absent/unreadable.

    Re-reads the whole current file each call: rotation-immune (no byte offsets) and cheap for an
    operator log. A burst between polls larger than the returned tail can be missed — acceptable for
    a monitor. Used by ``worc top`` / the console to stream the daemon ``--log-file``.
    """
    if path is None or n <= 0 or not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-n:]


class _ActiveView(NamedTuple):
    """One active (slot-owning) task, as the read-only monitor shows it."""

    task_id: str
    status_label: str  # "running" or "running (paused)" (B-lite park)
    title: str | None
    branch: str | None
    current_node: str | None  # flow checkpoint: where the engine will resume
    fix_iterations: int
    subtask: str | None  # "2/5" for a decomposed task, else None
    parked_since: str | None  # tasks.blocked_since (ISO) when parked, else None
    gate_pending: bool  # a durable HITL gate is waiting on the operator


class _QueueView(NamedTuple):
    """One pending file, in the exact order the daemon will run it."""

    label: str  # the front-matter id, or the filename when unparseable
    priority: str  # high / mid / low
    queue: str


class TopSnapshot(NamedTuple):
    """A single read-only frame for ``worc top`` / the console ``ps`` view.

    Pure data assembled by :func:`build_top_snapshot` from already-redacted sources; rendered by
    :func:`render_top`. No live handles — safe to build under a poll and assert on in tests.
    """

    db_present: bool
    selector: str  # the served queue: what the daemon picks + what the queue view is filtered to
    active: tuple[_ActiveView, ...]
    queue: tuple[_QueueView, ...]
    recent: tuple[dict[str, str | None], ...]  # reuses the _task_entry dicts
    log_path: str | None
    log_lines: tuple[str, ...]


def _has_pending_gate(worc_home: Path, task_id: str) -> bool:
    """True iff a durable HITL gate interaction is waiting on the operator (e.g. max-turns gate).

    Reads the durable ``hitl/*.json`` artifacts via :func:`iter_task_interactions`; the non-durable
    next-task gate has no artifact and is deliberately not surfaced. Fail-safe: a read/parse error
    yields ``False`` so the monitor never crashes on a malformed artifact.
    """
    try:
        return any(
            interaction.get("status") == "waiting"
            for interaction in iter_task_interactions(worc_home, task_id)
        )
    except (OSError, ValueError):  # StageOutputError (non-dict JSON) is a ValueError subclass
        return False


def build_top_snapshot(
    config: OrchestratorConfig,
    store: StateStore | None,
    *,
    selector: str,
    log_path: Path | None,
    log_tail_lines: int,
    recent_limit: int,
) -> TopSnapshot:
    """Assemble the read-only monitor view from already-redacted sources.

    Reads the active task(s) + flow checkpoint + parked marker + pending-gate marker from the
    read-only ``state.db``, the pending queue ranked exactly as the daemon runs it
    (:func:`scan_pending_sorted`, filtered to ``selector``), recent terminal tasks
    (``store.recent_tasks``), and a tail of the daemon log. Pure given ``store`` + ``log_path``;
    ``store`` is ``None`` when no database exists yet (fresh install), yielding empty task sections.
    """
    worc_home = worc_home_for(config)
    active: list[_ActiveView] = []
    for row in store.find_active_tasks() if store is not None else []:
        try:
            current_node = store.get_flow_checkpoint(row.task_id)[0] if store else None
        except KeyError:
            current_node = None  # row vanished between the two reads (terminal race)
        parked = row.blocked_since if (row.status is Status.RUNNING and row.blocked_since) else None
        subtask = (
            f"{row.active_subtask}/{row.subtask_count}"
            if row.active_subtask is not None and row.subtask_count is not None
            else None
        )
        active.append(
            _ActiveView(
                task_id=row.task_id,
                status_label=_task_entry(row)["status"] or row.status.value,
                title=row.title,
                branch=row.branch,
                current_node=current_node,
                fix_iterations=row.fix_iterations,
                subtask=subtask,
                parked_since=parked,
                gate_pending=_has_pending_gate(worc_home, row.task_id),
            )
        )
    queue = tuple(
        _QueueView(
            label=s.task_id or path.name,
            priority=_PRIORITY_LABEL.get(s.priority_rank, "mid"),
            queue=s.queue,
        )
        for path, s in scan_pending_sorted(pending_dir(config), selector)
    )
    recent = tuple(_task_entry(r) for r in (store.recent_tasks(recent_limit) if store else []))
    return TopSnapshot(
        db_present=store is not None,
        selector=selector,
        active=tuple(active),
        queue=queue,
        recent=recent,
        log_path=str(log_path) if log_path is not None else None,
        log_lines=tuple(tail_lines(log_path, log_tail_lines)),
    )


# `worc top` defaults: refresh cadence and how many daemon-log lines to tail per frame.
_TOP_DEFAULT_POLL_SECONDS = 2.0
_TOP_LOG_TAIL_LINES = 12


def render_top(snapshot: TopSnapshot) -> str:
    """Render one read-only monitor frame as plain text (pure; golden-tested).

    Mirrors what the daemon will actually do: the queue is already filtered to ``selector`` and
    priority-ordered by :func:`build_top_snapshot`. Carries no ANSI styling — the loop owns the
    screen clear; this is just text, so it is trivially asserted on in tests.
    """
    lines: list[str] = [
        f"worc top — queue {snapshot.selector!r}    (type q + Enter to quit)",
        "=" * 78,
        "ACTIVE",
    ]
    if not snapshot.db_present:
        lines.append("  (no state database yet)")
    elif not snapshot.active:
        lines.append("  (idle — no active task)")
    else:
        for view in snapshot.active:
            head = f"  {view.status_label:<18} {view.task_id}"
            if view.title:
                head += f"  {view.title}"
            lines.append(head)
            meta = [f"node={view.current_node}"] if view.current_node else []
            meta.append(f"fix={view.fix_iterations}")
            if view.subtask:
                meta.append(f"subtask={view.subtask}")
            if view.branch:
                meta.append(f"branch={view.branch}")
            lines.append("    " + "  ".join(meta))
            if view.parked_since:
                lines.append(f"    paused — every provider unavailable since {view.parked_since}")
            if view.gate_pending:
                lines.append("    awaiting operator (gate pending)")

    lines += ["", f"QUEUE ({snapshot.selector})"]
    if snapshot.queue:
        lines += [f"  {view.priority:<4}  {view.label}" for view in snapshot.queue]
    else:
        lines.append("  (empty)")

    lines += ["", "RECENT"]
    if snapshot.recent:
        lines += [f"  {_entry_line(entry)}" for entry in snapshot.recent]
    else:
        lines.append("  (none)")

    lines += ["", f"LOG ({snapshot.log_path or 'no --log-file'})"]
    if snapshot.log_lines:
        lines += [f"  {line}" for line in snapshot.log_lines]
    else:
        lines.append("  (no output)")

    return "\n".join(lines)


def _stdin_quit_watcher(stop_event: threading.Event, stream: TextIO | None = None) -> None:
    """Set ``stop_event`` when the operator types ``q``/``quit`` on stdin (blocking reader thread).

    A blocking ``readline`` is platform-neutral (no select / termios / msvcrt) and works whether
    stdin is a TTY or a pipe; EOF (closed stdin) ends the watcher without quitting, leaving Ctrl-C
    as the other exit. Costs an Enter after the key — a trade for zero platform branching.
    """
    source = stream if stream is not None else sys.stdin
    while not stop_event.is_set():
        line = source.readline()
        if line == "":  # EOF: stdin closed — stop watching, but let the loop keep refreshing
            return
        if line.strip().lower() in ("q", "quit"):
            stop_event.set()
            return


def _run_top_loop(
    config: OrchestratorConfig,
    *,
    selector: str,
    log_path: Path | None,
    poll_seconds: float,
    recent_limit: int,
    log_tail_lines: int,
    stop_event: threading.Event,
    out: TextIO = sys.stdout,
    clear: bool = True,
) -> int:
    """Refresh the read-only monitor until ``stop_event`` fires (``q`` or a signal).

    Re-opens ``state.db`` read-only each tick (so a database the daemon creates a tick later starts
    showing), renders a frame, then waits ``poll_seconds`` on the event. Pure given the seams: a
    pre-set ``stop_event`` renders exactly one frame and returns, which is how it is tested.
    """
    db_path = Path(worc_home_for(config)) / "state.db"
    while True:
        store = StateStore.open_readonly(db_path) if db_path.is_file() else None
        try:
            snapshot = build_top_snapshot(
                config,
                store,
                selector=selector,
                log_path=log_path,
                log_tail_lines=log_tail_lines,
                recent_limit=recent_limit,
            )
        finally:
            if store is not None:
                store.close()
        if clear:
            out.write("\x1b[H\x1b[2J")  # cursor home + clear screen
        out.write(render_top(snapshot) + "\n")
        out.flush()
        if stop_event.wait(poll_seconds):
            return 0


def cmd_top(args: argparse.Namespace) -> int:
    """Live, read-only monitor of the single slot: active task + node, the priority-ordered pending
    queue (filtered to the served queue), recent terminal tasks, and a tail of the daemon log.

    A client over the daemon — it never starts the engine; it polls ``state.db`` read-only and tails
    the ``--log-file`` the daemon was launched with. ``q``/``quit`` (or Ctrl-C) exits.
    """
    # Read-only UI: keep our own logging quiet (errors only, no file) so stray lines don't fight the
    # full-screen redraw — and never open the tailed --log-file for writing (that would race the
    # daemon's rotating handler). ``--log-file`` here names the daemon log to *tail*, not a sink.
    configure_logging(
        level=logging.ERROR, fmt=getattr(args, "log_format", "logfmt"), file_path=None
    )
    config = load_config_for(args)
    if config is None:
        return 2
    set_log_level(logging.ERROR)
    selector = args.queue or config.orchestrator.queue
    log_path = Path(args.tail_file) if args.tail_file else None
    recent_limit = args.recent if args.recent is not None else _LIST_RECENT_DEFAULT
    poll = float(args.poll_seconds) if args.poll_seconds is not None else _TOP_DEFAULT_POLL_SECONDS
    stop_event = threading.Event()
    watcher = threading.Thread(target=_stdin_quit_watcher, args=(stop_event,), daemon=True)
    watcher.start()
    try:
        return _run_top_loop(
            config,
            selector=selector,
            log_path=log_path,
            poll_seconds=poll,
            recent_limit=recent_limit,
            log_tail_lines=_TOP_LOG_TAIL_LINES,
            stop_event=stop_event,
        )
    except KeyboardInterrupt:
        return 0


def cmd_shell(args: argparse.Namespace) -> int:
    """Interactive operator console: a client over the watch daemon (needs the ``[shell]`` extra).

    Spawns or attaches to a ``watch`` daemon, streams its log above a prompt, and dispatches console
    commands onto the existing ``cmd_*`` verbs — it never starts the engine itself. prompt_toolkit
    is imported lazily inside the console; without the extra it exits with an install hint.
    """
    # The console owns the screen, like `top`: keep our own logging quiet so stray lines don't fight
    # the prompt. The forwarded cmd_* verbs print their own output (above the prompt).
    configure_logging(
        level=logging.ERROR, fmt=getattr(args, "log_format", "logfmt"), file_path=None
    )
    config = load_config_for(args)
    if config is None:
        return 2
    set_log_level(logging.ERROR)
    cfg_path = resolve_config_path(args)
    from wastech_orchestrator import cli_shell  # lazy: avoids a circular import at module load

    return cli_shell.run_shell(
        config,
        config_path=str(cfg_path) if cfg_path else None,
        queue=args.queue,
        log_file=args.tail_file,
    )


def _list_sections(
    args: argparse.Namespace, config: OrchestratorConfig, store: StateStore | None
) -> list[tuple[str, list[dict[str, str | None]]]]:
    """The (section name, entries) groups for the table/json views, per the focus flags."""
    pending = [
        _pending_entry(path, _scan_pending_meta(path).task_id)
        for path in select_pending(pending_dir(config))
    ]
    if args.all:
        rows = store.all_tasks() if store else []
        return [("all", [_task_entry(r) for r in rows])]
    if args.pending:
        return [("pending", pending)]
    if args.recent is not None:
        rows = store.recent_tasks(args.recent) if store else []
        return [("recent", [_task_entry(r) for r in rows])]
    active = store.find_active_tasks() if store else []
    recent = store.recent_tasks(_LIST_RECENT_DEFAULT) if store else []
    return [
        ("active", [_task_entry(r) for r in active]),
        ("pending", pending),
        ("recent", [_task_entry(r) for r in recent]),
    ]


def _list_ids(store: StateStore | None, scope: str | None) -> int:
    """Print bare task ids (one per line, stdout) for completion/scripting.

    DB-derived: these are the ids the id-taking verbs accept. ``--scope rerun`` keeps only
    rerun-eligible terminal tasks; ``status`` and ``finalize`` both yield every known id —
    ``finalize`` is status-agnostic (``plan_finalize`` refuses only on a dirty tree or an existing
    manual ledger record, never on status), so it coincides with ``status`` today. Both stay as
    distinct, command-aligned scope values so the completion script can pass ``--scope <command>``
    verbatim and the rule can diverge here later without touching the shell.
    """
    if store is None:
        return 0
    rows = store.all_tasks()
    if scope == "rerun":
        rows = [r for r in rows if r.status in (Status.FAILED, Status.MANUAL_ACTION_REQUIRED)]
    for task_id in sorted({r.task_id for r in rows}):
        print(task_id)
    return 0


def _print_section_ids(sections: list[tuple[str, list[dict[str, str | None]]]]) -> int:
    """Print the bare ids of the focused sections (F4): the same disk+DB source as the table view,
    so `--pending --format ids` lists queued tasks that have no DB row yet. An unparseable pending
    file has no id and is skipped (there is no usable id to print)."""
    ids = {tid for _, items in sections for e in items if (tid := e.get("task_id"))}
    for task_id in sorted(ids):
        print(task_id)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """Enumerate tasks read-only: the active task, the ``tasks/pending`` queue, and recent terminal
    tasks. The default view shows all three; ``--pending`` / ``--recent [N]`` / ``--all`` focus it.
    ``--format ids`` prints bare ids (the completion/scripting source) and ``--scope`` filters them
    to what a given command accepts. Opens the DB read-only (``status``'s path) and never mutates.
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2

    # --scope is completion-facing and only meaningful as an id list.
    fmt = "ids" if args.scope else args.format
    db_path = Path(worc_home_for(config)) / "state.db"
    store = StateStore.open_readonly(db_path) if db_path.is_file() else None
    # F4: when a section focus flag is combined with `--format ids`, derive the ids from the same
    # source as the table view (disk pending files + DB) instead of DB-only — a freshly-queued
    # pending task has no DB row yet, so the DB-only path printed nothing. `--scope` stays
    # DB-derived (it is completion-facing, about rerun/status eligibility, which is a DB property).
    section_focus = bool(args.all or args.pending or args.recent is not None)
    try:
        if fmt == "ids":
            if args.scope is None and section_focus:
                return _print_section_ids(_list_sections(args, config, store))
            return _list_ids(store, args.scope)
        sections = _list_sections(args, config, store)
    finally:
        if store is not None:
            store.close()

    if fmt == "json":
        entries = [entry for _, items in sections for entry in items]
        print(json.dumps(entries, indent=2))
        return 0

    if not any(items for _, items in sections):
        print("list: no tasks")
        return 0
    for index, (name, items) in enumerate(sections):
        if index:
            print()
        print(f"{name}:")
        if items:
            for entry in items:
                print(f"  {_entry_line(entry)}")
        else:
            print("  (none)")
    return 0


def _parser_surface() -> tuple[list[str], dict[str, list[str]]]:
    """The completion surface read off the live parser: subcommand names + each one's flags.

    Sourced from :func:`build_parser` so the emitted completion script never drifts from the CLI.
    """
    parser = build_parser()
    commands: list[str] = []
    flags: dict[str, list[str]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            choices = action.choices
            if isinstance(choices, dict):
                for name, subparser in choices.items():
                    commands.append(name)
                    flags[name] = sorted(
                        {opt for a in subparser._actions for opt in a.option_strings}
                    )
            break
    return commands, flags


def _flag_cases(commands: list[str], flags: dict[str, list[str]]) -> str:
    """The shell ``case`` arms mapping each subcommand to its flags (shared by bash and zsh)."""
    return "\n".join(
        f'            {name}) flags="{" ".join(flags[name])}";;'
        for name in commands
        if flags.get(name)
    )


# The dynamic id completion shells out to `worc list` so the enumeration rule lives in one place
# (the three id-taking verbs each pass their own name as --scope). Task ids match
# ^[a-z0-9][a-z0-9._-]{0,63}$ (no shell metacharacters), so feeding the output through
# compgen/compadd carries no injection risk.
_BASH_COMPLETION = """\
_worc_ids() { compgen -W "$(worc list --format ids --scope "$1" 2>/dev/null)" -- "$2"; }
_worc() {
    local cur cmd i
    cur="${COMP_WORDS[COMP_CWORD]}"
    COMPREPLY=()
    cmd=""
    for (( i=1; i < COMP_CWORD; i++ )); do
        case "${COMP_WORDS[i]}" in
            -*) ;;
            *) cmd="${COMP_WORDS[i]}"; break ;;
        esac
    done
    if [[ -z "$cmd" ]]; then
        COMPREPLY=( $(compgen -W "__SUBCOMMANDS__" -- "$cur") )
        return
    fi
    if [[ "$cur" == -* ]]; then
        local flags=""
        case "$cmd" in
__FLAG_CASES__
        esac
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
        return
    fi
    case "$cmd" in
        rerun|status|finalize) COMPREPLY=( $(_worc_ids "$cmd" "$cur") );;
        run)                   COMPREPLY=( $(compgen -f -- "$cur") );;
    esac
}
complete -F _worc worc wastech-orchestrator
"""

_ZSH_COMPLETION = """\
#compdef worc wastech-orchestrator
_worc_ids() { compadd -- ${(f)"$(worc list --format ids --scope "$1" 2>/dev/null)"}; }
_worc() {
    local cmd i
    cmd=""
    for (( i=2; i < CURRENT; i++ )); do
        case "${words[i]}" in
            -*) ;;
            *) cmd="${words[i]}"; break ;;
        esac
    done
    if [[ -z "$cmd" ]]; then
        local -a subcommands
        subcommands=(__SUBCOMMANDS__)
        _describe 'command' subcommands
        return
    fi
    if [[ "${words[CURRENT]}" == -* ]]; then
        local flags=""
        case "$cmd" in
__FLAG_CASES__
        esac
        compadd -- ${=flags}
        return
    fi
    case "$cmd" in
        rerun|status|finalize) _worc_ids "$cmd" ;;
        run)                   _files ;;
    esac
}
# Register on `source <(worc completion zsh)`; run directly when invoked as the completer.
if [[ "${funcstack[1]}" == "_worc" ]]; then
    _worc "$@"
else
    compdef _worc worc wastech-orchestrator
fi
"""


def cmd_completion(args: argparse.Namespace) -> int:
    """Print a bash/zsh completion script (no config needed) to stdout.

    Subcommand names + per-command flags are baked in statically from the live parser; the task-id
    positionals (``status`` / ``rerun`` / ``finalize``) complete dynamically by shelling out to
    ``worc list --format ids --scope <command>``, and ``run`` completes task files. Wiring is a
    single ``source <(worc completion <shell>)``.
    """
    commands, flags = _parser_surface()
    template = _BASH_COMPLETION if args.shell == "bash" else _ZSH_COMPLETION
    script = template.replace("__SUBCOMMANDS__", " ".join(commands)).replace(
        "__FLAG_CASES__", _flag_cases(commands, flags)
    )
    print(script, end="")
    return 0


def _install_atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + ``os.replace``).

    The temp name is derived from the target (``.<stem>-…<suffix>``) so a ``.md`` guide does not get
    a ``.config-*.yaml``-named sibling (this helper serves config and ``upgrade-docs`` alike).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix)
    try:
        # newline="" disables newline translation so installed/templated files stay LF on every OS
        # (the packaged sources are LF; otherwise Windows would rewrite them with CRLF).
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _install_backup_config(path: Path) -> Path:
    """Copy an existing config to a timestamped ``.bak-<UTC>`` sibling and return that path."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    backup.write_bytes(path.read_bytes())
    return backup


def _install_create_dirs(repo_local_path: Path) -> None:
    """Create the tracked task dirs at the repo root and the gitignored ``.worc/`` runtime dirs.

    Idempotent. The repo task dirs are created empty, so they do not appear in ``git status`` until
    a task writes into them; everything under ``.worc/`` is gitignored as a whole.
    """
    worc_home = repo_local_path / WORC_HOME
    for rel in REPO_TASK_DIRS:
        (repo_local_path / rel).mkdir(parents=True, exist_ok=True)
    for rel in WORC_RUNTIME_DIRS:
        (worc_home / rel).mkdir(parents=True, exist_ok=True)


def _install_write_env_example(worc_home: Path) -> bool:
    """Write ``.worc/.env.example`` (a commented, value-free template). Never clobbers an existing
    one — the operator's real ``.env`` (and any edits to the example) are preserved. Returns whether
    a file was written."""
    target = worc_home / ENV_EXAMPLE_FILENAME
    if target.exists():
        return False
    target.write_text(_ENV_EXAMPLE_TEMPLATE, encoding="utf-8")
    return True


def _install_print_plan(
    spec: config_writer.InstallSpec, config_path: Path, missing: tuple[ProviderId, ...]
) -> None:
    """Print what ``install`` would do, writing nothing (``--dry-run``)."""
    worc_home = config_path.parent
    print("install (dry-run): no changes written")
    print(f"  repo:       {spec.repo_local_path}")
    print(f"  origin:     {spec.repo_url}")
    print(f"  base:       {spec.base_branch}")
    print(f"  config:     {config_path}")
    print(f"  providers:  {', '.join(p.value for p in spec.providers)}")
    print("  checks:     command_sets (author in config.yaml)")
    print(f"  create_pr:  {spec.create_pull_request}")
    print(f"  auto_mode:  {spec.auto_mode}")
    for rel in REPO_TASK_DIRS:
        print(f"  would create {spec.repo_local_path / rel}")
    for rel in WORC_RUNTIME_DIRS:
        print(f"  would create {worc_home / rel}")
    print(f"  would create {worc_home / 'guide'}/ (agent task-authoring docs)")
    print(f"  would create {worc_home / 'flows'}/ (built-in flows + node prompt templates)")
    print(f"  would create {worc_home / ENV_EXAMPLE_FILENAME} (secrets template)")
    print(f"  would ignore {WORC_HOME}/ via .gitignore")
    if missing:
        print(f"  note: provider(s) not on PATH: {', '.join(p.value for p in missing)}")


def _install_run_preflight(config_path: Path, *, skip: bool) -> int:
    """Auto-run preflight after writing config; on failure keep config but exit non-zero."""
    if skip:
        return 0
    ok, lines = run_preflight(_load_config(str(config_path)), env_file=config_path.parent / ".env")
    for line in lines:
        print(line)
    if not ok:
        print(
            "install: preflight is NOT ready — the config was written; resolve the items above, "
            "then run 'wastech-orchestrator preflight'."
        )
        return 1
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Set up the orchestrator in the current repo under ``.worc/`` and generate config.

    Runs the wizard to resolve settings, then idempotently writes a validated ``config.yaml`` into
    ``<repo>/.worc/``, scaffolds the runtime + task dirs, copies the task-authoring guide and
    editable copies of the built-in flows + their per-node prompt templates into ``.worc/flows/``,
    and gitignores ``.worc/``. Re-running is a no-op unless ``--reconfigure`` (which backs up and
    regenerates). After a successful write it auto-runs preflight.
    """
    _configure_runtime_logging(args)
    try:
        outcome = wizard.run_wizard(
            repo_path=Path(args.repo_path),
            provider=args.provider,
            create_pr=args.create_pr,
            auto_mode=args.auto_mode,
            non_interactive=args.non_interactive,
            prompter=wizard.ConsolePrompter(),
        )
    except wizard.InstallError as exc:
        print(f"install: {exc}")
        return 1

    spec = outcome.spec
    worc_home = (spec.repo_local_path / WORC_HOME).resolve()
    config_path = worc_home / "config.yaml"

    if args.dry_run:
        _install_print_plan(spec, config_path, outcome.missing_providers)
        return 0

    if config_path.exists():
        if not args.reconfigure:
            print(f"install: already configured at {config_path} (use --reconfigure to redo)")
            return _install_run_preflight(config_path, skip=args.skip_preflight)
        print(f"install: backed up existing config to {_install_backup_config(config_path)}")

    text = config_writer.build_and_validate(spec)
    _install_create_dirs(spec.repo_local_path)
    _install_atomic_write(config_path, text)
    print(f"install: wrote {config_path}")
    # An editable copy of the task-authoring guide lives in .worc/. --reconfigure refreshes it to
    # the packaged version; a plain re-run leaves existing files.
    worc_written, _ = _copy_worc_docs(worc_home, overwrite=args.reconfigure, dry=False)
    if worc_written:
        print(f"install: wrote agent task-authoring docs to {worc_home / 'guide'}")
    # Editable, active copies of the built-in flows + their per-node prompt templates land in
    # .worc/flows/ (these override the packaged built-ins). A plain re-run only fills in missing
    # files; --reconfigure snapshots the existing dir first, then refreshes to the packaged version.
    if args.reconfigure:
        flows_backup = _backup_flows_dir(worc_home)
        if flows_backup is not None:
            print(f"install: backed up existing flows to {flows_backup}")
    flows_written, _ = _copy_packaged_flows(worc_home, overwrite=args.reconfigure, dry=False)
    if flows_written:
        print(f"install: wrote built-in flows + node prompts to {worc_home / 'flows'}")
    # The executables that packaged `tool` nodes resolve against (e.g. the content-flow prose gate
    # `check_journey`) land in .worc/tools/, delivered per machine so the launcher matches the OS.
    # --reconfigure snapshots the existing dir first; a plain re-run only fills in missing files.
    if args.reconfigure:
        tools_backup = _backup_tools_dir(worc_home)
        if tools_backup is not None:
            print(f"install: backed up existing tools to {tools_backup}")
    tools_written, _ = _copy_packaged_tools(worc_home, overwrite=args.reconfigure, dry=False)
    if tools_written:
        print(f"install: wrote packaged tools to {worc_home / 'tools'}")
    # A commented reference copy beside the executable config.yaml — never read at runtime.
    # --reconfigure refreshes it; a plain re-run leaves an existing copy in place.
    if _install_write_config_example(worc_home, overwrite=args.reconfigure):
        print(f"install: wrote {worc_home / 'config.example.yaml'} (commented reference)")
    if _install_write_env_example(worc_home):
        print(f"install: wrote {worc_home / ENV_EXAMPLE_FILENAME} (copy to .worc/.env, fill in)")
    # Gitignore the whole .worc/ runtime home so the operator's `git status` stays clean.
    if append_runtime_excludes(spec.repo_local_path):
        print(f"install: ignored {WORC_HOME}/ via .gitignore")
    if outcome.missing_providers:
        names = ", ".join(p.value for p in outcome.missing_providers)
        print(f"install: note — selected provider(s) not on PATH yet: {names}")
    return _install_run_preflight(config_path, skip=args.skip_preflight)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.heartbeat_seconds < 0:
        parser.error("--heartbeat-seconds must be >= 0")
    if getattr(args, "poll_seconds", None) is not None and args.poll_seconds < 0:
        parser.error("--poll-seconds must be >= 0")
    if getattr(args, "timeout", None) is not None and args.timeout < 0:
        parser.error("--timeout must be >= 0")

    # A config/DB written by a newer orchestrator is refused with a clean message + exit 2 here,
    # rather than surfacing as a traceback (fail loud, not ugly). See the versioning gates. The
    # .env is loaded first (inside the try) so a bad --env-file also exits 2 cleanly.
    try:
        _load_env_file_for(args)
        if args.command == "install":
            return cmd_install(args)
        if args.command == "run":
            return cmd_run(args)
        if args.command == "watch":
            return cmd_watch(args)
        if args.command == "stop":
            return cmd_stop(args)
        if args.command == "restart":
            return cmd_restart(args)
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "telegram-test":
            return cmd_telegram_test(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "top":
            return cmd_top(args)
        if args.command == "shell":
            return cmd_shell(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "completion":
            return cmd_completion(args)
        if args.command == "upgrade-config":
            return cmd_upgrade_config(args)
        if args.command == "upgrade-docs":
            return cmd_upgrade_docs(args)
        if args.command == "rerun":
            return cmd_rerun(args)
        if args.command == "finalize":
            return cmd_finalize(args)
        if args.command == "prs":
            return cmd_prs(args)
        if args.command == "merge-task":
            return cmd_merge_task(args)
        if args.command == "tasks":
            return cmd_tasks(args)
        if args.command == "logs":
            return cmd_logs(args)
        if args.command == "memory":
            return cmd_memory(args)
    except (ConfigError, IncompatibleStateError, preflight.GhNotAvailableError) as exc:
        print(f"error: {exc}")
        return 2
    raise SystemExit(f"Unknown command '{args.command}'.")


if __name__ == "__main__":
    sys.exit(main())
