"""Allowlisted path context shared by prompt rendering and the tool-node stdin.

The single collector of the allowlisted artifact paths a node may see — the repo root plus the
task / plan / diff / checks / review artifact paths. Extracted from the agent runner's private
``_prompt_variables`` so exactly one definition feeds both:

* the agent / evaluator prompt-variable dict (``{repo}``, ``{task_path}``, …), and
* the ``tool`` node's stdin ``paths`` object — the *same* allowlisted set, never secrets, the
  full environment, or a raw session id.

Keeping it here (not on a runner) means a new node kind reuses the same allowlist with no
duplication and no drift. :func:`build_node_output_paths` is the same story for the generic
``{<node_id>_path}`` channel: the agent and evaluator runners share one definition of it.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from wastech_orchestrator.core.flow.output_policy import resolve_output_policy
from wastech_orchestrator.core.flow.schema import AgentNode, FlowNode, ToolNode
from wastech_orchestrator.providers.artifacts import (
    TOOL_STDOUT_FILENAME,
    exchange_latest_run_file,
    latest_run_file,
)

if TYPE_CHECKING:
    from wastech_orchestrator.core.flow.nodes.base import NodeInputs
    from wastech_orchestrator.core.flow.snapshot import FlowSnapshot


def build_path_context(
    inputs: NodeInputs, repo_dir: str, report_dir: str | None = None
) -> dict[str, str | None]:
    """The allowlisted path context for a node: repo root + task/plan/diff/checks/review artifacts.

    Values are the raw path strings the orchestrator already resolved (``None`` when an artifact
    does not exist yet — e.g. ``diff_path`` before any edit, ``review_path`` before review). No
    secret, full environment, or session id is ever included — only these fixed, allowlisted keys.

    ``report_dir`` is this flow's resolved report directory for this task
    (:func:`resolve_report_dir` — repo-relative POSIX, ``None`` for ``code_change``). It is the one
    entry that is not an artifact the orchestrator wrote: it is where the node is *told to write*,
    which is why it is stated once here for every reader (prompt and tool stdin) rather than
    reconstructed per runner.
    """
    return {
        "repo": repo_dir,
        "task_path": inputs.task_path,
        "plan_path": inputs.plan_path,
        "diff_path": inputs.diff_path,
        "checks_path": inputs.checks_path,
        "review_path": inputs.review_path,
        "report_dir": report_dir,
    }


def resolve_report_dir(snapshot: FlowSnapshot, task_id: str) -> str | None:
    """This flow's report directory for *task_id*: repo-relative POSIX, or ``None``.

    The flow's ``output_policy`` (with its optional ``report_dir`` base) decides it, so the answer
    is the same one the after-stage write guard and the publish node enforce against — the node is
    never told to write somewhere its own flow would refuse.
    """
    return resolve_output_policy(
        snapshot.doc.output_policy, task_id, snapshot.doc.report_dir
    ).report_subdir


def build_node_output_paths(
    nodes: Iterable[FlowNode],
    task_id: str,
    *,
    exchange_root: str,
    artifacts_root: str,
) -> dict[str, object | None]:
    """The generic ``{<node_id>_path}`` variables for every agent + tool node in the flow.

    A value resolves to the node's **latest** persisted output (an agent's ``<node_id>.out.md`` or a
    tool's redacted ``stdout.txt``), kept per-run under ``stages/<node_id>/run-<id>/``, so a
    re-running upstream node exposes its most recent pass while every earlier pass stays on disk. A
    not-yet-run or special-slot node's variable is empty (``None``) and a
    ``{?<id>_path}…{/<id>_path}`` block drops cleanly. Fan-in is free: a node names each upstream
    output it wants (``{scan_path}``, ``{md-check_path}``). The stored value is a POSIX path string
    (cross-platform).

    Only agent and tool nodes *produce* this channel; both the agent and the evaluator runner *read*
    it — a coverage gate that cannot see what the analysis nodes reported can only judge the
    repository, never the audit of it.
    """
    paths: dict[str, object | None] = {}
    for node in nodes:
        if isinstance(node, AgentNode):
            filename = f"{node.id}.out.md"
        elif isinstance(node, ToolNode):
            filename = TOOL_STDOUT_FILENAME
        else:
            continue
        # Fan-in resolves the upstream node's newest published output. With an exchange wired,
        # outputs are published there (postprocess/tool), so resolve from it; a harness without one
        # keeps resolving the private tree.
        if exchange_root:
            found = exchange_latest_run_file(exchange_root, task_id, node.id, filename)
        else:
            found = latest_run_file(artifacts_root, task_id, node.id, filename)
        paths[f"{node.id}_path"] = found.as_posix() if found is not None else None
    return paths
