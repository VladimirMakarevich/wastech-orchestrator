# Transient provider-failure recovery (API 5xx mid-task)

Status: **implemented** (2026-06-27, A + symmetric fallback + B-lite — see [§ Decision (locked)](#decision-locked)) Date: 2026-06-23 Owner: Vladimir Makarevich

> **Implemented 2026-06-27.** All three parts shipped in one change: bounded same-provider transient retry with backoff (`agents.retry`), symmetric Claude↔Codex fallback (`resolve_route`), and the B-lite resumable soft pause (`tasks.blocked_since`, ceiling on resume). **Version note:** the plan text below predates later schema bumps — the actual bumps were **config `schema_version` 19 → 20** (not 15 → 16) and **`DB_SCHEMA_VERSION` 12 → 13** (B-lite's `blocked_since` column). **Phase 0 note:** the CLI-internal-retry verification spike was _not_ run (it needs the real binaries against a live outage, not reproducible in CI); the ADR's stated defaults shipped as-is and are operator-tunable — tracked in [follow_ups.md](../../follow_ups.md).

Detail file for the [README.md](../../README.md) backlog item _"Auto-retry on network errors — retry transient network/provider failures before fallback or terminal failure (must be bounded and audited; never retry quality failures)."_ It records the investigation of what happens today when a provider returns a transient server error mid-task, the options for surviving it, and the **locked decision** (A + symmetric fallback + B-lite) with its implementation plan ([§ План реализации](#план-реализации)).

## The situation

A coding-agent CLI (Codex / Claude Code) sometimes dies mid-run with a transient, server-side error:

```
API Error: 500 Internal server error. This is a server-side issue, usually
temporary — try again in a moment. If it persists, check https://status.claude.com.
```

By its own description this class of failure is **transient and idempotent to retry** — the work was not rejected on quality grounds, the request simply did not reach a healthy backend. The question: does the orchestrator recover and continue, and if not, how should it?

## TL;DR (findings)

1. The 500 is correctly classified as `PROVIDER_UNAVAILABLE` (an infrastructure error class), via a per-adapter stderr signature.
2. **On a default-flow node there is no fallback and no retry.** Every node in the packaged flows runs on the single global primary provider, and the Router only has a fallback target when the node's provider differs from the global primary. So the attempt sequence is one provider, one attempt — the first transient blip raises `NodeInfraError`, which the orchestrator turns into a terminal `FAILED` task.
3. There is **no same-provider retry and no backoff** for this class. The only same-provider retry in the Router is the `SESSION_UNAVAILABLE` safety net; the `max_stage_attempts: 3` budget is otherwise spent only on the (here non-existent) cross-provider fallback.
4. The orchestrator already has rich **checkpoint + resume + durable-session** machinery — but it is wired to _process crashes_ and the _quality fix-loop_, never to infra failures. An infra failure is caught and converted straight to terminal `FAILED`, so the process never crashes and restart-recovery never sees a resumable task.

Net: the error class that is _most_ recoverable in principle (transient, server-side, safe to retry) is today the _least_ recovered in practice — a single blip on the global-primary path discards the entire task's progress.

**Recommended direction:** Option A (bounded same-provider retry with backoff for the transient infra classes, reusing the existing `SESSION_UNAVAILABLE` retry shape in the Router) as the core fix, plus Option B-lite (on infra-exhaustion, checkpoint-and-pause into a _resumable_ state instead of immediate `FAILED`) for sustained outages. Both reuse machinery that already exists.

## How a 500 is handled today (traced end to end)

### 1 — Surfacing and classification (the adapter)

The CLI exits non-zero with the error on stderr. The adapter's `classify()` matches a provider-specific stderr signature and raises a `ProviderError` carrying a normalized, secret-free error class ([providers/errors.py:63-86](../../../../src/wastech_orchestrator/providers/errors.py#L63-L86)). Both adapters map a 5xx to `PROVIDER_UNAVAILABLE`:

- Claude — `service unavailable|\b50[023]\b|bad gateway|internal server error` ([claude.py:109-110](../../../../src/wastech_orchestrator/providers/claude.py#L109-L110))
- Codex — same pattern ([codex.py:88-91](../../../../src/wastech_orchestrator/providers/codex.py#L88-L91))

Two classification edges worth noting, because they change which recovery path runs:

- The signature matches `500/502/503` but **not `504`** (a gateway timeout would fall through to `PROCESS_CRASHED`, or to `TIMEOUT` if the watchdog fired first) and **not `529`** (Anthropic's "overloaded"). For Claude, `529` is instead caught by the rate-limit signature (`overloaded`) → `RATE_LIMITED` ([claude.py:97](../../../../src/wastech_orchestrator/providers/claude.py#L97)); Codex has no `overloaded` token, so a `529` there would land in `PROVIDER_UNAVAILABLE`/`PROCESS_CRASHED`. Any "transient retry" policy must decide which of these classes it covers.

### 2 — Routing (the Router)

`PROVIDER_UNAVAILABLE` is in `FALLBACK_ELIGIBLE` ([providers/base.py:48-60](../../../../src/wastech_orchestrator/providers/base.py#L48-L60)), so a fallback is _allowed_. But whether one _exists_ depends on the route:

- The Router builds its attempt sequence as `[primary]` plus `[fallback]` only when a fallback exists ([router.py:191-193](../../../../src/wastech_orchestrator/routing/router.py#L191-L193)).
- The fallback is the global primary, _unless the resolved primary already is the global primary_, in which case `fallback is None` and "a primary infra failure is terminal" ([router.py:169-173](../../../../src/wastech_orchestrator/routing/router.py#L169-L173)).
- **No packaged flow declares a per-node `provider:`** (verified: zero `provider:` keys across `packaged/flows/*.yaml`). So every default-flow node resolves its primary to the global primary → `fallback is None` → the sequence is a single attempt.

There is **no backoff and no same-provider retry** for `PROVIDER_UNAVAILABLE`. The _only_ same-provider retry is the `SESSION_UNAVAILABLE` safety net: when a session could not be resumed, the Router retries the same provider once with a fresh session ([router.py:248-299](../../../../src/wastech_orchestrator/routing/router.py#L248-L299)). That path is the natural template for a transient-retry feature, but `PROVIDER_UNAVAILABLE` does not use it.

When attempts are exhausted (or there is no fallback), the Router returns a `StageOutcome` with `result=None` and `terminal_error` set ([router.py:361-369](../../../../src/wastech_orchestrator/routing/router.py#L361-L369)). Note it _does_ capture the partial diff and would hand it to a fallback without rolling back ([router.py:327-329](../../../../src/wastech_orchestrator/routing/router.py#L327-L329)) — but with no fallback, nothing consumes it.

### 3 — Core reaction (the node runner and orchestrator)

A `StageOutcome` with `result is None` makes the agent (or evaluator) node raise `NodeInfraError` ([agent.py:235-241](../../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L235-L241), [evaluator.py:91-95](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L91-L95)). The orchestrator catches it and **fails the task** ([orchestrator.py:1201-1203](../../../../src/wastech_orchestrator/core/orchestrator.py#L1201-L1203)):

```python
except NodeInfraError as exc:
    self._sync_counters_from_run_state(p, run_state)
    return self._fail(p, str(exc))
```

`_fail` commits and pushes the failed attempt (no PR), then transitions to terminal `FAILED` ([orchestrator.py:1689-1711](../../../../src/wastech_orchestrator/core/orchestrator.py#L1689-L1711)). `FAILED` is terminal — it has no outgoing transitions and releases the processing slot ([state_machine.py:37-38](../../../../src/wastech_orchestrator/core/state_machine.py#L37-L38)). There is no automatic re-queue.

### 4 — The recovery machinery that exists, and why it does not help here

The orchestrator is _not_ short on recovery primitives:

- **Checkpoints.** `current_node` is seeded and `recorder.save_checkpoint(run_state)` is written before each phase ([orchestrator.py:1249-1250](../../../../src/wastech_orchestrator/core/orchestrator.py#L1249-L1250)). The phase driver can resume from the hydrated `current_node`.
- **Restart recovery.** On startup the reconciler resumes the single _active_ task idempotently (`RecoveryAction.RESUME`), skipping already-committed subtasks and never re-committing a recorded SHA ([core/recovery.py:57-107](../../../../src/wastech_orchestrator/core/recovery.py#L57-L107)).
- **Durable provider sessions.** A `resume_own_lineage` node (the supervisor / research critic) persists and rehydrates its provider session across restarts via the `node_lineage` table ([core/supervisor.py:156-187](../../../../src/wastech_orchestrator/core/supervisor.py#L156-L187)).

The reason none of this fires for a transient 500: **an infra failure is _caught_, not a crash.** It is converted to terminal `FAILED` inside the same process. The process keeps running, so restart-recovery never triggers; and a `FAILED` task is no longer active, so the reconciler would not resume it even on a later restart. The machinery is keyed to _process death_ and _quality reworks_, not to a provider blip.

### The gap, in one sentence

The orchestrator can resume a task after _its own_ crash, but throws the task away on the _provider's_ transient crash — even though the provider's crash is the one explicitly labelled "temporary, try again in a moment."

## Constraints that bound any solution

These come from [.agents/rules/architecture.md](../../../../.agents/rules/architecture.md) and the code as it stands:

1. **Never retry a quality failure.** Only a _raised infrastructure_ `ProviderError` may be retried; a returned `AgentRunResult(status=failed)` is a quality verdict and must flow to the fix-loop, never be re-run as infra ([router.py:17-18](../../../../src/wastech_orchestrator/routing/router.py#L17-L18)). This boundary already exists and must be preserved.
2. **Bounded and audited.** Any retry must have a hard cap and every attempt must be recorded (the Router already writes a `provider_attempts` audit row per attempt — [router.py:114-123](../../../../src/wastech_orchestrator/routing/router.py#L114-L123)).
3. **Commit/push/PR is orchestrator-only and _post_-node.** A node never commits; the orchestrator commits after the node returns. So a retried or resumed node **cannot double-commit**, and Git Manager fingerprints already enforce commit/push/PR idempotency. This is why a transient retry is safe even when the failed run made partial edits.
4. **The core does not know CLI syntax; retry/backoff belongs in the Router.** The Router already owns the attempt budget _and_ the one existing same-provider retry — it is the correct home, no new layer needed.
5. **Single processing slot.** A backoff that sleeps blocks the slot. For a single-active-task orchestrator this is acceptable _if bounded_ to seconds–low-minutes; a sustained outage should not hold the slot for an hour.
6. **The CLIs already retry internally.** Both Codex and Claude Code perform their own backoff-retry on 5xx/429 before exiting non-zero. **Implication:** by the time the orchestrator observes `PROVIDER_UNAVAILABLE`, the CLI has _already_ exhausted its own retry budget, so an _immediate_ orchestrator re-invocation will likely hit the same outage. The value of an orchestrator-level retry is therefore the **backoff window** (wait out a longer blip) and the **alternate engine** (a different backend), not a tight loop. _(Exact CLI retry counts/knobs to be verified per CLI version — see open questions.)_

## Options

### A — Bounded same-provider retry with backoff in the Router _(recommended core)_

Treat a small, explicit set of _transient_ infra classes — start with `PROVIDER_UNAVAILABLE` and `NETWORK_UNAVAILABLE`; consider `TIMEOUT` — as **same-provider retryable**: before declaring the stage exhausted, retry the same provider up to _N_ times with exponential backoff + jitter, then fall back to the other engine (if any), then terminal. Resume the node's session when one is available (so partial reasoning/edits are not lost); if the session itself was the outage's casualty, fall back to a fresh attempt that sees the partial diff — exactly the degrade path the `SESSION_UNAVAILABLE` retry already uses.

- **Reuses** the `SESSION_UNAVAILABLE` same-provider-retry shape ([router.py:248-299](../../../../src/wastech_orchestrator/routing/router.py#L248-L299)) — a known, tested, accepted pattern. New surface is one config block (`agents.retry: {max_attempts, base_delay_s, max_delay_s}`) and a backoff sleep.
- **Fixes the common case** the current design misses: a default-flow node on the global primary, where there is no fallback today, finally gets a recovery path.
- **Honors the "try again in a moment" semantics** and stays inside one node run — no fix-iteration charged, no state-machine change.
- **Bounded + audited** by construction (cap + existing per-attempt audit rows).
- **Cost / risk:** the backoff sleep holds the single slot (constraint 5 — keep the cap small, e.g. ≤3 attempts, ≤~60s total); given constraint 6, the backoff _window_ is the point, so the delays must be long enough to matter (seconds, not milliseconds), and `RATE_LIMITED` should be _excluded_ from tight retry (it wants a long defer, not a 30s loop).

### B — Non-terminal, resumable state on infra-exhaustion _(recommended complement)_

When retries (Option A) _and_ fallback are exhausted, do **not** convert straight to terminal `FAILED`. Instead persist the checkpoint and stop into a **resumable** state — either keep the task active so restart-recovery resumes it, or re-queue it to `pending` with a cool-off before the next `watch` tick. This leans entirely on the _existing_ checkpoint + resume machinery (§4): the task resumes from `current_node`, committed subtasks are skipped, nothing is re-committed.

- **Right tool for a _sustained_ outage** (minutes–hours, or both engines down): Option A's seconds-scale window cannot ride that out, but B preserves the whole task's progress instead of discarding it.
- **B-lite (smallest viable):** just don't `_fail()` on the transient-infra-exhausted case — checkpoint and pause, with a `max_blocked_duration` after which it _does_ go `FAILED`/`manual_action_required` so a task cannot hang forever. No new status strictly required if we reuse the active+restart-resume path; a dedicated `blocked` status would be clearer but is a larger state-machine change.
- **Cost / risk:** touches orchestrator queue/terminal semantics and needs a max-blocked policy + audit; a paused task holds its context/branch. Larger than A; do it _after_ A or together.

### C — Operator-driven manual retry from checkpoint _(cheap complement / fallback)_

Keep today's behavior but add a CLI `retry <task-id>` that re-queues a `FAILED` task from its last checkpoint (the audit and branch are already persisted by `_fail`). Lowest engineering cost; gives a human a button. But the human _is_ the recovery mechanism — useless for unattended `watch` runs. Best as a complement to A/B, not a substitute.

### D — Do nothing in the orchestrator; rely on / tune the CLIs' own retry _(baseline + caveat)_

Accept that the CLIs already retry 5xx internally (constraint 6) and treat a surfaced `PROVIDER_UNAVAILABLE` as a genuinely sustained outage that _should_ fail. Optionally raise the CLIs' own retry budgets via their config/env where exposed. This is the honest baseline and an important caveat that _shapes_ A (it's why A needs a real backoff window, not a tight loop). On its own it leaves the core gap unsolved: a sustained-enough blip still discards all task progress.

### E — Pre-admission capacity gate _(related, not a recovery)_

The existing [runtime provider capacity gate](../../README.md) backlog item would check provider health _before_ `watch` claims a pending task, deferring admission when a provider is down. Worth noting because it reduces _how often_ we start a task into an outage — but it does **not** recover a task that fails _mid-run_, which is this document's problem. Complementary, not a substitute.

## Decision (locked)

**Locked 2026-06-23: adopt A + symmetric cross-provider fallback + B-lite**, with **D** as the framing caveat and **C** as an easy add-on. The locked target flow is: transient failure → N same-provider retries with backoff → switch to the other allowed provider (Claude↔Codex, symmetric — an explicit extension of the PRE.1 single-fallback rule) → soft, resumable pause only when **both** providers are unavailable. The implementation plan (all parts shipped as one change) is in [§ План реализации](#план-реализации) (in Russian, at the owner's request).

- **A** turns the most common failure (transient 500 on the global-primary path) from "task dead" into "task waits a few seconds and continues," reusing the `SESSION_UNAVAILABLE` retry shape and the post-node-commit idempotency that already make this safe. Smallest change, highest leverage, fully inside the Router.
- **B-lite** ensures a _sustained_ outage parks the task as resumable instead of discarding progress, reusing the checkpoint/restart-resume machinery — gated by a `max_blocked_duration` so nothing hangs forever.
- **D** is why A's backoff must be a real window (and why `RATE_LIMITED` is deferred, not tight-retried), and why B matters at all.

Deliberately **out of scope** for a first cut (YAGNI, per the greenfield-MVP posture): a new first-class `blocked` status (reuse active+resume first), vendor session transfer between engines (artifacts, not sessions, are the source of truth — already a "not supported" backlog line), and per-model/per-node retry tuning (one `agents.retry` block to start).

## План реализации

Раздел на русском по просьбе владельца. Зафиксированное решение — **A + симметричный fallback + B-lite**. **Все фазы реализуются одновременно, в рамках одного изменения** — деление на «Фазы» ниже это логическая структура работ, а не отдельные итерации/мержи. **Проверки запускаются один раз, в самом конце, после всех изменений** (`ruff` + `mypy` + `pytest` через `/run-checks`, затем `/sync-docs` и `prettier` по докам) — см. [§ Проверки и документация](#проверки-и-документация-в-самом-конце).

Целевой сквозной сценарий (то, что просил владелец): провайдер падает с транзиентной ошибкой → **N повторов того же провайдера** с backoff → если не помогло, **переключение на второй разрешённый провайдер** (Claude↔Codex, симметрично) → если **оба недоступны** — **мягкое завершение** (резюмируемая пауза, а не жёсткий `FAILED`).

### Зафиксированные решения (ответы на открытые вопросы)

1. **Какие классы считаем «транзиентными» (ретраим в A).** Только `PROVIDER_UNAVAILABLE` и `NETWORK_UNAVAILABLE`. `TIMEOUT` **исключаем** (таймаут часто означает частично/долго выполнявшуюся работу — повтор рискует дорого продублировать её; поведение как сейчас). `RATE_LIMITED` **исключаем** (ему нужен длинный defer, а не плотный ретрай — это зона fallback'а и будущего capacity-gate, Вариант E). Набор оформляем как отдельное множество `TRANSIENT_RETRYABLE` в [providers/base.py](../../../../src/wastech_orchestrator/providers/base.py) рядом с `FALLBACK_ELIGIBLE`.
2. **Resume или fresh при ретрае.** По умолчанию **resume сессии ноды** (сохраняем частичную работу/рассуждения), с деградацией в **fresh-попытку с диффом**, если resume не удался — ровно та же логика, что уже есть в ветке `SESSION_UNAVAILABLE` ([router.py:248-299](../../../../src/wastech_orchestrator/routing/router.py#L248-L299)).
3. **Backoff блокирует слот.** В A — да: короткий ограниченный `sleep` в Router (один активный слот → блокировка приемлема). Суммарное окно жёстко ограничено (по умолчанию ≤ ~60 c). Длительный аутэйдж сверх этого окна — зона Фазы 2 (пауза с резюмом, слот освобождается).
4. **Max blocked duration (Фаза 2).** У припаркованной задачи есть потолок `max_blocked` (предлагаемый дефолт — 1 час); по его истечении задача переходит в терминальный `FAILED` (или `manual_action_required`), чтобы ничего не висело вечно.
5. **Jitter не добавляем.** Оркестратор однослотовый, конкурирующих клиентов нет — «thundering herd» не возникает, поэтому детерминированный экспоненциальный backoff проще и достаточен (и удобнее для тестов).
6. **Верификация внутреннего ретрая CLI** (спайк) — см. ниже; от неё зависят дефолты задержек.
7. **Fallback симметричный (Claude↔Codex), а не только «на глобальный primary».** Сегодня цель fallback — единственный глобальный primary, и она существует, только если провайдер ноды ≠ primary ([router.py:169-173](../../../../src/wastech_orchestrator/routing/router.py#L169-L173)); для дефолтного флоу (нода без `provider:`) это значит `fallback = None`, т.е. отказ primary никуда не переключается. **Расширяем правило PRE.1:** когда резолвнутый primary _и есть_ глобальный primary, цель fallback — **второй разрешённый+сконфигурированный провайдер** из `agents.allowed` (если он ровно один). Так failover становится симметричным в обе стороны. Если разрешён только один провайдер — fallback остаётся `None` (переключаться некуда → сразу зона B-lite). Бюджет повторов (A) применяется к каждому провайдеру в последовательности.

### Бюджет ретрая отделён от `max_stage_attempts`

Транзиентный ретрай — это «переждать блип на том же провайдере», а `max_stage_attempts` считает **хопы провайдер/fallback**. Поэтому вводим **отдельный** бюджет `agents.retry.max_attempts` (число повторов одного провайдера), не вычитающийся из `max_stage_attempts`. Каждая попытка по-прежнему пишет строку аудита `provider_attempts` (требование «bounded and audited»). _(Альтернатива — переиспользовать `max_stage_attempts`, как делает ветка `SESSION_UNAVAILABLE`; отвергнута, т.к. дефолт `3` слишком мал, чтобы вместить и fallback, и повторы.)_

### Фаза 0 — спайк-верификация поведения CLI

Проверить фактическое поведение внутреннего ретрая у запиненных версий Codex и Claude Code на 5xx/429 (количество повторов, backoff, env/конфиг-ручки). Цель — убедиться, что к моменту, когда оркестратор видит `PROVIDER_UNAVAILABLE`, CLI уже исчерпал свой бюджет (тогда ценность A — именно **окно ожидания** и **другой движок**, а не плотный цикл), и осмысленно задать дефолты `base_delay_s` / `max_delay_s`. Результат записать в этот файл (короткой заметкой) и, при необходимости, в [follow_ups.md](../../follow_ups.md).

### Фаза 1 — Вариант A: bounded same-provider retry + backoff в Router

Самодостаточна, целиком внутри Router + конфига. Изменения по файлам:

- **[providers/base.py](../../../../src/wastech_orchestrator/providers/base.py)** — добавить `TRANSIENT_RETRYABLE: frozenset[ErrorClass] = {PROVIDER_UNAVAILABLE, NETWORK_UNAVAILABLE}` рядом с `FALLBACK_ELIGIBLE`.
- **[config/schema.py](../../../../src/wastech_orchestrator/config/schema.py)** — новый frozen-dataclass `RetryConfig(max_attempts: int, base_delay_s: float, max_delay_s: float)`; поле `retry: RetryConfig` в `AgentsConfig`; поднять `CONFIG_SCHEMA_VERSION` 15 → 16.
- **[config/loader.py](../../../../src/wastech_orchestrator/config/loader.py)** — распарсить `agents.retry` по образцу `_int(m, "max_stage_attempts", 3, ...)` ([loader.py:414](../../../../src/wastech_orchestrator/config/loader.py#L414)); дефолты `max_attempts=2`, `base_delay_s=2.0`, `max_delay_s=30.0`. Блок `retry` опционален (отсутствует → дефолты), это сохраняет обратную совместимость существующих конфигов.
- **[packaged/config.example.yaml](../../../../src/wastech_orchestrator/packaged/config.example.yaml)**, **[install/config_writer.py](../../../../src/wastech_orchestrator/install/config_writer.py)** — добавить дефолтный блок `agents.retry` (синхронно с дефолтами загрузчика).
- **[routing/router.py](../../../../src/wastech_orchestrator/routing/router.py)** — основная логика:
  - **`resolve_route` (симметричный fallback, решение №7):** когда резолвнутый primary совпадает с глобальным primary, выбирать цель fallback как второй провайдер из `agents.allowed` (если он ровно один разрешён и сконфигурирован), а не `None` ([router.py:169-173](../../../../src/wastech_orchestrator/routing/router.py#L169-L173)). При единственном разрешённом провайдере — `None`, как сейчас;
  - в конструктор добавить инъекцию `sleep: Callable[[float], None] = time.sleep` (рядом с уже инъектируемым `monotonic` — для детерминизма тестов);
  - в `run_stage`, в `except ProviderError`: если `exc.error_class in TRANSIENT_RETRYABLE` и остался бюджет `retry.max_attempts` — **до** перехода к fallback повторить **тот же** провайдер с экспоненциальным backoff (`min(base*2**k, max_delay_s)`), резюмя сессию при наличии, с деградацией в fresh+`diff_path` при провале resume (переиспользуем форму ветки `SESSION_UNAVAILABLE`, [router.py:248-299](../../../../src/wastech_orchestrator/routing/router.py#L248-L299));
  - бюджет повторов применяется к **каждому** провайдеру в последовательности `[primary, fallback]`: исчерпали повторы primary → переключение на второй провайдер → повторы для него;
  - каждую попытку фиксировать как `ProviderAttempt`/`provider_attempts`; счётчик повторов — отдельный, не уменьшает `max_stage_attempts`;
  - после исчерпания повторов и fallback — терминальная инфра-ошибка (которую Фаза 2 переводит в мягкую паузу, а не в `FAILED`).
- **Тесты:**
  - юнит Router как таблица решений + backoff: инъекция `monotonic`/`sleep`, проверка числа попыток, последовательности задержек, перехода resume→fresh, того что `RATE_LIMITED`/`TIMEOUT`/quality НЕ ретраятся, и что бюджет повторов не съедает `max_stage_attempts`;
  - интеграция через фейковые CLI (скилл `fake-cli`) — сценарий «провайдер отдаёт 500 N раз, затем успех» (восстановление) и «500 всегда» (исчерпание → fallback/терминал);
  - тесты загрузчика/схемы на новый блок и на bump версии (обновить снапшоты конфиг-версии, если есть);
  - тест симметричного fallback: отказ primary при двух разрешённых провайдерах → переключение на второй (в обе стороны Claude↔Codex); при одном разрешённом — `fallback = None`.

**Функциональный критерий A:** транзиентный 500 на ноде дефолтного флоу (где сегодня нет ни fallback, ни ретрая) переживается прозрачно — задача продолжается после ≤ N повторов с backoff и/или переключения на второй провайдер; quality-фейлы и нетранзиентные классы поведения не меняют.

### Фаза 2 — Вариант B-lite: резюмируемая пауза вместо немедленного `FAILED`

Для **длительного** аутэйджа (минуты–часы, или оба движка лежат), который окно A не пересидит. Опирается на уже существующую машинерию checkpoint + restart-resume (§ How a 500 is handled today, п. 4). Изменения:

- **[core/flow/nodes/base.py](../../../../src/wastech_orchestrator/core/flow/nodes/base.py)** — `NodeInfraError` начинает нести `error_class: ErrorClass | None`, чтобы оркестратор мог отличить транзиентно-инфра-исчерпание от прочих инфра-фейлов.
- **[core/flow/nodes/agent.py](../../../../src/wastech_orchestrator/core/flow/nodes/agent.py)** ([agent.py:235-241](../../../../src/wastech_orchestrator/core/flow/nodes/agent.py#L235-L241)) и **[evaluator.py](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)** ([evaluator.py:91-95](../../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L91-L95)) — прокинуть `outcome.terminal_error.error_class` в `NodeInfraError`.
- **[core/orchestrator.py](../../../../src/wastech_orchestrator/core/orchestrator.py)** ([orchestrator.py:1201-1203](../../../../src/wastech_orchestrator/core/orchestrator.py#L1201-L1203)) — при `NodeInfraError` с транзиентным классом и исчерпанным окном A: не `_fail()`, а оставить задачу **резюмируемой**. Чекпойнт уже сохранён ([orchestrator.py:1249-1250](../../../../src/wastech_orchestrator/core/orchestrator.py#L1249-L1250)); задача остаётся `active`, текущий прогон завершается без терминального перехода, с записью времени паузы.
- **watch-loop + [core/recovery.py](../../../../src/wastech_orchestrator/core/recovery.py)** — после cool-off переадмитить задачу; на рестарте процесса `reconcile` уже резюмит единственную активную задачу с `current_node` ([recovery.py:57-107](../../../../src/wastech_orchestrator/core/recovery.py#L57-L107)). Потолок `max_blocked`: по истечении — `_fail()` (терминал). **Точку интеграции в watch-loop уточнить на этапе дизайна Фазы 2** (для одноразового `run` без тиков задача просто остаётся резюмируемой до следующего `run`/рестарта).
- **State machine** — по возможности **без нового статуса**: переиспользуем `active` + restart-resume. Отдельный статус `blocked` дал бы более явный operator-surface, но это более крупное изменение [state_machine.py](../../../../src/wastech_orchestrator/core/state_machine.py) — выносим в отдельное решение, если по итогам Фазы 2 понадобится.
- **Тесты** — сценарий «исчерпание окна A и fallback → мягкая пауза → резюм с чекпойнта без повторного коммита» (идемпотентность коммитов уже гарантируют фингерпринты Git Manager).

### Риски и границы

- **Инварианты соблюдены:** ретраим только _поднятый_ инфра-`ProviderError`, никогда quality (`status=failed`); коммит/пуш/PR — только оркестратор и только _после_ ноды, поэтому повтор/резюм не делает двойной коммит; всё bounded и пишется в аудит; backoff в Router — ядро не учит синтаксис CLI.
- **PRE.1 расширяется осознанно:** симметричный fallback (решение №7) меняет правило «единственный primary — единственная цель fallback». Это явное архитектурное решение, поэтому его нужно отразить в [.agents/rules/architecture.md](../../../../.agents/rules/architecture.md) и функциональной карте.
- **Сознательно вне скоупа (YAGNI, greenfield-MVP):** первоклассный статус `blocked`, перенос вендорской сессии между движками (источник истины — артефакты, не сессии), пер-модельный/пер-нодовый тюнинг ретрая (стартуем с одного блока `agents.retry`), fallback при >2 разрешённых провайдерах (поддерживаем ровно пару Claude/Codex).

### Проверки и документация (в самом конце)

Всё это запускается **один раз, после того как реализованы все фазы** (A + симметричный fallback + B-lite), а не пофазно.

- **Проверки:** `ruff check .`, `mypy src`, `pytest` (одной командой через `/run-checks`); затем `npx prettier@3 --write "**/*.md"` по затронутым докам.
- **Техническая документация (обязательно обновить всю затронутую):** `/sync-docs` для [Functional Map](../../../functional/index.md) (блоки роутинга/ретрая/fallback и восстановления) и, если затронута топология, модель C4 в [docs/likec4](../../../likec4); [docs/configuration.md](../../../configuration.md) — новый блок `agents.retry` и bump `config_version` до 16; [.agents/rules/architecture.md](../../../../.agents/rules/architecture.md) — расширение правила fallback (решение №7).
- **Краткая заметка в пользовательских документах:** добавить короткий абзац про отказоустойчивость провайдеров (ретрай → симметричный fallback Claude↔Codex → мягкая пауза при недоступности обоих) в [docs/worc_architecture.md](../worc_architecture.md) (раздел про архитектуру/восстановление) и операторскую заметку (поведение при аутэйдже + новые ручки `agents.retry`) в [docs/operations.md](../../../operations.md). Если найдётся более подходящее место — добавить туда.
- **Отложенные хвосты** — в [follow_ups.md](../../follow_ups.md).
