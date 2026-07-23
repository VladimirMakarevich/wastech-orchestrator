# Worked path — before and after the isolation change

Companion to the [decision record](README.md). This example follows one task through a recoverable checks failure, a fix, accepted review, documentation, publish, and terminal sealing. It is a successful end-to-end path while exercising the live `{checks_path}` contract that the current implementation misses.

## Example

| Item                                | Value                               |
| ----------------------------------- | ----------------------------------- |
| Repository and provider `cwd`       | `/work/app`                         |
| Task                                | `add-http-retry`                    |
| Task file                           | `/work/app/tasks/add-http-retry.md` |
| Flow                                | packaged `implementation`           |
| Editing provider                    | Codex, `workspace-write`, offline   |
| Review provider                     | Claude, `read-only`                 |
| Control/private home before WRI-005 | `/work/app/.worc`                   |
| Exchange                            | `/work/app/.worc-io/add-http-retry` |

The sequence is refinement → planning → implementation → checks fail → fixing → checks pass → review accepts → documentation → publish → `DONE`. The exact node ids remain flow-owned; exchange routing is based on typed artifact roles, not these example names.

## What crosses the boundary

| Artifact/path | Why an agent needs it | Destination |
| --- | --- | --- |
| Validated task and selected tracked skill snapshots | Direct task/skill input | Exchange; source files remain ordinary repository content |
| `plan.md` | `output_artifact: plan`, exposed as `{plan_path}` | Exchange |
| `current.diff` | Exposed as `{diff_path}` | Exchange |
| Redacted first failing checks log | Exposed as `{checks_path}` to `fixing` | Exchange |
| Evaluator `findings.json` | Exposed as `{review_path}` on rework/documentation paths | Exchange |
| Generic agent/tool output | Exposed only when the flow produces a downstream `{node_id_path}` | Exchange |
| Subtask spec, handoff, memory retrieval packet | Explicit downstream provider input | Exchange |
| Sanitized answer-only HITL packet | `AgentRunRequest.human_input_path` | Exchange |
| `task.enriched.md` | Refinement/audit slot; no provider input field | Private |
| Supervisor/publish `summary.md` and `summary.json` | Orchestrator publish input | Private |
| Checker JSON, rendered prompts, prompt audit, raw provider attempts | Audit/diagnostics, never context paths | Private |
| Durable HITL record and transport handle | Recovery/transport state | Private |
| State DB, flows/tools, `.env`, memory store, process control | Orchestrator control/runtime | Private/control |

Every exchange write passes through one symlink-safe, atomic redaction publisher. A file is not exchange-eligible merely because it currently lives in the task log directory. Agent-written files exist in neither root: the one legacy contract that had the agent write into `.worc` (the `security_audit` report node) migrates to the same orchestrator-captured structured output as every other slot.

## Before and active-run layout

Before the change, agent inputs and private audit share one readable subtree:

```text
/work/app/.worc/
├── .env
├── state.db
├── flows/
├── memory/
└── logs/add-http-retry/
    ├── plan.md
    ├── current.diff
    ├── checks/run-000004.log
    ├── summary.md
    ├── prompt-audit/
    └── stages/.../{rendered-prompt.md, provider attempts, outputs}
```

During an active run after WRI-001, the trees are explicit:

```text
/work/app/
├── tasks/add-http-retry.md
├── .worc-io/add-http-retry/
│   ├── task.md
│   ├── skills/...
│   ├── plan.md
│   ├── current.diff
│   ├── checks/run-000004.log
│   ├── memory/fixing.md
│   └── stages/
│       ├── implementation/run-000003/implementation.out.md
│       ├── review/run-000007/findings.json
│       └── documentation/run-000008/documentation.out.md
└── .worc/
    ├── .env
    ├── state.db
    ├── config.yaml
    ├── flows/
    ├── tools/
    ├── memory/
    └── logs/add-http-retry/
        ├── task.enriched.md
        ├── summary.md
        ├── summary.json
        ├── durable HITL/checker/audit files
        ├── prompt-audit/
        └── stages/.../{rendered-prompt.md, raw provider attempts}
```

There is no `.worc-io/logs/` segment. Related run ids may appear in both roots because one holds the curated projection and the other holds private evidence.

After WRI-005, `config.yaml`, flows, tools, and guide remain under `/work/app/.worc`; private DB/log/memory/secret/process state moves to the platform user-state location. The live control plane remains provider-denied, and later role/tool consumers use the task's frozen private control bundle. This topology reduction is defense in depth; the provider policy must still deny both roots.

## Live checks failure

