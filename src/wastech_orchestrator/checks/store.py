"""Resolved-profile persistence (backlog: automatic check discovery, §10).

Stores the resolved profile at ``<artifacts_root>/checks/resolved-profile.json`` (the control
workspace). Writes are atomic (temp + replace) so an interrupted write never leaves a half-profile.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from wastech_orchestrator.checks.profile import ResolvedCheckProfile

PROFILE_FILENAME = "resolved-profile.json"


class ResolvedCheckProfileStore:
    """Load/save the resolved :class:`ResolvedCheckProfile` for a control workspace."""

    def __init__(self, checks_dir: str | Path) -> None:
        self._dir = Path(checks_dir)
        self._path = self._dir / PROFILE_FILENAME

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> ResolvedCheckProfile | None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            data = json.loads(text)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return ResolvedCheckProfile.from_json(data)

    def save(self, profile: ResolvedCheckProfile) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(profile.to_json(), indent=2, ensure_ascii=False) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=self._dir, prefix=".profile-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp_name, self._path)
        except OSError:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return str(self._path)
