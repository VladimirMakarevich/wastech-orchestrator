"""Per-user registry binding a repo root to its generated ``config.yaml``.

``install`` records ``<resolved repo root> -> <absolute config.yaml path>`` here so that
``preflight`` / ``watch`` / ``status`` can find the config from anywhere inside the repo without
``--config`` (config discovery, step 3). The file lives in the per-user config directory resolved by
``platformdirs`` — ``%LOCALAPPDATA%`` on Windows, ``~/Library/Application Support`` on macOS, the
XDG config dir on Linux — or under ``WASTECH_ORCHESTRATOR_HOME`` when that env var is set (used by
tests so they never touch the real user directory).

Stored as JSON. Reads tolerate a missing or corrupt file (treated as empty). Writes are atomic
(temp file in the same directory + ``os.replace``). No secrets are ever stored — only paths.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir

_APP_NAME = "wastech-orchestrator"
_REGISTRY_FILENAME = "registry.json"
_REGISTRY_VERSION = 1
# Env override for the registry directory; lets tests redirect it away from the real user config.
HOME_ENV = "WASTECH_ORCHESTRATOR_HOME"


def registry_dir() -> Path:
    """The registry's directory: ``$WASTECH_ORCHESTRATOR_HOME`` or the per-user config dir."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override)
    return Path(user_config_dir(_APP_NAME, appauthor=False, roaming=False))


def registry_path() -> Path:
    """Absolute path to the registry JSON file."""
    return registry_dir() / _REGISTRY_FILENAME


def _normalize(root: Path | str) -> str:
    """Canonical key for a repo root (absolute, with symlinks/case resolved)."""
    return str(Path(root).resolve())


def _read_bindings() -> dict[str, str]:
    """Load the bindings map, tolerating a missing or malformed file (returns ``{}``).

    Intentionally a **forward-tolerant** reader: it reads ``bindings`` regardless of the file's
    ``version``. Bindings are plain repo→config paths and are version-stable, so config discovery
    must never hard-fail on a registry written by a newer orchestrator (unlike the config/DB schema
    gates, which do refuse newer versions).
    """
    try:
        raw: Any = json.loads(registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    bindings = raw.get("bindings")
    if not isinstance(bindings, dict):
        return {}
    return {str(key): value for key, value in bindings.items() if isinstance(value, str)}


def _write_bindings(bindings: dict[str, str]) -> None:
    """Atomically persist the bindings map (temp file in the same dir + ``os.replace``)."""
    directory = registry_dir()
    directory.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"version": _REGISTRY_VERSION, "bindings": dict(sorted(bindings.items()))},
        indent=2,
        ensure_ascii=False,
    )
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=".registry-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        os.replace(tmp_name, registry_path())
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)  # never leave a temp file behind
        raise


def lookup(repo_root: Path | str) -> str | None:
    """Return the bound ``config.yaml`` path for ``repo_root``, or ``None`` if unbound."""
    return _read_bindings().get(_normalize(repo_root))


def bind(repo_root: Path | str, config_path: Path | str) -> None:
    """Record ``repo_root -> config_path`` (both absolute), replacing any previous binding."""
    bindings = _read_bindings()
    bindings[_normalize(repo_root)] = str(Path(config_path).resolve())
    _write_bindings(bindings)


def unbind(repo_root: Path | str) -> None:
    """Remove the binding for ``repo_root`` if present (a no-op when absent)."""
    bindings = _read_bindings()
    if bindings.pop(_normalize(repo_root), None) is not None:
        _write_bindings(bindings)
