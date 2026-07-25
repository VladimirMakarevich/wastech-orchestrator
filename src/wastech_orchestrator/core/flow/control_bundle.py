"""Frozen control bundle (WRI-010) — a per-task immutable snapshot of the effective control plane.

The operator control plane (``<repo>/.worc`` = ``control_home``: the flow YAML, role/supervisor
prompts, and ``tools/`` executables) lives under the provider working directory. Every consumer
historically re-read it **live** on each call, so a workspace-write agent could rewrite a later role
prompt or a tool executable mid-run and a *later* orchestrator node would then read/execute those
provider-chosen bytes outside the provider sandbox with the orchestrator's own authority.

At task start the orchestrator freezes the exact effective control inputs referenced by the task's
flow into a private, immutable bundle under ``<private_home>/control-bundles/<task-id>/`` and binds
all later flow/supervisor/tool consumers to it. The bundle is never written to the exchange and is a
provider deny target (:class:`~wastech_orchestrator.runtime_layout.InternalDenyPolicy`).

Three operations:

* :func:`freeze_control_bundle` — copy the referenced flow YAML, role files, and tool executables
  into the bundle (regular single-link files only), record a manifest, and return the bound handle.
* :func:`verify_bundle_integrity` — re-hash the frozen bundle against its recorded digest (used on
  ``continue``/resume, which must reuse the original frozen bytes, never re-freeze).
* :func:`digest_live_control_inputs` — re-hash the **live** control inputs with the same identity
  checks; the orchestrator compares this to the frozen baseline after every provider attempt (once
  WRI-012 has proven the provider tree quiescent). Any drift is a non-fallback security violation.

Identity is enforced by reusing the WRI-001 no-follow inspector
(:func:`~wastech_orchestrator.providers.exchange.default_file_inspector`), the shared containment
belt (:func:`~wastech_orchestrator.providers.artifacts.assert_contained_path`), and the chunked
digest (:func:`~wastech_orchestrator.providers.artifacts.sha256_file`) — no new identity code. A
source that is a symlink/reparse point, a hard link, a special file, or carries an NTFS alternate
data stream is refused; the frozen copy is always a fresh regular file (``shutil.copy2`` copies
bytes, so it can never be a hard link back to, or a symlink onto, mutable live control data).

``core.flow`` may import the ``providers`` interface leaves (``exchange``/``artifacts``) — they
never import ``core`` — so this module keeps the import-linter contract green.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from wastech_orchestrator.core.flow.frozen_bundle import (
    FrozenBundleError,
    digest_entries,
    inspect_frozen_source,
    reject_key_collisions,
)
from wastech_orchestrator.core.flow.snapshot import FlowSnapshot
from wastech_orchestrator.core.flow.tools_registry import ToolRegistry, ToolResolutionError
from wastech_orchestrator.providers.artifacts import assert_contained_path, sha256_file
from wastech_orchestrator.providers.exchange import FileInspector, default_file_inspector

#: Bundle layout: the flow YAML + role files live under ``flows/`` (mirroring the live ``flow_dir``
#: so a relative ``role_file`` resolves identically), tool executables under ``tools/``, and the
#: audit/reproduce record in ``manifest.json``.
_FLOWS_SUBDIR = "flows"
_TOOLS_SUBDIR = "tools"
MANIFEST_NAME = "manifest.json"

#: Bump when the on-disk bundle layout / manifest schema changes (an old bundle then fails to
#: verify and the task is treated as needing a fresh/restart).
_BUNDLE_FORMAT = 1


class ControlBundleError(FrozenBundleError):
    """Raised when a control bundle cannot be frozen, verified, or re-hashed (fail-closed)."""


@dataclass(frozen=True)
class FrozenControlBundle:
    """A bound handle to one task's frozen control bundle.

    The orchestrator points ``NodeInputs.flow_dir``, the supervisor, and a per-task
    :class:`~wastech_orchestrator.core.flow.tools_registry.ToolRegistry` at these frozen paths so no
    later consumer reopens live ``.worc``. ``bundle_digest`` is the stable identity persisted on the
    run state; ``continue``/resume verifies the on-disk bundle against it.
    """

    root: Path
    flow_dir: Path
    flow_source_path: Path
    tools_dir: Path
    bundle_digest: str


@dataclass(frozen=True)
class _Ref:
    """One referenced control input: its bundle-relative key and its live source path."""

    key: str  # POSIX bundle-relative key, e.g. "flows/implementation/plan.md" or "tools/check.cmd"
    source: Path


def _referenced_inputs(snapshot: FlowSnapshot, flow_dir: Path, tools: ToolRegistry) -> list[_Ref]:
    """Enumerate the exact control inputs the flow references, in a deterministic order.

    The flow YAML, every node ``role_file``, the supervisor block's three role files, and the
    executable each ``tool`` node resolves to (via the **live** registry — a resolution failure is
    a mutation/misconfig and fails closed). Role files are keyed by their flow-relative path so the
    frozen copy resolves identically under the bundle ``flows/`` dir; tools by their resolved
    filename so the per-task registry finds them with the same candidate logic.
    """
    if snapshot.source_path is None:
        raise ControlBundleError("flow snapshot has no source path; cannot freeze control plane")
    # The flow YAML is resolved under ``flow_dir`` (like the role files), never from
    # ``snapshot.source_path`` directly: on the live-digest path the snapshot is the *frozen* one,
    # so its source_path points into the bundle — ``flow_dir`` maps it back onto the live file.
    yaml_name = snapshot.source_path.name
    refs: list[_Ref] = [_Ref(f"{_FLOWS_SUBDIR}/{yaml_name}", flow_dir / yaml_name)]
    seen_roles: set[str] = set()

    def _add_role(role_file: str | None) -> None:
        if role_file and role_file not in seen_roles:
            seen_roles.add(role_file)
            refs.append(_Ref(f"{_FLOWS_SUBDIR}/{role_file}", flow_dir / role_file))

    for node in snapshot.doc.nodes:
        _add_role(getattr(node, "role_file", None))
    supervisor = snapshot.doc.supervisor
    if supervisor is not None:
        _add_role(supervisor.role_file)
        _add_role(supervisor.finalize_role_file)
        _add_role(supervisor.handoff_role_file)

    tool_names = sorted({node.tool for node in snapshot.doc.nodes if node.kind == "tool"})
    for name in tool_names:
        try:
            resolved = tools.resolve(name)
        except ToolResolutionError as exc:
            raise ControlBundleError(f"tool {name!r} no longer resolves: {exc}") from exc
        refs.append(_Ref(f"{_TOOLS_SUBDIR}/{resolved.name}", resolved))
    return refs


def _inspect_source(path: Path, inspector: FileInspector) -> None:
    """No-follow identity gate for a control input (shared :func:`inspect_frozen_source`)."""
    inspect_frozen_source(path, inspector, label="control input", error_cls=ControlBundleError)


def freeze_control_bundle(
    bundle_dir: Path,
    snapshot: FlowSnapshot,
    flow_dir: Path,
    tools: ToolRegistry,
    *,
    inspect: FileInspector | None = None,
    metadata: dict[str, str] | None = None,
) -> FrozenControlBundle:
    """Freeze the flow's referenced control inputs into ``bundle_dir`` and return the bound handle.

    ``bundle_dir`` must be a fresh, empty per-task directory under ``private_home``. Each referenced
    source is inspected no-follow (:func:`_inspect_source`), containment-checked, and copied with
    ``shutil.copy2`` (bytes + mode, so the frozen executable keeps its ``+x`` bit and is never a
    hard link/symlink to live data). The manifest records per-file digests plus reproduce metadata
    (flow fingerprint, format version, caller ``metadata``) — never config/env contents.
    """
    inspector = inspect or default_file_inspector()
    refs = _referenced_inputs(snapshot, flow_dir, tools)
    reject_key_collisions(
        [ref.key for ref in refs], label="control-input", error_cls=ControlBundleError
    )
    assert snapshot.source_path is not None  # guaranteed by _referenced_inputs

    entries: list[tuple[str, str]] = []
    for ref in refs:
        _inspect_source(ref.source, inspector)
        dest = assert_contained_path(bundle_dir, bundle_dir / ref.key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ref.source, dest)
        entries.append((ref.key, sha256_file(dest)))

    digest = digest_entries(entries)
    flow_source = f"{_FLOWS_SUBDIR}/{snapshot.source_path.name}"
    manifest = {
        "format": _BUNDLE_FORMAT,
        "flow_fingerprint": snapshot.flow_fingerprint,
        "flow_source": flow_source,
        "bundle_digest": digest,
        "entries": [{"path": key, "sha256": file_digest} for key, file_digest in sorted(entries)],
        "metadata": dict(metadata or {}),
    }
    (bundle_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return FrozenControlBundle(
        root=bundle_dir,
        flow_dir=bundle_dir / _FLOWS_SUBDIR,
        flow_source_path=bundle_dir / _FLOWS_SUBDIR / snapshot.source_path.name,
        tools_dir=bundle_dir / _TOOLS_SUBDIR,
        bundle_digest=digest,
    )


def _read_manifest(bundle_dir: Path) -> dict[str, object]:
    """Read and shape-check ``manifest.json``; raise :class:`ControlBundleError` on any problem."""
    path = bundle_dir / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControlBundleError(
            f"cannot read control-bundle manifest {path.as_posix()}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ControlBundleError(f"malformed control-bundle manifest {path.as_posix()}")
    if raw.get("format") != _BUNDLE_FORMAT:
        raise ControlBundleError(
            f"unsupported control-bundle format {raw.get('format')!r} (expected {_BUNDLE_FORMAT})"
        )
    return raw


def load_control_bundle(
    bundle_dir: Path, expected_digest: str, *, inspect: FileInspector | None = None
) -> FrozenControlBundle:
    """Verify a frozen bundle against ``expected_digest`` and return its bound handle.

    Used on ``continue``/resume, which reuses the original frozen bytes (never re-freezes). It
    re-hashes every manifest entry with the same no-follow identity checks; a missing/altered file,
    a planted symlink, a digest mismatch, or a manifest whose recorded digest disagrees with the
    parent-held ``expected_digest`` is a fail-closed condition — the caller routes it to
    ``manual_action_required``. ``expected_digest`` is the parent-held (state-store) value, so a
    provider that rewrote both a file *and* the manifest is still caught.
    """
    inspector = inspect or default_file_inspector()
    manifest = _read_manifest(bundle_dir)
    recorded = manifest.get("bundle_digest")
    if recorded != expected_digest:
        raise ControlBundleError(
            f"control-bundle manifest digest {recorded!r} != expected {expected_digest!r}"
        )
    flow_source = manifest.get("flow_source")
    if not isinstance(flow_source, str):
        raise ControlBundleError("control-bundle manifest is missing its flow_source")
    manifest_entries = manifest["entries"]
    assert isinstance(manifest_entries, list)  # validated by _read_manifest
    entries: list[tuple[str, str]] = []
    for entry in manifest_entries:
        if not isinstance(entry, dict) or "path" not in entry:
            raise ControlBundleError("malformed control-bundle manifest entry")
        key = str(entry["path"])
        path = assert_contained_path(bundle_dir, bundle_dir / key)
        _inspect_source(path, inspector)
        entries.append((key, sha256_file(path)))
    if digest_entries(entries) != expected_digest:
        raise ControlBundleError(
            f"frozen control bundle content drifted from its recorded digest "
            f"({bundle_dir.as_posix()})"
        )
    return FrozenControlBundle(
        root=bundle_dir,
        flow_dir=bundle_dir / _FLOWS_SUBDIR,
        flow_source_path=assert_contained_path(bundle_dir, bundle_dir / flow_source),
        tools_dir=bundle_dir / _TOOLS_SUBDIR,
        bundle_digest=expected_digest,
    )


def digest_live_control_inputs(
    snapshot: FlowSnapshot,
    flow_dir: Path,
    tools: ToolRegistry,
    *,
    inspect: FileInspector | None = None,
) -> str:
    """Re-hash the **live** control inputs the frozen bundle was built from, same order/keys.

    The orchestrator captures this at freeze time (it equals ``bundle_digest``) and recomputes it
    after every provider attempt once WRI-012 has proven the provider tree quiescent. A mismatch —
    or a planted symlink/hard-link surfaced by :func:`_inspect_source` — means a live control file
    changed under the running task: a non-fallback security violation.
    """
    inspector = inspect or default_file_inspector()
    refs = _referenced_inputs(snapshot, flow_dir, tools)
    entries = [(ref.key, sha256_file(_checked(ref.source, inspector))) for ref in refs]
    return digest_entries(entries)


def _checked(path: Path, inspector: FileInspector) -> Path:
    """Inspect ``path`` no-follow and return it (so the live-digest reads as one expression)."""
    _inspect_source(path, inspector)
    return path
