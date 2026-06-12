"""CLI entry point.

``init`` scaffolds a project layout and templates; ``run`` processes one task end to end through
the Orchestrator Core; ``watch`` resumes any in-flight task and then processes pending tasks (one at
a time, continuing only when ``orchestrator.auto_mode.enabled``); ``status`` reads persisted
progress without starting work. See the spec §15.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from wastech_orchestrator.config.loader import load_config
from wastech_orchestrator.config.schema import FootprintLocation, OrchestratorConfig
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.core.orchestrator import (
    Orchestrator,
    PipelineResult,
    build_orchestrator,
    build_providers,
)
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.observability.logging import configure_logging
from wastech_orchestrator.providers.base import Stage
from wastech_orchestrator.security.isolation import check_isolation
from wastech_orchestrator.state_store import StateStore

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

# Friendly --git-mode names mapped onto the two git.footprint axes (spec §21).
GIT_MODES: dict[str, tuple[str, str]] = {
    "external": ("external", "none"),
    "in_repo_exclude": ("in_repo", "exclude_local"),
    "in_repo_commit": ("in_repo", "commit"),
}

# Runtime directories created by `init`, relative to the target path (spec §20.2).
RUNTIME_DIRS: tuple[str, ...] = (
    "tasks/pending",
    "tasks/processing",
    "tasks/done",
    "tasks/failed",
    "tasks/rejected",
    "logs",
    "workspace",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wastech-orchestrator",
        description="Orchestrator for coding agents (Codex / Claude Code) on top of Git.",
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
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

    init_cmd = sub.add_parser("init", help="scaffold the project layout and templates")
    init_cmd.add_argument(
        "path", nargs="?", default=".", help="target directory (default: current directory)"
    )
    init_cmd.add_argument(
        "--git-mode",
        choices=sorted(GIT_MODES),
        default="external",
        help="git footprint mode seeded into config.yaml (default: external)",
    )
    init_cmd.add_argument(
        "--force", action="store_true", help="re-copy template files (never touches config.yaml)"
    )
    init_cmd.add_argument(
        "--dry-run", action="store_true", help="print the created/skipped plan; write nothing"
    )
    init_cmd.add_argument(
        "--quiet", action="store_true", help="suppress the per-file report (exit code only)"
    )

    run_cmd = sub.add_parser("run", help="run a single task from a file")
    run_cmd.add_argument("task_file", help="path to the task file (.md or .json)")

    sub.add_parser("watch", help="watch the tasks folder and run the tasks in it")

    sub.add_parser(
        "preflight", help="check both CLIs' health and the strict_isolation policy (read-only)"
    )
    status_cmd = sub.add_parser("status", help="show the active or latest persisted task status")
    status_cmd.add_argument("task_id", nargs="?", help="specific task id (default: active/latest)")

    return parser


def _templates_root() -> Traversable:
    """The packaged templates directory (works from a source tree or an installed wheel)."""
    return resources.files("wastech_orchestrator").joinpath("templates")


def _iter_template_files(root: Path) -> Iterator[Path]:
    """Yield template file paths relative to ``root``, sorted for deterministic output."""
    for path in sorted(root.rglob("*")):
        if path.is_file():
            yield path.relative_to(root)


def _apply_git_mode(config_text: str, git_mode: str) -> str:
    """Seed the git.footprint location/tracking for the selected --git-mode (spec §21)."""
    location, tracking = GIT_MODES[git_mode]
    config_text = config_text.replace("location: external", f"location: {location}", 1)
    return config_text.replace("tracking: none", f"tracking: {tracking}", 1)


def cmd_init(args: argparse.Namespace) -> int:
    """Idempotently scaffold the project layout and templates (spec §20)."""
    target = Path(args.path).resolve()
    dry: bool = args.dry_run
    created: list[str] = []
    skipped: list[str] = []

    def add_dir(rel: str) -> None:
        directory = target / rel
        if directory.is_dir():
            skipped.append(f"{rel}/")
            return
        created.append(f"{rel}/")
        if not dry:
            directory.mkdir(parents=True, exist_ok=True)

    def add_file(rel: str, content: bytes, *, overwrite: bool) -> None:
        file = target / rel
        if file.exists() and not overwrite:
            skipped.append(rel)
            return
        created.append(rel)
        if not dry:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(content)

    # 1. Runtime directories, each kept by a .gitkeep so empty dirs survive in git.
    for rel in RUNTIME_DIRS:
        add_dir(rel)
        add_file(f"{rel}/.gitkeep", b"", overwrite=False)

    # 2. config.yaml from the packaged example, with the chosen git mode. Never overwritten.
    config_text = _apply_git_mode(
        _templates_root().joinpath("config.example.yaml").read_text(encoding="utf-8"),
        args.git_mode,
    )
    add_file("config.yaml", config_text.encode("utf-8"), overwrite=False)

    # 3. The templates/ tree (config.example.yaml is used for config.yaml, not copied verbatim).
    with resources.as_file(_templates_root()) as troot:
        for rel_path in _iter_template_files(Path(troot)):
            if rel_path.name == "config.example.yaml":
                continue
            add_file(
                str(Path("templates") / rel_path),
                (Path(troot) / rel_path).read_bytes(),
                overwrite=args.force,
            )

    if not args.quiet:
        verb_created = "would create" if dry else "create"
        verb_skipped = "would skip" if dry else "skip"
        for rel in created:
            print(f"  {verb_created} {rel}")
        for rel in skipped:
            print(f"  {verb_skipped} {rel}")
    summary = "init (dry-run)" if dry else "init"
    print(f"{summary}: {len(created)} created, {len(skipped)} skipped")
    return 0


def _load_config(path: str) -> OrchestratorConfig:
    """Load and semantically validate the config (fail-closed, §11/§21.4)."""
    config = load_config(path).config
    validate_config(config)
    return config


def artifacts_root_for(config: OrchestratorConfig) -> str:
    """Where ``logs/<task-id>/`` lives: ``external_root`` for external, else the clone (§21)."""
    if config.git.footprint.location is FootprintLocation.EXTERNAL:
        return config.git.footprint.external_root
    return config.repo.local_path


def pending_dir() -> Path:
    """The folder ``watch`` scans for new tasks (created by ``init``, §20.2)."""
    return Path("tasks") / "pending"


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


def cmd_run(args: argparse.Namespace) -> int:
    """Process exactly one task file through the Core pipeline (§5)."""
    _configure_runtime_logging(args)
    config = _load_config(args.config)
    orchestrator = build_orchestrator(
        config,
        artifacts_root=artifacts_root_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
    )
    result = orchestrator.run_task(args.task_file)
    suffix = f" → {result.pr_url}" if result.pr_url else ""
    print(f"{result.task_id}: {result.final_status.value}{suffix}")
    return _EXIT_BY_STATUS.get(result.final_status, 1)


def cmd_preflight(args: argparse.Namespace) -> int:
    """Report each CLI's health and the strict_isolation verdict (read-only diagnostics, §6.7).

    Runs every allowed provider's ``preflight()`` (``<cli> --version`` — no task is processed) and
    the deterministic ``check_isolation`` policy check. Exit 0 iff every provider is healthy and the
    required isolation can be enabled; non-zero otherwise. Messages are secret-free by contract.
    """
    _configure_runtime_logging(args)
    config = _load_config(args.config)
    providers = build_providers(config, artifacts_root=artifacts_root_for(config))

    ok = True
    for pid in config.agents.allowed:
        provider = providers.get(pid)
        if provider is None:
            print(f"{pid.value}: FAIL — no provider adapter configured")
            ok = False
            continue
        health = provider.preflight()
        healthy = health.executable_found and health.supports_required_features
        ok = ok and healthy
        print(
            f"{pid.value}: {'OK' if healthy else 'FAIL'} — {health.message} "
            f"(version={health.version or 'unknown'}, authenticated={health.authenticated})"
        )

    reasons = check_isolation(config)
    if reasons:
        ok = False
        print("isolation: FAIL")
        for reason in reasons:
            print(f"  - {reason}")
    else:
        enforced = "enforced" if config.security.strict_isolation else "strict_isolation=false"
        print(f"isolation: OK ({enforced})")

    print(f"preflight: {'ready' if ok else 'NOT ready'}")
    return 0 if ok else 1


def cmd_watch(args: argparse.Namespace) -> int:
    """Resume an in-flight task and process pending tasks (auto mode permitting)."""
    _configure_runtime_logging(args)
    config = _load_config(args.config)
    orchestrator = build_orchestrator(
        config,
        artifacts_root=artifacts_root_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
    )
    results = watch_once(orchestrator, config, pending_dir())
    if not results:
        print("watch: nothing to do (slot free, no pending tasks)")
        return 0
    for result in results:
        print(f"{result.task_id}: {result.final_status.value}")
    worst = max(_EXIT_BY_STATUS.get(r.final_status, 1) for r in results)
    return worst


def cmd_status(args: argparse.Namespace) -> int:
    """Show persisted progress without starting providers, checks, or git operations."""
    _configure_runtime_logging(args)
    config = _load_config(args.config)
    db_path = Path(artifacts_root_for(config)) / "state.db"
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
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.heartbeat_seconds < 0:
        parser.error("--heartbeat-seconds must be >= 0")

    if args.command == "init":
        return cmd_init(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "watch":
        return cmd_watch(args)
    if args.command == "preflight":
        return cmd_preflight(args)
    if args.command == "status":
        return cmd_status(args)
    raise SystemExit(f"Unknown command '{args.command}'.")


if __name__ == "__main__":
    sys.exit(main())
