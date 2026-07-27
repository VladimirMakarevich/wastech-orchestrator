"""Unit tests for the frozen instruction bundle (task / skills / repository instructions).

Covers the freeze/manifest/verify primitives and their fail-closed identity, cap, collision, and
secret gates directly (the orchestrator-level wiring is covered separately in
tests/core/test_orchestrator.py and tests/providers/). The identity refusals reuse the exchange's
no-follow inspector seam via an injected ``FileInspector`` (same pattern as test_control_bundle).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from wastech_orchestrator.core.flow.instruction_bundle import (
    TASK_PACKET_KEY,
    InstructionBundleError,
    assert_no_required_secret,
    discover_repository_instructions,
    freeze_repository_instructions,
    freeze_skill_package,
    freeze_task_packet,
    governance_changed_paths,
    load_instruction_bundle,
    write_instruction_manifest,
)
from wastech_orchestrator.providers.exchange import FileFacts, default_file_inspector


def _inspector_reporting(target_name: str, facts: FileFacts):
    """A ``FileInspector`` that reports ``facts`` for the entry named ``target_name``, real else."""
    real = default_file_inspector()

    def inspect(path: Path) -> FileFacts:
        return facts if path.name == target_name else real(path)

    return inspect


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- task packet ----------------------------------------------------------------------------------


def test_freeze_task_packet_copies_and_digests(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    task = _write(tmp_path / "tasks/pending/t.md", "# do the thing\n")
    canonical, (key, digest) = freeze_task_packet(bundle, task)
    assert key == TASK_PACKET_KEY
    assert canonical.read_text() == "# do the thing\n"
    assert canonical == bundle / TASK_PACKET_KEY
    assert len(digest) == 64  # sha256 hex


# -- repository instructions ----------------------------------------------------------------------


def test_discover_repository_instructions_root_only_and_tracked(tmp_path: Path) -> None:
    _write(tmp_path / "AGENTS.md", "a")
    _write(tmp_path / "CLAUDE.md", "c")
    _write(tmp_path / "docs/nested/AGENTS.md", "nested")  # nested — never discovered
    # CLAUDE.md exists on disk but is NOT tracked → excluded; AGENTS.md is tracked → included.
    found = discover_repository_instructions(tmp_path, frozenset({"AGENTS.md"}))
    assert found == [tmp_path / "AGENTS.md"]


# -- governance-path detection --------------------------------------------------------------------


def test_governance_changed_paths_matches_root_instruction_files() -> None:
    changed = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "src/app.py")
    assert governance_changed_paths(changed) == ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md")


def test_governance_changed_paths_instruction_names_are_root_only() -> None:
    # The instruction names match only at the repo root — a nested same-named file is ordinary code.
    assert governance_changed_paths(("docs/AGENTS.md", "sub/CLAUDE.md")) == ()


def test_governance_changed_paths_matches_agents_rules_tree_including_nested() -> None:
    changed = (".agents/rules/security.md", ".agents/rules/a/b.md", ".agentsX/rules/x.md")
    assert governance_changed_paths(changed) == (
        ".agents/rules/a/b.md",
        ".agents/rules/security.md",
    )


def test_governance_changed_paths_ignores_ordinary_paths_empty_and_sorts() -> None:
    assert governance_changed_paths(()) == ()
    assert governance_changed_paths(("src/x.py", "README.md")) == ()
    # deduped + sorted regardless of input order
    assert governance_changed_paths(("CLAUDE.md", "AGENTS.md", "CLAUDE.md")) == (
        "AGENTS.md",
        "CLAUDE.md",
    )


def test_freeze_repository_instructions_copies_and_digests_each_source(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    agents = _write(tmp_path / "AGENTS.md", "AGENTS body\n")
    claude = _write(tmp_path / "CLAUDE.md", "CLAUDE body\n")
    entries = freeze_repository_instructions(bundle, [agents, claude])
    # Each source is frozen under instructions/src/<name> for the manifest digest; NO
    # concatenated payload is produced (the agent reads the live root files itself).
    keys = {k for k, _ in entries}
    assert keys == {"instructions/src/AGENTS.md", "instructions/src/CLAUDE.md"}
    assert (bundle / "instructions/src/AGENTS.md").read_text() == "AGENTS body\n"
    assert (bundle / "instructions/src/CLAUDE.md").read_text() == "CLAUDE body\n"
    assert not (bundle / "instructions/repository.md").exists()  # no injected concat payload
    assert all(len(d) == 64 for _, d in entries)  # sha256 hex


def test_freeze_repository_instructions_empty_when_none(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    assert freeze_repository_instructions(bundle, []) == []


# -- skill packages -------------------------------------------------------------------------------


def test_freeze_skill_package_copies_closure_preserving_layout(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(tmp_path / ".claude/skills/safe/SKILL.md", "skill body")
    _write(tmp_path / ".claude/skills/safe/refs/help.md", "resource")
    package_files = [".claude/skills/safe/SKILL.md", ".claude/skills/safe/refs/help.md"]
    pkg = freeze_skill_package(bundle, "safe", package_files[0], package_files, tmp_path)
    assert pkg.skill_md_key == "skills/safe/SKILL.md"
    assert (bundle / "skills/safe/SKILL.md").read_text() == "skill body"
    assert (bundle / "skills/safe/refs/help.md").read_text() == "resource"  # layout preserved
    assert {k for k, _ in pkg.entries} == {"skills/safe/SKILL.md", "skills/safe/refs/help.md"}


def test_freeze_skill_package_rejects_root_level(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(tmp_path / "SKILL.md", "root skill")
    with pytest.raises(InstructionBundleError, match="repository root"):
        freeze_skill_package(bundle, "root", "SKILL.md", ["SKILL.md"], tmp_path)


def test_freeze_skill_package_rejects_missing_skill_md(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(tmp_path / "pkg/skill/other.md", "x")
    with pytest.raises(InstructionBundleError, match="not a tracked file"):
        freeze_skill_package(
            bundle, "skill", "pkg/skill/SKILL.md", ["pkg/skill/other.md"], tmp_path
        )


def test_freeze_skill_package_enforces_file_count_cap(tmp_path: Path, monkeypatch) -> None:
    import wastech_orchestrator.core.flow.instruction_bundle as ib

    monkeypatch.setattr(ib, "MAX_SKILL_FILES", 1)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(tmp_path / "pkg/s/SKILL.md", "s")
    _write(tmp_path / "pkg/s/extra.md", "e")
    with pytest.raises(InstructionBundleError, match="cap 1"):
        freeze_skill_package(
            bundle, "s", "pkg/s/SKILL.md", ["pkg/s/SKILL.md", "pkg/s/extra.md"], tmp_path
        )


def test_freeze_skill_package_enforces_file_byte_cap(tmp_path: Path, monkeypatch) -> None:
    import wastech_orchestrator.core.flow.instruction_bundle as ib

    monkeypatch.setattr(ib, "MAX_SKILL_FILE_BYTES", 4)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(tmp_path / "pkg/s/SKILL.md", "way too many bytes")
    with pytest.raises(InstructionBundleError, match="bytes"):
        freeze_skill_package(bundle, "s", "pkg/s/SKILL.md", ["pkg/s/SKILL.md"], tmp_path)


@pytest.mark.parametrize(
    "facts",
    [
        FileFacts(True, False, False, 1, (), 1),  # symlink / reparse point
        FileFacts(False, False, True, 2, (), 1),  # hard-linked
        FileFacts(False, False, False, 1, (), 1),  # special (non-regular)
        FileFacts(False, False, True, 1, ("$DATA:x",), 1),  # NTFS alternate data stream
    ],
)
def test_freeze_skill_package_refuses_bad_identity(tmp_path: Path, facts: FileFacts) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    _write(tmp_path / "pkg/s/SKILL.md", "s")
    with pytest.raises(InstructionBundleError):
        freeze_skill_package(
            bundle,
            "s",
            "pkg/s/SKILL.md",
            ["pkg/s/SKILL.md"],
            tmp_path,
            inspect=_inspector_reporting("SKILL.md", facts),
        )


# -- manifest / digest / verify -------------------------------------------------------------------


def _frozen_bundle(tmp_path: Path, *, control: str | None = "ctrl-digest") -> tuple[Path, str]:
    bundle = tmp_path / "bundle"
    bundle.mkdir(parents=True)
    task = _write(tmp_path / "t.md", "task body\n")
    _, task_entry = freeze_task_packet(bundle, task)
    agents = _write(tmp_path / "AGENTS.md", "agents\n")
    repo_entries = freeze_repository_instructions(bundle, [agents])
    digest = write_instruction_manifest(
        bundle, entries=[task_entry, *repo_entries], control_digest=control
    )
    return bundle, digest


def test_manifest_roundtrip_verifies(tmp_path: Path) -> None:
    bundle, digest = _frozen_bundle(tmp_path)
    loaded = load_instruction_bundle(bundle, digest)
    assert loaded.manifest_digest == digest


def test_load_returns_file_entries_for_resume(tmp_path: Path) -> None:
    # A resumed run repopulates ``instruction_entries`` from the verified manifest so the
    # lifecycle-vs-packet audit check runs on resume. ``load`` must therefore surface the
    # real (key, sha256) file entries — including the task packet — and exclude the synthetic
    # control-digest entry (which is not a file under the bundle).
    import hashlib

    bundle, digest = _frozen_bundle(tmp_path)
    loaded = load_instruction_bundle(bundle, digest)
    keys = {key for key, _ in loaded.entries}
    assert TASK_PACKET_KEY in keys and "instructions/src/AGENTS.md" in keys
    assert not any(key.startswith("control::") for key, _ in loaded.entries)
    # The task-packet digest recovered from the manifest is the sha256 of the frozen packet file.
    packet_digest = next(d for key, d in loaded.entries if key == TASK_PACKET_KEY)
    assert packet_digest == hashlib.sha256((bundle / TASK_PACKET_KEY).read_bytes()).hexdigest()


def test_manifest_verify_rejects_wrong_parent_digest(tmp_path: Path) -> None:
    bundle, _ = _frozen_bundle(tmp_path)
    with pytest.raises(InstructionBundleError, match="!= expected"):
        load_instruction_bundle(bundle, "0" * 64)


def test_manifest_verify_detects_content_drift(tmp_path: Path) -> None:
    bundle, digest = _frozen_bundle(tmp_path)
    # A provider that rewrote a frozen file AND the manifest is still caught: the parent-held digest
    # no longer matches the recomputed content.
    (bundle / TASK_PACKET_KEY).write_text("tampered\n", encoding="utf-8")
    with pytest.raises(InstructionBundleError):
        load_instruction_bundle(bundle, digest)


def test_control_digest_is_folded_into_composite(tmp_path: Path) -> None:
    # Two otherwise-identical bundles with different control digests get different composite digests
    # (In-scope bullet #4: one digest binds task + skills + repo instructions + control plane).
    b1, d1 = _frozen_bundle(tmp_path / "a", control="ctrl-A")
    b2, d2 = _frozen_bundle(tmp_path / "b", control="ctrl-B")
    assert d1 != d2


# -- secret gate ----------------------------------------------------------------------------------


def test_assert_no_required_secret_passes_clean_text() -> None:
    assert_no_required_secret("just prose, no secrets", extra_secrets=(), label="task packet")


def test_assert_no_required_secret_fails_on_extra_secret() -> None:
    with pytest.raises(InstructionBundleError, match="known secret value"):
        assert_no_required_secret(
            "the token is SUPERSECRETVALUE123 here",
            extra_secrets=("SUPERSECRETVALUE123",),
            label="repository instructions",
        )
