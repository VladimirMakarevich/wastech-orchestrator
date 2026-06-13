# Backlog: Runtime provider capacity gate for autonomous watch mode

Status: **backlog / not scheduled**
Date: 2026-06-13
Owner: Vladimir Makarevich

This document captures the product task of checking Codex and Claude Code subscription capacity
before an autonomous `watch` process admits a pending task into the active pipeline. It is a
backlog item, not current runtime behavior. Nothing here overrides the canonical specification,
[CLAUDE.md](../../CLAUDE.md), [AGENTS.md](../../AGENTS.md), or the hard invariants in
[docs/rules/](../rules/).

## 1. Background

The orchestrator is intended to run unattended in `watch` mode. A task may remain pending for
hours and then be selected after an operator has left the machine. The current provider
`preflight()` checks executable availability and CLI version, but it does not determine whether
the authenticated Codex or Claude account has enough current subscription capacity to start a
multi-stage task.

Without a runtime capacity gate, the orchestrator can:

- take a task out of the pending queue;
- create its task branch and occupy the single active-task slot;
- complete one or more stages;
- then receive a provider rate-limit error because the five-hour or weekly allowance is exhausted;
- spend attempts on fallback or stop in a partially completed state even though waiting for the
  provider reset would have been preferable.

This is different from the operator-facing `wastech-orchestrator preflight` command. Installation
preflight answers whether the configured CLIs and isolation policy are usable. The proposed gate
is a runtime admission decision made automatically whenever `watch` considers starting a new task.

## 2. Goal

Before `watch` admits a pending task, determine whether the providers required by that task's
resolved routes have sufficient reported capacity under configured headroom thresholds.

The desired flow is:

```text
watch tick
    -> refresh base branch and discover pending tasks
    -> validate the next task
    -> resolve its provider routes
    -> query current provider capacity
    -> capacity sufficient: claim the active slot and start the task
    -> capacity insufficient: leave the task pending and defer until a later eligible check
```

The gate should reduce avoidable mid-task rate-limit failures while preserving deterministic
routing, infrastructure-only fallback, the single-active-task invariant, and autonomous operation.

## 3. Non-goals and limits

The gate cannot prove that a task will finish within the available allowance:

- neither provider reports how many tokens an arbitrary future coding task will consume;
- subscription limits are generally reported as utilization percentages, not a guaranteed
  remaining token budget;
- usage may change concurrently on another device, in another process, or through another product
  sharing the same allowance;
- task size, model choice, reasoning effort, tool output, fixes, and review findings make future
  consumption uncertain;
- provider capacity interfaces may be unavailable or change between CLI/SDK versions.

Therefore the feature is a **headroom admission policy**, not a completion guarantee or a token
estimator. Historical per-stage usage may improve policy later, but it must not be presented as an
exact prediction.

This task does not:

- replace installation `preflight`;
- change rate-limited errors from infrastructure errors;
- allow the Core to know provider CLI syntax;
- automatically purchase credits or change account limits;
- read or persist provider credentials;
- introduce concurrent task execution.

## 3.1. Authentication and billing modes

The admission policy must distinguish how each provider is authenticated and billed:

| Mode | Capacity model | Recommended pickup behavior |
| --- | --- | --- |
| ChatGPT/Codex subscription | Shared five-hour/weekly allowance and optional credits | Apply subscription capacity thresholds |
| Claude.ai/Claude Code subscription | Five-hour/weekly/model-specific allowance and optional extra usage | Apply subscription capacity thresholds |
| OpenAI API key | Usage-based API billing plus API rate/spend limits | Skip subscription-window thresholds |
| Anthropic API key | Usage-based API billing plus API rate/spend limits | Skip subscription-window thresholds |
| Bedrock, Vertex, Foundry, or another third-party provider | Provider-specific billing and quota controls | Skip subscription thresholds unless a dedicated adapter is configured |
| Unknown authentication mode | Cannot determine applicable capacity model safely | Apply `unknown_capacity` policy |

Using an API key does not mean that capacity is unlimited. API requests may still fail because of
rate limits, exhausted prepaid balance, organization/project spend limits, provider availability,
or an operator-defined task budget. However, these conditions are not equivalent to subscription
window utilization and should not be evaluated using five-hour/weekly percentage thresholds.