The existing check runner already computes `CheckOutcome.first_failure_log`, but the live checks node does not assign `NodeInputs.checks_path`; only recovery rehydrates it. The corrected live path is:

1. Write the authoritative full check log privately.
2. Produce a redacted first-failure projection at `.worc-io/add-http-retry/checks/run-000004.log`.
3. Set `NodeInputs.checks_path` to that exchange path before returning the `fail` outcome.
4. Render the fixing prompt with the path; never inline the log body.
5. On restart/continue, resolve the same semantic input through the active or verified restored exchange.

This prevents a first-run/fresh-recovery behavioral mismatch.

## Paths in provider requests

| Variable | Before | After |
| --- | --- | --- |
| `{task_path}` | `/work/app/tasks/add-http-retry.md` | `/work/app/.worc-io/add-http-retry/task.md` |
| `{plan_path}` | `/work/app/.worc/logs/add-http-retry/plan.md` | `/work/app/.worc-io/add-http-retry/plan.md` |
| `{diff_path}` | `/work/app/.worc/logs/add-http-retry/current.diff` | `/work/app/.worc-io/add-http-retry/current.diff` |
| `{checks_path}` | `/work/app/.worc/logs/add-http-retry/checks/run-000004.log` | `/work/app/.worc-io/add-http-retry/checks/run-000004.log` |
| `{review_path}` | Private run path | Current exchange run path |
| `{memory_path}` | Private task-log packet | Current exchange packet; store remains private |
| `human_input_path` | Durable private interaction record | Sanitized exchange packet |

Selected skills similarly point to bounded immutable packages under the current exchange. The source task/skills remain ordinary repository files that a task may legitimately propose changing, but no later provider reads them as its live instructions. Applicable AGENTS/CLAUDE repository guidance is frozen privately and injected at controlled provider precedence. The provider footer continues to contain paths only. `task.enriched.md` and supervisor/publish summaries do not move because no agent request consumes them.

## Claude invocation contract

The adapter creates one private, attempt-scoped settings policy and closes the other CLI extension surfaces:

1. Deny built-in `Read` for the private-home root and internal secret sources, including dotfiles.
2. Deny built-in `Write`/`Edit` for the exchange and resolved Git control directories while keeping exchange reads available.
3. On macOS/Linux/WSL2 enable Claude's Bash sandbox with `failIfUnavailable`, private `denyRead`, exchange/Git `denyWrite`, request-derived network rules, no exclusions, and no unsandboxed-command escape.
4. On native Windows omit Bash from strict workspace-write because the current Claude sandbox does not support that host; use Edit/Write-only operation, or route to another provider through the pre-model `CAPABILITY_UNAVAILABLE` infrastructure classification — never silent unsandboxed execution.
5. Disable user/project/local customizations and MCP, and disable or inventory settings, hooks, plugins, agents, skills, Chrome/IDE/remote-control, and managed-policy surfaces before launch.
6. Reject `extra_args` that replace or extend the owned tools/settings/permission/workspace/session authority.

These controls apply to fresh/resume and agent/evaluator/supervisor attempts. Claude's OS sandbox covers Bash and child processes only; the built-in tool policy and minimized configuration cover the remaining admitted tool surface. Enterprise managed policy cannot be overridden by argv and is therefore an explicit trusted-computing-base input that must be positively shown safe.

The orchestrator also hashes exchange and Git control state into parent-process memory immediately before an attempt and verifies it afterward. A mutation is rejected as a policy violation before any downstream node can read it; the changed tree is quarantined as evidence and cannot be used for continue unless an independent clean snapshot exists. This is detection in depth, not a substitute for either built-in tool policy or Bash containment.

## Codex invocation contract

The current provider shape is conceptually:

```text
codex exec --sandbox workspace-write \
  -c sandbox_workspace_write.network_access=false ...
```

That shape must be replaced, not extended. Permission profiles do not compose with legacy `--sandbox`/`sandbox_mode`; any loaded legacy setting makes Codex use the old policy instead. The new adapter therefore:

1. Generates an attempt-scoped profile with `:minimal`, `:workspace_roots`, exact private/exchange rules, configured denies, and the resolved network grant.
2. Keeps authentication and session state in the operator's `CODEX_HOME` (credentials stay outside the orchestrator) while denying sandboxed commands read access to that home; operator-home rule/hook layers that could authorize a bypass fail the strict preflight. (`denied_commands` runtime projection into a Codex execpolicy layer was descoped — the commands are already contained without it; see [follow_ups](../follow_ups.md).) A dedicated orchestrator-controlled Codex home is deferred ([archived task](../archive/codex-controlled-provider-home.md)).
3. Selects the profile with `default_permissions`, uses `--ignore-user-config` when supported, forces the project `.codex` layer to `untrusted`, disables live project-doc discovery, and injects the frozen repository instruction manifest.
4. Disables hooks and custom subagents and disables or positively inventories MCP/apps/plugins/computer-use surfaces.
5. Passes neither `--sandbox` nor `sandbox_workspace_write.*` and rejects authority-bearing `extra_args`.
6. Runs the exact effective profile through a no-model `codex sandbox` canary, then inspects effective rules/config/features/MCP inventory.
7. Launches the model only after both the filesystem controls and the non-shell tool-surface proof pass.

The filesystem policy is:

| Path                       | Read-only node | Workspace-write node |
| -------------------------- | -------------- | -------------------- |
| Runtime/tool minimum       | Read           | Read                 |
| Repository workspace       | Read           | Write                |
| Resolved gitdir/common dir | Read           | Read                 |
| Current exchange           | Read           | Read                 |
| Live control home          | Deny           | Deny                 |
| Private home               | Deny           | Deny                 |
| Source task/lifecycle path | Read           | Read                 |
| Configured sensitive paths | Deny           | Deny                 |

More-specific exchange `read` overrides the broader workspace `write`; `deny` wins on the private paths. Exact native paths are emitted on each host. Unbounded deny globs are bounded or rejected where Codex needs pre-expansion. The "Source task/lifecycle path" rows cover the entire `tasks/` lifecycle tree: providers may read it, but writes anywhere in it are denied, so a node can neither corrupt lifecycle bookkeeping nor inject new task files for the daemon to pick up. Workspace-write Codex nodes also keep the existing hard rule: no network grant — network is resolvable only for read-only Codex nodes.

Current Codex supports permission profiles on macOS, Linux, WSL, and native Windows. The orchestrator does not generate Seatbelt/Landlock/ACL policy and does not automatically fail Windows. It records capability evidence and fails strict isolation if either the requested effective policy or the minimized Codex tool/config surface cannot be demonstrated on that host/CLI.

## Terminal lifecycle

After any terminal outcome, not only success:

1. Close the provider process containment and prove no background/reparented descendant remains.
2. Build and verify a manifest of the active exchange.
3. Move/copy it into the private task audit using the WRI-007 cross-platform protocol.
4. Remove the active `.worc-io/add-http-retry` directory.
5. Refuse the next provider launch if any subtree is unproven or any stale/foreign active exchange remains.

`rerun --continue` from a terminal resumable status restores only the same task's verified latest snapshot. A parked/crashed nonterminal task verifies and reuses its already-active same-task exchange. Fresh/restart-in-place (`rerun`) starts clean; the daemon `restart` command is unrelated to this lifecycle. A retention toggle cannot expose an old terminal task to a new agent.

An exchange already flagged as Claude-mutated does not follow the clean-seal branch: it is quarantined with expected/observed manifests as contaminated evidence and is never eligible for restore.

## End-to-end assertions

- Every provider orchestration input path is inside the current exchange; only `repo_path` names the live workspace itself.
- Live `.worc` control files are provider-denied; later prompts/tools come from the verified private task bundle and cannot be swapped by an earlier node.
- The live fixing node receives `{checks_path}` before any recovery cycle.
- Every exchange artifact is redacted; unredactable content remains private and receives a sanitized projection if the flow needs it.
- Codex enforcement is proven on the effective generated profile plus the effective rules/config/tool inventory, not inferred from argv, `codex sandbox` alone, or fake-CLI output.
- Claude uses built-in tool denies plus the supported Bash sandbox on macOS/Linux/WSL2; native-Windows strict workspace-write omits Bash.
- Claude extension/configuration surfaces are minimized and inventoried; managed policy is explicitly inside the host trust boundary.
- Claude exchange/Git integrity is checked pre/post attempt; detection is not mislabeled as containment.
- No post-attempt manifest, check, Git command, seal, fallback, or next task runs before provider process-tree quiescence is proven.
- Windows paths, junctions/reparse points, locks, Codex native sandbox behavior, Claude's native-Windows restricted branch, and macOS/Linux/WSL2 behavior are all tested.
- Terminal exchange data is preserved privately, checksum-verified, and absent from the next task's readable surface.
- Neither `.worc/` nor `.worc-io/` can be staged or committed.
- The provider cannot mutate the Git index undetected, and every orchestrator commit validates the complete staged set rather than trusting its own scoped `git add` call.
