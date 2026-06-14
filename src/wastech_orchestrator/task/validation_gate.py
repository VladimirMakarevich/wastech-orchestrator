"""Validation gate (spec §19).

Runs on ``new -> validated``, **before** the processing slot is acquired and before any
branch/provider. Two phases:

* **Phase A — structural, hard reject** (§19.2): deterministic, no agent. Each failure maps to a
  machine-readable :class:`ValidationReason`; the **first** failure short-circuits. A Phase-A
  failure is terminal ``failed``: the task file moves ``processing/ -> tasks/rejected/`` and the
  only artifact written is ``validation_report.json`` — **no branch is ever created**.
* **Phase B — semantic completeness** (§19.1): never rejects. Classifies ``complete`` vs.
  ``needs_enrichment`` to feed the deterministic refinement-skip decision (§5). Missing acceptance
  criteria/constraints is **not** a reject.

Task content reaches providers only as file paths (§19.5), so a task field can never become a CLI
flag — the front-matter injection scan here is belt-and-braces on top of that structural guarantee.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import (
    ROUTABLE_STAGES,
    SKIPPABLE_STAGES,
    OrchestratorConfig,
)
from wastech_orchestrator.config.validation import check_task_route_override
from wastech_orchestrator.providers.artifacts import task_artifact_dir
from wastech_orchestrator.providers.base import ProviderId, Stage
from wastech_orchestrator.security.injection import scan_frontmatter
from wastech_orchestrator.task.model import (
    ALLOWED_TASK_KEYS,
    NormalizedTask,
    StageParams,
    is_valid_task_id,
)
from wastech_orchestrator.task.parser import (
    ParsedSource,
    extract_section,
    split_frontmatter,
)

VALIDATION_REPORT_FILENAME = "validation_report.json"

_REASONING_LEVELS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh", "max"})

# Characters allowed alongside printable text (everything else counts as a control char, §19.2).
_ALLOWED_CONTROL = {"\t", "\n", "\r"}


class ValidationReason(StrEnum):
    """The Phase-A structural reject reasons (§19.2). Canonical machine-readable strings."""

    FILE_TOO_LARGE = "file_too_large"
    NOT_UTF8 = "not_utf8"
    BINARY_OR_CONTROL_CHARS = "binary_or_control_chars"
    TOO_LONG = "too_long"
    FRONTMATTER_MISSING = "frontmatter_missing"
    FRONTMATTER_MALFORMED = "frontmatter_malformed"
    UNKNOWN_TOP_LEVEL_FIELD = "unknown_top_level_field"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    INVALID_FIELD_TYPE = "invalid_field_type"
    INVALID_TASK_ID = "invalid_task_id"
    DUPLICATE_TASK_ID = "duplicate_task_id"
    INVALID_ROUTE_OVERRIDE = "invalid_route_override"
    INVALID_STAGE_OVERRIDE = "invalid_stage_override"
    REVIEW_SKIP_NOT_ALLOWED = "review_skip_not_allowed"
    INJECTION_SUSPECTED = "injection_suspected"


class Completeness(StrEnum):
    """Phase-B classification feeding the refinement-skip decision (§5, §19.1)."""

    COMPLETE = "complete"
    NEEDS_ENRICHMENT = "needs_enrichment"


@dataclass(frozen=True)
class ValidationResult:
    """The gate outcome for one task."""

    passed: bool
    reason: ValidationReason | None = None
    detail: str = ""
    normalized: NormalizedTask | None = None
    completeness: Completeness | None = None


@dataclass(frozen=True)
class _Reject:
    reason: ValidationReason
    detail: str


# Map a front-matter stage key to the canonical Stage (only the agent-routed stages are valid in a
# task override). ``implementation`` is the spec's key (not ``implementing``, which is a status).
_STAGE_BY_KEY = {stage.value: stage for stage in Stage}


class ValidationGate:
    """Applies the §19 gate. Dependencies are injected so the gate stays pure and testable."""

    def __init__(
        self,
        config: OrchestratorConfig,
        *,
        store_has_task_id: Callable[[str], bool],
        ledger_has_task_id: Callable[[str], bool],
        is_recovery_rerun: Callable[[str], bool] = lambda _id: False,
    ) -> None:
        self._config = config
        self._store_has_task_id = store_has_task_id
        self._ledger_has_task_id = ledger_has_task_id
        self._is_recovery_rerun = is_recovery_rerun

    def validate(self, source: ParsedSource) -> ValidationResult:
        """Run Phase A then Phase B and return the combined :class:`ValidationResult`."""
        reject, task = self._phase_a(source)
        if reject is not None:
            return ValidationResult(passed=False, reason=reject.reason, detail=reject.detail)
        assert task is not None  # phase_a returns a task whenever it does not reject
        completeness = self.phase_b(task)
        return ValidationResult(passed=True, normalized=task, completeness=completeness)

    # --- Phase A --------------------------------------------------------------------------

    def _phase_a(self, source: ParsedSource) -> tuple[_Reject | None, NormalizedTask | None]:
        v = self._config.validation

        raw = source.raw_bytes
        if len(raw) > v.max_task_bytes:
            return _rej(ValidationReason.FILE_TOO_LARGE, f"{len(raw)} > {v.max_task_bytes} bytes")
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            return _rej(ValidationReason.NOT_UTF8, str(exc))

        control_reject = self._check_control_chars(text, v.max_control_ratio)
        if control_reject is not None:
            return control_reject, None

        length_reject = self._check_length(text, v.max_task_lines, v.max_line_bytes)
        if length_reject is not None:
            return length_reject, None

        parse = split_frontmatter(text, source.suffix)
        if not parse.present:
            return _rej(ValidationReason.FRONTMATTER_MISSING, "no front matter")
        if parse.malformed:
            return _rej(ValidationReason.FRONTMATTER_MALFORMED, parse.detail)

        return self._validate_fields(parse.frontmatter, parse.body)

    def _check_control_chars(self, text: str, max_ratio: float) -> _Reject | None:
        control = 0
        for ch in text:
            if ch == "\x00":
                return _Reject(ValidationReason.BINARY_OR_CONTROL_CHARS, "NUL byte")
            if ch in _ALLOWED_CONTROL:
                continue
            if ord(ch) < 0x20 or ord(ch) == 0x7F:
                control += 1
        if text and control / len(text) > max_ratio:
            return _Reject(
                ValidationReason.BINARY_OR_CONTROL_CHARS,
                f"control-char ratio {control / len(text):.4f} > {max_ratio}",
            )
        return None

    def _check_length(self, text: str, max_lines: int, max_line_bytes: int) -> _Reject | None:
        lines = text.splitlines()
        if len(lines) > max_lines:
            return _Reject(ValidationReason.TOO_LONG, f"{len(lines)} lines > {max_lines}")
        for line in lines:
            if len(line.encode("utf-8")) > max_line_bytes:
                return _Reject(
                    ValidationReason.TOO_LONG,
                    f"line of {len(line.encode('utf-8'))} bytes > {max_line_bytes}",
                )
        return None

    def _validate_fields(
        self, frontmatter: Mapping[str, Any], body: str
    ) -> tuple[_Reject | None, NormalizedTask | None]:
        # unknown_top_level_field — fail-closed (§19.3).
        for key in frontmatter:
            if key not in ALLOWED_TASK_KEYS:
                return _rej(ValidationReason.UNKNOWN_TOP_LEVEL_FIELD, f"key {key!r}")

        # The Description *section* must be non-empty (§19.3); the full body becomes the agent
        # context (and feeds the Phase-B acceptance-criteria scan).
        description_section = self._extract_description(body)

        # missing_required_field — id/title present, body Description non-empty (§19.3).
        if "id" not in frontmatter:
            return _rej(ValidationReason.MISSING_REQUIRED_FIELD, "id")
        title_value = frontmatter.get("title")
        if "title" not in frontmatter or (isinstance(title_value, str) and not title_value.strip()):
            return _rej(ValidationReason.MISSING_REQUIRED_FIELD, "title")
        if not description_section.strip():
            return _rej(ValidationReason.MISSING_REQUIRED_FIELD, "description")

        # invalid_field_type (§19.2).
        type_reject = self._check_field_types(frontmatter)
        if type_reject is not None:
            return type_reject, None

        # invalid_task_id (§19.3).
        id_value = frontmatter["id"]
        if not isinstance(id_value, str) or not is_valid_task_id(id_value):
            return _rej(ValidationReason.INVALID_TASK_ID, repr(id_value))

        # duplicate_task_id — vs. the tasks table + the ledger, exempting a recovery re-run (§13).
        if not self._is_recovery_rerun(id_value) and (
            self._store_has_task_id(id_value) or self._ledger_has_task_id(id_value)
        ):
            return _rej(ValidationReason.DUPLICATE_TASK_ID, id_value)

        # invalid_route_override — map the agents override and validate against the config.
        agents, route_reject = self._build_agents(frontmatter.get("agents"))
        if route_reject is not None:
            return route_reject, None

        # invalid_stage_override — map the per-stage model/reasoning override (agent-routed stages).
        stage_params, stage_reject = self._build_stage_params(frontmatter.get("stages"))
        if stage_reject is not None:
            return stage_reject, None

        # injection_suspected — argv-shaped tokens in front-matter values (§19.5). The scanner is
        # belt-and-braces over the file-path-only structural guarantee (see security/injection.py).
        finding = scan_frontmatter(frontmatter)
        if finding is not None:
            return _rej(ValidationReason.INJECTION_SUSPECTED, finding.detail)

        raw_pr_title = frontmatter.get("pr_title")
        pr_title = (str(raw_pr_title).strip() or None) if isinstance(raw_pr_title, str) else None
        task = NormalizedTask(
            id=id_value,
            title=str(title_value),
            description=body.strip(),
            pr_title=pr_title,
            refined=bool(frontmatter.get("refined", False)),
            decompose=_as_tristate(frontmatter.get("decompose")),
            auto_merge=_as_tristate(frontmatter.get("auto_merge")),
            agents=agents,
            contacts=[str(c) for c in frontmatter.get("contacts", [])],
            model=frontmatter.get("model") or None,
            reasoning=frontmatter.get("reasoning") or None,
            stage_params=stage_params,
        )
        return None, task

    def _extract_description(self, body: str) -> str:
        """The body's ``## Description`` section, or the whole body when there are no sections."""
        section = extract_section(body, "Description")
        if section is not None:
            return section
        return body

    def _check_field_types(self, fm: Mapping[str, Any]) -> _Reject | None:
        if not isinstance(fm.get("title"), str):
            return _Reject(ValidationReason.INVALID_FIELD_TYPE, "title must be a string")
        if "pr_title" in fm and fm["pr_title"] is not None and not isinstance(fm["pr_title"], str):
            return _Reject(ValidationReason.INVALID_FIELD_TYPE, "pr_title must be a string")
        if "refined" in fm and not isinstance(fm["refined"], bool):
            return _Reject(ValidationReason.INVALID_FIELD_TYPE, "refined must be a boolean")
        if (
            "decompose" in fm
            and fm["decompose"] is not None
            and not isinstance(fm["decompose"], bool)
        ):
            return _Reject(ValidationReason.INVALID_FIELD_TYPE, "decompose must be a boolean")
        if (
            "auto_merge" in fm
            and fm["auto_merge"] is not None
            and not isinstance(fm["auto_merge"], bool)
        ):
            return _Reject(ValidationReason.INVALID_FIELD_TYPE, "auto_merge must be a boolean")
        if "agents" in fm and not isinstance(fm["agents"], Mapping):
            return _Reject(ValidationReason.INVALID_FIELD_TYPE, "agents must be a mapping")
        if "contacts" in fm:
            contacts = fm["contacts"]
            if not isinstance(contacts, Sequence) or isinstance(contacts, str | bytes):
                return _Reject(ValidationReason.INVALID_FIELD_TYPE, "contacts must be a list")
            if not all(isinstance(c, str) for c in contacts):
                return _Reject(
                    ValidationReason.INVALID_FIELD_TYPE, "contacts must be a list of strings"
                )
        return _validate_model_reasoning(
            fm.get("model"), fm.get("reasoning"), reason=ValidationReason.INVALID_FIELD_TYPE
        )

    def _build_agents(self, raw: Any) -> tuple[dict[Stage, ProviderId], _Reject | None]:
        if raw is None:
            return {}, None
        agents: dict[Stage, ProviderId] = {}
        for key, value in raw.items():
            stage = _STAGE_BY_KEY.get(str(key))
            if stage is None:
                return {}, _Reject(
                    ValidationReason.INVALID_ROUTE_OVERRIDE, f"unknown stage {key!r}"
                )
            try:
                provider = ProviderId(str(value))
            except ValueError:
                return {}, _Reject(
                    ValidationReason.INVALID_ROUTE_OVERRIDE, f"unknown provider {value!r}"
                )
            agents[stage] = provider
        # Reuse the config-time override validator for allowed/configured/agent-routed checks.
        issues = check_task_route_override(agents, self._config)
        if issues:
            return {}, _Reject(ValidationReason.INVALID_ROUTE_OVERRIDE, "; ".join(issues))
        return agents, None

    def _build_stage_params(self, raw: Any) -> tuple[dict[Stage, StageParams], _Reject | None]:
        """Map the ``stages`` front-matter block to ``{Stage: StageParams}`` (fail-closed).

        Each key is a stage; the valid sub-keys depend on the stage:

        * ``model`` / ``reasoning``: only for agent-routed stages (``ROUTABLE_STAGES``). The
          ``testing`` stage runs no agent, so a model/reasoning there is rejected.
        * ``enabled``: only for skippable stages (``SKIPPABLE_STAGES``); ``enabled: false`` skips
          it. ``implementation``/``refinement`` cannot be disabled here (refinement uses
          ``refined``) and ``publishing`` is not a valid key at all.

        A value of ``null``/``{}`` means "inherit / default". Unknown stages, unknown sub-keys, a
        sub-key not valid for that stage, and non-mapping values all reject with
        ``INVALID_STAGE_OVERRIDE``. Disabling ``review`` additionally requires
        ``agents.allow_review_skip`` (else ``REVIEW_SKIP_NOT_ALLOWED``).
        """
        if raw is None:
            return {}, None
        if not isinstance(raw, Mapping):
            return {}, _Reject(ValidationReason.INVALID_STAGE_OVERRIDE, "stages must be a mapping")
        stage_params: dict[Stage, StageParams] = {}
        for key, value in raw.items():
            stage = _STAGE_BY_KEY.get(str(key))
            if stage is None or stage not in (ROUTABLE_STAGES | SKIPPABLE_STAGES):
                return {}, _Reject(
                    ValidationReason.INVALID_STAGE_OVERRIDE, f"unknown stage {key!r}"
                )
            if value is None:
                stage_params[stage] = StageParams()
                continue
            if not isinstance(value, Mapping):
                return {}, _Reject(
                    ValidationReason.INVALID_STAGE_OVERRIDE,
                    f"stages.{stage.value} must be a mapping",
                )
            allowed_keys: set[str] = set()
            if stage in ROUTABLE_STAGES:
                allowed_keys |= {"model", "reasoning"}
            if stage in SKIPPABLE_STAGES:
                allowed_keys |= {"enabled"}
            unknown = {str(k) for k in value} - allowed_keys
            if unknown:
                return {}, _Reject(
                    ValidationReason.INVALID_STAGE_OVERRIDE,
                    f"stages.{stage.value}: unknown keys {sorted(unknown)}",
                )
            reject = _validate_model_reasoning(
                value.get("model"),
                value.get("reasoning"),
                reason=ValidationReason.INVALID_STAGE_OVERRIDE,
                prefix=f"stages.{stage.value} ",
            )
            if reject is not None:
                return {}, reject
            enabled = value.get("enabled")
            if enabled is not None and not isinstance(enabled, bool):
                return {}, _Reject(
                    ValidationReason.INVALID_STAGE_OVERRIDE,
                    f"stages.{stage.value} enabled must be a boolean",
                )
            if (
                stage is Stage.REVIEW
                and enabled is False
                and not self._config.agents.allow_review_skip
            ):
                return {}, _Reject(
                    ValidationReason.REVIEW_SKIP_NOT_ALLOWED,
                    "stages.review.enabled: false requires agents.allow_review_skip: true",
                )
            stage_params[stage] = StageParams(
                model=value.get("model") or None,
                reasoning=value.get("reasoning") or None,
                enabled=enabled,
            )
        return stage_params, None

    # --- Phase B --------------------------------------------------------------------------

    def phase_b(self, task: NormalizedTask) -> Completeness:
        """Classify ``complete`` vs ``needs_enrichment`` (never rejects, §19.1).

        Complete when the task is flagged ``refined: true`` or it carries both a description and
        acceptance criteria. Anything less is ``needs_enrichment`` — ``refinement`` will run (§5).
        """
        if task.refined:
            return Completeness.COMPLETE
        has_description = bool(task.description.strip())
        has_acceptance = extract_section(task.description, "Acceptance criteria") is not None or (
            "acceptance" in task.description.lower()
        )
        if has_description and has_acceptance:
            return Completeness.COMPLETE
        return Completeness.NEEDS_ENRICHMENT


