"""The one sentence a node is handed about where to start on the task's change.

Every node after the first edit receives the same facts about the change — the overview, the base
commit and the full diff — and this sentence differs only in which of them it says to start from.
A node that can run commands is sent to the overview and to per-file inspection against the base,
so it opens what it needs instead of loading the whole diff and then inspecting the tree on top of
it; a node that cannot is sent to the full diff, its only view of the change, with the overview as
the map of what it holds. The choice is made for the most restrictive provider the node's route may
land on, because the sentence is written once per request and a fallback attempt reuses it: a node
is never told to run a command it may not be able to run.

Built here rather than in a role prompt because a prompt cannot branch on capability, and kept to
``git`` verbs — which the orchestrator already owns — so the core names no provider tool or flag.
"""

from __future__ import annotations

from wastech_orchestrator.diff_overview import LAST_COMMIT_TITLE


def change_guidance(*, base_sha: str, has_shell: bool, decomposed: bool) -> str:
    """The where-to-start sentence for a node, given whether it is sure to have a shell.

    *has_shell* must be the answer for **every** provider on the node's route, unknown counting as
    no. An added file has no history at *base_sha* to diff against and a path git leaves untracked
    is not in ``git diff`` at all, so the shell variant sends such a file to be read directly. In a
    decomposed run (*decomposed*) the overview may carry a second section, measured from the
    orchestrator's last commit — the subtask in progress — and the sentence says which one it is.
    """
    if has_shell:
        sentence = (
            "Start from the change overview: it lists every changed path, how it changed and its "
            "line counts. Open only what you need — read an added (A) file directly, and inspect "
            f"any other change with `git diff {base_sha} -- <path>` (quote a path that contains "
            "spaces); read the full diff only if you need all of it. Do not inspect a path marked "
            "[denied]."
        )
    else:
        sentence = (
            "Read the full diff for the change; the change overview lists what it contains and how "
            "large it is, so you can plan your reading."
        )
    if decomposed:
        sentence += (
            f' This is one subtask of a larger task: the overview\'s "{LAST_COMMIT_TITLE}" '
            "section, when present, is this subtask's own change, and the section above it is the "
            "whole task so far."
        )
    return sentence
