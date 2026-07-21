# Backlog: agent read-isolation from orchestrator runtime data

Status: **open — design re-reviewed, implementation tasks ready** Date: 2026-07-21 Owner: Vladimir Makarevich

This decision record defines the boundary between the target repository, the current task's agent-facing exchange, and the orchestrator's private/control data. The code and the mandatory rules remain authoritative. A concrete before/after walkthrough is in [happy-path.md](happy-path.md).

## Problem confirmed against the code

The provider working directory is `repo.local_path`, while `worc_home_for(config)` resolves to `<repo.local_path>/.worc`. The same tree therefore contains both files intentionally handed to an agent and files the agent never needs:

- Agent inputs: `plan.md`, `current.diff`, the first failing checks log, evaluator `findings.json`, generic agent/tool outputs, subtask specs, handoff briefs, memory packets, and a sanitized HITL answer packet.
- Private/control data: `.env`, `state.db`, editable flows and role prompts, tool executables, memory store, security reports, rendered prompts, prompt audit, provider-attempt streams, durable HITL state, supervisor output, and other tasks' audit data.

The path-only prompt contract is intentional: downstream nodes receive paths, not inlined artifact bodies. A blanket deny of `.worc` would therefore also deny required inputs until those inputs are separated.

## Review findings and corrections

| Severity | Finding in the previous plan | Correction |
| --- | --- | --- |
| Critical | It said Codex had no per-path deny and no Windows sandbox. | Current Codex supports permission profiles with filesystem `read`/`write`/`deny` rules on macOS, Linux/WSL, and native Windows. The installed CLI is 0.144.4 and a local `codex sandbox` smoke denied a carved-out subtree. WRI-003 now uses that supported surface. |
| Critical | It proposed hand-generating Seatbelt/Landlock policy and failing every Windows run. | Do not reimplement Codex's platform sandboxes. Generate a Codex permission profile, feature-probe it, and fail closed only on a host/CLI where the requested rule cannot be demonstrated. Native Windows is a supported target, not an automatic failure. |
| Critical | It kept passing legacy `--sandbox` while planning to add permission profiles. | Codex permission profiles do not compose with legacy `sandbox_mode`/`--sandbox`. WRI-003 replaces that argv/config path, covers network policy in the generated profile, and checks fresh/resume parity. |
| Critical | Non-weakening was asserted without closing `extra_args`/config overrides. | WRI-003 reserves authority-bearing Codex flags/config keys, isolates user config where supported, validates all orchestrator-controlled layers, and runs an effective-policy canary after every remaining Codex config layer has been applied. |
| Critical | The plan depended on CODX-001/002/003 and linked `codex-provider-improvements/p0-codx-003-enforce-deny-policy.md`, but none of those tasks exists in the tracked backlog. | WRI-003 now owns the complete Codex authority-validation, denied-path projection, network, capability-probe, and error-routing contract; the broken dependencies and link are removed. |
| Critical | A `codex sandbox` check was treated as proof of the whole Codex agent boundary. | Permission profiles constrain sandboxed local commands, but loaded allow-rules, hooks, MCP/plugins/apps, computer-use surfaces, or custom subagents can sit outside that proof. WRI-003 uses a controlled Codex home, marks the project `.codex` layer untrusted, rejects external rule layers under strict isolation, disables/inventories non-shell tool surfaces, and tests both the sandbox and resolved Codex tool/config surface before model execution. |
| Critical | Moving the private home outside `cwd` was called a cross-provider hard guarantee. | Relocation is defense in depth only. It does not stop a Claude `Bash` command that knows an absolute path. Claude remains tool-policy-limited until a separate OS-level Claude containment design exists. |
| Critical | The exchange was described as containing only the current task, but failed/manual tasks and a retention opt-out left older task dirs in it. | WRI-007 seals the exchange on every terminal outcome, restores it only for `rerun --continue`, and adds a fail-closed next-task preflight for any stale active exchange. There is no in-repo retention escape. |
| High | Success teardown deleted the curated plan/findings/diff while claiming audit completeness was unchanged. | WRI-007 snapshots and verifies the curated exchange inside the private audit before removing the active copy. |
| High | `shutil.rmtree(ignore_errors=True)` was proposed while also requiring errors to be logged. | Cleanup records failures explicitly, handles Windows read-only/locked files, and blocks the next task if stale exchange data could remain visible. |
| High | `AgentRunRequest.human_input_path` was omitted. | Durable HITL state stays private; an answer-only redacted packet is written to the exchange and only that path is given to the provider. |
| High | The plan assumed `{checks_path}` worked during the live fix loop. | Today `ChecksNodeRunner` never assigns it after a failure; only recovery restores it. WRI-001 must publish the first failing log to the exchange and update `NodeInputs.checks_path` before taking the fail edge. |
| High | `enriched_spec` and `summary` output slots were classified as agent-facing. | Only the `plan` slot feeds an agent path. `task.enriched.md` is audit-only and `summary.md` is a publish input; both stay private. |
| High | Uneven redaction was left as an open question although the exchange was called curated. | Every exchange publication is redacted at the exchange boundary. Plan, findings, checks stdout, tool output, HITL answer, and memory packet all have explicit tests. |
| High | Exchange files were readable and writable under the current workspace-write policy. | Codex profiles make the exchange read-only. Claude gets internal `Write`/`Edit` denies in addition to the private-home `Read` deny; its remaining `Bash` limitation is stated explicitly. |
| High | Terminal cleanup and rerun lifecycle could lose or shadow run-number fan-in. | WRI-001 defines one layout and fresh/restart/continue behavior; WRI-007 owns terminal sealing/restoration with checksums. |
| Medium | `.worc-io/<task>` vs `.worc-io/logs/<task>` was undecided. | The layout is fixed as `<repo>/.worc-io/<task-id>/`; dedicated exchange path builders do not insert `logs/`. |
| Medium | WRI-004 treated every `.worc` literal as the same concern and used a brittle grep criterion. | WRI-004 introduces a typed layout with distinct `control_home`, `private_home`, and `exchange_root`; consumers are tested by responsibility, not by banning a legitimate literal. |
| Medium | Relocating all of `.worc` created an unresolved bootstrap for `config.yaml`, `.env`, flows, and tools. | WRI-005 keeps the discoverable operator control plane under `<repo>/.worc` and relocates only private runtime state. Config is loaded first, then the external runtime path and default `.env` are resolved. |
| Medium | The plan claimed `.env` was already contained. | The environment allowlist prevents automatic propagation to child env, but it does not prevent filesystem reads. `.worc/.env` remains a primary item to deny/relocate. |
| Medium | The project requires three-OS support but CI currently runs only Ubuntu. | WRI-006 adds the mandatory Windows/macOS/Linux verification gate and host capability smokes without real model calls in normal CI. |

