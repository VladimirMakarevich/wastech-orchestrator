"""Guard: the deleted legacy execution path must never reappear in the source (flow-engine Slice 7).

The FlowEngine is the sole driver. These symbols were removed when the hardcoded ``_drive``
pipeline, the granular-status loop, the stage-indexed prompt store, and the ``stage_runs`` audit
table were deleted. A grep over the package source keeps them gone (a regression that re-introduces
any of them trips here, not a subtle runtime path). Only ``.py`` text is scanned.
"""

from __future__ import annotations

import re
from pathlib import Path

import wastech_orchestrator

# Word-boundary patterns so ``_drive_via_engine`` (the live engine entry) and ``node_run_id`` do not
# trip the guard for ``_drive`` / ``stage_run``.
_FORBIDDEN = (
    r"\b_drive\b",  # the deleted legacy driver method
    r"\b_run_unit\b",
    r"\b_enter_fixing\b",
    r"\b_run_units_and_finish\b",
    r"\b_after_edit_target\b",
    r"\bPromptTemplateStore\b",  # stage-indexed prompt store (role_file is the template now)
    r"\bLoopController\b",  # the engine owns loop budgets in FlowRunState
    r"\brecord_stage_run\b",  # the engine writes node_runs
    # The four-field summary stub. One renderer writes the PR body on every terminal now, so a
    # second writer reappearing is the defect: two formats for one committed artifact.
    r"\bwrite_minimal_summary\b",
)
_SRC_ROOT = Path(wastech_orchestrator.__file__).parent


def test_no_legacy_execution_symbols_in_source() -> None:
    compiled = [(p, re.compile(p)) for p in _FORBIDDEN]
    offenders: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for pattern, rx in compiled:
            if rx.search(text):
                offenders.append(f"{path.relative_to(_SRC_ROOT.parent)}: {pattern}")
    assert not offenders, "legacy execution symbols resurfaced:\n" + "\n".join(offenders)
