# VF-7 investigation — provider-neutral orchestrator security preamble (defense-in-depth)

Status: **investigation only (read-only analysis)** Date: 2026-07-24 Owner: Vladimir Makarevich Related: [VF-5](runtime-validation-findings.md) (repo-instruction injection rollback), [VF-6](runtime-validation-findings.md) (`disable_read_isolation` escape hatch), [WRI-011](wri-011-freeze-agent-inputs.md)

This is a feasibility + design investigation for a new mechanism: a short, fixed, **provider-neutral security block that the orchestrator prepends to every role prompt** as defense-in-depth, so that even when isolation is relaxed the agent is told up-front not to touch/read the orchestrator's service files (`.worc`, `.worc-io`, `.git`, `tasks/`, secrets). It is **not** an enforcement mechanism (README §3), it is **not** the repo-instruction injection VF-5 removed, and it must stay a single Core-owned seam — not per-adapter. Everything below is verified against the current working tree (branch `feat/agent-worc-read-isolation`, with the uncommitted VF-5 rollback applied). Citations are `file:line`.

## 1. Current state — how the prompt is assembled and the precedence model

### 1.1 The prompt-assembly path (verified)

A node's prompt is the content of its `role_file` with only allowlisted **path** variables substituted — never task bodies, diffs, logs, env, or secrets:

- `render_role_prompt` reads the role file (flow-dir contained) and calls the fixed renderer — [core/flow/prompt.py:41](../../../src/wastech_orchestrator/core/flow/prompt.py) → `render_prompt` [core/prompts.py:55](../../../src/wastech_orchestrator/core/prompts.py). The renderer substitutes only names in `ALLOWED_PROMPT_VARS` [core/prompts.py:21](../../../src/wastech_orchestrator/core/prompts.py) and is explicitly "the fixed security core … only stdin prompt text … never provider argv, CLI syntax" ([core/prompts.py:1-12](../../../src/wastech_orchestrator/core/prompts.py)).
- The **agent** node builds `request.prompt = render_role_prompt(...)` at [core/flow/nodes/agent.py:621](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) inside `_build_request` ([agent.py:606-662](../../../src/wastech_orchestrator/core/flow/nodes/agent.py)).
- The **evaluator** node builds `request.prompt = render_role_prompt(...)` at [core/flow/nodes/evaluator.py:295](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) inside `_build_request` ([evaluator.py:286-329](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)).
- The **supervisor** (a constant layer, not a node) builds `request.prompt = prompt` at [core/supervisor.py:791-808](../../../src/wastech_orchestrator/core/supervisor.py), where `prompt` is composed by `_step_prompt` / `_finalize_prompt` / `_proposal_prompt` / `_handoff_prompt` ([supervisor.py:952-1080](../../../src/wastech_orchestrator/core/supervisor.py)); each of those calls `_render_chain` → `render_role_prompt` for the operator lens, with a built-in fallback string when no role file resolves ([supervisor.py:1082-1096](../../../src/wastech_orchestrator/core/supervisor.py), builtins at [supervisor.py:159-170](../../../src/wastech_orchestrator/core/supervisor.py)).

`render_role_prompt` is shared by agent + evaluator but the supervisor only reaches it **indirectly** and additionally wraps/prepends its own headers and can bypass it entirely (builtins). So `render_role_prompt` is **not** a single seam that all three share.

### 1.2 The one seam all three share: `build_effective_prompt`

Every request — agent, evaluator, supervisor — converges on `build_effective_prompt(request)`, which returns `request.prompt` plus the context-files footer ([providers/base.py:232-237](../../../src/wastech_orchestrator/providers/base.py); footer builder [providers/base.py:209-229](../../../src/wastech_orchestrator/providers/base.py)). This function is the **universal, provider-neutral choke point** for what actually reaches the model:

