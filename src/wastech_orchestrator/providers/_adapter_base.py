"""Shared CLI-adapter infrastructure for the Codex/Claude providers.

The two provider adapters (:mod:`wastech_orchestrator.providers.claude` /
:mod:`wastech_orchestrator.providers.codex`) have a byte-identical infrastructure spine — the
attempt-directory lifecycle, the redact-every-sink discipline, the durable-session scrubbing, the
request/result artifact shapes, and the env-secret harvesting. This module owns that spine so the
two adapters carry only what genuinely differs: the argv they build, their stderr-signature table,
and how they parse their own event stream.

**This module deliberately knows no CLI syntax.** It never names a flag, subcommand, or sandbox
value — that is the inviolable boundary the per-provider subclasses sit on the other side of.
A subclass supplies the syntax through the four hooks (``_build_argv`` /
``_parse`` / ``_signatures`` / ``_executable_label``); the base supplies the rest.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig, SecurityConfig
from wastech_orchestrator.observability.logging import bind
from wastech_orchestrator.observability.progress import run_with_heartbeat
from wastech_orchestrator.providers.artifacts import (
    ArtifactPaths,
    create_attempt_dir,
    prune_attempt_artifacts,
    write_request_artifact,
    write_result_artifact,
)
from wastech_orchestrator.providers.base import (
    MAX_TURNS_SUBTYPE,
    AgentRunRequest,
    AgentRunResult,
    AuthProbe,
    ErrorClass,
    NormalizedError,
    NormalizedUsage,
    ProviderError,
    ProviderHealth,
    RunStatus,
    build_effective_prompt,
)
from wastech_orchestrator.providers.errors import StderrSignature, classify, message_for
from wastech_orchestrator.providers.process import (
    AgentHandleRecorder,
    ProcessResult,
    run_process,
)
from wastech_orchestrator.providers.redaction import (
    REDACTED,
    normalized_session_id,
    read_denied_secrets,
    redact_jsonl,
    redact_mapping,
    redact_text,
    secret_env_values,
)
from wastech_orchestrator.runtime_layout import InternalDenyPolicy
from wastech_orchestrator.security.env import build_child_env

_PREFLIGHT_TIMEOUT_SECONDS = 10
_LOG = logging.getLogger(__name__)

# The injected process-runner seam (defaults to the real one).
RunProcess = Callable[..., ProcessResult]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_instant(epoch_seconds: float | None) -> str | None:
    """A provider-reported Unix instant as an ISO-8601 UTC string, else ``None``.

    The single point where a CLI's epoch becomes the orchestrator's wall-clock spelling, so nothing
    downstream does timezone arithmetic and the carried field stays provider-neutral. A value the
    platform cannot represent as a datetime yields ``None``: a missing wake instant costs one blind
    retry, while a wrong one would defer a task nobody is waiting on.
    """
    if epoch_seconds is None:
        return None
    try:
        return datetime.fromtimestamp(epoch_seconds, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


@dataclass(frozen=True)
class ParsedEvents:
    """The fields a provider extracts from its CLI's event stream."""

    final_message: str | None
    structured_output: dict[str, Any] | None
    usage: dict[str, Any] | None
    session_id: str | None
    succeeded: bool
    # The provider-neutral view of ``usage`` (cumulative for Codex, per-invocation for Claude),
    # ``None`` when the CLI emitted no usage. Derived from the same resolved ``usage`` dict, so the
    # two always describe the same numbers.
    normalized_usage: NormalizedUsage | None = None
    # The CLI's terminal ``result`` subtype when the run did not succeed (e.g. ``error_max_turns``),
    # else ``None``. Lets the adapter classify a parseable terminal failure as a task outcome (never
    # a crash) and surface a precise message.
    failure_subtype: str | None = None
    # True when the terminal event reports a subscription/usage rate-limit (HTTP 429 / a
    # ``rate_limit_event`` / a "session limit … resets" banner). Such a terminal is NOT a quality
    # ``task_failure`` but a transient infra event: the finalize step RAISES ``RATE_LIMITED`` (so
    # the Router falls over / the orchestrator parks) instead of returning a quality failure.
    rate_limited: bool = False
    # The Unix instant the provider said its limit window reopens, when the terminal event carried
    # one. Epoch here and ISO on the raised error: each adapter owns its own CLI's spelling, and
    # exactly one place converts. ``None`` when the CLI reports no reset instant at all.
    rate_limit_resets_at: float | None = None


