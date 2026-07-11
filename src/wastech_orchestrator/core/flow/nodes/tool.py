"""Custom tool node runner (P5) — runs an operator executable out-of-process under the ceiling.

A ``tool`` node runs an operator's own program (any language) from ``<repo>/.worc/tools/`` through
the same :func:`~wastech_orchestrator.providers.process.run_process` ceiling an ``agent`` node uses:
an **argv list** (never a shell string), a **mandatory timeout**, and exactly the allowlisted child
``env`` (the parent environment is never inherited). The program receives only the allowlisted path
context + the flow's ``args`` on **stdin** — never secrets, the full environment, or a raw session
id — and reports back through its **exit code** and an optional JSON object on stdout.

Outcome contract (see :func:`parse_tool_output`), in priority order:

1. **launch-error / timeout** → :class:`~.base.NodeManualRequired` (infra, not a quality fail — it
   never spends a fix iteration), mirroring the ``checks`` command-profile gate.
2. Otherwise a JSON object with an ``outcome`` key is **authoritative** (``pass`` / ``fail`` /
   ``route:<label>``); an invalid value fails closed to manual.
3. Otherwise the **exit code** gates: ``0`` → ``pass``, non-zero → ``fail`` (linter style). Any JSON
   object still enriches ``findings`` / ``data``.

The core **records** ``findings`` (→ ``NodeOutcome.findings``) and ``data`` (→
``NodeOutcome.structured_output``) but never *applies* them: the orchestrator hands a tool no git
credentials (env-allowlist) and has no code path where a returned value triggers a git / state write
(the "git only the orchestrator" invariant). stdout / stderr are redacted before they are written
under the tool run's per-run dir (``stages/<node_id>/run-<id>/``), and the redacted stdout is
exposed downstream as ``{<node_id>_path}``
(symmetric with an agent node's output), so a tool → agent hand-off is pure flow wiring.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from wastech_orchestrator.core.flow.context_paths import build_path_context
from wastech_orchestrator.core.flow.engine import Finding, NodeContext, NodeOutcome, NodeResult
from wastech_orchestrator.core.flow.nodes.base import (
    NodeInputs,
    NodeManualRequired,
    NodeServices,
)
from wastech_orchestrator.core.flow.schema import FlowNode, ToolNode
from wastech_orchestrator.core.flow.tools_registry import ToolResolutionError
from wastech_orchestrator.providers.artifacts import (
    TOOL_STDERR_FILENAME,
    TOOL_STDOUT_FILENAME,
    node_run_dir,
)
from wastech_orchestrator.providers.process import ProcessResult
from wastech_orchestrator.providers.redaction import redact_text
from wastech_orchestrator.state_store import NodeRunRow

# Raw severity tokens a tool may emit in a finding, mapped onto the typed :class:`Finding` taxonomy
# (low/medium/high). Findings are audit-trail / supervisor context only — the engine never inspects
# them to route a tool node (its outcome kind does), so the mapping is deliberately generous.
_HIGH_SEVERITIES = frozenset({"error", "critical", "blocking", "high"})
_MEDIUM_SEVERITIES = frozenset({"warning", "medium", "moderate"})

# Windows batch wrappers (`.bat`/`.cmd`) are the portable way to ship a script tool cross-platform
# (the resolver finds `<tool>.cmd` where POSIX finds an extensionless `+x` file), but CreateProcess
# cannot launch a batch file directly with ``shell=False`` — it must run through the command
# interpreter. `.exe`/`.com` are PE images CreateProcess starts directly, so they are NOT here.
_BATCH_SUFFIXES = frozenset({".bat", ".cmd"})


class ToolContractError(Exception):
    """The tool emitted a JSON ``outcome`` that is not ``pass`` / ``fail`` / ``route:<label>``.

    A contract violation by the tool (not a clean quality result), so the runner fails it closed to
    manual review rather than routing it as a ``fail`` a fixer agent cannot act on.
    """


@dataclass(frozen=True)
class ToolContract:
    """The parsed result of a tool run: the edge-selecting outcome + recorded (not applied) data."""

    outcome: str  # pass | fail | route:<label>
    findings: tuple[Finding, ...] = ()
    data: Mapping[str, object] | None = None


class ToolNodeRunner:
    """Run a ``tool`` node: resolve the operator executable, run it under the ceiling, gate on it.

    Constructed per execution unit with its shared services + inputs (like every node runner).
    """

    def __init__(self, services: NodeServices, inputs: NodeInputs) -> None:
        self._s = services
        self._in = inputs

    def run(self, node: FlowNode, ctx: NodeContext) -> NodeResult:
        assert isinstance(node, ToolNode)
        run_id = self._s.store.record_node_run(
            NodeRunRow(
                task_id=ctx.task_id,
                node_id=node.id,
                node_kind="tool",
                subtask_order=ctx.subtask_order,
                status="running",
                started_at=self._s.clock(),
            )
        )
        tool_path = self._resolve(node, run_id)

        # Per-run dir keyed by node.id + run_id (mirrors agent/evaluator runs): a tool node that
        # re-runs in a loop keeps every pass's streams; {<node_id>_path} resolves the latest run.
        node_dir = node_run_dir(self._s.artifacts_root, ctx.task_id, node.id, run_id)
        node_dir.mkdir(parents=True, exist_ok=True)
        stdout_path = node_dir / TOOL_STDOUT_FILENAME

        result = self._s.run_process(
            _launch_argv(tool_path),
            cwd=self._s.repo_dir,
            env=dict(self._s.process_env),  # exactly the allowlisted child env — no secrets
            timeout_seconds=node.timeout_seconds or self._s.tools_default_timeout_seconds,
            stdout_path=str(stdout_path),
            stdin_text=self._build_stdin(node, ctx),
        )
        redacted_stdout = self._write_redacted_artifacts(node_dir, stdout_path, result)
        # Expose the redacted stdout artifact downstream as {<node_id>_path} (symmetric with an
        # agent node), regardless of outcome — a partial output on failure is still useful context.
        self._register(ctx.task_id, node.id, str(stdout_path))

        # (1) Infrastructure failure — launch error / timeout → manual, never a quality fail. It
        #     mirrors the one existing external-command gate (checks command-profile); no fix loop.
        if result.launch_error is not None or result.timed_out:
            self._complete(run_id, status="timeout" if result.timed_out else "launch_error")
            reason = "timed out" if result.timed_out else "could not be launched"
            raise NodeManualRequired(
                f"tool node {node.id!r}: the tool {node.tool!r} {reason} — an infrastructure "
                "failure a fix loop cannot resolve; task parked for manual action"
            )

        # (2)/(3) Outcome by JSON ``outcome`` (authoritative) or exit code (linter style).
        try:
            contract = parse_tool_output(result.exit_code, redacted_stdout)
        except ToolContractError as exc:
            self._complete(run_id, status="invalid_output")
            raise NodeManualRequired(
                f"tool node {node.id!r}: the tool {node.tool!r} emitted an invalid outcome ({exc}) "
                "— failing closed to manual review"
            ) from exc

        self._complete(run_id, status=_run_status(contract.outcome), outcome=contract.outcome)
        return NodeResult(
            node_id=node.id,
            outcome=NodeOutcome(
                contract.outcome, findings=contract.findings, structured_output=contract.data
            ),
            node_run_id=run_id,
        )

    # -- helpers ---------------------------------------------------------------

    def _resolve(self, node: ToolNode, run_id: int) -> Path:
        """Resolve the tool name → executable, fail-closed to manual if the registry can't (P5.2).

        Validation already resolved every ``tool`` at preflight, so this succeeds in the normal
        case; it fails closed only if the operator layer is absent or the file changed since then.
        """
        registry = self._s.tool_registry
        if registry is None:
            self._complete(run_id, status="launch_error")
            raise NodeManualRequired(
                f"tool node {node.id!r}: no operator tool registry is configured "
                "(no .worc/tools/ layer) — cannot run a tool node"
            )
        try:
            return registry.resolve(node.tool)
        except ToolResolutionError as exc:
            self._complete(run_id, status="launch_error")
            raise NodeManualRequired(f"tool node {node.id!r}: {exc}") from exc

    def _build_stdin(self, node: ToolNode, ctx: NodeContext) -> str:
        """The stdin context JSON: allowlisted paths + the flow ``args`` only — never secrets."""
        context: dict[str, Any] = {
            "task_id": ctx.task_id,
            "node_id": node.id,
            "subtask_order": ctx.subtask_order,
            "paths": build_path_context(self._in, self._s.repo_dir),
            "args": dict(node.args),
        }
        return json.dumps(context, ensure_ascii=False)

    def _write_redacted_artifacts(
        self, node_dir: Path, stdout_path: Path, result: ProcessResult
    ) -> str:
        """Redact and persist stdout (in place) + stderr; return the redacted stdout text.

        ``run_process`` streams raw stdout to ``stdout_path``; we read it back, redact, and
        overwrite so the artifact — and the ``{<node_id>_path}`` a downstream agent reads — never
        carries a secret. stderr is captured in memory (secret-prone) and written redacted too.
        """
        secrets = self._s.prompt_secrets
        redacted_stdout = redact_text(_read_text(stdout_path), extra_secrets=secrets)
        stdout_path.write_text(redacted_stdout, encoding="utf-8")
        (node_dir / TOOL_STDERR_FILENAME).write_text(
            redact_text(result.stderr_text, extra_secrets=secrets), encoding="utf-8"
        )
        return redacted_stdout

    def _register(self, task_id: str, node_id: str, path: str) -> None:
        if self._s.register_artifact is not None:
            self._s.register_artifact(task_id, f"tool:{node_id}", path)

    def _complete(self, run_id: int, *, status: str, outcome: str | None = None) -> None:
        self._s.store.complete_node_run(
            run_id, status=status, outcome=outcome, finished_at=self._s.clock()
        )


def _launch_argv(tool_path: Path) -> list[str]:
    """The argv used to launch a resolved tool — always a list, never a shell string.

    A Windows ``.bat``/``.cmd`` cannot be started directly by CreateProcess under ``shell=False``,
    so such a tool is launched through the command interpreter as ``[COMSPEC, "/c", <path>]``
    (``COMSPEC`` is read only to locate the interpreter binary, never passed as child env — the
    child still gets exactly the allowlisted env). Everything else — a POSIX ``+x`` script, a
    Windows ``.exe`` — launches directly. Keeping it a list means no user string is shell-parsed.
    """
    if os.name == "nt" and tool_path.suffix.lower() in _BATCH_SUFFIXES:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(tool_path)]
    return [str(tool_path)]


def parse_tool_output(exit_code: int | None, stdout: str) -> ToolContract:
    """Resolve a tool's outcome from its exit code + optional JSON stdout (P5 outcome contract).

    A JSON object with an ``outcome`` key is authoritative (``pass`` / ``fail`` / ``route:<label>``;
    an invalid value raises :class:`ToolContractError`). Otherwise the exit code gates (``0`` →
    ``pass``, else ``fail``). ``findings`` / ``data`` are read from any JSON object regardless —
    they enrich the audit trail but never change the outcome the exit code already decided. Pure;
    the launch-error / timeout infra case is handled by the runner before this is called.
    """
    parsed = _parse_json_object(stdout)
    findings = _findings_from(parsed)
    data = _data_from(parsed)
    if parsed is not None and "outcome" in parsed:
        outcome = _validated_outcome(parsed["outcome"])
    else:
        outcome = "pass" if exit_code == 0 else "fail"
    return ToolContract(outcome=outcome, findings=findings, data=data)


def _validated_outcome(raw: object) -> str:
    if isinstance(raw, str) and (
        raw in ("pass", "fail") or (raw.startswith("route:") and len(raw) > len("route:"))
    ):
        return raw
    raise ToolContractError(f"outcome {raw!r} is not 'pass', 'fail', or 'route:<label>'")


def _parse_json_object(stdout: str) -> dict[str, Any] | None:
    """Parse stdout as a JSON object, or ``None`` (empty / not JSON / not object → linter style)."""
    text = stdout.strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _findings_from(parsed: dict[str, Any] | None) -> tuple[Finding, ...]:
    if parsed is None:
        return ()
    raw = parsed.get("findings")
    if not isinstance(raw, list):
        return ()
    return tuple(_to_tool_finding(item) for item in raw if isinstance(item, Mapping))


def _to_tool_finding(raw: Mapping[str, Any]) -> Finding:
    token = str(raw.get("severity", "")).lower()
    if token in _HIGH_SEVERITIES:
        severity = "high"
    elif token in _MEDIUM_SEVERITIES:
        severity = "medium"
    else:
        severity = "low"
    reason = str(raw.get("reason") or raw.get("what") or raw.get("message") or "")
    paths_raw = raw.get("paths")
    if isinstance(paths_raw, (list, tuple)):
        paths = tuple(str(p) for p in paths_raw)
    else:
        single = raw.get("path")
        paths = (str(single),) if single else ()
    return Finding(severity=severity, reason=reason, paths=paths)  # type: ignore[arg-type]


def _data_from(parsed: dict[str, Any] | None) -> Mapping[str, object] | None:
    if parsed is None:
        return None
    data = parsed.get("data")
    return data if isinstance(data, Mapping) else None


def _run_status(outcome: str) -> str:
    """The ``node_runs`` status string for a completed tool run (audit-trail label)."""
    if outcome == "pass":
        return "passed"
    if outcome == "fail":
        return "failed"
    return "routed"  # route:<label>


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
