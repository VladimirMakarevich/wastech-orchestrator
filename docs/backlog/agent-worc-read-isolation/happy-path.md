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
| Tracked task and skill files | Direct task/skill input | Repository; unchanged |
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

Every exchange write passes through one symlink-safe, atomic redaction publisher. A file is not exchange-eligible merely because it currently lives in the task log directory.

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

After WRI-005, `config.yaml`, flows, tools, and guide remain under `/work/app/.worc`; private DB/log/memory/secret/process state moves to the platform user-state location. This topology reduction does not turn the Claude `Bash` residual into a hard deny.

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
| `{task_path}` | `/work/app/tasks/add-http-retry.md` | Unchanged |
| `{plan_path}` | `/work/app/.worc/logs/add-http-retry/plan.md` | `/work/app/.worc-io/add-http-retry/plan.md` |
| `{diff_path}` | `/work/app/.worc/logs/add-http-retry/current.diff` | `/work/app/.worc-io/add-http-retry/current.diff` |
| `{checks_path}` | `/work/app/.worc/logs/add-http-retry/checks/run-000004.log` | `/work/app/.worc-io/add-http-retry/checks/run-000004.log` |
| `{review_path}` | Private run path | Current exchange run path |
| `{memory_path}` | Private task-log packet | Current exchange packet; store remains private |
| `human_input_path` | Durable private interaction record | Sanitized exchange packet |

The provider footer continues to contain paths only. `task.enriched.md` and supervisor/publish summaries do not move because no agent request consumes them.

## Claude invocation contract

The adapter adds orchestrator-owned, absolute platform-correct tool rules:

- Deny `Read` for the private-home root and descendants, including dotfiles.
- Deny `Write` and `Edit` for the exchange root and descendants.
- Keep exchange `Read` available.
- Reject `extra_args` that replace the owned tools/settings/permission/workspace authority.

These rules apply to fresh/resume and read-only/workspace-write attempts. They are Claude tool policy, not an OS sandbox: workspace-write `Bash` is outside the claim.

## Codex invocation contract

The current provider shape is conceptually:

```text
codex exec --sandbox workspace-write \
  -c sandbox_workspace_write.network_access=false ...
```

That shape must be replaced, not extended. Permission profiles do not compose with legacy `--sandbox`/`sandbox_mode`; any loaded legacy setting makes Codex use the old policy instead. The new adapter therefore:

1. Generates an attempt-scoped profile with `:minimal`, `:workspace_roots`, exact private/exchange rules, configured denies, and the resolved network grant.
2. Uses a private, orchestrator-controlled Codex home for auth/session state and generated `denied_commands` execpolicy; it does not silently copy credentials.
3. Selects the profile with `default_permissions`, uses `--ignore-user-config` when supported, and forces the project `.codex` layer to `untrusted`.
4. Disables hooks and custom subagents and disables or positively inventories MCP/apps/plugins/computer-use surfaces.
5. Passes neither `--sandbox` nor `sandbox_workspace_write.*` and rejects authority-bearing `extra_args`.
6. Runs the exact effective profile through a no-model `codex sandbox` canary, then inspects effective rules/config/features/MCP inventory.
7. Launches the model only after both the filesystem controls and the non-shell tool-surface proof pass.

The filesystem policy is:

| Path                       | Read-only node | Workspace-write node |
| -------------------------- | -------------- | -------------------- |
| Runtime/tool minimum       | Read           | Read                 |
| Repository workspace       | Read           | Write                |
| Current exchange           | Read           | Read                 |
| Private home               | Deny           | Deny                 |
| Configured sensitive paths | Deny           | Deny                 |

More-specific exchange `read` overrides the broader workspace `write`; `deny` wins on the private paths. Exact native paths are emitted on each host. Unbounded deny globs are bounded or rejected where Codex needs pre-expansion.

Current Codex supports permission profiles on macOS, Linux, WSL, and native Windows. The orchestrator does not generate Seatbelt/Landlock/ACL policy and does not automatically fail Windows. It records capability evidence and fails strict isolation if either the requested effective policy or the minimized Codex tool/config surface cannot be demonstrated on that host/CLI.

## Terminal lifecycle

After any terminal outcome, not only success:

1. Build and verify a manifest of the active exchange.
2. Move/copy it into the private task audit using the WRI-007 cross-platform protocol.
3. Remove the active `.worc-io/add-http-retry` directory.
4. Refuse the next provider launch if any stale/foreign active exchange remains.

`rerun --continue` restores only the same task's verified latest snapshot. Fresh/restart starts clean. A retention toggle cannot expose an old terminal task to a new agent.

## End-to-end assertions

- Every provider orchestration path is either a tracked repository/task/skill path or inside the current exchange.
- The live fixing node receives `{checks_path}` before any recovery cycle.
- Every exchange artifact is redacted; unredactable content remains private and receives a sanitized projection if the flow needs it.
- Codex enforcement is proven on the effective generated profile plus the effective rules/config/tool inventory, not inferred from argv, `codex sandbox` alone, or fake-CLI output.
- Claude's guarantee is limited to its Read/Write/Edit tool policy; `Bash` remains explicit.
- Windows paths, junctions/reparse points, locks, permission-profile behavior, and native sandbox mode are tested alongside macOS/Linux behavior.
- Terminal exchange data is preserved privately, checksum-verified, and absent from the next task's readable surface.
- Neither `.worc/` nor `.worc-io/` can be staged or committed.
