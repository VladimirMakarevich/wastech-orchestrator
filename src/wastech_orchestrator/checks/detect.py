"""Deterministic check-candidate detection (backlog: automatic check discovery).

Turns :class:`~wastech_orchestrator.checks.inspect.RepositoryEvidence` into ordered
:class:`~wastech_orchestrator.checks.model.CheckCandidate` proposals for recognized ecosystems —
**all** matches, not first-match, so the resolver can probe in precedence order and keep the
highest-confidence launchable one per logical check. Detection inspects the actual manifest before
proposing a script/target (file presence alone is never enough).

This module is provider-agnostic and read-only: it proposes argv lists; it never launches anything.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from wastech_orchestrator.checks.inspect import RepositoryEvidence, RepositoryInspector
from wastech_orchestrator.checks.model import CheckCandidate, CheckSource, Confidence

# Logical check names. ``checks`` is a combined project-owned wrapper (e.g. ``make check``) that
# supersedes the per-language tests/lint/types checks when it is launchable (resolver policy).
_TESTS = "tests"
_LINT = "lint"
_TYPES = "types"
_CHECKS = "checks"


class CheckCandidateDetector:
    """Produce deterministic check candidates from repository evidence."""

    def detect(self, evidence: RepositoryEvidence) -> list[CheckCandidate]:
        candidates: list[CheckCandidate] = []
        candidates += self._wrappers(evidence)
        candidates += self._venv_checks(evidence)
        candidates += self._manifest_checks(evidence)
        candidates += self._plain_python(evidence)
        return candidates

    # --- project-owned wrappers (highest precedence) ---------------------------------------

    def _wrappers(self, ev: RepositoryEvidence) -> list[CheckCandidate]:
        out: list[CheckCandidate] = []
        for target in ("check", "test"):
            if target in ev.make_targets:
                out.append(_c(_CHECKS, ("make", target), [f"Makefile target {target!r}"]))
                break
        for recipe in ("check", "test"):
            if recipe in ev.just_recipes:
                out.append(_c(_CHECKS, ("just", recipe), [f"Justfile recipe {recipe!r}"]))
                break
        for task in ("check", "test"):
            if task in ev.task_targets:
                out.append(_c(_CHECKS, ("task", task), [f"Taskfile task {task!r}"]))
                break
        if ev.has("tox.ini"):
            out.append(_c(_CHECKS, ("tox",), ["tox.ini present"]))
        if ev.has("noxfile.py"):
            out.append(_c(_CHECKS, ("nox",), ["noxfile.py present"]))
        return out

    # --- local virtual environments --------------------------------------------------------

    def _venv_checks(self, ev: RepositoryEvidence) -> list[CheckCandidate]:
        if not ev.venvs:
            return []
        venv = ev.venvs[0]
        suffix = ".exe" if venv.python.endswith(".exe") else ""
        out: list[CheckCandidate] = []
        if "pytest" in venv.tools:
            script = f"{venv.bin_dir}/pytest{suffix}"
            out.append(_c(_TESTS, (script,), [f"{venv.bin_dir} has pytest"]))
        else:
            out.append(
                CheckCandidate(
                    name=_TESTS,
                    argv=(venv.python, "-m", "pytest"),
                    source=CheckSource.DETECTED,
                    evidence=(f"{venv.python} exists",),
                    confidence=Confidence.MEDIUM,
                )
            )
        if "ruff" in venv.tools:
            ruff = f"{venv.bin_dir}/ruff{suffix}"
            out.append(
                _c(_LINT, _ruff_argv((ruff,), ev), [f"{venv.bin_dir} has ruff{_ruff_note(ev)}"])
            )
        if "mypy" in venv.tools:
            mypy = f"{venv.bin_dir}/mypy{suffix}"
            out.append(
                _c(_TYPES, _mypy_argv((mypy,), ev), [f"{venv.bin_dir} has mypy{_mypy_note(ev)}"])
            )
        return out

    # --- manifests / lock files ------------------------------------------------------------

    def _manifest_checks(self, ev: RepositoryEvidence) -> list[CheckCandidate]:
        out: list[CheckCandidate] = []
        out += self._python_runner(ev, "uv.lock", ("uv", "run"))
        out += self._python_runner(ev, "poetry.lock", ("poetry", "run"))
        out += self._node_checks(ev)
        if ev.has("Cargo.toml"):
            out.append(_c(_TESTS, ("cargo", "test"), ["Cargo.toml present"]))
        if ev.has("go.mod"):
            out.append(_c(_TESTS, ("go", "test", "./..."), ["go.mod present"]))
        return out

    def _python_runner(
        self, ev: RepositoryEvidence, lockfile: str, prefix: tuple[str, ...]
    ) -> list[CheckCandidate]:
        if not ev.has(lockfile):
            return []
        out: list[CheckCandidate] = []
        if "pytest" in ev.python_tools:
            out.append(_c(_TESTS, (*prefix, "pytest"), [f"{lockfile} present; pytest declared"]))
        if "ruff" in ev.python_tools:
            argv = _ruff_argv((*prefix, "ruff"), ev)
            out.append(_c(_LINT, argv, [f"{lockfile}; ruff declared{_ruff_note(ev)}"]))
        if "mypy" in ev.python_tools:
            argv = _mypy_argv((*prefix, "mypy"), ev)
            out.append(_c(_TYPES, argv, [f"{lockfile}; mypy declared{_mypy_note(ev)}"]))
        return out

    def _node_checks(self, ev: RepositoryEvidence) -> list[CheckCandidate]:
        if not ev.has("package.json"):
            return []
        pm: str
        run: tuple[str, ...]
        if ev.has("pnpm-lock.yaml"):
            pm, run = "pnpm", ("pnpm", "run")
        elif ev.has("yarn.lock"):
            pm, run = "yarn", ("yarn",)
        else:
            pm, run = "npm", ("npm", "run")
        out: list[CheckCandidate] = []
        if "test" in ev.node_scripts:
            out.append(_c(_TESTS, (pm, "test"), [f"package.json script 'test' ({pm})"]))
        if "lint" in ev.node_scripts:
            out.append(_c(_LINT, (*run, "lint"), [f"package.json script 'lint' ({pm})"]))
        return out

    # --- plain ecosystem defaults (lowest confidence) --------------------------------------

    def _plain_python(self, ev: RepositoryEvidence) -> list[CheckCandidate]:
        if not ev.has("pyproject.toml"):
            return []
        out: list[CheckCandidate] = [_low(_TESTS, ("pytest",), ["pyproject.toml present"])]
        if "ruff" in ev.python_tools:
            argv = _ruff_argv(("ruff",), ev)
            out.append(_low(_LINT, argv, [f"ruff declared in pyproject.toml{_ruff_note(ev)}"]))
        if "mypy" in ev.python_tools:
            argv = _mypy_argv(("mypy",), ev)
            out.append(_low(_TYPES, argv, [f"mypy declared in pyproject.toml{_mypy_note(ev)}"]))
        return out


def _c(name: str, argv: tuple[str, ...], evidence: list[str]) -> CheckCandidate:
    return CheckCandidate(
        name=name,
        argv=argv,
        source=CheckSource.DETECTED,
        evidence=tuple(evidence),
        confidence=Confidence.HIGH,
    )


def _low(name: str, argv: tuple[str, ...], evidence: list[str]) -> CheckCandidate:
    return CheckCandidate(
        name=name,
        argv=argv,
        source=CheckSource.DETECTED,
        evidence=tuple(evidence),
        confidence=Confidence.LOW,
    )


def _ruff_argv(ruff_cmd: tuple[str, ...], ev: RepositoryEvidence) -> tuple[str, ...]:
    """``<ruff> check`` with a trailing ``.`` only when the project pins no ruff scope.

    A configured ``[tool.ruff]`` scope (``src``/``include``/``exclude``) is *overridden* by an
    explicit ``.``, so we drop it and let ruff read its own config — the post-test-run scope bug.
    """
    base = (*ruff_cmd, "check")
    return base if ev.ruff_has_scope else (*base, ".")


def _mypy_argv(mypy_cmd: tuple[str, ...], ev: RepositoryEvidence) -> tuple[str, ...]:
    """``<mypy>`` targets honoring the project's scope: the configured ``[tool.mypy] files``
    when set, else a bare ``mypy`` when any scope (files/exclude) is configured, else ``mypy .``."""
    if ev.mypy_files:
        return (*mypy_cmd, *ev.mypy_files)
    if ev.mypy_has_scope:
        return mypy_cmd
    return (*mypy_cmd, ".")


def _ruff_note(ev: RepositoryEvidence) -> str:
    return "; scope from [tool.ruff]" if ev.ruff_has_scope else ""


def _mypy_note(ev: RepositoryEvidence) -> str:
    return "; scope from [tool.mypy]" if (ev.mypy_files or ev.mypy_has_scope) else ""


# Logical-check confidence ranking, mirroring the runtime resolver's ``_priority``.
_CONFIDENCE_RANK: dict[Confidence, int] = {
    Confidence.LOW: 1,
    Confidence.MEDIUM: 2,
    Confidence.HIGH: 3,
}


def propose_default_commands(repo_root: Path | str) -> list[str]:
    """Propose default ``checks.commands`` (shell strings) for the installer to seed config.

    Delegates ecosystem detection to :class:`CheckCandidateDetector` — the **same** deterministic,
    lockfile-aware detector the runtime ``checks.resolver.CheckResolver`` uses — then renders the
    highest-confidence candidate per logical check to a shell string via :func:`shlex.join`
    (``checks.commands`` is operator-friendly, so the seed is a string, not argv; the round-trip
    back to argv is lossless for these simple commands). One ecosystem-detection source of truth,
    so the installer can never seed a command the resolver disagrees with.

    Offline: detection inspects manifests/lockfiles/venvs but launches nothing — the installer only
    seeds the config; the resolver re-validates launchability at preflight/runtime.
    """
    evidence = RepositoryInspector(repo_root).collect()
    candidates = CheckCandidateDetector().detect(evidence)
    chosen = _best_candidate_per_name(candidates)
    # A project-owned wrapper (``make check``/``tox``/…) supersedes the per-language checks when it
    # is present, mirroring the resolver's selection.
    selected = [chosen[_CHECKS]] if _CHECKS in chosen else [chosen[name] for name in sorted(chosen)]
    return [shlex.join(candidate.argv) for candidate in selected]


def _best_candidate_per_name(candidates: list[CheckCandidate]) -> dict[str, CheckCandidate]:
    """Keep the highest-confidence candidate per logical name (ties broken by detection order)."""
    best: dict[str, tuple[int, int, CheckCandidate]] = {}
    for index, candidate in enumerate(candidates):
        rank = _CONFIDENCE_RANK[candidate.confidence]
        current = best.get(candidate.name)
        if current is None or rank > current[0]:
            best[candidate.name] = (rank, index, candidate)
    return {name: item[2] for name, item in best.items()}