- It is the CLI **stdin** for both adapters, via the shared base hook `_stdin_text` ([_adapter_base.py:253-260](../../../src/wastech_orchestrator/providers/_adapter_base.py)) fed to the process at [_adapter_base.py:449-464](../../../src/wastech_orchestrator/providers/_adapter_base.py) (`stdin_text=self._stdin_text(request)`). **Neither adapter overrides `_stdin_text`** (verified — no override in `claude.py`/`codex.py`), and Codex reads the prompt from stdin (`argv.append("-")`, [providers/codex.py:473](../../../src/wastech_orchestrator/providers/codex.py)). So the entire model-facing prompt is the user-prompt stdin channel — there is no orchestrator-controlled system-prompt channel in play (see §5, VF-5).
- It is the audit **rendered prompt / prompt-audit** for all three: agent [agent.py:431](../../../src/wastech_orchestrator/core/flow/nodes/agent.py), evaluator [evaluator.py:165](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py), supervisor [supervisor.py:862](../../../src/wastech_orchestrator/core/supervisor.py).
- It is the persisted **request artifact** (`prompt` field), [_adapter_base.py:650](../../../src/wastech_orchestrator/providers/_adapter_base.py).

Full caller set of `build_effective_prompt`: base.py:232 (def), _adapter_base.py:260 + :650, agent.py:431, evaluator.py:165, supervisor.py:862. There is no other path from a request to the model. Prepending inside `build_effective_prompt` therefore reaches stdin (all three kinds × both providers), the rendered-prompt audit, the prompt-audit, and `request.json` **consistently and unforgeably** — a runner cannot forget it.

### 1.3 Precedence model — declared but not implemented in code

WRI-011 declared the intended instruction precedence: **"built-in provider safety/system policy, orchestrator security contract, frozen repository instructions, flow role, and task/context paths"** ([wri-011-freeze-agent-inputs.md:42](wri-011-freeze-agent-inputs.md)). The **"orchestrator security contract"** layer is exactly the block VF-7 would add. But that precedence was tied to the frozen-instruction **injection surface**, which **VF-5 rolled back** ([runtime-validation-findings.md:118-185](runtime-validation-findings.md); WRI-011 header amendment [wri-011-freeze-agent-inputs.md:5](wri-011-freeze-agent-inputs.md)). Consequently, **today there is no code that implements an "orchestrator security contract" prompt layer** — it exists only as a phrase in the ADR. Grep confirms: no "security contract" prompt text and no `security preamble` anywhere in `src/` or `docs/` (only unrelated "defense-in-depth" code comments).

Where the block belongs in precedence: **below** the provider's built-in safety/system policy (which we neither control nor can outrank from a user prompt) and **above** the repository instructions (`AGENTS.md`/`CLAUDE.md`, now read natively per VF-5), the flow role text (`request.prompt`), and the task/context paths (the footer). In the single stdin channel this means the block is the **first** orchestrator-authored text: `preamble → flow-role prompt → context footer`; the repo instructions arrive through the provider's own native/system channel, outside stdin.

### 1.4 Distinguish from the existing "preamble"

The word "preamble" already appears in the code and in [.agents/rules/architecture.md](../../../.agents/rules/architecture.md) ("Core … owns … the isolation/check preamble"). That is the **operational** preamble in `_drive_via_engine` — the strict-isolation preflight + check preflight + branch prep ([core/orchestrator.py:2293-2320](../../../src/wastech_orchestrator/core/orchestrator.py)), not a prompt-text prefix. VF-7 is a **prompt** preamble; it is conceptually aligned (Core-owned, security-first) but a different artifact. The report uses "security preamble" to avoid conflation.

## 2. Proposed injection seam(s) in Core

### 2.1 Recommended: content owned by Core, prepended at the neutral choke point

The single-seam design keeps the **content** in Core and the **concatenation** at the universal neutral choke point, carrying the text on the request:

