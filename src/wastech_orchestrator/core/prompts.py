"""Operator-customizable stage prompts (backlog: prompt_template_customization).

This module sits between the Core stage driver and ``AgentRunRequest.prompt``:

    Core stage -> PromptTemplateStore -> render_prompt -> AgentRunRequest.prompt -> Provider adapter

The :class:`PromptTemplateStore` loads the packaged default for each agent-routed stage and, when a
``<stage>.md`` is present in ``prompts.templates_dir``, the operator's template from that file —
combined per ``prompts.mode``. :func:`render_prompt` then substitutes an **allowlisted** set of
metadata/artifact *path* variables (never task bodies, diffs, check logs, env, or secrets — those
stay in the artifact files the provider references by path, §6).

Invariants this module preserves: it produces only stdin prompt *text*. It never touches provider
argv, CLI syntax, the sandbox/approvals, denied commands/reads, the env allowlist, or fallback
policy. Operator templates cannot weaken any of those (backlog §6).
"""

from __future__ import annotations

import logging
import re
from importlib import resources
from pathlib import Path

from wastech_orchestrator.config.schema import (
    ROUTABLE_STAGES,
    PromptMode,
    PromptsConfig,
)
from wastech_orchestrator.providers.base import Stage

_LOG = logging.getLogger(__name__)

# The only names a template may interpolate (backlog §5). Everything is metadata or an artifact
# *path*; large content is never injected. An unknown ``{name}`` is left verbatim, so a template
# carrying code/JSON braces renders unchanged.
ALLOWED_PROMPT_VARS: frozenset[str] = frozenset(
    {
        "task_id",
        "stage",
        "repo_path",
        "repo",  # flow-engine alias for ``repo_path`` (flow-contract §6); single shared allowlist
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


def _packaged_default(stage: Stage) -> str:
    """Read the packaged default template for *stage* (``templates/prompts/<stage>.md``)."""
    resource = resources.files("wastech_orchestrator").joinpath(
        "templates", "prompts", f"{stage.value}.md"
    )
    return resource.read_text(encoding="utf-8").strip()


class PromptTemplateStore:
    """Resolve the per-stage prompt template from packaged defaults + operator template files.

    Built once at orchestrator startup. A ``<stage>.md`` present in ``templates_dir`` is the
    **activation signal** (no opt-in map): if it reads, it is the operator's template for that
    stage; otherwise the packaged default is used as a per-stage **fallback**. An empty
    ``templates_dir`` forces the packaged defaults for every stage. A missing file is never an
    error — there is no fail-closed-on-missing path. The packaged defaults always load (they ship
    with the wheel).
    """

    def __init__(self, config: PromptsConfig, *, logger: logging.Logger | None = None) -> None:
        self._mode = config.mode
        log = logger or _LOG
        self._defaults: dict[Stage, str] = {
            stage: _packaged_default(stage) for stage in ROUTABLE_STAGES
        }
        self._overrides: dict[Stage, str] = {}
        if not config.templates_dir:
            return  # explicit opt-out: force the packaged defaults for every stage
        base = Path(config.templates_dir)
        for stage in ROUTABLE_STAGES:
            path = base / f"{stage.value}.md"
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue  # no file for this stage → packaged-default fallback (the normal case)
            if text:
                self._overrides[stage] = text
            else:
                log.warning(
                    "prompt template file is empty; using packaged default",
                    extra={"stage": stage.value, "path": str(path)},
                )

    def resolved(self, stage: Stage) -> str:
        """The combined template text for *stage*, before variable substitution.

        No template file → the packaged default. ``replace`` → the file only. ``append`` → the
        packaged default, then the file (deterministic order), separated by a blank line.
        """
        default = self._defaults[stage]
        override = self._overrides.get(stage)
        if override is None:
            return default
        if self._mode is PromptMode.REPLACE:
            return override
        return f"{default}\n\n{override}"

    def override_for(self, stage: Stage) -> str | None:
        """The operator's template text for *stage* (the user's own guidance), or ``None``.

        Used by the skill dedup to compare the operator's planning instructions with the chosen
        skill bodies. This is the scanned ``<stage>.md`` content, never the packaged default.
        """
        return self._overrides.get(stage)
