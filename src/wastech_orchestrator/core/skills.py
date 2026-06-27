"""Repo skill inventory + skill-token resolution (Model A: read-only reference paths).

Skills are provider-neutral ``SKILL.md`` files anywhere in the **target repo** — a monorepo may
scatter them (``mobile/``, ``backend/``, ``.claude/skills/``, ``.agents/skills/``, or any tracked
directory). This module discovers them by enumerating tracked ``**/SKILL.md`` via ``git ls-files``
(ignore-aware and bounded for free — untracked ``node_modules``/build/vendor never appear), reads
``name``/``description`` frontmatter bounded + denied-aware, and resolves operator-pinned /
supervisor-proposed skill *tokens* against that inventory.

Selection is **provenance-closed and deterministic** — "the proposer proposes, the Core decides":
a token is accepted only when it resolves to exactly one *discovered* skill (by globally-unique
``name``, else by repo-relative path), so a name/path can never introduce a file the scan did not
independently find. The Core surfaces accepted skills to flow nodes purely as **read-only reference
paths** (never the Claude-only Skill tool, so both providers behave identically; never executed).
Skill bodies are repo-controlled (untrusted) and are only ever surfaced by path.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml

# Cap per-file reads so a pathological SKILL.md can never wedge or balloon the scan (cf. inspect).
_MAX_FILE_BYTES = 262_144

_SKILL_BASENAME = "SKILL.md"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class SkillRef:
    """One repo skill: frontmatter ``name``/``description`` and its repo-relative ``SKILL.md`` path.

    ``path`` is the repo-relative POSIX path (what ``git ls-files`` yields). It is the collision
    key, the operator path-pin token, and the persisted identity — stable across platforms. The
    absolute path surfaced to a provider is derived by joining the clone root at the wiring seam.
    """

    name: str
    description: str
    path: str


@dataclass(frozen=True)
class SkillResolveResult:
    """The outcome of resolving one token: a hit, an ambiguous bare name, or no match."""

    ref: SkillRef | None
    status: Literal["resolved", "ambiguous", "unknown"]


@dataclass(frozen=True)
class SkillInventory:
    """The discovered skill inventory; a token resolves by unique name, else repo-relative path."""

    skills: tuple[SkillRef, ...] = ()

    def resolve(self, token: str) -> SkillResolveResult:
        """Resolve one operator/supervisor token to a skill, or report why it cannot.

        Exact-match and provenance-closed: a token equal to exactly one repo-relative path resolves
        to it; else a token equal to exactly one frontmatter ``name`` resolves to it; a bare name
        shared by more than one skill is ``ambiguous`` (address it by path); anything else is
        ``unknown``. Identity never depends on scope — a name resolves to exactly one skill or not.
        """
        name = token.strip()
        if not name:
            return SkillResolveResult(ref=None, status="unknown")
        by_path = [s for s in self.skills if s.path == name]
        if by_path:
            return SkillResolveResult(ref=by_path[0], status="resolved")
        by_name = [s for s in self.skills if s.name == name]
        if len(by_name) == 1:
            return SkillResolveResult(ref=by_name[0], status="resolved")
        if len(by_name) > 1:
            return SkillResolveResult(ref=None, status="ambiguous")
        return SkillResolveResult(ref=None, status="unknown")


@dataclass(frozen=True)
class SkillSelection:
    """The Core's deterministic acceptance of a set of tokens (pins or a dynamic proposal).

    ``refs`` are the accepted skills (de-duplicated by path, ordered by ``(name, path)``).
    ``unknown`` / ``ambiguous`` are the tokens that did not resolve — an error only for operator
    pins under ``skills.strict``; a dynamic proposal just drops them.
    """

    refs: tuple[SkillRef, ...] = ()
    unknown: tuple[str, ...] = ()
    ambiguous: tuple[str, ...] = ()

    @property
    def unresolved(self) -> tuple[str, ...]:
        """All tokens that did not resolve (unknown ∪ ambiguous), sorted — for a pin report."""
        return tuple(sorted({*self.unknown, *self.ambiguous}))


class SkillInventoryScanner:
    """Discover tracked ``**/SKILL.md`` via ``git ls-files`` and read their frontmatter."""

    def __init__(
        self,
        repo_dir: str | Path,
        list_tracked: Callable[[], Sequence[str]],
        *,
        denied_read_paths: tuple[str, ...] = (),
        max_file_bytes: int = _MAX_FILE_BYTES,
    ) -> None:
        self._repo = Path(repo_dir)
        self._list_tracked = list_tracked
        self._denied = tuple(denied_read_paths)
        self._max_bytes = max_file_bytes

    def collect(self) -> SkillInventory:
        """Scan every tracked ``SKILL.md`` for name+description (read-only, bounded frontmatter)."""
        refs: list[SkillRef] = []
        seen: set[str] = set()
        for raw in sorted(self._list_tracked()):
            rel = raw.replace("\\", "/")
            if rel in seen or PurePosixPath(rel).name != _SKILL_BASENAME:
                continue
            seen.add(rel)
            ref = self._scan_one(rel)
            if ref is not None:
                refs.append(ref)
        return SkillInventory(skills=tuple(refs))

    def _scan_one(self, rel: str) -> SkillRef | None:
        text = self._read_text(rel)
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
        # The repo-relative POSIX path is the surfaced reference + collision identity (forward
        # slashes on Windows too); the clone root is joined back on at the wiring seam.
        return SkillRef(name=name.strip(), description=desc, path=rel)

    def _read_text(self, rel: str) -> str | None:
        """Read one repo-relative file, bounded and denied-aware (matches the security globs)."""
        rel_path = PurePosixPath(rel)
        abs_path = self._repo / rel
        if any(rel_path.match(glob) or abs_path.match(glob) for glob in self._denied):
            return None
        try:
            if not abs_path.is_file() or abs_path.stat().st_size > self._max_bytes:
                return None
            return abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None


def resolve_skills(tokens: Sequence[str], inventory: SkillInventory) -> SkillSelection:
    """Resolve skill *tokens* against the inventory — "the proposer proposes, the Core decides".

    Deterministic and provenance-closed: a token is accepted only when it resolves to exactly one
    discovered skill (by unique name or by repo-relative path). Unresolved tokens are categorized as
    ``unknown`` (no match) or ``ambiguous`` (a bare name shared by more than one skill). Accepted
    refs are de-duplicated by path and ordered by ``(name, path)``; the dropped lists are sorted.
    """
    refs: list[SkillRef] = []
    seen_paths: set[str] = set()
    unknown: list[str] = []
    ambiguous: list[str] = []
    for raw in tokens:
        token = raw.strip() if isinstance(raw, str) else ""
        if not token:
            continue
        result = inventory.resolve(token)
        if result.status == "resolved" and result.ref is not None:
            if result.ref.path not in seen_paths:
                seen_paths.add(result.ref.path)
                refs.append(result.ref)
        elif result.status == "ambiguous":
            ambiguous.append(token)
        else:
            unknown.append(token)
    refs.sort(key=lambda r: (r.name, r.path))
    return SkillSelection(
        refs=tuple(refs),
        unknown=tuple(sorted(set(unknown))),
        ambiguous=tuple(sorted(set(ambiguous))),
    )
