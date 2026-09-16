# Batch 3 — the packet and the seams

Covers **P1.8, P1.9, P1.10, P2.13**. One pull request against `dev`; three phases, three commits.

This batch runs last because none of it is on the critical path of a run: it improves what the supervisor is told, what the evidence directories hold, and what a repository can declare about itself. Phase 8 shares a seam with [merge-flow-conflict-competence.md](../merge-flow-conflict-competence.md) and should be reviewed beside it.

Back to the [campaign index](README.md); the evidence is in [inventory.md](inventory.md).

---

## Phase 7 — give the supervisor the verdicts it writes the pull request from

**Item:** P1.8. **Touches:** [`core/supervisor_packet.py`](../../../src/wastech_orchestrator/core/supervisor_packet.py), [`flow/recorder.py`](../../../src/wastech_orchestrator/core/flow/recorder.py), [`core/supervisor.py`](../../../src/wastech_orchestrator/core/supervisor.py).

### What is wrong

The saved packet for `en-adapt-01a` holds 32 steps. Agent steps carry a full `message`; tool and evaluator steps carry nothing but timing and verdict, because `_steps` renders only the durable scalar facts and tool nodes write no `<node_id>.out.md` for `_step_message` to read. `findings_path` appears once for the whole run, pointing at the last evaluator; the ten `fidelity_critic` finding sets are absent. `checks` reflects the config's `checks` section, which tool-node verdicts never reach.

So the supervisor wrote an accurate pull-request body only because the fixing agents happened to quote the findings in their own messages — and it noticed, and wrote the gap into the pull request itself.

Two further defects in the same artifact: the packet is built before publish completes, so the last step reads `status: running` and the finalizer narrated that fact **in the pull request body**; and every exchange path in the saved packet points into `.worc-io/<task>/…`, which terminal cleanup removes.

### The constraint that shapes the fix

The private read-deny projection keeps `.worc` unreadable to agents at **either** value of `security.disable_read_isolation` ([isolation.py](../../../src/wastech_orchestrator/security/isolation.py)). Rewriting a dead exchange path to a live one under `logs/` therefore makes it durable and still useless to the supervisor. Only inline content reaches the agent; paths serve the human reading the post-mortem. Both are worth having, for different readers — which is why the fix does both rather than choosing.

### What to build

1. Include a tool step's `data` and a bounded head of its redacted stdout in its packet step.
2. Include each evaluator step's findings in **its own** step — one `findings_path` for the whole run is not enough for a flow with two lenses and ten rework rounds.
3. Give each new field its own cap, stated beside `_STEP_MESSAGE_MAX` and applied in the same place, so the finalize turn's budget stays the bounded thing it is built on. A chatty tool must not be able to inflate every packet.
4. Either exclude an unfinished `publish` step from the packet or label it so the finalizer knows it is expected and does not narrate it. Internal state must not leak into text people read.
5. When persisting the packet, rewrite exchange paths to their private copies under `logs/<task>/stages/…`, and say in the field's own documentation that these are for the post-mortem reader, not for the agent.

### Definition of done

- A flow with a tool gate and two evaluator lenses produces a packet in which each gate's verdict and each lens's findings are readable without opening another file.
- The packet's size stays bounded under a deliberately chatty tool and a deliberately long finding set.
- No saved packet contains a `publish` step in `status: running`.
- Every path in a saved packet resolves after terminal cleanup.

---

## Phase 8 — evidence that contains evidence, and node rows that close

