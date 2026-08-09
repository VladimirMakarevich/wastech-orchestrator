# Provider exhaustion: park with fidelity, prove the fallback is alive, honor the reset

Status: **implemented** Date: 2026-08-06 Owner: Vladimir Makarevich Reviewed: 2026-08-06 (every code claim below re-verified against `dev`)

All three parts shipped, one commit each. The design below is kept as written, with the as-built deviations recorded inline where they matter — read those notes as the authority where they disagree with the surrounding text. The five that changed real behavior:

- **A second security hole, not in the design.** `_terminal_infra_manual` flagged the exchange unsafe from the _representative_ class, and the Router rewrites that to `cancelled` on any error once a stop is pending — including `containment_unverified`. A stop landing on an unproven process tree therefore left the quarantine flag unset while the status still said manual, so the terminal seam would seal, commit and push a tree an unknown descendant might still be writing. It reads the class set now (P1, §5b).
- **The two exception handlers' precedence was already inverted relative to each other** — the generic one tested `PARK_ELIGIBLE` before the containment classes, the evaluator one the reverse. Invisible with a single class, a security regression the moment classes aggregate. The shared dispatch makes re-inverting it impossible (P1, §5).
- **The third disposition is `TERMINAL`, not `FAIL`**, because the evaluator's fail-closed schema raise carries no class and lands there, and a member named `FAIL` invites `Status.FAILED` — silently destroying an already-green diff (P1, §4).
- **The codex logged-out answer is a non-zero exit on stderr**, not exit 0 on stdout. Only the logged-**in** answers had been verified; keying the probe on a clean exit would have made the `LOGGED_OUT` branch unreachable and shipped P2 as a no-op that reads as done (P2, §8).
- **Hop 4 as specified would not have helped this incident.** The instant is now aggregated across attempts like the class, instead of taken from the settling attempt (P3, hop 4).

Two decisions were taken against the text below: `AuthProbe.proves_validity` is dropped as an unread field (P2, §1), and `cmd_rerun` is gated alongside `run`/`watch` (P2, §9).

One task in three ordered parts, all found by a single production incident. **P1** is a correctness defect that destroyed work; **P2** closes the blind spot that let a dead fallback sit unnoticed for eight hours; **P3** is what makes the recovery cheap. P1 without P2 parks correctly and then waits on a provider that will never answer; P2 without P1 leaves the class-aggregation bug live for the next trigger; P3 is meaningless until P1 restores the park it refines.

**One correction to the framing, because the ordering depends on it: P2 would not have prevented this incident.** The probe P2 can actually ship for codex reports credential _presence_, and the credentials were present — they had simply stopped refreshing (see the caveat in P2). A presence probe run at 06:36:00 would have said "authenticated" and the run would have failed identically. So **P1 carries the entire fix for the lost work**, which is why it ships first and alone if necessary; P2's value is the different, common, and currently undetectable class of a fallback that has _no_ credentials at all.

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

[`config.example.yaml`](../../../src/wastech_orchestrator/packaged/config.example.yaml) states the contract on the `max_blocked_s` line: _"every provider down OR rate-limited -> park as resumable; fail only after"_. A rate-limited stage parks (`RUNNING` + `blocked_since`), the single-slot park holds the queue, and the next tick resumes after the reset. That is also what [`test_rate_limited_exhaustion_parks_task_resumable`](../../../tests/core/test_orchestrator.py) asserts.

### Why it did not

The class that reaches the park decision is the class of the **last** attempt:

1. [`router.py`](../../../src/wastech_orchestrator/routing/router.py) — `last_error` is reassigned inside `except ProviderError` on every attempt. After claude it holds `rate_limited`; after codex, `authentication_failed`.
2. Same file — the exhausted-stage return is `StageOutcome(..., terminal_error=last_error)`. The rate limit is gone from the outcome.
3. [`agent.py`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) — `NodeInfraError` is raised carrying that single class.
4. [`orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py) — `if exc.error_class in PARK_ELIGIBLE`. `PARK_ELIGIBLE` is `{provider_unavailable, network_unavailable, rate_limited}` ([`base.py`](../../../src/wastech_orchestrator/providers/base.py)); `authentication_failed` is not in it → `_fail()` → terminal.

**The defect stated plainly: a broken fallback provider masks a park-eligible failure of the primary.** The worse the fallback's own failure, the worse the core's decision. Whether the task survives depends on a provider that never ran a token of work.

### Why the tests missed it

Every park test builds providers with `_both(infra_error_class=...)` ([`test_orchestrator.py`](../../../tests/core/test_orchestrator.py)) — one kwarg, applied to both providers, so **both always fail with the identical class**. A mixed pair is not merely untested, it is not expressible with the current helper.

### Proposed design

Carry every attempt's class to the decision, and make the decision a documented precedence in the Core.

`StageOutcome.attempts` already holds each `ProviderAttempt.error_class`, so the router needs no new plumbing for the _data_ — what is missing is that `NodeInfraError` narrows it to one. Add the set (e.g. `error_classes: tuple[ErrorClass, ...]`, with the existing `error_class` kept as the representative for messages) and change the predicate to consider all of them.

**Keep the policy in the Core, not the router.** `StageOutcome`'s docstring is explicit — _"Everything the Core needs to act on a stage run. The Router decides nothing downstream."_ Computing an aggregate class inside the router would work and is the smaller diff, but it moves a park/fail policy decision into the component whose contract says it holds none. Prefer widening `NodeInfraError`.

**The precedence is the whole design, and getting it wrong is worse than the bug.** It must be, in order:

1. **containment / capability wins over everything** — `_CONTAINMENT_MANUAL_CLASSES` (`containment_unverified`, `capability_unavailable`) → `manual_action_required`. A rate-limited primary must **never** paper over an unproven process tree on the fallback. A naive "any attempt is park-eligible → park" predicate does exactly that, and an auto-resume over a possibly-live unknown writer is the one outcome the security rules forbid outright.
2. then **park-eligible** — any attempt in `PARK_ELIGIBLE` → park.
3. then **fail** — as today.

Two smaller decisions to record while writing it:

- `agent_no_progress` is deliberately fallback-eligible but **not** park-eligible ([`base.py`](../../../src/wastech_orchestrator/providers/base.py)). Under the new predicate, `rate_limited` primary + `agent_no_progress` fallback parks. That is the right answer (the primary's limit is a real transient) but it widens where the no-work net can hold the slot — state it in the comment rather than leaving a reader to infer it.
- `cancelled` keeps its own branch and must not be reachable through the aggregate: a stop-kill on one attempt plus a rate limit on another still means _stopped_, not _waiting_. **Note what this does and does not change today:** [`orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py) routes `PARK_ELIGIBLE` and `CANCELLED` to the _same_ `_park`, so the distinction is currently only the label on the log line and the report — there is no routing difference to preserve. It becomes a real routing difference in P3, where a cancel park must **not** inherit a rate-limit `resets_at`. Write the branch now so P3 does not have to reopen the predicate.

### Implementation (P1)

