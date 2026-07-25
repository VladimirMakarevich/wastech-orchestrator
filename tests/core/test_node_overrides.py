"""Unit tests for the task per-node override resolver (``core.node_overrides``).

The resolver overlays a task's ``nodes.<id>.{model,reasoning,provider}`` onto the resolved flow,
best-effort: an override invalid for the flow/config is warned + skipped (the flow's declared value
stands) — never fatal, so autonomous ``watch`` admission is never blocked.
"""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.node_overrides import resolve_node_overrides
from wastech_orchestrator.providers.base import ProviderId
from wastech_orchestrator.task.model import NodeOverride


def _snap(tmp_path: Path):  # type: ignore[no-untyped-def]
    # A minimal flow: one agent node (defaults to the global primary), one evaluator, one publish.
    content = """\
flow:
  name: t
  task_type: t
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: implementation
      kind: agent
      role_file: roles/impl.md
      provider: claude
    - id: review
      kind: evaluator
      role: reviewer
      role_file: roles/review.md
      provider: codex
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - { from: implementation, to: review }
    - { from: review, to: out, outcome: accept }
    - { from: review, to: implementation, outcome: rework, budget: 1 }
"""
    p = tmp_path / "flow.yaml"
    p.write_text(content)
    return load_flow(p)


def _config(tmp_path: Path, *, allowed: str = "[claude, codex]") -> OrchestratorConfig:
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
      permission_profile: workspace-write
      primary: true
    codex:
      command: "codex"
      permission_profile: workspace-write
security:
  strict_isolation: false
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
    return loads_config(text).config


def test_empty_overrides_yield_empty_overlay(tmp_path: Path) -> None:
    res = resolve_node_overrides(_snap(tmp_path), {}, _config(tmp_path))
    assert res.overlay == {}
    assert res.warnings == ()


def test_disable_only_override_is_not_an_overlay(tmp_path: Path) -> None:
    # ``enabled: false`` is the engine's disabled_nodes concern, not a field overlay.
    res = resolve_node_overrides(
        _snap(tmp_path), {"implementation": NodeOverride(enabled=False)}, _config(tmp_path)
    )
    assert res.overlay == {}
    assert res.warnings == ()


def test_valid_model_reasoning_provider_overlay(tmp_path: Path) -> None:
    override = NodeOverride(model="claude-opus-5", reasoning="high", provider="codex")
    res = resolve_node_overrides(_snap(tmp_path), {"implementation": override}, _config(tmp_path))
    assert res.warnings == ()
    assert res.overlay == {
        "implementation": {
            "provider": ProviderId.CODEX,
            "reasoning": "high",
            "model": "claude-opus-5",
        }
    }


def test_model_is_passed_through_unchecked(tmp_path: Path) -> None:
    # No model tier/ceiling check — any non-empty string survives.
    override = NodeOverride(model="some-experimental-model")
    res = resolve_node_overrides(_snap(tmp_path), {"implementation": override}, _config(tmp_path))
    assert res.overlay == {"implementation": {"model": "some-experimental-model"}}
    assert res.warnings == ()


def test_provider_not_in_allowed_is_skipped_with_warning(tmp_path: Path) -> None:
    res = resolve_node_overrides(
        _snap(tmp_path),
        {"implementation": NodeOverride(provider="codex")},
        _config(tmp_path, allowed="[claude]"),
    )
    assert res.overlay == {}
    assert any("provider" in w and "implementation" in w for w in res.warnings)


def test_unknown_provider_value_is_skipped_with_warning(tmp_path: Path) -> None:
    res = resolve_node_overrides(
        _snap(tmp_path), {"implementation": NodeOverride(provider="gpt")}, _config(tmp_path)
    )
    assert res.overlay == {}
    assert any("provider" in w for w in res.warnings)


def test_reasoning_unsupported_for_provider_is_skipped(tmp_path: Path) -> None:
    # ``minimal`` is a Codex level, not a Claude one; the implementation node resolves to claude.
    res = resolve_node_overrides(
        _snap(tmp_path), {"implementation": NodeOverride(reasoning="minimal")}, _config(tmp_path)
    )
    assert res.overlay == {}
    assert any("reasoning" in w and "minimal" in w for w in res.warnings)


def test_reasoning_validated_against_override_provider(tmp_path: Path) -> None:
    # The implementation node is claude, but the override switches it to codex, which supports
    # ``minimal`` — so the reasoning override must be accepted against the *override* provider.
    res = resolve_node_overrides(
        _snap(tmp_path),
        {"implementation": NodeOverride(provider="codex", reasoning="minimal")},
        _config(tmp_path),
    )
    assert res.warnings == ()
    assert res.overlay == {"implementation": {"provider": ProviderId.CODEX, "reasoning": "minimal"}}


def test_unknown_node_is_skipped_with_warning(tmp_path: Path) -> None:
    res = resolve_node_overrides(
        _snap(tmp_path), {"nope": NodeOverride(model="x")}, _config(tmp_path)
    )
    assert res.overlay == {}
    assert any("nope" in w and "no such node" in w for w in res.warnings)


def test_non_agent_node_override_is_skipped_with_warning(tmp_path: Path) -> None:
    res = resolve_node_overrides(
        _snap(tmp_path), {"out": NodeOverride(model="x")}, _config(tmp_path)
    )
    assert res.overlay == {}
    assert any("out" in w and "publish" in w for w in res.warnings)


def test_partial_validity_keeps_valid_fields(tmp_path: Path) -> None:
    # A valid model + an invalid provider: keep the model, drop the provider, warn once.
    res = resolve_node_overrides(
        _snap(tmp_path),
        {"implementation": NodeOverride(model="opus", provider="gpt")},
        _config(tmp_path),
    )
    assert res.overlay == {"implementation": {"model": "opus"}}
    assert len(res.warnings) == 1 and "provider" in res.warnings[0]
