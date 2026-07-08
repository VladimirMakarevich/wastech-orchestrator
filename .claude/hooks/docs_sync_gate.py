#!/usr/bin/env python
"""Stop-hook docs-sync gate for wastech-orchestrator.

When a turn ends with code changed under ``src/`` but no docs change, this blocks the stop
once and reminds Claude to sync the docs (or to state that the change has no doc impact). It is the
deterministic backstop for the "keep docs in sync" rule in
CLAUDE.md / .agents/rules/git-workflow.md; the *how* lives in the ``/sync-docs`` skill.

A loop guard (``stop_hook_active``) surfaces the reminder at most once per stop-cycle — it never
spins. Cross-platform and side-effect-free: it only reads ``git status`` (fixed argv, no shell) and
prints a decision. Tests-only / config-only / doc-only change sets never trigger it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Paths that count as "documentation" for the purpose of this gate (git uses forward slashes).
_DOC_FILES = ("README.md",)


def _changed_paths() -> list[str]:
    """Repo-relative paths with any working-tree change (staged, unstaged, or untracked)."""
    proc = subprocess.run(  # fixed argv, no shell, no user input
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        return []
    paths: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) <= 3:
            continue
        entry = line[3:]  # drop the two-char XY status + the separating space
        if " -> " in entry:  # a rename is reported as "old -> new"; the new path is what changed
            entry = entry.split(" -> ", 1)[1]
        paths.append(entry.strip().strip('"'))
    return paths


def _should_block(paths: list[str]) -> bool:
    """True when code under ``src/`` changed but no docs/README did (pure, testable)."""
    code_changed = any(p.startswith("src/") for p in paths)
    docs_changed = any(
        p.startswith("docs/") or p.startswith(".agents/") or p in _DOC_FILES for p in paths
    )
    return code_changed and not docs_changed


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # Surface the reminder at most once per stop-cycle, so we never loop.
    if payload.get("stop_hook_active"):
        return 0

    if _should_block(_changed_paths()):
        reason = (
            "You changed code under src/ this session but touched no docs/ files. "
            "If this affects behavior, the CLI, config, or contracts, update the relevant docs "
            "in the same change (run /sync-docs), and record any deferred work in "
            "docs/backlog/follow_ups.md. Also check the shipped operator-facing docs under "
            "src/wastech_orchestrator/packaged/ (the guide/ quickstarts, config.example.yaml, the "
            "built-in flows / role prompts) — they live under src/ and are routinely forgotten. "
            "Note: docs/functional/ and docs/likec4/ are updated separately via weekly reverse "
            "engineering — do not touch them here. "
            "If the change has no documentation impact, say so explicitly and finish."
        )
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
