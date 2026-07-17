# B18 — Agent Providers (Codex/Claude)

> Reconstructed from code (`providers/base.py`, `providers/claude.py`, `providers/codex.py`, `providers/errors.py`) and tests (`tests/providers/`). The code is the only source of truth; this document was rebuilt from the implementation, not from prose or comments. Significant claims carry a `file:line` reference.

**Status:** documented · **Source modules:** `src/wastech_orchestrator/providers/base.py`, `src/wastech_orchestrator/providers/_adapter_base.py`, `src/wastech_orchestrator/providers/claude.py`, `src/wastech_orchestrator/providers/codex.py`, `src/wastech_orchestrator/providers/errors.py`

> Note: `claude.py`/`codex.py` `file:line` references in this doc predate the 2026-06-22 `_adapter_base.py` extraction (audit #7) and have shifted; the comprehensive line-number re-sync is the deferred functional-map pass. The behavior described is unchanged.

## Responsibility

The only place in the system that knows coding-agent CLI syntax. `base.py` defines the provider-agnostic contract (`AgentProvider` and its data structures); `claude.py` and `codex.py` are the two adapters that implement it, each translating an `AgentRunRequest` into an argv **list** for its CLI, launching the process via the shared subprocess runner, parsing the structured event stream into a normalized `AgentRunResult`, and mapping infrastructure failures to a normalized `ErrorClass`. `errors.py` holds the shared classification taxonomy and the secret-free messages.

The two adapters' shared infrastructure spine — the attempt-directory lifecycle, the heartbeat-wrapped launch, the redact-every-sink discipline, durable-session scrubbing, and common request/result artifacts — lives in `_adapter_base.py` as `BaseCliProvider`. `ClaudeCodeProvider`/`CodexProvider` subclass it and supply only what genuinely differs through CLI-aware hooks: argv/aux-file construction, event parsing, stderr signatures, executable label, and provider-specific version/capability preflight. **`_adapter_base.py` deliberately names no CLI flag, subcommand, sandbox value, or vendor version** — that boundary keeps all CLI syntax in the adapters. `build_context_footer`/`build_effective_prompt`/`ParsedEvents` are defined there and re-exported for callers/tests.

The adapters honour a hard contract: no fallback, no git, no state-machine changes, no shell interpolation of user strings, and no secret in any artifact ([claude.py:8-17](../../../src/wastech_orchestrator/providers/claude.py#L8), [codex.py:8-14](../../../src/wastech_orchestrator/providers/codex.py#L8)). Fallback, retries, and provider selection belong to the router ([B17](B17-agent-router-and-fallback.md)).

## Public surface

- `ProviderId` ([base.py:10](../../../src/wastech_orchestrator/providers/base.py#L10)) — the only two providers: `codex`, `claude`.
- `CodexComputeMode` / `CodexMultiAgentMode` — typed advanced Codex controls (`max` / `ultra`), kept separate from scalar `AgentRunRequest.reasoning`.
- `Stage` ([base.py:17](../../../src/wastech_orchestrator/providers/base.py#L17)) — a `StrEnum` of stage identities carried on the request/result; now a transitional _identity_ (output schema / HITL parsing / audit path), not the router key.
- `RunStatus` ([base.py:28](../../../src/wastech_orchestrator/providers/base.py#L28)) — `succeeded` / `failed`.
- `ErrorClass` ([base.py:33](../../../src/wastech_orchestrator/providers/base.py#L33)) — the normalized error taxonomy, including `SESSION_UNAVAILABLE`, `INVALID_INVOCATION` (a bad argv we built), and `MODEL_REQUEST_INVALID` (a model/schema HTTP 400 the provider rejected), all deliberately **not** in `FALLBACK_ELIGIBLE` — they surface loudly instead of silently failing over to the other provider.
- `FALLBACK_ELIGIBLE` ([base.py:59](../../../src/wastech_orchestrator/providers/base.py#L59)) — the frozenset of unconditionally fallback-eligible classes; `authorization_failed` / `permission_denied` are excluded (the router decides those conditionally).
- `AgentRunRequest` ([base.py:88](../../../src/wastech_orchestrator/providers/base.py#L88)) — the run input; context arrives as **paths only**, plus `network_access` ([base.py:116](../../../src/wastech_orchestrator/providers/base.py#L116)).
- `AgentRunResult` ([base.py:126](../../../src/wastech_orchestrator/providers/base.py#L126)), `ProviderHealth` ([base.py:78](../../../src/wastech_orchestrator/providers/base.py#L78)), `NormalizedError` ([base.py:120](../../../src/wastech_orchestrator/providers/base.py#L120)), `ProviderError` ([base.py:144](../../../src/wastech_orchestrator/providers/base.py#L144)).
- `AgentProvider` ([base.py:160](../../../src/wastech_orchestrator/providers/base.py#L160)) — the runtime-checkable `Protocol` with `preflight()` and `run()`.
- `ClaudeCodeProvider` ([claude.py:398](../../../src/wastech_orchestrator/providers/claude.py#L398)) and `build_claude_argv` ([claude.py:258](../../../src/wastech_orchestrator/providers/claude.py#L258)), `map_permission` ([claude.py:212](../../../src/wastech_orchestrator/providers/claude.py#L212)), `parse_stream_json` ([claude.py:345](../../../src/wastech_orchestrator/providers/claude.py#L345)), `isolation_reasons` ([claude.py:324](../../../src/wastech_orchestrator/providers/claude.py#L324)).
- `CodexProvider` ([codex.py:519](../../../src/wastech_orchestrator/providers/codex.py#L519)),
  `build_codex_argv` ([codex.py:330](../../../src/wastech_orchestrator/providers/codex.py#L330)),
  `build_codex_capability_manifest`
  ([codex.py:291](../../../src/wastech_orchestrator/providers/codex.py#L291)), `parse_events`, and
  `isolation_reasons`.
- `classify` ([errors.py:63](../../../src/wastech_orchestrator/providers/errors.py#L63)), `make_signatures` ([errors.py:52](../../../src/wastech_orchestrator/providers/errors.py#L52)), `message_for` ([errors.py:39](../../../src/wastech_orchestrator/providers/errors.py#L39)).

## Behavior

### The contract: argv list, stdin prompt, paths-only context, no fallback/git/state

Both adapters build an argv **list** and never a shell string, so no user-supplied text can be interpreted as a flag or command. `build_claude_argv` returns a list ([claude.py:289-321](../../../src/wastech_orchestrator/providers/claude.py#L289)); `build_codex_argv` returns a list ending in `-` so the prompt is read from stdin ([codex.py:231-232](../../../src/wastech_orchestrator/providers/codex.py#L231)). The effective prompt (the Core prompt plus a deterministic context-file footer) is delivered on stdin via `stdin_text=build_effective_prompt(request)` ([claude.py:506](../../../src/wastech_orchestrator/providers/claude.py#L506), [codex.py:419](../../../src/wastech_orchestrator/providers/codex.py#L419)); `test_no_prompt_text_is_interpolated_into_argv` and `test_prompt_is_delivered_via_stdin_not_argv` enforce this. The footer only ever lists context **paths**, never their contents ([claude.py:151-171](../../../src/wastech_orchestrator/providers/claude.py#L151)). The child environment is built from the security allowlist via `build_child_env(self._security.allowed_environment)` ([claude.py:491](../../../src/wastech_orchestrator/providers/claude.py#L491), [codex.py:404](../../../src/wastech_orchestrator/providers/codex.py#L404)) — only allowlisted vars reach the process. Neither adapter commits, pushes, opens PRs, or touches the state machine; Claude additionally denies the publish commands at the tool level (below). `test_prompt_argv_isolation.py` proves the argv is byte-identical whether the prompt is benign or hostile (`git commit ... --dangerously-bypass-...` in the prompt body never reaches argv).

### Repository instruction files (`AGENTS.md` / `CLAUDE.md`): CLI auto-discovery, not orchestrator-injected

The orchestrator never reads, templates, or injects a target repo's `AGENTS.md` / `CLAUDE.md`: the prompt is the Core prompt plus a **paths-only** context footer (`build_context_footer` lists context paths, never their contents), so the only way these instruction files reach a coding agent is the CLI's own discovery. Both adapters launch with `cwd = request.working_directory`, which the agent node sets to the **target repo root** ([agent.py:387](../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L387) `working_directory=self._s.repo_dir` → [\_adapter_base.py:314](../../../src/wastech_orchestrator/providers/_adapter_base.py#L314) `cwd=request.working_directory`), and neither builder overrides the CLI's system prompt (no `--system-prompt` / `--append-system-prompt`) nor passes any flag that suppresses project-instruction discovery. The consequence is that each CLI applies its own default behaviour:

- **Codex** runs `codex exec --cd <repo root>`, so it discovers the repo's `AGENTS.md` hierarchy.
  Its user home is replaced with an isolated policy home, so host-global `~/.codex/AGENTS.md` does
  not join the invocation.
- **Claude** runs `claude -p` in the repo root with the default system prompt, so it loads `CLAUDE.md` project memory (and `~/.claude/CLAUDE.md` user memory).

Instruction-file loading is **automatic context, not a tool call**, so the `--allowedTools` / `--disallowedTools` lists do not gate it; the `denied_read_paths` deny globs (`.env`, `secrets/**`) do not match these files either. This behaviour is **not orchestrator-controlled**: there is no per-task or per-node switch to enable or disable instruction-file discovery, and no normalization across providers — it is whatever the installed CLI version does by default.

**Caveat — Claude host-global context.** The env allowlist forwards `HOME` and, when present,
`CLAUDE_CONFIG_DIR` (see [B25](B25-security-policy.md)), so Claude can still read the host
operator's global `CLAUDE.md`. That context sits outside the orchestrator's reproducibility boundary. Codex is not
affected: the adapter replaces `CODEX_HOME` with its isolated policy home. Authoring/managing
repo-scoped stubs (and further Claude isolation) is the deferred `agent_instructions:` feature.
The separate durable Claude native project-memory store remains confined by the config-dir tool
denial described below (F37).

### `AgentRunRequest`: context as paths, `network_access` is the only external grant

Every context input is a path field — `task_path`, `plan_path`, `diff_path`, `check_artifacts_path`, `review_artifacts_path`, `human_input_path`, and the advisory read-only `skill_reference_paths` ([base.py:97-105](../../../src/wastech_orchestrator/providers/base.py#L97)). `network_access` defaults to `False` ([base.py:116](../../../src/wastech_orchestrator/providers/base.py#L116)): the flow grants network only by declaring a `network_policy` (P3.2). When granted, the adapter maps it onto its own sandbox and **only** the network — never the filesystem sandbox/approvals:

- Claude appends the web tools `("WebFetch", "WebSearch")` to `--allowedTools`; absent the grant they are omitted, so a headless run cannot reach the network through them, and the permission mode is unchanged ([claude.py:282-286](../../../src/wastech_orchestrator/providers/claude.py#L282)). Verified by `test_network_access_off_by_default_no_web_tools` / `test_network_access_allows_web_tools_when_granted`.
- Codex renders both values explicitly in its generated permission profile: offline means profile
  network `false` + `web_search="disabled"`; online means profile network `true` +
  `web_search="live"`. Apps/MCP/browser/computer-use/plugins/hooks and equivalent channels stay
  disabled in both cases ([codex.py](../../../src/wastech_orchestrator/providers/codex.py),
  [codex_policy.py](../../../src/wastech_orchestrator/providers/codex_policy.py)). Offline
  `danger-full-access` raises `CONFIGURATION_ERROR` unless `strict_isolation: false` and network is
  granted. Under that explicit operator opt-out, command rules remain active but denied reads are
  not enforceable. Command snapshot tests cover all decisions.

### Codex controlled config boundary and capability audit

Every fresh/resume Codex argv includes `--strict-config --ignore-user-config` before the optional
`resume` subcommand. The fixed CLI config layer marks the project untrusted, selects an
adapter-owned permission profile, explicitly sets network/web state, and disables external
capabilities without a typed grant ([codex.py](../../../src/wastech_orchestrator/providers/codex.py)).
The closed `extra_args` extension cannot overwrite an authority-bearing key.

The adapter points the child at a stable isolated home under the existing Codex home. It contains
only generated forbidden execpolicy rules and a hard link to an existing file-backed `auth.json`;
user/project config and rules cannot join the invocation, while credential contents are neither
copied nor inspected. The policy projection lives in
[codex_policy.py](../../../src/wastech_orchestrator/providers/codex_policy.py). The provider's
credential/path-free `capabilities.json` records only effective grants and deny counts; B20 retains
it at every artifact level.

### Codex reasoning: scalar aliases, Max, and bounded Ultra

`providers.capabilities` normalizes only documented aliases: `light` → `low` and
`extra-high`/`extra_high` → `xhigh`. Scalar `minimal`/`low`/`medium`/`high`/`xhigh` stay scalar.
The Router projects `max` into `AgentRunRequest.codex_compute_mode` and `ultra` into
`codex_multi_agent_mode`; it clears both on a cross-provider fallback. Max is therefore never
silently reduced to xhigh.

Codex CLI 0.144.4's non-interactive native surface is `model_reasoning_effort="max|ultra"`.
Ultra is a CLI-level selection: the CLI sends maximum model compute and enables proactive
multi-agent behavior. The adapter conditionally enables native `multi_agent_v2`, fixes its
concurrency to four threads, applies the node timeout to agent jobs, and relies on the existing
process-tree cancellation to stop root and children together. The event stream remains the audit
source for child activity; `capabilities.json` additionally records the mode, cap, and timeout.
Request/result artifacts retain the effective scalar/mode fields even when verbose artifacts are
pruned.

Known older public GPT-5.x families fail model/mode validation before launch. Unknown future model
ids remain pass-through instead of becoming a static catalog; a provider-side unsupported-model or
entitlement error is `MODEL_REQUEST_INVALID`/configuration failure and never triggers fallback.

### Permission/sandbox mapping; forbidden values raise `CONFIGURATION_ERROR`

Claude maps a profile to a `(permission_mode, allowed_tools)` pair: `read-only → ("plan", Read/Glob/Grep)`, `workspace-write → ("acceptEdits", Read/Glob/Grep/Edit/Write/Bash)` ([claude.py](../../../src/wastech_orchestrator/providers/claude.py)). Codex resolves an effective permission-profile parent without relaxing a read-only node. Its provider/node `extra_args` pass the closed typed parser immediately before argv construction: only harmless flags and presentation/reasoning keys survive. All path/sandbox/profile/feature/tool/network/rule/environment selectors fail with `CONFIGURATION_ERROR`; typed Codex full access is available only through provider config with `strict_isolation: false` and network granted. Claude argument behavior is unchanged. `CONFIGURATION_ERROR` is not fallback-eligible.

### Runtime command/read denials

Claude translates `security.denied_commands` into `Bash(<cmd>:*)` and
`security.denied_read_paths` into `Read(<glob>)` tool-denial patterns. Codex always translates
command prefixes into `prefix_rule(..., decision="forbidden")` entries; in `workspace-write` and
`read-only` modes it also translates path globs into OS-sandbox `deny` entries under the generated
profile's `:workspace_roots`. The profile also denies `:root`
and reopens only `:minimal` runtime paths plus inherited workspace roots, keeping the managed
auth/policy home unreadable to agent commands. The boundary covers direct shell reads and reads
through interpreters/tools. The explicit full-access opt-out omits this path profile. A detected blocked Codex operation returns
`POLICY_DENIED`, which is not fallback-eligible; the agent runner records the failed node and stops
the task at `manual_action_required`. Secret harvesting remains active as redaction defense in
depth. Claude's separate native-memory deny/opt-in behavior is unchanged.

### Session resume; the raw id lives only in state.db

Claude resumes with `--resume <id>` ([claude.py:311-312](../../../src/wastech_orchestrator/providers/claude.py#L311)); Codex resumes with `exec resume <id>` where the id is **positional** right after `resume`, the prompt still on stdin, and the global security flags preserved ([codex.py:196-197](../../../src/wastech_orchestrator/providers/codex.py#L196)). The emitted resumable id is recovered from the stream: Claude reads any `session_id` field ([claude.py:369-370](../../../src/wastech_orchestrator/providers/claude.py#L369)); Codex reads `thread_id` from a `thread.started` event (or `session_id` from `session`/`session.created`) ([codex.py:276-281](../../../src/wastech_orchestrator/providers/codex.py#L276)).

The raw session id is treated as a secret. The resume id passed in is added to `_extra_secrets` so it is scrubbed from request argv / stdout / stderr / events / result ([claude.py:667-676](../../../src/wastech_orchestrator/providers/claude.py#L667), [codex.py:585-594](../../../src/wastech_orchestrator/providers/codex.py#L585)); the freshly emitted id is additionally replaced with `[REDACTED]` on disk by `_scrub_raw_session` ([claude.py:591-596](../../../src/wastech_orchestrator/providers/claude.py#L591)); and `result.json` records only the normalized correlator `session:<sha256[:12]>` via `_redact_result_session` ([claude.py:695-703](../../../src/wastech_orchestrator/providers/claude.py#L695), `normalized_session_id` in [B21](B21-secret-redaction.md)). The **in-memory** `AgentRunResult.session_id` keeps the raw id so the orchestrator can persist it to `editing_lineage` (state.db only — see [B07](B07-state-machine-and-store.md)). `test_raw_session_id_redacted_in_artifacts` asserts neither the resume id nor the emitted id appears in any on-disk artifact while the in-memory result still carries the raw id.

### Output parsing → success/quality split

A clean OS-level exit is parsed, then split into success vs. a quality `TASK_FAILURE` — task quality is judged later by review/checks, never by the adapter. Claude's `parse_stream_json` treats a `result` event with `subtype == "success"` and `not is_error` as succeeded, tolerates stray non-JSON lines, and raises `ProviderError(INVALID_OUTPUT)` when no terminal `result` event is seen ([claude.py:345-395](../../../src/wastech_orchestrator/providers/claude.py#L345)). Codex's `parse_events` accepts `result` / `task_complete` / `turn.completed` as terminal, marks failure when `status ∈ {error, failed, failure, incomplete, aborted}` ([codex.py:75](../../../src/wastech_orchestrator/providers/codex.py#L75), [codex.py:288-291](../../../src/wastech_orchestrator/providers/codex.py#L288)), and the `--output-last-message` file, when present, overrides the streamed `final_message` ([codex.py:299-300](../../../src/wastech_orchestrator/providers/codex.py#L299)). A non-success parse yields `AgentRunResult(status=failed, error=TASK_FAILURE)` rather than raising ([claude.py:559-565](../../../src/wastech_orchestrator/providers/claude.py#L559), [codex.py:478-484](../../../src/wastech_orchestrator/providers/codex.py#L478)). The CLI's own terminal subtype (e.g. Claude's `error_max_turns`) is carried both in the error message and **structurally** on `NormalizedError.failure_subtype` ([base.py](../../../src/wastech_orchestrator/providers/base.py), shared constant `MAX_TURNS_SUBTYPE`), so the flow layer can detect the max-turns outcome cleanly without substring-matching — the trigger for the optional max-turns gate ([B30](B30-flow-node-runners.md)).

### Redaction of every sink

Before anything is written, stdout / stderr / events are passed through `redact_text` and the request representation through `redact_mapping` ([claude.py:518-525](../../../src/wastech_orchestrator/providers/claude.py#L518), [codex.py:431-438](../../../src/wastech_orchestrator/providers/codex.py#L431)); parsing uses the in-memory raw stream for correctness. `_extra_secrets` supplies literals to scrub: secret-named non-allowlisted parent env values (length ≥ 8) ([claude.py:678-685](../../../src/wastech_orchestrator/providers/claude.py#L678)), the contents harvested from `denied_read_paths` files in the workspace, and the raw resume session id. `test_redaction_sinks.py` proves `stdout.log` / `events.jsonl` / `request.json` (not just `stderr.log`) are scrubbed of both a token-shaped and a file-only secret.

### Error classification (`errors.classify`)

`classify` applies a fixed precedence ([errors.py:77-86](../../../src/wastech_orchestrator/providers/errors.py#L77)):

```mermaid
flowchart TB
  start(["classify(...)"]) --> le{"launch_error?"}
  le -- yes --> bnf["BINARY_NOT_FOUND"]
  le -- no --> to{"timed_out?"}
  to -- yes --> tmo["TIMEOUT"]
  to -- no --> sig{"stderr matches a signature?"}
  sig -- yes --> sx["that signature's ErrorClass"]
  sig -- no --> ec{"exit_code == 0?"}
  ec -- yes --> tf["TASK_FAILURE"]
  ec -- no --> pc["PROCESS_CRASHED"]
```

Each adapter passes its own ordered, case-insensitive `make_signatures` table (`SESSION_UNAVAILABLE` first, then rate-limit / auth / authz / network / provider-unavailable / model-request-invalid (HTTP 400) / unsupported-version / invalid-invocation / permission-denied) ([claude.py](../../../src/wastech_orchestrator/providers/claude.py), [codex.py](../../../src/wastech_orchestrator/providers/codex.py)). The rate-limit signature also matches subscription/session-limit phrasing (`session limit` / `usage limit` / `hit your … limit` / `limit … resets`), not just `rate limit` / `429`. `INVALID_OUTPUT` is raised by the parser, not by `classify` ([errors.py:8-9](../../../src/wastech_orchestrator/providers/errors.py#L8)). The returned message is always the canonical category text and never echoes raw stderr ([errors.py:22-36](../../../src/wastech_orchestrator/providers/errors.py#L22)); `test_message_never_echoes_stderr_secret` enforces it.

A subscription/session limit does **not** always reach stderr, though: the Claude CLI can report it **structurally on stdout** as a terminal `result` event (`is_error: true`, `subtype: "success"`, `api_error_status: 429`, plus a rejected `rate_limit_event` and a "You've hit your session limit · resets …" banner) with **empty stderr**. `parse_stream_json` recognizes that shape (429 / `rate_limit_event` / banner) and the finalize step **raises** `RATE_LIMITED` instead of returning a quality `TASK_FAILURE` — so a limit that arrives on stdout still reaches the Router's fallback and the orchestrator's park, exactly like a stderr-signature limit. A genuine quality failure (e.g. `error_max_turns`) is untouched and still returned. Codex has no analogous structured stdout event, so a codex limit is caught only by the stderr signature above.

### `preflight` and `isolation_reasons`

`preflight` launches `<cli> --version` in a throwaway temp dir with the allowlisted env: a
`launch_error` means not found; a timeout/non-zero exit means not ready. Codex rejects `< 0.144.4`,
probes the controlled exec flags, checks every generated denied-command prefix through
`codex execpolicy check` (each must evaluate to `forbidden`), and runs a real host-sandbox smoke in
which a native reader can read a control file but cannot read a denied file
([codex.py](../../../src/wastech_orchestrator/providers/codex.py)). Any failure sets
`supports_required_features=False` before a model turn. Authentication remains best-effort/offline;
`isolation_reasons` remains pure for the separate `strict_isolation` gate.

## Invariants & guarantees

- argv is a **list**, never a shell string; the prompt is always on stdin; context is paths only ([claude.py:289-321](../../../src/wastech_orchestrator/providers/claude.py#L289), [codex.py:231-232](../../../src/wastech_orchestrator/providers/codex.py#L231)).
- Adapters perform **no** fallback, git, or state-machine mutation ([claude.py:8-17](../../../src/wastech_orchestrator/providers/claude.py#L8)); the router is the sole caller of `run` ([B17](B17-agent-router-and-fallback.md)).
- Rejected `extra_args` raise `ProviderError(CONFIGURATION_ERROR)` **before** launch; on that path the request artifact is written with `argv=None` and the error propagates. Codex preserves option names for audit but redacts every operator-supplied value before the generic secret-redaction pass. Tests assert the process is never launched and an ordinary non-token-shaped secret is absent from both the exception and `request.json`.
- Only the allowlisted env reaches the child process ([claude.py:491](../../../src/wastech_orchestrator/providers/claude.py#L491), [B25](B25-security-policy.md)).
- No secret lands in any artifact: every sink is redacted before writing, and the raw session id only ever reaches state.db ([claude.py:518-525](../../../src/wastech_orchestrator/providers/claude.py#L518), [B21](B21-secret-redaction.md)).
- An infrastructure failure raises `ProviderError`; a clean run that did not satisfy the task returns `AgentRunResult(status=failed, error=TASK_FAILURE)` (never an exception). `TASK_FAILURE`, `POLICY_DENIED`, `SESSION_UNAVAILABLE`, `INVALID_INVOCATION`, and `MODEL_REQUEST_INVALID` are not in `FALLBACK_ELIGIBLE` ([base.py](../../../src/wastech_orchestrator/providers/base.py)); a policy violation or bad request therefore fails loud rather than burning the fallback provider.
- `network_access` toggles only network/web capabilities, never the filesystem sandbox/approvals
  ([claude.py:282-286](../../../src/wastech_orchestrator/providers/claude.py#L282),
  [codex.py:383-420](../../../src/wastech_orchestrator/providers/codex.py#L383)).
- Codex user/project config authority and external capabilities are fixed by the adapter for fresh
  and resume; `capabilities.json` records the effective policy without credentials or host paths.

## Dependencies

- **Uses:** [B19](B19-subprocess-runner.md) (`run_process` — the safe launcher), [B20](B20-artifact-layout.md) (`create_attempt_dir` / `write_request_artifact` / `write_result_artifact`), [B21](B21-secret-redaction.md) (`redact_text` / `redact_mapping` / `read_denied_secrets` / `normalized_session_id`), [B25](B25-security-policy.md) (`build_child_env`, `find_forbidden_args`, `FORBIDDEN_SANDBOX_VALUE`), [B27](B27-observability.md) (`run_with_heartbeat`, `bind`), [B05](B05-configuration.md) (`ProviderConfig`, `SecurityConfig`).
- **Used by:** [B17](B17-agent-router-and-fallback.md) (the sole caller of `run`, via the `AgentProvider` map it holds), [B25](B25-security-policy.md) (imports `isolation_reasons` for the isolation preflight), [B23](B23-check-discovery.md) and [B01](B01-cli-and-operator-commands.md) (`preflight`). Instances are constructed by `build_providers` ([composition.py:40](../../../src/wastech_orchestrator/composition.py#L40)). The raw `session_id` returned in-memory is persisted by [B07](B07-state-machine-and-store.md) into `editing_lineage`.

## Tests

- `tests/providers/test_claude_command.py`, `tests/providers/test_codex_command.py` — argv shape, stdin-only prompt, permission/sandbox mapping, forbidden `extra_args` / profiles / sandbox, weaker-override rejection, network toggle, model/reasoning/session/output-schema flags, denied-tool patterns.
- `tests/providers/test_claude_parsing.py`, `tests/providers/test_codex_parsing.py` — terminal-event detection, success/failure subtype/status, stray-line tolerance, `INVALID_OUTPUT` on missing terminal event, Codex `thread.started` and last-message override.
- `tests/providers/test_claude_run.py`, `tests/providers/test_codex_run.py` — full `run()`/`preflight()` with an injected process runner: success, quality failure, timeout, missing binary, rate-limit signature, invalid output, config-error-before-launch, stdin delivery, artifact redaction, and (Codex) raw-session-id redaction, hostile-home isolation, capability manifests, minimum version, fresh/resume parity, and cross-platform auth paths.
- `tests/providers/test_errors.py` — `classify` precedence table and the secret-free message guarantee.
- `tests/providers/test_prompt_argv_isolation.py` — argv is independent of (hostile) prompt text for both adapters.
- `tests/providers/test_redaction_sinks.py` — every written sink (stdout/events/stderr/request) is redacted of both token-shaped and file-only secrets; Claude `Read(...)` deny patterns reach argv.
- `tests/providers/test_provider_integration.py` — the same scenario matrix against the dialect-aware fake CLI proves the two adapters are behaviourally interchangeable behind the contract.
- `tests/providers/test_artifacts.py` — attempt-directory layout and request/result serialization (the [B20](B20-artifact-layout.md) writer used here).
