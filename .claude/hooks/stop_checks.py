#!/usr/bin/env python
"""Stop-hook quality gate for wastech-orchestrator.

Runs the *fast* checks (ruff + pytest) when a Claude Code turn ends, so the suite stays green during
development. If anything is red, it blocks the stop and feeds the failure back to Claude
(``decision: block``) so the cause is fixed on the next turn. A loop guard (``stop_hook_active``)
surfaces a red gate at most once per stop-cycle — it never spins. ``mypy`` is left to the heavier
``/run-checks`` gate (run before a commit / PR / phase transition).

Cross-platform: invoked as ``python .claude/hooks/stop_checks.py`` (works under PowerShell and
bash); sub-checks run via ``sys.executable -m ...`` so they use the same interpreter that has the
project's dev deps installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAX_REASON = 6000  # keep the feedback compact when it blocks


def _run(args: list[str]) -> tuple[int, str]:
    proc = subprocess.run(  # fixed argv, no shell, no user input
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    # Surface a red gate at most once per stop-cycle, so we never loop on an unfixable failure.
    if payload.get("stop_hook_active"):
        return 0

    failures: list[str] = []
    for label, args in (
        ("ruff check .", [sys.executable, "-m", "ruff", "check", "."]),
        ("pytest", [sys.executable, "-m", "pytest", "-q"]),
    ):
        code, output = _run(args)
        if code != 0:
            failures.append(f"$ {label}\n{output.strip()}")

    if not failures:
        return 0

    reason = (
        "The quality gate is red after this turn — fix the cause before finishing "
        "(ruff/pytest below). mypy is checked separately by /run-checks.\n\n"
        + "\n\n".join(failures)
    )
    if len(reason) > MAX_REASON:
        reason = reason[:MAX_REASON] + "\n...[truncated]"
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
