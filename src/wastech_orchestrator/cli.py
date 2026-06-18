"""CLI entry point.

``init`` scaffolds a project layout and templates; ``run`` processes one task end to end through
the Orchestrator Core; ``watch`` resumes any in-flight task and then processes pending tasks (one at
a time, continuing only when ``orchestrator.auto_mode.enabled``); ``status`` reads persisted
progress without starting work. See the spec §15.
"""

from __future__ import annotations

import argparse
import logging
import os
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

from wastech_orchestrator import __version__, process_control
from wastech_orchestrator.checks import diagnostics as check_diagnostics
from wastech_orchestrator.config import upgrade as config_upgrade
from wastech_orchestrator.config.loader import ConfigError, load_config, loads_config
from wastech_orchestrator.config.schema import (
    CONFIG_SCHEMA_VERSION,
    OrchestratorConfig,
)
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.core.orchestrator import (
    FinalizePlan,
    Orchestrator,
    PipelineResult,
    RerunPlan,
    build_orchestrator,
    build_providers,
)
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.git_manager import append_runtime_excludes
from wastech_orchestrator.install import config_writer, detect, wizard
from wastech_orchestrator.notify import build_notifier
from wastech_orchestrator.notify.telegram import check_telegram_preflight
from wastech_orchestrator.observability.logging import configure_logging
from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.security.isolation import check_isolation
from wastech_orchestrator.state_store import IncompatibleStateError, StateStore

# --log-level names → stdlib logging levels for the structured operator trace (§6.6).
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

_STATUS_STAGE: dict[Status, Stage] = {
    Status.REFINING: Stage.REFINEMENT,
    Status.PLANNING: Stage.PLANNING,
    Status.IMPLEMENTING: Stage.IMPLEMENTATION,
    Status.TESTING: Stage.TESTING,
    Status.REVIEWING: Stage.REVIEW,
    Status.FIXING: Stage.FIXING,
    Status.SUMMARIZING: Stage.SUMMARY,
    Status.READY_TO_PUBLISH: Stage.PUBLISHING,
    Status.COMMITTING: Stage.PUBLISHING,
    Status.PUSHING: Stage.PUBLISHING,
    Status.CREATING_PR: Stage.PUBLISHING,
}

# The orchestrator's runtime home inside the target repo (spec §21). Everything the orchestrator
# generates or installs lives under `<repo>/.worc/` — gitignored as a whole — except the audit
# trail: the task lifecycle dirs below sit at the repo root and are audit-committed.
WORC_HOME = ".worc"

# Task lifecycle dirs created at the repo root by `install` (tracked; the audit commit captures the
# task file + its `<id>.summary.md` in done/failed). `tasks/rejected` is the §19 quarantine and
# lives under `.worc/` instead, so rejected tasks are never swept into the audit commit.
REPO_TASK_DIRS: tuple[str, ...] = (
    "tasks/pending",
    "tasks/processing",
    "tasks/done",
    "tasks/failed",
)