1. Add an optional neutral field to the core↔provider contract: `security_preamble: str | None = None` on `AgentRunRequest` ([providers/base.py:160-206](../../../src/wastech_orchestrator/providers/base.py)) — a plain text field, exactly like the neutral `write_guard` policy object already carried there ([providers/base.py:197-206](../../../src/wastech_orchestrator/providers/base.py)). No hidden channel; it is an explicit field (architecture.md contract rule).
2. Prepend it in `build_effective_prompt` ([providers/base.py:232-237](../../../src/wastech_orchestrator/providers/base.py)): when present, `f"{request.security_preamble}\n\n{request.prompt}"`, then the footer — i.e. order `preamble → role → footer`. `build_context_footer` is untouched. `build_effective_prompt` stays pure text concatenation with **zero CLI syntax**, so this respects "core does not know CLI syntax" even though the function physically lives in the provider **interface** module that core already imports (`from wastech_orchestrator.providers.base import build_effective_prompt` at agent.py:96, evaluator.py:57, supervisor.py:44).
3. Build the content once in a small **Core** module (e.g. `core/flow/security_preamble.py`) — `build_orchestrator_security_preamble(*, read_isolation_off: bool) -> str` — deriving path names from the layout constants so the text cannot drift from the actual denies: `CONTROL_HOME_DIRNAME` (`.worc`), `EXCHANGE_HOME_DIRNAME` (`.worc-io`) ([runtime_layout.py:37-39](../../../src/wastech_orchestrator/runtime_layout.py)), and `REPO_INSTRUCTION_NAMES` ([core/flow/instruction_bundle.py:62](../../../src/wastech_orchestrator/core/flow/instruction_bundle.py)).
4. Thread the built string through `NodeServices` — one field alongside the existing security-derived carriers `trust_level`/`protected_paths`/`prompt_secrets` ([core/flow/nodes/base.py:273](../../../src/wastech_orchestrator/core/flow/nodes/base.py), :307, :326, :329). The orchestrator/composition resolves it once (it depends only on config, not per-node data) and injects it; the agent/evaluator set `security_preamble=self._s.security_preamble` in `_build_request`, and the supervisor sets it in `_run_result` ([supervisor.py:791-808](../../../src/wastech_orchestrator/core/supervisor.py)) from its own injected value. These are one-line assignments from a single source of truth, so drift risk is negligible and the guaranteed choke point (§1.2) is the actual enforcement of "every request carries it".

This is the ADR's "orchestrator security contract" precedence layer (§1.3) realized provider-neutrally, at one seam, with the content in Core.

### 2.2 Alternative (considered, not recommended): prepend inside each `request.prompt`