The minimum implementation should therefore:

1. detect and normalize the provider authentication/billing mode without exposing credentials;
2. run subscription headroom checks only for subscription-authenticated providers;
3. return a distinct `not_applicable` result for usage-based API authentication;
4. admit API-backed tasks when the subscription gate is the only enabled policy;
5. continue handling an actual API `429`, quota, or billing failure through the existing normalized
   provider-error path.

A later, separate **API budget gate** may enforce operator-defined controls such as:

- maximum estimated or actual cost per task;
- maximum cumulative daily/monthly orchestrator spend;
- required prepaid balance, where an official supported balance endpoint exists;
- project/workspace spend ceilings;
- API request/token headroom derived from supported rate-limit APIs or response headers.

That future gate must not issue a model request merely to discover response headers: the probe
would consume money/capacity and still would not guarantee availability for the later task.

## 4. Provider capability research

The following interfaces were available when this backlog item was written on June 13, 2026.
Their stability must be re-verified immediately before implementation.

### 4.1. Codex

Codex App Server exposes the read-only RPC:

```text
account/rateLimits/read
```

The response can include:

- primary utilization and window duration, normally the five-hour window;
- secondary utilization and window duration, such as a weekly window;
- reset timestamps;
- `rateLimitReachedType`;
- plan type;
- credit availability and balance;
- multiple named limits through `rateLimitsByLimitId`.

The RPC does not create an agent turn and is suitable for a read-only capacity query. The installed
Codex CLI still labels App Server as experimental, so the adapter must handle protocol or schema
changes without crashing the watch loop.

