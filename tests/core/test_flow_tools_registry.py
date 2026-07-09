"""Operator tool registry + config-aware / preflight validation (P5.2).

The ``ToolRegistry`` is the fail-closed boundary for the free-string ``tool`` name: it resolves only
a contained, existing, executable file under ``.worc/tools/`` and rejects everything else before any
side effect. The flow validator (config-aware layer) and the install/preflight gate use it so a flow
that names an unregistered tool is fatal *before* a task starts, never mid-run.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from wastech_orchestrator.config.loader import loads_config
from wastech_orchestrator.config.schema import OrchestratorConfig
from wastech_orchestrator.core.flow.registry import FlowRegistry
from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.tools_registry import ToolRegistry, ToolResolutionError
from wastech_orchestrator.core.flow.validator import (
    FlowValidationError,
    validate_flow,
    validate_flow_against_config,
)


def _install_tool(tools_dir: Path, base: str = "md-check") -> str:
    """Write an OS-appropriate executable tool and return the name a flow references.

    POSIX: a shebang file with the ``+x`` bit. Windows: a ``.bat`` (launchable by suffix, no x-bit).
    Returns the registered name (with the ``.bat`` suffix on Windows) so callers stay portable.
    """
    tools_dir.mkdir(parents=True, exist_ok=True)
    name = f"{base}.bat" if os.name == "nt" else base
    path = tools_dir / name
    path.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    if os.name != "nt":
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return name


def _tool_flow(task_type: str, tool_name: str) -> str:
    return f"""\
flow:
  name: {task_type}
  task_type: {task_type}
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  nodes:
    - id: md-check
      kind: tool
      tool: {tool_name}
    - id: out
      kind: publish
      policy: pull_request
  edges:
    - {{ from: md-check, to: out, outcome: pass }}
"""


def _config(tmp_path: Path) -> OrchestratorConfig:
    text = f"""
repo:
  url: "git@example.com:o/r.git"
  local_path: {str(tmp_path)!r}
  base_branch: "main"
  branch_prefix: "worc"
agents:
  allowed: [claude, codex]
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
  strict_isolation: true
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


# -- ToolRegistry unit behavior ----------------------------------------------