Prepend via a shared Core helper at the three `request.prompt` construction sites (agent.py:621, evaluator.py:295, and the supervisor's ~4 prompt builders). It also flows to audit (because it becomes part of `request.prompt`), but it is **3-plus call sites** including the supervisor's multiple builders, it is easy for a future node kind to forget, and it mixes the orchestrator contract into `request.prompt` (worse audit separation). The choke-point design in §2.1 is strictly better on single-seam and forget-proofness.

## 3. Proposed preamble content (concrete draft, derived from the real denies)

Derived directly from the enforced policy so text and enforcement cannot diverge:

- `InternalDenyPolicy.denied_paths` — control home + private home (`.worc`), the resolved `.env`, provider auth/config homes (`~/.claude`/`$CLAUDE_CONFIG_DIR`, `$CODEX_HOME`), frozen control + instruction bundles ([runtime_layout.py:99-142](../../../src/wastech_orchestrator/runtime_layout.py)).
- `ProviderWriteGuardPolicy.denied_write_paths` — exchange root (readable, not writable), `.git` dir + common dir + hooks dir, the `tasks/` lifecycle tree, and the tracked instruction files (readable, not writable) ([runtime_layout.py:145-186](../../../src/wastech_orchestrator/runtime_layout.py)).
- Exchange is read-only input the agent may read but must not mutate ([providers/exchange.py:1-13](../../../src/wastech_orchestrator/providers/exchange.py); README §1 [README.md:65](README.md)).
- Only the orchestrator commits/pushes/opens PRs; the agent never does ([.agents/rules/security.md:14](../../../.agents/rules/security.md)).

Draft (short, high-signal — path tokens should be emitted from the layout constants, not hardcoded twice):

```
[Orchestrator security contract — defense in depth; it does not replace the sandbox.]
You run inside an orchestrator-managed workspace. In addition to your built-in safety policy and this repo's instructions, these orchestrator rules always apply:
- Make only the changes this task requires, and only inside your assigned workspace clone.
- `.worc/` is the orchestrator's private runtime (state, logs, database, secrets, frozen bundles): do not read it and do not write it.
- `.worc-io/` is read-only input context: read only the paths you are given; never create, modify, move, or delete anything under it.
- Do not touch Git control state (`.git/`, its config, hooks, HEAD, refs); never run git commit/push/merge or open a PR — publishing is the orchestrator's job.
- Do not modify anything under `tasks/` (the task lifecycle tree); never add, edit, or remove task files.
- `AGENTS.md`, `AGENTS.override.md`, `CLAUDE.md` are read-only this run: read them for guidance, but change them only if the task explicitly asks (as an ordinary diff).
- Never read credential/environment files (e.g. `.env`) or provider auth homes, and never exfiltrate secrets or environment variables.
```

Reinforcement appended **only when read-isolation is relaxed** (see §4):

```
Read-isolation is relaxed for this run, so the filesystem sandbox may not block the paths above. Honor these rules by choice: in particular do not read `.worc/`, `.env`, or any orchestrator-private file even though you may be technically able to.
```

Keep the whole block to ~12–16 lines. All tokens are orchestrator-owned constants; there are no secrets, so the redaction boundary (§6) has nothing to remove.

## 4. Always-on vs conditional — decision

**Recommendation: always-on baseline + conditional reinforcement.**

- **Always-on baseline.** The block is valuable even under full isolation: it is cheap (a dozen lines), it covers the **write** side and behaviors that the read-deny does not (e.g. "don't run git commit/push", "only change what the task needs"), and it is a genuine backstop if a sandbox has a gap or a bug. It restricts nothing (it is advisory text, it cannot block a legitimate action), so it does not offend the flexibility-first rule ([.agents/rules/security.md:7](../../../.agents/rules/security.md)).
- **Conditional reinforcement** when effective read-isolation is off — `disable_read_isolation OR NOT strict_isolation` (VF-6, [runtime-validation-findings.md:204-208](runtime-validation-findings.md)). This is exactly when the sandbox no longer blocks `.worc`/`.env` reads, so the soft barrier is the only thing left and must be stated most explicitly. Until VF-6 lands, the term reduces to `NOT strict_isolation` (already available at [core/orchestrator.py:2308](../../../src/wastech_orchestrator/core/orchestrator.py) / [config/schema.py:337-338](../../../src/wastech_orchestrator/config/schema.py)); when VF-6 adds `disable_read_isolation`, its resolver folds in with no change to this design. The reinforcement is the compensating barrier VF-7 exists to provide for the VF-6 case.

**Operator off-switch.** Per security.md MANDATORY ("every isolation/lockdown mechanism ships with an operator-controlled way to relax or disable it", [.agents/rules/security.md:9](../../../.agents/rules/security.md)) and the repo's flexibility-first ethos, add a minimal **operator-only** toggle (e.g. `security.orchestrator_preamble: bool = true`) so an operator can disable/replace it. It is advisory (arguably outside the strict scope of that rule, which targets functionality-limiting isolation), so this toggle is a low-priority nicety rather than a hard requirement; if included it is config-only, defaults on, and — like `disable_read_isolation` — must be rejected when it arrives via a task/`extra_args`/flow.

## 5. Consistency with the invariants

- **Provider-neutral / single Core seam.** Content is built in Core (§2.1 step 3), concatenated at one neutral function ([providers/base.py:232](../../../src/wastech_orchestrator/providers/base.py)). No adapter code changes; both adapters inherit it through `_stdin_text` ([_adapter_base.py:253-260](../../../src/wastech_orchestrator/providers/_adapter_base.py)).
- **"Core does not know the CLI syntax."** `build_effective_prompt` is pure text concatenation with no flag/subcommand/sandbox token; the field on `AgentRunRequest` is neutral text. Adapters stay thin — they gain nothing. Satisfies [AGENTS.md:19](../../../AGENTS.md) and [architecture.md:16-18](../../../.agents/rules/architecture.md).
- **Does not revive what VF-5 removed.** VF-5 removed per-provider **repository-instruction** injection — Codex's `_stdin_text` `<repository-instructions>` block and Claude's `--append-system-prompt-file` ([runtime-validation-findings.md:126-128](runtime-validation-findings.md)); `--append-system-prompt`/`--append-system-prompt-file` are now in the Claude **reserved/forbidden** set ([providers/claude.py:386-389](../../../src/wastech_orchestrator/providers/claude.py)). VF-7 is **different content** (the orchestrator's own short contract, not repo instructions) delivered by a **different mechanism** (one neutral stdin prefix in Core, not a per-adapter system-prompt flag). It does not re-add any forbidden flag, does not disable native discovery, and does not reintroduce a shadow discovery engine — the exact "does-not-scale" cost VF-5 rejected.
- **Not enforcement (ADR §3).** The block is defense-in-depth/advisory; the enforcement remains the sandbox + deny projection. It must never satisfy an enforcement acceptance criterion ([README.md:81](README.md)) and must not be reported as access control ([README.md:132](README.md)). The wording ("defense in depth; it does not replace the sandbox") states this in the artifact itself.
- **Cannot be weakened by a task/extra_args/flow.** The text is a Core-owned constant; no prompt variable feeds it (it is not in `ALLOWED_PROMPT_VARS`, [core/prompts.py:21](../../../src/wastech_orchestrator/core/prompts.py)); any toggle is operator-config-only. Matches [AGENTS.md:22](../../../AGENTS.md) and the VF-6 escape-hatch boundary.

## 6. Risks and limits

- **Soft / advisory only.** A capable model can ignore it; **workspace prompt-injection** (a malicious file the agent reads) can countermand it. It is never a substitute for the sandbox — and it is weakest exactly when it matters most (read-isolation off, §4), because then there is no enforcement backstop. Document as reduced-assurance.
- **Over-trust hazard.** Operators must not read "the agent is told not to read `.worc`" as "`.worc` is protected". Docs/preflight must keep saying enforcement = sandbox; VF-7 = advisory.
- **Token cost.** It runs on **every** node/turn (agent + evaluator + each supervisor turn), so length multiplies by node count — keep it short and cap it.
- **Drift.** If the path list were hardcoded twice it could diverge from the denies; mitigate by emitting the dir names from the layout constants ([runtime_layout.py:37-39](../../../src/wastech_orchestrator/runtime_layout.py), [instruction_bundle.py:62](../../../src/wastech_orchestrator/core/flow/instruction_bundle.py)).
- **Redaction.** The block is orchestrator-authored constant text with no secret, carried on stdin (not published to the exchange), and it appears in the already-redacted rendered-prompt/prompt-audit — nothing to redact, no new leak surface.

## 7. Recommendation — VF-6 vs a separate VF-7, with acceptance criteria

**Recommendation: a separate task, VF-7.** Rationale: it is a distinct provider-neutral prompt mechanism with its own seam (`build_effective_prompt` + a Core content builder + an `AgentRunRequest` field + `NodeServices` threading), whereas VF-6 is a tightly-scoped **config escape hatch** (new key + Claude argv branch + isolation-gate + composition, [runtime-validation-findings.md:220-222](runtime-validation-findings.md)). The always-on baseline is valuable and shippable **independently** of VF-6; VF-6 can ship without it (louder warnings only). Folding VF-7 into VF-6 would blur VF-6's scope. **Sequence VF-7 to land with or right after VF-6** so its conditional reinforcement reuses VF-6's effective-read-isolation resolver; the baseline can even precede VF-6 using `NOT strict_isolation`.

### Acceptance criteria (proposed)

- [ ] The orchestrator prepends a short, Core-owned security block to the stdin prompt of every **agent, evaluator, and supervisor** provider call, at a single provider-neutral seam, with **no** adapter/CLI-syntax change and **without** `--append-system-prompt` or any per-provider injection.
- [ ] The block content is derived from the actual deny policies (`InternalDenyPolicy` / `ProviderWriteGuardPolicy` / exchange read-only) using the layout constants, so it cannot drift from enforcement.
- [ ] Always-on baseline; when effective read-isolation is off (`disable_read_isolation OR NOT strict_isolation`) the block gains the explicit private-path read-restraint paragraph.
- [ ] The block is documented as defense-in-depth/advisory and is **not** cited as satisfying any enforcement AC (ADR §3); sandbox + deny projection remain the enforcement.
- [ ] The block is Core-owned constant text, not reachable/overridable from a task, `extra_args`, or a flow node; any operator toggle is config-only and defaults on.
- [ ] The block appears in the redacted rendered-prompt / prompt-audit / `request.json` for all three request kinds and contains no secret; ordering is `preamble → flow-role prompt → context footer`.
- [ ] VF-5 is not revived: no repository-instruction injection, no forbidden system-prompt flag, native discovery untouched.

### Likely files

- [src/wastech_orchestrator/providers/base.py](../../../src/wastech_orchestrator/providers/base.py) — add `AgentRunRequest.security_preamble`; prepend in `build_effective_prompt`.
- `src/wastech_orchestrator/core/flow/security_preamble.py` (new) — the Core content builder using `runtime_layout` constants + `REPO_INSTRUCTION_NAMES`.
- [core/flow/nodes/agent.py](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) (`_build_request`), [core/flow/nodes/evaluator.py](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) (`_build_request`), [core/supervisor.py](../../../src/wastech_orchestrator/core/supervisor.py) (`_run_result`) — set `security_preamble=` from services.
- [core/flow/nodes/base.py](../../../src/wastech_orchestrator/core/flow/nodes/base.py) (`NodeServices`) + `core/flow/wiring.py` + [composition.py](../../../src/wastech_orchestrator/composition.py) — thread the resolved string; the effective-read-isolation input comes from VF-6 (`security/isolation.py` / `config/schema.py`).
- Optional operator toggle: [config/schema.py](../../../src/wastech_orchestrator/config/schema.py) `SecurityConfig` + the config validator (reject task/flow/`extra_args`).
- Docs: [docs/worc_architecture.md](../../../docs/worc_architecture.md), [.agents/rules/security.md](../../../.agents/rules/security.md) (advisory note), [docs/operations.md](../../../docs/operations.md)/`configuration.md`, packaged guide + `config.example.yaml` if a toggle lands; amend ADR [README.md](README.md) §1/§3 and add a [follow_ups.md](../follow_ups.md) entry.

### Likely tests

- `tests/providers/` — `build_effective_prompt` prepends when the field is set; ordering `preamble → prompt → footer`; `None`/empty = today's output byte-for-byte.
- `tests/core/` — the content builder: baseline always present; reinforcement present **iff** read-isolation off; the emitted path tokens equal the `runtime_layout`/`instruction_bundle` constants.
- `tests/core/` node-runner tests — agent/evaluator/supervisor requests carry the preamble; it shows up in observability + request-artifact (redacted).
- Integration (fake CLI, see the `fake-cli` skill) — the stdin fed to the fake binary **begins** with the block for agent, evaluator, and supervisor, on both providers, fresh and resumed.
- Negative — a task / `extra_args` / flow cannot alter or remove the block; the block never contains a secret value.
