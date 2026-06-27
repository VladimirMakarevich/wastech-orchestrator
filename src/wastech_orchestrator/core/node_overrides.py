"""Resolve a task's per-node model/reasoning/provider overrides into a validated field overlay.

A task may overlay the flow node's declared executor for one run via
``nodes.<node-id>.{model,reasoning,provider}`` (see
:class:`~wastech_orchestrator.task.model.NodeOverride`), so one default flow can cover several
model/effort/provider variants without a separate flow file. The override is **best-effort**: the
task gate validated only its *shape*; here the field is checked against the resolved flow + config,
and an invalid field is warned + skipped (falling back to the flow's declared value) rather than
aborting the task — autonomous ``watch`` admission must never be blocked by a well-formed-but-
unsupported value.

This lives in ``core`` (not ``core/flow``) on purpose: it reads the task model, and ``core/flow``
stays free of any ``task`` dependency. The resolver emits a primitive overlay
(``node_id -> {field: value}``) that the engine applies mechanically at its single node-fetch seam —
the engine never learns what a field means (the same separation ``disabled_nodes`` keeps).

Validation reuses the config-aware flow validator's checks (``agents.allowed`` membership,
:func:`~wastech_orchestrator.providers.capabilities.is_reasoning_supported`, and the shared
:func:`~wastech_orchestrator.core.flow.validator.global_primary`); there is deliberately **no**
model check — model names have no reliable tier ordering, so a model string is passed through
unverified (the provider config still supplies the default when none is given).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.flow.schema import AgentNode, EvaluatorNode
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.flow.validator import global_primary
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.providers.capabilities import (
    all_reasoning_levels,
    is_reasoning_supported,
    reasoning_levels_for,
)
from wastech_orchestrator.task.model import NodeOverride


@dataclass(frozen=True)
class NodeOverrideResolution:
    """The outcome of resolving a task's per-node overrides against its flow + config.

    ``overlay`` maps a flow node id to the **valid** field overrides to overlay onto that node
    (``provider`` already coerced to :class:`ProviderId`); only nodes with at least one valid field
    appear. ``warnings`` describes every skipped field (and why) so the orchestrator can log them
    once per run — a skipped field is never fatal.
    """

    overlay: Mapping[str, Mapping[str, object]]
    warnings: tuple[str, ...]


def resolve_node_overrides(
    snapshot: FlowSnapshot,
    node_overrides: Mapping[str, NodeOverride],
    config: OrchestratorConfig,
) -> NodeOverrideResolution:
    """Validate a task's ``nodes.<id>.{model,reasoning,provider}`` overrides into a field overlay.

    Best-effort: an override targeting an unknown node, a non-agent/evaluator node, a provider not
    in ``agents.allowed``, or a reasoning level the resolved provider rejects is dropped with a
    warning; ``model`` is passed through unchecked. The flow's declared value stands wherever a
    field is dropped. Only the ``enabled`` toggle is handled elsewhere (the engine's
    ``disabled_nodes``).
    """
    overlay: dict[str, dict[str, object]] = {}
    warnings: list[str] = []
    allowed = frozenset(config.agents.allowed)
    primary = global_primary(config)

    for node_id, override in node_overrides.items():
        if override.model is None and override.reasoning is None and override.provider is None:
            continue  # disable-only override (``enabled``) — nothing to overlay here
        node = snapshot.nodes_by_id.get(node_id)
        if node is None:
            warnings.append(f"node {node_id!r}: override ignored (no such node in the flow)")
            continue
        if not isinstance(node, AgentNode | EvaluatorNode):
            warnings.append(
                f"node {node_id!r} ({node.kind}): takes no model/reasoning/provider override; "
                "ignored"
            )
            continue

        fields: dict[str, object] = {}

        if override.provider is not None:
            provider = _coerce_provider(override.provider)
            if provider is None or provider not in allowed:
                warnings.append(
                    f"node {node_id!r}: provider {override.provider!r} not in agents.allowed "
                    f"{sorted(p.value for p in allowed)}; using the flow's provider"
                )
            else:
                fields["provider"] = provider

        if override.reasoning is not None:
            resolved_provider = fields.get("provider") or node.provider or primary
            assert resolved_provider is None or isinstance(resolved_provider, ProviderId)
            if _reasoning_ok(resolved_provider, override.reasoning):
                fields["reasoning"] = override.reasoning
            else:
                warnings.append(
                    f"node {node_id!r}: reasoning {override.reasoning!r} "
                    f"{_reasoning_expectation(resolved_provider)}; using the flow's reasoning"
                )

        if override.model is not None:
            fields["model"] = override.model  # passthrough — no reliable model tier ordering

        if fields:
            overlay[node_id] = fields

    return NodeOverrideResolution(overlay=overlay, warnings=tuple(warnings))


def _coerce_provider(value: str) -> ProviderId | None:
    try:
        return ProviderId(value)
    except ValueError:
        return None


def _reasoning_ok(provider: ProviderId | None, reasoning: str) -> bool:
    # No provider resolves (none declared and no single global primary): fall back to the broad
    # structural allowlist, mirroring the config-aware validator's reasoning check.
    if provider is None:
        return reasoning in all_reasoning_levels()
    return is_reasoning_supported(provider, reasoning)


def _reasoning_expectation(provider: ProviderId | None) -> str:
    if provider is None:
        return f"not in {sorted(all_reasoning_levels())}"
    return (
        f"is not supported by provider {provider.value!r} "
        f"(expected one of {sorted(reasoning_levels_for(provider))})"
    )
