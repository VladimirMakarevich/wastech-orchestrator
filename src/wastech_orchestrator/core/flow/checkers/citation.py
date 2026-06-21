"""Deterministic citation-manifest validator (P3.1) — no LLM, no network.

The research synthesis node writes a ``sources.json`` manifest beside its report: one entry per
cited source, each pointing into the repository (``path`` + optional ``line`` / ``snippet``) or at
an external ``url``. This checker validates every entry against the repository and classifies it:

* ``verified``   — the cited path exists in the repo and the line/snippet (when given) is present;
* ``broken``     — the citation points at something that does not exist (a hallucinated path/line,
                   or a snippet absent from the file) — the gating signal;
* ``uncheckable``— the citation cannot be validated deterministically (an external ``url``, or a
                   malformed entry) — recorded, never gating.

The aggregate ``passed`` is ``False`` iff any entry is ``broken``: a hallucinated citation fails the
check, and the flow's ``citation_check → synthesis (fail)`` edge sends synthesis back to fix or drop
it. A **missing or malformed manifest** is ``uncheckable`` and does **not** crash or fail — we
cannot prove a hallucination from an unreadable manifest, and the after-stage output guard (P3.2) is
what enforces that ``sources.json`` exists at all. No network: an external ``url`` is uncheckable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.output_policy import is_within


class CitationStatus(StrEnum):
    """The three deterministic outcomes for one cited source."""

    VERIFIED = "verified"
    BROKEN = "broken"
    UNCHECKABLE = "uncheckable"


@dataclass(frozen=True, slots=True)
class CitationEntry:
    """One classified citation: its source id (best-effort), status, and a human-readable reason."""

    source_id: str
    status: CitationStatus
    reason: str


@dataclass(frozen=True, slots=True)
class CitationReport:
    """Aggregate citation-validation result. ``passed`` is ``False`` iff any entry is ``broken``."""

    passed: bool
    entries: tuple[CitationEntry, ...]
    manifest_status: str  # "ok" | "missing" | "malformed"

    @property
    def broken(self) -> tuple[CitationEntry, ...]:
        return tuple(e for e in self.entries if e.status is CitationStatus.BROKEN)


def validate_citations(repo_dir: str | Path, manifest_path: str | Path) -> CitationReport:
    """Validate the ``sources.json`` at *manifest_path* against the repository at *repo_dir*.

    Never raises on a missing / malformed manifest — those return an ``uncheckable`` report with
    ``passed=True`` (we cannot prove a hallucination from an unreadable manifest).
    """
    path = Path(manifest_path)
    if not path.is_file():
        return CitationReport(
            passed=True,
            entries=(
                CitationEntry("<manifest>", CitationStatus.UNCHECKABLE, "sources.json missing"),
            ),
            manifest_status="missing",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return CitationReport(
            passed=True,
            entries=(
                CitationEntry(
                    "<manifest>", CitationStatus.UNCHECKABLE, f"malformed sources.json: {exc}"
                ),
            ),
            manifest_status="malformed",
        )
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list):
        return CitationReport(
            passed=True,
            entries=(
                CitationEntry(
                    "<manifest>", CitationStatus.UNCHECKABLE, "sources.json has no 'sources' list"
                ),
            ),
            manifest_status="malformed",
        )
    entries = tuple(_classify(repo_dir, src, index) for index, src in enumerate(sources))
    passed = not any(e.status is CitationStatus.BROKEN for e in entries)
    return CitationReport(passed=passed, entries=entries, manifest_status="ok")


def _classify(repo_dir: str | Path, src: Any, index: int) -> CitationEntry:
    """Classify a single manifest entry as verified / broken / uncheckable."""
    if not isinstance(src, dict):
        return CitationEntry(f"#{index}", CitationStatus.UNCHECKABLE, "entry is not a mapping")
    source_id = str(src.get("id") or f"#{index}")
    rel_path = src.get("path")
    if not isinstance(rel_path, str) or not rel_path:
        # No in-repo path → only an external reference (url) or an unanchored claim: uncheckable.
        if isinstance(src.get("url"), str):
            return CitationEntry(
                source_id, CitationStatus.UNCHECKABLE, "external url (not fetched)"
            )
        return CitationEntry(source_id, CitationStatus.UNCHECKABLE, "no 'path' to validate")

    target = Path(repo_dir) / rel_path
    if not is_within(repo_dir, target):
        return CitationEntry(source_id, CitationStatus.BROKEN, f"path escapes repo: {rel_path!r}")
    if not target.is_file():
        return CitationEntry(source_id, CitationStatus.BROKEN, f"no such file: {rel_path!r}")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CitationEntry(
            source_id, CitationStatus.UNCHECKABLE, f"could not read {rel_path!r}: {exc}"
        )
    lines = text.splitlines()

    line_no = src.get("line")
    if isinstance(line_no, int) and (line_no < 1 or line_no > len(lines)):
        return CitationEntry(
            source_id, CitationStatus.BROKEN, f"line {line_no} out of range in {rel_path!r}"
        )

    snippet = src.get("snippet")
    if isinstance(snippet, str) and snippet.strip():
        on_line = isinstance(line_no, int) and snippet.strip() in lines[line_no - 1]
        if not (on_line or snippet.strip() in text):
            return CitationEntry(
                source_id, CitationStatus.BROKEN, f"snippet not found in {rel_path!r}"
            )
    return CitationEntry(source_id, CitationStatus.VERIFIED, "path/line/snippet present")
