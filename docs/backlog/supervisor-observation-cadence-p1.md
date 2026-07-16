# Supervisor P1: управляемый cadence (observe/finalize split, observation_mode + events, бюджеты)

**Статус:** proposal **Приоритет:** P1 (даёт основную экономию — снимает промежуточные наблюдения; безопасно только поверх P0-пакета) **Источник:** [2026-07-16 варианты оптимизации supervisor](../analysis/2026-07-16-supervisor-token-optimization-options.md) (§8 P1, Варианты B/C/D/H/I).

**Дорожная карта:** [P0 — packet + fresh finalize](supervisor-finalize-packet-and-cadence.md) → **P1 (этот документ)** → [P2 — разделение обязанностей и telemetry](supervisor-responsibility-split-p2.md).

## Зависимости

- **Требует P0.** `observation_mode: none`/`events` безопасны только когда finalize уже независим от тёплой сессии и засеян `SupervisorPacket` (иначе на прогоне без наблюдений finalize теряет грунтинг). Не начинать P1, пока P0 не смёржен.
- Точный token-budget (`max_digest_tokens` в токенах) опирается на уже реализованный [normalized-usage-accounting.md](normalized-usage-accounting.md); в P1 достаточно char/count-границ, токенную точность подключаем как уточнение.

## Проблема

После P0 finalize дешёвый и воспроизводимый, но per-step наблюдения по-прежнему запускаются на **каждой** executed non-`tool`/`checks` ноде и по-прежнему на тёплой растущей сессии. На исследованном прогоне шесть наблюдений стоили 375 726 input-токенов и $0.44, не влияя на исход задачи (supervisor advisory-only). Один общий `SupervisorConfig` (`role_file, model, reasoning, provider` — `config/schema.py:470`) применяется и к дешёвому наблюдению, и к сложному finalize: нельзя задать low для заметок и medium для синтеза, нельзя выключить наблюдения, сохранив summary. Flow-local блок (`SupervisorBlock`: `role_file, finalize_role_file, handoff_role_file, emit_follow_ups` — `core/flow/schema.py:195`) умеет менять только формулировки, не cadence.

## Требуемый результат

Оператор управляет частотой наблюдений (`observation_mode`), а расход растёт вместе с реальными отклонениями, а не с числом обычных шагов. Настройки observe и finalize разделены (своя модель/reasoning/сессия у каждого). Есть жёсткий потолок на число наблюдений и размер digest. Content-flow работает finalize-only, implementation-flow — в event-режиме.

## Решения

- **`observation_mode: all | selected | events | none`** в конфиге supervisor (Варианты B/C). Default для packaged content-flow (`blog_article*`, `content_*`) — `none`; для `implementation` — `events`.
- **Event-триггеры** для `events`: `rework`, `failure`, `hitl`, `dangerous_diff`, `fallback`, `subtask_boundary`. Обычные `done`/`pass`/`accept` пишутся детерминированной step-записью **без** LLM.
- **Раздельные `observe` и `finalize` настройки** (Вариант H): своя `model`/`reasoning`/`session` у каждого. Наблюдение — дешёвая модель + low/medium; finalize — сильнее, medium.
- **Бюджеты** (Вариант I): `max_calls` и `max_digest_tokens`; при исчерпании — `on_budget_exhausted: deterministic_only` (Core продолжает писать events, LLM-observer выключается). Deep fix-loop получает предсказуемый потолок.
- **Flow-local narrowing**: flow-блок может сузить глобальную политику (например, задать `observation_mode: none`), но не расширить её сверх ceiling.
- **Опционально в P1 — fresh observe session** (Вариант D, `session: fresh_digest` для observe): каждое наблюдение стартует свежим с bounded rolling digest вместо resume растущей истории. Default остаётся `warm`; переключение валидируется A/B.

Предлагаемая схема (новая, требует bump версии config и loader/validation):

```yaml
supervisor:
  provider: claude

  observe:
    mode: events # all | selected | events | none
    triggers:
      [rework, failure, hitl, dangerous_diff, fallback, subtask_boundary]
    include_nodes: [] # для mode: selected
    model: claude-sonnet-5
    reasoning: low
    session: warm # warm | fresh_digest
    max_calls: 3
    max_digest_tokens: 4000
    on_budget_exhausted: deterministic_only

  finalize:
    enabled: true
    model: claude-sonnet-5
    reasoning: medium
    session: fresh_packet # уже поведение P0

  handoff:
    enabled: true
    reasoning: medium
```

Flow-local сужение:

