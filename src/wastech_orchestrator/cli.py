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
import re
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

from wastech_orchestrator import __version__, preflight, process_control, runs_retention
from wastech_orchestrator.composition import (
    HOST_FLOOR_CHECKS,
    ISOLATION_CHECKS,
    build_internal_deny_policy,
    build_orchestrator,
    build_providers,
)
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
from wastech_orchestrator.core.loop_control import ExhaustedLoop
from wastech_orchestrator.core.orchestrator import (
    Eligibility,
    FinalizePlan,
    MergePlan,
    Orchestrator,
    PipelineFailed,
    PipelineResult,
    RerunPlan,
)
from wastech_orchestrator.core.state_machine import TERMINAL, Status
from wastech_orchestrator.env_file import count_env_file, load_env_file
from wastech_orchestrator.git_manager import (
    KIND_PR,
    GitCommandError,
    GitManager,
    ManualActionRequired,
    append_runtime_excludes,
    ensure_path_excluded,
    gh_repo_pin,
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
from wastech_orchestrator.providers._adapter_base import IsolationCapabilityReport
from wastech_orchestrator.providers.base import AuthProbe, AuthState, ProviderId
from wastech_orchestrator.providers.claude import claude_config_home
from wastech_orchestrator.providers.codex import codex_config_home
from wastech_orchestrator.runtime_layout import CONTROL_HOME_DIRNAME, RuntimeLayout, runs_root
from wastech_orchestrator.security.env import (
    describe_expansions,
    expand_allowed_environment,
    launch_critical_env_issue,
)
from wastech_orchestrator.security.env_paths import (
    assigned_path_elements,
    canonical_collision,
    denied_read_path_collision,
    host_protected_paths,
    is_inside,
)
from wastech_orchestrator.security.isolation import (
    check_isolation,
    describe_advanced_mode,
    describe_host_floor,
)
from wastech_orchestrator.security.launchers import Which, resolve_launcher
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

# The orchestrator's runtime home inside the target repo is named by `runtime_layout`
# (`CONTROL_HOME_DIRNAME` / `PRIVATE_HOME_DIRNAME`, both `.worc` today). Everything the orchestrator
# generates or installs lives under `<repo>/.worc/` — gitignored as a whole — except the audit
# trail: the task lifecycle dirs below sit at the repo root and are audit-committed. Consumers reach
# the home via `layout_for(config)` (private) / `.control_home`, never a rebuilt `.worc` literal.

# Task lifecycle dirs created at the repo root by `install` (tracked; the audit commit captures the
# task file + its `<id>.summary.md` in done/failed). `tasks/rejected` is the quarantine and
# lives under `.worc/` instead, so rejected tasks are never swept into the audit commit.
# `tasks/preparing` is the staging area: the watch scanner never looks in it, so a task file can be
# composed there without being picked up mid-write. `promote` moves a finished file into `pending`.
# These are the install-time *default* layout (`paths.tasks_dir` defaults to "tasks"); the runtime
# reads `config.paths.tasks_dir` (see `pending_dir`). An operator who configures a different
# directory creates its lifecycle subfolders themselves.
REPO_TASK_DIRS: tuple[str, ...] = (
    "tasks/preparing",
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

# Add any other variables the orchestrator process needs. In strict mode, an exact
# security.allowed_environment entry may forward one to an agent child; a prefix match alone does
# not. In advanced mode agent children withhold every name loaded here. Use non-secret
# security.extra_environment assignments for an intentional agent-side value. Orchestrator git/gh
# keeps the allowlist in both modes and also scrubs names that could retarget publication.
# GH_TOKEN=
"""


def _add_stop_force_flags(parser: argparse.ArgumentParser) -> None:
    """The stop-ladder force flags shared by ``stop`` and ``restart`` (mutually exclusive).

    No flag → idle stops with no prompt; a busy daemon refuses (interactive: confirm ``YES``).
    ``--force`` → soft stop at the next flow-node boundary (never escalates; a timeout leaves the
    stop pending). ``--force-full`` → hard stop **whether or not a task is active**: kill the
    daemon's process tree/group now. See ``_resolve_stop_level``.
    """
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--force",
        action="store_true",
        help="stop even while a task is active: soft (finish the current flow node, then exit)",
    )
    group.add_argument(
        "--force-full",
        dest="force_full",
        action="store_true",
        help="hard stop now, idle or busy: kill the daemon and any active agent (POSIX groups / "
        "Windows tree). The rung for a wedged or suspended watcher a soft stop cannot reach",
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

    run_cmd = sub.add_parser(
        "run", help="run one task from its file (give a path to the .md/.json, NOT a task id)"
    )
    run_cmd.add_argument(
        "task_file",
        metavar="PATH",
        help="filesystem path to the task file (.md or .json), e.g. tasks/pending/my-task.md — "
        "not a task id (unlike `rerun`/`status`/`finalize`, which take an id)",
    )

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

    promote_cmd = sub.add_parser(
        "promote",
        help="promote a staged task file from tasks/preparing/ into tasks/pending/ (atomic move)",
    )
    promote_cmd.add_argument(
        "target",
        nargs="?",
        metavar="ID_OR_FILE",
        help="task id or file name to promote (a decomposition root pulls its subtask specs too); "
        "omit and pass --all to promote everything staged",
    )
    promote_cmd.add_argument(
        "--all",
        dest="all_files",
        action="store_true",
        help="promote every staged top-level task plus the whole subtasks/ subfolder",
    )

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

    preflight_cmd = sub.add_parser(
        "preflight",
        help="check both CLIs' health and the strict_isolation policy (runs no task)",
    )
    preflight_cmd.add_argument(
        "--paid-isolation-probe",
        action="store_true",
        help="additionally spend ONE real model call per provider that supports it, letting an "
        "agent try to write into .git and the control home; the verdict is read from the "
        "filesystem (Claude has no no-model way to prove this)",
    )
    validate_flow_cmd = sub.add_parser(
        "validate-flow",
        help="validate operator flow(s) in .worc/flows/ (config-aware, read-only)",
    )
    validate_flow_cmd.add_argument(
        "name",
        nargs="?",
        help="flow to validate — a bare stem or NAME.yaml resolved within .worc/flows/ "
        "(omit and pass --all to validate every flow)",
    )
    validate_flow_cmd.add_argument(
        "--all",
        dest="all_flows",
        action="store_true",
        help="validate every *.yaml in .worc/flows/",
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

    sub.add_parser("clear", help="clear the terminal screen (and scrollback); no files are deleted")

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
    reset_fix_budget_group = rerun_cmd.add_mutually_exclusive_group()
    reset_fix_budget_group.add_argument(
        "--reset-fix-budget",
        dest="reset_fix_budget",
        action="store_const",
        const=True,
        default=None,
        help="(--continue only) reset the consecutive fix-loop counters so an exhausted fix "
        "budget runs again; the global max_total_fix_iterations backstop is unchanged. Also "
        "answers the exhausted-fix-budget confirmation prompt with yes, non-interactively",
    )
    reset_fix_budget_group.add_argument(
        "--no-reset-fix-budget",
        dest="reset_fix_budget",
        action="store_const",
        const=False,
        help="(--continue only) decline to reset an exhausted fix budget; refuses the resume "
        "instead of prompting, non-interactively",
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
    rerun_cmd.add_argument(
        "--non-interactive",
        dest="non_interactive",
        action="store_true",
        help="never prompt: any confirmation that isn't already resolved by --yes/"
        "--reset-fix-budget/--no-reset-fix-budget is refused (exit 1) instead of asked. Used by "
        "scripts/CI and by 'worc shell' (a prompt would fight the REPL's stdin)",
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
        help="sweep .worc/logs/: task artifact dirs + the daemon logs (the ledger is kept by "
        "default). Refuses while a task is active; keeps the daemon logs while a daemon runs",
    )
    logs_clean.add_argument(
        "--keep",
        type=int,
        metavar="N",
        help="keep the N most recently modified task dirs, remove the rest (no prompt unless N=0); "
        "the daemon logs are removed either way",
    )
    logs_clean.add_argument(
        "--all",
        action="store_true",
        help="also remove the ledger (completed.jsonl); combines with --keep N",
    )
    logs_clean.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

    runs_cmd = sub.add_parser(
        "runs", help="manage per-task runtime state under .worc/runs/ (frozen bundles + seals)"
    )
    runs_sub = runs_cmd.add_subparsers(dest="runs_action", required=True)
    runs_clean = runs_sub.add_parser(
        "clean",
        help="remove per-task frozen bundles and sealed exchanges under .worc/runs/. Refuses while "
        "a task is active; quarantined evidence is kept unless --include-quarantine",
    )
    runs_clean.add_argument(
        "--keep",
        type=int,
        metavar="N",
        help="keep the N most recently touched tasks, remove the rest (no prompt unless N=0)",
    )
    runs_clean.add_argument(
        "--include-quarantine",
        dest="include_quarantine",
        action="store_true",
        help="also remove quarantined exchange evidence (written only when mutation detection "
        "caught an agent-side write to the read-only exchange — read it before deleting it)",
    )
    runs_clean.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")

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
    """Every packaged built-in flow and its per-node role-prompt templates, under each flow's own
    subdir (works from a source tree or a wheel).

    Deliberately *not* enumerated here: the shipped set is whatever ``packaged/flows/*.yaml``
    contains, and a name list in a docstring only drifts behind it (it already had). Read the
    directory for the current set.

    ``install`` copies this whole tree into ``.worc/flows/`` so the operator gets editable, *active*
    copies — which is the only reason a built-in is runnable at all: the registry resolves
    ``<repo>/.worc/flows/<task_type>.yaml`` and nothing else, so this tree is delivery-only and is
    never read at task-execution time (an unresolved ``task_type`` is an error, not a fall-back to
    the bundled copy). A node's ``role_file`` resolves under its flow-owned dir
    ``.worc/flows/<task_type>/``. Unlike the generated ``guide/``, these are operator-editable, so a
    plain re-run never clobbers them (see ``_copy_packaged_flows`` / ``_backup_flows_dir``).
    """
    return resources.files("wastech_orchestrator").joinpath("packaged", "flows")


# How many of the orchestrator's own `--reconfigure` snapshots survive per kind. What the
# orchestrator writes, it also reclaims: `flows.bak-*` / `tools.bak-*` are whole-directory copies
# taken on every refresh, so an unbounded series is the one that actually costs disk. Three is
# enough to undo a bad refresh and still notice it a few runs later. Not a config knob.
_INSTALL_BACKUP_KEEP = 3

# The exact shape `_install_backup_config` / `_backup_flows_dir` / `_backup_tools_dir` stamp:
# `%Y%m%dT%H%M%SZ`. Matching the stamp rather than a bare `bak-*` keeps the prune to what the
# orchestrator itself wrote — an operator's hand-named `config.yaml.bak-before-upgrade` (and
# anything under `state.db*.bak*`) is not ours to delete.
_INSTALL_BACKUP_STAMP_GLOB = "????????T??????Z"


def _prune_install_backups(worc_home: Path, prefix: str) -> None:
    """Keep only the newest :data:`_INSTALL_BACKUP_KEEP` ``<prefix>.bak-<UTC>`` snapshots.

    The UTC stamp is fixed-width, so the name sorts chronologically without a single ``stat`` — and
    ordering by name means the prune matches the order the operator sees the directory listed in.
    Files and whole-directory snapshots are both handled; a failure to remove one is ignored, since
    a locked stale backup must never turn an ``install --reconfigure`` into an error.
    """
    pattern = f"{prefix}.bak-{_INSTALL_BACKUP_STAMP_GLOB}"
    for stale in sorted(worc_home.glob(pattern), reverse=True)[_INSTALL_BACKUP_KEEP:]:
        if stale.is_dir():
            shutil.rmtree(stale, ignore_errors=True)
        else:
            stale.unlink(missing_ok=True)


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
    ``.worc/`` home, so it never shows up in ``git status``, and only the newest few are kept.
    """
    flows = worc_home / "flows"
    if not flows.is_dir() or not any(flows.iterdir()):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = flows.with_name(f"flows.bak-{stamp}")
    shutil.copytree(flows, backup)
    _prune_install_backups(worc_home, "flows")
    return backup


def _tools_root() -> Traversable:
    """The packaged operator tools delivered into ``.worc/tools/`` (works from a source tree or a
    wheel).

    ``install`` copies this tree so a packaged flow's ``tool`` node resolves against a real
    executable on the install host: the shipped ``check_chapter`` prose gate arrives as an
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
    backup lives under the gitignored ``.worc/`` home and only the newest few are kept. Returns the
    backup path, or ``None`` when there is nothing to back up.
    """
    tools = worc_home / "tools"
    if not tools.is_dir() or not any(tools.iterdir()):
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = tools.with_name(f"tools.bak-{stamp}")
    shutil.copytree(tools, backup)
    _prune_install_backups(worc_home, "tools")
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
    loaded = load_config(path)
    # Two channels, one voice: the loader's warnings say what the file resolved TO (a key whose
    # value another key overrode), the validator's say what it MEANS. Two loops, not one
    # concatenation:
    # `validate_config` raises on a fatal issue, and a config with both problems must still report
    # the resolution rather than dying before it is printed.
    for warning in loaded.warnings:
        _LOG.warning("config warning: %s", warning)
    for warning in validate_config(loaded.config):
        _LOG.warning("config warning: %s", warning)
    return loaded.config


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
        candidate = RuntimeLayout.default(info.root).control_home / "config.yaml"
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
        return RuntimeLayout.default(info.root).private_home / ".env", False
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
    # (handled in main with a clean message + exit 2) — never upgrade a config we cannot read. The
    # read-back runs against a throwaway copy with the removed keys already stripped: a key this
    # command exists to drop is rejected by the current loader (v33's flat `supervisor.model`, say),
    # and refusing on it would leave the operator no automated path off the old schema. Every other
    # problem still refuses here, and the regenerated file is validated again below before we write.
    probe = config_upgrade.parse_mapping(text)
    config_upgrade.strip_removed_keys(probe)
    probed = loads_config(config_upgrade.render(probe), source=str(path))
    # This is the command that REWRITES the config, so an operator who never reads `run`'s log
    # has to see it here. Printed like every other line this command emits (a log record
    # would be filtered by `logging.level`), and taken from the probe rather than the merged copy
    # because the "already up to date" path below returns before the merge is rendered.
    for warning in probed.warnings:
        print(f"upgrade-config: warning: {warning}")

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


def layout_for(config: OrchestratorConfig) -> RuntimeLayout:
    """The one provider-neutral :class:`RuntimeLayout` for the configured repo.

    Built here at the CLI composition boundary and injected into consumers so each declares which
    surface it owns. ``control_home`` and ``private_home`` both resolve to ``<repo>/.worc`` today.
    """
    return RuntimeLayout.default(config.repo.local_path)


def worc_home_for(config: OrchestratorConfig) -> Path:
    """The gitignored **private** runtime home — ``layout.private_home`` (``<repo>/.worc/`` today).

    Everything the orchestrator generates privately — ``state.db``, ``logs/``, ``orchestrator.pid``,
    ``workspace/``, ``checks/``, the resolved check profile, validation reports, the memory store —
    lives here. It is the private-surface accessor; control-plane consumers
    (config/flows/tools/guide) use ``layout_for(config).control_home`` instead. The two coincide
    until the private home is relocated.
    """
    return layout_for(config).private_home


def tasks_root_for(config: OrchestratorConfig) -> Path:
    """The repo root that holds the tracked ``tasks/`` lifecycle dirs (the audit trail).

    Unlike :func:`worc_home_for`, ``tasks/`` stays at the repo root so the task file and its
    committed ``<id>.summary.md`` can be audit-committed into the repo's history.
    """
    return Path(config.repo.local_path)


def pending_dir(config: OrchestratorConfig) -> Path:
    """The folder ``watch`` scans for new tasks: ``<repo>/<paths.tasks_dir>/pending``."""
    return tasks_root_for(config) / config.paths.tasks_dir / "pending"


def preparing_dir(config: OrchestratorConfig) -> Path:
    """The staging area ``watch`` never scans: ``<repo>/<paths.tasks_dir>/preparing``.

    Compose a task file here (invisible to the daemon by construction), then :func:`promote_tasks`
    moves it into :func:`pending_dir` once it is complete — closing the mid-write pickup race.
    """
    return tasks_root_for(config) / config.paths.tasks_dir / "preparing"


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


def _daemon_alive(config: OrchestratorConfig) -> bool:
    """Read-only: is a live ``watch`` daemon recorded for this worc home (PID file present + alive)?

    Wraps the standard liveness idiom used across the mutating commands. On Windows an unrelated PID
    cannot be probed, so a present PID file counts as alive; a clean ``stop`` clears it either way.
    """
    return (
        process_control.running_daemon_pid(process_control.pid_file_path(worc_home_for(config)))
        is not None
    )


def _display_status(row: TaskRow, *, daemon_alive: bool) -> str:
    """The human status label for a task row (the single source of truth for all read-only views).

    A ``running`` row with no live daemon is parked at its checkpoint, awaiting resume — not
    executing — so it reads as ``parked (no daemon)``. This dominates the B-lite ``(paused)``
    marker, which only makes sense while the daemon is alive and waiting out a provider outage.

    A pause carrying a provider-reported wake instant names it, because otherwise a daemon correctly
    waiting out a limit is indistinguishable from a hung one.
    """
    if row.status is Status.RUNNING and not daemon_alive:
        return "parked (no daemon)"
    if row.status is Status.RUNNING and row.blocked_since:
        if row.blocked_until:
            return f"{row.status.value} (paused until {row.blocked_until})"
        return f"{row.status.value} (paused)"
    return row.status.value


def _parked_slot_note(config: OrchestratorConfig) -> str | None:
    """Actionable note when a ``running`` task still holds the slot after a stop (else ``None``).

    Read-only, reopening ``state.db`` like :func:`has_active_task`. The task is parked at its
    checkpoint (the recovery invariant), not executing — so point the operator at the levers that
    actually clear or continue it, rather than leaving a silent, queue-blocking ``running`` row.
    """
    db_path = Path(worc_home_for(config)) / "state.db"
    if not db_path.is_file():
        return None
    store = StateStore.open_readonly(db_path)
    try:
        parked = next((t for t in store.find_active_tasks() if t.status is Status.RUNNING), None)
        node = store.get_flow_checkpoint(parked.task_id)[0] if parked is not None else None
    finally:
        store.close()
    if parked is None:
        return None
    where = f" (parked at node {node})" if node else " (parked)"
    return (
        f"stop: note: task {parked.task_id} is still running{where}, holding the processing slot; "
        f"it resumes on the next `up`/`watch`. To continue it now: `rerun {parked.task_id} "
        f"--continue`; to close it: `finalize {parked.task_id} --as failed`"
    )


def _configure_runtime_logging(args: argparse.Namespace) -> None:
    # The flag wins; absent (default None) we set up at INFO and let load_config_for re-apply the
    # persisted logging.level once the config is known.
    level = _LOG_LEVELS[args.log_level] if args.log_level else logging.INFO
    configure_logging(
        level=level,
        fmt=getattr(args, "log_format", "logfmt"),
        file_path=getattr(args, "log_file", None),
        # A detached daemon's raw stderr is captured into a startup log that nothing rotates or
        # caps, so keeping the terminal handler once the rotating --log-file exists would grow that
        # file forever as a byte-for-byte duplicate. Everything written before this point (argparse
        # errors, import failures, a preflight abort) still lands there, which is what it is for.
        console=os.environ.get(agent_process.STARTUP_CAPTURE_ENV) != "1",
    )


# Split a filename into runs of ASCII digits vs everything else (capturing group → the digit runs
# land on odd indices of the result). ASCII-only so ``lstrip("0")`` and the magnitude comparison
# below stay well-defined; any non-ASCII digit falls into a text run and is compared as folded text.
_DIGIT_RUN = re.compile(r"([0-9]+)")

# One token per run: ``(0, 0, text)`` for a text run, ``(1, magnitude, digits)`` for a digit run
# (text sorts before digits at the same position). Uniform 3-tuple shape so the key type is flat.
_NaturalToken = tuple[int, int, str]
# The operator-visible ordering key: the natural tokens, then the raw name as the final tie-break.
_NaturalKey = tuple[tuple[_NaturalToken, ...], str]


def natural_sort_key(name: str) -> _NaturalKey:
    """Ordering key for a pending task filename — the tie-break the whole scheduler sorts on.

    This is the order the operator reads in ``worc list`` / ``worc top`` *and* the order the daemon
    actually claims files in, so it must match a file manager's numeric-aware listing and be
    identical on every OS:

    - **Natural**: digit runs compare by magnitude, not bytewise, so ``p9`` sorts before ``p10``. A
      digit run compares as ``(len(no_leading_zeros), no_leading_zeros)`` — a string compare, so a
      pathological all-digits name never triggers an unbounded ``int()``, while ``007`` still equals
      ``7`` in magnitude.
    - **Platform-stable**: the name is casefolded *here* rather than left to ``Path.__lt__`` (whose
      case handling is case-sensitive on POSIX but case-folded on Windows), so the scheduler's
      decision never depends on the host OS. Never sort ``Path`` objects for a scheduling decision.
    - **Strict total order**: the original (non-folded) name is the final element, so distinct names
      — ``p9-07`` vs ``p9-7``, or two names differing only in case — never compare equal and the
      result cannot depend on ``iterdir()`` yield order.
    """
    tokens: list[_NaturalToken] = []
    for index, part in enumerate(_DIGIT_RUN.split(name.casefold())):
        if index % 2:  # capturing split → odd indices are the (always non-empty) digit runs
            stripped = part.lstrip("0")
            tokens.append((1, len(stripped), stripped))
        elif part:  # even indices are text; skip the empties re.split emits between adjacent runs
            tokens.append(
                (0, 0, part)
            )  # middle 0 keeps the 3-tuple shape uniform (unused for text)
    return (tuple(tokens), name)


def select_pending(folder: Path) -> list[Path]:
    """Pending task files (``.md`` / ``.json``) in :func:`natural_sort_key` order.

    Natural, platform-stable, and a strict total order — the same tie-break every ordering consumer
    (the scheduler, ``worc list``, ``promote --all``) uses, so the order never depends on the host
    OS or on ``iterdir()`` yield order.
    """
    if not folder.is_dir():
        return []
    return sorted(
        (p for p in folder.iterdir() if p.suffix.lower() in (".md", ".json")),
        key=lambda p: natural_sort_key(p.name),
    )


class _PendingScan(NamedTuple):
    """Lightweight scheduler view of a pending task file (no validation)."""

    task_id: str | None
    depends_on: tuple[str, ...]
    priority_rank: int
    queue: str
    # Front-matter ``title`` (or ``None``) — shown in the next-task confirmation prompt,
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
    sort by ``(priority_rank, natural_sort_key(name))`` — ``priority`` is the only intentional lever
    and the filename is the natural, platform-stable tie-break within a priority. This is the single
    source of truth for "what order will the daemon actually run", shared by ``watch_once`` and the
    read-only monitor (``worc list`` / ``worc top`` / the console ``ps`` view) so the shown order
    never drifts from the claim order.
    """
    scans = [
        (p, s)
        for p, s in ((p, _scan_pending_meta(p)) for p in select_pending(folder))
        if s.queue == selector
    ]
    scans.sort(key=lambda item: (item[1].priority_rank, natural_sort_key(item[0].name)))
    return scans


def find_task_file(folder: Path, target: str) -> Path | None:
    """First task file in ``folder`` matching ``target`` by file name, stem, or front-matter id."""
    for path in select_pending(folder):
        if target in (path.name, path.stem, _scan_pending_meta(path).task_id):
            return path
    return None


def _atomic_copy(src: Path, dest: Path) -> None:
    """Copy ``src`` onto ``dest`` atomically: write a temp sibling, then a single ``os.replace``.

    The temp uses a ``.tmp`` suffix (never ``.md``/``.json``) so that, when ``dest`` lives in the
    watch-scanned ``pending/`` folder, the half-written temp is never itself a scan candidate.
    ``os.replace`` is an atomic rename on the same filesystem on both POSIX and Windows.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.stem}-", suffix=".tmp")
    try:
        os.close(fd)
        shutil.copyfile(src, tmp)
        Path(tmp).replace(dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_subtask_refs(task_file: Path) -> list[str]:
    """Repo-relative ``subtasks:`` spec paths declared in a root task's front matter (else empty).

    Best-effort: a read/parse problem or a non-list value yields no refs, so a single-file promote
    simply moves the one file (the validation gate rejects a genuinely broken file if it later lands
    in ``pending/``). Refs that escape the staging dir (absolute or containing ``..``) are dropped.
    """
    try:
        source = read_task_source(task_file)
        parse = split_frontmatter(source.raw_bytes.decode("utf-8"), source.suffix)
    except (OSError, UnicodeDecodeError):
        return []
    if not parse.present or parse.malformed:
        return []
    raw = parse.frontmatter.get("subtasks", [])
    if not isinstance(raw, (list, tuple)):
        return []
    refs: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            continue
        ref = entry.strip()
        if Path(ref).is_absolute() or ".." in Path(ref).parts:
            continue
        refs.append(ref)
    return refs


def _promote_one(src: Path, dest: Path, moved: list[str], errors: list[str]) -> None:
    """Atomically move one staged file into ``dest``; record the outcome in ``moved``/``errors``.

    Refuses to overwrite an existing file in ``pending/`` — a same-named queued task is never
    clobbered. ``src.replace`` is a single rename syscall (no partial-write window).
    """
    if not src.is_file():
        errors.append(f"no such staged file: {src.name}")
        return
    if dest.exists():
        errors.append(f"{dest.name} already in pending — not overwriting a queued task")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dest)
    moved.append(dest.name)


def promote_tasks(
    config: OrchestratorConfig, *, target: str | None = None, all_files: bool = False
) -> tuple[list[str], list[str]]:
    """Move staged task files from ``preparing/`` into ``pending/`` atomically (never a copy).

    ``all_files`` moves every top-level ``.md``/``.json`` plus the whole ``subtasks/`` subfolder;
    otherwise ``target`` (a task id or file name/stem) selects one root, and a decomposition root
    pulls the subtask specs it references along with it. In both modes subtask specs move *before*
    their root, so a root never appears in ``pending/`` without its specs. Returns
    ``(moved_names, errors)`` for the caller to render.
    """
    preparing = preparing_dir(config)
    pending = pending_dir(config)
    moved: list[str] = []
    errors: list[str] = []

    if all_files:
        sub_src = preparing / "subtasks"
        if sub_src.is_dir():
            for spec in select_pending(sub_src):
                _promote_one(spec, pending / "subtasks" / spec.name, moved, errors)
        for root in select_pending(preparing):
            _promote_one(root, pending / root.name, moved, errors)
        if not moved and not errors:
            errors.append(f"nothing staged in {preparing.name}/")
        return moved, errors

    if not target:
        errors.append("nothing to promote: give a task id/file or --all")
        return moved, errors
    match = find_task_file(preparing, target)
    if match is None:
        errors.append(f"{target!r} is not a staged file in {preparing.name}/")
        return moved, errors
    for ref in _read_subtask_refs(match):  # deco root: specs first, then the root
        _promote_one(preparing / ref, pending / Path(ref), moved, errors)
    _promote_one(match, pending / match.name, moved, errors)
    return moved, errors


def _confirm_next_task(
    orchestrator: Orchestrator,
    config: OrchestratorConfig,
    task_id: str | None,
    title: str | None,
) -> bool:
    """Ask the operator (Telegram) to approve claiming the next pending task.

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


def _already_settled(orchestrator: Orchestrator, task_id: str, task_file: Path) -> bool:
    """True iff ``task_id`` already reached a terminal status and ``task_file`` is its own leftover.

    A ``manual_action_required`` task keeps its file in ``pending/`` by design (branch preserved,
    the operator reviews/publishes); a committed-``tasks/`` done/failed move can also resurface in
    ``pending/`` after a base-branch checkout. Either way the daemon must **not** re-run it — that
    would re-reject it as ``duplicate_task_id`` and quarantine the operator's file. A *different*
    file colliding on a used id is left to fall through to the gate, which rejects it loudly.
    """
    row = orchestrator.lookup_task(task_id)
    if row is None or row.status not in TERMINAL or not row.source_path:
        return False
    try:
        return Path(row.source_path).resolve() == task_file.resolve()
    except OSError:
        return False


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

    A pending file whose id already reached a terminal status and is that task's own leftover is
    also skipped (:func:`_already_settled`): a ``manual_action_required`` task keeps its file in
    ``pending/`` for the operator, and re-running it would only reject it as ``duplicate_task_id``
    and quarantine the file. Resolving it (``rerun``/``finalize``) is the operator's call.

    Eligible tasks are ranked by ``priority`` (high → mid → low), ties broken by the natural,
    platform-stable filename order from :func:`natural_sort_key`. ``depends_on`` is always stronger:
    a higher-priority but WAITING task is skipped, so a lower-priority eligible task runs first.
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
    # then order by (priority_rank, natural filename key). pending_map is order-independent.
    scans = scan_pending_sorted(folder, selector)
    pending_map = {s.task_id: s.depends_on for _p, s in scans if s.task_id is not None}
    for task_file, scan in scans:
        task_id, depends_on = scan.task_id, scan.depends_on
        if not orchestrator.acquire_slot(""):
            break  # the slot is not free (an active task remains)
        if task_id is not None and _already_settled(orchestrator, task_id, task_file):
            # A terminal task's own file lingering in pending/ (e.g. manual_action_required keeps
            # it there for the operator). Never re-run it — that would reject it as a duplicate id
            # and quarantine the file. Non-blocking skip, like a WAITING dependency.
            _LOG.info("task %s already settled; leaving its file for the operator", task_id)
            continue
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
            break  # operator denied / silent → leave pending, stop chaining this cycle
        result = orchestrator.run_task(str(task_file))
        results.append(result)
        if result.final_status is Status.MANUAL_ACTION_REQUIRED:
            break  # a manual task blocks automatic continuation
        if not auto:
            break  # auto mode off: process exactly one task
    return results


def _build_cleanup_hook(config: OrchestratorConfig) -> Callable[[], None] | None:
    """A rate-limited memory-cleanup callable for the ``watch_loop`` idle gap, or ``None``.

    Returns ``None`` when memory is disabled — then no cleanup is ever scheduled. Otherwise a
    best-effort closure that runs one bounded :meth:`CleanupJob.run_once` at most every
    ``cleanup_min_interval_s``, building a fresh store view + ``DerivedIndex`` each pass so the
    repo-introspection never goes stale across a long-lived daemon. A failure is logged and
    swallowed — cleanup must never crash the watcher or delay the next task pickup."""
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
            layout = MemoryLayout(layout_for(config).private_home)
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
        except Exception as exc:
            _LOG.warning("memory cleanup failed (best-effort, ignored): %s", type(exc).__name__)

    return _run


# Granularity for noticing a stop request during the between-tick poll sleep. Bounds how long a
# stop takes to be seen on Windows, where the SIGTERM event never fires cross-process and the
# stop-file is the only channel (see watch_loop's poll-sleep loop). Kept small so shutdown lands
# well within `stop --timeout` (30s) even with a large poll_interval (300s default).
_STOP_POLL_SECONDS = 1.0


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

    Two stop channels are checked around ticks and during idle sleep: a ``stop_event`` (set by a
    POSIX ``SIGTERM`` handler) and the cross-platform ``stop_file`` sentinel. During an active tick,
    ``cmd_watch`` injects the same predicate into the FlowEngine, which parks before the next node;
    this loop then sees the still-present channel and exits. The ``sleep_fn`` path is kept for
    callers without an event (existing tests).
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
        # delays the next pickup. Rate-limiting + bounds live inside the hook.
        if cleanup_hook is not None and not has_active_task(config):
            cleanup_hook()
        if poll_interval <= 0:
            break
        if max_iterations is not None and iteration >= max_iterations:
            break
        if stop_event is not None:
            # Interruptible poll sleep: wait in _STOP_POLL_SECONDS chunks, re-checking both stop
            # channels each chunk. On POSIX stop_event.wait wakes the instant SIGTERM fires; on
            # Windows the event never fires cross-process, so the stop-file (checked by
            # _stop_requested) is the only channel — a monolithic wait(poll_interval) would delay
            # shutdown by up to poll_interval (300s), far past `stop --timeout` (30s), orphaning the
            # daemon. Chunking bounds that latency to ~_STOP_POLL_SECONDS regardless of platform.
            remaining = float(poll_interval)
            while remaining > 0 and not _stop_requested():
                chunk = min(_STOP_POLL_SECONDS, remaining)
                if stop_event.wait(chunk):  # returns True the instant SIGTERM fires (POSIX)
                    break
                remaining -= chunk
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
    require_launch_environment(config, env_file=resolve_env_file_path(args)[0])
    preflight.require_git_control()  # git must honor `core.hooksPath` (>= 2.9)
    if config.git.create_pull_request:
        preflight.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish
        # Non-blocking advisory if gh is present but logged out. The policy travels with it so
        # the probe sees the same environment (a proxy, a token) every other gh call gets.
        preflight.warn_if_gh_logged_out(security=config.security)
    # A node may route to ANY allowed provider, so one that cannot start is refused up front
    # rather than discovered at the first fallback with a stage's work already spent.
    require_provider_auth(config)
    orchestrator = build_orchestrator(
        config,
        layout=layout_for(config),
        env_file=resolve_env_file_path(args)[0],
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
        # A gate reject prints the machine reason AND the field+cause detail, so the operator
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


def _format_exhausted(loops: tuple[ExhaustedLoop, ...]) -> str:
    """Human-readable ``name (counter/cap)`` list for the exhausted-fix-budget prompt/messages."""
    return "; ".join(f"{loop.loop} ({loop.counter}/{loop.cap})" for loop in loops)


def _report_rerun_plan(plan: RerunPlan) -> None:
    """Print the planned reconciliation for ``rerun --dry-run``; writes nothing."""
    mode = "restart" if plan.restart_in_place else "continue" if plan.continue_mode else "fresh"
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
        if plan.exhausted_fix_loops:
            print(
                f"  budget:    exhausted: {_format_exhausted(plan.exhausted_fix_loops)} — "
                "resuming would re-park immediately unless reset"
            )
        if plan.global_backstop_exhausted:
            print(
                "  budget:    the global max_total_fix_iterations backstop is exhausted; it is a "
                "hard ceiling and cannot be reset"
            )
    elif plan.restart_in_place:
        archive = f"attempt-{max(plan.attempt - 1, 0)}"
        print(f"  branch:    reuse {plan.branch or '(current)'} as-is (operator-owned; no reset)")
        print(f"  artifacts: archived to logs/{plan.task_id}/{archive}/")
        print("  state:     per-attempt row state cleared; re-driven from the top")
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


def _resolve_reset_fix_budget(
    args: argparse.Namespace, plan: RerunPlan, *, non_interactive: bool
) -> tuple[bool, int | None]:
    """Resolve the effective reset-fix-budget decision for a ``--continue`` resume.

    Returns ``(resolved, abort_code)``; a non-``None`` ``abort_code`` means the caller should return
    it immediately (the refusal message is already printed). Never gated by ``--yes`` — a budget
    reset is exactly the consequential decision this prompt exists to surface, so it always needs
    its own explicit answer, interactively or via ``--reset-fix-budget``/``--no-reset-fix-budget``.
    """
    resolved = bool(args.reset_fix_budget)
    if plan.exhausted_fix_loops:
        names = _format_exhausted(plan.exhausted_fix_loops)
        if args.reset_fix_budget is False:
            print(f"rerun: refusing to resume — fix budget exhausted: {names}")
            return False, 1
        if args.reset_fix_budget is True:
            resolved = True
        else:
            if non_interactive:
                print(
                    f"rerun: {names} exhausted; non-interactive — pass "
                    "--reset-fix-budget or --no-reset-fix-budget"
                )
                return False, 1
            if not _confirm(f"{names} exhausted. Reset it to allow further fix rounds? [y/N] "):
                print(f"rerun: refusing to resume — fix budget exhausted: {names}")
                return False, 1
            resolved = True
    if resolved and plan.global_backstop_exhausted:
        print(
            "rerun: resetting the fix budget will not unblock this task — the global "
            "max_total_fix_iterations backstop is a hard ceiling and cannot be reset; nothing "
            "further can run automatically"
        )
        return False, 1
    return resolved, None


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
        layout=layout_for(config),
        env_file=resolve_env_file_path(args)[0],
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

    require_launch_environment(config, env_file=resolve_env_file_path(args)[0])
    preflight.require_git_control()  # git must honor `core.hooksPath` (>= 2.9)
    if config.git.create_pull_request:
        preflight.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish
        # Non-blocking advisory if gh is present but logged out. The policy travels with it so
        # the probe sees the same environment (a proxy, a token) every other gh call gets.
        preflight.warn_if_gh_logged_out(security=config.security)
    # A node may route to ANY allowed provider, so one that cannot start is refused up front
    # rather than discovered at the first fallback with a stage's work already spent.
    require_provider_auth(config)

    for note in plan.notes:
        print(f"rerun: note: {note}")
    # --non-interactive (also forced by a non-TTY stdin) never calls input(): a nested blocking
    # input() fights 'worc shell's own stdin reader (the single-stdin-reader rule) — the same
    # class of bug already fixed for 'stop'/'restart'. Refuse-with-instructions instead of hanging.
    non_interactive = getattr(args, "non_interactive", False) or not sys.stdin.isatty()
    # The prompt names what actually happens in each mode. --continue reuses the existing branch in
    # place (base_branch is never touched); restart-in-place likewise re-drives on the operator's
    # branch with no reset (a plain `rerun` of a pre-checkpoint failure); only a fresh rerun resets
    # to base. Saying "from base" on the first two would wrongly imply a base_branch checkout.
    if plan.restart_in_place:
        mode, target = "restart", f"on branch '{plan.branch}'"
    elif args.continue_:
        mode, target = "continue", f"on branch '{plan.branch}'"
    else:
        mode, target = "fresh", f"from base '{plan.base_branch}'"
    if not args.yes:
        if non_interactive:
            print("rerun: refusing without confirmation (non-interactive); pass --yes to proceed")
            return 1
        if not _confirm(f"Rerun {args.task_id} [{mode}] {target}? [y/N] "):
            print("rerun: aborted")
            return 0

    if plan.restart_in_place:
        assert plan.source_path is not None  # guarded by plan_rerun refusals
        result = orchestrator.restart_task_in_place(args.task_id, source_path=plan.source_path)
        label = "rerun/restart"
    elif args.continue_:
        resolved_reset, abort_code = _resolve_reset_fix_budget(
            args, plan, non_interactive=non_interactive
        )
        if abort_code is not None:
            return abort_code
        result = orchestrator.continue_task(
            args.task_id,
            reset_fix_budget=resolved_reset,
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
    if plan.returns_to_base:
        print(f"  cleanup:   checkout base '{plan.base_branch}'")
    else:
        print(f"  cleanup:   stay on branch '{plan.branch or '(current)'}'")
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
        config,
        layout=layout_for(config),
        env_file=resolve_env_file_path(args)[0],
        heartbeat_seconds=args.heartbeat_seconds,
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
        config,
        layout=layout_for(config),
        env_file=resolve_env_file_path(args)[0],
        heartbeat_seconds=args.heartbeat_seconds,
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
        config,
        layout=layout_for(config),
        env_file=resolve_env_file_path(args)[0],
        heartbeat_seconds=args.heartbeat_seconds,
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

    Sorted by mtime descending so ``--keep N`` retains the most recently modified runs.
    """
    if not logs_root.is_dir():
        return []
    dirs = [p for p in logs_root.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)


def _daemon_log_files(logs_root: Path, *, ledger_path: Path) -> list[Path]:
    """Every non-directory entry at the root of ``.worc/logs/`` except the ledger.

    These are the daemon's own runtime noise — the rotating operator log, its numbered backups, and
    the startup capture of a console-spawned daemon — which sit beside the per-task dirs rather than
    inside one. Selected by *shape* (anything that is not a task dir and not the ledger) rather than
    by filename, so a rotated backup or a future daemon-written file is reclaimable the day it
    appears instead of surviving a command whose name promises a clean logs root.
    """
    if not logs_root.is_dir():
        return []
    return sorted(p for p in logs_root.iterdir() if not p.is_dir() and p != ledger_path)


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
    """Sweep the whole ``.worc/logs/`` root: per-task artifact dirs plus the daemon's own logs.

    ``--keep N`` retains the N newest task dirs (no prompt unless N=0); bare ``clean`` removes every
    task dir. Either way the daemon logs beside them go too — they are runtime noise, and leaving
    them behind is what made "clean" fail to mean a clean logs root. The ledger
    (``completed.jsonl``) is the audit trail: it survives unless ``--all``, and the output names the
    flag that takes it rather than reporting a bare "kept".

    Refuses outright while a task is active, and holds the daemon logs back while a watch daemon is
    live — both refusals are reported distinctly, because an active task and a live daemon are
    different things for the operator to clear.
    """
    if has_active_task(config):
        print("logs clean: a task is active — refusing; run when the orchestrator is idle")
        return 1
    logs_root = worc_home_for(config) / "logs"
    ledger_path = Ledger(logs_root).path
    task_dirs = _task_log_dirs(logs_root)
    if args.keep is not None and args.keep < 0:
        print("logs clean: --keep must be >= 0")
        return 2
    kept_dirs, doomed_dirs = (
        (task_dirs[: args.keep], task_dirs[args.keep :])
        if args.keep is not None
        else ([], task_dirs)
    )
    # The daemon's rotating handler keeps daemon.log open, and its startup capture is the child's
    # own stdout descriptor: on Windows both unlinks fail while POSIX unlinks them happily, and a
    # cleanup command whose result depends on the host OS is not acceptable. So the daemon logs wait
    # for the daemon to stop, on every platform.
    daemon_alive = _daemon_alive(config)
    doomed_files = [] if daemon_alive else _daemon_log_files(logs_root, ledger_path=ledger_path)
    take_ledger = bool(args.all) and ledger_path.exists()

    notes: list[str] = []
    if daemon_alive:
        notes.append(
            "logs clean: kept the daemon logs — a watch daemon is running; stop it and re-run"
        )
    if not args.all and ledger_path.exists():
        notes.append(
            f"logs clean: kept the ledger ({ledger_path.name}) — 'logs clean --all' removes it too"
        )

    if not doomed_dirs and not doomed_files and not take_ledger:
        print("logs clean: nothing to remove")
        for note in notes:
            print(note)
        return 0

    # A bounded --keep N>0 is a routine prune; anything that empties the root confirms first.
    bounded_prune = args.keep is not None and args.keep > 0
    confirmed = (
        bounded_prune
        or args.yes
        or _confirm(
            f"Remove {_clean_plan_phrase(doomed_dirs, doomed_files, ledger=take_ledger)} "
            f"under {logs_root.as_posix()}? [y/N] "
        )
    )
    if not confirmed:
        print("logs clean: aborted")
        return 0

    for path in doomed_dirs:
        shutil.rmtree(path, ignore_errors=True)
    for path in doomed_files:
        path.unlink(missing_ok=True)
    if take_ledger:
        ledger_path.unlink(missing_ok=True)
    removed = _clean_plan_phrase(doomed_dirs, doomed_files, ledger=take_ledger)
    kept = f"; kept {len(kept_dirs)} task dir(s)" if args.keep is not None else ""
    print(f"logs clean: removed {removed}{kept}")
    for note in notes:
        print(note)
    return 0


def _clean_plan_phrase(dirs: Sequence[Path], files: Sequence[Path], *, ledger: bool) -> str:
    """Operator-facing description of a ``logs clean`` target set (shared by prompt and report).

    The counts are always both named, even at zero: "0 daemon log file(s)" is how the operator sees
    that the sweep did cover them, which is the whole point of the wider default.
    """
    parts = [f"{len(dirs)} task dir(s)", f"{len(files)} daemon log file(s)"]
    if ledger:
        parts.append("the ledger")
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def cmd_runs(args: argparse.Namespace) -> int:
    """Dispatch the ``runs`` subcommands (currently only ``clean``)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if args.runs_action == "clean":
        return _cmd_runs_clean(args, config)
    raise SystemExit(f"Unknown runs action '{args.runs_action}'.")


def _cmd_runs_clean(args: argparse.Namespace, config: OrchestratorConfig) -> int:
    """Reclaim per-task frozen bundles and sealed exchanges under ``.worc/runs/``.

    The manual half of run retention: with ``logging.clean_runs_on_success`` on, a successful task
    already evicts its own subtree and this verb rarely finds anything; with it off every run keeps
    its frozen inputs and seals for analysis, and this is how they are reclaimed. ``--keep N``
    retains the N most recently touched tasks.

    Quarantined exchange evidence needs ``--include-quarantine``: it exists only when an agent wrote
    the read-only exchange, so it is never swept up by a routine cleanup. Refuses while a task is
    active — the same guard, and the same reason, as ``logs clean``.
    """
    if has_active_task(config):
        print("runs clean: a task is active — refusing; run when the orchestrator is idle")
        return 1
    if args.keep is not None and args.keep < 0:
        print("runs clean: --keep must be >= 0")
        return 2
    private_home = worc_home_for(config)
    include_quarantine = bool(args.include_quarantine)
    task_ids = runs_retention.run_task_ids(private_home, include_quarantine=include_quarantine)
    kept, doomed = (
        (task_ids[: args.keep], task_ids[args.keep :]) if args.keep is not None else ((), task_ids)
    )
    runs_home = runs_root(private_home)
    # Only worth mentioning when there is evidence sitting there to keep.
    quarantine_note = (
        not include_quarantine and (runs_home / runs_retention.QUARANTINE_ROOT).is_dir()
    )

    def report_kept_quarantine() -> None:
        if quarantine_note:
            print(
                "runs clean: quarantined exchange evidence is kept — "
                "'runs clean --include-quarantine' removes it too"
            )

    if not doomed:
        print("runs clean: nothing to remove")
        report_kept_quarantine()
        return 0

    # A bounded --keep N>0 is a routine prune of caches and skips the prompt. Quarantined evidence
    # is not a cache — it exists only because an agent wrote a surface it was told not to — so
    # touching it always confirms, whatever the scope, unless the operator says --yes.
    bounded_prune = args.keep is not None and args.keep > 0 and not include_quarantine
    target = f"the run artifacts of {len(doomed)} task(s)" + (
        " including quarantined evidence" if include_quarantine else ""
    )
    if (
        not bounded_prune
        and not args.yes
        and not _confirm(f"Remove {target} under {runs_home.as_posix()}? [y/N] ")
    ):
        print("runs clean: aborted")
        return 0
    removed_dirs = sum(
        len(
            runs_retention.remove_task_runs(
                private_home, task_id, include_quarantine=include_quarantine
            )
        )
        for task_id in doomed
    )
    kept_note = f"; kept {len(kept)} task(s)" if args.keep is not None else ""
    print(f"runs clean: removed {removed_dirs} dir(s) across {len(doomed)} task(s){kept_note}")
    report_kept_quarantine()
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    """Dispatch the ``memory`` subcommands: show / validate (read-only) | compact / restore.

    Disabled memory (``memory.enabled: false`` or the block absent) is a clean no-op for every verb
    The mutating verbs (compact / restore) refuse while a task is active and offer
    ``--dry-run`` to print their plan first."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if not config.memory.enabled:
        print("memory: disabled in config (memory.enabled: false) — nothing to do")
        return 0
    layout = MemoryLayout(layout_for(config).private_home)
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
    """Run a fuller (uncapped) cleanup pass now — refused while a task is active."""
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
    """Roll the store back to an audit snapshot — refused while a task is active."""
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


def _auth_report_field(auth: AuthProbe | None) -> str:
    """The ``auth=…`` fragment of a provider's preflight health line.

    Empty when nothing was probed, so a provider whose adapter implements no credential verb prints
    no auth claim at all rather than an invented one.
    """
    if auth is None:
        return ""
    method = f" ({auth.method})" if auth.method else ""
    return f", auth={auth.state.value}{method}"


def _logged_out_refusal(pid: ProviderId, auth: AuthProbe) -> str:
    """The operator-facing refusal for an allowed provider whose CLI reports no credentials.

    Shared by the preflight report and the startup gate so both name the same levers. It names
    ``agents.allowed`` because that is the second lever: the verdict does not care whether the
    provider is anyone's primary, so a host that only ever uses one CLI has to say so in the config
    rather than leaving a provider listed that no node can actually reach. And it names the
    environment allowlist because that list *replaces* its default — a host whose CLI resolves
    credentials through a variable the allowlist no longer passes reports logged out while being
    logged in, and that failure looks identical to a real one.
    """
    return (
        f"{auth.detail}; {pid.value} is in agents.allowed, so a node may route to it — log in or "
        f"remove it from agents.allowed (if this host IS logged in, check that "
        f"security.allowed_environment still passes the variables the CLI needs to reach its "
        f"credential store)"
    )


def _auth_verdict(pid: ProviderId, auth: AuthProbe | None) -> tuple[bool, str | None]:
    """The blocking verdict and any extra report line for one provider's credential probe.

    A logged-out provider is fatal **whatever its role in any route** — a deliberate inversion of
    the fallback-aware rule governing an advisory degradation, because "a fallback will cover" is
    precisely the assumption a dead fallback breaks, and its silence is only discovered at the
    moment it is needed. A probe that could not answer warns instead, on the same principle that
    already governs the logged-out ``gh`` advisory: a flaky or drifted probe must never stop a run.
    """
    if auth is None or auth.state is AuthState.LOGGED_IN:
        return True, None
    if auth.state is AuthState.LOGGED_OUT:
        return False, f"{pid.value}: FAIL — {_logged_out_refusal(pid, auth)}"
    return True, f"{pid.value}: WARN — {auth.detail} (a probe that cannot answer must not block)"


def require_provider_auth(config: OrchestratorConfig) -> None:
    """Refuse to start when an allowed provider's CLI reports no stored credentials.

    The unattended counterpart to the preflight auth line: a daemon has no operator to read a
    warning, and a provider that cannot start is otherwise discovered only once a node routes to
    it — by which point a stage's work is already spent. Deliberately narrower than
    :func:`run_preflight`, which also gates Telegram, ``gh``, the isolation policy and the live
    capability smoke: none of those may decide whether a task starts.

    Only an explicit logged-out answer blocks. A provider with no probe, an unreadable answer, or
    no configured adapter never does — refusing on a probe that could not answer would trade a rare
    wasted run for a daemon that will not start at all. Auth is the whole scope: a missing
    executable is a strictly worse condition, deliberately still left to surface at first use.

    Every allowed provider is probed through ``preflight()``, the only channel to a credential
    answer, so this costs a handful of short, timeout-bounded child-process launches per invocation.

    :raises ProviderNotLoggedInError: an allowed provider reported no credentials.
    """
    providers = build_providers(config, layout=layout_for(config))
    for pid in config.agents.allowed:
        provider = providers.get(pid)
        auth = provider.preflight().auth if provider is not None else None
        if auth is not None and auth.state is AuthState.LOGGED_OUT:
            raise preflight.ProviderNotLoggedInError(_logged_out_refusal(pid, auth))


def require_launch_environment(
    config: OrchestratorConfig,
    *,
    env_file: Path | None,
    system: str | None = None,
) -> None:
    """Refuse a task launch when host-specific environment safety checks fail.

    ``worc preflight`` remains the full diagnostic surface, but a daemon or explicit run must not
    depend on the operator remembering to invoke it after every config edit. This repeats only the
    launch-critical checks whose damage occurs during a run: the Windows ``SystemRoot`` floor and
    canonical assigned-path collisions (symlinks, case aliases, and the env-file).
    Values are never included in the error.

    :raises ConfigError: the current host cannot safely use the configured environment.
    """
    issues: list[str] = []
    launch_issue = launch_critical_env_issue(config.security.allowed_environment, system=system)
    if launch_issue is not None:
        issues.append(launch_issue)

    clone = Path(config.repo.local_path)
    protected = host_protected_paths(
        config,
        build_internal_deny_policy(layout_for(config), env_file=env_file),
    )
    for name, value in config.security.extra_environment.items():
        entry = canonical_collision(value, protected, system=system)
        if entry is None:
            entry = denied_read_path_collision(
                value,
                clone,
                config.security.denied_read_paths,
                canonical=True,
                system=system,
            )
        if entry is not None:
            issues.append(
                f"security.extra_environment.{name}: the assigned path resolves onto "
                f"{entry.label} ({entry.path.as_posix()}); choose a separate toolchain directory"
            )
    if issues:
        raise ConfigError(issues)


def _allowed_environment_pattern_lines(config: OrchestratorConfig) -> list[str]:
    """Preflight report lines for the ``allowed_environment`` prefix patterns (empty when none).

    Host-specific by nature — the same pattern resolves to different names on different machines —
    which is exactly why this belongs to preflight and not to ``validate_config``. The run itself
    announces the same expansion once at start of flow, from the same formatter.
    """
    _, expansions = expand_allowed_environment(config.security.allowed_environment)
    described = describe_expansions(expansions)
    if not described:
        return []
    forwarded = sum(len(item.kept) for item in expansions)
    dropped = sum(len(item.dropped) for item in expansions)
    header = (
        f"allowed-environment: {len(expansions)} prefix pattern(s) — {forwarded} name(s) forwarded"
    )
    if dropped:
        header += f", {dropped} dropped as secret-named"
    if config.security.strict_isolation:
        header += (
            "; applies to orchestrator git/gh and strict-mode agent children (agent children also "
            "withhold env-file names matched only by a prefix pattern)"
        )
    else:
        header += (
            "; gates orchestrator git/gh only — advanced-mode agent/check/tool children receive "
            "the parent environment whole except variables loaded from the env-file"
        )
    return [header, *(f"  - {line}" for line in described)]


def _assigned_path_lines(
    config: OrchestratorConfig, *, env_file: Path | None
) -> tuple[bool, list[str]]:
    """The host-specific verdict on where ``security.extra_environment`` values point.

    Three things only this side of the gate can decide, because each needs the filesystem or the
    environment of *this* machine:

    * a value that reaches a protected directory through a **symlink**, a Windows case variant, or a
      drive-letter/UNC alias of the same path — and the resolved env-file, which is host state, not
      config. The
      plain-path half of this is a load error already, so a config cannot be valid-here-only;
    * a value pointing **into the clone**, which is the recipe that makes a toolchain cache work at
      all: the orchestrator excludes it from git itself, and only reports a failure if the path is
      still not ignored afterwards. Without that the cache's thousands of files land in the next
      task's diff and trip a gate that has nothing to do with caches. No clone on disk yet, nothing
      to exclude — the step is skipped, and says so;
    * a value pointing **outside** the clone, which is a warning rather than a failure: the path may
      be perfectly deliberate, but a sandboxed node cannot write there, so a build using it fails
      with a permission error that reads like a broken toolchain.

    Every line names the *variable*, never its value — an operator reads the value in their own
    config file, and a value holding a secret against the guide's advice must not gain a terminal or
    a CI log to leak from. A line does name the protected path it collided with, which is
    orchestrator-owned and is the one thing the operator cannot infer.
    """
    protected = host_protected_paths(
        config,
        build_internal_deny_policy(layout_for(config), env_file=env_file),
    )
    clone = Path(config.repo.local_path)
    clone_present = clone.is_dir()
    ok = True
    lines: list[str] = []
    for name, value in config.security.extra_environment.items():
        entry = canonical_collision(value, protected)
        if entry is None:
            entry = denied_read_path_collision(
                value,
                clone,
                config.security.denied_read_paths,
                canonical=True,
            )
        if entry is not None:
            ok = False
            lines.append(
                f"assigned-paths: FAIL — {name} resolves onto {entry.label} "
                f"({entry.path.as_posix()}), or inside it; the orchestrator's own state lives "
                "there and a toolchain writing into it corrupts the run or the repository"
            )
            continue
        elements = assigned_path_elements(value, include_unsplit=False)
        inside = [element for element in elements if is_inside(element, clone)]
        outside = len(elements) - len(inside)
        if outside and config.security.strict_isolation:
            lines.append(
                f"assigned-paths: WARN — {name} points outside the clone; a node running under the "
                "sandbox can only write inside the clone, so a build using that path fails with a "
                "permission error that looks like a broken toolchain"
            )
        if not inside:
            continue
        if not clone_present:
            lines.append(
                f"assigned-paths: SKIP — {name} points into the clone, which is not on disk yet, "
                "so it cannot be excluded from git; re-run this check once the first task has "
                "cloned the repository"
            )
            continue
        excluded = [
            element
            for element in inside
            if not ensure_path_excluded(clone, Path(element).expanduser(), security=config.security)
        ]
        if excluded:
            ok = False
            lines.append(
                f"assigned-paths: FAIL — {name} points into the clone but git still does not "
                "ignore it, so a filled cache would land in the task's diff. A tracked path, or a "
                "negation rule in the repository's ignore files, overrides the exclusion — move "
                "the cache to a path of its own"
            )
        else:
            lines.append(
                f"assigned-paths: OK — {name} points into the clone and git ignores it (excluded "
                "via .git/info/exclude), so a filled cache stays out of the task's diff"
            )
    return ok, lines


def _append_isolation_probe_lines(
    lines: list[str],
    pid: ProviderId,
    report: IsolationCapabilityReport | None,
    ok: bool,
    *,
    has_fallback: bool,
    advanced_mode: bool,
) -> bool:
    """Render one isolation-probe verdict (free smoke or paid probe) and return the new ``ok``.

    Shared by both so the two probes cannot drift into different severities for the same answer: a
    proven policy leak is unconditionally fatal (a non-fallback security result), while a probe that
    could not demonstrate the policy degrades like a capability gap. ``None`` means the provider
    offers no such probe — not a verdict, so nothing is printed.

    In the advanced mode an undemonstrable probe is a warning even with no fallback provider (owner
    decision 2026-08-20, applied identically in the per-attempt canary): the host class that answers
    "cannot demonstrate" — native Windows without the elevated Codex backend — is exactly the one
    the mode exists to keep working, and a preflight stop there made the mode unavailable on that
    host while proving nothing. Under strict isolation the old rule stands: with no fallback to
    cover the gap, an unprovable sandbox fails preflight.
    """
    if report is None:
        return ok
    if report.ok:
        lines.append(f"{pid.value}: isolation probe OK — {report.detail}")
        return ok
    if report.fatal:
        lines.append(f"{pid.value}: FAIL — isolation probe: {report.detail}")
        return False
    if not has_fallback and not advanced_mode:
        lines.append(f"{pid.value}: FAIL — isolation probe: {report.detail}")
        return False
    cover = (
        "a fallback provider will cover"
        if has_fallback
        else "strict_isolation is off, so the run continues with the floor unproven"
    )
    lines.append(f"{pid.value}: WARN — isolation probe: {report.detail} ({cover})")
    return ok


#: The Claude CLI environment variable that changes what a generated settings file MEANS: in its
#: env-scrub branch a volume-wide ``allowWrite`` entry is filtered out by name, so the advanced
#: mode's write grant quietly does not apply. Named here because ``worc preflight`` is the one place
#: that announces that grant, and in the mode the parent environment reaches the agent whole.
_CLAUDE_ENV_SCRUB_VAR = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"


def _provider_binary_lines(config: OrchestratorConfig, *, which: Which = shutil.which) -> list[str]:
    """One diagnostic line per configured provider: where its CLI binary really lies.

    Prints the launch path (the same ``resolve_launcher`` answer the run pins into ``argv[0]``),
    the real file behind it after following symlinks, and whether that file falls inside the
    provider's own config home. The standalone-package layout is the one fact that explains why the
    same build behaves differently on two hosts — Codex keeps its binary inside ``$CODEX_HOME``
    there — and learning it must not require reading a failed attempt's stderr (Т5.9). Diagnostic
    only: no line is a verdict, and nothing here fails preflight. On Windows ``which`` may answer
    with a ``.cmd`` shim whose contents ``resolve()`` does not chase; the line then truthfully
    reports the shim, which is the file the OS executes.
    """
    resolvers: dict[ProviderId, Callable[[], Path]] = {
        ProviderId.CLAUDE: claude_config_home,
        ProviderId.CODEX: codex_config_home,
    }
    lines: list[str] = []
    for pid, provider_cfg in config.agents.providers.items():
        subject = f"{pid.value}-binary"
        resolved = resolve_launcher(provider_cfg.command, which=which)
        if resolved is None:
            lines.append(f"{subject}: {provider_cfg.command!r} does not resolve on PATH")
            continue
        try:
            real = Path(resolved).resolve()
        except OSError:
            real = Path(resolved)
        head = f"{subject}: {resolved} — "
        if real != Path(resolved):
            head = f"{subject}: {resolved} — the file it runs is {real}, "
        resolver = resolvers.get(pid)
        if resolver is None:  # defensive: a provider id with no home resolver bound
            lines.append(f"{subject}: {resolved}")
            continue
        try:
            home = resolver()
        except (RuntimeError, OSError) as exc:
            lines.append(
                f"{head}the provider's config home could not be resolved ({exc}), so whether "
                "the binary lies inside it is unknown"
            )
            continue
        if real.is_relative_to(home):
            lines.append(
                f"{head}inside the provider's config home ({home}): whatever covers that home "
                "covers the binary itself"
            )
        else:
            lines.append(f"{head}outside the provider's config home ({home})")
    return lines


def _gh_repo_pin_line(config: OrchestratorConfig) -> tuple[bool, str]:
    """``(ok, line)`` for the ``gh --repo`` pin verdict.

    Fatal only where the configuration actually needs GitHub: with ``create_pull_request`` on, an
    unpinnable repository means the run would open its pull request against whatever ``gh`` infers,
    so learning that here costs nothing and learning it at publish time costs a PR. Otherwise it is
    a warning that says plainly which promise is off, rather than a refusal over a capability this
    configuration never uses.
    """
    slug, source = gh_repo_pin(config.repo.url, config.repo.local_path, security=config.security)
    if slug is not None:
        return True, (
            f"gh-repo-pin: OK ({source}) — every gh call names {slug} outright, so a planted "
            "gh config or an insteadOf rewrite cannot retarget it"
        )
    detail = (
        "repo.url names no hosted OWNER/REPO (an ssh alias, a file:// URL or a local path), so no "
        "gh call can be pinned with --repo: gh would infer the repository from the clone, which is "
        "the surface the control-state fingerprint exists to watch — the probe that decides which "
        "pull request this task appends to included"
    )
    if config.git.create_pull_request:
        return False, (
            f"gh-repo-pin: FAIL — {detail}. This configuration opens pull requests "
            "(git.create_pull_request: true), so set repo.url to the https://host/owner/name form"
        )
    return True, (
        f"gh-repo-pin: WARN — {detail}. This configuration opens no pull requests, so nothing is "
        "blocked; floor 4's 'every gh call names its repository outright' does not hold here"
    )


def run_preflight(
    config: OrchestratorConfig,
    *,
    env_file: Path | None = None,
    capability_smoke: bool = False,
    paid_isolation_probe: bool = False,
) -> tuple[bool, list[str]]:
    """Compute the preflight verdict + report lines; no task is processed.

    Read-only with one deliberate exception: an assigned toolchain cache inside the clone has its
    exclusion repaired in that clone's untracked ``.git/info/exclude``. Reporting "your cache will
    pollute the diff" without fixing what the orchestrator can fix would put the work on the
    operator for no reason. Nothing tracked is touched, so no diff or pull request changes.

    Runs every allowed provider's ``preflight()`` (``<cli> --version``) and the deterministic
    ``check_isolation`` policy check. Returns ``(ready, lines)`` where ``ready`` is true iff every
    provider is healthy and the required isolation can be enabled. Lines are secret-free by
    contract. Shared by ``cmd_preflight`` and the installer's post-write auto-preflight.
    ``env_file`` is the resolved ``.env`` path (already loaded at startup); its status is reported
    as a health line here — the only place the ``.env`` notice appears.

    ``capability_smoke`` (set only by ``worc preflight``, never the installer's auto-run) opts into
    each healthy provider's live no-model isolation capability probe: Codex runs a real
    ``codex sandbox`` smoke of the generated profile so an old CLI / missing sandbox helper /
    mis-generated policy surfaces here rather than mid-run. A proven policy leak fails preflight
    unconditionally; an undemonstrable sandbox degrades like a capability gap (fatal only with no
    fallback provider).
    """
    lines: list[str] = [_env_preflight_line(env_file)]
    providers = build_providers(config, layout=layout_for(config))
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
            f"(version={health.version or 'unknown'}{_auth_report_field(health.auth)})"
        )
        # A logged-out provider is fatal in ANY role, unlike the fallback-aware degradations below.
        # Kept off ``healthy`` deliberately: that flag also gates the opt-in capability smoke, and
        # an OK version line followed by its own FAIL reason is the shape already set below.
        auth_ok, auth_line = _auth_verdict(pid, health.auth)
        ok = ok and auth_ok
        if auth_line is not None:
            lines.append(auth_line)
        # Advisory degradations are fatal only when this provider has no fallback (it is the sole
        # allowed provider), else a warning — a fallback provider will cover the degraded nodes.
        has_fallback = any(other != pid for other in config.agents.allowed)
        for reason in health.degraded_reasons:
            if has_fallback:
                lines.append(f"{pid.value}: WARN — {reason} (a fallback provider will cover)")
            else:
                ok = False
                lines.append(f"{pid.value}: FAIL — {reason} (no fallback provider)")

        # Live no-model isolation capability smoke (Codex ``codex sandbox``), opt-in via ``worc
        # preflight`` for any healthy provider — including one under `strict_isolation: false`,
        # where the generated profile is what the local floor rests on. A proven leak is
        # unconditionally fatal (non-fallback security result); an undemonstrable sandbox degrades
        # like a capability gap (fatal only with no fallback provider).
        if capability_smoke and healthy:
            smoke = getattr(provider, "isolation_capability_smoke", None)
            report = smoke(home_dir=Path.home()) if callable(smoke) else None
            ok = _append_isolation_probe_lines(
                lines,
                pid,
                report,
                ok,
                has_fallback=has_fallback,
                advanced_mode=not config.security.strict_isolation,
            )

        # The paid probe (Claude): a separate opt-in because it spends a real model call. Same
        # verdict handling as the free smoke — a proven leak is fatal, an undemonstrable probe is
        # advisory — and, crucially, "the agent wrote nothing at all" reports as undemonstrable
        # rather than as a pass. Ungated on `strict_isolation` for the same reason as the smoke.
        if paid_isolation_probe and healthy:
            paid = getattr(provider, "paid_isolation_probe", None)
            report = paid(home_dir=Path.home()) if callable(paid) else None
            ok = _append_isolation_probe_lines(
                lines,
                pid,
                report,
                ok,
                has_fallback=has_fallback,
                advanced_mode=not config.security.strict_isolation,
            )

    # Where each provider binary really lies (Т5.9): informational lines, never a verdict.
    lines.extend(_provider_binary_lines(config))

    reasons = check_isolation(config, ISOLATION_CHECKS)
    if reasons:
        ok = False
        lines.append("isolation: FAIL")
        lines.extend(f"  - {reason}" for reason in reasons)
    else:
        enforced = "enforced" if config.security.strict_isolation else "strict_isolation=false"
        lines.append(f"isolation: OK ({enforced})")

    # Loudly surface the operator's read-isolation escape hatch — never a silent weakening. One
    # line: what it does and does not open is `guide/config/security.md`'s job, not every report's.
    if config.security.read_isolation_off:
        why = (
            "security.disable_read_isolation=true"
            if config.security.strict_isolation
            else "strict_isolation=false"
        )
        lines.append(f"read-isolation: OFF ({why})")

    # What this host cannot enforce, whatever the config says. Deliberately not a FAIL: the floor
    # is missing either way, and refusing to run would leave the operator without the guarantee AND
    # without the work. The same text lands in the run log, from the same formatter.
    floor_gaps = describe_host_floor(config, HOST_FLOOR_CHECKS)
    lines.extend(f"isolation-floor: NONE — {gap}" for gap in floor_gaps)
    # Ам1-6: the price of making that verdict advisory instead of fatal. Dropping the preflight stop
    # was justified by "a node can still fall back to the other provider" — a compensation that does
    # not exist when only one provider is allowed. Under strict isolation the attempt that needs a
    # sandboxed shell is then refused mid-run with nothing to cover it, and preflight said `ready`.
    # Still not a FAIL (the host verdict is advisory by decision), but said out loud here.
    if floor_gaps and len(config.agents.allowed) == 1 and config.security.strict_isolation:
        lines.append(
            "isolation-floor: WARN — this host cannot enforce the write floor and "
            f"{config.agents.allowed[0].value} is the only allowed provider, so a node needing a "
            "sandboxed shell will be refused mid-run (CAPABILITY_UNAVAILABLE) with no fallback to "
            "cover it. Allow a second provider, install the missing sandbox dependencies, or set "
            "security.strict_isolation: false and read guide/config/security.md for what that mode "
            "holds instead"
        )

    # The mode itself: the loudest line in the report, from the shared formatter so the run log
    # says the same thing. Placed after the host-floor lines because the floor those lines qualify
    # is the one the mode's line points at, and never a FAIL — the operator chose this, and
    # refusing to report on a configuration the run accepts is what produced the `isolation:`
    # disagreement this phase also fixed.
    mode_lines = describe_advanced_mode(config)
    lines.extend(mode_lines)

    # The one environment variable that silently changes what the mode's write grant means. In the
    # CLI's env-scrub branch the settings compiler filters a volume-wide `allowWrite` entry by name,
    # so with this set the grant this report just announced is not the grant the agent gets — and in
    # the mode the parent environment is forwarded whole, so it takes no config change to arrive.
    # A warning, not a failure: a narrower write grant is not a security problem, it is a
    # correctness surprise (a toolchain cache stops being writable and the build looks broken).
    if mode_lines and os.environ.get(_CLAUDE_ENV_SCRUB_VAR):
        lines.append(
            f"write-grant: WARN — {_CLAUDE_ENV_SCRUB_VAR} is set in this environment, and the "
            "Claude CLI filters the volume-wide write grant out of its settings in that branch. "
            "The mode's write grant then does not apply as documented: expect writes outside the "
            "clone to be refused, and unset the variable if you meant the grant to apply"
        )

    # Same principle for the git-evidence grant: an operator reading preflight should see which
    # optional capabilities are live, not have to infer them from the config file. The mode makes
    # the grant inert (every node has an unscoped shell there), which the line has to say — else it
    # announces a capability it did not add.
    if config.security.allow_git_evidence:
        inert = "" if config.security.strict_isolation else " — inert under strict_isolation=false"
        lines.append(f"git-evidence: ON (security.allow_git_evidence=true){inert}")

    # The host-dependent half of the ``allowed_environment`` gate — its host-independent half
    # (``PATH`` is mandatory) is a validator error, because one config file must get the same
    # verdict on every machine. FAIL rather than WARN: the CLI would not start at all, and this is
    # the one place where learning that costs nothing.
    #
    # Advanced mode widens agent-side children only. Orchestrator-owned git/gh keeps this allowlist,
    # so the Windows launch floor is checked at both strict-isolation values.
    env_issue = launch_critical_env_issue(config.security.allowed_environment)
    if env_issue is not None:
        ok = False
        lines.append(f"allowed-environment: FAIL — {env_issue}")

    # What each prefix pattern actually matched ON THIS HOST — the one place the width of a pattern
    # is visible before it is used. A pattern that resolved to nothing is the interesting case and
    # is printed like the others; never a FAIL, since an uninstalled toolchain is legitimate.
    lines.extend(_allowed_environment_pattern_lines(config))

    # Assigned variables are announced by NAME only. The values are in the operator's own config
    # already, and printing them would hand a secret that landed there against the guide's advice a
    # second surface (a terminal, a CI log) to leak from.
    if config.security.extra_environment:
        names = ", ".join(config.security.extra_environment)
        lines.append(
            f"extra-environment: {len(config.security.extra_environment)} assigned "
            f"({names}) — agent/check/tool children receive these; orchestrator git/gh receives "
            "only names not removed by its publication-retargeting scrub. Values are not printed"
        )
        paths_ok, path_lines = _assigned_path_lines(config, env_file=env_file)
        ok = ok and paths_ok
        lines.extend(path_lines)

    lines.extend(_summarize_command_sets(config))

    # Preflight is a run-surface health gate — it deliberately does not validate flows. Flow
    # correctness is an on-demand, operator-scoped concern handled by ``worc validate-flow`` over
    # ``.worc/flows/``, and every dispatched flow is validated fatally at task time by
    # ``FlowRegistry.resolve`` regardless (a broken flow fails that task, not the whole gate).

    if config.git.create_pull_request:
        gh_ok, gh_line = preflight_gh(config.security)
        if not gh_ok:
            ok = False
        lines.append(gh_line)

    # Whether every `gh` call can actually name its repository — the second half of floor 4, which
    # switches off silently when the configured URL names no hosted repository (an ssh alias, a
    # `file://` URL, a local path). Without the pin `gh` infers the repository from the clone, i.e.
    # from the very surface the fingerprint exists to watch, the pull-request reuse probe included.
    # Printed at every isolation setting: the pin is not a mode feature.
    pin_ok, pin_line = _gh_repo_pin_line(config)
    ok = ok and pin_ok
    lines.append(pin_line)

    tg_ok, tg_line = check_telegram_preflight(config.telegram)
    if not tg_ok:
        ok = False
    lines.append(tg_line)

    lines.append(f"preflight: {'ready' if ok else 'NOT ready'}")
    return ok, lines


def cmd_preflight(args: argparse.Namespace) -> int:
    """Report readiness without running a task (may repair clone-local ignore rules)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    env_file, _ = resolve_env_file_path(args)
    # ``worc preflight`` opts into the live no-model capability smoke; the installer's
    # auto-preflight (``_install_run_preflight``) keeps the default (offline) to stay fast. The paid
    # probe is a second, explicit opt-in: it spends a real model call, so nothing may imply it.
    ok, lines = run_preflight(
        config,
        env_file=env_file,
        capability_smoke=True,
        paid_isolation_probe=bool(getattr(args, "paid_isolation_probe", False)),
    )
    for line in lines:
        print(line)
    return 0 if ok else 1


def cmd_validate_flow(args: argparse.Namespace) -> int:
    """Validate operator flow(s) in ``.worc/flows/`` config-aware, on demand (read-only).

    Scoped to the operator's own flows — packaged built-ins are excluded (they are covered by the
    orchestrator's test suite, not by validating them against this repo's config). Runs the full
    fatal validator (graph + ceiling + the config-aware layer, incl. the ``.worc/tools/`` tool
    check) so it catches exactly what the engine sees at dispatch, plus the non-fatal prompt-var
    lint (WARN). Exit ``0`` = every checked flow valid, ``1`` = any invalid, ``2`` = name not found,
    usage error, or config load error.
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    registry = FlowRegistry(
        operator_flows_dir=layout_for(config).control_home / "flows", config=config
    )
    available = registry.operator_flow_names()
    if args.all_flows and args.name is not None:
        print("validate-flow: pass a flow NAME or --all, not both")
        return 2
    if args.all_flows:
        names = available
        if not names:
            print("validate-flow: no operator flows in .worc/flows/")
            return 0
    elif args.name is not None:
        stem = args.name.removesuffix(".yaml")
        if stem not in available:
            print(f"validate-flow: flow {stem!r} not found in .worc/flows/")
            return 2
        names = [stem]
    else:
        print("validate-flow: specify a flow NAME or --all")
        return 2

    ok = True
    for check in registry.check_flows(names):
        if check.error is None:
            print(f"flow {check.name}: OK")
        else:
            ok = False
            print(f"flow {check.name}: FAIL — {check.error.splitlines()[0]}")
        for warning in check.warnings:
            print(f"flow {check.name}: WARN — {warning} (renders verbatim to the agent)")
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

    The looping daemon writes ``<artifacts_root>/orchestrator.pid`` and shuts down cooperatively at
    the next flow-node boundary when ``stop``/``restart`` ask it to: via a ``SIGTERM``-set event on
    POSIX and an ``orchestrator.stop`` sentinel everywhere. It refuses to start when another
    watcher is recorded for the same artifact root, and removes its PID file on exit (how ``stop``
    confirms it).
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    require_launch_environment(config, env_file=resolve_env_file_path(args)[0])
    preflight.require_git_control()  # git must honor `core.hooksPath` (>= 2.9)
    if config.git.create_pull_request:
        preflight.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish
        # Non-blocking advisory if gh is present but logged out. The policy travels with it so
        # the probe sees the same environment (a proxy, a token) every other gh call gets.
        preflight.warn_if_gh_logged_out(security=config.security)
    # A node may route to ANY allowed provider, so one that cannot start is refused up front
    # rather than discovered at the first fallback with a stage's work already spent.
    require_provider_auth(config)
    poll = (
        args.poll_seconds
        if args.poll_seconds is not None
        else config.orchestrator.poll_interval_seconds
    )
    folder = pending_dir(config)
    pid_path = process_control.pid_file_path(worc_home_for(config))
    stop_path = process_control.stop_file_path(worc_home_for(config))
    children_path = process_control.children_file_path(worc_home_for(config))
    cleanup_hook = _build_cleanup_hook(config)  # None when memory is disabled

    # Single pass: no PID file, no signal handler, no stop wiring — and NO agent-handle recorder, so
    # it never clobbers a concurrent daemon's children file. A hung single-pass agent is still
    # reaped by run_process's own timeout/Ctrl-C subtree-kill.
    if poll <= 0:
        orchestrator = build_orchestrator(
            config,
            layout=layout_for(config),
            env_file=resolve_env_file_path(args)[0],
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
        layout=layout_for(config),
        env_file=resolve_env_file_path(args)[0],
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
    """The stop ladder: an explicit ``--force-full`` first, then whether a task is active.

    ``--force-full`` → hard (full), idle or busy: the flag is the operator naming the hardest rung,
    and it outranks the activity probe because a daemon that needs it is usually **idle** — wedged,
    suspended, or stuck in a syscall — so keying the rung on activity removes it exactly where it is
    needed. Otherwise, keyed on a read-only ``find_active_tasks`` probe: idle → ordinary (soft)
    stop, no prompt; busy + no flag → refuse (interactive: confirm the literal ``YES`` → soft;
    non-interactive: exit non-zero, require a flag); busy + ``--force`` → soft.
    """
    if force_full:
        return _StopDecision(proceed=True, level="full")  # explicit hardest rung; never downgraded
    if not has_active_task(config):
        return _StopDecision(proceed=True, level="soft")  # nothing in flight: any form just stops
    if force:
        return _StopDecision(proceed=True, level="soft")
    if interactive:
        if _confirm_yes(
            "a task is active. YES = soft stop (lets the current flow node finish, then exits); "
            "to interrupt the running agent NOW use --force-full: "
        ):
            return _StopDecision(proceed=True, level="soft")
        return _StopDecision(proceed=False, message="stop: aborted", exit_code=0)
    return _StopDecision(
        proceed=False,
        message=(
            "stop: a task is active; pass --force (soft: finishes the current flow node) or "
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
        # prompt_toolkit REPL (the single-stdin-reader rule).
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


def _timed_out_stop_message(pid: int | None, timeout: float, *, is_windows: bool) -> str:
    """Operator message for a soft stop that did not confirm shutdown within ``timeout``.

    The graceful stop stays **pending**: POSIX never kills, and Windows keeps every handle when no
    hard-kill seam is available. The PID and stop-file remain intact, so the daemon still exits at
    its next node boundary, a second watcher cannot start, and ``--force-full`` remains the only
    immediate interrupt.

    Where the state is free to read (Linux ``/proc``), a watcher still stopped after the soft path's
    own SIGCONT is named as such rather than left to read as "busy" — the operator's own
    ``kill -CONT`` is then the cheapest resolution, and it exits cleanly instead of being killed.
    """
    base = (
        f"stop: watcher {pid} did not confirm shutdown in {timeout:g}s; "
        "graceful stop is still pending (kept its PID file)"
    )
    if pid is not None and process_control.read_process_state(pid) == "T":
        return (
            f"{base}; it is suspended (state T), not busy — resume it with `kill -CONT {pid}` "
            "and the pending stop completes, or use --force-full to kill it"
        )
    if is_windows:
        return f"{base}; retry with --force-full to kill its process tree"
    return f"{base}; retry with --force-full to interrupt now and reap the agent subtree"


def _has_unconfirmed_runtime_handles(
    config: OrchestratorConfig, outcome: process_control.StopOutcome
) -> bool:
    """Whether a PID-less stop preserved handles that make shutdown ambiguous.

    POSIX reaps stale handles when no PID is recorded. On Windows they deliberately survive because
    the missing PID may be the residue of an older timed-out stop while the watcher is still alive.
    Treat either handle as unconfirmed so ``restart`` never clears the sentinel by starting a second
    watcher.
    """
    if outcome.found:
        return False
    root = worc_home_for(config)
    return (
        process_control.stop_file_path(root).exists()
        or process_control.children_file_path(root).exists()
    )


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop a running ``watch`` daemon via the stop ladder (idle: no prompt; busy: confirm/force).

    Soft (default / ``--force`` / typed ``YES``) finishes the current flow node, then exits;
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
    if _has_unconfirmed_runtime_handles(config, outcome):
        print(
            "stop: no watcher PID; preserved pending stop/child handles because shutdown is "
            "unconfirmed"
        )
    elif not outcome.found:
        print("stop: no running watcher (no PID file)")
    elif outcome.already_dead:
        print(f"stop: no running watcher (cleared stale PID {outcome.pid})")
    elif outcome.group_killed:
        print(
            f"stop: watcher {outcome.pid} hard-stopped (killed its process group); "
            "it resumes from its checkpoint on next start"
        )
    elif outcome.tree_killed:
        reason = " after the graceful timeout" if outcome.timed_out else ""
        print(
            f"stop: watcher {outcome.pid} hard-stopped{reason} (killed its process tree); "
            "it resumes from its checkpoint on next start"
        )
    elif outcome.timed_out:
        print(_timed_out_stop_message(outcome.pid, args.timeout, is_windows=os.name == "nt"))
    else:
        print(f"stop: watcher {outcome.pid} stopped")
    note = _parked_slot_note(config)
    if note:
        print(note)
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    """Move a staged task file from ``tasks/preparing/`` into ``tasks/pending/`` (atomic rename).

    The daemon never scans ``preparing/``, so a file can be composed there without a mid-write
    pickup; ``promote`` is the explicit "it's ready" step. A decomposition root pulls the subtask
    specs it references; ``--all`` promotes everything staged (specs first, roots last).
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    moved, errors = promote_tasks(config, target=args.target, all_files=args.all_files)
    for name in moved:
        print(f"promote: {name} -> pending")
    for err in errors:
        print(f"promote: {err}")
    return 1 if errors else 0


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
    if _has_unconfirmed_runtime_handles(config, outcome):
        print(
            "restart: no watcher PID but pending stop/child handles remain; shutdown is "
            "unconfirmed and no replacement was started"
        )
        return 1
    if outcome.degraded_to_soft:
        print("restart: hard stop (--force-full) is unavailable on Windows; doing a soft stop")
    if not outcome.found or outcome.already_dead:
        print("restart: no previous watcher running")
    elif outcome.group_killed:
        print(f"restart: hard-stopped previous watcher {outcome.pid} (process group)")
    elif outcome.tree_killed:
        reason = " after the graceful timeout" if outcome.timed_out else ""
        print(f"restart: hard-stopped previous watcher {outcome.pid}{reason} (process tree)")
    elif outcome.timed_out:
        print(
            f"restart: previous watcher {outcome.pid} did not confirm shutdown; "
            "kept its PID file and did not start a replacement"
        )
        return 1
    else:
        print(f"restart: stopped previous watcher {outcome.pid}")
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
    daemon_alive = _daemon_alive(config)
    for index, task in enumerate(tasks):
        if index:
            print()
        print(f"task_id={task.task_id}")
        print(f"title={task.title}")
        print(f"status={_display_status(task, daemon_alive=daemon_alive)}")
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


def _task_entry(row: TaskRow, *, daemon_alive: bool = True) -> dict[str, str | None]:
    # Status label via the shared renderer: a RUNNING row shows "parked (no daemon)" with no live
    # daemon, else "(paused)" on a B-lite provider-outage park, else the plain status. The
    # daemon_alive default keeps terminal rows (recent/all) and any direct caller unchanged.
    return {
        "task_id": row.task_id,
        "status": _display_status(row, daemon_alive=daemon_alive),
        "title": row.title,
        "branch": row.branch,
    }


def _pending_entry(path: Path, scan: _PendingScan, rank: int) -> dict[str, str | None]:
    # A queued file has no DB row yet, so this view is file-derived. It carries the scheduler's own
    # ranking — the 1-based rank position plus the priority/queue it sorted on — so the operator
    # reads the *run* order here, not the file manager's alphabetical listing. An unparseable file
    # has no id and is shown by filename instead.
    return {
        "task_id": scan.task_id,
        "status": "pending",
        "title": None,
        "branch": None,
        "file": path.name,
        "rank": str(rank),
        "priority": _PRIORITY_LABEL.get(scan.priority_rank, "mid"),
        "queue": scan.queue,
    }


def _entry_line(entry: dict[str, str | None]) -> str:
    status = entry["status"] or ""
    label = entry["task_id"] or entry.get("file") or "(unknown)"
    rank = entry.get("rank")
    if rank is not None:
        # A ranked (pending) row leads with its run position and the priority/queue it sorted on;
        # the "pending" status is implied by the section header, so it is dropped to keep the line
        # about the ordering.
        line = f"{rank + '.':<4} {entry.get('priority') or 'mid':<4}  {label}"
        line += f"  (queue={entry.get('queue') or DEFAULT_QUEUE})"
        return line
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
    status_label: str  # "running", "running (paused)" (B-lite park), or "parked (no daemon)"
    title: str | None
    branch: str | None
    current_node: str | None  # flow checkpoint: where the engine will resume
    fix_iterations: int
    subtask: str | None  # "2/5" for a decomposed task, else None
    parked_since: str | None  # tasks.blocked_since (ISO) when parked, else None
    # tasks.blocked_until (ISO) when a provider named its own reset instant, else None.
    parked_until: str | None
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
    daemon_alive = _daemon_alive(config)
    active: list[_ActiveView] = []
    for row in store.find_active_tasks() if store is not None else []:
        try:
            current_node = store.get_flow_checkpoint(row.task_id)[0] if store else None
        except KeyError:
            current_node = None  # row vanished between the two reads (terminal race)
        parked = row.blocked_since if (row.status is Status.RUNNING and row.blocked_since) else None
        parked_until = row.blocked_until if parked else None
        subtask = (
            f"{row.active_subtask}/{row.subtask_count}"
            if row.active_subtask is not None and row.subtask_count is not None
            else None
        )
        active.append(
            _ActiveView(
                task_id=row.task_id,
                status_label=_task_entry(row, daemon_alive=daemon_alive)["status"]
                or row.status.value,
                title=row.title,
                branch=row.branch,
                current_node=current_node,
                fix_iterations=row.fix_iterations,
                subtask=subtask,
                parked_since=parked,
                parked_until=parked_until,
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

# ANSI for `clear`: cursor home + clear screen + clear scrollback — wipes the visible console and
# its history. The trailing scrollback wipe is what sets it apart from the top loop's per-frame
# clear (which must keep scrollback). The shell's `clear` verb forwards to `worc clear` (cmd_clear).
_CLEAR_SCREEN = "\x1b[H\x1b[2J\x1b[3J"


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
                paused = f"    paused — every provider unavailable since {view.parked_since}"
                if view.parked_until:
                    paused += f"; next attempt at {view.parked_until}"
                lines.append(paused)
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


def cmd_clear(args: argparse.Namespace) -> int:
    """Clear the terminal screen (and scrollback). A visual wipe only — no logs or files are removed
    (``logs clean`` deletes on-disk logs). The shell's ``clear`` verb forwards here.
    """
    sys.stdout.write(_CLEAR_SCREEN)
    sys.stdout.flush()
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
    # Pending goes through the scheduler's own ranking (queue-filtered to this instance's selector,
    # then priority-ordered), so ``worc list`` shows the same sequence and membership as ``watch``
    # claims and ``top``/``ps`` display — never the raw, unfiltered file-manager order.
    pending = [
        _pending_entry(path, scan, rank)
        for rank, (path, scan) in enumerate(
            scan_pending_sorted(pending_dir(config), config.orchestrator.queue), start=1
        )
    ]
    # Only RUNNING rows are relabelled by daemon liveness; probe once and pass it to the sections
    # that can contain a RUNNING row (active/all). recent/pending are terminal/file-only.
    daemon_alive = _daemon_alive(config)
    if args.all:
        rows = store.all_tasks() if store else []
        return [("all", [_task_entry(r, daemon_alive=daemon_alive) for r in rows])]
    if args.pending:
        return [("pending", pending)]
    if args.recent is not None:
        rows = store.recent_tasks(args.recent) if store else []
        return [("recent", [_task_entry(r) for r in rows])]
    active = store.find_active_tasks() if store else []
    recent = store.recent_tasks(_LIST_RECENT_DEFAULT) if store else []
    return [
        ("active", [_task_entry(r, daemon_alive=daemon_alive) for r in active]),
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
    """Print the bare ids of the focused sections: the same disk+DB source as the table view,
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
    # When a section focus flag is combined with `--format ids`, derive the ids from the same
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
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _install_backup_config(path: Path) -> Path:
    """Copy an existing config to a timestamped ``.bak-<UTC>`` sibling and return that path.

    Only the newest few snapshots of this config are kept — the series is written by the
    orchestrator, so bounding it is the orchestrator's job.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.bak-{stamp}")
    backup.write_bytes(path.read_bytes())
    _prune_install_backups(path.parent, path.name)
    return backup


def _install_create_dirs(repo_local_path: Path) -> None:
    """Create the tracked task dirs at the repo root and the gitignored ``.worc/`` runtime dirs.

    Idempotent. The repo task dirs are created empty, so they do not appear in ``git status`` until
    a task writes into them; everything under ``.worc/`` is gitignored as a whole.
    """
    worc_home = RuntimeLayout.default(repo_local_path).control_home
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
    print(f"  would ignore {CONTROL_HOME_DIRNAME}/ via .gitignore")
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
    worc_home = RuntimeLayout.default(spec.repo_local_path).control_home.resolve()
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
    # `check_chapter`) land in .worc/tools/, delivered per machine so the launcher matches the OS.
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
        print(f"install: ignored {CONTROL_HOME_DIRNAME}/ via .gitignore")
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
        if args.command == "promote":
            return cmd_promote(args)
        if args.command == "restart":
            return cmd_restart(args)
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "validate-flow":
            return cmd_validate_flow(args)
        if args.command == "telegram-test":
            return cmd_telegram_test(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "top":
            return cmd_top(args)
        if args.command == "shell":
            return cmd_shell(args)
        if args.command == "clear":
            return cmd_clear(args)
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
        if args.command == "runs":
            return cmd_runs(args)
        if args.command == "memory":
            return cmd_memory(args)
    except (
        ConfigError,
        IncompatibleStateError,
        preflight.GhNotAvailableError,
        preflight.ProviderNotLoggedInError,
    ) as exc:
        print(f"error: {exc}")
        return 2
    except ManualActionRequired as exc:
        # A foreground command (e.g. `rerun`'s reset-to-base filter refuse-gate) hit a condition
        # that needs an operator to act. Surface it as a clean message + exit 2, not a traceback. In
        # the daemon/run paths catch ManualActionRequired internally and map it to a status,
        # so it never reaches here.
        print(f"manual action required: {exc}")
        return 2
    raise SystemExit(f"Unknown command '{args.command}'.")


if __name__ == "__main__":
    sys.exit(main())