**1. Widen the exception, keep every existing raise site untouched.** In [`nodes/base.py`](../../../src/wastech_orchestrator/core/flow/nodes/base.py), `NodeInfraError` gains `error_classes: tuple[ErrorClass, ...]` beside the existing `error_class`, which stays the _representative_ used in messages and logs. Derive the set from the single class when only that is given, so the raise sites that legitimately know one class — `_typed`'s invalid-structured-output, the evaluator's fail-closed findings schema, the `FlowCancelled` synthesis at [`orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py) — need no edit at all. (This list previously named "the check-launch failure"; there is no such raise site — [`nodes/checks.py`](../../../src/wastech_orchestrator/core/flow/nodes/checks.py) raises only `NodeManualRequired`, and `NodeInfraError`'s own docstring carried the same stale claim.)

```python
def __init__(self, message, *, error_class=None, error_classes=()):
    super().__init__(message)
    self.error_class = error_class  # representative: messages, logs, reports
    self.error_classes = tuple(error_classes) or (() if error_class is None else (error_class,))
```

**As built, the `or`-fallback turned out to be load-bearing rather than merely tidy.** `run_stage` returns `attempts=()` when `max_stage_attempts == 0`, and every `FakeRouter`-shaped exhausted outcome in `tests/core/test_flow_node_runners.py` does the same while carrying a set `terminal_error`. Written as `if error_classes is not None` those all collapse to an empty set and change disposition, so the derivation is commented at the assignment.

**2. Fill the set at the two exhaustion raise sites.** [`nodes/agent.py`](../../../src/wastech_orchestrator/core/flow/nodes/agent.py) (`_invoke`, the `outcome.result is None` branch) and [`nodes/evaluator.py`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py) (`EvaluatorInfraError`, same branch) both already hold the `StageOutcome`. The set is `tuple(a.error_class for a in outcome.attempts if a.status is None and a.error_class is not None)`. In an exhausted outcome _every_ attempt row is a raise row — `run_stage` returns early the moment any attempt yields a result — so the `status is None` filter changes nothing today; keep it anyway, because it is what makes the predicate's domain "the classes that were _raised_" instead of "whatever is in the tuple", and a returned `task_failure` row must never be able to reach a park/manual decision.

**3. This also fixes a same-provider instance of the identical bug.** `_retry_transient` in [`router.py`](../../../src/wastech_orchestrator/routing/router.py) rebuilds `last_error` from `last_class` — the class of the **last** retry — and overwrites `exc` with it, so a `rate_limited` first attempt followed by two `network_unavailable` retries also loses its class. The aggregate covers it for free: each retry appends its own `ProviderAttempt` row. Do not "fix" it in the router.

**4. The predicate is a pure function in Core, table-tested.** Shaped like `fallback_allowed` is in the router — a pure decision table with its own unit test, not logic buried in an `except` block:

```python
class InfraDisposition(StrEnum):
    MANUAL = "manual"  # containment / capability — fail-closed, never auto-resume
    PARK = "park"  # resumable soft pause
    TERMINAL = "terminal"  # terminal by the failing node kind's own rule


def classify_exhaustion(
    classes: Sequence[ErrorClass], *, representative: ErrorClass | None
) -> InfraDisposition:
    if any(c in _CONTAINMENT_MANUAL_CLASSES for c in classes):
        return InfraDisposition.MANUAL  # 1. security wins over everything, incl. the stop below
    if representative is ErrorClass.CANCELLED:
        return InfraDisposition.PARK  # 2. stopped, not waiting
    if any(c in PARK_ELIGIBLE for c in classes):
        return InfraDisposition.PARK  # 3. any park-eligible attempt parks
    return InfraDisposition.TERMINAL  # 4. fail closed
```

An empty `classes` (no attempt carried a class — the `no_provider_available` case) falls through to `TERMINAL`, which is today's behavior and the correct fail-closed default.

**As built, two deviations from this sketch.** The third member is named **`TERMINAL`, not `FAIL`**: `nodes/evaluator.py`'s fail-closed findings-schema raise carries no class at all, so it lands in the third branch, and that branch must map to the evaluator's _manual_ terminal. A member named `FAIL` invites `Status.FAILED` there and silently destroys an already-green diff. And the predicate lives in its own module, `core/infra_disposition.py`, taking `_CONTAINMENT_MANUAL_CLASSES` with it — after the rewrite the constant has exactly one reader, `core/loop_control.py` is the established precedent for pure Core policy pulled out of the orchestrator, and a new module earns the full complexity ratchet instead of inheriting `orchestrator.py`'s blanket per-file exemption.

**5. Rewrite both dispatch sites to call it.** The generic `except NodeInfraError` handler and the `except EvaluatorInfraError` handler above it currently each hand-roll their own class checks. Both become one `classify_exhaustion(...)` call plus a three-way dispatch, so the security precedence physically cannot diverge between node kinds — the reason the evaluator handler already re-checks `_CONTAINMENT_MANUAL_CLASSES` before its degrade.

**Worth recording, because it is the strongest argument for step 5: the two handlers' precedence is already inverted relative to each other.** The generic handler tests `PARK_ELIGIBLE` _before_ `_CONTAINMENT_MANUAL_CLASSES`; the evaluator handler tests containment first. With a single class in play that inversion is unobservable, and it becomes a live security regression the moment `any(c in PARK_ELIGIBLE)` can be true while another attempt reported `containment_unverified`. Collapsing to one shared dispatch is what makes re-inverting it impossible.

**5b. `_terminal_infra_manual` must read the class set too — this is a second, separate hole.** It flags `exchange_active_unsafe` off the **representative** class, and `router.py` overwrites the representative with `CANCELLED` on _any_ `ProviderError` once a stop is pending — including `containment_unverified`. So an operator stop landing on the same attempt as an unproven process tree leaves the quarantine flag unset while the status still says manual, and `_fail`/`_go_terminal` then seal, commit and push a tree an unknown descendant may still be writing. Key it on `CONTAINMENT_UNVERIFIED in exc.error_classes`.

**6. Decision to record: a rate-limited _evaluator_ should park, not degrade to manual.** Today an evaluator that cannot run degrades to `manual_action_required` to preserve an already-green diff; it can never park. That degrade exists for "could not run, ever" (a misconfigured or no-work evaluator), not for "the window resets in 34 minutes" — and parking preserves strictly more than the manual terminal does (the diff survives _and_ the review still runs afterwards, with no operator involved). So the evaluator branch becomes MANUAL → PARK → degrade-to-manual. This is a behavior change beyond the incident's node kind, it needs its own test, and it is the difference between "six tasks needed an operator" and "six tasks waited". Recorded here as chosen, not assumed.

**7. No config keys, no new error classes, no schema change.** P1 touches types, one predicate, two raise sites, two dispatch sites, and the report. If the diff grows a config toggle, the design went wrong.

### The reporting half, which is user-visible

The artifact the operator actually reads named only the masking error. Verbatim from the incident:

```markdown
# Task p14-03-init-disclosure stuck

The **infra** fix loop exhausted its limit (`agent node 'implementation': no provider could complete it (authentication_failed)`).
```

The 429, the reset time, the fact that a fallback was even attempted — all absent. `counters` was empty, the diff empty, and there was no hint that a stage had succeeded and was being discarded. `stuck.md` and `failure_report.json` must name **every** attempt with its provider and class. Without this, P1 fixes the behavior and leaves the diagnosis just as expensive.

**The attempts need no new plumbing — they are already in `state.db` at the moment of the raise.** Both node runners call `record_run_observability` _before_ the `outcome.result is None` check, so every `provider_attempts` row for the failing node run is committed before the exception exists. `_write_infra_failure_report` has `task_id` + `node_id`, and the two getters it needs already exist on the store: `get_node_runs(task_id)` (take the last row whose `node_id` matches) then `get_provider_attempts(run_id)`. Read them there rather than threading a second payload through the exception — the exception carries policy input (`error_classes`), the report carries evidence, and the evidence is already durable.

`write_failure_report` in [`ledger.py`](../../../src/wastech_orchestrator/ledger.py) gains one optional `provider_attempts: Sequence[Mapping[str, Any]] = ()` parameter → a `provider_attempts` key in the JSON and a `## Provider attempts` section in `stuck.md`, one line per attempt: `provider · attempt · error_class · exit_code · started_at`. Every field is already secret-free by the `ProviderAttemptRow` contract; do not add the attempt directory or any message text without re-checking that.

**Fix the sentence while you are there.** For an infra terminal the writer emits `The **infra** fix loop exhausted its limit (…)` — but `loop="infra"` is a sentinel, there is no infra fix loop, and no limit was exhausted. The operator's first line should state what happened: when `loop == "infra"`, render `This task could not run: <limit_name>` instead. One branch in the `stuck_md` f-string; it is the single most-read line in the artifact.

## P2 — an unauthenticated provider must be provable, and unattended runs must check

Two defects, both necessary for the incident.

**(a) `authenticated` is decorative.** [`_adapter_base.py`](../../../src/wastech_orchestrator/providers/_adapter_base.py) hardcodes `authenticated=True` on every path where `<cli> --version` exits 0 — the docstring concedes _"auth is best-effort/offline"_. Worse, `run_preflight` ([`cli.py`](../../../src/wastech_orchestrator/cli.py)) computes `healthy = executable_found and supports_required_features` and merely **prints** `authenticated=...`. So `worc preflight` on the night of the incident would have reported `codex: OK — codex 0.144.4 available (authenticated=True)`. A false statement, in the field the operator would check.

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

### Implementation (P2)

**1. Delete the boolean instead of trying to make it honest.** `ProviderHealth.authenticated: bool` is replaced by `auth: AuthProbe | None` in [`base.py`](../../../src/wastech_orchestrator/providers/base.py). The requirement "do not let `authenticated` make a claim the probe cannot support" then holds structurally — there is no boolean left to lie with, and `proves_validity` is the field that keeps a presence check from reading as a validity check:

```python
class AuthState(StrEnum):
    LOGGED_IN = "logged_in"  # the CLI reports stored credentials present — presence, NOT validity
    LOGGED_OUT = "logged_out"  # the CLI reports no credentials
    UNKNOWN = "unknown"  # the probe could not run / its output was unparseable


@dataclass(frozen=True)
class AuthProbe:
    state: AuthState
    method: str | None  # "claude.ai" / "chatgpt" / "api_key" — a mechanism, never an identity
    detail: str  # secret-free AND PII-free by contract
```

**`proves_validity` was dropped as built.** Nothing reads it and it is `False` for every probe that ships, so it distinguishes nothing while tripping the no-unrequested-complexity rule. The requirement it existed for holds structurally instead: the member is named `LOGGED_IN`, not `AUTHENTICATED`, and `AuthState`'s docstring states that presence is not validity. Read the acceptance criterion below as "no `AuthState` member claims validity, and `LOGGED_IN` is documented as presence-only".

`None` means _not probed_ — a provider whose adapter implements no hook makes no claim at all, which is the correct reading for a third adapter added later.

**2. The hook.** `_preflight_auth_state(env) -> AuthProbe | None` on `BaseCliProvider` in [`_adapter_base.py`](../../../src/wastech_orchestrator/providers/_adapter_base.py), default `None`, called from `preflight()` on the healthy path only (a CLI whose `--version` did not succeed has nothing to probe). It runs through the existing `_probe()` helper, so it inherits the preflight timeout and the allowlisted env for free and stays inside the "no CLI syntax in the base" boundary — the verb lives in the subclass.

**3. Redact at the parse boundary, not at the print site.** The Claude probe parses `claude auth status`, reads exactly `loggedIn` and `authMethod`, and discards the rest of the object _there_. `email`, `orgId`, `orgName` must never be assigned to a field, appended to a `detail`, or included in a probe-failure message — if they never enter the dataclass, no later f-string can leak them. The codex probe's output is a fixed sentence with nothing to redact, but the same rule applies to its failure text.

**4. The verdict in `run_preflight`.** `LOGGED_OUT` → `FAIL` regardless of the provider's role in any route (the deliberate inversion). `UNKNOWN` → `WARN`, on the same principle that already governs `warn_if_gh_logged_out` — a flaky probe must not block a run. `None` → print nothing. The report line replaces `authenticated={…}` with `auth=logged_in (claude.ai)` / `auth=LOGGED OUT` / `auth=unknown`. The failure message must name the lever, because the inversion has a consequence an operator will hit: a host with only Claude logged in now fails preflight while `codex` sits in `agents.allowed`. So: `codex: FAIL — not logged in ('codex login'); it is in agents.allowed, so a node may route to it — log in or remove it from agents.allowed`.

**5. Do not call `run_preflight` from `cmd_watch`.** It is the obvious move and it is wrong: `run_preflight` also gates Telegram, `gh`, the isolation policy check, and (opt-in) the live capability smoke, so wiring it into `watch` silently makes an unrelated Telegram or `gh` result able to refuse a daemon start — a much larger behavior change than this task asks for. Add a narrow gate instead, shaped like the ones it sits beside: `require_provider_auth(config)` that builds the providers, runs only the auth probe for each `agents.allowed` provider, and exits non-zero on any `LOGGED_OUT`. Put it in [`cli.py`](../../../src/wastech_orchestrator/cli.py) next to `run_preflight` rather than in [`preflight.py`](../../../src/wastech_orchestrator/preflight.py) — that module deliberately imports only `install.detect` and knows nothing of the config or the provider composition, and `lint-imports` is the gate that will say so.

**6. One rule for every entry point: refuse.** `cmd_watch` (both the single-pass and daemon paths) and `cmd_run` call it unconditionally, exactly like `require_git_control()`. Splitting the rule by `poll > 0` buys nothing — an operator running one task against a logged-out provider gains no information by watching it fail at the first fallback — and a single rule is one less branch to test.

**7. A note for whoever re-verifies the codex caveat.** The evidence window has closed: `~/.codex/auth.json` on the incident host was rewritten at 2026-08-06 11:25, so the expired-refresh-token bytes that made `codex login status` exit 0 while the CLI 401'd are gone. Re-confirmed today: `codex login status` has no `--json` and no validity mode (`codex login --help` lists only `status`). Take the caveat from the incident record — do not conclude from a green probe on a freshly refreshed token that the probe proves validity.

**8. Both logged-out answers, measured as built — one of them changes the probe.** The design above verified only what each verb prints when logged **in**. Probed against throwaway credential directories:

| Verb | Logged out |
| --- | --- |
| `claude auth status --json` | exit **0**, `{"loggedIn": false, "authMethod": "none", "apiProvider": "firstParty"}` |
| `codex login status` | exit **1**, `Not logged in` on **stderr** (stdout empty) |

So the codex probe must **not** gate on a clean exit — it reads the combined stdout+stderr that `_probe` already returns, and matches `"not logged in"` before `"logged in"` because the former contains the latter. Had it keyed off `ok`, the logged-out branch would have been unreachable and the whole part would have shipped as a no-op that reads as done. The Claude probe passes `--json` explicitly: it is the default today, but the verb also offers `--text`. The login verbs the messages name are `claude auth login` and `codex login`.

**9. `cmd_rerun` is gated too, so the "one rule for every entry point" heading is now true.** The implementation section below names only `cmd_watch` and `cmd_run`; `rerun --continue` is this incident's own documented recovery path and re-spends real money from a checkpoint, so the same argument applies verbatim. The gate sits exactly where `require_git_control` does in that command — after the plan is accepted, before any work is re-driven.

## P3 — honor `resetsAt`, and stop feeding the queue into a known-limited provider

The adapter already captures the event that carries the answer: [`claude.py`](../../../src/wastech_orchestrator/providers/claude.py) stores `rate_limit_event` — including `resetsAt: 1785993000` (07:10 local, 34 minutes out) — and uses it only to set a boolean. The instant is discarded.

Consequence, even after P1 lands: a parked task waits blind. `_park_ceiling_exceeded` measures against `agents.retry.max_blocked_s` (6h) and the wake-up is whenever the next `watch` tick happens to come around. The orchestrator would know it needs 34 minutes and instead poll for six hours' worth of ticks.

Two parts:

- **Carry the instant.** A `resets_at` / `retry_after` on `NormalizedError`, threaded to `_park` so the resume is scheduled rather than stumbled into. Treat it as **untrusted provider input**: clamp to `max_blocked_s`, reject absurd or past values, and compare through the injected `clock()` — never `datetime.now()` — so the behavior stays testable, which is how every other time-dependent path here is written.
- **A provider cooldown the queue can see.** After P1 the single-slot park already stops the bleeding — the emergent circuit breaker described in [`test_cli_pipeline.py`](../../../tests/core/test_cli_pipeline.py) (_"no separate breaker; it falls out of the single-slot park"_) works again the moment a park happens. P3 only makes the wait precise instead of 300s churn against a provider that is certain to 429.

**Scope boundary, stated so this does not quietly become a different feature.** This is _reactive_: a provider has already reported a limit **and its own reset instant**, so the orchestrator is recording a fact. It is explicitly **not** [`runtime_provider_capacity_gate.md`](runtime_provider_capacity_gate.md), which stays deferred — that item is _proactive_ admission control (query account headroom before claiming a task) and carries all the hard parts this one does not: no provider reports a remaining token budget, utilization is not a completion guarantee, and usage moves concurrently on other devices. No capacity query, no headroom thresholds, no token estimation here.

### Implementation (P3)

**1. The instant crosses six hops, and one of them is not where the design summary says it is.** Adding the field to `NormalizedError` alone silently drops it: the router builds its `NormalizedError` from the raised **exception**, not from any object the adapter kept (`except ProviderError as exc: last_error = NormalizedError(error_class=exc.error_class, message=str(exc))`). So `ProviderError` must carry it too. The full thread:

| # | Where | Change |
| --- | --- | --- |
| 1 | [`claude.py`](../../../src/wastech_orchestrator/providers/claude.py) `parse_stream_json` | read `resetsAt` off the already-captured `rate_limit_event` into a new `ParsedEvents.rate_limit_resets_at` |
| 2 | [`_adapter_base.py`](../../../src/wastech_orchestrator/providers/_adapter_base.py) rate-limit raise | epoch → ISO-8601 UTC once, here; set it on the `NormalizedError` **and** the raised `ProviderError` |
| 3 | [`base.py`](../../../src/wastech_orchestrator/providers/base.py) | `NormalizedError.resets_at: str \| None` + the same optional kwarg on `ProviderError.__init__` |
| 4 | [`router.py`](../../../src/wastech_orchestrator/routing/router.py) | copy `exc.resets_at` into each `NormalizedError` it builds — the main `except`, the fresh-session retry, the transient-exhausted synthesis; **not** the `CANCELLED` one |
| 5 | `nodes/agent.py` / `nodes/evaluator.py` | pass `outcome.terminal_error.resets_at` onto `NodeInfraError` |
| 6 | [`orchestrator.py`](../../../src/wastech_orchestrator/core/orchestrator.py) `_park` | validate, clamp, stamp `tasks.blocked_until` |

The epoch→ISO conversion belongs at hop 2 because the field type stays provider-neutral (`resetsAt` is a Claude spelling) and nothing downstream should do timezone arithmetic. Codex reports no reset instant, so its errors leave the field `None` — that is not a gap, it is the field working.

**Hop 4 as specified would have shipped P3 as a no-op for this very incident, and this is the most important correction to the design.** "Copy `exc.resets_at` into each `NormalizedError` it builds" makes the surviving instant the one belonging to whichever attempt **settled** — and the settling attempt is the fallback. In the incident the fallback died on expired credentials and reported no instant at all, so `terminal_error.resets_at` would have been `None` and the park would still have waited blind. This is precisely the last-attempt-wins bug P1 exists to fix, reappearing one field over.

So the Router tracks the **earliest instant any attempt reported** across the whole stage and stamps it on the exhausted outcome, exactly as the class is now aggregated. Earliest rather than latest because waking too early costs one cheap re-park while waking too late is the blind wait being removed. The `CANCELLED` exclusion moves to that single point, which is also where it belongs: one place decides, instead of three sites each remembering not to copy.

**2. Schema v21: additive `tasks.blocked_until TEXT`.** Add the `_migrate` guard next to the `blocked_since` one, the `TaskRow` field, and — this is the project convention, not optional — the `v21` entry in the version-comment block at the top of [`state_store.py`](../../../src/wastech_orchestrator/state_store.py) stating that it is additive (a brand-new `0` database adopts it; an older _versioned_ database is still refused fail-closed and recreated, which is free here — nothing is deployed).

**3. There is no scheduler, and none should be built.** The wake is "the first `watch` tick at or after the instant". In `_resume_via_engine`, **after** `_park_ceiling_exceeded` (the ceiling must always win over a provider-supplied instant) and **before** `_check_preflight` / `prepare_branch`, short-circuit: if `blocked_until` is still in the future, log once and return `PipelineResult(final_status=Status.RUNNING)`. `watch_once` already returns early on a non-terminal resume, so the slot stays held, no git is touched, and no provider is launched. Precision is therefore bounded by `orchestrator.poll_interval_seconds` (300s default) — the wait becomes one cheap no-op tick per interval instead of a full re-entry that 429s in 2.5s. That bound is the whole feature; a timer thread or an interruptible sleep would be a different, larger one.

**4. Treat the instant as hostile input.** Parse with `datetime.fromisoformat`; on anything unparseable, absent, or `<= self._clock()`, leave `blocked_until` `NULL` (today's blind behavior — never worse). Clamp to `blocked_since + agents.retry.max_blocked_s`, so a provider claiming a reset next week cannot outlive the ceiling. Compare only through the injected `self._clock()`; a direct `datetime.now()` here would make the whole path untestable and would be the one thing every neighboring time-dependent path in this file already avoids. No new config key — the clamp is the existing ceiling.

**5. A cancel park must not inherit a wake instant.** This is where P1's `representative is CANCELLED` branch pays off: `_park` stamps `blocked_until` only when the disposition is PARK _and_ the representative class is not `CANCELLED`. An operator stop resumes when the operator says so, not when some provider's window happens to reopen.

**6. Clear it wherever `blocked_since` is cleared.** There is exactly one such site today (the terminal transition sets `blocked_since=None`); a stale `blocked_until` surviving into a rerun would silently defer a task nobody is waiting on.

**7. Surface it, or the operator sees a frozen daemon.** `blocked_since` already reaches the read-only views as `parked_since` (`_display_status` renders `running (paused)`, `build_top_snapshot` fills `_ActiveView.parked_since`). Add `parked_until` beside it so `worc top` / `status` reads `running (paused until 07:10)` — otherwise P3's success case is indistinguishable from a hung daemon, which is exactly the diagnosis cost P1's reporting half exists to remove.

## Ordering

**P1 first, alone if necessary.** It is the only part that fixes lost work, and P2/P3 both assume a park exists. P2 second: it removes this trigger class entirely and is independently shippable. P3 last: pure efficiency over a park that must already be correct.

## Tests

The test gap is as much the deliverable as the code — the existing suite asserts this exact scenario and passes.

**Unblock the fixture first, or none of P1 is expressible.** `_both(**kwargs)` in [`test_orchestrator.py`](../../../tests/core/test_orchestrator.py) applies one kwarg set to both fakes. Widen it with per-provider overrides and leave the shared path alone, so all ~40 existing call sites are untouched:

```python
def _both(*, claude=None, codex=None, **kwargs) -> dict[ProviderId, FakeProvider]:
    return {
        ProviderId.CLAUDE: FakeProvider("claude", **{**kwargs, **(claude or {})}),
        ProviderId.CODEX: FakeProvider("codex", **{**kwargs, **(codex or {})}),
    }
```

The shared test config ([`conftest.py`](../../../tests/conftest.py)) is already the incident's shape — `allowed: [claude, codex]`, `claude.primary: true`, both configured — so an unpinned node resolves claude→codex and a mixed pair reproduces the incident exactly. (While there: the note above `test_prompt_audit_*` claiming the global primary's "fallback target is itself (none)" predates symmetric cross-provider failover and now contradicts `resolve_route`; verify and correct it, since a reader would conclude this whole scenario is unreachable.)

**P1 — unit (fast, in `tests/core/`):** `classify_exhaustion` as a decision table, the same shape as the existing `fallback_allowed` table in `tests/routing/`. Every row: containment-anywhere → MANUAL (including containment + rate_limited in both orders), representative CANCELLED + rate_limited → PARK-as-cancel, rate_limited + authentication_failed → PARK, authentication_failed alone → FAIL, empty → FAIL. This is where the precedence is actually pinned; the integration tests below prove it is wired.

**P1 — integration:**

- Mixed-class exhaustion parks: `rate_limited` primary + `authentication_failed` fallback → `RUNNING` + `blocked_since` stamped, no ledger record, no failure report. The direct regression test for the incident.
- Precedence: `rate_limited` primary + `containment_unverified` fallback → `manual_action_required`, `exchange_active_unsafe` set, **not** parked. This is the test that keeps P1 from becoming a security regression.
- `cancelled` on one attempt plus a park-eligible class on another parks with the cancel representative (assert the recorded class, not just the status — the two dispositions are otherwise indistinguishable until P3).
- A rate-limited **evaluator** with a broken fallback parks instead of degrading to `manual_action_required` (decision 6 above), and the green diff survives the park.
- `stuck.md` / `failure_report.json` name every attempt's provider and class; the infra `stuck.md` no longer claims a fix loop exhausted a limit.

**P2:**

- A `LOGGED_OUT` provider fails `run_preflight` whether or not it is the primary, and the message names `agents.allowed` (extend [`test_cli_preflight.py`](../../../tests/test_cli_preflight.py), which already flips `primary` between providers).
- `UNKNOWN` warns and does not fail; a provider with no probe hook prints no auth claim at all.
- `cmd_watch` (daemon and single-pass) and `cmd_run` refuse to start on `LOGGED_OUT`; nothing in `state.db` is touched.
- The Claude probe's `AuthProbe` and every line it produces contain no `email` / `orgId` / `orgName` — assert on a fake probe payload carrying all three.

**P3:**

- A park carrying a `resets_at` stamps `blocked_until`; the next tick returns `RUNNING` without launching a provider (assert on the fake's `requests` list staying empty), and the tick after the instant re-enters.
- Absurd, past, unparseable, and beyond-ceiling values: `blocked_until` is `NULL` or clamped to `blocked_since + max_blocked_s`; the ceiling still fails the task at the ceiling.
- A `CANCELLED` park never gets a `blocked_until`, even when another attempt reported one.
- The instant survives the full six-hop thread — a router-level test asserting `StageOutcome.terminal_error.resets_at` is the cheap guard against hop 4 dropping it, which is the failure this design is most likely to ship with.

## Files touched

Grouped by part, so P1 can ship alone and each part's blast radius is visible before anyone starts.

| Part | Files |
| --- | --- |
| P1 | `core/flow/nodes/base.py` (`NodeInfraError.error_classes`), `core/flow/nodes/agent.py` + `nodes/evaluator.py` (fill the set), `core/orchestrator.py` (`classify_exhaustion`, both dispatch sites, `_write_infra_failure_report`), `ledger.py` (`write_failure_report` attempts section + the infra wording) |
| P2 | `providers/base.py` (`AuthState` / `AuthProbe`, `ProviderHealth.auth` replacing `authenticated`), `providers/_adapter_base.py` (the hook + the healthy-path call), `providers/claude.py` + `providers/codex.py` (the two verbs), `cli.py` (`run_preflight` verdict + line, `require_provider_auth`, `cmd_watch`, `cmd_run`) |
| P3 | `providers/claude.py` (`resetsAt`), `providers/_adapter_base.py` (`ParsedEvents` field + the raise), `providers/base.py` (`NormalizedError.resets_at`, `ProviderError` kwarg), `routing/router.py` (copy at 3 of 4 sites), `nodes/agent.py` + `nodes/evaluator.py`, `core/orchestrator.py` (`_park`, `_resume_via_engine`), `state_store.py` (v21 + `TaskRow`), `cli.py` (`parked_until` in the read-only views) |

`ProviderHealth.authenticated` is read in `run_preflight` and in provider-health tests; grep it before removing so no surface keeps printing a field that no longer exists.

## Acceptance criteria

**P1**

- A `rate_limited` primary with a fallback that fails on _any_ non-park class parks the task (`RUNNING`, `blocked_since` set) and writes no ledger record and no failure report.
- A `containment_unverified` on any attempt routes to `manual_action_required` with the exchange flagged unsafe, no matter what the other attempts reported.
- `classify_exhaustion` is pure and covered by a decision table; both dispatch sites call it, neither re-implements it.
- `stuck.md` and `failure_report.json` list every provider attempt with its provider and error class, and the infra `stuck.md` opening line no longer claims a fix loop exhausted a limit.
- No new config key, no new `ErrorClass`, no schema change.

**P2**

- `ProviderHealth` exposes no boolean that can assert authentication the probe did not prove; a presence-only probe reports `proves_validity=False`.
- `worc preflight` fails on a logged-out provider regardless of its role, with a message naming `codex login` and `agents.allowed`.
- `worc watch` (both modes) and `worc run` refuse to start on a logged-out allowed provider.
- No probe output beyond the login state and the auth method reaches any log line, preflight line, or report.

**P3**

- A Claude-reported `resetsAt` reaches `tasks.blocked_until`, clamped to `max_blocked_s`, validated against the injected clock, ignored when absurd/past/unparseable.
- Ticks between the park and the instant launch no provider and touch no git; the first tick at or after the instant resumes.
- `max_blocked_s` still terminates the task at the ceiling — the provider's instant can never extend it.
- A cancel park carries no `blocked_until`.
- `worc top` / `status` show the wake instant.

Plus the repo-wide Definition of Done from [AGENTS.md](../../../AGENTS.md): `ruff check .`, `ruff format --check .`, `mypy src`, `lint-imports`, `pytest`, and the `interrogate` / `vulture` / `deptry` gates.

## Non-goals

- **No proactive capacity or headroom query** — that is the archived capacity-gate item, deliberately left deferred.
- **No automatic re-login or credential handling.** Credentials stay outside the orchestrator; a dead token is detected and reported, never repaired. (The "Automatic CLI installation/authorization" row in [the backlog README](../README.md) is a separate, deliberately deferred idea.) Reading or parsing `~/.codex/auth.json` / the Claude credential store to inspect a token's expiry is credential handling and is out of scope even though it would technically answer the validity question.
- **No change to fallback eligibility.** `FALLBACK_ELIGIBLE` is untouched; the fallback in the incident was correct. Only what the Core concludes _after_ exhaustion changes.
- **No new error classes**, no change to which classes are infra, no concurrency change.
- **No new config keys in any of the three parts.** The clamp reuses `agents.retry.max_blocked_s`; the auth verdict and the park precedence are policy, not operator preference. A toggle here would let a config weaken the park or the security precedence, which is the opposite of the point.
- **No timer, thread, or scheduler for the wake**, and no re-probing auth at tick boundaries — mid-run credential expiry is real (it happened) but it belongs with a provider cooldown, after this task.

## Docs to sync (on `dev`, in the same change)

`/sync-docs` scopes itself to the branch; on `dev` the surfaces that can go stale here are:

- [`config.example.yaml`](../../../src/wastech_orchestrator/packaged/config.example.yaml) — the `max_blocked_s` comment is the line that states the park contract P1 restores; extend it to say the class is aggregated across attempts and that a provider-reported reset shortens the wait within this ceiling.
- `src/wastech_orchestrator/packaged/guide/` — whichever quickstart describes `worc preflight` / `worc watch` startup, now that a logged-out provider refuses to start; and the operator-facing description of a parked task if it names `blocked_since`.
- [.agents/rules/architecture.md](../../../.agents/rules/architecture.md) — the "Soft, resumable pause" rule reads _"when both retries and cross-provider fallback are exhausted **for a transient class**"_. That singular phrasing is exactly the reading the bug hid behind; restate it as the aggregate ("when **any** attempt in the exhausted stage reported a park-eligible class, and no attempt reported a containment/capability failure").
- **PR doc-impact note (breadcrumb for the `main` reconstruction):** touched provider preflight, the park/fail decision, and `state.db` schema (v21) — likely affects `configuration.md`, `operations.md`, and `worc_architecture.md`.

## Operator recovery for the incident itself (already known, recorded for the reconstruction)

All six tasks are `branch_mode: existing` on an operator-owned branch, so a fresh `rerun` is refused by design — `rerun --continue` resumes from the recorded checkpoint (`implementation` for `p14-03`, which reuses its surviving `plan.md`; `planning` for the other five). The daemon must be stopped first. `source_path` in `state.db` already points into `tasks/failed/`, so the files need no moving.
