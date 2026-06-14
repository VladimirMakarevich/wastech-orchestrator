"""Repo skill inventory + planning-selected references (post-test-run §2.1/§2.2).

Skills are provider-neutral ``SKILL.md`` files in the **target repo** (``<repo>/.claude/skills/*``):
``name``/``description`` frontmatter plus a markdown body of procedural guidance. This module does a
cheap, bounded, presence-only **inventory scan** (frontmatter only — mirroring
``checks/inspect.RepositoryInspector``), lets the ``planning`` stage pick the relevant ones, and the
Core then passes the chosen files to downstream stages as **read-only reference paths** (never the
Claude-only Skill tool, so both providers behave identically; never executed).

Two deterministic, auditable steps live here:

* :func:`resolve_planning_skills` — the "agent proposes, Core decides" filter (cf. decomposition):
  keep only names the scan actually found and that are not on the gate-duplicating denylist.
* :func:`compute_skill_dedup` — flag skill sections whose heading matches the operator's appended
  planning guidance, so plan.md can record that the operator's text wins for that topic (§2.2).

Nothing here builds CLI argv, reads env, or weakens the sandbox; skill bodies are repo-controlled
(untrusted) and are only ever surfaced by path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

# Cap per-file reads so a pathological SKILL.md can never wedge or balloon the scan (cf. inspect).
_MAX_FILE_BYTES = 262_144

# Gate-duplicating skills the orchestrator already owns as deterministic gates/guardrails — excluded
# from what is surfaced to planning by default (two-sources-of-truth + scope-creep risk, §2.1).
DEFAULT_EXCLUDED_SKILLS: tuple[str, ...] = ("run-checks", "test", "sync-docs")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SkillRef:
    """One repo skill: its name, one-line description, and the path to its ``SKILL.md``."""

    name: str
    description: str
    path: str


@dataclass(frozen=True)
class SkillInventory:
    """The scanned skill inventory. ``excluded`` names are present but withheld from planning."""

    skills: tuple[SkillRef, ...] = ()
    excluded: frozenset[str] = frozenset()

    def relevant(self) -> tuple[SkillRef, ...]:
        """Procedural-knowledge skills offered to planning (everything not on the denylist)."""
        return tuple(s for s in self.skills if s.name not in self.excluded)

    def by_name(self, name: str) -> SkillRef | None:
        for skill in self.relevant():
            if skill.name == name:
                return skill
        return None


@dataclass(frozen=True)
class SkillSelection:
    """The Core's deterministic acceptance of the planning agent's proposed skill names (§2.1)."""

    refs: tuple[SkillRef, ...] = ()
    dropped_unknown: tuple[str, ...] = ()
    dropped_excluded: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillDedupEntry:
    """A selected skill with section headings that overlap the operator's planning text (§2.2)."""

    skill: str
    path: str
    overlapping_headings: tuple[str, ...]


class SkillInventoryScanner:
    """Scan ``<skills_root>/*/SKILL.md`` for name+description (read-only, bounded, frontmatter)."""

    def __init__(
        self,
        skills_root: str | Path,
        *,
        denied_read_paths: tuple[str, ...] = (),
        excluded_names: Sequence[str] = DEFAULT_EXCLUDED_SKILLS,
        max_file_bytes: int = _MAX_FILE_BYTES,
    ) -> None:
        self._root = Path(skills_root)
        self._denied = tuple(denied_read_paths)
        self._excluded = frozenset(excluded_names)
        self._max_bytes = max_file_bytes

    def collect(self) -> SkillInventory:
        if not self._root.is_dir():
            return SkillInventory(skills=(), excluded=self._excluded)
        refs: list[SkillRef] = []
        try:
            entries = sorted(p for p in self._root.iterdir() if p.is_dir())
        except OSError:
            return SkillInventory(skills=(), excluded=self._excluded)
        for skill_dir in entries:
            ref = self._scan_one(skill_dir / "SKILL.md")
            if ref is not None:
                refs.append(ref)
        return SkillInventory(skills=tuple(refs), excluded=self._excluded)

    def read_body(self, ref: SkillRef) -> str | None:
        """Read a selected skill's body (used only by the §2.2 dedup; bounded, denied-aware)."""
        return self._read_text(Path(ref.path))

    def _scan_one(self, skill_md: Path) -> SkillRef | None:
        text = self._read_text(skill_md)
        if text is None:
            return None
        match = _FRONTMATTER_RE.match(text)
        if match is None:
            return None  # no frontmatter → not a well-formed skill; skip defensively
        try:
            meta = yaml.safe_load(match.group(1))
        except yaml.YAMLError:
            return None
        if not isinstance(meta, dict):
            return None
        name = meta.get("name")
        description = meta.get("description")
        if not isinstance(name, str) or not name.strip():
            return None
        desc = description.strip() if isinstance(description, str) else ""
        return SkillRef(name=name.strip(), description=desc, path=str(skill_md))

    def _read_text(self, path: Path) -> str | None:
        try:
            rel = path.relative_to(self._root)
        except ValueError:
            rel = path
        if any(rel.match(glob) or path.match(glob) for glob in self._denied):
            return None
        try:
            if not path.is_file() or path.stat().st_size > self._max_bytes:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


