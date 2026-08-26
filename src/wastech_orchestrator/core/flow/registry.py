"""Flow registry — resolves task_type → validated FlowSnapshot.

Flows resolve from exactly one place: the operator's ``<repo>/.worc/flows/<task_type>.yaml``. The
bundled ``packaged/flows/`` tree is **delivery-only** — ``worc install`` copies it into ``.worc/``
so the operator gets editable, active copies — and is never read at task-execution time. A requested
``task_type`` with no file in ``.worc/flows/`` is an explicit :class:`FlowResolutionError`, not a
silent fall-back to the bundled copy: what the operator sees in their repo is exactly what the
orchestrator runs.

Resolution rules:

- ``task_type=None`` defaults to :data:`DEFAULT_TASK_TYPE` (``"implementation"``).
- Unknown ``task_type`` (no ``.worc/flows/<task_type>.yaml``) raises :class:`FlowResolutionError`
  before any side-effects, naming the missing file and how to deliver the built-ins.
- The ``task_type`` field inside the YAML must match the lookup key; mismatch raises
  :class:`FlowResolutionError` (prevents a misconfigured operator file being used under
  the wrong dispatch key).
- :func:`~.validator.validate_flow` is always called before returning the snapshot; a
  flow that violates graph or ceiling rules raises
  :class:`~.validator.FlowValidationError`. When the registry is constructed with a *config*,
  :func:`~.validator.validate_flow_against_config` runs too (the config-aware layer): node
  providers/reasoning ∈ allowlist, ``permission_ceiling`` ≤ a configured provider's capability, no
  Codex node with both ``workspace-write`` and network (unless the advanced mode grants the network
  to every node anyway), every ``tool`` node's name resolvable under ``.worc/tools/``, a flow-local
  ``supervisor.observe.mode`` that does not *widen* the global cadence, and no ``extra_args``
  selecting a provider full-access mode — refused at every value of ``security.strict_isolation``.
  Flow ``budgets`` vs ``agents.max_*`` and ``publishing`` vs the git config are
  deliberately **not** checked here: the engine clamps a budget to ``min(flow, cap)`` and
  ``git.create_pull_request: false`` is a supported local-commit mode (see the validator's module
  docstring).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.flow.snapshot import FlowLoadError, FlowSnapshot, load_flow
from wastech_orchestrator.core.flow.tools_registry import ToolRegistry
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    lint_prompt_variables,
    validate_flow,
    validate_flow_against_config,
)

DEFAULT_TASK_TYPE: str = "implementation"


class FlowResolutionError(Exception):
    """Raised when task_type cannot be mapped to a known flow."""


@dataclass(frozen=True)
class FlowCheck:
    """Result of validating one flow for ``worc validate-flow``: fatal error + non-fatal warnings.

    *error* is ``None`` when the flow passes the full validator, else a one-line message. *warnings*
    are the prompt-variable lint messages (empty for a clean or an unresolved flow — a broken flow's
    fatal error is the signal, not its lint).
    """

    name: str
    error: str | None
    warnings: tuple[str, ...]


class FlowRegistry:
    """Resolve ``task_type`` → validated :class:`~.snapshot.FlowSnapshot`.

    Flows live in the operator's ``<repo>/.worc/flows/`` — the sole resolution source. The bundled
    ``packaged/`` tree is delivery-only (``worc install`` copies it into ``.worc/``) and is never
    read here. When constructed with a *config*, every resolved snapshot also passes the
    config-aware validator; without one, only the config-free graph + ceiling layers run
    (used by unit tests that have no config).
    """

    def __init__(
        self,
        operator_flows_dir: Path | None = None,
        config: OrchestratorConfig | None = None,
    ) -> None:
        self._operator_dir = operator_flows_dir
        self._config = config
        # Operator tools live in the sibling ``<worc-home>/tools/`` of the flows dir (both under
        # ``.worc/``). Built here so config-aware resolution validates a ``tool`` node's name
        # fail-closed; ``None`` (no operator layer, e.g. a bare unit test) has no flows dir, so
        # nothing resolves and tool nodes are unresolvable — a flow that uses one is rejected.
        self._tools = (
            ToolRegistry(operator_flows_dir.parent / "tools")
            if operator_flows_dir is not None
            else None
        )

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
            op_dir = self._operator_dir
            where = op_dir.as_posix() if op_dir is not None else ".worc/flows"
            available = self.operator_flow_names()
            hint = ", ".join(available) if available else "none"
            raise FlowResolutionError(
                f"unknown task_type {effective!r}: no flow file {effective}.yaml in {where} "
                f"(available: {hint}). Run `worc install` to deliver the built-in flows into "
                f".worc/flows/, or add {effective}.yaml there."
            )
        snap = load_flow(path)
        if snap.doc.task_type != effective:
            raise FlowResolutionError(
                f"task_type mismatch in {path}: "
                f"requested {effective!r} but flow declares {snap.doc.task_type!r}"
            )
        validate_flow(snap)
        if self._config is not None:
            validate_flow_against_config(snap, self._config, self._tools)
        return snap

    def check_flows(self, names: Iterable[str]) -> list[FlowCheck]:
        """Resolve + prompt-lint each named flow, config-aware, no-raise (``validate-flow`` seam).

        Per name: resolve through the full fatal validator (graph + ceiling + the config-aware layer
        when a config is set, incl. the ``.worc/tools/`` tool check) — exactly what the engine sees
        at dispatch; on success also lint the role prompts for unknown ``{name}`` tokens. Returns a
        :class:`FlowCheck` per name (``error`` set on failure, ``warnings`` the non-fatal lint).
        Does not raise — the caller decides how to surface failures and the exit code.
        """
        results: list[FlowCheck] = []
        for name in names:
            try:
                snap = self.resolve(name)
            except (FlowResolutionError, FlowLoadError, FlowValidationError) as exc:
                results.append(FlowCheck(name=name, error=str(exc), warnings=()))
                continue
            warnings = tuple(
                f"{w.role_file} references unknown {{{w.token}}}"
                for w in lint_prompt_variables(snap)
            )
            results.append(FlowCheck(name=name, error=None, warnings=warnings))
        return results

    def operator_flow_names(self) -> list[str]:
        """Flow file stems in the operator's ``.worc/flows/`` only (packaged built-ins excluded).

        The discovery scope for ``worc validate-flow``: operator-authored flows are the only ones
        validated on demand — packaged built-ins are covered by the orchestrator's own test suite,
        not by validating them against an arbitrary target repo's config. Empty when there is no
        operator dir or it holds no ``*.yaml``.
        """
        if self._operator_dir is None or not self._operator_dir.is_dir():
            return []
        return sorted(p.stem for p in self._operator_dir.glob("*.yaml"))

    def _find(self, task_type: str) -> Path | None:
        """The operator flow file for *task_type*, or ``None`` when absent.

        Resolves only under the operator's ``.worc/flows/`` — the bundled ``packaged/`` tree is
        delivery-only and is never consulted at run time.
        """
        if self._operator_dir is None:
            return None
        candidate = self._operator_dir / f"{task_type}.yaml"
        return candidate if candidate.is_file() else None
