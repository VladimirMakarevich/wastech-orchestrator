# Provider exhaustion: park with fidelity, prove the fallback is alive, honor the reset

Status: **proposed** Date: 2026-08-06 Owner: Vladimir Makarevich

One task in three ordered parts, all found by a single production incident. **P1** is a correctness defect that destroyed work; **P2** is what would have prevented the incident from mattering; **P3** is what makes the recovery cheap. They ship together because each one alone leaves the incident reproducible: P1 without P2 parks correctly and then waits on a provider that will never answer; P2 without P1 removes this trigger but leaves the class-aggregation bug live for the next one; P3 is meaningless until P1 restores the park it refines.

## The incident (measured, `wastech-mdlint`, night of 2026-08-05→06)

An unattended `watch` run processed the P13–P17 remediation queue on the shared branch `worc/p13-p17-remediation`. Nine tasks completed and merged cleanly (~$173, 00:04→06:19 local). Then **six consecutive tasks went terminal `failed` in 26 minutes**, inside a 34-minute window in which the correct behavior was to wait.

| Time (local) | Event |
| --- | --- |
| 06:36:23 | Claude reports `rate_limit_event`: `five_hour`, utilization **0.99**, `allowed_warning` |
| 06:36:35 | Claude reports `rate_limit_event`: **`status: rejected`**, `overageDisabledReason: group_zero_credit_limit`; HTTP 429, `You've hit your session limit · resets 7:10am` |
| 06:36:36 | Router classifies `rate_limited` and falls back claude → codex — **correctly, per the invariant** |
| 06:36:40 | Codex fails in 4.7s: `refresh_token_expired` → `authentication_failed` |
| 06:36:41 | `p14-03-init-disclosure` → `failed`, discarding a **successful** `planning` stage (11m18s, 3.27M input tokens, $4.45) |
| 06:41 … 07:02 | Five more tasks pulled at the 300s poll interval, each 429ing in ~2.5s, each → `failed` |
| **07:10** | Claude's window resets. The queue is already empty. |

`blocked_since` is `NULL` for all six rows in `state.db` — **no park ever happened**, on any of them.

Two independent faults met. The rate limit was real and unavoidable (nine `claude-opus-5` / `reasoning: xhigh` tasks at 3–6M input tokens each exhausted the five-hour window; overage was disabled account-side, so paying through it was not an option). Codex's ChatGPT refresh token had expired before the run even started — `~/.codex/auth.json` last refreshed 2026-08-05 22:59 — and nothing noticed for eight hours **because codex was never called until it was the last hope**.

Neither fault is the interesting part. The interesting part is that the orchestrator's own documented behavior for this exact situation did not fire.

## P1 — the exhaustion class must aggregate across attempts, not be the last one

### What should have happened

[`config.example.yaml`](../../src/wastech_orchestrator/packaged/config.example.yaml) states the contract on the `max_blocked_s` line: _"every provider down OR rate-limited -> park as resumable; fail only after"_. A rate-limited stage parks (`RUNNING` + `blocked_since`), the single-slot park holds the queue, and the next tick resumes after the reset. That is also what [`test_rate_limited_exhaustion_parks_task_resumable`](../../tests/core/test_orchestrator.py) asserts.

### Why it did not

The class that reaches the park decision is the class of the **last** attempt:

1. [`router.py`](../../src/wastech_orchestrator/routing/router.py) — `last_error` is reassigned inside `except ProviderError` on every attempt. After claude it holds `rate_limited`; after codex, `authentication_failed`.
2. Same file — the exhausted-stage return is `StageOutcome(..., terminal_error=last_error)`. The rate limit is gone from the outcome.
3. [`agent.py`](../../src/wastech_orchestrator/core/flow/nodes/agent.py) — `NodeInfraError` is raised carrying that single class.
4. [`orchestrator.py`](../../src/wastech_orchestrator/core/orchestrator.py) — `if exc.error_class in PARK_ELIGIBLE`. `PARK_ELIGIBLE` is `{provider_unavailable, network_unavailable, rate_limited}` ([`base.py`](../../src/wastech_orchestrator/providers/base.py)); `authentication_failed` is not in it → `_fail()` → terminal.

**The defect stated plainly: a broken fallback provider masks a park-eligible failure of the primary.** The worse the fallback's own failure, the worse the core's decision. Whether the task survives depends on a provider that never ran a token of work.

### Why the tests missed it

Every park test builds providers with `_both(infra_error_class=...)` ([`test_orchestrator.py`](../../tests/core/test_orchestrator.py)) — one kwarg, applied to both providers, so **both always fail with the identical class**. A mixed pair is not merely untested, it is not expressible with the current helper.

### Proposed design

Carry every attempt's class to the decision, and make the decision a documented precedence in the Core.

`StageOutcome.attempts` already holds each `ProviderAttempt.error_class`, so the router needs no new plumbing for the _data_ — what is missing is that `NodeInfraError` narrows it to one. Add the set (e.g. `error_classes: tuple[ErrorClass, ...]`, with the existing `error_class` kept as the representative for messages) and change the predicate to consider all of them.

