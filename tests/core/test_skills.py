"""Repo skill inventory scan, planning-selection acceptance, and dedup (post-test-run)."""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.skills import (
    SkillInventory,
    SkillInventoryScanner,
    SkillRef,
    compute_skill_dedup,
    resolve_planning_skills,
)


def _write_skill(root: Path, name: str, *, description: str = "d", body: str = "") -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    path = skill_dir / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}", encoding="utf-8"
    )
    return path


# --- inventory scan ----------------------------------------------------------------------------


def test_scan_reads_frontmatter_only(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "skills"
    _write_skill(root, "safe-change", description="Review before finishing", body="# Body\nlong")
    _write_skill(root, "add-provider", description="Add an adapter")
    inv = SkillInventoryScanner(root, excluded_names=()).collect()
    names = {s.name: s for s in inv.skills}
    assert set(names) == {"safe-change", "add-provider"}
    assert names["safe-change"].description == "Review before finishing"
    assert names["safe-change"].path.endswith("safe-change/SKILL.md")


def test_scan_skips_malformed_or_frontmatterless(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    (root / "no-frontmatter").mkdir()
    (root / "no-frontmatter" / "SKILL.md").write_text("# Just a heading\n", encoding="utf-8")
    (root / "no-name").mkdir()
    (root / "no-name" / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
    _write_skill(root, "good")
    inv = SkillInventoryScanner(root, excluded_names=()).collect()
    assert [s.name for s in inv.skills] == ["good"]


def test_scan_missing_root_is_empty(tmp_path: Path) -> None:
    inv = SkillInventoryScanner(tmp_path / "absent").collect()
    assert inv.skills == ()


def test_scan_honors_denied_read_paths(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "secret-skill")
    _write_skill(root, "ok-skill")
    inv = SkillInventoryScanner(
        root, excluded_names=(), denied_read_paths=("secret-skill/SKILL.md",)
    ).collect()
    assert [s.name for s in inv.skills] == ["ok-skill"]


def test_relevant_excludes_denylisted_gate_skills(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    for name in ("safe-change", "run-checks", "test", "sync-docs"):
        _write_skill(root, name)
    inv = SkillInventoryScanner(root).collect()  # default exclude = run-checks/test/sync-docs
    assert {s.name for s in inv.skills} == {"safe-change", "run-checks", "test", "sync-docs"}
    assert {s.name for s in inv.relevant()} == {"safe-change"}
    assert inv.by_name("run-checks") is None  # excluded → not selectable
    assert inv.by_name("safe-change") is not None


# --- deterministic planning selection ----------------------------------------------------------


def _inv(*names: str, excluded: tuple[str, ...] = ()) -> SkillInventory:
    skills = tuple(SkillRef(name=n, description="", path=f"/skills/{n}/SKILL.md") for n in names)
    return SkillInventory(skills=skills, excluded=frozenset(excluded))


def test_resolve_keeps_known_drops_unknown_and_excluded() -> None:
    inv = _inv("safe-change", "self-review", "run-checks", excluded=("run-checks",))
    sel = resolve_planning_skills(["self-review", "run-checks", "ghost"], inv)
    assert [r.name for r in sel.refs] == ["self-review"]
    assert sel.dropped_excluded == ("run-checks",)
    assert sel.dropped_unknown == ("ghost",)


def test_resolve_dedups_and_sorts_proposed() -> None:
    inv = _inv("b-skill", "a-skill")
    sel = resolve_planning_skills(["b-skill", "a-skill", "b-skill", "  "], inv)
    assert [r.name for r in sel.refs] == ["a-skill", "b-skill"]


def test_resolve_empty_proposal() -> None:
    sel = resolve_planning_skills([], _inv("safe-change"))
    assert sel.refs == () and sel.dropped_unknown == () and sel.dropped_excluded == ()


# --- dedup (heading-level, deterministic) ------------------------------------------------------


def test_dedup_flags_overlapping_heading() -> None:
    user = "## Testing\nRun the suite my way.\n## Style\nUse tabs."
    bodies = [
        (SkillRef("safe-change", "", "/s/safe-change/SKILL.md"), "## Testing\nx\n## Scope\ny"),
    ]
    entries = compute_skill_dedup(user, bodies)
    assert len(entries) == 1
    assert entries[0].skill == "safe-change"
    assert entries[0].overlapping_headings == ("Testing",)  # "Scope" not in the user text


def test_dedup_no_user_text_is_noop() -> None:
    bodies = [(SkillRef("s", "", "/s/SKILL.md"), "## Testing\nx")]
    assert compute_skill_dedup(None, bodies) == ()
    assert compute_skill_dedup("", bodies) == ()


def test_dedup_no_overlap() -> None:
    user = "## Deployment\nShip it."
    bodies = [(SkillRef("s", "", "/s/SKILL.md"), "## Testing\nx")]
    assert compute_skill_dedup(user, bodies) == ()


def test_dedup_heading_match_is_normalized() -> None:
    user = "## Testing!!!\n..."
    bodies = [(SkillRef("s", "", "/s/SKILL.md"), "### testing\n...")]  # case/punct/level differ
    entries = compute_skill_dedup(user, bodies)
    assert len(entries) == 1 and entries[0].overlapping_headings == ("testing",)
