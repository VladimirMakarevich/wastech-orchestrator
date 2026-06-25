"""The safe prompt renderer.

A flow node's prompt template is the content of its ``role_file`` (see
:mod:`wastech_orchestrator.core.flow.prompt`); this module only turns a template into the final
``AgentRunRequest.prompt`` by substituting an **allowlisted** set of metadata/artifact *path*
variables (never task bodies, diffs, check logs, env, or secrets — those stay in the artifact files
the provider references by path).

Invariant this module preserves: it produces only stdin prompt *text*. It never touches provider
argv, CLI syntax, the sandbox/approvals, denied commands/reads, the env allowlist, or fallback
policy. A template cannot weaken any of those (backlog).
"""

from __future__ import annotations

import re

# The only names a template may interpolate (backlog). Everything is metadata or an artifact
# *path*; large content is never injected. An unknown ``{name}`` is left verbatim, so a template
# carrying code/JSON braces renders unchanged.
ALLOWED_PROMPT_VARS: frozenset[str] = frozenset(
    {
        "task_id",
        "stage",
        "repo_path",
        "repo",  # flow-engine alias for ``repo_path``; single shared allowlist
        "task_path",
        "plan_path",
        "diff_path",
        "checks_path",
        "review_path",
        "subtask_order",
        "subtask_count",
        "subtask_spec_path",
        "skills_path",
    }
)

_VAR_RE = re.compile(r"\{([a-z_]+)\}")


def render_prompt(template: str, variables: dict[str, object | None]) -> str:
    """Substitute allowlisted ``{name}`` tokens in *template*; leave everything else verbatim.

    Only names in :data:`ALLOWED_PROMPT_VARS` are replaced. A ``None`` value renders as the empty
    string. Any other ``{...}`` (an unknown name, or literal braces in code/JSON) passes through
    unchanged — there is no ``KeyError`` and no breakage on stray braces (the "safe renderer").
    """

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in ALLOWED_PROMPT_VARS:
            return match.group(0)
        value = variables.get(name)
        return "" if value is None else str(value)

    return _VAR_RE.sub(_replace, template)