def test_tool_registry_resolves_operator_tool(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    name = _install_tool(tools)
    resolved = ToolRegistry(tools).resolve(name)
    assert resolved.name == name
    assert resolved.is_file()


def test_tool_registry_unknown_fails_before_side_effects(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    with pytest.raises(ToolResolutionError, match="not found"):
        ToolRegistry(tools).resolve("does-not-exist")


def test_tool_registry_rejects_non_executable(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    plain = tools / "plain"
    plain.write_text("not executable\n", encoding="utf-8")  # no +x bit / no launchable suffix
    if os.name == "nt":
        pytest.skip("Windows executability is by suffix; an extensionless file is inert")
    with pytest.raises(ToolResolutionError, match="not executable"):
        ToolRegistry(tools).resolve("plain")


def test_tool_registry_rejects_path_outside_dir(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    with pytest.raises(ToolResolutionError, match="outside the tools directory"):
        ToolRegistry(tools).resolve("../escape")


def test_tool_registry_rejects_symlink_escape(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    outside = tmp_path / "evil"
    outside.write_text("#!/bin/sh\n", encoding="utf-8")
    if os.name != "nt":
        outside.chmod(0o755)
    try:
        (tools / "link").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform/filesystem")
    with pytest.raises(ToolResolutionError, match="outside the tools directory"):
        ToolRegistry(tools).resolve("link")


# -- cross-platform name resolution (both os.name branches) ------------------


def test_resolve_posix_finds_bare_extensionless_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # POSIX: one fixed flow name resolves to the extensionless +x script (as delivered).
    monkeypatch.setattr(os, "name", "posix")
    tools = tmp_path / "tools"
    tools.mkdir()
    script = tools / "check_journey"
    script.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    resolved = ToolRegistry(tools).resolve("check_journey")
    assert resolved.name == "check_journey"


def test_resolve_windows_finds_cmd_wrapper_for_bare_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows: the same bare flow name resolves to the launchable `.cmd` sibling. The extensionless
    # file (delivered too) is inert on Windows, so the suffix iteration is what makes one packaged
    # flow work on both OSes.
    monkeypatch.setattr(os, "name", "nt")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "check_journey").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (tools / "check_journey.cmd").write_text('@python "%~dp0check_journey" %*\n', encoding="utf-8")
    resolved = ToolRegistry(tools).resolve("check_journey")
    assert resolved.name == "check_journey.cmd"


def test_resolve_windows_without_wrapper_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the inert extensionless file is present → no launchable candidate → fail-closed.
    monkeypatch.setattr(os, "name", "nt")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "check_journey").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    with pytest.raises(ToolResolutionError, match="not executable"):
        ToolRegistry(tools).resolve("check_journey")


def test_resolve_windows_traversal_still_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The suffix iteration never weakens containment: a name traversal is fatal on Windows too.
    monkeypatch.setattr(os, "name", "nt")
    tools = tmp_path / "tools"
    tools.mkdir()
    with pytest.raises(ToolResolutionError, match="outside the tools directory"):
        ToolRegistry(tools).resolve("../escape")


# -- config-aware flow validation --------------------------------------------


def test_unknown_tool_rejected_fail_closed(tmp_path: Path) -> None:
    # A tool node naming a tool absent from the registry is a fatal config violation (pre-launch).
    flow = tmp_path / "custom.yaml"
    flow.write_text(_tool_flow("custom", "md-check"))
    snap = load_flow(flow)
    validate_flow(snap)  # structurally fine
    empty_tools = ToolRegistry(tmp_path / "tools")  # dir does not exist → nothing registered
    with pytest.raises(FlowValidationError) as exc:
        validate_flow_against_config(snap, _config(tmp_path), empty_tools)
    assert any(v.category == "config" and "md-check" in v.message for v in exc.value.violations)


def test_registered_tool_passes_config_validation(tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    name = _install_tool(tools)
    flow = tmp_path / "custom.yaml"
    flow.write_text(_tool_flow("custom", name))
    snap = load_flow(flow)
    validate_flow_against_config(snap, _config(tmp_path), ToolRegistry(tools))  # no raise


# -- preflight (FlowRegistry.validate_all) ------------------------------------


def test_preflight_validates_tools(tmp_path: Path) -> None:
    # The FlowRegistry derives the tools dir from the sibling of the flows dir; validate_all reports
    # a flow referencing a missing tool as FAIL and a registered one as OK — the preflight gate.
    worc = tmp_path / ".worc"
    flows = worc / "flows"
    flows.mkdir(parents=True)
    (flows / "custom.yaml").write_text(_tool_flow("custom", "md-check"))
    config = _config(tmp_path)

    missing = dict(FlowRegistry(operator_flows_dir=flows, config=config).validate_all())
    assert missing["custom"] is not None and "md-check" in missing["custom"]

    _install_tool(worc / "tools")  # register the tool in the sibling .worc/tools/
    present = dict(FlowRegistry(operator_flows_dir=flows, config=config).validate_all())
    assert present["custom"] is None  # now OK


def test_packaged_content_flows_require_delivered_check_journey(tmp_path: Path) -> None:
    # The §5 coupling: because the content flows are packaged, preflight validates them config-aware
    # in EVERY repo, so their `check_journey` tool must be delivered — which `worc install` does. An
    # empty operator flows dir still points the registry at the sibling `.worc/tools/`.
    worc = tmp_path / ".worc"
    flows = worc / "flows"
    flows.mkdir(parents=True)
    config = _config(tmp_path)
    content = ("content_chapter", "content_book", "content_translate")

    missing = dict(FlowRegistry(operator_flows_dir=flows, config=config).validate_all())
    for name in content:
        assert missing[name] is not None and "check_journey" in missing[name]

    _install_tool(worc / "tools", base="check_journey")  # as `worc install` delivers it
    present = dict(FlowRegistry(operator_flows_dir=flows, config=config).validate_all())
    for name in content:
        assert present[name] is None
