# Happy path — worked example (before / after)

Companion to the [decision record](README.md). This traces **one** task run end to end under the two-root design so every concrete change is visible, and verifies that the agent still receives every input it needs while the private home becomes unreachable. All paths, node ids and file names below are concrete.

## Example setup

| Thing | Value |
| --- | --- |
| Repo working tree (agent `cwd`) | `/work/app` |
| Task id | `add-http-retry` |
| Task branch | `worc/add-http-retry` |
| Task file (tracked, outside both homes) | `/work/app/tasks/add-http-retry.md` |
| Flow | packaged `implementation`: refinement → planning → implementation → testing → review → documentation → publish (fix loops through `fixing`) |
| `implementation` provider | `codex` (`workspace-write`) |
| `review` provider | `claude` (read-only profile) |
| **`private_home`** (today) | `/work/app/.worc/` |
| **`exchange_root`** (new) | `/work/app/.worc-io/add-http-retry/` (layout note: if the artifact-dir builders are reused, this becomes `.worc-io/logs/add-http-retry/`; WRI-001 must reconcile) |

Happy path = no rework: `review` **accepts**, so `fixing` is skipped; `documentation` then runs (an ordinary workspace-write agent that resumes the implementation lineage and updates the target project's docs — its edits join the same diff), and `publish` commits/PRs.

## The node sequence and where each output goes

| Node | Reads (path variables) | Produces | Agent-facing copy → `exchange` | Audit/secret copy → `private_home` |
| --- | --- | --- | --- | --- |
| refinement | `{task_path}` | `task.enriched.md` (audit-only slot, no variable) | — | `task.enriched.md` |
| planning | `{task_path}` | `plan.md` (`output_artifact: plan`, task root) | `plan.md` | rendered prompt + raw provider I/O |
| implementation | `{task_path}`, `{plan_path}`, `{memory_path}` | edits + `implementation.out.md` (run dir) + `current.diff` (task root) | `implementation.out.md`, `current.diff` | rendered prompt + raw provider I/O |
| testing / checks | `{diff_path}` | checks report (task root) | `checks/…` report | check logs, raw I/O |
| review | `{task_path}`, `{plan_path}`, `{diff_path}`, `{memory_path}` | `findings.json` + evaluator `summary.md` | `findings.json` (`{review_path}`) | evaluator `summary.md`, rendered prompt + raw provider I/O |
| documentation | `{task_path}`, `{plan_path}`, `{diff_path}`, `{review_path}` | doc edits (join the diff) + `documentation.out.md` (run dir) | `documentation.out.md`, updated `current.diff` | rendered prompt + raw provider I/O |
| publish | (orchestrator-only; no agent) | commit / push / PR from `summary.md` | — | `summary.md`, `summary.json` (PR body) |

Rule of thumb: **only the artifacts a downstream node is pointed at by a `{…_path}` variable go to `exchange`; everything else — rendered prompts, prompt-audit, raw provider streams, the PR-body summary, and all the standing stores — stays in `private_home`.**

```mermaid
flowchart TD
    REF["refinement"] --> PLAN["planning"] --> IMPL["implementation"] --> TEST["testing / checks"] --> REV["review"] --> DOC["documentation"] --> PUB["publish"]

    PLAN -->|"plan.md"| EX
    IMPL -->|"implementation.out.md + current.diff"| EX
    TEST -->|"checks report"| EX
    REV -->|"findings.json"| EX
    DOC -->|"documentation.out.md + updated current.diff"| EX

    REF -->|"task.enriched.md (audit)"| PH
    REV -->|"evaluator summary.md (audit)"| PH
    SUP["supervisor layer (above the flow)"] -->|"summary.md / summary.json (PR body)"| PH
    PUB -->|"reads the supervisor summary.md, then commits and PRs"| PH

    EX["exchange_root<br/>.worc-io/add-http-retry/<br/>AGENT READS THIS"]
    PH[".worc/ private_home<br/>HIDDEN FROM AGENT<br/>also holds every node's rendered-prompt.md and raw provider I/O"]
```

Note: the PR-body `summary.md` is written by the constant **supervisor layer** at whole-task close (before publish), not by the `review` node — `review`'s own `summary.md` is the evaluator's audit summary inside its run dir.

On **successful** completion (`Status.DONE`, after `publish`), the task's exchange dir `.worc-io/add-http-retry/` is torn down automatically (WRI-007) — its curated results are no longer needed by any node, and leaving them would clutter the next task's agent read surface. The private home `.worc/logs/add-http-retry/` (the whole audit trail) is retained. A `FAILED` or `manual_action_required` outcome keeps the exchange for debugging/resume.

## Before / after — the on-disk artifact tree for this run

**BEFORE** — one tree; the agent (its `cwd` is `/work/app`) can read all of it:

```
/work/app/
├── tasks/add-http-retry.md                       # tracked, {task_path}
└── .worc/                                         # gitignored — but fully readable by the agent
    ├── .env                                       # real tokens
    ├── state.db                                   # state + raw session ids + unredacted findings
    ├── flows/implementation/roles/*.md            # system prompts (own + other nodes')
    ├── memory/…                                   # cross-task memory store
    └── logs/add-http-retry/
        ├── task.enriched.md
        ├── plan.md                                # {plan_path}  (task-root output_artifact slot)
        ├── current.diff                           # {diff_path}  (task-root, NOT under a run dir)
        ├── summary.md  summary.json               # PR body (supervisor layer, at whole-task close)
        ├── checks/run-000004.log                  # {checks_path} (latest FAILED check log; may be absent on a clean run)
        ├── memory/implementation.md               # {memory_path} packet (store lives at .worc/memory/)
        ├── prompt-audit/…                         # full prompts
        └── stages/
            ├── planning/run-000002/{rendered-prompt.md, 1-codex/…}
            ├── implementation/run-000003/
            │   ├── implementation.out.md          # {implementation_path}
            │   ├── rendered-prompt.md
            │   └── 1-codex/{request.json,stdout.log,stderr.log,events.jsonl,result.json}
            ├── review/run-000005/
            │   ├── findings.json                  # {review_path}
            │   ├── summary.md                      # evaluator audit summary
            │   ├── rendered-prompt.md
            │   └── 1-claude/…
            └── documentation/run-000006/
                ├── documentation.out.md           # {documentation_path}
                ├── rendered-prompt.md
                └── 1-codex/…
```

**AFTER** — the same run, split across two roots. The agent-facing files move to `.worc-io/`; the audit/secret files stay in `.worc/`. Note that `run-000003/` now exists in **both** trees:

```
/work/app/
├── tasks/add-http-retry.md                       # tracked, {task_path} — UNCHANGED
│
├── .worc-io/add-http-retry/                       # NEW gitignored root — the ONLY ".worc*" the agent reads
│   ├── plan.md                                    # {plan_path}  (task-root slot)
│   ├── current.diff                               # {diff_path}  (task-root, NOT under a run dir)
│   ├── checks/run-000004.log                      # {checks_path}
│   ├── memory/implementation.md                   # {memory_path} — retrieval packet only (redacted at store-ingest)
│   └── stages/
│       ├── implementation/run-000003/
│       │   └── implementation.out.md              # {implementation_path}
│       ├── review/run-000005/
│       │   └── findings.json                      # {review_path}
│       └── documentation/run-000006/
│           └── documentation.out.md               # {documentation_path}
│
└── .worc/                                         # private_home — DENIED / unreachable to the agent
    ├── .env  state.db  flows/  memory/  security-reports/
    └── logs/add-http-retry/
        ├── task.enriched.md
        ├── summary.md  summary.json               # PR body (orchestrator reads it, not the agent)
        ├── prompt-audit/…
        └── stages/
            ├── planning/run-000002/{rendered-prompt.md, 1-codex/…}
            ├── implementation/run-000003/{rendered-prompt.md, 1-codex/…}
            ├── review/run-000005/{rendered-prompt.md, summary.md, 1-claude/…}
            └── documentation/run-000006/{rendered-prompt.md, 1-codex/…}
```

Note: the task-root files (`plan.md`, `current.diff`, `checks/`, `memory/` packet) move to the exchange task root, while `task.enriched.md`, `prompt-audit/`, the supervisor `summary.md`, and every `rendered-prompt.md` / raw-provider dir stay in the private home. `run-000003/` therefore exists in **both** trees.

The audit trail is **not weakened**: `rendered-prompt.md`, `prompt-audit/`, the `1-codex/` / `1-claude/` raw streams, `state.db` and the PR-body `summary.md` are all still written — they just stop being agent-readable.

## Before / after — the paths handed to the agent

Nothing about _how_ context is delivered changes: the orchestrator still injects **paths**, never inlined content (the path-only prompt invariant holds). Only the destination flips from `.worc/logs/` to `.worc-io/`.

| Variable | BEFORE | AFTER |
| --- | --- | --- |
| `{task_path}` | `/work/app/tasks/add-http-retry.md` | _unchanged_ |
| `{plan_path}` | `.worc/logs/add-http-retry/plan.md` | `.worc-io/add-http-retry/plan.md` |
| `{diff_path}` | `.worc/logs/add-http-retry/current.diff` (task root) | `.worc-io/add-http-retry/current.diff` (task root) |
| `{checks_path}` | `.worc/logs/add-http-retry/checks/run-000004.log` | `.worc-io/add-http-retry/checks/run-000004.log` |
| `{implementation_path}` | `.worc/logs/add-http-retry/stages/implementation/run-000003/implementation.out.md` | `.worc-io/add-http-retry/stages/implementation/run-000003/implementation.out.md` |
| `{review_path}` | `.worc/logs/add-http-retry/stages/review/run-000005/findings.json` | `.worc-io/add-http-retry/stages/review/run-000005/findings.json` |
| `{memory_path}` | packet under `.worc/logs/add-http-retry/memory/<node>.md` | `.worc-io/add-http-retry/memory/<node>.md` (packet only; the `memory/` **store** stays private) |

Concrete "Context files" footer the `review` node's prompt ends with:

```diff
  Context files (read them as needed; do not assume their contents):
  - task: /work/app/tasks/add-http-retry.md
- - plan: /work/app/.worc/logs/add-http-retry/plan.md
- - diff: /work/app/.worc/logs/add-http-retry/current.diff
+ - plan: /work/app/.worc-io/add-http-retry/plan.md
+ - diff: /work/app/.worc-io/add-http-retry/current.diff
```

## Before / after — the provider invocation

**`review` on Claude.** A dedicated internal deny of the private home is appended to `--disallowedTools` (it is _not_ routed through the overloaded `security.denied_read_paths`). The exchange lives at `.worc-io/`, a sibling path, so the private-home deny glob does not touch it.

The deny must be **absolute-anchored**, not a bare relative `.worc/**`. The paths Claude is handed in the "Context files" footer carry the full `/work/app/.worc/…` prefix (they are rooted at `repo.local_path`), and `_deny_read_tools_for` forwards patterns verbatim with no anchoring — so a relative `Read(.worc/**)` would not match the path the tool is invoked with. Use the same `//`-anchored absolute form `_native_memory_deny_tools` already emits for `~/.claude`:

```diff
  claude -p --output-format stream-json --verbose \
    --permission-mode default \
    --allowedTools Read,Glob,Grep \
-   --disallowedTools "Read(.env),Read(secrets/**),Read(~/.claude/**),Write(~/.claude/**),Edit(~/.claude/**)"
+   --disallowedTools "Read(.env),Read(secrets/**),Read(//work/app/.worc),Read(//work/app/.worc/**),Read(~/.claude/**),Write(~/.claude/**),Edit(~/.claude/**)"
```

(Confirm the `/**` also matches the hidden `/work/app/.worc/.env`; if the matcher skips leading-dot entries, add an explicit dotfile deny — see WRI-002.)

**`implementation` on Codex.** Phase 1 cannot enforce a per-path read deny (Codex's OS sandbox governs writes/network, not reads), so the argv is **unchanged** — the isolation in Phase 1 is only that the agent is handed `.worc-io/` paths and a role-prompt hygiene note. Real enforcement arrives in Phase 2.

```text
# Phase 1 argv — identical before and after (obscurity only, no read enforcement):
codex --ask-for-approval never exec --cd /work/app --sandbox workspace-write --json \
      --output-last-message /work/app/.worc/logs/add-http-retry/stages/implementation/run-000003/1-codex/last-message.txt -

# Phase 2 — a generated OS-sandbox profile denies reads of the (now out-of-tree) private home:
#   macOS Seatbelt : (deny file-read* (subpath "<private_home>"))
#   Linux Landlock : no read handle granted for "<private_home>"
#   Windows        : no OS sandbox -> fail preflight under strict_isolation
```

## The read-access boundary

```mermaid
flowchart LR
    subgraph BEFORE["BEFORE"]
        A1["agent, cwd = /work/app"]
        A1 --> B1["repo source files"]
        A1 --> B2["tasks/add-http-retry.md"]
        A1 --> B4[".worc/ — .env, state.db, flows,<br/>memory, prompts, raw I/O, OTHER tasks"]
    end
    subgraph AFTER["AFTER"]
        A2["agent, cwd = /work/app"]
        A2 --> C1["repo source files"]
        A2 --> C2["tasks/add-http-retry.md"]
        A2 --> C3[".worc-io/add-http-retry/ — curated results"]
        A2 -.->|"denied / out of reach"| C4[".worc/ — all private_home"]
    end
```

How strong the "denied" arrow is, honestly, by phase:

| Provider | Phase 1 (hygiene) | Phase 2 (hard isolation) |
| --- | --- | --- |
| Claude | absolute-anchored `Read(//…/.worc/**)` deny is real (once anchored + dotfile-covering — see WRI-002); residual hole: `Bash(cat .worc/.env)` still works under `workspace-write` | private home moved out of the tree → `Bash` can't reach it either |
| Codex | not handed `.worc` paths + role-prompt note — **obscurity, not enforcement** | generated OS-sandbox read-deny (macOS/Linux); Windows fails preflight under `strict_isolation` |

## One node run in detail (`review`, after)

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant EX as exchange
    participant PH as private_home
    participant A as Agent

    Note over EX: plan.md and current.diff already written by upstream nodes
    O->>O: render prompt with EXCHANGE paths + Context-files footer
    O->>PH: persist rendered-prompt.md (audit)
    O->>A: launch Claude, disallowedTools Read(//abs/.worc/**)
    A->>EX: read plan.md, current.diff (allowed)
    A-xPH: read .worc/state.db via Read tool (DENIED)
    A-->>O: streamed node output
    O->>EX: write findings.json (redacted) = review_path
    O->>PH: write 1-claude/ raw stdout, stderr, events (audit)
```

## Correctness checklist

Verifying the happy path is complete and sound:

- [x] **Every path variable the flow uses resolves into `exchange`** (`{plan_path}`, `{diff_path}`, `{checks_path}`, `{implementation_path}`, `{review_path}`, `{documentation_path}`, `{memory_path}`); `{task_path}` is unchanged and already outside both homes.
- [x] **Each node's inputs exist in `exchange` before it runs** — planning's `plan.md` is there for implementation, review and documentation; implementation's `current.diff` is there for testing, review and documentation; review's `findings.json` is there for documentation and a would-be `fixing` node.
- [x] **Nothing the agent needs is left only in `private_home`** — `task.enriched.md` (audit-only, no variable) and the supervisor `summary.md` (read by the orchestrator at publish, not by an agent) are the only agent-untouched outputs.
- [x] **Audit completeness is unchanged** — rendered prompts, prompt-audit, raw provider streams, the evaluator `summary.md`, `state.db` and the PR-body summary are all still written, to `private_home`.
- [x] **Neither home is ever committed** — `exchange_root` is gitignored (its own tracked-`.gitignore` + `.git/info/exclude` line and its own `check-ignore` probe target), so scoped staging skips it via the ignore — not via a staging pathspec.
- [x] **The deny is non-weakenable** — it is an internal rule, not `denied_read_paths`, and cannot be removed by the task, `extra_args`, or a flow node.
- [x] **Path-only prompt invariant preserved** — still paths, only the destination changed.
- [~] **The Claude deny actually matches** — only once it is absolute-anchored and dotfile-covering (WRI-002); a bare relative `Read(.worc/**)` would not match the footer paths.
- [~] **Cross-platform** — Phase-1 Claude deny holds on all OSes; Codex enforcement is Phase 2, fail-closed on Windows under `strict_isolation`.
- [~] **Decomposition** — a decomposed subtask adds a `sub-<NN>/` level; the exchange routing must cover it (out of this walkthrough's single-task scope; tracked in WRI-001).

Items to confirm during implementation (also tracked in the ADR's Open questions):

- The exchange artifacts are **not uniformly redacted at write**: `findings.json` (raw model structured output), `plan.md` (the slot, written raw), the checks **stdout** log, and the per-run checker JSON are not scrubbed at write; the `{memory_path}` packet is redacted at store-ingest, not at packet write. All are agent-readable today (no net-new exposure), but confirm and decide whether any needs a write-time pass.
- Confirmed: no packaged flow points a node **at** `task.enriched.md` — it is the `enriched_spec` slot with a `None` inputs field, so it has no prompt variable and correctly stays private. Re-check for custom flows.
