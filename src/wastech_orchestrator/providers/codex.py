"""CodexProvider — the Codex CLI adapter.

Implements the :class:`~wastech_orchestrator.providers.base.AgentProvider` contract for
``id = "codex"`` using ``codex exec`` (OpenAI Codex CLI). This is the **only** module that knows
Codex syntax; it composes the provider-agnostic infrastructure (process runner, env allowlist,
redaction, artifacts, error normalization).

Invariants (architecture.md / security.md): the adapter performs **no fallback** and **never**
touches the state machine; it never commits/pushes/PRs. It raises
:class:`~wastech_orchestrator.providers.base.ProviderError` (with the right
:class:`~wastech_orchestrator.providers.base.ErrorClass`) for infrastructure failures, and returns
``AgentRunResult(status=failed, error=task_failure)`` for a clean run that did not satisfy the task.
The CLI is launched as an argv list (no shell); the prompt is fed on stdin; context reaches Codex
only as file paths.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from wastech_orchestrator.config.schema import ProviderConfig
from wastech_orchestrator.providers._adapter_base import (
    BaseCliProvider,
    ParsedEvents,
    coerce_usage_int,
    read_text,
)
from wastech_orchestrator.providers.artifacts import ArtifactPaths
from wastech_orchestrator.providers.base import (
    AgentRunRequest,
    ErrorClass,
    NormalizedError,
    NormalizedUsage,
    ProviderError,
    ProviderId,
    UsageScope,
    build_context_footer,
    build_effective_prompt,
)
from wastech_orchestrator.providers.capabilities import normalize_codex_reasoning
from wastech_orchestrator.providers.errors import (
    StderrSignature,
    make_signatures,
    message_for,
)
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.security.forbidden_args import (
    FORBIDDEN_SANDBOX_VALUE,
    find_forbidden_args,
    find_full_access_args,
)

__all__ = [
    "CodexProvider",
    "ParsedEvents",
    "build_codex_argv",
    "build_context_footer",
    "build_effective_prompt",
    "isolation_reasons",
    "parse_events",
    "resolve_codex_resources_dir",
]

_DEFAULT_SANDBOX = "workspace-write"
_LAST_MESSAGE_FILENAME = "last-message.txt"
_OUTPUT_SCHEMA_FILENAME = "output-schema.json"

# The Windows sandbox helper and its package layout. On Windows ``workspace-write``, Codex launches
# this helper BY NAME, so the directory holding it must be discoverable on the child ``PATH``. The
# orchestrator rebuilds a clean allowlisted env that drops it, so
# :func:`resolve_codex_resources_dir` locates the Codex standalone package's ``codex-resources``
# directory and the adapter prepends it.
_SANDBOX_HELPER_EXE = "codex-windows-sandbox-setup.exe"
_PACKAGE_MANIFEST_NAME = "codex-package.json"
_DEFAULT_RESOURCES_DIRNAME = "codex-resources"

# A fatal Windows-sandbox failure on stderr — either the setup helper could not be launched OR the
# sandbox could not spawn a child process at run time (seclogon ``CreateProcessWithLogonW`` failing
# on every command and on the ``apply_patch`` write path). Codex can still print a clean terminal
# SUCCESS event (exit 0) while this error means the run never touched the workspace, so it is
# matched both as an ordinary ``PERMISSION_DENIED`` stderr signature (the nonzero-exit path) and by
# the post-success guard (``_post_success_infra_error``) that flips a false success into an infra
# failure so the Router falls over to the other provider.
_HELPER_LAUNCH_FAILED_PATTERN = (
    r"orchestrator_helper_launch_failed"
    r"|codex-windows-sandbox-setup\.exe"
    r"|setup refresh failed to launch helper"
    r"|CreateProcessWithLogonW failed"  # seclogon could not spawn the sandbox child at run time
    r"|fs sandbox helper failed"  # the same failure on the apply_patch (write) path
    r"|windows sandbox(?: failed)?:"  # the general Codex windows-sandbox error prefix
)
_HELPER_LAUNCH_FAILED_RE = re.compile(_HELPER_LAUNCH_FAILED_PATTERN, re.IGNORECASE)

# Statuses on a terminal Codex ``result`` event that mark the turn as NOT having satisfied the task.
# Any other status (incl. a missing one) is treated as a completed run — task quality is judged
# later by the orchestrator's review/checks, not by the adapter.
_FAILURE_STATUSES = frozenset({"error", "failed", "failure", "incomplete", "aborted"})

# Codex stderr signatures → normalized error classes (most specific first).
_CODEX_SIGNATURES = make_signatures(
    [
        (
            ErrorClass.SESSION_UNAVAILABLE,
            r"session not found|no such session|unknown session|conversation not found"
            r"|no conversation with|thread not found|cannot resume",
        ),
        (
            ErrorClass.RATE_LIMITED,
            r"rate limit|\b429\b|too many requests|quota exceeded"
            r"|session limit|usage limit|hit your (session|usage) limit|limit .* resets",
        ),
        (
            ErrorClass.AUTHENTICATION_FAILED,
            r"not logged in|codex login|authentication|unauthorized|\b401\b",
        ),
        (ErrorClass.AUTHORIZATION_FAILED, r"forbidden|not authorized|\b403\b"),
        (
            ErrorClass.NETWORK_UNAVAILABLE,
            r"could not resolve|connection refused|network is unreachable|getaddrinfo|dns",
        ),
        (
            ErrorClass.PROVIDER_UNAVAILABLE,
            r"service unavailable|\b50[023]\b|bad gateway|internal server error",
        ),
        (
            # A model/schema HTTP 400 the provider rejected (a request WE built) — split from a
            # generic PROCESS_CRASHED so it surfaces loudly instead of wastefully falling over to
            # the other provider, which 400s the same request. Disjoint from the 401/403/429/50x
            # word-boundary numeric signatures above.
            ErrorClass.MODEL_REQUEST_INVALID,
            r"\b400\b|bad request|invalid[_ ]?(json[_ ]?)?schema|unsupported parameter",
        ),
        (ErrorClass.UNSUPPORTED_VERSION, r"unsupported version"),
        (
            # argparse/usage rejection of OUR argv (codex exit 2) — a bad-argv bug on our side, not
            # a version gate. A separate class so it surfaces loudly instead of silently failing
            # over (F38 was masked as unsupported_version). A stale CLI that emits "unknown option"
            # for a newer flag is caught by the preflight version check first.
            ErrorClass.INVALID_INVOCATION,
            r"unknown option|unrecognized option|unexpected argument",
        ),
        (
            ErrorClass.PERMISSION_DENIED,
            r"sandbox denied|permission denied|operation not permitted|blocked by sandbox|"
            + _HELPER_LAUNCH_FAILED_PATTERN,
        ),
    ]
)


def _resources_dir_for_package(package_root: Path) -> Path:
    """The package's resources directory: ``resourcesDir`` from ``codex-package.json`` if present,
    else the default ``codex-resources`` name (the observed layout: a sibling of ``bin``)."""
    manifest = package_root / _PACKAGE_MANIFEST_NAME
    subdir: object = None
    try:
        subdir = json.loads(manifest.read_text(encoding="utf-8")).get("resourcesDir")
    except (OSError, ValueError, AttributeError):
        subdir = None
    name = subdir if isinstance(subdir, str) and subdir else _DEFAULT_RESOURCES_DIRNAME
    return package_root / name


def resolve_codex_resources_dir(
    command: str,
    *,
    system: str | None = None,
    which: Callable[[str], str | None] | None = None,
    userprofile: str | None = None,
) -> Path | None:
    """Locate the Codex ``codex-resources`` directory that holds the Windows sandbox helper.

    Returns the directory (the one containing :data:`_SANDBOX_HELPER_EXE`) to prepend onto the child
    ``PATH`` on Windows, or ``None`` when it need not / cannot be resolved. Pure and injectable
    (``system`` / ``which`` / ``userprofile`` seams, resolved at call time) so it is unit-testable
    on any host. Resolution tries, first-match wins: (1) the package the ``command`` executable
    resolves into — ``codex.exe`` sits in ``bin\\`` with ``codex-resources`` as a sibling, reached
    after resolving the AppData junction; (2) the well-known
    ``%USERPROFILE%\\.codex\\packages\\standalone\\current`` package.
    """
    name = system if system is not None else platform.system()
    if name != "Windows":
        return None
    which_fn = which if which is not None else shutil.which
    candidates: list[Path] = []
    exe = which_fn(command)
    if exe:
        # `…\bin\codex.exe` → resolve the junction → package root (…\releases\<ver>\) holding `bin`.
        package_root = Path(exe).resolve().parent.parent
        candidates.append(_resources_dir_for_package(package_root))
    profile = userprofile if userprofile is not None else os.environ.get("USERPROFILE")
    if profile:
        current = (Path(profile) / ".codex" / "packages" / "standalone" / "current").resolve()
        candidates.append(_resources_dir_for_package(current))
    for candidate in candidates:
        if (candidate / _SANDBOX_HELPER_EXE).exists():
            return candidate
    return None


def _effective_sandbox(config: ProviderConfig, request: AgentRunRequest) -> str:
    """Resolve the Codex sandbox without relaxing the node's requested permission profile."""
    configured = config.sandbox or config.permission_profile or _DEFAULT_SANDBOX
    if request.permission_profile == "read-only" or configured == "read-only":
        return "read-only"
    return configured


