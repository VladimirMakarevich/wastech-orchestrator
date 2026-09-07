"""Deterministic citation-manifest validator — no LLM, no network.

The research synthesis node writes a manifest beside its report (``sources.json`` by default; the
checks node's ``manifest`` field names it): one entry per cited source, each pointing into the
repository (``path`` + optional ``line`` / ``snippet``) or at an external ``url``. This checker
validates every entry against the repository and classifies it:

* ``verified``   — the cited path exists and the snippet is present **at the cited line**;
* ``weak``       — the snippet is in the file but not at the cited line, so the location is
                   mis-attributed even though the quote is real — recorded, never gating;
* ``broken``     — the citation points at something that does not exist (a hallucinated path/line,
                   or a snippet absent from the file) — the gating signal;
* ``uncheckable``— the citation cannot be validated deterministically (an external ``url``, an entry
                   carrying no snippet to check, or a malformed entry) — recorded, never gating.

What this checker does **not** do is judge the ``claim``: a real snippet at a real line can still be
attached to a fabricated assertion, and that is the verifier node's job, not a deterministic check.
Read a passing result as "every location resolves", never as "every claim holds".

The aggregate ``passed`` is ``False`` iff any entry is ``broken``: a hallucinated citation fails the
check, and the flow's ``citation_check → synthesis (fail)`` edge sends synthesis back to fix or drop
it. ``weak`` is deliberately non-gating — an imprecise line number is worth surfacing to the
verifier, but failing a run over it would park the task for a citation whose quote is genuine. A
**missing or malformed manifest** is ``uncheckable`` and does **not** crash or fail — we cannot
prove a hallucination from an unreadable manifest, and the after-stage output guard is what
enforces that the manifest exists at all. No network: an external ``url`` is uncheckable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.output_policy import is_within


class CitationStatus(StrEnum):
    """The four deterministic outcomes for one cited source. Only ``BROKEN`` gates."""

    VERIFIED = "verified"
    WEAK = "weak"
    BROKEN = "broken"
    UNCHECKABLE = "uncheckable"


@dataclass(frozen=True, slots=True)
class CitationEntry:
    """One classified citation: its id (best-effort), status, reason, and the location it cited.

    ``path`` / ``line`` are echoed back so a consumer of the report — the verifier node, which now
    receives it — can act on an entry without parsing the reason prose.
    """

    source_id: str
    status: CitationStatus
    reason: str
    path: str | None = None
    line: int | None = None


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
    """Validate the citation manifest at *manifest_path* against the repository at *repo_dir*.

    Never raises on a missing / malformed manifest — those return an ``uncheckable`` report with
    ``passed=True`` (we cannot prove a hallucination from an unreadable manifest). The reasons name
    the manifest's actual filename, which the flow chooses, so a renamed manifest is diagnosable.
    """
    path = Path(manifest_path)
    name = path.name
    if not path.is_file():
        return CitationReport(
            passed=True,
            entries=(CitationEntry("<manifest>", CitationStatus.UNCHECKABLE, f"{name} missing"),),
            manifest_status="missing",
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return CitationReport(
            passed=True,
            entries=(
                CitationEntry("<manifest>", CitationStatus.UNCHECKABLE, f"malformed {name}: {exc}"),
            ),
            manifest_status="malformed",
        )
    sources = raw.get("sources") if isinstance(raw, dict) else None
    if not isinstance(sources, list):
        return CitationReport(
            passed=True,
            entries=(
                CitationEntry(
                    "<manifest>", CitationStatus.UNCHECKABLE, f"{name} has no 'sources' list"
                ),
            ),
            manifest_status="malformed",
        )
    entries = tuple(_classify(repo_dir, src, index) for index, src in enumerate(sources))
    passed = not any(e.status is CitationStatus.BROKEN for e in entries)
    return CitationReport(passed=passed, entries=entries, manifest_status="ok")


def _classify(repo_dir: str | Path, src: Any, index: int) -> CitationEntry:
    """Classify a single manifest entry as verified / weak / broken / uncheckable."""
    if not isinstance(src, dict):
        return CitationEntry(f"#{index}", CitationStatus.UNCHECKABLE, "entry is not a mapping")
    source_id = str(src.get("id") or f"#{index}")
    rel_path = src.get("path")
    line_no = src.get("line") if isinstance(src.get("line"), int) else None
    if not isinstance(rel_path, str) or not rel_path:
        # No in-repo path → only an external reference (url) or an unanchored claim: uncheckable.
        if isinstance(src.get("url"), str):
            return CitationEntry(
                source_id, CitationStatus.UNCHECKABLE, "external url (not fetched)"
            )
        return CitationEntry(source_id, CitationStatus.UNCHECKABLE, "no 'path' to validate")

    here = partial(CitationEntry, source_id, path=rel_path, line=line_no)
    target = Path(repo_dir) / rel_path
    if not is_within(repo_dir, target):
        return here(CitationStatus.BROKEN, f"path escapes repo: {rel_path!r}")
    if not target.is_file():
        return here(CitationStatus.BROKEN, f"no such file: {rel_path!r}")

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return here(CitationStatus.UNCHECKABLE, f"could not read {rel_path!r}: {exc}")
    lines = text.splitlines()

    if line_no is not None and (line_no < 1 or line_no > len(lines)):
        return here(CitationStatus.BROKEN, f"line {line_no} out of range in {rel_path!r}")

    snippet = src.get("snippet")
    if not isinstance(snippet, str) or not snippet.strip():
        # Nothing to check against the file. Calling this `verified` claimed a verification that
        # never happened: `path` + `line` alone only prove the file exists and the line is in range.
        return here(CitationStatus.UNCHECKABLE, "no 'snippet' to verify the cited location")
    needle = snippet.strip()
    # A snippet may quote several lines, so match against a window of that height rather than a
    # single line — a correct multi-line quote must not be downgraded for its shape.
    span = needle.count("\n") + 1

    def window_at(start: int) -> str:
        return "\n".join(lines[start - 1 : start - 1 + span])

    found_at = next((i for i in range(1, len(lines) + 1) if needle in window_at(i)), None)
    if found_at is None and needle not in text:
        return here(CitationStatus.BROKEN, f"snippet not found in {rel_path!r}")
    if line_no is None:
        # The entry claimed nothing about *where* in the file, so there is no location to
        # mis-attribute: "this quote is in this file" is the whole claim, and it holds.
        return here(CitationStatus.VERIFIED, "snippet present in the cited file (no line cited)")
    if needle in window_at(line_no):
        return here(CitationStatus.VERIFIED, "snippet present at the cited line")
    # The quote is real but the location is not — a distinct verdict, not a pass: accepting it as
    # `verified` would make the cited line number decorative, since a correct snippet on a wrong
    # in-range line would clear the gate. Name the real line so synthesis can repair it in one
    # round.
    where = f"at line {found_at}" if found_at is not None else "elsewhere"
    return here(
        CitationStatus.WEAK, f"snippet found {where} in {rel_path!r}, not at line {line_no}"
    )