def _rej(reason: ValidationReason, detail: str) -> tuple[_Reject, None]:
    return _Reject(reason=reason, detail=detail), None


def _as_tristate(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _validate_model_reasoning(
    model: Any, reasoning: Any, *, reason: ValidationReason, prefix: str = ""
) -> _Reject | None:
    """Validate a ``(model, reasoning)`` pair — shared by the top-level fields and each stage block.

    ``model`` must be a string or null; ``reasoning`` must be null or one of ``_REASONING_LEVELS``.
    ``None`` for either is "unset" and skips its check. ``reason`` selects the reject code
    (``INVALID_FIELD_TYPE`` for the top-level pair, ``INVALID_STAGE_OVERRIDE`` per stage) and
    ``prefix`` frames the detail (e.g. ``"stages.planning "``) while keeping the ``model``/
    ``reasoning`` substrings intact.
    """
    if model is not None and not isinstance(model, str):
        return _Reject(reason, f"{prefix}model must be a string or null")
    if reasoning is not None:
        if not isinstance(reasoning, str):
            return _Reject(reason, f"{prefix}reasoning must be a string or null")
        if reasoning not in _REASONING_LEVELS:
            return _Reject(reason, f"{prefix}reasoning must be one of {sorted(_REASONING_LEVELS)}")
    return None


def write_validation_report(
    result: ValidationResult, task_id: str, artifacts_root: str | Path
) -> str:
    """Write ``validation_report.json`` under ``logs/<task-id>/``; return its path (§10, §19.4)."""
    task_dir = task_artifact_dir(artifacts_root, task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / VALIDATION_REPORT_FILENAME
    data = {
        "task_id": task_id,
        "passed": result.passed,
        "reason": result.reason.value if result.reason else None,
        "detail": result.detail,
        "completeness": result.completeness.value if result.completeness else None,
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return str(path)