def build_codex_argv(
    config: ProviderConfig,
    request: AgentRunRequest,
    *,
    output_schema_path: str | None,
    last_message_path: str,
) -> list[str]:
    """Build the ``codex exec`` argv (a list, never a shell string).

    Raises :class:`ProviderError` (``CONFIGURATION_ERROR``) if ``extra_args`` would weaken the
    sandbox/approvals (the absolutely-forbidden ``--dangerously*`` / ``--yolo`` / ``--ignore-rules``
    flags) — defence in depth over the P1 config validator. The full-access sandbox
    (``danger-full-access``) is **not** rejected here: it is operator-selectable and gated by
    ``strict_isolation`` at preflight (security rule #3), so it passes through to the CLI. The
    prompt is delivered on stdin (the trailing ``-``), never on the command line.
    """
    combined_extra = tuple(config.extra_args) + tuple(request.extra_args)
    reasons = find_forbidden_args(combined_extra)
    if reasons:
        raise ProviderError(
            ErrorClass.CONFIGURATION_ERROR, "rejected unsafe extra_args: " + "; ".join(reasons)
        )

    sandbox = _effective_sandbox(config, request)

    # Approval policy is a global Codex flag. Both Codex CLI 0.57 and current releases reject it
    # when it is placed after the ``exec`` subcommand.
    argv = [
        config.command,
        "--ask-for-approval",
        "never",
        "exec",
    ]
    # Exec-level options belong to parent ``codex exec`` and MUST precede the optional ``resume``
    # subcommand (codex 0.142.x grammar: ``codex exec [OPTIONS] resume [SESSION_ID] [PROMPT]``).
    # --cd / --sandbox / --json / --output-last-message / --output-schema and the network -c are
    # exec options; placing any after ``resume`` is rejected (unexpected argument '--cd', exit 2).
    # Only -m/--model and -c/--config are accepted by ``resume`` itself, so those go after it below.
    argv += [
        "--cd",
        request.working_directory,
        "--sandbox",
        sandbox,
        "--json",
        "--output-last-message",
        last_message_path,
    ]
    if request.network_access:
        # The flow granted network (network_policy). Codex blocks network in the sandbox by default;
        # enable it for the workspace-write sandbox. This toggles ONLY network — the sandbox's
        # filesystem limit and the ``never`` approval policy stay in force (the ceiling holds).
        argv += ["-c", "sandbox_workspace_write.network_access=true"]
    else:
        # No network grant → also deny the host-side ``web_search`` tool. It runs on the OpenAI
        # backend, OUTSIDE the sandbox network toggle above, so without this an "offline" node can
        # still reach the web (F5: a network_access=false writer performed 9 web searches).
        # Disabling the tool makes network_access=false actually offline.
        argv += ["-c", 'web_search="disabled"']
    if output_schema_path is not None:
        argv += ["--output-schema", output_schema_path]
    # Durable session resume (P2.2): ``codex exec [exec-options] resume <SESSION_ID>`` continues the
    # prior session. SESSION_ID is positional right after ``resume``; the prompt is read from stdin
    # (-). --model and model_reasoning_effort (-c) are resume-compatible, so they follow the
    # subcommand; on the fresh path (no subcommand) they follow the exec options.
    if request.session_id:
        argv += ["resume", request.session_id]
    model = request.model or config.model
    if model:
        argv += ["--model", model]
    reasoning = request.reasoning or config.reasoning
    if reasoning:
        effort = normalize_codex_reasoning(reasoning)
        if effort is None:
            raise ProviderError(
                ErrorClass.CONFIGURATION_ERROR,
                f"unsupported Codex reasoning value {reasoning!r}",
            )
        argv += ["-c", f'model_reasoning_effort="{effort}"']
    argv += list(combined_extra)
    argv.append("-")  # read the prompt from stdin
    return argv