# Runtime dirs created under `<repo>/.worc/` by `install` (all gitignored).
WORC_RUNTIME_DIRS: tuple[str, ...] = ("logs", "workspace", "checks", "tasks/rejected")


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
        "--check",
        action="append",
        default=None,
        metavar="COMMAND",
        help="a check command (repeatable); overrides ecosystem auto-detection",
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

    stop_cmd = sub.add_parser("stop", help="stop a running 'watch' daemon (SIGTERM, then SIGKILL)")
    stop_cmd.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="seconds to wait for graceful shutdown before SIGKILL (default: 30)",
    )

    restart_cmd = sub.add_parser(
        "restart", help="stop the running 'watch' daemon, then start a fresh one with these flags"
    )
    restart_cmd.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        metavar="SECONDS",
        help="seconds to wait for the previous watcher to exit before SIGKILL (default: 30)",
    )
    restart_cmd.add_argument(
        "--poll-seconds",
        type=int,
        default=None,
        metavar="N",
        help="override orchestrator.poll_interval_seconds for the new loop (0 = single pass)",
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
        "--keep-branch", action="store_true", help="keep the agent branch (the default; no-op)"
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
    """The packaged ``worc/`` agent task-authoring docs (works from a source tree or a wheel).

    These ship as package data next to ``templates/`` and are copied into ``.worc/guide/`` by
    ``install`` so an AI agent can author tasks from a local, self-contained guide. Unlike
    ``templates/``, they are generated content with no operator edits — ``upgrade-docs`` overwrites
    them to the packaged version.
    """
    return resources.files("wastech_orchestrator").joinpath("worc")


def _copy_worc_docs(dest_root: Path, *, overwrite: bool, dry: bool) -> tuple[list[str], list[str]]:
    """Copy the packaged ``worc/`` docs into ``dest_root/guide`` (the installed authoring guide).

    The packaged source dir is ``worc/``; it lands as ``guide/`` so the path reads ``.worc/guide/``
    rather than the redundant ``.worc/worc/``. Existing files are skipped unless ``overwrite``;
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


def _load_config(path: str) -> OrchestratorConfig:
    """Load and semantically validate the config (fail-closed, §11/§21.4)."""
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
    # Defensive: the regenerated config must load and pass §11/§21.4 before we touch the file.
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
    """The orchestrator's gitignored runtime home: ``<repo>/.worc/`` (§21).

    Everything the orchestrator generates — ``state.db``, ``logs/``, ``orchestrator.pid``,
    ``workspace/``, ``checks/``, the resolved check profile, validation reports — lives here, plus
    the installed ``config.yaml``, ``templates/``, and ``guide/``. The whole dir is gitignored.
    """
    return Path(config.repo.local_path) / WORC_HOME


def tasks_root_for(config: OrchestratorConfig) -> Path:
    """The repo root that holds the tracked ``tasks/`` lifecycle dirs (the audit trail, §21).

    Unlike :func:`worc_home_for`, ``tasks/`` stays at the repo root so the task file and its
    committed ``<id>.summary.md`` can be audit-committed into the repo's history.
    """
    return Path(config.repo.local_path)


def pending_dir(config: OrchestratorConfig) -> Path:
    """The folder ``watch`` scans for new tasks: ``<repo>/tasks/pending`` (§21)."""
    return tasks_root_for(config) / "tasks" / "pending"


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


def watch_once(
    orchestrator: Orchestrator, config: OrchestratorConfig, folder: Path
) -> list[PipelineResult]:
    """Resume any in-flight task, then process pending tasks per the auto-mode rule (§8.2, §8.3).

    Resumes the single active task first. Then picks pending tasks **only** when the slot is free;
    with auto mode off it processes exactly one, with auto mode on it continues to the next after a
    successful terminal cleanup. A ``manual_action_required`` outcome blocks further continuation.
    """
    results: list[PipelineResult] = []
    resumed = orchestrator.resume()
    if resumed is not None:
        results.append(resumed)
        if resumed.final_status is Status.MANUAL_ACTION_REQUIRED:
            return results

    auto = config.orchestrator.auto_mode.enabled
    for task_file in select_pending(folder):
        if not orchestrator.acquire_slot(""):
            break  # the slot is not free (an active task remains)
        result = orchestrator.run_task(str(task_file))
        results.append(result)
        if result.final_status is Status.MANUAL_ACTION_REQUIRED:
            break  # a manual task blocks automatic continuation (§8.3)
        if not auto:
            break  # auto mode off: process exactly one task
    return results


def watch_loop(
    orchestrator: Orchestrator,
    config: OrchestratorConfig,
    folder: Path,
    *,
    poll_interval: int,
    max_iterations: int | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    stop_event: threading.Event | None = None,
) -> list[PipelineResult]:
    """Run ``watch_once`` on a loop, refreshing the repo each tick (periodic discovery, §8.3).

    Each tick: ``refresh_repo`` (fetch + ff-only pull of ``base_branch`` when the slot is free, so a
    task pushed to git later becomes visible), then ``watch_once``, then sleep ``poll_interval``.
    ``poll_interval <= 0`` runs exactly one tick (single pass, no sleep). ``max_iterations`` bounds
    the loop for tests; in production the loop runs until interrupted.

    A ``stop_event`` (set by a ``SIGTERM`` handler) is honored only *between* ticks, so an in-flight
    task run finishes its current stage rather than being interrupted; when set, it also cuts the
    poll sleep short for a prompt shutdown. The ``sleep_fn`` path is kept for callers without an
    event (existing tests).
    """
    results: list[PipelineResult] = []
    iteration = 0
    while True:
        if stop_event is not None and stop_event.is_set():
            break
        orchestrator.refresh_repo()
        results.extend(watch_once(orchestrator, config, folder))
        iteration += 1
        if poll_interval <= 0:
            break
        if max_iterations is not None and iteration >= max_iterations:
            break
        if stop_event is not None:
            if stop_event.wait(poll_interval):  # returns True the instant SIGTERM fires
                break
        else:
            sleep_fn(poll_interval)
    return results


def cmd_run(args: argparse.Namespace) -> int:
    """Process exactly one task file through the Core pipeline (§5)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if config.git.create_pull_request:
        detect.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish (§6.7)
    orchestrator = build_orchestrator(
        config,
        artifacts_root=worc_home_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
    )
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
        stage = plan.interrupted_status.value if plan.interrupted_status else "unknown"
        print(f"  branch:    reuse {plan.branch or '(none)'}")
        print(f"  re-enter:  {stage}")
        print("  artifacts: kept; pending HITL prompt reset so the stage re-asks")
        print("  state:     terminal markers cleared; counters/subtasks/publish-ops kept")
    else:
        target = plan.branch or "agent/<id>-<slug>"
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
    pid = process_control.read_pid(process_control.pid_file_path(root))
    if pid is not None and process_control.is_running(pid):
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
        detect.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish (§6.7)

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
    pid = process_control.read_pid(process_control.pid_file_path(root))
    if pid is not None and process_control.is_running(pid):
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
    """Compute the read-only preflight verdict + report lines (spec §6.7); no task is processed.

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

    chk_ok, chk_lines = check_diagnostics.check_preflight(config, worc_home_for(config))
    ok = ok and chk_ok
    lines.extend(chk_lines)

    tg_ok, tg_line = check_telegram_preflight(config.telegram)
    if not tg_ok:
        ok = False
    lines.append(tg_line)

    lines.append(f"preflight: {'ready' if ok else 'NOT ready'}")
    return ok, lines


def cmd_preflight(args: argparse.Namespace) -> int:
    """Report each CLI's health and the strict_isolation verdict (read-only diagnostics, §6.7)."""
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

    The looping daemon writes ``<artifacts_root>/orchestrator.pid`` and installs a ``SIGTERM``
    handler so ``stop``/``restart`` can shut it down gracefully between ticks; it refuses to start
    when another watcher is already live for the same artifact root.
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    if config.git.create_pull_request:
        detect.require_gh()  # fail fast on a missing GitHub CLI, not mid-publish (§6.7)
    poll = (
        args.poll_seconds
        if args.poll_seconds is not None
        else config.orchestrator.poll_interval_seconds
    )
    folder = pending_dir(config)
    pid_path = process_control.pid_file_path(worc_home_for(config))

    # Only the looping mode is a daemon; refuse a second watcher for the same artifact root. A stale
    # PID file (process gone) is overwritten on start.
    if poll > 0:
        existing = process_control.read_pid(pid_path)
        if existing is not None and process_control.is_running(existing):
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
        return _summarize_watch(watch_loop(orchestrator, config, folder, poll_interval=poll))

    print(f"watch: polling every {poll}s for git-pushed tasks (Ctrl-C or 'stop' to exit)")
    results: list[PipelineResult] = []
    controller = process_control.StopController()  # SIGTERM -> event, restored on exit
    try:
        with controller:
            process_control.write_pid_file(pid_path)
            results = watch_loop(
                orchestrator, config, folder, poll_interval=poll, stop_event=controller.event
            )
    except KeyboardInterrupt:
        print("watch: stopped")
        return 0
    finally:
        pid_path.unlink(missing_ok=True)  # clean exit, Ctrl-C, SIGKILL-survivor, or error
    if controller.event.is_set():
        print("watch: stopped")  # graceful SIGTERM shutdown
        return 0
    return _summarize_watch(results)


def cmd_stop(args: argparse.Namespace) -> int:
    """Stop a running ``watch`` daemon (SIGTERM, then SIGKILL after ``--timeout``). Idempotent."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    pid_path = process_control.pid_file_path(worc_home_for(config))
    outcome = process_control.stop_process(pid_path, timeout=args.timeout)
    if not outcome.found:
        print("stop: no running watcher (no PID file)")
    elif outcome.already_dead:
        print(f"stop: no running watcher (cleared stale PID {outcome.pid})")
    elif outcome.killed:
        print(f"stop: watcher {outcome.pid} did not exit in {args.timeout:g}s; sent SIGKILL")
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
    outcome = process_control.stop_process(pid_path, timeout=args.timeout)
    if outcome.found and not outcome.already_dead:
        suffix = " (SIGKILL)" if outcome.killed else ""
        print(f"restart: stopped previous watcher {outcome.pid}{suffix}")
    else:
        print("restart: no previous watcher running")
    return cmd_watch(args)


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
        stage = _STATUS_STAGE.get(task.status)
        if stage is not None:
            print(f"stage={stage.value}")
            route = config.agents.routing.get(stage)
            if route is not None:
                print(f"configured_primary={route.primary.value}")
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

    # The resolved check profile, read-only (status never resolves, probes, or runs anything).
    profile = check_diagnostics.load_profile(worc_home_for(config))
    print()
    if profile is None:
        print("checks_profile: none (run preflight or install to resolve)")
    else:
        for line in check_diagnostics.summarize_profile(profile):
            print(line)
    return 0


def _install_atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
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
    a task writes into them; everything under ``.worc/`` is gitignored as a whole (§21).
    """
    worc_home = repo_local_path / WORC_HOME
    for rel in REPO_TASK_DIRS:
        (repo_local_path / rel).mkdir(parents=True, exist_ok=True)
    for rel in WORC_RUNTIME_DIRS:
        (worc_home / rel).mkdir(parents=True, exist_ok=True)


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
    print(f"  checks:     {', '.join(spec.checks) or '(none)'}")
    print(f"  discovery:  {spec.discovery_mode}")
    print(f"  create_pr:  {spec.create_pull_request}")
    print(f"  auto_mode:  {spec.auto_mode}")
    for rel in REPO_TASK_DIRS:
        print(f"  would create {spec.repo_local_path / rel}")
    for rel in WORC_RUNTIME_DIRS:
        print(f"  would create {worc_home / rel}")
    print(f"  would create {worc_home / 'guide'}/ (agent task-authoring docs)")
    print(f"  would ignore {WORC_HOME}/ via .gitignore")
    if missing:
        print(f"  note: provider(s) not on PATH: {', '.join(p.value for p in missing)}")


def _install_resolve_checks(config: OrchestratorConfig) -> None:
    """Seed the resolved profile at install, running the read-only agent fallback when configured.

    Deterministic resolution is also performed by the auto-preflight, but only the agent fallback
    needs an explicit provider run here. No-op unless ``checks.discovery`` enables agent fallback
    and names a cheap model (opt-in); the deterministic preflight then seeds the profile on its own.
    """
    from wastech_orchestrator.checks.discovery_factory import build_discovery
    from wastech_orchestrator.checks.resolver import CheckResolver

    artifacts_root = str(worc_home_for(config))
    providers = build_providers(config, artifacts_root=artifacts_root)
    discovery = build_discovery(config, providers, artifacts_root)
    if discovery is None:
        return
    resolver = CheckResolver(
        config,
        repo_root=config.repo.local_path,
        artifacts_root=artifacts_root,
        discovery=discovery,
    )
    resolver.resolve(allow_agent=True)


def _install_run_preflight(config_path: Path, *, skip: bool) -> int:
    """Auto-run preflight after writing config; on failure keep config but exit non-zero (§6.7)."""
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
    """Set up the orchestrator in the current repo under ``.worc/`` and generate config (§21).

    Runs the wizard to resolve settings, then idempotently writes a validated ``config.yaml`` into
    ``<repo>/.worc/``, scaffolds the runtime + task dirs, copies the editable templates and the
    task-authoring guide, and gitignores ``.worc/``. Re-running is a no-op unless ``--reconfigure``
    (which backs up and regenerates). After a successful write it auto-runs preflight (§6.7).
    """
    _configure_runtime_logging(args)
    try:
        outcome = wizard.run_wizard(
            repo_path=Path(args.repo_path),
            provider=args.provider,
            checks=args.check,
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
    # Gitignore the whole .worc/ runtime home so the operator's `git status` stays clean (§21.2).
    if append_runtime_excludes(spec.repo_local_path):
        print(f"install: ignored {WORC_HOME}/ via .gitignore")
    if outcome.missing_providers:
        names = ", ".join(p.value for p in outcome.missing_providers)
        print(f"install: note — selected provider(s) not on PATH yet: {names}")
    _install_resolve_checks(_load_config(str(config_path)))
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
    # rather than surfacing as a traceback (fail loud, not ugly). See the versioning gates.
    try:
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
        if args.command == "upgrade-config":
            return cmd_upgrade_config(args)
        if args.command == "upgrade-docs":
            return cmd_upgrade_docs(args)
        if args.command == "rerun":
            return cmd_rerun(args)
        if args.command == "finalize":
            return cmd_finalize(args)
    except (ConfigError, IncompatibleStateError, detect.GhNotAvailableError) as exc:
        print(f"error: {exc}")
        return 2
    raise SystemExit(f"Unknown command '{args.command}'.")


if __name__ == "__main__":
    sys.exit(main())
