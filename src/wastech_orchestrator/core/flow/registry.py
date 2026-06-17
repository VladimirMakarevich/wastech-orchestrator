"""Flow registry — resolves task_type → validated FlowSnapshot (P0.4).

Two lookup layers, in priority order:

  1. **Operator flows**: ``<operator_flows_dir>/<task_type>.yaml`` — allows operators to
     override or extend built-ins by placing YAML files in ``<repo>/.worc/flows/``.
  2. **Packaged built-ins**: shipped with the wheel under ``packaged/`` (``implementation``,
     ``deep_research``, ``security_audit``).

Resolution rules:

- ``task_type=None`` defaults to :data:`DEFAULT_TASK_TYPE` (``"implementation"``).
- Unknown ``task_type`` raises :class:`FlowResolutionError` before any side-effects.
- The ``task_type`` field inside the YAML must match the lookup key; mismatch raises
  :class:`FlowResolutionError` (prevents a misconfigured operator file being used under
  the wrong dispatch key).
- :func:`~.validator.validate_flow` is always called before returning the snapshot; a
  flow that violates graph or ceiling rules raises
  :class:`~.validator.FlowValidationError`.

See ``docs/backlog/flows/plan.md`` (P0.4) and ``flow-contract.md`` §10.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.flow.snapshot import FlowSnapshot, load_flow
from wastech_orchestrator.core.flow.validator import validate_flow

DEFAULT_TASK_TYPE: str = "implementation"

_PACKAGED_DIR: Path = Path(__file__).parent / "packaged"


class FlowResolutionError(Exception):
    """Raised when task_type cannot be mapped to a known flow."""


class FlowRegistry:
    """Resolve ``task_type`` → validated :class:`~.snapshot.FlowSnapshot`.

    Built-in flows ship in ``packaged/``; operator flows live in
    ``<repo>/.worc/flows/`` and take priority when present.
    """

    def __init__(self, operator_flows_dir: Path | None = None) -> None:
        self._operator_dir = operator_flows_dir

    def resolve(self, task_type: str | None = None) -> FlowSnapshot:
        """Return the validated snapshot for *task_type*.

        ``task_type=None`` resolves to :data:`DEFAULT_TASK_TYPE`.

        :raises FlowResolutionError: if no flow file is found for *task_type*, or if the
            YAML ``flow.task_type`` field does not match the requested key.
        :raises ~.snapshot.FlowLoadError: if the YAML is malformed.
        :raises ~.validator.FlowValidationError: if the snapshot fails validation.
        """
        effective = task_type or DEFAULT_TASK_TYPE
        path = self._find(effective)
        if path is None:
            raise FlowResolutionError(
                f"unknown task_type {effective!r}: no flow file found "
                f"(packaged built-ins: {self._builtin_names()})"
            )
        snap = load_flow(path)
        if snap.doc.task_type != effective:
            raise FlowResolutionError(
                f"task_type mismatch in {path}: "
                f"requested {effective!r} but flow declares {snap.doc.task_type!r}"
            )
        validate_flow(snap)
        return snap

    def _find(self, task_type: str) -> Path | None:
        if self._operator_dir is not None:
            candidate = self._operator_dir / f"{task_type}.yaml"
            if candidate.is_file():
                return candidate
        candidate = _PACKAGED_DIR / f"{task_type}.yaml"
        if candidate.is_file():
            return candidate
        return None

    @staticmethod
    def _builtin_names() -> list[str]:
        return sorted(p.stem for p in _PACKAGED_DIR.glob("*.yaml"))
