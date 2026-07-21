# Backlog: agent read-isolation from orchestrator runtime data

Status: **open — design re-reviewed, implementation tasks ready** Date: 2026-07-21 Owner: Vladimir Makarevich

This decision record defines the boundary between the target repository, the current task's agent-facing exchange, and the orchestrator's private/control data. The code and the mandatory rules remain authoritative. A concrete before/after walkthrough is in [happy-path.md](happy-path.md).

## Problem confirmed against the code

The provider working directory is `repo.local_path`, while `worc_home_for(config)` resolves to `<repo.local_path>/.worc`. The same tree therefore contains both files intentionally handed to an agent and files the agent never needs:

- Agent inputs: validated task and selected skill snapshots, `plan.md`, `current.diff`, the first failing checks log, evaluator `findings.json`, generic agent/tool outputs, subtask specs, handoff briefs, memory packets, and a sanitized HITL answer packet.
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
| Critical | Mandatory security rule #11 says both blacklists apply before every run, but the current Codex adapter consumes neither `security.denied_read_paths` nor `security.denied_commands`; only Claude projects them into tool policy. | WRI-003 is a full standalone fix: denied paths enter the permission profile and denied commands enter controlled Codex execpolicy, with direct/wrapped enforcement tests. The security rule now records the current gap instead of describing it as implemented. |
| Critical | The exchange layout joined unchecked flow node ids into directories/filenames. Current private builders already do the same, while flow loading validates reserved-name collisions but not path segments. | New standalone WRI-008 rejects non-portable/path-traversing node identities before any run and adds containment checks to both private and exchange builders. |
| Critical | The plan treated gitignore plus scoped `git add` as proof that exchange files cannot be committed and ignored other mutable Git control state. | A provider can force-add an ignored file, rewrite `.git/config`/HEAD/refs, or select hooks/filters/diff drivers that later run in the orchestrator's Git process. New standalone WRI-009 fingerprints Git control state, neutralizes target-repo execution surfaces, inventories external filters, and validates the complete staged set before commits; Codex gets read-only worktree gitdir/common-dir grants. |
| Critical | A `codex sandbox` check was treated as proof of the whole Codex agent boundary. | Permission profiles constrain sandboxed local commands, but loaded allow-rules, hooks, MCP/plugins/apps, computer-use surfaces, or custom subagents can sit outside that proof. WRI-003 uses a controlled Codex home, marks the project `.codex` layer untrusted, rejects external rule layers under strict isolation, disables/inventories non-shell tool surfaces, and tests both the sandbox and resolved Codex tool/config surface before model execution. |
| Critical | The plan called the deny non-weakening without reconciling the existing operator-selectable `danger-full-access` path under `strict_isolation: false`. | The guarantee is explicit: under the default strict mode, legacy `sandbox` config is migrated/rejected and the profile must pass. An operator's deliberate full-access opt-out remains possible only under the existing unsafe-mode contract, is reported as isolation disabled, and can never be selected by a task, flow, or `extra_args`. |
| Critical | Moving the private home outside `cwd` was called a cross-provider hard guarantee. | Relocation is defense in depth only. Provider isolation must independently protect the resolved external path; moving it neither enables a sandbox nor proves its effective policy. |
| Critical | Claude was modeled as having only `Read`/`Write`/`Edit` policy and an unavoidable `Bash` residual on every host. The plan also ignored settings, hooks, MCP, plugins, custom agents, and other CLI extensions. | Current Claude has an OS-enforced Bash sandbox on macOS, Linux, and WSL2, but not native Windows. WRI-002 enables it fail-closed on supported hosts, closes non-shell configuration surfaces, and removes Bash from native-Windows strict workspace-write instead of silently weakening the guarantee. |
| Critical | The current Claude adapter and old plan assume `--permission-mode default`; installed Claude Code 2.1.210 no longer lists that mode (`manual`, `auto`, `dontAsk`, `acceptEdits`, `plan`, `bypassPermissions` are current). Fake-command tests encode the stale value. | WRI-002 revalidates the read-only/workspace-write mapping against the supported CLI, updates config/tests/docs together, and adds a real parser/capability preflight so a removed provider enum cannot reach a paid/model invocation. |
| Critical | Relocating only private runtime left live `.worc` flows, later-read role/supervisor prompts, config, and executable tools writable by the provider. A later orchestrator tool node could execute provider-replaced bytes outside the provider sandbox. | New standalone WRI-010 denies the live control plane, freezes effective prompts/tools into a private task bundle, binds later consumers to it, and detects live control mutation before downstream execution or a new task. |
| Critical | The task file and selected skills remained live writable repository paths, and WRI-003 kept Codex `AGENTS.md` discovery while only distrusting `.codex` config. An early node could rewrite instructions consumed by a later node/resume/fallback. | New standalone WRI-011 snapshots task/skill/repository instructions, disables provider-native live project instruction discovery, and injects one frozen manifest at controlled precedence across providers and sessions. |
| High | The constant supervisor was included in the routing audit, but its skill proposal still receives title/description text inline and finalize receives the task title, contradicting the repository's path-only task-content contract. Skill selection also occurs through that provider call before selected skill packages exist. | WRI-011 freezes the task and bounded skill inventory before the proposal, passes exchange paths/metadata instead of task bodies, and only then materializes the selected node packages. Adapter isolation applies to this earliest supervisor call too. |
| Critical | The exchange was described as containing only the current task, but failed/manual tasks and a retention opt-out left older task dirs in it. | WRI-007 seals the exchange on every terminal outcome, restores it only for `rerun --continue`, and adds a fail-closed next-task preflight for any stale active exchange. There is no in-repo retention escape. |
| High | Success teardown deleted the curated plan/findings/diff while claiming audit completeness was unchanged. | WRI-007 snapshots and verifies the curated exchange inside the private audit before removing the active copy. |
| High | `shutil.rmtree(ignore_errors=True)` was proposed while also requiring errors to be logged. | Cleanup records failures explicitly, handles Windows read-only/locked files, and blocks the next task if stale exchange data could remain visible. |
| High | `AgentRunRequest.human_input_path` was omitted. | Durable HITL state stays private; an answer-only redacted packet is written to the exchange and only that path is given to the provider. |
| High | The plan assumed `{checks_path}` worked during the live fix loop. | Today `ChecksNodeRunner` never assigns it after a failure; only recovery restores it. WRI-001 must publish the first failing log to the exchange and update `NodeInputs.checks_path` before taking the fail edge. |
| High | `enriched_spec` and `summary` output slots were classified as agent-facing. | Only the `plan` slot feeds an agent path. `task.enriched.md` is audit-only and `summary.md` is a publish input; both stay private. |
| High | The routing audit focused on graph agent/evaluator nodes and did not explicitly cover the constant supervisor's `AgentRunRequest`. | Provider enforcement and exchange preflight apply at the adapter boundary to agent, evaluator, and supervisor calls. Supervisor prompts/results/attempts remain private and add no exchange artifact unless a future request field explicitly needs one. |
| High | Uneven redaction was left as an open question although the exchange was called curated. | Every exchange publication is redacted at the exchange boundary. Plan, findings, checks stdout, tool output, HITL answer, and memory packet all have explicit tests. |
| High | Exchange files were readable and writable under the current workspace-write policy. | Codex profiles make the exchange read-only. Claude combines built-in `Write`/`Edit` denies with sandbox `denyWrite` on supported hosts and omits Bash from native-Windows strict mode. |
| High | Claude `Write`/`Edit` denies were treated as sufficient to preserve a curated exchange even though workspace-write Claude also had Bash. | WRI-002 adds supported-host Bash containment plus a pre/post exchange manifest; native-Windows strict mode exposes no Bash. Any observed mutation is a non-fallback policy violation and downstream nodes never consume the changed copy. |
| High | Terminal cleanup and rerun lifecycle could lose or shadow run-number fan-in. | WRI-001 defines one layout and fresh/restart/continue behavior; WRI-007 owns terminal sealing/restoration with checksums. |
| Medium | `.worc-io/<task>` vs `.worc-io/logs/<task>` was undecided. | The layout is fixed as `<repo>/.worc-io/<task-id>/`; dedicated exchange path builders do not insert `logs/`. |
| Medium | WRI-004 treated every `.worc` literal as the same concern and used a brittle grep criterion. | WRI-004 introduces a typed layout with distinct `control_home`, `private_home`, and `exchange_root`; consumers are tested by responsibility, not by banning a legitimate literal. |
| Medium | Relocating all of `.worc` created an unresolved bootstrap for `config.yaml`, `.env`, flows, and tools. | WRI-005 keeps the discoverable operator control plane under `<repo>/.worc` and relocates only private runtime state. Config is loaded first, then the external runtime path and default `.env` are resolved. |
| Medium | The plan claimed `.env` was already contained. | The environment allowlist prevents automatic propagation to child env, but it does not prevent filesystem reads. `.worc/.env` remains a primary item to deny/relocate. |
| High | The deny target was inferred only from `private_home`, but startup also accepts an explicit `--env-file` that may live anywhere. | The typed runtime policy carries resolved internal secret-source paths separately from public `security.denied_read_paths`; Claude and Codex deny the actual env-file path, and WRI-005 relocation does not pretend to move an explicit operator path. |
| Medium | The project requires three-OS support but CI currently runs only Ubuntu. | WRI-006 adds the mandatory Windows/macOS/Linux verification gate and host capability smokes without real model calls in normal CI. |
| Medium | The current task-id regex accepts Windows device names and a trailing dot even though the id becomes a directory/file component. | WRI-008 strengthens the shared task validator host-independently; invalid names are rejected, never sanitized. |
| Medium | Symlink/junction checks omitted hard links, case-insensitive collisions, and NTFS alternate data streams. | WRI-001 requires single-link regular files, canonical collision checks, and Windows named-stream enumeration/rejection; WRI-002/006 include mutation and native-OS coverage. |
| High | Path denies were specified only by textual path, without proving that a readable hard-link/alias in the workspace did not identify the same private/control file. | Internal sensitive/control sources must be single-link regular files or pass file-identity alias checks; WRI-002/003 canaries include workspace aliases, and WRI-006 covers symlink/junction/reparse/hard-link behavior natively. |
| Critical | Post-attempt manifests assumed the provider root process exiting meant all writers were gone. Current `run_process` kills the subtree only on timeout/interrupt, not after an ordinary exit. | New standalone WRI-012 owns a platform process-containment/quiescence barrier. No manifest, check, Git action, fallback, seal, or next task runs until all provider descendants are proven gone. |

