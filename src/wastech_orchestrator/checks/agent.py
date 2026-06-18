"""Agent-assisted read-only check discovery (automatic check discovery §6).

When deterministic detection cannot produce a launchable test command, a single bounded, read-only
provider run may *propose* structured candidates. It is advisory only: it cannot mark a check
passing, cannot execute anything, and its output is fail-closed validated and then fed through the
same deterministic validator + prober as detected candidates. It runs as a pre-task resolver step
(install time), never inside the state machine, and uses a deliberately cheap model + low reasoning
(``checks.discovery.{model,reasoning,timeout_seconds}``). Provider-agnostic: it speaks only the
``AgentProvider`` protocol.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from wastech_orchestrator.checks.inspect import RepositoryEvidence
from wastech_orchestrator.checks.model import CheckCandidate, CheckSource, Confidence
from wastech_orchestrator.checks.schema_validate import DiscoveryDoc, validate_discovery_output
from wastech_orchestrator.config.schema import CheckDiscoveryConfig
from wastech_orchestrator.providers.base import (
    AgentProvider,
    AgentRunRequest,
    ProviderError,
    RunStatus,
    Stage,
)

# The strict structured-output schema the provider must satisfy (doc §6). Mirrored by
# ``schema_validate.validate_discovery_output`` (we validate in the consumer, not the adapter).
DISCOVERY_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["checks"],
    "properties": {
        "checks": {
            "type": "array",
            "minItems": 1,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "argv", "confidence"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 64},
                    "argv": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {"type": "string", "minLength": 1, "maxLength": 256},
                    },
                    "evidence": {"type": "array", "items": {"type": "string", "maxLength": 256}},
                    "confidence": {"enum": ["low", "medium", "high"]},
                },
            },
        },
    },
}

_INSTRUCTIONS = (
    "Resolve this repository's quality-gate commands. Return ONLY structured JSON matching the "
    "schema: an object with `checks`, a list of {name, argv, evidence, confidence}. Every `argv` "
    "is an executable plus arguments as a list (NEVER a shell string, no pipes/redirects). Prefer "
    "repository-local interpreters (e.g. .venv/bin/python) over bare commands. Do not include "
    "dependency-install/setup commands — they are not checks. Do not invent commands without "
    "evidence in the facts below. Machine config (pyproject `[tool.*]`, package.json scripts, "
    "Makefile/Justfile targets, lock files) takes precedence over prose in CLAUDE.md/AGENTS.md; "
    "use prose only to break ties.\n\n"
    "Repository facts:\n"
)

_CONFIDENCE = {"low": Confidence.LOW, "medium": Confidence.MEDIUM, "high": Confidence.HIGH}


class AgentCheckDiscovery:
    """A read-only, advisory provider run that proposes check candidates."""

    def __init__(
        self,
        provider: AgentProvider,
        *,
        discovery_cfg: CheckDiscoveryConfig,
        artifacts_root: str | Path,
        validate: Callable[[Any], DiscoveryDoc | None] = validate_discovery_output,
    ) -> None:
        self._provider = provider
        self._cfg = discovery_cfg
        self._artifacts_root = str(artifacts_root)
        self._validate = validate

    def discover(
        self, repo_root: str | Path, evidence: RepositoryEvidence
    ) -> tuple[CheckCandidate, ...]:
        """Run one bounded read-only discovery call; return validated candidates (else ``()``)."""
        request = AgentRunRequest(
            task_id="check-discovery",
            stage=Stage.PLANNING,  # a label only — this runs outside the state machine
            working_directory=str(repo_root),
            prompt=_INSTRUCTIONS + _evidence_facts(evidence),
            permission_profile="read-only",
            timeout_seconds=self._cfg.timeout_seconds,
            attempt=1,
            node_run_id=0,
            output_schema=DISCOVERY_OUTPUT_SCHEMA,
            model=self._cfg.model or None,
            reasoning=self._cfg.reasoning,
        )
        try:
            result = self._provider.run(request)
        except ProviderError:
            return ()  # advisory + bounded: an infra failure is not fatal, deterministic proceeds
        if result.status is not RunStatus.SUCCEEDED:
            return ()
        doc = self._validate(result.structured_output)
        if doc is None:
            return ()
        return tuple(
            CheckCandidate(
                name=proposal.name,
                argv=proposal.argv,
                source=CheckSource.AGENT,
                evidence=proposal.evidence,
                confidence=_CONFIDENCE.get(proposal.confidence, Confidence.MEDIUM),
            )
            for proposal in doc.checks
        )


def _evidence_facts(evidence: RepositoryEvidence) -> str:
    """Render bounded, secret-free structural facts for the prompt (never contents/env values)."""
    facts: list[str] = []
    if evidence.files_present:
        facts.append(f"manifest/lock files: {', '.join(sorted(evidence.files_present))}")
    if evidence.python_tools:
        facts.append(f"python tools declared: {', '.join(sorted(evidence.python_tools))}")
    if evidence.node_scripts:
        facts.append(f"package.json scripts: {', '.join(sorted(evidence.node_scripts))}")
    for label, values in (
        ("make targets", evidence.make_targets),
        ("just recipes", evidence.just_recipes),
        ("task targets", evidence.task_targets),
    ):
        if values:
            facts.append(f"{label}: {', '.join(sorted(values))}")
    for venv in evidence.venvs:
        tools = ", ".join(sorted(venv.tools)) or "none"
        facts.append(f"virtualenv {venv.python} (tools: {tools})")
    if evidence.ci_workflows:
        facts.append(f"CI workflow files: {', '.join(evidence.ci_workflows)}")
    if evidence.instruction_docs:
        facts.append(f"instruction docs: {', '.join(evidence.instruction_docs)}")
    return "\n".join(f"- {fact}" for fact in facts) or "- (no recognizable build/test markers)"
