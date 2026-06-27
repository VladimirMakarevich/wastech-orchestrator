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
from typing import NamedTuple

from wastech_orchestrator import __version__, preflight, process_control
from wastech_orchestrator.config import upgrade as config_upgrade
from wastech_orchestrator.config.loader import ConfigError, load_config, loads_config
from wastech_orchestrator.config.schema import (
    CONFIG_SCHEMA_VERSION,
    OrchestratorConfig,
)
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.orchestrator import (
    Eligibility,
    FinalizePlan,
    Orchestrator,
    PipelineResult,
    RerunPlan,
    build_orchestrator,
    build_providers,
)
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.env_file import load_env_file
from wastech_orchestrator.git_manager import append_runtime_excludes
from wastech_orchestrator.install import config_writer, detect, wizard
from wastech_orchestrator.notify import build_notifier
from wastech_orchestrator.notify.telegram import check_telegram_preflight
from wastech_orchestrator.observability.logging import configure_logging
from wastech_orchestrator.preflight import preflight_gh
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

# Exit codes for a terminal pipeline outcome.
_EXIT_BY_STATUS: dict[Status, int] = {
    Status.DONE: 0,
    Status.FAILED: 1,
    Status.MANUAL_ACTION_REQUIRED: 2,
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
    "tasks/processing",
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
        default="info",
        help="structured operator log level (default: info)",
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
    their per-node role-prompt templates under ``roles/`` (works from a source tree or a wheel).

    ``install`` copies this whole tree into ``.worc/flows/`` so the operator gets editable, *active*
    copies: the registry prefers ``<repo>/.worc/flows/<task_type>.yaml`` over the packaged built-in,
    and a node's ``role_file`` resolves beside it under ``.worc/flows/roles/``. Unlike the generated
    ``guide/``, these are operator-editable, so a plain re-run never clobbers them (see
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


def _load_config(path: str) -> OrchestratorConfig:
    """Load and semantically validate the config (fail-closed)."""
    config = load_config(path).config
    validate_config(config)
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

    Emits a single secret-free notice (count + path) to stderr when it loads anything. A missing
    *explicit* ``--env-file`` is a fail-closed :class:`ConfigError` (exit 2); a missing
    auto-discovered ``.env`` is a silent no-op.
    """
    path, required = resolve_env_file_path(args)
    if path is None:
        return
    if not path.is_file():
        if required:
            raise ConfigError([f"--env-file not found: {path}"])
        return
    count = load_env_file(path)
    if count:
        print(f"env: loaded {count} variable(s) from {path}", file=sys.stderr)


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
    return _load_config(path)


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


def _configure_runtime_logging(args: argparse.Namespace) -> None:
    configure_logging(
        level=_LOG_LEVELS[args.log_level],
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
    rank = priority_rank(parse.frontmatter.get("priority"))
    raw_queue = parse.frontmatter.get("queue")
    queue = raw_queue.strip() if isinstance(raw_queue, str) and raw_queue.strip() else DEFAULT_QUEUE
    raw_deps = parse.frontmatter.get("depends_on", [])
    if not isinstance(raw_deps, (list, tuple)) or not all(
        isinstance(d, str) and d.strip() for d in raw_deps
    ):
        return _PendingScan(task_id, (), rank, queue)
    return _PendingScan(task_id, tuple(d.strip() for d in raw_deps), rank, queue)


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

    auto = config.orchestrator.auto_mode.enabled
    selector = queue if queue is not None else config.orchestrator.queue
    # Partition first: drop pending tasks tagged for another queue before ranking and before the
    # dependency map is built, so this instance only ever sees its own tasks.
    scans = [
        (p, s)
        for p, s in ((p, _scan_pending_meta(p)) for p in select_pending(folder))
        if s.queue == selector
    ]
    # Sort by (priority_rank, filename); select_pending is already filename-sorted, so the path tie
    # break preserves the deterministic order within a priority. pending_map is order-independent.
    scans.sort(key=lambda item: (item[1].priority_rank, item[0]))
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
        result = orchestrator.run_task(str(task_file))
        results.append(result)
        if result.final_status is Status.MANUAL_ACTION_REQUIRED:
            break  # a manual task blocks automatic continuation
        if not auto:
            break  # auto mode off: process exactly one task
    return results


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


def _report_rerun_plan(plan: RerunPlan) -> None:
    """Print the planned reconciliation for ``rerun --dry-run``; writes nothing."""
    mode = "continue" if plan.continue_mode else "fresh"
    current = plan.current_status.value if plan.current_status else "unknown"
    print(f"rerun (dry-run): would re-attempt {plan.task_id} [{mode}]")
    print(f"  current status: {current}")
    if plan.continue_mode:
        node = plan.interrupted_node or "unknown"
        print(f"  branch:    reuse {plan.branch or '(none)'}")
        print(f"  re-enter:  {node}")
        print("  artifacts: kept; pending HITL prompt reset so the node re-asks")
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

    mode = "continue" if args.continue_ else "fresh"
    if not args.yes and not _confirm(
        f"Rerun {args.task_id} [{mode}] from base '{plan.base_branch}'? [y/N] "
    ):
        print("rerun: aborted")
        return 0

    if args.continue_:
        result = orchestrator.continue_task(args.task_id)
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


def run_preflight(config: OrchestratorConfig) -> tuple[bool, list[str]]:
    """Compute the read-only preflight verdict + report lines; no task is processed.

    Runs every allowed provider's ``preflight()`` (``<cli> --version``) and the deterministic
    ``check_isolation`` policy check. Returns ``(ready, lines)`` where ``ready`` is true iff every
    provider is healthy and the required isolation can be enabled. Lines are secret-free by
    contract. Shared by ``cmd_preflight`` and the installer's post-write auto-preflight.
    """
    lines: list[str] = []
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
    ok, lines = run_preflight(config)
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
        print(f"{result.task_id}: {result.final_status.value}")
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

    # Only the looping mode is a daemon; refuse a second watcher for the same artifact root. A stale
    # PID file (process gone) is overwritten on start.
    if poll > 0:
        existing = process_control.running_daemon_pid(pid_path)
        if existing is not None:
            print(
                f"watch: already running (pid {existing}); stop it first with "
                f"'wastech-orchestrator stop', or use 'restart' ({pid_path})"
            )
            return 1

    orchestrator = build_orchestrator(
        config,
        artifacts_root=worc_home_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
    )

    # Single pass: no PID file, no signal handler.
    if poll <= 0:
        return _summarize_watch(
            watch_loop(orchestrator, config, folder, poll_interval=poll, queue=args.queue)
        )

    print(f"watch: polling every {poll}s for git-pushed tasks (Ctrl-C or 'stop' to exit)")
    results: list[PipelineResult] = []
    stopped = False
    controller = process_control.StopController()  # SIGTERM -> event, restored on exit
    try:
        with controller:
            stop_path.unlink(missing_ok=True)  # clear a stale sentinel so it can't stop us on start
            process_control.write_pid_file(pid_path)
            results = watch_loop(
                orchestrator,
                config,
                folder,
                poll_interval=poll,
                queue=args.queue,
                stop_event=controller.event,
                stop_file=stop_path,
            )
            # Graceful stop arrived via SIGTERM (event) or the stop-file (Windows / cross-shell).
            stopped = controller.event.is_set() or process_control.stop_file_requested(stop_path)
    except KeyboardInterrupt:
        print("watch: stopped")
        return 0
    finally:
        pid_path.unlink(missing_ok=True)  # clean exit, Ctrl-C, SIGKILL-survivor, or error
        stop_path.unlink(missing_ok=True)  # reap our own sentinel
    if stopped:
        print("watch: stopped")  # graceful shutdown (SIGTERM or stop-file)
        return 0
    return _summarize_watch(results)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop a running ``watch`` daemon (SIGTERM, then SIGKILL after ``--timeout``). Idempotent."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    pid_path = process_control.pid_file_path(worc_home_for(config))
    stop_path = process_control.stop_file_path(worc_home_for(config))
    outcome = process_control.stop_process(pid_path, timeout=args.timeout, stop_file=stop_path)
    if not outcome.found:
        print("stop: no running watcher (no PID file)")
    elif outcome.already_dead:
        print(f"stop: no running watcher (cleared stale PID {outcome.pid})")
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
    """Stop the running watcher (if any), then start a fresh ``watch`` with these flags.

    Targets the daemon recorded in the PID file (a different process), waits for it to exit, then
    runs its own loop in-process — it does not need to remember the old daemon's arguments.
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    pid_path = process_control.pid_file_path(worc_home_for(config))
    stop_path = process_control.stop_file_path(worc_home_for(config))
    outcome = process_control.stop_process(pid_path, timeout=args.timeout, stop_file=stop_path)
    if not outcome.found or outcome.already_dead:
        print("restart: no previous watcher running")
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
    return {
        "task_id": row.task_id,
        "status": row.status.value,
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
    try:
        if fmt == "ids":
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
    ok, lines = run_preflight(_load_config(str(config_path)))
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
    except (ConfigError, IncompatibleStateError, preflight.GhNotAvailableError) as exc:
        print(f"error: {exc}")
        return 2
    raise SystemExit(f"Unknown command '{args.command}'.")


if __name__ == "__main__":
    sys.exit(main())
