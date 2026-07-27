"""Unit tests for the WRI-010 frozen control bundle.

The freeze copies the exact control inputs a flow references (flow YAML, role files, tool
executables) into a private immutable bundle, records a manifest + digest, binds later consumers to
the frozen bytes, and lets the orchestrator re-hash the live inputs to detect an in-run mutation.

Path-identity refusals (symlink/reparse, hard link, special file, NTFS ADS) are exercised through an
**injected file inspector** — the same seam ``providers.exchange`` uses — so the checks run
deterministically on every OS without needing to plant a real symlink/hard link/ADS on the host.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.control_bundle import (
    MANIFEST_NAME,
    ControlBundleError,
    digest_live_control_inputs,
    freeze_control_bundle,
    load_control_bundle,
)
from wastech_orchestrator.core.flow.snapshot import load_flow
from wastech_orchestrator.core.flow.tools_registry import ToolRegistry
from wastech_orchestrator.providers.artifacts import sha256_file
from wastech_orchestrator.providers.exchange import FileFacts, default_file_inspector

_FLOW_YAML = """\
flow:
  name: sample
  task_type: sample
  permission_ceiling: workspace-write
  output_policy: code_change
  publishing: pull_request
  supervisor:
    role_file: roles/supervisor.md
  nodes:
    - id: planning
      kind: agent
      role_file: sample/planning.md
    - id: build
      kind: agent
      role_file: sample/build.md
    - id: run_tool
      kind: tool
      tool: mytool