Official reference:
[Codex App Server account and rate-limit methods](https://developers.openai.com/codex/app-server#6-rate-limits-chatgpt).

### 4.2. Claude Code subscriptions

Claude Code's interactive `/usage` command displays session cost and plan usage limits, but its
human-oriented terminal output should not be scraped.

Starting with Anthropic Claude Agent SDK for TypeScript `0.3.169`, an experimental Query method is
available:

```typescript
usage_EXPERIMENTAL_MAY_CHANGE_DO_NOT_RELY_ON_THIS_API_YET()
```

Its structured response can include:

- five-hour utilization and reset time;
- seven-day utilization and reset time;
- separate Opus and Sonnet weekly windows;
- extra-usage credit state;
- subscription type;
- whether subscription rate-limit data is available.

The method name explicitly declares that the API is unstable. An implementation should pin and
probe a supported SDK version, isolate schema parsing in `ClaudeCodeProvider`, and degrade according
to configured unknown-capacity policy when the method is unavailable.

Official references:

- [Claude Code commands (`/usage`)](https://code.claude.com/docs/en/commands)
- [Claude Agent SDK TypeScript releases](https://github.com/anthropics/claude-agent-sdk-typescript/releases/tag/v0.3.169)

### 4.3. Claude API organization limits are a separate case

Anthropic's Rate Limits Admin API returns configured organization/workspace API limits and requires
an Admin API key. It is unavailable to individual accounts and is not a direct replacement for
Claude Code subscription utilization.

If API-key provider backends are added later, API capacity should be treated as a separate
capability using configured limits, usage data, and response headers. Subscription and API
capacity must not be conflated.

Reference:
[Anthropic Rate Limits API](https://platform.claude.com/docs/en/manage-claude/rate-limits-api).

## 5. Proposed architecture

Extend the provider abstraction with a provider-neutral capacity query. Provider-specific process
and protocol details remain in `src/wastech_orchestrator/providers/`.

Conceptual contract:

```text
AgentProvider
  id
  preflight() -> ProviderHealth
  capacity() -> ProviderCapacity
  run(AgentRunRequest) -> AgentRunResult
```

Suggested normalized structures:

```text
ProviderCapacity
  provider_id
  status                  # sufficient | insufficient | unavailable | unsupported | not_applicable
  billing_mode            # subscription | api | third_party | unknown
  observed_at
  windows[]
  credits
  source
  message

CapacityWindow
  kind                    # five_hour | weekly | model_weekly | other
  model_scope
  used_percent
  remaining_percent
  resets_at

ProviderCredits
  available
  unlimited
  balance                 # optional redacted/non-secret display value
```

The normalized contract should preserve enough provider evidence for a deterministic policy while
avoiding raw vendor payloads in Core. Unknown fields must be ignored forward-compatibly.

Suggested components:

```text
Watch Loop
    -> Task Validator
    -> Agent Router (resolve routes)
    -> Capacity Gate
         -> AgentProvider.capacity()
         -> Capacity Policy
         -> Deferral Store
    -> active-task claim
    -> normal pipeline
```

The Capacity Gate owns provider-neutral admission policy. Each adapter owns command construction,
SDK/RPC protocol handling, authentication interpretation, response parsing, and provider-specific
error normalization.

## 6. Admission point and task lifecycle

The capacity gate must run after enough task validation and route resolution to know which
providers are needed, but before any externally visible task work begins.

Required ordering:

1. discover a candidate pending task;
2. perform structural validation that has no task-branch side effects;
3. resolve global routes and validated task overrides;
4. determine the providers relevant to agent-driven stages;
5. query and evaluate provider capacity;
6. only on admission, claim the single active-task slot and create the task branch;
7. start the normal stage pipeline.

An insufficient-capacity result must not:

- move the task out of `pending`;
- create a task branch;
- occupy the active-task slot;
- create a provider attempt or consume `stage_attempts`;
- consume the fixing budget;
- trigger provider fallback;
- transition the task to `failed` or `manual_action_required`.

Capacity deferral is queue scheduling, not an attempted task execution.

## 7. Which providers to check

The gate should inspect providers used by the task's resolved routes, not every provider listed in
`agents.allowed`.

For the current default routes, this normally means both Claude and Codex because Claude handles
implementation-oriented stages and Codex is the primary reviewer. Task-level route overrides may
change the set.

Policy options should distinguish:

- **all routed providers required:** do not start unless every provider that may be needed has
  sufficient capacity;
- **primary providers required:** admit when stage primaries are sufficient, treating fallback
  capacity as advisory;
- **first-stage provider only:** weakest policy; not recommended because it readily admits tasks
  that cannot reach review or fixing.

Recommended default: require sufficient capacity for all primary providers across agent-driven
stages and report fallback-provider capacity separately. A stricter operator setting may require
both primary and fallback capacity.

## 8. Capacity policy

Illustrative configuration:

```yaml
agents:
  capacity_gate:
    enabled: false
    check_on_task_pickup: true
    check_before_each_stage: true
    required_routes: primary

    minimum_remaining:
      five_hour_percent: 25
      weekly_percent: 15

    unknown_capacity: defer
    query_timeout_seconds: 10
    minimum_recheck_seconds: 60
    notification_cooldown_seconds: 3600
```

Possible `unknown_capacity` values:

- `defer`: fail closed for autonomous operation;
- `warn`: admit the task but record that capacity could not be verified;
- `allow`: admit silently except for audit logging; primarily useful during staged rollout.

Configuration must define global ceilings. A task must not be able to lower headroom thresholds,
disable the gate, or change unknown-capacity policy. At most, future task metadata may request
stricter thresholds.

`not_applicable` is not the same as `unavailable`: it means the adapter successfully determined
that subscription windows do not apply, for example because the CLI uses an API key. Under a
subscription-only capacity policy, `not_applicable` passes admission without warning. If a future
API budget gate is enabled, that separate policy decides whether the API-backed task may start.

Threshold evaluation should:

- calculate remaining percentage as `100 - used_percent` when only utilization is reported;
- evaluate every applicable required window;
- use the most restrictive relevant window;
- treat an explicit provider-reported exhausted/rejected state as insufficient regardless of
  percentage;
- consider usable paid/extra credits only when operator policy explicitly permits them;
- preserve provider reset timestamps for scheduling and diagnostics.

## 9. Deferral and autonomous retry behavior

When capacity is insufficient:

1. keep the task in `pending`;
2. persist a small capacity-deferral record;
3. log one structured event with provider, window, utilization, threshold, and reset time;
4. schedule the next eligibility check no earlier than the configured minimum interval;
5. when a trustworthy reset time exists, avoid repeated provider queries until near that reset;
6. re-evaluate automatically in later `watch` ticks;
7. notify the operator once, then suppress duplicate notifications until the reason changes or the
   cooldown expires.

Suggested persisted fields:

```text
task_id
provider
window_kind
used_percent
required_remaining_percent
observed_at
resets_at
next_check_at
reason
source
```

Do not persist raw authentication data, vendor access tokens, full RPC payloads, or full process
environment.

### Queue ordering

The first implementation should preserve strict deterministic pending order: if the first eligible
task is capacity-deferred, the queue waits.

Skipping a deferred task and considering later tasks may improve throughput, especially when tasks
use different providers, but it introduces fairness and starvation concerns. That should be a
separate opt-in policy with:

- a bounded scan of pending tasks;
- auditable skip reasons;
- an aging or fairness rule;
- protection against a large task being deferred forever.

## 10. Rechecks during an active task

Task-pickup admission reduces risk but cannot protect a long pipeline from concurrent usage or
changing limits. The recommended design also supports a capacity recheck immediately before each
agent-driven stage.

Stage rechecks differ from initial admission:

- the task already owns the active slot and may have a branch and partial changes;
- insufficient capacity should pause/defer the active task rather than return it to the pending
  queue;
- no provider attempt should be created until the stage is actually launched;
- recovery after restart must preserve the paused stage and resume it after capacity becomes
  sufficient;
- a capacity query failure must follow the configured unknown-capacity policy;
- an actual provider `rate_limited` error remains an infrastructure error and retains the existing
  fallback behavior.

This active-task pause state needs explicit state-machine design before implementation. If that
scope is deferred, the minimum viable version may implement pickup-only admission while retaining
current mid-pipeline fallback behavior.

## 11. Failure handling

Capacity query failures must be distinguished from provider run failures:

| Condition | Capacity result | Default admission behavior |
| --- | --- | --- |
| Supported response, above thresholds | `sufficient` | Start task |
| Supported response, below threshold | `insufficient` | Defer task |
| Explicit limit exhausted/rejected | `insufficient` | Defer task |
| Provider capacity API unsupported | `unsupported` | Apply `unknown_capacity` |
| Protocol/schema changed | `unavailable` | Apply `unknown_capacity`, alert once |
| Query timeout/network error | `unavailable` | Apply `unknown_capacity`, retry later |
| Not authenticated | `unavailable` | Defer and report actionable auth diagnosis |
| API-key auth without subscription windows | `not_applicable` | Skip subscription gate; use future API budget policy if configured |
| Third-party billing without a dedicated capacity adapter | `not_applicable` | Skip subscription gate; use provider-specific policy if configured |

Do not reinterpret a capacity-query protocol error as a task quality failure. Do not run an agent
turn merely to test capacity, because that would consume the resource being measured.

## 12. Security requirements

- Provider credentials remain managed outside the orchestrator.
- Capacity subprocesses receive only the existing allowlisted environment.
- All commands use argv lists without shell interpolation.
- RPC/SDK output is parsed in memory and normalized before persistence.
- Logs and artifacts contain no access tokens, cookies, authorization headers, or raw credential
  files.
- Provider-specific SDK additions must not introduce proxying or traffic interception.
- A task and `extra_args` cannot disable the capacity gate or lower configured thresholds.
- Timeouts are mandatory so a broken capacity endpoint cannot hang `watch`.

## 13. Observability and operator experience

`status` should distinguish:

```text
pending
pending_capacity_deferred
active
active_capacity_paused
```

Example diagnostic:

```text
task task-123 deferred before admission
provider: codex
window: weekly
remaining: 8%
required: 15%
observed: 2026-06-13T16:10:00Z
next check: 2026-06-18T18:46:47Z
```

Useful metrics:

- capacity checks by provider and result;
- task deferrals and deferral duration;
- admission-to-rate-limit failure rate;
- unknown/unsupported capacity checks;
- capacity API latency and schema errors;
- remaining headroom at task admission;
- number of duplicate notifications suppressed.

Capacity snapshots should be bounded and retained according to the existing artifact/log policy.

## 14. Interaction with token usage measurement

This feature complements
[token optimization](token_optimization.md), especially its Phase 0 measurement:

- persist `AgentRunResult.usage` per provider attempt;
- build per-provider, per-model, per-stage distributions;
- compare admitted headroom with actual task outcomes;
- tune thresholds from evidence rather than guesses.

A later version may derive conservative estimated task demand from historical percentiles and task
metadata. Such estimates must include uncertainty and must never claim exact completion guarantees.

## 15. Testing requirements

### Unit tests

- normalization of Codex primary/secondary windows and credits;
- normalization of Claude five-hour, weekly, model-specific, and extra-usage data;
- authentication/billing-mode normalization without exposing credentials;
- API-key and third-party modes return `not_applicable`, not `unsupported`;
- forward-compatible handling of unknown fields;
- malformed payload, timeout, unsupported version, and unauthenticated results;
- threshold boundaries and most-restrictive-window selection;
- required provider set derived from resolved task routes;
- task overrides cannot weaken global capacity policy;
- no attempt/fix counters are consumed on admission deferral;
- duplicate notification suppression and `next_check_at` calculation;
- secret redaction for all diagnostics and persisted records.

### Integration tests

Use fake provider capacity endpoints/processes:

- both providers sufficient -> task starts;
- API-key provider -> subscription gate is skipped and task starts;
- mixed route with one subscription provider and one API-key provider -> check only the
  subscription provider;
- primary insufficient -> task remains pending with no branch;
- fallback insufficient under `required_routes: primary` -> task starts with warning;
- unknown capacity under each configured policy;
- reset timestamp passes -> next watch tick rechecks and admits;
- capacity protocol changes -> watch remains alive and task is handled by policy;
- restart preserves capacity deferral and does not duplicate task admission.

### End-to-end tests

- autonomous `watch` discovers a task, defers it without side effects, then starts it after a fake
  reset;
- strict queue order blocks later tasks while the head task is deferred;
- optional future skip-deferred policy preserves fairness;
- pickup capacity checks do not invoke `codex exec` or `claude -p`;
- stage recheck, if implemented, pauses and resumes without duplicate provider attempts, commits,
  pushes, or PRs.

Real provider accounts must not be required in deterministic CI.

## 16. Rollout plan

### Phase 1: capacity contract and diagnostics

- add provider-neutral capacity structures;
- implement read-only adapter queries;
- expose results through `status` or a diagnostic command;
- do not block task admission yet.

### Phase 2: pickup admission gate

- add configuration and validation;
- evaluate resolved primary routes before task claim/branch creation;
- persist deferrals and schedule autonomous rechecks;
- add notification deduplication.

### Phase 3: stage-level rechecks and pause/resume

- design the explicit active capacity-paused state;
- recheck immediately before agent-driven stages;
- resume safely after provider reset and orchestrator restart.

### Phase 4: evidence-based thresholds

- combine capacity snapshots with persisted provider usage;
- publish operator guidance based on measured stage/task distributions;
- optionally support conservative per-model/per-stage thresholds.

## 17. Open questions

- Should the default unknown-capacity policy be `defer` for fully autonomous operation or `warn`
  while provider interfaces remain experimental?
- Should fallback-provider capacity be required at admission or only reported?
- May paid/extra credits satisfy the gate automatically, or must an operator explicitly opt in?
- Should an API budget gate be part of this feature or remain a separate backlog task?
- Which official provider endpoints are sufficiently stable to check API balance/spend without
  issuing a billable model request?
- Does pickup-only gating provide enough value for the first release, or is active-task pause/resume
  required in the same change?
- Should strict pending order remain mandatory, or should capacity-deferred tasks allow a bounded
  scan for work that uses a different provider?
- What initial thresholds are justified before real per-stage usage baselines exist?

## 18. Acceptance criteria

- In autonomous `watch` mode, a pending task is not claimed or branched when a required provider is
  below configured capacity thresholds.
- The deferred task remains pending and is reconsidered automatically without operator action.
- Deferral consumes no stage attempt or fixing budget.
- Provider-specific capacity protocols remain confined to provider adapters.
- Unknown/unavailable capacity follows explicit, validated policy.
- API-key and third-party billing modes skip subscription-window thresholds through an audited
  `not_applicable` result rather than being treated as unlimited or broken.
- Reset times and decisions are audited without storing secrets or raw credentials.
- Capacity is re-evaluated after restart without duplicate task execution.
- Existing infrastructure-only fallback semantics remain unchanged for actual provider runs.
- Documentation clearly states that the gate provides headroom, not a completion guarantee.