def isolation_reasons(config: ProviderConfig) -> list[str]:
    """Reasons the configured Codex isolation cannot be enabled — an empty list means OK.

    Pure and offline (no CLI launched), so it can drive the ``strict_isolation`` preflight
    (:mod:`wastech_orchestrator.security.isolation`). Codex has no per-tool deny mechanism — the
    sandbox *is* the isolation, so "isolation enabled" means a real sandbox mode is in force. The
    full-access sandbox (``danger-full-access``) is reported as "no isolation" whether it is set via
    the ``sandbox`` field or selected in ``extra_args`` (the gate, not an absolute ban), and
    ``extra_args`` must not weaken the sandbox/approvals.
    """
    sandbox = config.sandbox or config.permission_profile or _DEFAULT_SANDBOX
    reasons: list[str] = []
    if sandbox == FORBIDDEN_SANDBOX_VALUE:
        reasons.append(f"sandbox {sandbox!r} grants full filesystem access (no isolation)")
    reasons.extend(f"extra_args {r}" for r in find_full_access_args(config.extra_args))
    reasons.extend(f"extra_args {r}" for r in find_forbidden_args(config.extra_args))
    return reasons


def _normalize_codex_usage(usage: Mapping[str, Any] | None) -> NormalizedUsage | None:
    """Map Codex's raw ``usage`` to the provider-neutral cumulative record.

    Codex reports ``input_tokens`` inclusive of the cached subset, so uncached input is derived; it
    has no cache-creation counter, so ``cache_write`` stays ``None``. Returns ``None`` when no usage
    was emitted, preserving the no-work guard's "absent usage never fires" contract.
    """
    if not usage:
        return None
    input_total = coerce_usage_int(usage.get("input_tokens"))
    cache_read = coerce_usage_int(usage.get("cached_input_tokens"))
    uncached_input = (
        input_total - cache_read
        if input_total is not None and cache_read is not None
        else input_total
    )
    return NormalizedUsage(
        scope=UsageScope.SESSION_CUMULATIVE,
        input_total=input_total,
        cache_read=cache_read,
        cache_write=None,
        uncached_input=uncached_input,
        output_total=coerce_usage_int(usage.get("output_tokens")),
        reasoning_output=coerce_usage_int(usage.get("reasoning_output_tokens")),
    )