## Decision

### 1. Three explicit surfaces

- **Repository workspace** — the target source tree. Workspace-write nodes may edit ordinary repo files; read-only nodes may inspect them.
- **Exchange root** — `<repo>/.worc-io/<task-id>/`, a gitignored, symlink-safe, current-task-only surface. The agent may read it but must not mutate it. It contains only redacted files actually exposed through provider paths: task/skill snapshots, `plan.md`, `current.diff`, the first failing checks log, evaluator `findings.json`, generic agent/tool outputs, subtask specs, handoff briefs, memory packets, and sanitized HITL answer packets.
- **Private/control surface** — initially `<repo>/.worc`. It contains everything else. WRI-004 distinguishes the in-repo control plane (`config.yaml`, guide, flows, tools) from private runtime state (DB, logs, memory, secrets, process-control files); WRI-010 freezes and provider-denies active control inputs, while WRI-005 may relocate only private runtime state.

The source task and tracked skills remain ordinary repository content, but `{task_path}` and selected skill paths point to immutable exchange snapshots. Applicable repository instructions and WRI-010 flow/control inputs are frozen into a private manifest and injected through controlled provider layers; live provider-native project instruction discovery is disabled. `task.enriched.md`, supervisor/publish summaries, deterministic checker JSON, durable HITL records, rendered prompts, prompt audit, and provider attempts never move to the exchange.

