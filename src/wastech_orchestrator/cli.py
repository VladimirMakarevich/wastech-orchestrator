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
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from wastech_orchestrator import __version__
from wastech_orchestrator.checks import diagnostics as check_diagnostics
from wastech_orchestrator.config.loader import ConfigError, load_config
from wastech_orchestrator.config.schema import FootprintLocation, OrchestratorConfig
from wastech_orchestrator.config.validation import validate_config
from wastech_orchestrator.core.orchestrator import (
    Orchestrator,
    PipelineResult,
    build_orchestrator,
    build_providers,
)
from wastech_orchestrator.core.state_machine import Status
from wastech_orchestrator.install import config_writer, detect, registry, wizard
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

# `install` binds the existing repo as repo.local_path and uses the in-repo footprint (§21): the
# task lifecycle + artifact dirs live in the repo (created empty here, so they stay invisible to
# `git status` until a task writes into them), while config.yaml and the rejected-task quarantine
# stay in the sibling control workspace, out of the repo.
INSTALL_REPO_DIRS: tuple[str, ...] = (
    "tasks/pending",
    "tasks/processing",
    "tasks/done",
    "tasks/failed",
    "logs",
)
INSTALL_WORKSPACE_DIRS: tuple[str, ...] = ("tasks/rejected",)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wastech-orchestrator",
        description="Orchestrator for coding agents (Codex / Claude Code) on top of Git.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--config",
        default=None,
        help="path to config.yaml (default: ./config.yaml, else the bound config from 'install')",
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

    init_cmd = sub.add_parser("init", help="scaffold the project layout and templates")
    init_cmd.add_argument(
        "path", nargs="?", default=".", help="target directory (default: current directory)"
    )
    init_cmd.add_argument(
        "--git-mode",
        choices=sorted(GIT_MODES),
        default="in_repo_commit",
        help="git footprint mode seeded into config.yaml (default: in_repo_commit — tasks & "
        "artifacts live in the repo and are audit-committed)",
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

    install_cmd = sub.add_parser(
        "install", help="bind the current repo to a sibling workspace and generate config.yaml"
    )
    install_cmd.add_argument(
        "repo_path", nargs="?", default=".", help="repository path (default: current directory)"
    )
    install_cmd.add_argument(
        "--workspace",
        default=None,
        help="control workspace directory (default: a <repo-name>-orchestrator sibling)",
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
    """Seed the git.footprint location/tracking for the selected --git-mode (spec §21).

    Anchored on the packaged config's defaults (``in_repo``/``commit``); selecting a different mode
    rewrites just those two value lines (the trailing comments are left intact).
    """
    location, tracking = GIT_MODES[git_mode]
    config_text = config_text.replace("location: in_repo", f"location: {location}", 1)
    return config_text.replace("tracking: commit", f"tracking: {tracking}", 1)


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


def resolve_config_path(args: argparse.Namespace) -> str | None:
    """Find the config to load (backlog: interactive installer), in priority order:

    1. an explicit ``--config PATH``;
    2. ``./config.yaml`` in the current directory (backward compatibility with ``init``);
    3. the binding for the current Git repository recorded by ``install`` (works from any subdir);
    4. otherwise ``None`` (the caller prints an actionable hint).
    """
    explicit = getattr(args, "config", None)
    if explicit is not None:
        return str(explicit)
    if Path("config.yaml").is_file():
        return "config.yaml"
    info = detect.git_info(Path.cwd())
    if info is not None:
        return registry.lookup(info.root)
    return None


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


def artifacts_root_for(config: OrchestratorConfig) -> str:
    """Where ``logs/<task-id>/`` lives: ``external_root`` for external, else the clone (§21)."""
    if config.git.footprint.location is FootprintLocation.EXTERNAL:
        return config.git.footprint.external_root
    return config.repo.local_path


def pending_dir(config: OrchestratorConfig) -> Path:
    """The folder ``watch`` scans for new tasks: ``tasks/pending`` under the artifact root (§21).

    For an external footprint this is the control workspace (``external_root``); for an in-repo
    footprint it is the clone. With ``install``'s absolute ``external_root``, ``watch`` therefore
    works from anywhere, not only the directory the tasks happen to live in.
    """
    return Path(artifacts_root_for(config)) / "tasks" / "pending"


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
) -> list[PipelineResult]:
    """Run ``watch_once`` on a loop, refreshing the repo each tick (periodic discovery, §8.3).

    Each tick: ``refresh_repo`` (fetch + ff-only pull of ``base_branch`` when the slot is free, so a
    task pushed to git later becomes visible), then ``watch_once``, then sleep ``poll_interval``.
    ``poll_interval <= 0`` runs exactly one tick (single pass, no sleep). ``max_iterations`` bounds
    the loop for tests; in production the loop runs until interrupted.
    """
    results: list[PipelineResult] = []
    iteration = 0
    while True:
        orchestrator.refresh_repo()
        results.extend(watch_once(orchestrator, config, folder))
        iteration += 1
        if poll_interval <= 0:
            break
        if max_iterations is not None and iteration >= max_iterations:
            break
        sleep_fn(poll_interval)
    return results


def cmd_run(args: argparse.Namespace) -> int:
    """Process exactly one task file through the Core pipeline (§5)."""
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    orchestrator = build_orchestrator(
        config,
        artifacts_root=artifacts_root_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
    )
    result = orchestrator.run_task(args.task_file)
    suffix = f" → {result.pr_url}" if result.pr_url else ""
    print(f"{result.task_id}: {result.final_status.value}{suffix}")
    return _EXIT_BY_STATUS.get(result.final_status, 1)


def run_preflight(config: OrchestratorConfig) -> tuple[bool, list[str]]:
    """Compute the read-only preflight verdict + report lines (spec §6.7); no task is processed.

    Runs every allowed provider's ``preflight()`` (``<cli> --version``) and the deterministic
    ``check_isolation`` policy check. Returns ``(ready, lines)`` where ``ready`` is true iff every
    provider is healthy and the required isolation can be enabled. Lines are secret-free by
    contract. Shared by ``cmd_preflight`` and the installer's post-write auto-preflight.
    """
    lines: list[str] = []
    providers = build_providers(config, artifacts_root=artifacts_root_for(config))
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

    chk_ok, chk_lines = check_diagnostics.check_preflight(config, artifacts_root_for(config))
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


def cmd_watch(args: argparse.Namespace) -> int:
    """Resume an in-flight task and process pending tasks (auto mode permitting).

    With ``poll_interval > 0`` (config ``orchestrator.poll_interval_seconds`` or ``--poll-seconds``)
    this runs as a daemon: each tick fetch/pulls ``base_branch`` to discover git-pushed tasks, then
    processes pending, then sleeps. ``0`` is a single pass. Stop the daemon with Ctrl-C.
    """
    _configure_runtime_logging(args)
    config = load_config_for(args)
    if config is None:
        return 2
    poll = (
        args.poll_seconds
        if args.poll_seconds is not None
        else config.orchestrator.poll_interval_seconds
    )
    orchestrator = build_orchestrator(
        config,
        artifacts_root=artifacts_root_for(config),
        heartbeat_seconds=args.heartbeat_seconds,
    )
    if poll > 0:
        print(f"watch: polling every {poll}s for git-pushed tasks (Ctrl-C to stop)")
    try:
        results = watch_loop(orchestrator, config, pending_dir(config), poll_interval=poll)
    except KeyboardInterrupt:
        print("watch: stopped")
        return 0
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
    config = load_config_for(args)
    if config is None:
        return 2
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

    # The resolved check profile, read-only (status never resolves, probes, or runs anything).
    profile = check_diagnostics.load_profile(artifacts_root_for(config))
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


def _install_create_dirs(repo_local_path: Path, workspace: Path) -> None:
    """Create the runtime task/log dirs in the repo and the quarantine in the workspace.

    Idempotent. The repo dirs are created empty, so they do not appear in ``git status`` until a
    task writes into them (the install leaves the target repo's tracked state untouched, §21).
    """
    for rel in INSTALL_REPO_DIRS:
        (repo_local_path / rel).mkdir(parents=True, exist_ok=True)
    for rel in INSTALL_WORKSPACE_DIRS:
        (workspace / rel).mkdir(parents=True, exist_ok=True)


def _install_print_plan(
    spec: config_writer.InstallSpec, config_path: Path, missing: tuple[ProviderId, ...]
) -> None:
    """Print what ``install`` would do, writing nothing (``--dry-run``)."""
    print("install (dry-run): no changes written")
    print(f"  repo:       {spec.repo_local_path}")
    print(f"  origin:     {spec.repo_url}")
    print(f"  base:       {spec.base_branch}")
    print(f"  workspace:  {spec.workspace}")
    print(f"  config:     {config_path}")
    print(f"  providers:  {', '.join(p.value for p in spec.providers)}")
    print(f"  checks:     {', '.join(spec.checks) or '(none)'}")
    print(f"  discovery:  {spec.discovery_mode}")
    print(f"  create_pr:  {spec.create_pull_request}")
    print(f"  auto_mode:  {spec.auto_mode}")
    for rel in INSTALL_REPO_DIRS:
        print(f"  would create {spec.repo_local_path / rel}")
    for rel in INSTALL_WORKSPACE_DIRS:
        print(f"  would create {spec.workspace / rel}")
    print(f"  would bind   {spec.repo_local_path} -> {config_path}")
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

    artifacts_root = artifacts_root_for(config)
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
    """Bind the current repo to a sibling control workspace and generate config (backlog).

    Runs the wizard to resolve settings, then idempotently writes a validated ``config.yaml`` into
    the workspace and records the ``repo -> config`` binding. Re-running is a no-op unless
    ``--reconfigure`` (which backs up and regenerates); a config that exists but is bound to another
    repo is never overwritten. After a successful write it auto-runs preflight (§6.7).
    """
    _configure_runtime_logging(args)
    try:
        outcome = wizard.run_wizard(
            repo_path=Path(args.repo_path),
            workspace=Path(args.workspace) if args.workspace else None,
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
    config_path = (spec.workspace / "config.yaml").resolve()

    if args.dry_run:
        _install_print_plan(spec, config_path, outcome.missing_providers)
        return 0

    bound = registry.lookup(spec.repo_local_path)
    bound_to_this = bound is not None and Path(bound).resolve() == config_path
    if config_path.exists():
        if not args.reconfigure:
            if bound_to_this:
                print(f"install: already configured at {config_path} (use --reconfigure to redo)")
                return _install_run_preflight(config_path, skip=args.skip_preflight)
            print(
                f"install: {config_path} already exists and is not bound to "
                f"{spec.repo_local_path}; choose another --workspace or pass --reconfigure"
            )
            return 1
        print(f"install: backed up existing config to {_install_backup_config(config_path)}")

    text = config_writer.build_and_validate(spec)
    _install_create_dirs(spec.repo_local_path, spec.workspace)
    _install_atomic_write(config_path, text)
    registry.bind(spec.repo_local_path, config_path)
    print(f"install: wrote {config_path}")
    print(f"install: bound {spec.repo_local_path} -> {config_path}")
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

    # A config/DB written by a newer orchestrator is refused with a clean message + exit 2 here,
    # rather than surfacing as a traceback (fail loud, not ugly). See the versioning gates.
    try:
        if args.command == "init":
            return cmd_init(args)
        if args.command == "install":
            return cmd_install(args)
        if args.command == "run":
            return cmd_run(args)
        if args.command == "watch":
            return cmd_watch(args)
        if args.command == "preflight":
            return cmd_preflight(args)
        if args.command == "status":
            return cmd_status(args)
    except (ConfigError, IncompatibleStateError) as exc:
        print(f"error: {exc}")
        return 2
    raise SystemExit(f"Unknown command '{args.command}'.")


if __name__ == "__main__":
    sys.exit(main())
