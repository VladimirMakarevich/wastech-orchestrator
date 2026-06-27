"""Whole-repo skill discovery (git ls-files based), collision identity, and token resolution."""

from __future__ import annotations

from pathlib import Path

from wastech_orchestrator.core.skills import (
    SkillInventory,
    SkillInventoryScanner,
    SkillRef,
    resolve_skills,
)


def _write_skill(repo: Path, rel_dir: str, name: str, *, description: str = "d") -> str:
    """Create ``<repo>/<rel_dir>/SKILL.md`` and return its repo-relative POSIX path."""
    d = repo / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n", encoding="utf-8"
    )
    return f"{rel_dir}/SKILL.md"


def _scanner(
    repo: Path, tracked: list[str], *, denied: tuple[str, ...] = ()
) -> SkillInventoryScanner:
    return SkillInventoryScanner(repo, lambda: tuple(tracked), denied_read_paths=denied)


# --- discovery -------------------------------------------------------------------------------


def test_scan_discovers_tracked_skills_whole_repo(tmp_path: Path) -> None:
    p1 = _write_skill(tmp_path, ".claude/skills/safe-change", "safe-change", description="Review")
    p2 = _write_skill(tmp_path, "backend/.agents/skills/add-provider", "add-provider")
    inv = _scanner(tmp_path, [p1, p2, "README.md", "src/app.py"]).collect()
    by = {s.name: s for s in inv.skills}
    assert set(by) == {"safe-change", "add-provider"}
    assert by["safe-change"].description == "Review"
    # identity is the repo-relative POSIX path (not absolute) — stable + collision key
    assert by["safe-change"].path == ".claude/skills/safe-change/SKILL.md"
    assert by["add-provider"].path == "backend/.agents/skills/add-provider/SKILL.md"


def test_scan_skips_malformed_or_frontmatterless(tmp_path: Path) -> None:
    good = _write_skill(tmp_path, "a/good", "good")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "SKILL.md").write_text("# just a heading\n", encoding="utf-8")
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
    inv = _scanner(tmp_path, [good, "b/SKILL.md", "c/SKILL.md"]).collect()
    assert [s.name for s in inv.skills] == ["good"]


def test_scan_filters_non_skill_basenames(tmp_path: Path) -> None:
    good = _write_skill(tmp_path, "a", "good")
    # Defensive guard: only ``SKILL.md`` basenames scanned, not ``SKILL.md.bak`` / ``SKILLS.md``.
    inv = _scanner(tmp_path, [good, "a/SKILL.md.bak", "docs/SKILLS.md"]).collect()
    assert [s.name for s in inv.skills] == ["good"]


def test_scan_empty_when_no_tracked_skills(tmp_path: Path) -> None:
    assert _scanner(tmp_path, ["README.md", "src/app.py"]).collect().skills == ()


def test_scan_honors_denied_read_paths(tmp_path: Path) -> None:
    secret = _write_skill(tmp_path, "secret", "secret-skill")
    ok = _write_skill(tmp_path, "ok", "ok-skill")
    inv = _scanner(tmp_path, [secret, ok], denied=("secret/SKILL.md",)).collect()
    assert [s.name for s in inv.skills] == ["ok-skill"]


# --- identity / resolution -------------------------------------------------------------------


def _inv(*entries: tuple[str, str]) -> SkillInventory:
    """Build an inventory from ``(name, repo-relative-path)`` pairs."""
    return SkillInventory(
        skills=tuple(SkillRef(name=n, description="", path=p) for n, p in entries)
    )


def test_resolve_by_unique_name() -> None:
    inv = _inv(("alpha", "x/alpha/SKILL.md"), ("beta", "y/beta/SKILL.md"))
    sel = resolve_skills(["beta"], inv)
    assert [r.name for r in sel.refs] == ["beta"]
    assert sel.unknown == () and sel.ambiguous == ()


def test_resolve_unknown_token() -> None:
    sel = resolve_skills(["ghost"], _inv(("alpha", "a/SKILL.md")))
    assert sel.refs == () and sel.unknown == ("ghost",) and sel.ambiguous == ()


def test_collision_bare_name_is_ambiguous_resolve_by_path() -> None:
    inv = _inv(
        ("testing", "backend/skills/testing/SKILL.md"),
        ("testing", "mobile/skills/testing/SKILL.md"),
    )
    amb = resolve_skills(["testing"], inv)
    assert amb.refs == () and amb.ambiguous == ("testing",)
    # addressing the colliding name by its repo-relative path is unambiguous
    byp = resolve_skills(["backend/skills/testing/SKILL.md"], inv)
    assert [r.path for r in byp.refs] == ["backend/skills/testing/SKILL.md"]


def test_resolve_dedups_by_path_and_sorts() -> None:
    inv = _inv(("b", "p/b/SKILL.md"), ("a", "p/a/SKILL.md"))
    # "a" named twice (once by name, once by its path), "b" twice, plus a blank token
    sel = resolve_skills(["b", "a", "b", "  ", "p/a/SKILL.md"], inv)
    assert [r.name for r in sel.refs] == ["a", "b"]  # sorted by (name, path), de-duped by path


def test_resolve_empty_proposal() -> None:
    sel = resolve_skills([], _inv(("a", "a/SKILL.md")))
    assert sel.refs == () and sel.unknown == () and sel.ambiguous == ()


def test_unresolved_property_merges_unknown_and_ambiguous() -> None:
    inv = _inv(("t", "a/SKILL.md"), ("t", "b/SKILL.md"))
    sel = resolve_skills(["t", "ghost"], inv)
    assert sel.unresolved == ("ghost", "t")  # sorted union of ambiguous + unknown