### 2. Exchange publication is a security boundary

Exchange writers use dedicated path builders and a single redaction/publication seam. Private writers continue to use `task_artifact_dir`/`node_run_dir`. A node run may therefore have related files in both roots, but no generic helper may silently choose a root.

The exchange directory and all parents are validated against symlinks/junctions/reparse-point escapes before use. The source task/lifecycle path, resolved default/explicit env file, and provider-owned auth/config homes join the internal deny set even when they are outside `private_home`. Stored, displayed, persisted, and asserted paths use POSIX form; provider policy emission uses the path form the target CLI/OS actually accepts and is tested through an injected platform seam.

### 3. Provider enforcement

- **Codex:** use current permission profiles. A read-only node receives repo/exchange read access with private/control paths denied. A workspace-write node receives repo write access, exchange and resolved Git control dirs read-only, and private/control paths denied. Existing `security.denied_read_paths` and `denied_commands` are projected by WRI-003. Legacy `--sandbox` and permission profiles must never be mixed. Codex runs from an orchestrator-controlled provider home with project-local `.codex` config untrusted; hooks, external rules, MCP/apps/plugins/computer use, and custom subagent configuration are disabled or fail the strict preflight. Enforcement is capability-probed on the actual host, including an indirect shell read; `strict_isolation` fails closed if either the profile or tool-surface proof fails.
- **Claude:** combine absolute internal tool denies with one adapter-owned settings policy. On macOS/Linux/WSL2, enable Claude's supported Bash sandbox fail-closed, disable unsandboxed escapes/exclusions, and project private/control read, exchange/Git write, and network rules into it. On native Windows, strict workspace-write omits Bash because that sandbox is unsupported; Edit/Write and read-only remain available. User/project/local settings, hooks, MCP, plugins, agents, skills, Chrome/IDE/remote-control, additional directories/files, and authority-bearing `extra_args` are disabled or rejected; managed policy is an explicit trusted-computing-base input and must be positively safe. Parent-held exchange/Git/control manifests remain detection in depth.

Prompt instructions may remain defense in depth, but never satisfy an enforcement acceptance criterion.

### 4. Lifecycle