def parse_events(
    stdout_text: str, last_message_text: str | None = None, *, schema_requested: bool = False
) -> ParsedEvents:
    """Parse a Codex JSONL event stream into :class:`ParsedEvents`.

    Tolerant of stray non-JSON lines as long as a recognizable terminal ``result`` event is present.
    Raises :class:`ProviderError` (``INVALID_OUTPUT``) when no terminal event can be found.

    F19/F22 (codex-cli 0.139.0, verified by smoke test): the terminal ``turn.completed`` event
    carries only ``{type, usage}`` — no ``output`` field — so a schema-requested run never fills
    ``structured_output`` from the event stream alone; the schema result instead lands in the
    ``--output-last-message`` file (and mirrors it as an ``agent_message`` event's text). When
    ``schema_requested`` and no terminal ``output`` was seen, parse ``last_message_text`` as the
    structured output. Fails **closed**: an unparseable/non-object last message leaves
    ``structured_output`` at ``None`` rather than guessing — the evaluator runner (F19) then routes
    the verdict to manual instead of a silent accept. ``usage`` is also read directly off the
    terminal event, mirroring ``claude.py``'s ``parse_stream_json``.
    """
    final_message: str | None = None
    structured_output: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    terminal_seen = False
    succeeded = False

    for line in stdout_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue  # tolerated only if a terminal event is still found below
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type", ""))
        if event_type in ("session", "session.created") or "session_id" in event:
            session_id = event.get("session_id", session_id)
        # ``codex exec`` emits ``{"type":"thread.started","thread_id":"..."}`` — the thread id is
        # the resumable session id passed back to ``codex exec resume <id>`` (durable sessions).
        if event_type == "thread.started":
            session_id = event.get("thread_id", session_id)
        if event_type in ("message", "assistant", "agent_message"):
            text = event.get("text") or event.get("message")
            if isinstance(text, str):
                final_message = text
        if event_type in ("usage", "token_count"):
            usage = {k: v for k, v in event.items() if k != "type"}
        if event_type in ("result", "task_complete", "turn.completed"):
            terminal_seen = True
            status = str(event.get("status", "success")).lower()
            succeeded = status not in _FAILURE_STATUSES
            output = event.get("output")
            if isinstance(output, dict):
                structured_output = output
            run_usage = event.get("usage")
            if isinstance(run_usage, dict):
                usage = run_usage

    if not terminal_seen:
        raise ProviderError(ErrorClass.INVALID_OUTPUT, message_for(ErrorClass.INVALID_OUTPUT))

    if last_message_text:
        final_message = last_message_text.strip()
    if schema_requested and structured_output is None and last_message_text:
        try:
            candidate = json.loads(last_message_text.strip())
        except json.JSONDecodeError:
            candidate = None
        if isinstance(candidate, dict):
            structured_output = candidate
    return ParsedEvents(
        final_message=final_message,
        structured_output=structured_output,
        usage=usage,
        normalized_usage=_normalize_codex_usage(usage),
        session_id=session_id,
        succeeded=succeeded,
    )