"""


def _tool_name() -> str:
    # On POSIX a bare +x script; on Windows a launchable suffix (ToolRegistry has no execute bit).
    return "mytool.bat" if os.name == "nt" else "mytool"


def _make_control_plane(root: Path) -> tuple[Path, Path]:
    """Write a live flow tree + tools dir under ``root``; return ``(flow_dir, tools_dir)``."""
    flow_dir = root / "flows"
    (flow_dir / "sample").mkdir(parents=True)
    (flow_dir / "roles").mkdir(parents=True)
    (flow_dir / "sample.yaml").write_text(_FLOW_YAML, encoding="utf-8")
    (flow_dir / "sample" / "planning.md").write_text("plan {task_path}\n", encoding="utf-8")
    (flow_dir / "sample" / "build.md").write_text("build {plan_path}\n", encoding="utf-8")
    (flow_dir / "roles" / "supervisor.md").write_text("observe\n", encoding="utf-8")
    tools_dir = root / "tools"
    tools_dir.mkdir()
    tool = tools_dir / _tool_name()
    tool.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    tool.chmod(0o755)
    return flow_dir, tools_dir


def _freeze(root: Path, bundle_dir: Path):
    flow_dir, tools_dir = _make_control_plane(root)
    snapshot = load_flow(flow_dir / "sample.yaml")
    tools = ToolRegistry(tools_dir)
    bundle = freeze_control_bundle(bundle_dir, snapshot, flow_dir, tools)
    return snapshot, flow_dir, tools, bundle


def test_freeze_copies_exactly_the_referenced_inputs(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _snapshot, _flow_dir, _tools, bundle = _freeze(tmp_path / "live", bundle_dir)

    assert (bundle.flow_dir / "sample.yaml").is_file()
    assert (bundle.flow_dir / "sample" / "planning.md").is_file()
    assert (bundle.flow_dir / "sample" / "build.md").is_file()
    assert (bundle.flow_dir / "roles" / "supervisor.md").is_file()
    assert (bundle.tools_dir / _tool_name()).is_file()
    assert (bundle_dir / MANIFEST_NAME).is_file()
    assert bundle.flow_source_path == bundle.flow_dir / "sample.yaml"
    assert bundle.bundle_digest  # non-empty stable id
    # The frozen tool keeps its executable bit on POSIX (bytes + mode via copy2).
    if os.name != "nt":
        assert os.access(bundle.tools_dir / _tool_name(), os.X_OK)


def test_freeze_windows_copies_launcher_and_same_name_payload(tmp_path: Path) -> None:
    flow_dir, tools_dir = _make_control_plane(tmp_path / "live")
    for delivered in tools_dir.iterdir():
        delivered.unlink()
    payload = tools_dir / "mytool"
    launcher = tools_dir / "mytool.cmd"
    payload.write_bytes(b"print('payload')\n")
    launcher.write_bytes(b'@python "%~dp0mytool" %*\r\n')
    snapshot = load_flow(flow_dir / "sample.yaml")

    bundle = freeze_control_bundle(
        tmp_path / "bundle",
        snapshot,
        flow_dir,
        ToolRegistry(tools_dir, system="Windows"),
    )

    assert (bundle.tools_dir / payload.name).read_bytes() == payload.read_bytes()
    assert (bundle.tools_dir / launcher.name).read_bytes() == launcher.read_bytes()
    manifest = json.loads((bundle.root / MANIFEST_NAME).read_text(encoding="utf-8"))
    entries = {entry["path"]: entry["sha256"] for entry in manifest["entries"]}
    assert entries["tools/mytool"] == sha256_file(bundle.tools_dir / "mytool")
    assert entries["tools/mytool.cmd"] == sha256_file(bundle.tools_dir / "mytool.cmd")
    # The frozen registry finds the same launcher and its relative `%~dp0` payload is now beside it.
    assert ToolRegistry(bundle.tools_dir, system="Windows").resolve("mytool").name == "mytool.cmd"


def test_freeze_posix_keeps_one_file_per_tool(tmp_path: Path) -> None:
    flow_dir, tools_dir = _make_control_plane(tmp_path / "live")
    for delivered in tools_dir.iterdir():
        delivered.unlink()
    payload = tools_dir / "mytool"
    payload.write_bytes(b"#!/usr/bin/env python3\nprint('payload')\n")
    payload.chmod(0o755)
    (tools_dir / "mytool.cmd").write_bytes(b'@python "%~dp0mytool" %*\r\n')
    snapshot = load_flow(flow_dir / "sample.yaml")

    bundle = freeze_control_bundle(
        tmp_path / "bundle",
        snapshot,
        flow_dir,
        ToolRegistry(tools_dir, system="Linux"),
    )

    assert (bundle.tools_dir / "mytool").read_bytes() == payload.read_bytes()
    assert not (bundle.tools_dir / "mytool.cmd").exists()
    manifest = json.loads((bundle.root / MANIFEST_NAME).read_text(encoding="utf-8"))
    tool_paths = [
        entry["path"] for entry in manifest["entries"] if entry["path"].startswith("tools/")
    ]
    assert tool_paths == ["tools/mytool"]


def test_freeze_digest_is_deterministic(tmp_path: Path) -> None:
    _s1, _f1, _t1, b1 = _freeze(tmp_path / "a", tmp_path / "ba")
    _s2, _f2, _t2, b2 = _freeze(tmp_path / "b", tmp_path / "bb")
    assert b1.bundle_digest == b2.bundle_digest  # identical content → identical digest


def test_load_control_bundle_roundtrips_and_reparses_flow(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _snapshot, _flow_dir, _tools, bundle = _freeze(tmp_path / "live", bundle_dir)
    loaded = load_control_bundle(bundle_dir, bundle.bundle_digest)
    assert loaded.bundle_digest == bundle.bundle_digest
    reparsed = load_flow(loaded.flow_source_path)
    assert reparsed.flow_fingerprint == _snapshot.flow_fingerprint


def test_live_digest_matches_frozen_then_diverges_on_edit(tmp_path: Path) -> None:
    snapshot, flow_dir, tools, bundle = _freeze(tmp_path / "live", tmp_path / "bundle")
    # At freeze the live inputs equal the frozen bytes.
    assert digest_live_control_inputs(snapshot, flow_dir, tools) == bundle.bundle_digest
    # Editing a live role file (a later prompt) changes the live digest — this is what the
    # orchestrator's post-node hook detects.
    (flow_dir / "sample" / "build.md").write_text("build MUTATED\n", encoding="utf-8")
    assert digest_live_control_inputs(snapshot, flow_dir, tools) != bundle.bundle_digest


def test_live_digest_diverges_when_tool_executable_replaced(tmp_path: Path) -> None:
    snapshot, flow_dir, tools, bundle = _freeze(tmp_path / "live", tmp_path / "bundle")
    (tools.tools_dir / _tool_name()).write_text("#!/bin/sh\necho evil\n", encoding="utf-8")
    assert digest_live_control_inputs(snapshot, flow_dir, tools) != bundle.bundle_digest


def test_load_control_bundle_rejects_wrong_expected_digest(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _freeze(tmp_path / "live", bundle_dir)
    with pytest.raises(ControlBundleError):
        load_control_bundle(bundle_dir, "deadbeef")


def test_load_control_bundle_detects_content_drift(tmp_path: Path) -> None:
    bundle_dir = tmp_path / "bundle"
    _snapshot, _flow_dir, _tools, bundle = _freeze(tmp_path / "live", bundle_dir)
    # Tamper with a frozen file without updating the manifest digest.
    (bundle.flow_dir / "sample" / "build.md").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ControlBundleError):
        load_control_bundle(bundle_dir, bundle.bundle_digest)


# -- identity refusals via the injected inspector seam ---------------------------------------------


def _inspector_reporting(target_name: str, facts: FileFacts):
    """A ``FileInspector`` that returns ``facts`` for the entry named ``target_name`` and the real
    facts otherwise, so one referenced input can be made to look like a symlink/hard-link/etc."""
    real = default_file_inspector()

    def inspect(path: Path) -> FileFacts:
        return facts if path.name == target_name else real(path)

    return inspect


@pytest.mark.parametrize(
    "facts",
    [
        FileFacts(True, False, False, 1, (), 1),  # symlink / reparse point
        FileFacts(False, False, True, 2, (), 1),  # hard-linked (link_count != 1)
        FileFacts(False, False, False, 1, (), 1),  # special (non-regular) file
        FileFacts(False, False, True, 1, ("$DATA:x",), 1),  # NTFS alternate data stream
    ],
)
def test_freeze_refuses_non_single_link_regular_source(tmp_path: Path, facts: FileFacts) -> None:
    flow_dir, tools_dir = _make_control_plane(tmp_path / "live")
    snapshot = load_flow(flow_dir / "sample.yaml")
    tools = ToolRegistry(tools_dir)
    inspector = _inspector_reporting("build.md", facts)
    with pytest.raises(ControlBundleError):
        freeze_control_bundle(tmp_path / "bundle", snapshot, flow_dir, tools, inspect=inspector)
