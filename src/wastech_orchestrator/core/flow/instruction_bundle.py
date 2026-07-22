"""Frozen instruction bundle (WRI-011) — a per-task immutable snapshot of the agent inputs.

WRI-010 froze the *control plane* (flow YAML, role prompts, tool executables). This module freezes
the *agent inputs* whose identity must stay stable for the task: the validated **task packet**, the
selected **skill packages**, and the root **repository instruction files** (``AGENTS.md`` /
``AGENTS.override.md`` / ``CLAUDE.md``). Without this, a workspace-write node could edit its own
task file, a selected ``SKILL.md``, or ``AGENTS.md`` mid-task and a *later* evaluator / supervisor /
resumed / fallback call would receive different instructions than were validated at task start — the
agent silently rewriting its own rules.

At task start the orchestrator freezes these inputs into a private, immutable bundle under
``<private_home>/instruction-bundles/<task-id>/`` (a provider deny target — see
:class:`~wastech_orchestrator.runtime_layout.InternalDenyPolicy.frozen_instruction_bundle`), records
one composite ``instruction_manifest_digest``, and publishes **redacted** agent-readable copies to
the exchange. Providers read only the frozen exchange copies (never the live files), native project
instruction discovery is disabled by the adapters, and continue/resume verifies the manifest digest
before reusing a provider session. The live repository files stay ordinary, editable source: a node
may still *propose* a change to ``AGENTS.md`` as a normal diff — it just cannot alter the running
task's frozen inputs.

Unlike the control bundle, a live edit here is **not** a security violation (it is a legitimate
proposed diff), so there is no post-node live-mutation gate — the frozen copy is simply what the
task uses.

Identity/hash/collision primitives are the shared WRI-010/011 helpers in
:mod:`~wastech_orchestrator.core.flow.frozen_bundle`; containment and the chunked digest come from
the ``providers`` interface leaves; the canonical copy is always a fresh regular file
(``shutil.copy2``), never a hard/symlink back to live data. ``core.flow`` may import those leaves
(they never import ``core``), keeping the import-linter contract green.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wastech_orchestrator.core.flow.frozen_bundle import (
    FrozenBundleError,
    digest_entries,
    inspect_frozen_source,
    reject_key_collisions,
)
from wastech_orchestrator.providers.artifacts import assert_contained_path, sha256_file
from wastech_orchestrator.providers.exchange import FileInspector, default_file_inspector

#: Bundle layout (all under ``<private_home>/instruction-bundles/<task-id>/``).
_TASK_SUBDIR = "task"
_SKILLS_SUBDIR = "skills"
_INSTRUCTIONS_SUBDIR = "instructions"
MANIFEST_NAME = "manifest.json"

#: The bundle-relative key of the frozen (canonical) task packet.
TASK_PACKET_KEY = f"{_TASK_SUBDIR}/task.md"

#: The bundle-relative key of the frozen concatenated repository-instruction payload (the exact
#: bytes injected, redacted, through the provider instruction layer).
REPO_INSTRUCTIONS_KEY = f"{_INSTRUCTIONS_SUBDIR}/repository.md"

#: Bump when the on-disk bundle layout / manifest schema changes.
_BUNDLE_FORMAT = 1

#: The root repository instruction files WRI-011 freezes, in fixed injection/precedence order
#: (root-only per the ADR scope decision — no nested discovery, no ``@``-reference closure).
REPO_INSTRUCTION_NAMES: tuple[str, ...] = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md")

#: Skill-package closure caps (fail closed above these — an oversized package is a packaging error,
#: never silently truncated). A skill is bounded documentation + small resources, not a code tree.
MAX_SKILL_FILES = 64
MAX_SKILL_FILE_BYTES = 262_144
MAX_SKILL_TOTAL_BYTES = 2_097_152

#: The synthetic composite-digest entry that folds the WRI-010 control-bundle digest into the
#: instruction manifest digest (In-scope bullet #4) without re-freezing the control plane.
_CONTROL_DIGEST_KEY = "control::bundle_digest"


class InstructionBundleError(FrozenBundleError):
    """Raised when the frozen instruction bundle cannot be built/verified (fail-closed)."""


@dataclass(frozen=True)
class FrozenSkillPackage:
    """One frozen skill package: its node-facing ``SKILL.md`` key plus every closed-over file."""

    name: str
    skill_md_key: str  # bundle-relative POSIX key of the package's SKILL.md
    entries: tuple[tuple[str, str], ...]  # (bundle-key, sha256) for every file in the package


@dataclass(frozen=True)
class LoadedInstructionBundle:
    """A verified handle returned by :func:`load_instruction_bundle` on continue/resume."""

    root: Path
    manifest_digest: str


def instruction_bundle_dir(private_home: Path, task_id: str) -> Path:
    """The private per-task frozen-instruction-bundle dir (a provider deny target, WRI-011)."""
    from wastech_orchestrator.runtime_layout import INSTRUCTION_BUNDLE_DIRNAME

    return private_home / INSTRUCTION_BUNDLE_DIRNAME / task_id


def assert_no_required_secret(text: str, *, extra_secrets: tuple[str, ...], label: str) -> None:
    """Fail closed if a *required* instruction input contains a KNOWN secret value (AC7).

    A task/skill/repository-instruction input must not carry a real secret: if it did, the redacted
    exchange copy the agent reads would replace that secret with ``[REDACTED]`` — silently changing
    what the operator wrote — so we stop before launch instead. "Known" means a value from the
    orchestrator's secret set (resolved env / denied / memory secrets in ``extra_secrets``); this is
    the precise, low-false-positive signal. We deliberately do NOT gate on the generic redaction
    heuristics (assignment/token patterns), which over-match benign content (e.g. a hyphenated
    task-id in front-matter) — those are cosmetically redacted in the exchange copy as before and
    never a semantic instruction change. ``label`` names the input for the error.
    """
    hit = next((s for s in extra_secrets if s and s in text), None)
    if hit is not None:
        raise InstructionBundleError(
            f"required instruction input {label!r} contains a known secret value that cannot be "
            "safely projected to the agent-readable exchange; remove it before running this task"
        )


def freeze_task_packet(
    bundle_dir: Path, task_file: Path, *, inspect: FileInspector | None = None
) -> tuple[Path, tuple[str, str]]:
    """Freeze the validated task packet into the bundle; return (canonical path, (key, digest)).

    The canonical (unredacted) copy is stored privately for audit; the orchestrator publishes the
    redacted copy to the exchange as ``{task_path}`` from *this* frozen file, never from live.
    """
    inspector = inspect or default_file_inspector()
    dest = assert_contained_path(bundle_dir, bundle_dir / TASK_PACKET_KEY)
    inspect_frozen_source(
        task_file, inspector, label="task packet", error_cls=InstructionBundleError
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(task_file, dest)
    return dest, (TASK_PACKET_KEY, sha256_file(dest))


def discover_repository_instructions(repo_root: Path, tracked_files: frozenset[str]) -> list[Path]:
    """Root-only repository instructions: the known root files that exist *and* are tracked.

    Returns absolute paths in fixed :data:`REPO_INSTRUCTION_NAMES` order (reproducible for the
    digest). An untracked file on disk is not a repository instruction (it is not part of the
    committed policy), so it is skipped rather than frozen.
    """
    return [
        repo_root / name
        for name in REPO_INSTRUCTION_NAMES
        if name in tracked_files and (repo_root / name).is_file()
    ]


def freeze_repository_instructions(
    bundle_dir: Path, files: list[Path], *, inspect: FileInspector | None = None
) -> tuple[list[tuple[str, str]], Path | None]:
    """Freeze the root repository-instruction files + a concatenated canonical payload.

    Each source is inspected no-follow, copied under ``instructions/src/`` for audit and digested;
    a single delimited concatenation is written to :data:`REPO_INSTRUCTIONS_KEY` — the exact bytes
    the adapters inject (after redaction). Returns ``([], None)`` when the repo defines no tracked
    root instruction files. The concatenation is also a manifest entry, so editing any source file
    changes the composite digest (AC3).
    """
    if not files:
        return [], None
    inspector = inspect or default_file_inspector()
    entries: list[tuple[str, str]] = []
    sections: list[str] = [
        "# Repository instructions (frozen at task start — immutable for this task)\n"
    ]
    for source in files:
        name = source.name
        src_key = f"{_INSTRUCTIONS_SUBDIR}/src/{name}"
        dest = assert_contained_path(bundle_dir, bundle_dir / src_key)
        inspect_frozen_source(
            source, inspector, label="repository instruction", error_cls=InstructionBundleError
        )
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        entries.append((src_key, sha256_file(dest)))
        body = dest.read_text(encoding="utf-8", errors="replace")
        sections.append(f"<!-- BEGIN {name} -->\n{body}\n<!-- END {name} -->\n")
    concat_path = assert_contained_path(bundle_dir, bundle_dir / REPO_INSTRUCTIONS_KEY)
    concat_path.parent.mkdir(parents=True, exist_ok=True)
    concat_path.write_text("\n".join(sections), encoding="utf-8", newline="")
    entries.append((REPO_INSTRUCTIONS_KEY, sha256_file(concat_path)))
    return entries, concat_path


def freeze_skill_package(
    bundle_dir: Path,
    skill_name: str,
    skill_md_rel: str,
    package_files_rel: list[str],
    repo_root: Path,
    *,
    inspect: FileInspector | None = None,
) -> FrozenSkillPackage:
    """Freeze one selected skill's *package closure* into ``skills/<name>/...`` (WRI-011).

    ``skill_md_rel`` is the repo-relative POSIX path of the skill's ``SKILL.md``; its parent is the
    package directory. ``package_files_rel`` is the tracked-file closure of that directory. Only
    tracked regular files inside the package are copied, preserving relative layout; links/reparse
    points/hard links/special files/ADS are refused (via the no-follow gate) and case-fold/NFC key
    collisions are rejected. A root-level package (``SKILL.md`` at the repo root, whose closure is
    the whole tree) or an over-cap package fails strict resolution rather than reading live docs.
    """
    inspector = inspect or default_file_inspector()
    package_dir = PurePosixPath(skill_md_rel).parent
    if str(package_dir) in ("", "."):
        raise InstructionBundleError(
            f"skill {skill_name!r} SKILL.md is at the repository root; its resource closure would "
            "be the whole tree and cannot be represented as a bounded package (unsupported under "
            "strict isolation)"
        )
    # Keep only the tracked files that actually live inside the package directory.
    closure = sorted(
        rel for rel in package_files_rel if PurePosixPath(rel).is_relative_to(package_dir)
    )
    if skill_md_rel not in closure:
        raise InstructionBundleError(
            f"skill {skill_name!r} SKILL.md {skill_md_rel!r} is not a tracked file in its package"
        )
    if len(closure) > MAX_SKILL_FILES:
        raise InstructionBundleError(
            f"skill {skill_name!r} package has {len(closure)} tracked files (cap {MAX_SKILL_FILES})"
        )
    entries: list[tuple[str, str]] = []
    keys: list[str] = []
    skill_md_key = ""
    total_bytes = 0
    for rel in closure:
        rel_within = PurePosixPath(rel).relative_to(package_dir).as_posix()
        key = f"{_SKILLS_SUBDIR}/{skill_name}/{rel_within}"
        keys.append(key)
        source = repo_root / rel
        inspect_frozen_source(
            source, inspector, label="skill file", error_cls=InstructionBundleError
        )
        size = source.stat().st_size
        if size > MAX_SKILL_FILE_BYTES:
            raise InstructionBundleError(
                f"skill {skill_name!r} file {rel!r} is {size} bytes (cap {MAX_SKILL_FILE_BYTES})"
            )
        total_bytes += size
        if total_bytes > MAX_SKILL_TOTAL_BYTES:
            raise InstructionBundleError(
                f"skill {skill_name!r} package exceeds {MAX_SKILL_TOTAL_BYTES} total bytes"
            )
        dest = assert_contained_path(bundle_dir, bundle_dir / key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        entries.append((key, sha256_file(dest)))
        if rel == skill_md_rel:
            skill_md_key = key
    reject_key_collisions(keys, label="skill file", error_cls=InstructionBundleError)
    return FrozenSkillPackage(name=skill_name, skill_md_key=skill_md_key, entries=tuple(entries))


def write_instruction_manifest(
    bundle_dir: Path,
    *,
    entries: list[tuple[str, str]],
    control_digest: str | None,
    metadata: dict[str, str] | None = None,
) -> str:
    """Write ``manifest.json`` and return the composite ``instruction_manifest_digest``.

    ``entries`` are every frozen ``(bundle-key, sha256)`` pair (task packet, repository
    instructions, and skill packages). The WRI-010 ``control_digest`` is folded in as a synthetic
    entry so one digest binds all of the task's frozen context (In-scope bullet #4). The manifest is
    an audit record — it never contains config/env contents.
    """
    all_entries = list(entries)
    if control_digest is not None:
        all_entries.append((_CONTROL_DIGEST_KEY, control_digest))
    reject_key_collisions([key for key, _ in all_entries], label="instruction-input")
    digest = digest_entries(all_entries)
    manifest = {
        "format": _BUNDLE_FORMAT,
        "manifest_digest": digest,
        "control_bundle_digest": control_digest,
        "entries": [
            {"path": key, "sha256": file_digest} for key, file_digest in sorted(all_entries)
        ],
        "metadata": dict(metadata or {}),
    }
    (bundle_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return digest


def _read_manifest(bundle_dir: Path) -> dict[str, object]:
    """Read and shape-check ``manifest.json`` (fail-closed :class:`InstructionBundleError`)."""
    path = bundle_dir / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstructionBundleError(
            f"cannot read instruction-bundle manifest {path.as_posix()}: {exc}"
        ) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise InstructionBundleError(f"malformed instruction-bundle manifest {path.as_posix()}")
    if raw.get("format") != _BUNDLE_FORMAT:
        got = raw.get("format")
        raise InstructionBundleError(
            f"unsupported instruction-bundle format {got!r} (expected {_BUNDLE_FORMAT})"
        )
    return raw


def load_instruction_bundle(
    bundle_dir: Path, expected_digest: str, *, inspect: FileInspector | None = None
) -> LoadedInstructionBundle:
    """Verify a frozen instruction bundle against ``expected_digest`` (continue/resume).

    Re-hashes every manifest entry file with the same no-follow identity checks and recomputes the
    composite digest (including the synthetic control-digest entry). A missing/altered file, a
    planted symlink, or a digest mismatch is fail-closed — the caller routes it to
    ``manual_action_required`` and refuses to resume the provider session (AC9). ``expected_digest``
    is the parent-held (state-store) value, so a provider that rewrote both a file and the manifest
    is still caught.
    """
    inspector = inspect or default_file_inspector()
    manifest = _read_manifest(bundle_dir)
    recorded = manifest.get("manifest_digest")
    if recorded != expected_digest:
        raise InstructionBundleError(
            f"instruction-bundle manifest digest {recorded!r} != expected {expected_digest!r}"
        )
    control_digest = manifest.get("control_bundle_digest")
    manifest_entries = manifest["entries"]
    assert isinstance(manifest_entries, list)  # validated by _read_manifest
    entries: list[tuple[str, str]] = []
    for entry in manifest_entries:
        if not isinstance(entry, dict) or "path" not in entry:
            raise InstructionBundleError("malformed instruction-bundle manifest entry")
        key = str(entry["path"])
        if key == _CONTROL_DIGEST_KEY:
            # Synthetic entry — its "digest" is the control-bundle digest, not a file under the
            # bundle. Fold it back in from the recorded control digest.
            entries.append((key, str(control_digest)))
            continue
        path = assert_contained_path(bundle_dir, bundle_dir / key)
        inspect_frozen_source(
            path, inspector, label="frozen instruction input", error_cls=InstructionBundleError
        )
        entries.append((key, sha256_file(path)))
    if digest_entries(entries) != expected_digest:
        raise InstructionBundleError(
            f"frozen instruction bundle content drifted from its recorded digest "
            f"({bundle_dir.as_posix()})"
        )
    return LoadedInstructionBundle(root=bundle_dir, manifest_digest=expected_digest)