class CodexProvider(BaseCliProvider):
    """Codex CLI adapter implementing the ``AgentProvider`` protocol.

    Carries only the Codex-specific syntax (argv, stderr signatures, JSONL parsing, the
    ``--output-last-message`` file); the run/preflight/redaction spine lives in
    :class:`BaseCliProvider`.
    """

    id: str = ProviderId.CODEX.value

    def _executable_label(self) -> str:
        return "codex"

    def _sandbox_needs_windows_helper(self) -> bool:
        """Whether the configured sandbox engages the Windows sandbox helper.

        The helper backs the OS sandbox used by ``workspace-write``; ``read-only`` and the
        full-access sandbox (no isolation) do not launch it, so they never need it discoverable.
        """
        configured = self._config.sandbox or self._config.permission_profile or _DEFAULT_SANDBOX
        return configured not in ("read-only", FORBIDDEN_SANDBOX_VALUE)

    def _augment_child_env(self, env: dict[str, str]) -> dict[str, str]:
        """Prepend the Codex ``codex-resources`` directory onto ``PATH`` on Windows.

        On Windows ``workspace-write``, Codex launches ``codex-windows-sandbox-setup.exe`` by name;
        the orchestrator's clean allowlisted ``PATH`` does not include it. Resolve the helper's
        package directory and prepend it so the CLI can find it — adjusting only the value of an
        already-allowlisted key (``PATH``), never widening the env allowlist or the sandbox.
        """
        resources = resolve_codex_resources_dir(self._config.command)
        if resources is None:
            return env
        prefix = str(resources)
        path = env.get("PATH", "")
        env["PATH"] = prefix + os.pathsep + path if path else prefix
        return env

    def _post_success_infra_error(self, stderr_text: str) -> NormalizedError | None:
        """Turn a clean terminal SUCCESS whose stderr proves a fatal sandbox-helper failure into a
        raised infra error (``permission_denied``), so the Router falls over instead of trusting a
        run that never touched the workspace. Matched narrowly (helper signatures only)."""
        if _HELPER_LAUNCH_FAILED_RE.search(stderr_text):
            return NormalizedError(
                ErrorClass.PERMISSION_DENIED, message_for(ErrorClass.PERMISSION_DENIED)
            )
        return None

    def _windows_sandbox_helper_error(self, env: Mapping[str, str]) -> str | None:
        """Operator-facing preflight message when the Windows sandbox helper is undiscoverable.

        Runs on the already-augmented ``env``. Returns ``None`` off Windows, for a sandbox that
        needs no helper, or when the helper is reachable (via the package layout or already on
        ``PATH``); otherwise a precise message naming where ``codex`` resolved and what is missing.
        """
        if platform.system() != "Windows" or not self._sandbox_needs_windows_helper():
            return None
        if resolve_codex_resources_dir(self._config.command) is not None:
            return None
        if shutil.which(_SANDBOX_HELPER_EXE, path=env.get("PATH")) is not None:
            return None
        exe = shutil.which(self._config.command) or self._config.command
        return (
            f"Codex sandbox helper {_SANDBOX_HELPER_EXE} is not discoverable for the "
            f"workspace-write sandbox on Windows: codex resolved to {exe}, but its "
            f"{_DEFAULT_RESOURCES_DIRNAME!r} package directory was not found and the helper is not "
            "on PATH. Reinstall or upgrade the Codex standalone package so its "
            f"{_DEFAULT_RESOURCES_DIRNAME} directory (with {_SANDBOX_HELPER_EXE}) exists next to "
            "bin/, or add it to PATH"
        )

    def _preflight_healthy_detail(self, env: Mapping[str, str]) -> str:
        """Show where the Windows sandbox helper resolved on the healthy preflight line."""
        if platform.system() != "Windows" or not self._sandbox_needs_windows_helper():
            return ""
        resources = resolve_codex_resources_dir(self._config.command)
        if resources is not None:
            return f" (Windows sandbox helper: {resources})"
        if shutil.which(_SANDBOX_HELPER_EXE, path=env.get("PATH")) is not None:
            return " (Windows sandbox helper: on PATH)"
        return ""

    def _preflight_capability_error(self, env: Mapping[str, str]) -> str | None:
        """Verify ``codex exec`` exposes ``-c/--config`` for model config overrides.

        Codex reasoning is set through the official ``model_reasoning_effort`` config key. Network
        grants also use ``-c``. Probe ``codex exec --help`` and fail preflight if the subcommand
        lacks config overrides, catching an incompatible CLI before a real node run. A probe that
        does not cleanly exit is treated as inconclusive (no block) — the version check already
        passed. First, on Windows, block when the sandbox helper is undiscoverable — a mid-run
        ``orchestrator_helper_launch_failed`` is far more useful surfaced here, before the flow.
        """
        helper_error = self._windows_sandbox_helper_error(env)
        if helper_error is not None:
            return helper_error
        ok, help_text = self._probe([self._config.command, "exec", "--help"], env)
        if ok and "--config" not in help_text and "-c" not in help_text:
            return (
                "codex exec does not expose -c/--config, required for "
                "model_reasoning_effort and sandbox network overrides; upgrade Codex CLI or clear "
                "Codex reasoning/network overrides"
            )
        return None

    def _preflight_degraded_reasons(self, env: Mapping[str, str]) -> tuple[str, ...]:
        """Confirm ``codex exec resume`` still accepts the options this adapter places after it.

        The resume argv is ``codex exec [exec-options] resume <SESSION_ID> --model .. -c ..`` — only
        ``-m/--model`` and ``-c/--config`` are valid after the ``resume`` subcommand. A future Codex
        that drops ``resume`` or those options would silently break every resume node (supervisor,
        documentation, rework, fixing). Probe ``codex exec resume --help`` and flag the drift so
        preflight surfaces it — fatal only when codex has no fallback provider (decided upstream),
        else a warning. Light grep contract (like the ``-c/--config`` probe): empty output is
        inconclusive (no flag); passes on the current 0.142.x grammar.
        """
        ok, help_text = self._probe([self._config.command, "exec", "resume", "--help"], env)
        if not help_text.strip():
            return ()  # inconclusive — the probe produced nothing to grep
        has_model = "--model" in help_text or "-m" in help_text
        has_config = "--config" in help_text or "-c" in help_text
        if ok and has_model and has_config:
            return ()
        return (
            "codex exec resume no longer accepts the -m/--model and -c/--config options this "
            "adapter places after `resume <SESSION_ID>` (Codex CLI grammar drift); resume nodes "
            "(supervisor, documentation, rework, fixing) will fail on codex — pin a compatible "
            "Codex CLI or route these nodes to another provider",
        )

    def _signatures(self) -> Sequence[StderrSignature]:
        return _CODEX_SIGNATURES

    def _build_argv(
        self, request: AgentRunRequest, paths: ArtifactPaths
    ) -> tuple[list[str], tuple[str, bool]]:
        last_message_path = str(Path(paths.attempt_dir) / _LAST_MESSAGE_FILENAME)
        schema_path = self._write_output_schema(paths, request)
        argv = build_codex_argv(
            self._config,
            request,
            output_schema_path=schema_path,
            last_message_path=last_message_path,
        )
        return argv, (last_message_path, schema_path is not None)

    def _parse(
        self,
        raw_stdout: str,
        paths: ArtifactPaths,
        parse_context: tuple[str, bool],
        extra_secrets: tuple[str, ...],
    ) -> ParsedEvents:
        # The --output-last-message file is the authoritative final message; redact it on disk too
        # since it may echo agent output.
        last_message_path, schema_requested = parse_context
        last_message_text = read_text(last_message_path)
        if last_message_text and Path(last_message_path).exists():
            Path(last_message_path).write_text(
                redact_text(last_message_text, extra_secrets=extra_secrets), encoding="utf-8"
            )
        return parse_events(
            raw_stdout, last_message_text or None, schema_requested=schema_requested
        )

    def _write_output_schema(self, paths: ArtifactPaths, request: AgentRunRequest) -> str | None:
        if request.output_schema is None:
            return None
        schema_path = str(Path(paths.attempt_dir) / _OUTPUT_SCHEMA_FILENAME)
        Path(schema_path).write_text(
            json.dumps(request.output_schema, ensure_ascii=False), encoding="utf-8"
        )
        return schema_path