```yaml
flow:
  supervisor:
    observation_mode: none # content-flow
    role_file: blog_article_revise/supervisor.md
    finalize_role_file: blog_article_revise/summary.md
```

## В объёме P1

1. Расширить `SupervisorConfig` вложенными `observe`/`finalize`/`handoff` блоками; сохранить обратную совместимость плоских `model`/`reasoning`/`provider` (или явно смигрировать — greenfield, миграции не нужны). Bump версии схемы + `config/loader.py` + `config/validation.py` (reasoning ∈ allowlist, provider ∈ `agents.allowed`, mode/triggers валидны).
2. Добавить `observation_mode` в flow-local `SupervisorBlock` (сужение глобальной политики).
3. В post-node hook **всегда** писать детерминированную step-запись (node/kind/outcome/факты, `note=""`), а LLM-observer вызывать **условно** по mode/триггерам. Это гарантирует полноту ledger/пакета даже когда наблюдения выключены.
4. Реализовать event-детекцию (rework/failure/hitl/dangerous_diff/fallback/subtask_boundary) из данных, уже доступных в hook.
5. Бюджеты `max_calls` + `max_digest_tokens` (char-граница в P1; токенная — через normalized usage) и `on_budget_exhausted: deterministic_only`.
6. Packaged-дефолты: content-flow → `observation_mode: none`; `implementation` → `events`.
7. Тесты (см. ниже) и синхронизация docs + `packaged/config.example.yaml` + `packaged/guide/`.

Ожидаемый эффект: для `blog_article_revise` finalize-only убирает исторические 375 726 observation input-токенов; общий supervisor input реалистично падает с ~480 тыс. до ~30–60 тыс. (целевой диапазон, подтверждается A/B).

## Критерии приёмки

- [ ] `rework`/`failure`/`HITL` создаёт observation в `events`-режиме; обычный `done`/`pass`/`accept` — нет.
- [ ] `none`-режим не создаёт ни одного observation, но finalize и summary сохраняются (через P0-пакет).
- [ ] `selected` наблюдает ровно `include_nodes`.
- [ ] Детерминированная step-запись пишется всегда — даже в `none`, — и попадает в `SupervisorPacket`.
- [ ] `max_calls`/`max_digest_tokens` соблюдаются; исчерпание переключает observer в deterministic-only, задача не падает.
- [ ] `observe.reasoning` и `finalize.reasoning` применяются раздельно; digest имеет детерминированную границу.
- [ ] Flow-local `observation_mode` сужает, но не расширяет глобальную политику; валидатор отвергает выход за ceiling.
- [ ] Handoff и skill-proposal работают независимо от `observation_mode`.
- [ ] Обратная совместимость config: старый плоский `supervisor.{model,reasoning,provider}` либо продолжает работать, либо явно отвергается загрузчиком с понятной ошибкой (без тихого дрейфа).

## Тесты под замену/добавление

- `tests/core/test_supervisor.py` — cadence по режимам (`all`/`selected`/`events`/`none`), раздельные observe/finalize настройки, бюджеты и deterministic-only при исчерпании.
- `tests/core/test_flow_engine.py` — post-node: событие → observation в `events`; обычный шаг → только детерминированная запись.
- `tests/config/` — round-trip нового блока, валидация mode/triggers/reasoning/provider, поведение на legacy-плоском блоке.
- Packaged flows — snapshot дефолтов (content=none, implementation=events).

## Вне объёма P1 (→ P2)

- Вынос детерминированного `StepRecorder` в отдельный компонент, handoff/skill-proposer как отдельные budgeted capabilities, per-function usage/cost telemetry и отчёт в task summary — см. [P2](supervisor-responsibility-split-p2.md).

## Вероятные области реализации

- `src/wastech_orchestrator/config/schema.py` — вложенные observe/finalize/handoff, bump версии.
- `src/wastech_orchestrator/config/loader.py`, `config/validation.py` — парсинг и валидация нового блока.
- `src/wastech_orchestrator/core/flow/schema.py` — `observation_mode` во flow-local narrowing.
- `src/wastech_orchestrator/core/orchestrator.py` — детерминированная step-запись + условный observe + event-детекция + бюджеты в post-node hook.
- `src/wastech_orchestrator/core/supervisor.py` — раздельные observe/finalize маршруты и (опц.) fresh observe digest.
- `src/wastech_orchestrator/packaged/config.example.yaml`, `packaged/flows/*`, `packaged/guide/` — operator-facing дефолты и доки.
- `docs/configuration.md`, `docs/worc_architecture.md` — канон поведения cadence.
