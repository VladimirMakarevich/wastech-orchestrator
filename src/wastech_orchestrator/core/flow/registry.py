"""Flow registry — resolves task_type → validated FlowSnapshot (P0.4).

Two lookup layers, in priority order:

  1. **Operator flows**: ``<operator_flows_dir>/<task_type>.yaml`` — allows operators to
     override or extend built-ins by placing YAML files in ``<repo>/.worc/flows/``.
  2. **Packaged built-ins**: shipped with the wheel under ``packaged/flows/`` (``implementation``,
     ``deep_research``, ``security_audit``).

Resolution rules:

- ``task_type=None`` defaults to :data:`DEFAULT_TASK_TYPE` (``"implementation"``).
- Unknown ``task_type`` raises :class:`FlowResolutionError` before any side-effects.
- The ``task_type`` field inside the YAML must match the lookup key; mismatch raises
  :class:`FlowResolutionError` (prevents a misconfigured operator file being used under
  the wrong dispatch key).
- :func:`~.validator.validate_flow` is always called before returning the snapshot; a
  flow that violates graph or ceiling rules raises
  :class:`~.validator.FlowValidationError`. When the registry is constructed with a *config*,
  :func:`~.validator.validate_flow_against_config` runs too (P4.2 config-aware layer): node
  providers/reasoning ∈ allowlist, ``permission_ceiling`` ≤ a configured provider, PR-publishing
  needs git, and ``budgets`` ≤ the config safety caps.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.flow.snapshot import FlowLoadError, FlowSnapshot, load_flow
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    validate_flow,
    validate_flow_against_config,
)

DEFAULT_TASK_TYPE: str = "implementation"

# Built-in flow YAML + role prompts ship as package data under the aggregated
# ``wastech_orchestrator/packaged/flows/`` tree (the single home for everything shipped/seeded);
# the flow-engine code lives here in ``core/flow/``.
_PACKAGED_DIR: Path = Path(__file__).resolve().parents[2] / "packaged" / "flows"


class FlowResolutionError(Exception):
    """Raised when task_type cannot be mapped to a known flow."""


class FlowRegistry:
    """Resolve ``task_type`` → validated :class:`~.snapshot.FlowSnapshot`.

    Built-in flows ship in ``packaged/``; operator flows live in ``<repo>/.worc/flows/`` and take
    priority when present. When constructed with a *config*, every resolved snapshot also passes the
    config-aware validator (P4.2); without one, only the config-free graph + ceiling layers run
    (used by unit tests that have no config).
    """

    def __init__(
        self,
        operator_flows_dir: Path | None = None,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self._operator_dir = operator_flows_dir
        self._config = config

    def resolve(self, task_type: str | None = None) -> FlowSnapshot:
        """Return the validated snapshot for *task_type*.

        ``task_type=None`` resolves to :data:`DEFAULT_TASK_TYPE`.

        :raises FlowResolutionError: if no flow file is found for *task_type*, or if the
            YAML ``flow.task_type`` field does not match the requested key.
        :raises ~.snapshot.FlowLoadError: if the YAML is malformed.
        :raises ~.validator.FlowValidationError: if the snapshot fails validation (structural or,
            when a config is set, config-aware).
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
        if self._config is not None:
            validate_flow_against_config(snap, self._config)
        return snap

    def validate_all(self) -> list[tuple[str, str | None]]:
        """Load + validate **every** resolvable flow (packaged + operator) for a preflight gate.

        Returns ``(name, error)`` per flow, where *name* is the file stem and *error* is ``None`` on
        success or a one-line message on failure. Operator files shadow a packaged file of the same
        name (only the operator one is reported, mirroring :meth:`resolve`). Does not raise — the
        caller decides how to surface failures (``install``/``preflight`` treat any as fatal).
        """
        results: list[tuple[str, str | None]] = []
        for name in self._all_flow_names():
            try:
                self.resolve(name)
                results.append((name, None))
            except (FlowResolutionError, FlowLoadError, FlowValidationError) as exc:
                results.append((name, str(exc)))
        return results

    def _find(self, task_type: str) -> Path | None:
        if self._operator_dir is not None:
            candidate = self._operator_dir / f"{task_type}.yaml"
            if candidate.is_file():
                return candidate
        candidate = _PACKAGED_DIR / f"{task_type}.yaml"
        if candidate.is_file():
            return candidate
        return None

    def _all_flow_names(self) -> list[str]:
        """Every flow file stem across packaged + operator dirs (operator shadows packaged)."""
        names = set(self._builtin_names())
        if self._operator_dir is not None and self._operator_dir.is_dir():
            names.update(p.stem for p in self._operator_dir.glob("*.yaml"))
        return sorted(names)

    @staticmethod
    def _builtin_names() -> list[str]:
        return sorted(p.stem for p in _PACKAGED_DIR.glob("*.yaml"))
