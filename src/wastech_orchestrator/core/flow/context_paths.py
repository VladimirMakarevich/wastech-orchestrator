"""Allowlisted path context shared by prompt rendering and the tool-node stdin (P5, seam #4).

The single collector of the allowlisted artifact paths a node may see — the repo root plus the
task / plan / diff / checks / review artifact paths. Extracted from the agent runner's private
``_prompt_variables`` so exactly one definition feeds both:

* the agent / evaluator prompt-variable dict (``{repo}``, ``{task_path}``, …), and
* the ``tool`` node's stdin ``paths`` object (P5) — the *same* allowlisted set, never secrets, the
  full environment, or a raw session id.

Keeping it here (not on a runner) means a new node kind reuses the same allowlist with no
duplication and no drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wastech_orchestrator.core.flow.nodes.base import NodeInputs


def build_path_context(inputs: NodeInputs, repo_dir: str) -> dict[str, str | None]:
    """The allowlisted path context for a node: repo root + task/plan/diff/checks/review artifacts.

    Values are the raw path strings the orchestrator already resolved (``None`` when an artifact
    does not exist yet — e.g. ``diff_path`` before any edit, ``review_path`` before review). No
    secret, full environment, or session id is ever included — only these fixed, allowlisted keys.
    """
    return {
        "repo": repo_dir,
        "task_path": inputs.task_path,
        "plan_path": inputs.plan_path,
        "diff_path": inputs.diff_path,
        "checks_path": inputs.checks_path,
        "review_path": inputs.review_path,
    }
