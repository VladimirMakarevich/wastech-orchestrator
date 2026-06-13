"""Deterministic check-candidate detection (backlog: automatic check discovery, §5).

Turns :class:`~wastech_orchestrator.checks.inspect.RepositoryEvidence` into ordered
:class:`~wastech_orchestrator.checks.model.CheckCandidate` proposals for recognized ecosystems —
**all** matches, not first-match, so the resolver can probe in precedence order and keep the
highest-confidence launchable one per logical check. Detection inspects the actual manifest before
proposing a script/target (file presence alone is never enough).

This module is provider-agnostic and read-only: it proposes argv lists; it never launches anything.
"""

from __future__ import annotations

from wastech_orchestrator.checks.inspect import RepositoryEvidence
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
            out.append(_c(_LINT, (ruff, "check", "."), [f"{venv.bin_dir} has ruff"]))
        if "mypy" in venv.tools:
            mypy = f"{venv.bin_dir}/mypy{suffix}"
            out.append(_c(_TYPES, (mypy, "."), [f"{venv.bin_dir} has mypy"]))
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
            out.append(_c(_LINT, (*prefix, "ruff", "check", "."), [f"{lockfile}; ruff declared"]))
        if "mypy" in ev.python_tools:
            out.append(_c(_TYPES, (*prefix, "mypy", "."), [f"{lockfile}; mypy declared"]))
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
            out.append(_low(_LINT, ("ruff", "check", "."), ["ruff declared in pyproject.toml"]))
        if "mypy" in ev.python_tools:
            out.append(_low(_TYPES, ("mypy", "."), ["mypy declared in pyproject.toml"]))
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