@dataclass(frozen=True)
class IsolationCapabilityReport:
    """A provider's live, no-model isolation capability-probe verdict for ``worc preflight``.

    ``ok`` is pass/fail; ``status`` a short machine label (e.g. ``passed``/``unsupported``/
    ``policy-failed``); ``detail`` a secret-free operator line. ``fatal`` marks a result that must
    fail preflight regardless of a fallback provider — a proven policy leak is a non-fallback
    security result — versus an advisory host-capability gap that degrades like a missing capability
    (fatal only when the provider has no fallback).
    """

    ok: bool
    status: str
    detail: str
    fatal: bool


def coerce_usage_int(value: object) -> int | None:
    """A plain ``int`` from a raw usage value, or ``None`` for an absent / non-integer / bool value.

    Shared by the provider adapters when mapping their raw ``usage`` payloads to
    :class:`NormalizedUsage`; ``bool`` is rejected because ``isinstance(True, int)`` is true.
    """
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def coerce_usage_cost(value: object) -> float | None:
    """A ``float`` USD cost from a raw value, or ``None`` for an absent / non-numeric / bool value.

    Shared by the adapters mapping a provider-reported dollar figure (e.g. Claude's stream-json
    ``total_cost_usd``) into :class:`NormalizedUsage.cost`. Accepts an ``int`` or ``float``;
    ``bool`` is rejected because ``isinstance(True, int)`` is true.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _produced_no_work(parsed: ParsedEvents, request: AgentRunRequest) -> bool:
    """EXPERIMENTAL(no-work-infra) — trial behavior; grep the tag ``no-work-infra`` to revert as one

    Conservative "the agent did no work at all" test on normalized fields only (no CLI syntax).

    Fires only when a non-success terminal produced ZERO NEW output tokens, carries no structured
    output, and is not an ``error_max_turns`` stop (which IS work — a quality outcome). On a resumed
    session the provider's ``output_total`` is cumulative and never 0, so the previous cumulative is
    subtracted (passed in via ``resume_baseline_output_tokens``) to recover the per-run figure; the
    baseline is honored only while ``session_id`` is set, so a run the router dropped to a fresh
    session (session_id cleared) correctly reads its own absolute output. Absent usage (or an absent
    output count) does NOT fire: a genuine quality ``task_failure`` must still flow on, so the net
    stays deliberately narrow — a masked real failure is worse than a missed no-work run. The caller
    gates this on ``not parsed.succeeded``.
    """
    if parsed.failure_subtype == MAX_TURNS_SUBTYPE:
        return False
    if parsed.structured_output:
        return False
    usage = parsed.normalized_usage
    if usage is None or usage.output_total is None:
        return False
    baseline = request.resume_baseline_output_tokens if request.session_id is not None else 0
    return usage.output_total - (baseline or 0) == 0


def read_text(path: str) -> str:
    candidate = Path(path)
    if not candidate.exists():
        return ""
    return candidate.read_text(encoding="utf-8", errors="replace")


def _redact_result_session(result: AgentRunResult) -> AgentRunResult:
    """A copy of ``result`` whose session id is the normalized (non-secret) form, for the artifact.

    The raw session id is kept on the in-memory result (so the orchestrator can persist it to the
    ``editing_lineage`` store, state.db only) and never written to ``result.json``.
    """
    if result.session_id is None:
        return result
    return replace(result, session_id=normalized_session_id(result.session_id))


class BaseCliProvider:
    """Provider-agnostic CLI-adapter spine implementing the ``AgentProvider`` protocol.

    A subclass binds a concrete CLI by setting :attr:`id` and implementing the four hooks below.
    Everything else — attempt-dir creation, the heartbeat-wrapped launch, the redact-every-sink
    discipline, durable-session scrubbing, and the request/result artifacts — is shared here.
    """

    id: str  # the provider id (set by each concrete subclass)

    def __init__(
        self,
        config: ProviderConfig,
        *,
        security: SecurityConfig,
        artifacts_root: str | Path,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        run_process: RunProcess = run_process,
        preflight_timeout_seconds: float = _PREFLIGHT_TIMEOUT_SECONDS,
        heartbeat_seconds: float = 30.0,
        artifact_level: str = "full",
        agent_handle_recorder: AgentHandleRecorder | None = None,
        deny_policy: InternalDenyPolicy | None = None,
    ) -> None:
        self._config = config
        self._security = security
        self._artifacts_root = Path(artifacts_root)
        # The internal read-deny set (private/control homes, secrets, provider auth
        # homes, frozen bundles) the adapter projects into its tool/OS-sandbox deny policy. On the
        # base so both adapters project the same set; ``None`` in unit harnesses that don't test it.
        self._deny_policy = deny_policy
        self._clock = clock
        self._monotonic = monotonic
        self._run_process = run_process
        # One-time preflight/probe launch ceiling. Injected so tests running under heavy parallel
        # load (`pytest -n auto`) can grant a generous budget without touching the production
        # default — a real cold-start CLI probe finishes in well under this.
        self._preflight_timeout_seconds = preflight_timeout_seconds
        self._heartbeat_seconds = heartbeat_seconds
        # Set only by the watch daemon: records the launched agent's (pid, pgid) so a hard stop can
        # reap its whole subtree. None everywhere else (one-shot CLI, tests) and never for the
        # short preflight/probe launches below — only the real agent run in ``run()`` records.
        self._agent_handle_recorder = agent_handle_recorder
        # logging.artifacts level (minimal|standard|full): which per-attempt files survive a run.
        # The constructor default is a no-op (keep everything); the real policy is the operator's
        # `logging.artifacts` (default "standard"), threaded in by build_providers.
        self._artifact_level = artifact_level

    # --- subclass hooks (the only CLI-aware seams) ---------------------------------------------

    def _executable_label(self) -> str:
        """The human-facing CLI name used in preflight messages (e.g. ``"claude"``/``"codex"``)."""
        raise NotImplementedError

    def _signatures(self) -> Sequence[StderrSignature]:
        """The provider's stderr-signature table for :func:`classify`."""
        raise NotImplementedError

    def _build_argv(self, request: AgentRunRequest, paths: ArtifactPaths) -> tuple[list[str], Any]:
        """Build the launch argv (writing any aux files); return ``(argv, parse_context)``.

        ``parse_context`` is provider-specific data threaded back into :meth:`_parse` (``None`` when
        none is needed). Raises :class:`ProviderError` to abort before launch.
        """
        raise NotImplementedError

    def _parse(
        self,
        raw_stdout: str,
        paths: ArtifactPaths,
        parse_context: Any,
        extra_secrets: tuple[str, ...],
    ) -> ParsedEvents:
        """Parse the clean-exit event stream into :class:`ParsedEvents` (raises on bad output)."""
        raise NotImplementedError

    def _representation_extras(self, request: AgentRunRequest) -> dict[str, Any]:
        """Extra provider-specific keys for the request artifact (inserted before ``argv``)."""
        return {}

    def _stdin_text(self, request: AgentRunRequest) -> str:
        """The text fed to the CLI on stdin: the Core prompt + the context-files footer.

        Both adapters use this as-is — neither injects repository instructions: the agent
        reads the repo's root instruction files itself (Codex via native ``AGENTS.md`` discovery,
        Claude via its Read tool), so stdin carries only the flow prompt + the context-file paths.
        """
        return build_effective_prompt(request)

    def _augment_child_env(self, env: dict[str, str]) -> dict[str, str]:
        """Subclass hook: adjust the allowlisted child env just before preflight/probe/run.

        The base builds the env purely from the security allowlist (:func:`build_child_env`) and
        knows no CLI syntax; a subclass may need to make its own runtime discoverable — e.g. prepend
        a package directory onto ``PATH`` so the CLI can find a sibling helper binary. It only ever
        adjusts the *value* of an already-allowlisted key; it never adds a key the allowlist omits.
        Default: return ``env`` unchanged.
        """
        return env

    def _post_success_infra_error(self, stderr_text: str) -> NormalizedError | None:
        """Subclass hook: veto a parseable terminal *success* when stderr proves it did no work.

        Some CLIs emit a clean terminal ``result`` event (exit 0) even though a fatal infrastructure
        error on stderr meant the run never actually touched the workspace (e.g. a sandbox helper
        that could not launch). The stdout parser cannot see that. A subclass returns a
        :class:`NormalizedError` (an infra class) to turn such a false success into a raised
        infrastructure failure; ``None`` (the default) trusts the parsed success.
        """
        return None

    def _pre_launch_check(
        self,
        request: AgentRunRequest,
        argv: list[str],
        env: Mapping[str, str],
        paths: ArtifactPaths,
    ) -> None:
        """Subclass hook: a deterministic, no-model check run AFTER argv is built and the request
        artifact written, but BEFORE the model process launches.

        Runs on the same augmented ``env`` the real launch uses. A subclass raises
        :class:`ProviderError` to fail closed pre-model (e.g. Codex proves its generated permission
        profile is actually enforced with a ``codex sandbox`` canary). Default: no-op, so
        no paid model call is a structural guarantee for providers that need no pre-launch proof.
        """
        return None

    def isolation_capability_smoke(self, *, home_dir: Path) -> IsolationCapabilityReport | None:
        """Subclass hook: a live, no-model isolation capability probe for ``worc preflight``.

        Default ``None`` — no live probe (the offline ``isolation_reasons`` gate already covers the
        provider). A subclass (Codex) stands up a throwaway fixture under *home_dir* — which MUST be
        a real, non-``/tmp`` path — and runs its ``codex sandbox`` capability smoke, mapping the
        outcome. Called ONLY by ``worc preflight`` (behind an explicit opt-in), never during a task
        run, so a normal run never pays for it. The returned ``detail`` must be secret-free.
        """
        return None

    # --- shared lifecycle ----------------------------------------------------------------------

    def preflight(self) -> ProviderHealth:
        """Detect the executable, parse its version, and ask the CLI about its own credentials."""
        label = self._executable_label()
        env = self._augment_child_env(build_child_env(self._security.allowed_environment))
        with tempfile.TemporaryDirectory() as scratch:
            stdout_path = str(Path(scratch) / "version.out")
            proc = self._run_process(
                [self._config.command, "--version"],
                cwd=scratch,
                env=env,
                timeout_seconds=self._preflight_timeout_seconds,
                stdout_path=stdout_path,
                monotonic=self._monotonic,
            )
            stdout_text = read_text(stdout_path)

        # A CLI that could not run makes no credential claim at all, so ``auth`` is left unset on
        # every unhealthy path below rather than being guessed in either direction.
        if proc.launch_error is not None:
            return ProviderHealth(
                provider_id=self.id,
                executable_found=False,
                version=None,
                supports_required_features=False,
                message=f"{label} executable not found",
            )
        if proc.timed_out or proc.exit_code != 0:
            return ProviderHealth(
                provider_id=self.id,
                executable_found=True,
                version=None,
                supports_required_features=False,
                message=f"{label} was found but '{label} --version' did not succeed",
            )
        version = _parse_version(stdout_text)
        capability_error = self._preflight_capability_error(env)
        if capability_error is not None:
            # This path already fails preflight, so a second reason buys nothing and probing here
            # would only spend another child-process launch.
            return ProviderHealth(
                provider_id=self.id,
                executable_found=True,
                version=version,
                supports_required_features=False,
                message=capability_error,
            )
        return ProviderHealth(
            provider_id=self.id,
            executable_found=True,
            version=version,
            supports_required_features=version is not None,
            message=f"{label} {version or 'unknown version'} available"
            f"{self._preflight_healthy_detail(env)}",
            degraded_reasons=self._preflight_degraded_reasons(env),
            auth=self._preflight_auth_state(env),
        )

    def _preflight_capability_error(self, env: Mapping[str, str]) -> str | None:
        """Subclass hook: a provider-specific capability probe run after the version check.

        Returns an operator-facing message when a *configured* CLI capability is missing — so
        preflight fails fast instead of a mid-run infra failure (e.g. a flag the installed CLI does
        not accept) — else ``None``. The base knows no CLI syntax; only the subclass implements the
        probe (via :meth:`_probe`). Default: no extra checks.
        """
        return None

    def _preflight_degraded_reasons(self, env: Mapping[str, str]) -> tuple[str, ...]:
        """Subclass hook: provider-specific degradations that depend on a fallback to be non-fatal.

        Unlike :meth:`_preflight_capability_error` (an unconditional block), these are advisory: a
        warning when a fallback provider exists, fatal only when this is the sole allowed provider.
        ``run_preflight`` applies that fallback-aware verdict — the adapter only detects (it knows
        CLI syntax; it does not know ``agents.allowed``). Default: none.
        """
        return ()

    def _preflight_auth_state(self, env: Mapping[str, str]) -> AuthProbe | None:
        """Subclass hook: report what this CLI says about its own stored credentials.

        Runs through :meth:`_probe`, so it inherits the preflight timeout and the allowlisted
        environment and never launches the model. The base knows no CLI syntax: only the subclass
        knows the verb and how to read the answer — and it must copy nothing out of that answer
        beyond the login state and the credential mechanism, because a credential answer can carry
        an account identity and everything placed on the returned record is printable.

        Default ``None``: an adapter with no such verb makes no claim rather than guessing one.
        """
        return None

    def _preflight_healthy_detail(self, env: Mapping[str, str]) -> str:
        """Subclass hook: extra detail appended to the healthy preflight message (e.g. a resolved
        runtime path a subclass wants an operator to see). Secret-free; default: empty string.
        """
        return ""

    def _probe(self, argv: list[str], env: Mapping[str, str]) -> tuple[bool, str]:
        """Run a short, read-only probe command (e.g. ``<cli> … --help``) for a capability check.

        Returns ``(clean_exit, combined_output)`` where ``combined_output`` is stdout + stderr. Used
        by :meth:`_preflight_capability_error`; bounded by the preflight timeout, never launches the
        model.
        """
        with tempfile.TemporaryDirectory() as scratch:
            out = str(Path(scratch) / "probe.out")
            proc = self._run_process(
                argv,
                cwd=scratch,
                env=env,
                timeout_seconds=self._preflight_timeout_seconds,
                stdout_path=out,
                monotonic=self._monotonic,
            )
            stdout_text = read_text(out)
        ok = proc.launch_error is None and not proc.timed_out and proc.exit_code == 0
        return ok, f"{stdout_text}\n{proc.stderr_text}"

    def run(self, request: AgentRunRequest) -> AgentRunResult:
        """Execute a single stage run. Infrastructure failures raise ``ProviderError``."""
        started_at = self._clock().isoformat()
        paths = create_attempt_dir(
            self._artifacts_root,
            request.task_id,
            request.node_id,
            request.attempt,
            self.id,
            node_run_id=request.node_run_id,
        )

        try:
            argv, parse_context = self._build_argv(request, paths)
        except ProviderError:
            self._write_request(paths, request, argv=None)
            raise

        self._write_request(paths, request, argv=argv)

        env = self._augment_child_env(build_child_env(self._security.allowed_environment))
        # Deterministic no-model pre-launch check on the real launch env (the Codex canary): a
        # ProviderError here fails closed BEFORE any model call. The request artifact is already
        # written; a subclass may record its own evidence under ``paths``.
        self._pre_launch_check(request, argv, env, paths)
        log = bind(
            _LOG,
            task_id=request.task_id,
            node_id=request.node_id,
            provider=self.id,
            attempt=request.attempt,
        )
        proc = run_with_heartbeat(
            lambda: self._run_process(
                argv,
                cwd=request.working_directory,
                env=env,
                timeout_seconds=request.timeout_seconds,
                stdout_path=paths.stdout_path,
                stdin_text=self._stdin_text(request),
                monotonic=self._monotonic,
                recorder=self._agent_handle_recorder,
            ),
            logger=log,
            message="provider heartbeat",
            interval_seconds=self._heartbeat_seconds,
            fields={"timeout_seconds": request.timeout_seconds},
        )
        finished_at = self._clock().isoformat()

        # Redact every captured sink before it is written: a leaked secret must never land
        # in stdout.log or events.jsonl. Parsing uses the in-memory raw stream for correctness.
        # Both stdout sinks are JSON-lines streams, so they are redacted per DECODED line
        # (``redact_jsonl``): scrubbing the serialized characters instead used to consume the
        # backslash of an escaped quote and leave a line that no longer parses, which silently cost
        # the audit trail whole tool results. stderr is plain text and stays on ``redact_text``.
        extra_secrets = self._extra_secrets(request)
        raw_stdout = read_text(paths.stdout_path)
        redacted_stdout = redact_jsonl(raw_stdout, extra_secrets=extra_secrets)
        Path(paths.stdout_path).write_text(redacted_stdout, encoding="utf-8")
        Path(paths.stderr_path).write_text(
            redact_text(proc.stderr_text, extra_secrets=extra_secrets), encoding="utf-8"
        )
        Path(paths.events_path).write_text(redacted_stdout, encoding="utf-8")

        # Quiescence barrier: before ANY output is parsed or trusted, the provider process
        # tree must be proven quiescent. If ``run_process`` could not prove the containment empty, a
        # background/detached descendant may still be writing the repo/exchange — a fail-closed
        # SECURITY condition, never a quality failure and never a fallback. Finalize the failed
        # attempt and raise the non-fallback ``CONTAINMENT_UNVERIFIED`` so the Router does not fall
        # over (the Core routes it to manual-action and the children-file handle is retained). The
        # ``detail`` is secret-free (platform + a member count + pids only).
        if proc.quiescence is not None and not proc.quiescence.proven:
            error = NormalizedError(
                ErrorClass.CONTAINMENT_UNVERIFIED,
                f"{message_for(ErrorClass.CONTAINMENT_UNVERIFIED)} ({proc.quiescence.detail})",
            )
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise ProviderError(error.error_class, error.message)

        # True infrastructure failure (launch / timeout) → no usable stream → classify + raise.
        if proc.launch_error is not None or proc.timed_out:
            error = classify(
                exit_code=proc.exit_code,
                stderr_text=proc.stderr_text,
                timed_out=proc.timed_out,
                launch_error=proc.launch_error,
                signatures=self._signatures(),
            )
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise ProviderError(error.error_class, error.message)

        # Parse the structured event stream. A CLI emits a terminal ``result`` event even when it
        # stops on an error and exits non-zero (e.g. Claude's ``error_max_turns`` exits 1): a
        # parseable terminal event is an agent OUTCOME, classified by its content below — never as a
        # crash by exit code. Only a non-zero exit with NO parseable terminal event is a genuine
        # abnormal termination → classify (stderr signature / process_crashed).
        try:
            parsed = self._parse(raw_stdout, paths, parse_context, extra_secrets)
        except ProviderError as exc:
            if proc.exit_code != 0:
                error = classify(
                    exit_code=proc.exit_code,
                    stderr_text=proc.stderr_text,
                    timed_out=proc.timed_out,
                    launch_error=proc.launch_error,
                    signatures=self._signatures(),
                )
                self._finalize_failure(paths, request, started_at, finished_at, proc, error)
                raise ProviderError(error.error_class, error.message) from exc
            error = NormalizedError(exc.error_class, str(exc))
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise

        # The raw session id lives ONLY in state.db (durable sessions). The resume id was
        # already redacted via ``extra_secrets``; scrub the freshly emitted id from the on-disk
        # streams too, while keeping the raw id on the in-memory result for the lineage store.
        if parsed.session_id:
            self._scrub_raw_session(paths, parsed.session_id)

        final_message = (
            redact_text(parsed.final_message, extra_secrets=extra_secrets)
            if parsed.final_message
            else None
        )
        usage = redact_mapping(parsed.usage, extra_secrets=extra_secrets) if parsed.usage else None
        if not parsed.succeeded and parsed.rate_limited:
            # A subscription/usage rate-limit terminal is a TRANSIENT INFRA event, not a quality
            # failure: RAISE ``RATE_LIMITED`` so the Router falls over to the other provider and, on
            # exhaustion, the orchestrator parks the task (resumable) instead of burning the queue /
            # a fix budget. Persist the failed-attempt artifact first, like the other raise paths.
            error = NormalizedError(
                ErrorClass.RATE_LIMITED,
                message_for(ErrorClass.RATE_LIMITED),
                resets_at=_iso_instant(parsed.rate_limit_resets_at),
            )
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            # Set on the raised exception too, not only on the recorded error: the Router rebuilds
            # its own normalized error from what was RAISED, so an instant living only here would be
            # dropped before the Core ever saw it.
            raise ProviderError(error.error_class, error.message, resets_at=error.resets_at)

        if not parsed.succeeded and _produced_no_work(parsed, request):
            # EXPERIMENTAL(no-work-infra) — trial block; revert this whole `if` to fall back to
            # the plain TASK_FAILURE return below if the trial is dropped.
            # The GENERIC no-work net: a parseable terminal event that did NOTHING (zero output
            # tokens, no structured output, not error_max_turns) is a no-progress INFRA failure,
            # not a quality task_failure. RAISE ``AGENT_NO_PROGRESS`` (fallback-eligible) so the
            # Router tries the other provider and, on exhaustion, the orchestrator fails the task —
            # instead of feeding a dead run into the review/fix machinery. Runs AFTER the specific
            # rate-limit signature above, so a recognized limit keeps its RATE_LIMITED (and park).
            error = NormalizedError(
                ErrorClass.AGENT_NO_PROGRESS, message_for(ErrorClass.AGENT_NO_PROGRESS)
            )
            self._finalize_failure(paths, request, started_at, finished_at, proc, error)
            raise ProviderError(error.error_class, error.message)

        if parsed.succeeded:
            infra_error = self._post_success_infra_error(proc.stderr_text)
            if infra_error is not None:
                # A parseable terminal SUCCESS whose stderr still proves a fatal infra failure (e.g.
                # a sandbox helper that could not launch): the run never did the work. RAISE the
                # infra class so the Router falls over to the other provider instead of trusting the
                # false success. Because this raises, ``outcome.result`` is None and no resumable
                # session lineage is persisted for the broken run — the next hop is a fallback, not
                # a resume of a session that did nothing.
                self._finalize_failure(paths, request, started_at, finished_at, proc, infra_error)
                raise ProviderError(infra_error.error_class, infra_error.message)

        if parsed.succeeded:
            status, error_obj = RunStatus.SUCCEEDED, None
        else:
            # A parsed-but-unsuccessful terminal event is a task failure, never a crash. Append the
            # CLI's own subtype (e.g. ``error_max_turns``) so the cause is visible, not lost.
            status = RunStatus.FAILED
            detail = message_for(ErrorClass.TASK_FAILURE)
            if parsed.failure_subtype:
                detail = f"{detail} ({parsed.failure_subtype})"
            error_obj = NormalizedError(
                ErrorClass.TASK_FAILURE, detail, failure_subtype=parsed.failure_subtype
            )

        result = AgentRunResult(
            status=status,
            provider=self.id,
            node_id=request.node_id,
            attempt=request.attempt,
            exit_code=proc.exit_code,
            started_at=started_at,
            finished_at=finished_at,
            final_message=final_message,
            structured_output=parsed.structured_output,
            usage=usage,
            normalized_usage=parsed.normalized_usage,
            session_id=parsed.session_id,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            event_log_path=paths.events_path,
            error=error_obj,
        )
        # result.json carries the normalized (non-secret) session id; the raw id is returned
        # in-memory for the orchestrator's editing_lineage store (state.db only).
        write_result_artifact(paths, _redact_result_session(result))
        prune_attempt_artifacts(paths, self._artifact_level)
        return result

    # --- shared internals ----------------------------------------------------------------------

    def _scrub_raw_session(self, paths: ArtifactPaths, raw_session_id: str) -> None:
        """Replace a raw session id with :data:`REDACTED` in the on-disk stdout/events streams.

        Word-bounded, like the literal path in ``redact_text``: an unbounded substring replace is
        the very defect that boundary exists to prevent, and here it would rewrite every occurrence
        of a short session id *inside* other words — shredding the JSON structure of the very sinks
        this method rewrites.
        """
        if not raw_session_id:
            return
        pattern = re.compile(rf"(?<!\w){re.escape(raw_session_id)}(?!\w)")
        for path in (paths.stdout_path, paths.events_path):
            existing = read_text(path)
            scrubbed = pattern.sub(REDACTED, existing)
            if scrubbed != existing:
                Path(path).write_text(scrubbed, encoding="utf-8")

    def _write_request(
        self, paths: ArtifactPaths, request: AgentRunRequest, *, argv: list[str] | None
    ) -> None:
        representation = self._request_representation(request, argv)
        redacted = redact_mapping(representation, extra_secrets=self._extra_secrets(request))
        write_request_artifact(paths, redacted)

    def _request_representation(
        self, request: AgentRunRequest, argv: list[str] | None
    ) -> dict[str, Any]:
        context_paths: dict[str, str | list[str] | None] = {
            "task_path": request.task_path,
            "plan_path": request.plan_path,
            "diff_path": request.diff_path,
            "check_artifacts_path": request.check_artifacts_path,
            "review_artifacts_path": request.review_artifacts_path,
            "human_input_path": request.human_input_path,
            "supervisor_packet_path": request.supervisor_packet_path,
            "skill_reference_paths": list(request.skill_reference_paths) or None,
        }
        representation: dict[str, Any] = {
            "provider": self.id,
            "task_id": request.task_id,
            "node_id": request.node_id,
            "node_run_id": request.node_run_id,
            "attempt": request.attempt,
            "working_directory": request.working_directory,
            "permission_profile": request.permission_profile,
            "timeout_seconds": request.timeout_seconds,
            "model": request.model or self._config.model or None,
            "prompt": build_effective_prompt(request),
            "context_paths": {k: v for k, v in context_paths.items() if v},
            "extra_args": list(request.extra_args),
            "config_extra_args": list(self._config.extra_args),
        }
        representation.update(self._representation_extras(request))
        representation["argv"] = argv
        return representation

    def _finalize_failure(
        self,
        paths: ArtifactPaths,
        request: AgentRunRequest,
        started_at: str,
        finished_at: str,
        proc: ProcessResult,
        error: NormalizedError,
    ) -> None:
        result = AgentRunResult(
            status=RunStatus.FAILED,
            provider=self.id,
            node_id=request.node_id,
            attempt=request.attempt,
            exit_code=proc.exit_code,
            started_at=started_at,
            finished_at=finished_at,
            stdout_path=paths.stdout_path,
            stderr_path=paths.stderr_path,
            event_log_path=paths.events_path,
            error=error,
        )
        write_result_artifact(paths, result)
        # ``minimal`` is strict — only result.json survives, even on failure (it records the exit
        # code + normalized error class). ``standard`` keeps stdout/stderr for debuggability.
        prune_attempt_artifacts(paths, self._artifact_level)

    def _extra_secrets(self, request: AgentRunRequest) -> tuple[str, ...]:
        """Literal secrets to redact: secret-named parent env values + denied-read file contents +
        the raw resume session id (durable sessions — it must never leave state.db, so it is
        scrubbed from the request argv / stdout / stderr / events / result)."""
        session = (request.session_id,) if request.session_id else ()
        return (
            self._secret_env_values()
            + read_denied_secrets(request.working_directory, self._security.denied_read_paths)
            + session
        )

    def _secret_env_values(self) -> tuple[str, ...]:
        """Values of non-allowlisted, secret-named parent env vars, for defensive redaction."""
        return secret_env_values(self._security.allowed_environment)


def _parse_version(text: str) -> str | None:
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", text)
    return match.group(1) if match else None