## Decision

### 1. Three explicit surfaces

- **Repository workspace** — the target source tree. Workspace-write nodes may edit ordinary repo files; read-only nodes may inspect them.
- **Exchange root** — `<repo>/.worc-io/<task-id>/`, a gitignored, symlink-safe, current-task-only surface. The agent may read it but must not mutate it. It contains only redacted files actually exposed through provider paths: `plan.md`, `current.diff`, the first failing checks log, evaluator `findings.json`, generic agent/tool outputs, subtask specs, handoff briefs, memory packets, and sanitized HITL answer packets.
- **Private/control surface** — initially `<repo>/.worc`. It contains everything else. WRI-004 distinguishes the in-repo control plane (`config.yaml`, guide, flows, tools) from private runtime state (DB, logs, memory, secrets, process-control files); WRI-005 may relocate only the latter.

`{task_path}` and tracked skill references remain ordinary repository paths. `task.enriched.md`, supervisor/publish summaries, deterministic checker JSON, durable HITL records, rendered prompts, prompt audit, and provider attempts never move to the exchange.

### 2. Exchange publication is a security boundary

Exchange writers use dedicated path builders and a single redaction/publication seam. Private writers continue to use `task_artifact_dir`/`node_run_dir`. A node run may therefore have related files in both roots, but no generic helper may silently choose a root.

The exchange directory and all parents are validated against symlinks/junctions/reparse-point escapes before use. Stored, displayed, persisted, and asserted paths use POSIX form; provider policy emission uses the path form the target CLI/OS actually accepts and is tested through an injected platform seam.

### 3. Provider enforcement