def resolve_planning_skills(proposed: Sequence[str], inventory: SkillInventory) -> SkillSelection:
    """Keep only proposed names the scan found and that are not gate-duplicating (§2.1).

    Deterministic, like decomposition's accept rule: the agent proposes names; the Core decides. A
    name that does not resolve to a relevant skill is dropped (``dropped_unknown``); a name that
    resolves only to an *excluded* (gate-duplicating) skill is dropped (``dropped_excluded``). The
    agent can never introduce a path the Core did not independently discover. Order is stable and
    de-duplicated.
    """
    refs: list[SkillRef] = []
    seen: set[str] = set()
    dropped_unknown: list[str] = []
    dropped_excluded: list[str] = []
    excluded_present = {s.name for s in inventory.skills if s.name in inventory.excluded}
    for raw in proposed:
        name = raw.strip() if isinstance(raw, str) else ""
        if not name or name in seen:
            continue
        seen.add(name)
        ref = inventory.by_name(name)
        if ref is not None:
            refs.append(ref)
        elif name in excluded_present:
            dropped_excluded.append(name)
        else:
            dropped_unknown.append(name)
    refs.sort(key=lambda r: r.name)
    return SkillSelection(
        refs=tuple(refs),
        dropped_unknown=tuple(sorted(dropped_unknown)),
        dropped_excluded=tuple(sorted(dropped_excluded)),
    )


def _normalize_heading(heading: str) -> str:
    """Lowercase, drop punctuation/extra whitespace — for deterministic heading match (§2.2)."""
    cleaned = re.sub(r"[^a-z0-9 ]+", "", heading.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def markdown_headings(text: str) -> list[str]:
    """The raw markdown heading titles (``# ...`` .. ``###### ...``) in *text*, in order."""
    return [m.strip() for m in _HEADING_RE.findall(text)]


def compute_skill_dedup(
    user_text: str | None, bodies: Sequence[tuple[SkillRef, str]]
) -> tuple[SkillDedupEntry, ...]:
    """Flag selected-skill sections whose heading matches the operator's planning text (§2.2).

    Heading-level and fully deterministic (v1): a skill section is "overlapping" when its normalized
    heading equals a heading in the operator's appended planning guidance. The skill is still
    referenced by path — nothing is deleted — but plan.md records that the operator's explicit text
    takes precedence there, so the agent is not handed the same instruction twice. With no appended
    planning text (the common case) this is a no-op.
    """
    if not user_text or not user_text.strip():
        return ()
    user_headings = {_normalize_heading(h) for h in markdown_headings(user_text)}
    user_headings.discard("")
    if not user_headings:
        return ()
    entries: list[SkillDedupEntry] = []
    for ref, body in bodies:
        seen: set[str] = set()
        unique: list[str] = []
        for heading in markdown_headings(body):
            if _normalize_heading(heading) in user_headings and heading not in seen:
                seen.add(heading)
                unique.append(heading)
        if unique:
            entries.append(
                SkillDedupEntry(skill=ref.name, path=ref.path, overlapping_headings=tuple(unique))
            )
    return tuple(entries)