**Items:** P1.9, P1.10. **Touches:** [`flow/exchange_seal.py`](../../../src/wastech_orchestrator/core/flow/exchange_seal.py), [`core/orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py), [`state_store.py`](../../../src/wastech_orchestrator/state_store.py).

### What is wrong

**An empty quarantine bundle, kept forever.** `_terminal_exchange` enters the quarantine branch on `if contaminated or mutation is not None`. On the second terminal the stored `contaminated` flag was still set from the first and `mutation` was `None`, so `expected=None` and `observed=()`. `quarantine_contaminated` then creates the directory and writes `evidence.json` **unconditionally**, and only afterwards checks `os.path.lexists(task_dir)` — false, because the tree already moved into bundle `000001`. The result is a `mkdir` plus a contentless JSON, in the one directory [`runs_retention.py`](../../../src/wastech_orchestrator/runs_retention.py) correctly never reclaims. Separately, `tasks.exchange_contaminated` is back to `0` for both tasks while the bundles sit on disk: the database no longer points at the incident and the only link left is a directory name.

**Node rows that never close.** Two runs of `worc merge-task` were stopped by the containment defect (since fixed) and left `node_runs` 45 and 46 `running` with no `finished_at`. `merge_task` converts `NodeManualRequired` into `ManualActionRequired` at the merge seam and aborts the git merge cleanly — the transactional promise covers git, not the state store. The general shape: any node that raises `NodeManualRequired` **before** its provider call, on a path that does not go through the task driver's terminal, leaves its row open. The merge flow is the only such path today, which is why it is the one that shows.

### What to build

1. Do not create a bundle when there is nothing to record: with `expected is None`, `observed_changes` empty **and** no live tree, log a `WARNING` and return.
2. Add `created_at`, `node_id` and attempt to `evidence.json`. It carries `task_id` and nothing temporal, so the order of two bundles is recoverable only from directory mtime.
3. Do not reset `exchange_contaminated` without leaving a trace: write a `quarantine_refs` list on the task row so a finished task still names its own evidence.
4. Close the open row on the way out of `_run_merge_flow` — status `aborted`, the raised reason in the new `abort_reason` column from [phase 5](batch-2-audit-record.md), a `finished_at`.
5. Better, and the reason this is worth doing properly: make the row's lifetime a **context manager** at the node layer, so no exit path can skip closing it. Then the merge flow needs no special case and the next such path cannot reintroduce the defect.

### Definition of done

- A second terminal with nothing to quarantine logs a warning and creates no directory.
- A real quarantine bundle carries `created_at`, `node_id` and attempt, so two bundles order themselves without filesystem metadata.
- A finished task whose exchange was quarantined still names its evidence from the database.
- After an aborted merge flow, `node_runs` for that task holds no `running` row — asserted beside the existing merge-task coverage.
- A node raising `NodeManualRequired` before its provider call closes its row on any path, not only the merge one.

---

## Phase 9 — let a repository name its governance, and close the campaign

**Item:** P2.13, plus the campaign's doc sync. **Touches:** [`flow/instruction_bundle.py`](../../../src/wastech_orchestrator/core/flow/instruction_bundle.py), [`config/schema.py`](../../../src/wastech_orchestrator/config/schema.py), the config validator, `src/wastech_orchestrator/packaged/config.example.yaml` and the packaged guide.

### What is wrong

```python
REPO_INSTRUCTION_NAMES = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md")
GOVERNANCE_PATH_GLOBS = (".agents/rules/**",)  # "A constant, never a config key"
```

The intent behind the comment is right: an operator must not be able to switch the notice off. But "not a config key" is being read as "not extensible", and a repository that keeps its rule set anywhere else gets a notice covering part of its governance and silently omitting the rest. In this run the diff touched `AGENTS.md` **and** `.rules/wastime-journey-rules.md`; the ledger reported `governance_changed: ["AGENTS.md"]` only. Half a governance edit reached the pull request unannounced.

### What to build

1. A config key that **appends** to `GOVERNANCE_PATH_GLOBS` and cannot remove from it. The additive-only guarantee is carried by the key's form — there is no exclusion syntax to express a removal — and the validator rejects any attempt to introduce one (a leading `!`, or a value that is not a plain glob).
2. Bump `CONFIG_SCHEMA_VERSION` 40 → 41, and extend `config/upgrade.py` so an existing config picks the key up.
3. Document it as **additive** in the config reference, with the reason restated where the constant is defined — the comment there is what stopped someone extending it the first time, so it has to explain the distinction rather than forbid the change.
4. Ship the key in `config.example.yaml` and the packaged guide, commented and off by default.

### Definition of done

- A repository with rules under `.rules/**` gets a complete `governance_changed` notice in the ledger, the console and the pull-request summary.
- No configuration can shrink the constant set; the validator has a test that proves it for the exclusion forms it rejects.
- `worc upgrade-config` moves an existing config from 40 to 41 without touching operator values.
- The packaged operator copy and the example config both carry the key.

### Campaign close-out

The last commit of this batch also closes the campaign, per the backlog index's rule that implemented items leave the folder:

- Remove this folder's entry from the open-backlog table and delete the folder, or move it to `.archives/<MMDDYYYY>/` if the evidence is judged worth keeping as a record of _why_. Either way it stops being listed.
- P1.10's entry in [merge-flow-conflict-competence.md](../merge-flow-conflict-competence.md)'s neighbourhood is settled by [phase 8](#phase-8--evidence-that-contains-evidence-and-node-rows-that-close); note that there rather than leaving two documents describing one seam.
- Leave the doc-impact note for the `site` branch in the pull request description, naming the derived documents the nine phases touched — `configuration.md` for the new config key, `operations.md` for the terminal line and the retention sets, `worc_architecture.md` for the loop guards.
