"""Repository inspection — bounded, non-secret evidence collection (automatic check discovery).

Reads only well-known, non-secret project files (manifests, lock files, wrappers, CI workflow
names, instruction docs) and the presence of local interpreters/tool scripts. Each file read is
size-bounded and any path matching the security ``denied_read_paths`` is skipped. Nothing here
launches a process or resolves environment-variable *values* — CI files contribute names only.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

import yaml

# Cap per-file reads so a pathological manifest can never wedge or balloon discovery.
_MAX_FILE_BYTES = 262_144

# Python quality tools we know how to turn into check candidates.
_PY_TOOLS: tuple[str, ...] = ("pytest", "ruff", "mypy", "tox", "nox")

# Candidate local virtual-environment directories (POSIX ``bin`` and Windows ``Scripts``).
_VENV_DIRS: tuple[str, ...] = (".venv", "venv")

# Marker files whose mere presence is evidence (parsed further where it adds signal).
_MARKERS: tuple[str, ...] = (
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "tox.ini",
    "noxfile.py",
    "package.json",
    "pnpm-lock.yaml",
    "package-lock.json",
    "yarn.lock",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "go.sum",
    "Makefile",
    "Justfile",
    "justfile",
    "Taskfile.yml",
    "Taskfile.yaml",
)

_INSTRUCTION_DOCS: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md", "README.md", "README.rst", "README")

# A Makefile/Justfile target line: ``name:`` but not a ``name :=`` assignment.
_TARGET_RE = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*:(?!=)")


@dataclass(frozen=True)
class VenvInfo:
    """A detected local virtual environment (portable relative paths into the repo)."""

    python: str  # e.g. ".venv/bin/python" or ".venv/Scripts/python.exe"
    bin_dir: str  # e.g. ".venv/bin" or ".venv/Scripts"
    tools: frozenset[str]  # tool scripts present in bin_dir (subset of pytest/ruff/mypy)


@dataclass(frozen=True)
class RepositoryEvidence:
    """Bounded, non-secret facts a detector turns into deterministic check candidates."""

    repo_root: Path
    files_present: frozenset[str] = frozenset()
    python_tools: frozenset[str] = frozenset()
    node_scripts: frozenset[str] = frozenset()
    make_targets: frozenset[str] = frozenset()
    just_recipes: frozenset[str] = frozenset()
    task_targets: frozenset[str] = frozenset()
    venvs: tuple[VenvInfo, ...] = ()
    ci_workflows: tuple[str, ...] = ()
    instruction_docs: tuple[str, ...] = ()
    # Configured tool scope: an explicit `.` argument overrides these, so detection must
    # honor them instead of hardcoding `mypy .` / `ruff check .`. ``mypy_files`` are the safe,
    # repo-relative paths from ``[tool.mypy] files``; the ``*_has_scope`` flags mean any scope key
    # (files/exclude for mypy; src/include/exclude for ruff) is configured.
    mypy_files: tuple[str, ...] = ()
    mypy_has_scope: bool = False
    ruff_has_scope: bool = False

    def has(self, name: str) -> bool:
        return name in self.files_present


class RepositoryInspector:
    """Collect :class:`RepositoryEvidence` from a repository root (read-only, bounded)."""

    def __init__(self, repo_root: str | Path, *, denied_read_paths: tuple[str, ...] = ()) -> None:
        self._root = Path(repo_root)
        self._denied = tuple(denied_read_paths)

    def collect(self) -> RepositoryEvidence:
        present = frozenset(name for name in _MARKERS if (self._root / name).is_file())
        text, data = self._pyproject()
        mypy_files, mypy_has_scope, ruff_has_scope = _tool_scopes(data)
        return RepositoryEvidence(
            repo_root=self._root,
            files_present=present,
            python_tools=self._python_tools(text, data),
            node_scripts=self._node_scripts(),
            make_targets=self._targets("Makefile"),
            just_recipes=self._targets("Justfile") | self._targets("justfile"),
            task_targets=self._task_targets(),
            venvs=self._venvs(),
            ci_workflows=self._ci_workflows(),
            instruction_docs=tuple(d for d in _INSTRUCTION_DOCS if (self._root / d).is_file()),
            mypy_files=mypy_files,
            mypy_has_scope=mypy_has_scope,
            ruff_has_scope=ruff_has_scope,
        )

    # --- file access -----------------------------------------------------------------------

    def _denied_path(self, rel: str) -> bool:
        return any(Path(rel).match(glob) for glob in self._denied)

    def _read_text(self, rel: str) -> str | None:
        if self._denied_path(rel):
            return None
        path = self._root / rel
        try:
            if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    # --- per-ecosystem parsing -------------------------------------------------------------

    def _pyproject(self) -> tuple[str | None, dict[str, object] | None]:
        """Read + parse ``pyproject.toml`` once. ``(text, data)``; ``data`` is ``None`` if absent
        or unparseable (callers fall back to a loose text scan / an empty scope)."""
        text = self._read_text("pyproject.toml")
        if text is None:
            return None, None
        try:
            return text, tomllib.loads(text)
        except (tomllib.TOMLDecodeError, ValueError):
            return text, None

    def _python_tools(self, text: str | None, data: dict[str, object] | None) -> frozenset[str]:
        if text is None:
            return frozenset()
        # parse failure (data is None): fall back to a loose text scan (probing decides)
        blob = " ".join(_collect_requirements(data)).lower() if data is not None else text.lower()
        return frozenset(tool for tool in _PY_TOOLS if tool in blob)

    def _node_scripts(self) -> frozenset[str]:
        text = self._read_text("package.json")
        if text is None:
            return frozenset()
        try:
            data = json.loads(text)
        except ValueError:
            return frozenset()
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if not isinstance(scripts, dict):
            return frozenset()
        return frozenset(str(k) for k in scripts)

    def _targets(self, filename: str) -> frozenset[str]:
        text = self._read_text(filename)
        if text is None:
            return frozenset()
        names: set[str] = set()
        for line in text.splitlines():
            match = _TARGET_RE.match(line)
            if match:
                names.add(match.group(1))
        return frozenset(names)

    def _task_targets(self) -> frozenset[str]:
        for filename in ("Taskfile.yml", "Taskfile.yaml"):
            text = self._read_text(filename)
            if text is None:
                continue
            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError:
                return frozenset()
            tasks = data.get("tasks") if isinstance(data, dict) else None
            if isinstance(tasks, dict):
                return frozenset(str(k) for k in tasks)
        return frozenset()

    def _venvs(self) -> tuple[VenvInfo, ...]:
        out: list[VenvInfo] = []
        layouts = (("bin", "python", ""), ("Scripts", "python.exe", ".exe"))
        for vdir in _VENV_DIRS:
            for bin_name, py_name, suffix in layouts:
                bin_dir = self._root / vdir / bin_name
                python = bin_dir / py_name
                if not python.is_file():
                    continue
                tools = frozenset(
                    tool
                    for tool in ("pytest", "ruff", "mypy")
                    if (bin_dir / f"{tool}{suffix}").is_file()
                )
                out.append(
                    VenvInfo(
                        python=f"{vdir}/{bin_name}/{py_name}",
                        bin_dir=f"{vdir}/{bin_name}",
                        tools=tools,
                    )
                )
        return tuple(out)

    def _ci_workflows(self) -> tuple[str, ...]:
        workflows = self._root / ".github" / "workflows"
        if not workflows.is_dir():
            return ()
        try:
            names = sorted(
                p.name for p in workflows.iterdir() if p.suffix in (".yml", ".yaml") and p.is_file()
            )
        except OSError:
            return ()
        return tuple(names)


def _tool_scopes(data: dict[str, object] | None) -> tuple[tuple[str, ...], bool, bool]:
    """``(mypy_files, mypy_has_scope, ruff_has_scope)`` from ``[tool.mypy]``/``[tool.ruff]``.

    Best effort and safe by default: an unparseable/absent ``pyproject.toml`` yields no scope, so
    detection keeps the historical ``mypy .`` / ``ruff check .``. ``[tool.mypy] exclude`` is a regex
    (not a pathspec), so it only contributes to ``mypy_has_scope`` — it is never turned into argv.
    """
    if not isinstance(data, dict):
        return (), False, False
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return (), False, False
    mypy = tool.get("mypy")
    mypy_files: tuple[str, ...] = ()
    mypy_has_scope = False
    if isinstance(mypy, dict):
        mypy_has_scope = "files" in mypy or "exclude" in mypy
        mypy_files = _safe_scope_paths(mypy.get("files"))
    ruff = tool.get("ruff")
    ruff_has_scope = False
    if isinstance(ruff, dict):
        ruff_keys = ("src", "include", "extend-include", "exclude", "extend-exclude")
        ruff_has_scope = any(key in ruff for key in ruff_keys)
    return mypy_files, mypy_has_scope, ruff_has_scope


def _safe_scope_paths(value: object) -> tuple[str, ...]:
    """Normalize a ``[tool.mypy] files`` value to safe repo-relative argv tokens (reject, sanitize).

    Accepts a string or list of strings. Drops any entry that is absolute or escapes the repo (a
    ``..`` segment) — a dropped entry leaves only ``mypy_has_scope`` set, so detection emits a bare
    ``mypy`` (which reads the project's own config) rather than an unvetted path. Never sanitizes a
    path into argv; it either passes the check verbatim or is rejected.
    """
    if isinstance(value, str):
        items: list[str] = [value]
    elif isinstance(value, list):
        items = [str(item) for item in value]
    else:
        return ()
    out: list[str] = []
    for raw in items:
        token = raw.strip()
        if not token:
            continue
        norm = token.replace("\\", "/")
        if norm.startswith(("/", "~")) or ".." in norm.split("/") or Path(norm).is_absolute():
            continue  # reject absolute / traversal; a bare `mypy` is emitted instead
        out.append(norm)
    return tuple(out)


def _collect_requirements(data: object) -> list[str]:
    """Gather dependency requirement strings from the common pyproject locations (best effort)."""
    out: list[str] = []
    if not isinstance(data, dict):
        return out
    project = data.get("project")
    if isinstance(project, dict):
        out.extend(_as_str_list(project.get("dependencies")))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for group in optional.values():
                out.extend(_as_str_list(group))
    groups = data.get("dependency-groups")
    if isinstance(groups, dict):
        for group in groups.values():
            out.extend(_as_str_list(group))
    tool = data.get("tool")
    if isinstance(tool, dict):
        poetry = tool.get("poetry")
        if isinstance(poetry, dict):
            deps = poetry.get("dependencies")
            if isinstance(deps, dict):
                out.extend(str(k) for k in deps)
            poetry_groups = poetry.get("group")
            if isinstance(poetry_groups, dict):
                for group in poetry_groups.values():
                    if isinstance(group, dict) and isinstance(group.get("dependencies"), dict):
                        out.extend(str(k) for k in group["dependencies"])
    return out


def _as_str_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []
