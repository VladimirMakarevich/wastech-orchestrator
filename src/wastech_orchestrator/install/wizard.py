"""The interactive install wizard and its non-interactive resolution (backlog: installer).

``run_wizard`` turns CLI flags + environment detection (and, interactively, operator answers) into a
validated :class:`~wastech_orchestrator.install.config_writer.InstallSpec`. All console I/O goes
through the :class:`Prompter` seam so the flow is fully testable: production uses
:class:`ConsolePrompter`; ``--non-interactive`` resolves every value from flags/detection without
prompting. Hard stops (not a repo, no ``origin``, no available provider, an aborted confirmation)
raise :class:`InstallError`, which the CLI turns into a message + non-zero exit. Nothing here writes
files, commits, or installs anything — detection is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from wastech_orchestrator.install import detect
from wastech_orchestrator.install.config_writer import InstallSpec
from wastech_orchestrator.providers.base import ProviderId


class InstallError(Exception):
    """A condition that stops the install (printed to the operator; non-zero exit)."""


class Prompter(Protocol):
    """Console I/O seam so the wizard can be driven deterministically in tests."""

    def info(self, message: str) -> None: ...
    def ask(self, prompt: str, *, default: str) -> str: ...
    def confirm(self, prompt: str, *, default: bool) -> bool: ...
    def ask_list(self, prompt: str) -> list[str]: ...


class ConsolePrompter:
    """Default :class:`Prompter` backed by ``input``/``print`` for real interactive use."""

    def info(self, message: str) -> None:
        print(message)

    def ask(self, prompt: str, *, default: str) -> str:
        raw = input(f"{prompt} [{default}]: ").strip()
        return raw or default

    def confirm(self, prompt: str, *, default: bool) -> bool:
        raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        return default if not raw else raw in ("y", "yes")

    def ask_list(self, prompt: str) -> list[str]:
        print(prompt)
        items: list[str] = []
        while True:
            raw = input("  - ").strip()
            if not raw:
                return items
            items.append(raw)


@dataclass(frozen=True)
class WizardOutcome:
    """The wizard's result: the config spec plus selected providers not yet on ``PATH``."""

    spec: InstallSpec
    missing_providers: tuple[ProviderId, ...]


def run_wizard(
    *,
    repo_path: Path,
    provider: str,
    create_pr: bool | None,
    auto_mode: bool | None,
    non_interactive: bool,
    prompter: Prompter,
) -> WizardOutcome:
    """Resolve an :class:`InstallSpec` from flags, detection, and (interactively) operator input."""
    if detect.find_executable("git") is None:
        raise InstallError("git was not found on PATH; install git and retry")

    info = detect.git_info(repo_path)
    if info is None:
        raise InstallError(
            f"{Path(repo_path).resolve()} is not inside a Git repository; "
            "cd into your repository or pass its path"
        )
    if info.origin_url is None:
        raise InstallError(
            "no 'origin' remote found; add one (e.g. git remote add origin <url>) so the "
            "orchestrator can push and open pull requests"
        )
    base_branch = info.default_branch or info.current_branch or "main"
    prompter.info(
        f"repository: {info.root}\norigin:     {info.origin_url}\nbase branch: {base_branch}"
    )
    _confirm_cleanliness(info.is_clean, non_interactive, prompter)

    providers, missing = _resolve_providers(provider, prompter)
    resolved_create_pr = _resolve_create_pr(create_pr, non_interactive, prompter)
    resolved_auto = _resolve_auto_mode(auto_mode, non_interactive, prompter)

    # Checks are not seeded at install: the gate is named ``checks.command_sets`` (with paths/cwd),
    # which a flat list can't express. ``init`` writes an empty mapping; the operator authors it
    # from the commented examples in config.example.yaml.
    spec = InstallSpec(
        repo_url=info.origin_url,
        repo_local_path=info.root,
        base_branch=base_branch,
        providers=providers,
        create_pull_request=resolved_create_pr,
        auto_mode=resolved_auto,
    )
    prompter.info(_summary(spec, missing))
    if not non_interactive and not prompter.confirm("Write this configuration?", default=True):
        raise InstallError("aborted by operator (no changes written)")
    return WizardOutcome(spec=spec, missing_providers=missing)


def _confirm_cleanliness(is_clean: bool, non_interactive: bool, prompter: Prompter) -> None:
    if is_clean:
        return
    message = (
        "the repository has uncommitted changes; the orchestrator switches branches and commits "
        "here while running tasks"
    )
    if non_interactive:
        prompter.info(f"warning: {message}")
    elif not prompter.confirm(f"{message}. Continue?", default=False):
        raise InstallError("aborted: the repository is not clean")


def _resolve_providers(
    provider: str, prompter: Prompter
) -> tuple[tuple[ProviderId, ...], tuple[ProviderId, ...]]:
    """Resolve ``--provider`` to ``(selected, missing)`` (missing = selected but not on PATH)."""
    detected = detect.detect_providers()
    present = tuple(pid for pid in ProviderId if detected[pid] is not None)

    if provider == "auto":
        if not present:
            raise InstallError(
                "no agent CLI found on PATH (codex / claude); install at least one, or pass "
                "--provider to select one explicitly"
            )
        selected = present
    elif provider == "both":
        selected = (ProviderId.CODEX, ProviderId.CLAUDE)
    else:
        selected = (ProviderId(provider),)

    missing = tuple(pid for pid in selected if detected[pid] is None)
    if missing:
        names = ", ".join(pid.value for pid in missing)
        prompter.info(f"note: selected provider(s) not on PATH yet: {names}")
    return selected, missing


def _resolve_create_pr(create_pr: bool | None, non_interactive: bool, prompter: Prompter) -> bool:
    if create_pr is not None:
        return create_pr
    default = detect.has_gh()  # no gh -> default off (it can't open a PR)
    if non_interactive:
        return default
    return prompter.confirm("Create a pull request after pushing?", default=default)


def _resolve_auto_mode(auto_mode: bool | None, non_interactive: bool, prompter: Prompter) -> bool:
    if auto_mode is not None:
        return auto_mode
    if non_interactive:
        return False
    return prompter.confirm("Enable auto mode (process pending tasks back-to-back)?", default=False)


def _summary(spec: InstallSpec, missing: tuple[ProviderId, ...]) -> str:
    providers = ", ".join(pid.value for pid in spec.providers)
    lines = [
        "",
        "configuration to write:",
        f"  repo:        {spec.repo_local_path}",
        f"  .worc home:  {spec.repo_local_path / '.worc'}",
        f"  base branch: {spec.base_branch}",
        f"  providers:   {providers}",
        "  checks:      command_sets (author in config.yaml)",
        f"  create PR:   {spec.create_pull_request}",
        f"  auto mode:   {spec.auto_mode}",
    ]
    if missing:
        lines.append(f"  (not on PATH: {', '.join(pid.value for pid in missing)})")
    return "\n".join(lines)