Only one active exchange may exist. A stopped/crashed/parked nonterminal task retains its same-task active exchange; continue verifies and reuses it. Fresh/restart reruns archive prior private audit and start a clean exchange. Continue from a terminal resumable state restores the last sealed exchange snapshot, verifies it, and resumes. Every normal terminal outcome first crosses WRI-012's process-quiescence barrier, then seals a verified copy into private audit and removes the active exchange; a tree flagged as agent-mutated is quarantined as contaminated evidence instead and is never a restore source. A stale exchange or unproven provider subtree blocks the next agent launch until safely resolved.

## Threat model and honest guarantees

| Provider/platform | After Milestone 1 |
| --- | --- |
| Codex on macOS | OS-enforced generated permission profile, contingent on the startup canary passing. |
| Codex on Linux/WSL2 | OS-enforced generated permission profile, contingent on the startup canary passing and the host sandbox being available. |
| Codex on native Windows | Native Windows permission profile, contingent on the startup canary passing; elevated mode is preferred, unelevated behavior must be tested and reported honestly. |
| Claude on macOS | Built-in tool denies plus OS-enforced Bash sandbox, contingent on fail-closed startup and effective config/tool inventory. |
| Claude on Linux/WSL2 | Built-in tool denies plus OS-enforced Bash sandbox, contingent on dependencies, fail-closed startup, and effective config/tool inventory. |
| Claude on native Windows | Built-in tool denies; strict workspace-write omits Bash. Operator-enabled Bash is explicitly unisolated. |

WRI-005 reduces discoverability and blast radius by moving private runtime data out of the repo, but it does not itself upgrade any provider/platform row.

The matrix describes the default `strict_isolation: true` contract. The repository's existing operator-only full-access opt-out under `strict_isolation: false` deliberately disables the Codex/Claude isolation guarantee and must be labeled as such in preflight, status, and audit; task files, flows, and `extra_args` cannot select it.

## Implementation plan

| Milestone | ID | Task | Depends on |
| --- | --- | --- | --- |
| 0 | WRI-008 | [Validate portable artifact path identities](wri-008-portable-artifact-identities.md) | — |
| 0 | WRI-004 | [Introduce a typed runtime/control/exchange layout](wri-004-centralize-worc-home-seam.md) | — |
| 0 | WRI-012 | [Prove provider process-tree quiescence](wri-012-reap-provider-subtree.md) | — |
| 0 | WRI-010 | [Isolate and freeze the in-repo control plane](wri-010-protect-control-plane.md) | WRI-004, WRI-012 |
| 0 | WRI-001 | [Split private artifacts from the curated exchange](wri-001-two-root-exchange-layout.md) | WRI-004, WRI-008 |
| 0 | WRI-011 | [Freeze task, skill, and repository instruction inputs](wri-011-freeze-agent-inputs.md) | WRI-001, WRI-010 |
| 0 | WRI-009 | [Protect Git control state and commits from provider poisoning](wri-009-protect-git-index-from-exchange.md) | WRI-001, WRI-012 |
| 1 | WRI-002 | [Enforce Claude isolation with sandbox and tool policy](wri-002-claude-private-home-read-deny.md) | WRI-009, WRI-011, WRI-012 |
| 1 | WRI-003 | [Enforce the boundary with Codex permission profiles](wri-003-codex-permission-profile-isolation.md) | WRI-009, WRI-011, WRI-012 |
| 1 | WRI-007 | [Seal terminal exchanges and restore only for continue](wri-007-seal-terminal-exchange.md) | WRI-001, WRI-012 |
| 1 | WRI-006 | [Add the cross-platform isolation verification gate](wri-006-cross-platform-isolation-verification.md) | WRI-002, WRI-003, WRI-007 |
| 2 | WRI-005 | [Relocate private runtime state outside the working tree](wri-005-relocate-private-home-out-of-tree.md) | WRI-002, WRI-003, WRI-004, WRI-007, WRI-010 |

Milestone 1 is complete only when the exchange is curated and current-task-only, Codex enforcement is proven on the active host, Claude's supported-host sandbox/restricted native-Windows contract is proven without extension-surface bypasses, stale exchange data blocks future launches, and the Windows/macOS/Linux test gate is green.

Milestone 2 is defense in depth and operational separation. It does not claim that relocation itself supplies provider isolation.

## Delivery rules

- Preserve provider syntax inside provider adapters and keep the Core provider-neutral.
- Every behavior change has tests; no real model/agent calls run in the deterministic unit/integration suite.
- Host sandbox smokes run through `codex sandbox` or an equivalent no-model capability probe and record the CLI version and platform. They are paired with a no-model effective config/rules/tool-surface inventory; a sandbox smoke alone is not whole-agent proof.
- Update architecture, security, configuration, operations, functional map, and shipped `packaged/` docs in the same implementation change.
- The relevant full suite must pass on Windows, Linux, and macOS before the cluster is complete.
- No prompt warning, path obscurity, directory move, or redaction-only fallback may be reported as access-control enforcement.
