#!/usr/bin/env python3
"""Deterministic stand-in for the Codex/Claude CLI, used by provider integration tests.

Invoked as ``fake_agent.py <cli_name> <scenario> <the real CLI args...>``. The launcher
(``<cli_name>.cmd`` on Windows, a shebang script on POSIX) embeds ``<cli_name>`` and ``<scenario>``;
the remaining args are exactly what the adapter built. ``<cli_name>`` selects the output dialect
(Codex JSONL vs Claude ``stream-json``) and ``<scenario>`` selects the canned stdout/stderr/exit
behavior — no network, no randomness, bounded sleep — so every success and infrastructure-failure
path is reproducible on Windows and POSIX, and the same scenario matrix runs against both adapters.

Both dialects emit the same logical success values (session ``sess-fake``, final message
"Fake implemented the task.", structured output ``{"summary": "fake done"}``) so the shared
integration matrix can assert identical results and prove interchangeability.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

_SESSION_ID = "sess-fake"
_FINAL_MESSAGE = "Fake implemented the task."
_STRUCTURED_OUTPUT = {"summary": "fake done"}

# A transient 5xx both adapters classify as PROVIDER_UNAVAILABLE (matches `internal server error` /
# `\b50[023]\b` / `service unavailable`). No network, no randomness — a fixed string.
_PROVIDER_UNAVAILABLE_STDERR = (
    "API Error: 500 Internal server error. This is a server-side issue, usually temporary.\n"
)
# State file (in the working dir) for the stateful `flaky_500_<n>` scenario: fail <n>, then pass.
_FLAKY_500_COUNTER = ".fake_500_count"


def _arg_value(args: list[str], flag: str) -> str | None:
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def _schema_output(cli_name: str, cli_args: list[str]) -> dict[str, object]:
    schema: dict[str, object] | None = None
    if cli_name == "codex":
        path = _arg_value(cli_args, "--output-schema")
        if path:
            with Path(path).open(encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                schema = loaded
    else:
        raw = _arg_value(cli_args, "--json-schema")
        if raw:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                schema = loaded
    if schema is None:
        return _STRUCTURED_OUTPUT

    required = schema.get("required")
    if isinstance(required, list) and "decompose" in required:
        return {
            "content": "Fake planning result.",
            "human_input": None,
            "decompose": False,
            "subtasks": [],
        }
    # F19: every in-flow evaluator (review/verifier/critic) requests the mandatory findings
    # schema — a well-formed empty array is a clean, accepting verdict.
    if isinstance(required, list) and "findings" in required:
        return {"findings": []}
    return {"content": "Fake refinement result.", "human_input": None}


def _emit(events: list[dict[str, object]]) -> None:
    sys.stdout.write("\n".join(json.dumps(event) for event in events) + "\n")


def _run_codex(scenario: str, cli_args: list[str]) -> int:
    if scenario == "success":
        structured = _schema_output("codex", cli_args)
        _emit(
            [
                {"type": "session", "session_id": _SESSION_ID},
                {"type": "message", "role": "assistant", "text": "stream message"},
                {"type": "usage", "input_tokens": 12, "output_tokens": 7},
                {"type": "result", "status": "success", "output": structured},
            ]
        )
        last_message = _arg_value(cli_args, "--output-last-message")
        if last_message:
            with Path(last_message).open("w", encoding="utf-8") as handle:
                handle.write(_FINAL_MESSAGE)
        return 0

    if scenario == "version":
        sys.stdout.write("codex-cli 1.2.3\n")
        return 0

    if scenario == "task_failure":
        _emit([{"type": "result", "status": "failed", "output": {"reason": "incomplete"}}])
        return 0

    if scenario == "no_work":
        # EXPERIMENTAL(no-work-infra) scenario — remove with the feature.
        # A terminal event that is NOT success and produced ZERO work: status failed, a usage event
        # reporting output_tokens 0, and NO ``output`` (no structured output). Distinct from
        # ``task_failure`` above (which carries an ``output`` payload) — the adapter must RAISE the
        # generic AGENT_NO_PROGRESS net (never return task_failure). No stderr / rate-limit signal.
        _emit(
            [
                {"type": "usage", "input_tokens": 8, "output_tokens": 0},
                {"type": "result", "status": "failed"},
            ]
        )
        return 0

    if scenario == "auth_failed":
        sys.stderr.write("Error: not logged in. Run `codex login` to authenticate.\n")
        return 1

    if scenario == "rate_limited":
        sys.stderr.write("Error: rate limit exceeded (429 Too Many Requests)\n")
        return 1

    if scenario == "session_limit":
        # Codex has no structured stdout limit event (verified) — a subscription/session limit can
        # only surface on stderr, caught by the extended RATE_LIMITED signature and raised.
        sys.stderr.write("You've hit your session limit · resets 6:30am (Europe/Warsaw)\n")
        return 1

    if scenario == "invalid_output":
        sys.stdout.write("this is not valid jsonl output\n")
        return 0

    if scenario == "process_crashed":
        sys.stderr.write("fatal: unexpected termination\n")
        return 134

    if scenario == "timeout":
        time.sleep(30)
        return 0

    sys.stderr.write(f"unknown scenario {scenario!r}\n")
    return 2


def _run_claude(scenario: str, cli_args: list[str]) -> int:
    if scenario == "success":
        structured = _schema_output("claude", cli_args)
        _emit(
            [
                {"type": "system", "subtype": "init", "session_id": _SESSION_ID},
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "stream message"}]},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": _FINAL_MESSAGE,
                    "session_id": _SESSION_ID,
                    "usage": {"input_tokens": 12, "output_tokens": 7},
                    "structured_output": structured,
                    "total_cost_usd": 0.001,
                },
            ]
        )
        return 0

    if scenario == "version":
        sys.stdout.write("1.2.3 (Claude Code)\n")
        return 0

    if scenario == "task_failure":
        _emit(
            [
                {"type": "system", "subtype": "init", "session_id": _SESSION_ID},
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "could not finish",
                    "session_id": _SESSION_ID,
                },
            ]
        )
        return 0

    if scenario == "no_work":
        # EXPERIMENTAL(no-work-infra) scenario — remove with the feature.
        # A terminal ``result`` that is NOT success and produced ZERO work: is_error with a plain
        # (non-max-turns) subtype, usage reporting output_tokens 0, NO structured_output, and NO
        # rate-limit signature (no 429 / banner / rate_limit_event). Distinct from ``task_failure``
        # above (which reports no usage) — the adapter must RAISE the generic AGENT_NO_PROGRESS net.
        _emit(
            [
                {"type": "system", "subtype": "init", "session_id": _SESSION_ID},
                {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": "",
                    "session_id": _SESSION_ID,
                    "usage": {"input_tokens": 8, "output_tokens": 0},
                },
            ]
        )
        return 1

    if scenario == "error_max_turns":
        # A clean run that exhausted its turn cap: a terminal result event with the CLI's own
        # ``error_max_turns`` subtype + a session id (so the max-turns gate can resume it). The real
        # CLI exits non-zero here; the adapter classifies from the terminal event, not the code.
        _emit(
            [
                {"type": "system", "subtype": "init", "session_id": _SESSION_ID},
                {
                    "type": "result",
                    "subtype": "error_max_turns",
                    "is_error": True,
                    "result": "Reached the maximum number of turns.",
                    "session_id": _SESSION_ID,
                },
            ]
        )
        return 1

    if scenario == "auth_failed":
        sys.stderr.write("Error: not logged in. Run `claude login` to authenticate.\n")
        return 1

    if scenario == "rate_limited":
        sys.stderr.write("Error: rate limit exceeded (429 Too Many Requests)\n")
        return 1

    if scenario == "session_limit":
        # A subscription/session limit surfaced STRUCTURALLY on stdout (the real Claude CLI shape):
        # a terminal ``result`` with ``is_error`` + ``api_error_status: 429`` + a rejected
        # ``rate_limit_event`` + the "session limit … resets" banner, and EMPTY stderr. The adapter
        # must RAISE this as RATE_LIMITED (never return task_failure). The exit code is irrelevant —
        # the adapter classifies from the parsed terminal event, not the code.
        _emit(
            [
                {"type": "system", "subtype": "init", "session_id": _SESSION_ID},
                {
                    "type": "rate_limit_event",
                    "status": "rejected",
                    "rateLimitType": "five_hour",
                    "overageDisabledReason": "out_of_credits",
                    "resetsAt": 1783639800,
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": True,
                    "api_error_status": 429,
                    "result": "You've hit your session limit · resets 6:30am (Europe/Warsaw)",
                    "session_id": _SESSION_ID,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            ]
        )
        return 1

    if scenario == "invalid_output":
        sys.stdout.write("this is not valid stream-json output\n")
        return 0

    if scenario == "process_crashed":
        sys.stderr.write("fatal: unexpected termination\n")
        return 134

    if scenario == "timeout":
        time.sleep(30)
        return 0

    sys.stderr.write(f"unknown scenario {scenario!r}\n")
    return 2


def _run_codex_sandbox(cli_args: list[str]) -> int:
    """Model ``codex sandbox -P`` for the WRI-003 no-model canary.

    Faithfully simulates the orchestrator's generated permission profile: a read of the exchange
    (``.worc-io``) is allowed; a read of the private home and any write are denied. Deliberately
    **scenario-independent** — the canary must pass so the real ``exec`` scenario below plays out;
    genuine OS enforcement is proven by the host smoke, not this stand-in. A probe command follows
    the ``--`` separator (e.g. ``-- /bin/cat <path>`` or ``-- /bin/sh -c "printf x >> <path>"``).
    """
    probe = cli_args[cli_args.index("--") + 1 :] if "--" in cli_args else []
    probe_str = " ".join(probe)
    is_write = ">>" in probe_str
    reads_exchange = ".worc-io" in probe_str  # exchange subtree (not a substring of `.worc/…`)
    if reads_exchange and not is_write:
        return 0
    sys.stderr.write("sandbox: operation not permitted\n")
    return 1


_DIALECTS = {"codex": _run_codex, "claude": _run_claude}


def _keep_ours(text: str) -> str:
    """Resolve every conflict block by keeping the HEAD (ours) side and dropping the markers."""
    out: list[str] = []
    in_conflict = False
    keep = True
    for line in text.splitlines(keepends=True):
        if line.startswith("<<<<<<<"):
            in_conflict, keep = True, True
            continue
        if in_conflict and line.startswith("======="):
            keep = False
            continue
        if in_conflict and line.startswith(">>>>>>>"):
            in_conflict = False
            continue
        if not in_conflict or keep:
            out.append(line)
    return "".join(out)


def _resolve_conflicts_in_tree(root: Path) -> None:
    """Strip conflict markers from every tracked-looking file in the tree (skips dotted dirs)."""
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part.startswith(".") for part in rel.parts):
            continue  # skip .git / .worc and other dotted dirs
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "<<<<<<<" in text:
            with contextlib.suppress(OSError):
                path.write_text(_keep_ours(text), encoding="utf-8")


def main() -> int:
    cli_name = sys.argv[1] if len(sys.argv) > 1 else "codex"
    scenario = sys.argv[2] if len(sys.argv) > 2 else "success"
    cli_args = sys.argv[3:]

    # Drain stdin (the prompt) so the parent's write never raises a broken pipe.
    with contextlib.suppress(OSError):
        sys.stdin.read()

    # WRI-003: the Codex adapter runs a no-model ``codex sandbox -P`` canary BEFORE ``exec``. Model
    # it scenario-independently (exchange readable, private/writes denied) so the canary passes and
    # the ``exec`` scenario below is what the test actually exercises.
    if cli_name == "codex" and cli_args and cli_args[0] == "sandbox":
        return _run_codex_sandbox(cli_args)

    # ``success_edit`` behaves like ``success`` but also makes a deterministic code change in the
    # working directory (the clone), so a pipeline run has something to commit (phases 4–5 e2e).
    if scenario == "success_edit":
        with (
            contextlib.suppress(OSError),
            Path("agent_change.py").open("w", encoding="utf-8") as handle,
        ):
            handle.write("# change made by the fake agent\nVALUE = 1\n")
        scenario = "success"

    # ``resolve_conflicts`` behaves like ``success`` but first resolves every Git conflict marker in
    # the working tree (keeping the HEAD/ours side), the way a conflict-resolution agent would. Used
    # by the merge-flow (worc merge-task) integration test.
    if scenario == "resolve_conflicts":
        _resolve_conflicts_in_tree(Path.cwd())
        scenario = "success"

    # Transient infra scenarios — identical failure surface for both dialects, so handled here:
    # ``provider_unavailable`` always emits a 5xx; ``flaky_500_<n>`` fails <n> times (counted in a
    # cwd state file) then succeeds — deterministic (no time/random), to drive the Router's retry.
    if scenario == "provider_unavailable":
        sys.stderr.write(_PROVIDER_UNAVAILABLE_STDERR)
        return 1
    if scenario.startswith("flaky_500_"):
        threshold = int(scenario.rsplit("_", 1)[1])
        counter = Path.cwd() / _FLAKY_500_COUNTER
        seen = int(counter.read_text()) if counter.exists() else 0
        if seen < threshold:
            counter.write_text(str(seen + 1))
            sys.stderr.write(_PROVIDER_UNAVAILABLE_STDERR)
            return 1
        with contextlib.suppress(OSError):
            counter.unlink()
        scenario = "success"

    dialect = _DIALECTS.get(cli_name)
    if dialect is None:
        sys.stderr.write(f"unknown cli {cli_name!r}\n")
        return 2
    return dialect(scenario, cli_args)


if __name__ == "__main__":
    sys.exit(main())