- **Codex:** use current permission profiles. A read-only node receives repo/exchange read access with private paths denied. A workspace-write node receives repo write access, exchange read-only access, and private paths denied. Existing `security.denied_read_paths` and `denied_commands` are projected by WRI-003. Legacy `--sandbox` and permission profiles must never be mixed. Codex runs from an orchestrator-controlled provider home with project-local `.codex` config untrusted; hooks, external rules, MCP/apps/plugins/computer use, and custom subagent configuration are disabled or fail the strict preflight. Enforcement is capability-probed on the actual host, including an indirect shell read; `strict_isolation` fails closed if either the profile or tool-surface proof fails.
- **Claude:** append absolute internal `Read(private_home)` denies plus `Write(exchange_root)`/`Edit(exchange_root)` denies. These are tool-level controls. They do not stop `Bash` from reading private data or mutating exchange files, so Phase 1 is not a hard Claude filesystem boundary.

Prompt instructions may remain defense in depth, but never satisfy an enforcement acceptance criterion.

### 4. Lifecycle

Only one active exchange may exist. Fresh/restart reruns archive prior private audit and start a clean exchange. Continue restores the last sealed exchange snapshot, verifies it, and resumes. Every terminal outcome seals a verified copy into private audit and removes the active exchange. A stale exchange blocks the next agent launch until it is safely sealed or explicitly cleaned.

## Threat model and honest guarantees

| Provider/platform | After Milestone 1 |
| --- | --- |
| Codex on macOS | OS-enforced generated permission profile, contingent on the startup canary passing. |
| Codex on Linux/WSL2 | OS-enforced generated permission profile, contingent on the startup canary passing and the host sandbox being available. |
| Codex on native Windows | Native Windows permission profile, contingent on the startup canary passing; elevated mode is preferred, unelevated behavior must be tested and reported honestly. |
| Claude on all OSes | Built-in Read/Write/Edit tool denial only; `Bash` remains outside this guarantee. |

WRI-005 reduces discoverability and blast radius by moving private runtime data out of the repo, but it does not upgrade the Claude row to a hard guarantee.

## Implementation plan

| Milestone | ID | Task | Depends on |
| --- | --- | --- | --- |
| 0 | WRI-004 | [Introduce a typed runtime/control/exchange layout](wri-004-centralize-worc-home-seam.md) | — |
| 0 | WRI-001 | [Split private artifacts from the curated exchange](wri-001-two-root-exchange-layout.md) | WRI-004 |
| 1 | WRI-002 | [Apply Claude private-read and exchange-write tool denies](wri-002-claude-private-home-read-deny.md) | WRI-001 |
| 1 | WRI-003 | [Enforce the boundary with Codex permission profiles](wri-003-codex-permission-profile-isolation.md) | WRI-001 |
| 1 | WRI-007 | [Seal terminal exchanges and restore only for continue](wri-007-seal-terminal-exchange.md) | WRI-001 |
| 1 | WRI-006 | [Add the cross-platform isolation verification gate](wri-006-cross-platform-isolation-verification.md) | WRI-002/003/007 |
| 2 | WRI-005 | [Relocate private runtime state outside the working tree](wri-005-relocate-private-home-out-of-tree.md) | WRI-002/003/004/007 |

Milestone 1 is complete only when the exchange is curated and current-task-only, Codex enforcement is proven on the active host, Claude's narrower guarantee is labeled accurately, stale exchange data blocks future launches, and the Windows/macOS/Linux test gate is green.

Milestone 2 is defense in depth and operational separation. It does not claim hard Claude `Bash` isolation.

## Delivery rules

- Preserve provider syntax inside provider adapters and keep the Core provider-neutral.
- Every behavior change has tests; no real model/agent calls run in the deterministic unit/integration suite.
- Host sandbox smokes run through `codex sandbox` or an equivalent no-model capability probe and record the CLI version and platform. They are paired with a no-model effective config/rules/tool-surface inventory; a sandbox smoke alone is not whole-agent proof.
- Update architecture, security, configuration, operations, functional map, and shipped `packaged/` docs in the same implementation change.
- The relevant full suite must pass on Windows, Linux, and macOS before the cluster is complete.
- No prompt warning, path obscurity, directory move, or redaction-only fallback may be reported as access-control enforcement.
