"""Operator-customizable stage prompts (backlog: prompt_template_customization).

This module sits between the Core stage driver and ``AgentRunRequest.prompt``:

    Core stage -> PromptTemplateStore -> render_prompt -> AgentRunRequest.prompt -> Provider adapter

The :class:`PromptTemplateStore` loads the packaged default for each agent-routed stage and, when
the operator configured one, the override template from ``prompts.templates_dir`` — combined per
``prompts.mode``. :func:`render_prompt` then substitutes an **allowlisted** set of metadata/artifact
*path* variables (never task bodies, diffs, check logs, env, or secrets — those stay in the artifact
files the provider references by path, §6).

Invariants this module preserves: it produces only stdin prompt *text*. It never touches provider
argv, CLI syntax, the sandbox/approvals, denied commands/reads, the env allowlist, or fallback
policy. Operator templates cannot weaken any of those (backlog §6).
"""

from __future__ import annotations

import logging
import re
from importlib import resources
from pathlib import Path

from wastech_orchestrator.config.loader import ConfigError
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
        "task_path",
        "plan_path",
        "diff_path",
        "checks_path",
        "review_path",
        "subtask_order",
        "subtask_count",
        "subtask_spec_path",
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
    """Resolve the per-stage prompt template from packaged defaults + operator overrides.

    Built once at orchestrator startup. In ``strict`` mode a missing or unreadable override file is
    a fail-closed :class:`ConfigError` (raised before any agent runs); otherwise a warning is logged
    and the packaged default is used. The packaged defaults always load — they ship with the wheel.
    """

    def __init__(self, config: PromptsConfig, *, logger: logging.Logger | None = None) -> None:
        self._mode = config.mode
        log = logger or _LOG
        self._defaults: dict[Stage, str] = {
            stage: _packaged_default(stage) for stage in ROUTABLE_STAGES
        }
        self._overrides: dict[Stage, str] = {}
        base = Path(config.templates_dir)
        for stage, filename in config.overrides:
            path = base / filename
            try:
                self._overrides[stage] = path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                if config.strict:
                    raise ConfigError(
                        [
                            f"prompts.overrides.{stage.value}: cannot read override template "
                            f"{str(path)!r} ({exc.strerror or exc}); strict mode requires it"
                        ]
                    ) from exc
                log.warning(
                    "prompt override unreadable; using packaged default",
                    extra={
                        "stage": stage.value,
                        "path": str(path),
                        "error": exc.strerror or str(exc),
                    },
                )

    def resolved(self, stage: Stage) -> str:
        """The combined template text for *stage*, before variable substitution.

        No override → the packaged default. ``replace`` → the override only. ``append`` → the
        packaged default, then the override (deterministic order), separated by a blank line.
        """
        default = self._defaults[stage]
        override = self._overrides.get(stage)
        if override is None:
            return default
        if self._mode is PromptMode.REPLACE:
            return override
        return f"{default}\n\n{override}"