**Keep the policy in the Core, not the router.** `StageOutcome`'s docstring is explicit — _"Everything the Core needs to act on a stage run. The Router decides nothing downstream."_ Computing an aggregate class inside the router would work and is the smaller diff, but it moves a park/fail policy decision into the component whose contract says it holds none. Prefer widening `NodeInfraError`.

**The precedence is the whole design, and getting it wrong is worse than the bug.** It must be, in order:

1. **containment / capability wins over everything** — `_CONTAINMENT_MANUAL_CLASSES` (`containment_unverified`, `capability_unavailable`) → `manual_action_required`. A rate-limited primary must **never** paper over an unproven process tree on the fallback. A naive "any attempt is park-eligible → park" predicate does exactly that, and an auto-resume over a possibly-live unknown writer is the one outcome the security rules forbid outright.
2. then **park-eligible** — any attempt in `PARK_ELIGIBLE` → park.
3. then **fail** — as today.

Two smaller decisions to record while writing it:

- `agent_no_progress` is deliberately fallback-eligible but **not** park-eligible ([`base.py`](../../src/wastech_orchestrator/providers/base.py)). Under the new predicate, `rate_limited` primary + `agent_no_progress` fallback parks. That is the right answer (the primary's limit is a real transient) but it widens where the no-work net can hold the slot — state it in the comment rather than leaving a reader to infer it.
- `cancelled` keeps its own branch and must not be reachable through the aggregate: a stop-kill on one attempt plus a rate limit on another still means _stopped_, not _waiting_.

### The reporting half, which is user-visible

The artifact the operator actually reads named only the masking error. Verbatim from the incident:

```markdown
# Task p14-03-init-disclosure stuck

The **infra** fix loop exhausted its limit (`agent node 'implementation': no provider could complete it (authentication_failed)`).
```

The 429, the reset time, the fact that a fallback was even attempted — all absent. `counters` was empty, the diff empty, and there was no hint that a stage had succeeded and was being discarded. `stuck.md` and `failure_report.json` must name **every** attempt with its provider and class. Without this, P1 fixes the behavior and leaves the diagnosis just as expensive.

## P2 — an unauthenticated provider must be provable, and unattended runs must check

Two defects, both necessary for the incident.

**(a) `authenticated` is decorative.** [`_adapter_base.py`](../../src/wastech_orchestrator/providers/_adapter_base.py) hardcodes `authenticated=True` on every path where `<cli> --version` exits 0 — the docstring concedes _"auth is best-effort/offline"_. Worse, `run_preflight` ([`cli.py`](../../src/wastech_orchestrator/cli.py)) computes `healthy = executable_found and supports_required_features` and merely **prints** `authenticated=...`. So `worc preflight` on the night of the incident would have reported `codex: OK — codex 0.144.4 available (authenticated=True)`. A false statement, in the field the operator would check.

**(b) `run_preflight` never runs unattended.** Its only callers are `cmd_preflight` and the installer's post-write auto-run. `cmd_watch` does call the `preflight` module for `require_git_control` / `require_gh` / `warn_if_gh_logged_out` — git and GitHub are verified before a daemon starts; **the agent providers are not**.

### Proposed design

A real auth probe as a subclass hook alongside `_preflight_capability_error` / `_preflight_degraded_reasons` — the base must stay ignorant of CLI syntax (hard invariant). Both CLIs have a verb, probed live on 2026-08-06 against `claude 2.1.222` / `codex-cli 0.144.4`:

| Provider | Verb | Output |
| --- | --- | --- |
| Claude | `claude auth status` | JSON: `{"loggedIn": true, "authMethod": "claude.ai", "apiProvider": "firstParty", "email": …, "orgId": …, "orgName": …, "subscriptionType": "team"}` |
| Codex | `codex login status` | text: `Logged in using ChatGPT` |

**The codex probe is not sufficient, and this is the finding that matters here.** Run today, with `auth.json` **untouched since 2026-08-05 22:59** — the same bytes that produced `refresh_token_expired` six times that night — `codex login status` still prints `Logged in using ChatGPT` and exits 0. It reports stored-credential _presence_, not _validity_. A probe built on it would have said "authenticated" at 06:36:40 while the CLI was 401ing in the next process. Either find a no-model round-trip that actually proves the token refreshes, or ship the presence check while labelling exactly what it does and does not prove. **Do not let `authenticated` make a claim the probe cannot support** — that is the defect being fixed, and re-introducing it one layer up is worse than leaving the field alone.

**Redaction is mandatory here.** `claude auth status` returns email, `orgId`, and `orgName`. Only `loggedIn` / `authMethod` may reach a log line, a preflight line, or a report — no-secrets-in-logs is a hard invariant, and this probe puts PII one careless f-string away from `daemon.log`.

**Verdict policy — a deliberate inversion.** The existing rule for `degraded_reasons` is _fatal only when this is the sole allowed provider, else a warning, because a fallback provider will cover_. For authentication that rule is precisely backwards: **the dead provider here _was_ the fallback**, and "a fallback will cover" was the assumption that failed. Proposal: an unauthenticated provider is `FAIL` regardless of its role in any route — a fallback that cannot start is not a fallback, and its silence is only discovered at the moment it is needed. Record this as a decision, since it deviates from the established fallback-aware shape.

**Where to call it.** `cmd_watch`, next to the existing `require_git_control()`, before the loop; and `cmd_run`. A daemon has no operator to read a warning, so for `poll > 0` a failed provider preflight should refuse to start rather than warn. Re-checking at tick boundaries (auth can expire mid-run, as it did) is the neighboring idea and belongs with P3's cooldown state, not here.

## P3 — honor `resetsAt`, and stop feeding the queue into a known-limited provider

The adapter already captures the event that carries the answer: [`claude.py`](../../src/wastech_orchestrator/providers/claude.py) stores `rate_limit_event` — including `resetsAt: 1785993000` (07:10 local, 34 minutes out) — and uses it only to set a boolean. The instant is discarded.

Consequence, even after P1 lands: a parked task waits blind. `_park_ceiling_exceeded` measures against `agents.retry.max_blocked_s` (6h) and the wake-up is whenever the next `watch` tick happens to come around. The orchestrator would know it needs 34 minutes and instead poll for six hours' worth of ticks.

Two parts:

- **Carry the instant.** A `resets_at` / `retry_after` on `NormalizedError`, threaded to `_park` so the resume is scheduled rather than stumbled into. Treat it as **untrusted provider input**: clamp to `max_blocked_s`, reject absurd or past values, and compare through the injected `clock()` — never `datetime.now()` — so the behavior stays testable, which is how every other time-dependent path here is written.
- **A provider cooldown the queue can see.** After P1 the single-slot park already stops the bleeding — the emergent circuit breaker described in [`test_cli_pipeline.py`](../../tests/core/test_cli_pipeline.py) (_"no separate breaker; it falls out of the single-slot park"_) works again the moment a park happens. P3 only makes the wait precise instead of 300s churn against a provider that is certain to 429.

**Scope boundary, stated so this does not quietly become a different feature.** This is _reactive_: a provider has already reported a limit **and its own reset instant**, so the orchestrator is recording a fact. It is explicitly **not** [`runtime_provider_capacity_gate.md`](archive/runtime_provider_capacity_gate.md), which stays deferred — that item is _proactive_ admission control (query account headroom before claiming a task) and carries all the hard parts this one does not: no provider reports a remaining token budget, utilization is not a completion guarantee, and usage moves concurrently on other devices. No capacity query, no headroom thresholds, no token estimation here.

## Ordering

**P1 first, alone if necessary.** It is the only part that fixes lost work, and P2/P3 both assume a park exists. P2 second: it removes this trigger class entirely and is independently shippable. P3 last: pure efficiency over a park that must already be correct.

## Tests

The test gap is as much the deliverable as the code — the existing suite asserts this exact scenario and passes.

- Mixed-class exhaustion parks: `rate_limited` primary + `authentication_failed` fallback → `RUNNING` + `blocked_since`, no ledger record, no failure report. Needs `_both()` split so the two providers can carry different classes.
- Precedence: `rate_limited` primary + `containment_unverified` fallback → `manual_action_required`, exchange flagged unsafe, **not** parked. This is the test that keeps P1 from becoming a security regression.
- `cancelled` on one attempt plus a park-eligible class on another still routes to the cancel branch.
- `stuck.md` / `failure_report.json` name every attempt's provider and class.
- An unauthenticated provider fails `run_preflight` whether or not it is the primary; a daemon `watch` refuses to start.
- The auth probe's output never reaches a log line beyond `loggedIn` / `authMethod`.
- A park with a `resets_at` wakes at that instant, not at the blind ceiling; an absurd/past/oversized `resets_at` is clamped or ignored.

## Non-goals

- **No proactive capacity or headroom query** — that is the archived capacity-gate item, deliberately left deferred.
- **No automatic re-login or credential handling.** Credentials stay outside the orchestrator; a dead token is detected and reported, never repaired. (The "Automatic CLI installation/authorization" row in this README is a separate, deliberately deferred idea.)
- **No change to fallback eligibility.** `FALLBACK_ELIGIBLE` is untouched; the fallback in the incident was correct. Only what the Core concludes _after_ exhaustion changes.
- **No new error classes**, no change to which classes are infra, no concurrency change.

## Operator recovery for the incident itself (already known, recorded for the reconstruction)

All six tasks are `branch_mode: existing` on an operator-owned branch, so a fresh `rerun` is refused by design — `rerun --continue` resumes from the recorded checkpoint (`implementation` for `p14-03`, which reuses its surviving `plan.md`; `planning` for the other five). The daemon must be stopped first. `source_path` in `state.db` already points into `tasks/failed/`, so the files need no moving.
