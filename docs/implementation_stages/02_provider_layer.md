# Phase 2 — Provider layer and the Codex adapter

**Goal:** build the shared provider infrastructure (safe process launch, env allowlist, redaction, artifact writing, error normalization) and the first concrete adapter — **CodexProvider** — implementing the `AgentProvider` contract end to end.

**Spec:** §4.4 (adapters), §7.1 (error classes), §10 (artifacts), §12 (security). **Rules:** [security.md](../rules/security.md), [coding-style.md](../rules/coding-style.md).

**Prerequisites:** Phase 1 (contracts + config + enums). The adapter consumes `ProviderConfig` (model, timeout, sandbox, permission_profile, extra_args) and returns `AgentRunResult`.

**Reference:** [OpenAI Codex CLI Reference](https://developers.openai.com/codex/cli/reference).

---

## Logical blocks

### 2.1 Safe process runner (`providers/process.py`)

The single chokepoint for launching any external CLI. Every later subprocess goes through it.

- `subprocess.run([...])` with an **argv list** — never a string, never `shell=True`, never user strings interpolated into the command (§12.5, coding-style). The command path comes only from config; task content is passed **only as file paths** in the request (§19.5 structural guarantee).
- Mandatory `timeout` from `ProviderConfig.timeout_seconds`; a `TimeoutExpired` maps to `ErrorClass.TIMEOUT`.
- The child environment is built from the **env allowlist** (2.2) — not the parent's full env.
- Run with `cwd = working_directory` (the clone). Capture stdout/stderr to files (streamed, not held whole in memory where avoidable).
- Returns a raw result (exit code, paths, duration) for the adapter to normalize — the runner itself has no provider-specific knowledge.

### 2.2 Environment allowlist (`security/env.py`)

- Build the child env from **only** `security.allowed_environment` keys present in the parent (default: `PATH`, `HOME`, `USERPROFILE`, `CODEX_HOME`, `CLAUDE_CONFIG_DIR`) — §12.3.
- No secret or token is ever forwarded implicitly; git/agent credentials are configured outside the orchestrator (§12.9). This module is reused by the Claude adapter (P3) and the Check Runner (P5).

### 2.3 Redaction (`providers/redaction.py`)

- A pure function that scrubs known-secret shapes (token-like strings, values of denied env keys, anything matching `security.denied_read_paths` content) from text and from the request representation **before** it is written to an artifact, log, or SQLite (§12.6, security.md).
- Applied to the `request.json` artifact (a redacted representation, §10) and anywhere stderr is echoed to logs. Comprehensive negative tests ("no secret ever lands in an artifact") are expanded in P6; the mechanism lives here.

### 2.4 Artifact writer (`providers/artifacts.py`)

Per the §10 layout, write under `logs/<task-id>/stages/<stage>/<attempt>-<provider>/` (or `sub-<NN>/...` for decomposed runs — the path is supplied by the caller; this module just writes):

- `request.json` (redacted), `stdout.log`, `stderr.log`, `events.jsonl` (raw event stream), `result.json` (the machine-readable `AgentRunResult`), `before.diff` / `after.diff` (set by the pipeline in P5).
- **Never overwrite** an existing log (§10); attempts get distinct directories.
- Return the artifact paths so they can be stamped into `AgentRunResult` and registered in SQLite (registration with checksums is P5/P6).

### 2.5 Error normalization (`providers/errors.py`)

- Map a raw failure (exit code + stderr signature + timeout/crash signal) onto an `ErrorClass` (§7.1) and produce a `NormalizedError` with a **secret-free** message.
- Distinguish infra classes (fallback-eligible per `FALLBACK_ELIGIBLE`) from `task_failure` (CLI exited 0 but didn't fulfil the request → no fallback) and from `invalid_output` (unparseable structured output).
- Provider-specific stderr/exit-code signatures live in the adapter and call into shared helpers here; the classification taxonomy itself is shared.

### 2.6 CodexProvider (`providers/codex.py`)

Implements `AgentProvider` for `id = "codex"` (§4.4):

- **`preflight()`** → `ProviderHealth`: detect the executable, parse the version, check auth status and required CLI capabilities via available Codex commands; a diagnostic message **without secrets**. Missing binary → `executable_found=False` (and at run time `binary_not_found`).
- **`run(request)`**: build a safe `codex exec` invocation (argv list) from the request + config — map `sandbox`/`permission_profile`, the model, `timeout`, and validated `extra_args`. Request structured output (JSONL) and point Codex at the artifact files by path.
- Parse the JSONL/structured event stream → `structured_output`, `final_message`, `usage` (if reported), `session_id` (audit only). Normalize the exit code and events via 2.5.
- **Invariants:** the adapter performs **no fallback** and **never** touches the state machine (architecture.md); it never commits/pushes/PRs; it raises `ProviderError(infra_class, …)` for infrastructure failures and returns `AgentRunResult(status=failed, error=…)` for a clean run that didn't satisfy the task.

---

## Tests

**Unit:**

- Command builder: given a request + `ProviderConfig`, the exact argv is produced; a sandbox-bypass flag in `extra_args` is rejected (defence-in-depth with P1's config check); no user string is interpolated.
- Structured-output parsing: well-formed JSONL → populated `structured_output`/`final_message`; malformed → `invalid_output`.
- Error classification: each `(exit_code, stderr)` signature → the right `ErrorClass`.
- Redaction: a token/secret in input never appears in the redacted request or message.
- Env allowlist: only allowlisted keys reach the child env.

**Integration (fake CLI executables — stub scripts, no real Codex):** a successful run; a missing binary (`binary_not_found`); failed authorization; a rate limit; a timeout; a process crash; malformed output. (The "infra error after files changed" / fallback scenarios are exercised once the Router exists — P4.)

## Definition of Done

- [ ] `process.py` launches via argv list with a mandatory timeout, allowlisted env, and no shell interpolation; user strings never enter the command.
- [ ] Env allowlist, redaction, artifact writer, and error normalization modules exist and are unit tested.
- [ ] `CodexProvider` implements `preflight()` and `run()`, builds a safe `codex exec`, parses structured output, and normalizes errors — performing no fallback and no state-machine changes.
- [ ] Artifacts written per §10 layout and never overwritten; `request.json` is redacted.
- [ ] Integration suite with fake CLIs covers success + each infra failure class.
- [ ] `/run-checks` green.

## Not in this phase

- The Claude adapter (P3) — but `process.py`/`redaction.py`/`artifacts.py`/`errors.py`/`env.py` are built to be provider-agnostic so P3 reuses them.
- Fallback and routing (P4); partial-change snapshots (P4); state, git, pipeline (P5).
