"""Discovery-input fingerprint (backlog: automatic check discovery).

A stable hash over the files and local executables that drive detection. When it changes the
resolver rediscovers; when it matches, a cached profile is reused. Missing inputs contribute a
stable "absent" marker so the fingerprint is order- and presence-stable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from wastech_orchestrator.config.schema import ChecksConfig

# Hash at most this many bytes per file (lock files can be large; bound the work).
_MAX_HASH_BYTES = 1_048_576

# Files whose contents drive detection.
_FINGERPRINT_FILES: tuple[str, ...] = (
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
    "AGENTS.md",
    "CLAUDE.md",
)

# Local executables whose mere presence/identity changes which checks are launchable.
_FINGERPRINT_EXECUTABLES: tuple[str, ...] = (
    ".venv/bin/python",
    ".venv/bin/pytest",
    ".venv/bin/ruff",
    ".venv/bin/mypy",
    ".venv/Scripts/python.exe",
    "venv/bin/python",
)


def compute_fingerprint(repo_root: str | Path) -> str:
    """Return a hex digest over the discovery inputs under ``repo_root`` (stable, secret-free)."""
    root = Path(repo_root)
    digest = hashlib.sha256()
    for rel in _FINGERPRINT_FILES:
        digest.update(rel.encode("utf-8"))
        digest.update(_file_marker(root / rel))
    for rel in sorted(_workflow_files(root)):
        digest.update(rel.encode("utf-8"))
        digest.update(_file_marker(root / rel))
    for rel in _FINGERPRINT_EXECUTABLES:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x01" if (root / rel).exists() else b"\x00")
    return digest.hexdigest()


def _file_marker(path: Path) -> bytes:
    try:
        if not path.is_file():
            return b"<absent>"
        with path.open("rb") as fh:
            return hashlib.sha256(fh.read(_MAX_HASH_BYTES)).digest()
    except OSError:
        return b"<error>"


def compute_config_signature(checks: ChecksConfig) -> str:
    """Return a hex digest over the config fields that affect the resolved profile content.

    Covers ``checks.commands`` and ``checks.discovery.mode`` — the two fields whose change must
    invalidate the cached profile.
    """
    from wastech_orchestrator.checks.model import normalize_commands  # noqa: PLC0415

    cmds = normalize_commands(checks.commands)
    parts = sorted(c.name + "\x00" + "\x00".join(c.argv) for c in cmds)
    mode = checks.discovery.mode.value
    payload = mode + "\x1e" + "\x1e".join(parts)
    return hashlib.sha256(payload.encode()).hexdigest()


def _combine(repo_fp: str, config_sig: str) -> str:
    """Combine the repo and config fingerprints into one stable hash."""
    return hashlib.sha256(f"{repo_fp}:{config_sig}".encode()).hexdigest()


def _workflow_files(root: Path) -> list[str]:
    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return []
    try:
        return [
            f".github/workflows/{p.name}"
            for p in workflows.iterdir()
            if p.suffix in (".yml", ".yaml") and p.is_file()
        ]
    except OSError:
        return []
