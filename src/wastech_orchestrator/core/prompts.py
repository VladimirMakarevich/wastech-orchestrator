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
        "memory_path",  # per-node retrieval packet path (memory subsystem); node-driven, may be ""
    }
)

_VAR_RE = re.compile(r"\{([a-z_]+)\}")

#: A conditional block ``{?name}body{/name}`` whose ``body`` is kept only when ``name`` resolves to
#: a present, non-empty allowlisted variable; otherwise the whole block (markers included) is
#: dropped. Lets a role keep optional prose — e.g. the decomposition's "subtask N of M" clause —
#: inline in its own text without leaving dangling empty placeholders when the variable is absent.
_BLOCK_RE = re.compile(r"\{\?([a-z_]+)\}(.*?)\{/\1\}", re.DOTALL)


def render_prompt(template: str, variables: dict[str, object | None]) -> str:
    """Substitute allowlisted ``{name}`` tokens in *template*; leave everything else verbatim.

    Only names in :data:`ALLOWED_PROMPT_VARS` are replaced. A ``None`` value renders as the empty
    string. Any other ``{...}`` (an unknown name, or literal braces in code/JSON) passes through
    unchanged — there is no ``KeyError`` and no breakage on stray braces (the "safe renderer").

    A conditional block ``{?name}body{/name}`` is resolved first: ``body`` is kept (and then
    flat-substituted in the normal pass) only when ``name`` is an allowlisted variable whose value
    is present and not the empty string; otherwise the entire block is removed. A non-allowlisted
    name or an unclosed/unbalanced block is left verbatim, like an unknown ``{name}``.
    """

    def _resolve_block(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in ALLOWED_PROMPT_VARS:
            return match.group(0)
        value = variables.get(name)
        return "" if value is None or value == "" else match.group(2)

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in ALLOWED_PROMPT_VARS:
            return match.group(0)
        value = variables.get(name)
        return "" if value is None else str(value)

    return _VAR_RE.sub(_replace, _BLOCK_RE.sub(_resolve_block, template))


def referenced_variables(template: str) -> set[str]:
    """The variable names *template* references via ``{name}`` or a ``{?name}...{/name}`` block.

    Uses the same token shape :func:`render_prompt` substitutes, so it is the single source of truth
    for "which names does this prompt actually use". Names outside the render allowlist are still
    returned — the caller (the flow validator's anti-drift lint) decides which are unknown; this
    function only extracts, it does not judge. Literal code/JSON braces that do not match the token
    shape are ignored, exactly as the renderer leaves them verbatim.
    """
    names = set(_VAR_RE.findall(template))
    names.update(match.group(1) for match in _BLOCK_RE.finditer(template))
    return names
