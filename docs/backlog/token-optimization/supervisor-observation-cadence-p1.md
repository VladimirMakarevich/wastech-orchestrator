# Supervisor P1: управляемый cadence (observe/finalize split, observe.mode + event-триггеры)

**Статус:** **accepted 2026-07-26** (все развилки закрыты в «[Решения приёмки](#решения-приёмки-2026-07-26)»; реализацию не начинать до мёржа P0 — это корректностное требование, а не предпочтение) **Приоритет:** P1 (даёт основную экономию — снимает промежуточные наблюдения; безопасно только поверх P0-пакета) **Источник:** [2026-07-16 варианты оптимизации supervisor](https://github.com/VladimirMakarevich/wastech-orchestrator/blob/main/docs/analysis/2026-07-16-supervisor-token-optimization-options.md) (§8 P1, Варианты B/C/D/H/I).

**Дорожная карта:** [P0 — packet + fresh finalize](supervisor-finalize-packet-and-cadence.md) → **P1 (этот документ)** → [P2 — разделение обязанностей и telemetry](supervisor-responsibility-split-p2.md).

## Зависимости

- **Требует P0.** `observe.mode: none`/`events` безопасны только когда finalize уже независим от тёплой сессии и засеян `SupervisorPacket` (иначе на прогоне без наблюдений finalize теряет грунтинг). Не начинать P1, пока P0 не смёржен.
- ~~**Актуализация 2026-07-23 про `max_digest_tokens` в реальных токенах**~~ — снята решением P1-D6 (2026-07-26): бюджетов в P1 нет, поэтому и выбор единицы измерения для них не нужен. Нормализованный usage ([normalized-usage-accounting.md](normalized-usage-accounting.md)) остаётся релевантен как мерная подложка для A/B, а не как источник для потолка.

## Проблема

После P0 finalize дешёвый и воспроизводимый, но per-step наблюдения по-прежнему запускаются на **каждой** executed non-`tool`/`checks` ноде и по-прежнему на тёплой растущей сессии. На исследованном прогоне шесть наблюдений стоили 375 726 input-токенов и $0.44, не влияя на исход задачи (supervisor advisory-only). Один общий `SupervisorConfig` (`role_file, model, reasoning, provider` — `config/schema.py:501`) применяется и к дешёвому наблюдению, и к сложному finalize: нельзя задать low для заметок и medium для синтеза, нельзя выключить наблюдения, сохранив summary. Flow-local блок (`SupervisorBlock`: `role_file, finalize_role_file, handoff_role_file, emit_follow_ups` — `core/flow/schema.py:204`) умеет менять только формулировки, не cadence.

## Требуемый результат

Оператор управляет частотой наблюдений (`observe.mode`), а расход растёт вместе с реальными отклонениями, а не с числом обычных шагов. Настройки observe и finalize разделены (своя модель и reasoning у каждого). Content-flow работает finalize-only, implementation-flow — в event-режиме. Отдельных бюджетных потолков нет (решение P1-D6) — ограничителем служит сам режим.

## Решения

- **`observe.mode: all | selected | events | none`** в конфиге supervisor (Варианты B/C).
- **Дефолты по типам flow живут в самих packaged-flow YAML, а не в коде движка (решение P1-D4, 2026-07-26).** Движок обязан оставаться flow-agnostic — ветвиться по имени flow или id ноды запрещено ([.agents/rules/architecture.md](../../../.agents/rules/architecture.md)), а user-authored flow — первоклассный. Механизм уже есть: у packaged-флоу есть flow-local блок `supervisor:` (`blog_article_revise.yaml:143`, `implementation.yaml:173`), в него и добавляется режим. Конкретно: `blog_article`, `blog_article_revise`, `content_chapter`, `content_translate` получают `none` в своих YAML; `implementation.yaml` — `events` (там `emit_follow_ups: true`, `none` просадил бы follow_ups/память).
- **Event-триггеры** для `events`: `rework` (в т.ч. `rework_exhausted`), `failure`, `fallback` — закрытый список из решения P1-D7. Обычные `done`/`pass`/`accept` пишутся детерминированной step-записью **без** LLM.
- **Раздельные `observe` и `finalize` настройки** (Вариант H): своя `model`/`reasoning` у каждого. Наблюдение — дешёвая модель + low/medium; finalize — сильнее, medium. Ключа `session` нет ни у одного из блоков (решения P1-D8 и P0-D4).
- ~~**Бюджеты** (Вариант I): `max_calls`, `max_digest_tokens`, `on_budget_exhausted`~~ — **исключены из P1 решением P1-D6 (2026-07-26).** Ограничителем расхода служит сам режим наблюдений, а не отдельный потолок; см. ниже.
- **Flow-local narrowing**: flow-блок может сузить глобальную политику (например, задать `observe.mode: none`), но не расширить её — шкала строгости и глобальный дефолт зафиксированы в решении P1-D5.
- ~~**Опционально в P1 — fresh observe session** (Вариант D, `session: fresh_digest`)~~ — **исключено решением P1-D8 (2026-07-26).** Наблюдения остаются на тёплой сессии; ключа `session` у `observe` нет.

Предлагаемая схема (новая, требует bump версии config и loader/validation):

```yaml
supervisor:
  provider: claude

  observe:
    mode: events # all | selected | events | none
    triggers: [rework, failure, fallback]
    include_nodes: [] # для mode: selected
    model: claude-sonnet-5
    reasoning: low

  finalize:
    enabled: true
    model: claude-sonnet-5
    reasoning: medium

  handoff:
    enabled: true
    reasoning: medium
```

Flow-local сужение (имя ключа — решение P1-D2, то же, что в конфиге):

```yaml
flow:
  supervisor:
    role_file: blog_article_revise/supervisor.md
    finalize_role_file: blog_article_revise/summary.md
    observe:
      mode: none # content-flow
```

## Решения приёмки (2026-07-26)

### P1-D1 — только вложенный вид; плоские ключи отвергаются, значения не переносятся

Вложенными становятся `model` / `reasoning` и политика наблюдений. `role_file` и `provider` остаются на верхнем уровне: `provider` — один на весь слой (как в схеме выше), а `role_file` — observe-линза, у которой уже есть flow-local тёзка (`SupervisorBlock.role_file`), и переименование сломало бы это соответствие без выгоды.

Плоские `supervisor.model` / `supervisor.reasoning` из схемы **удаляются**. Что это значит в трёх местах:

- **loader** — встретив плоский ключ, падает fail-closed с сообщением, которое называет новое место (`supervisor.model → supervisor.observe.model / supervisor.finalize.model`), а не абстрактным «unknown key».
- **`worc upgrade-config`** — плоские ключи заносятся в `_REMOVED_KEYS` (`config/upgrade.py:46`), поэтому команда их стрипает и печатает `- supervisor.model (removed in this schema version)`, а новые ключи доливает из шаблона.
- **значения не переносятся** — одно старое `model` пришлось бы дублировать в `observe` и в `finalize`: в первом случае мы своими руками вернули бы дорогую модель на дешёвые заметки (то, от чего уходим), во втором — угадывали бы намерение оператора. Оператор доопределяет сам; потеря видна в отчёте `upgrade-config`, тихого дрейфа нет. Прецедент value-transform (`_migrate_codex_sandbox`, v31) сознательно **не** используется: он оправдан для однозначного переименования, а здесь целевых мест два.

Обратная совместимость плоского вида не поддерживается: проект greenfield, а два способа задать одно и то же навсегда добавили бы правила приоритета, документацию к ним и комбинаторику в тестах.

### P1-D2 — одно имя в обоих файлах: `supervisor.observe.mode`

Флоу-локальный ключ называется так же, как глобальный — путь `supervisor.observe.mode` идентичен в `config.yaml` и в flow YAML; плоское имя `observation_mode` из исходной формулировки ADR не используется нигде:

```yaml
flow:
  supervisor:
    role_file: blog_article_revise/supervisor.md
    finalize_role_file: blog_article_revise/summary.md
    observe:
      mode: none # content-flow: только finalize
```

Цена — вложенный подблок в flow-парсере (`_parse_supervisor`, `core/flow/snapshot.py:608`) со своим `_reject_unknown`, ровно как у существующего `defaults.evaluator` (`:626-640`). Взамен: одна форма в документации, скилле `worc-flow-role` и примерах, и место для будущего `include_nodes` без второго переименования.

### P1-D3 — номер версии схемы не фиксируем, пересчитываем при реализации

Bump обязателен (плоские ключи удаляются — старый конфиг перестаёт грузиться), но конкретное число в документе не пиним: на 2026-07-26 это `31 → 32` (`config/schema.py:167`), однако любая другая задача, добравшаяся до мёржа раньше, сдвинет базу. Формулировка для реализации: «увеличить `CONFIG_SCHEMA_VERSION` на единицу от текущего значения на момент правки и внести плоские ключи в `_REMOVED_KEYS`».

### P1-D4 — дефолты по флоу живут в packaged YAML (см. §Решения)

Зафиксировано выше, в §Решения: движок не ветвится по имени flow, per-flow режим задаётся во flow-local блоке `supervisor:` самого packaged-флоу.

### P1-D5 — шкала строгости и глобальный дефолт `events`

Шкала (ранг = сколько LLM-вызовов режим способен породить): `none` (0) < `events` (только отклонения) < `selected` (перечисленные ноды) < `all` (каждый шаг). Flow-локальный режим допустим, если его ранг **не выше** глобального. Реализуется тем же приёмом, что уже применён к правам: таблица ранга + сравнение с fail-closed на незнакомом значении (`security/profiles.py:23-34`, применение — `core/flow/validator.py:431-438`).

Лазейка `selected` закрывается отдельным правилом: `selected` перечисляет ноды и в пределе шире `events`, поэтому под глобальным `events` flow может задать только `events` или `none`.

Глобальный дефолт — **`events`**. Так экономия достаётся и user-authored флоу, у которых своего блока `supervisor:` нет (а не только packaged), при этом отклонения — `rework`/`failure`/`fallback` — продолжают наблюдаться, то есть диагностическая ценность остаётся. Заземление summary при любом режиме гарантирует пакет из P0. `all` остаётся доступен как явная настройка для отладки.

### P1-D6 — бюджеты в P1 не вводим

`max_calls`, `max_digest_tokens` и `on_budget_exhausted` исключены из объёма. Причина простая: после `events`/`none` расход уже привязан к числу реальных отклонений, а не к числу шагов, так что потолок ограничивал бы то, что и так почти не тратится, — а вводить его пришлось бы с персистентным счётчиком (иначе restart обнуляет бюджет) и с оценщиком токенов до отправки (нормализованный usage известен только после вызова). Ни того, ни другого никто не просил.

Что остаётся вместо бюджетов: размер digest уже ограничен детерминированно и до отправки — 8 000 символов по решению P0-D3; режим наблюдений ограничивает частоту; `all` остаётся сознательным выбором оператора. Если потолок когда-нибудь понадобится, он вводится отдельной задачей и опирается на `FlowRunState.loop_counters` (durable-чекпоинт, `core/flow/run_state.py:5-13`) — там же, где живут бюджеты fix-циклов.

Тем самым отменяется и актуализация 2026-07-23 про «`max_digest_tokens` сразу в реальных токенах»: бюджета нет, вопрос снят.

### P1-D7 — три триггера вместо шести

`events` срабатывает на `rework` (включая `rework_exhausted`), `failure` и `fallback`. Это ровно то, что доступно на месте: первые два читаются из `outcome.kind`, третий — дешёвым чтением строки `node_runs` (`provider_used` против `route_primary`), для чего у хука уже есть `node_run_id`. Контракт post-node хука не расширяется.

Убраны из списка:

- `hitl` и `dangerous_diff` — их фактов в `outcome` нет, но главное: в обоих случаях человек **уже** был поднят (HITL-пауза шлёт запрос в Telegram и ждёт ответа, `core/hitl.py`), поэтому advisory-заметка ИИ вдогонку ничего не добавляет.
- `subtask_boundary` — это не post-node событие: на границе подзадач уже выполняется отдельный `handoff`-turn (`core/orchestrator.py:2891`), то есть наблюдение там есть по построению.

Формально это делает список триггеров закрытым и коротким; если позже понадобится новый, он добавляется вместе с фактами, которые для него нужны.

### P1-D8 — своя сессия для наблюдений не вводится

Ключ `session` у `observe` не появляется: наблюдения остаются на тёплой сессии supervisor'а (`resume_own_lineage`, персистится в `node_lineage`). Причина, по которой обсуждался `fresh_digest`, снимается самим режимом: эта сессия растёт от собственных turn'ов, а в `events` их единицы (только `rework`/`failure`/`fallback`), в `none` — ноль. Второй кодовый путь ради режима, который сам ADR помечал как «валидируется A/B», не оправдан. Если замер после P1 покажет, что и единичные наблюдения дорогие, `fresh_digest` вводится отдельной задачей — уже с числами.

По той же логике у `finalize` тоже **нет** ключа `session`: после решения P0-D4 у него единственный режим (fresh из пакета), а ключ с единственным допустимым значением — мёртвая настройка. Откат P0, если он понадобится, делается `git revert`, а не конфигом.

## В объёме P1

1. Расширить `SupervisorConfig` вложенными `observe`/`finalize`/`handoff` блоками; плоские `model`/`reasoning` удалить из схемы и отвергать в загрузчике, `role_file`/`provider` оставить на верхнем уровне (решение P1-D1). Bump версии схемы (номер не пиним — решение P1-D3) + `config/loader.py` + `config/validation.py` (reasoning ∈ allowlist, provider ∈ `agents.allowed`, mode/triggers валидны) + `_REMOVED_KEYS` в `config/upgrade.py`.
2. Добавить вложенный `observe.mode` в flow-local `SupervisorBlock` (сужение глобальной политики) — тем же паттерном, что `defaults.evaluator`: свой `_reject_unknown` в `_parse_supervisor` (решение P1-D2).
3. В post-node hook **всегда** писать детерминированную step-запись (node/kind/outcome/факты, `note=""`), а LLM-observer вызывать **условно** по mode/триггерам. Это гарантирует полноту ledger/пакета даже когда наблюдения выключены.
4. Реализовать event-детекцию (`rework`/`failure` из `outcome`, `fallback` из строки `node_runs`) — без расширения контракта post-node хука (решение P1-D7).
5. Packaged-дефолты — правкой YAML соответствующих флоу (их flow-local блок `supervisor:`), без какого-либо сопоставления имени flow в коде: content-флоу → `none`, `implementation` → `events` (решение P1-D4).
6. Тесты (см. ниже) и синхронизация доков, которые физически есть на `dev` (решение X2, 2026-07-26): `packaged/config.example.yaml` (новый вложенный блок supervisor), `packaged/guide/config/reference.md:173-182` (таблица плоских ключей `supervisor.{role_file,provider,model,reasoning}` перестаёт соответствовать схеме), `packaged/guide/flows/reference.md:22` (состав `SupervisorBlock` + режим наблюдений), `packaged/guide/flows/roles.md` (cadence) и packaged-flows. Derived `docs/` на `dev` нет — вместо правки строка doc-impact в описании PR.

Ожидаемый эффект (числа исторические, 2026-07-16): finalize-only убирает 375 726 observation input-токенов, общий supervisor input падал бы с ~480 тыс. до ~30–60 тыс. Для приёмки метрики те же, что зафиксированы в [P0 §A/B и метрики](supervisor-finalize-packet-and-cadence.md#ab-и-метрики-решение-x1-пересмотрено-2026-08-03) (решение X1, пересмотрено 2026-08-03): нормированная доля плюс структурный инвариант, читаемые с одного прогона после P1. Отдельного baseline-прогона нет ни у P0, ни у P1.

## Критерии приёмки

- [ ] `rework`/`failure`/`fallback` создаёт observation в `events`-режиме; обычный `done`/`pass`/`accept` — нет; `hitl`/`dangerous_diff`/`subtask_boundary` триггерами не являются (решение P1-D7).
- [ ] `none`-режим не создаёт ни одного observation, но finalize и summary сохраняются (через P0-пакет).
- [ ] `selected` наблюдает ровно `include_nodes`.
- [ ] Детерминированная step-запись пишется всегда — даже в `none`, — и попадает в `SupervisorPacket`.
- [ ] `observe.reasoning` и `finalize.reasoning` применяются раздельно; граница digest — та же детерминированная (8 000 симв. из P0-D3), никаких бюджетных ключей в схеме нет (решение P1-D6).
- [ ] Flow-local `observe.mode` сужает, но не расширяет глобальную политику: валидатор сравнивает ранги `none < events < selected < all`, отвергает более широкий режим и падает fail-closed на незнакомом значении; под глобальным `events` `selected` не принимается (решение P1-D5).
- [ ] Глобальный дефолт — `events`; flow без блока `supervisor:` наследует его, а не `all`.
- [ ] Handoff и skill-proposal работают независимо от `observe.mode`.
- [ ] Движок нигде не ветвится по имени flow: режим приходит из flow-local блока или из глобального конфига, packaged-дефолты заданы в YAML самих флоу (решение P1-D4).
- [ ] Плоские `supervisor.model` / `supervisor.reasoning` отвергаются загрузчиком fail-closed, и сообщение называет новое место ключа (решение P1-D1); `worc upgrade-config` стрипает их с отчётом и доливает новый блок; значения не переносятся молча.
- [ ] Метрики (метод — [P0 §A/B и метрики](supervisor-finalize-packet-and-cadence.md#ab-и-метрики-решение-x1-пересмотрено-2026-08-03)): на content-flow после P1 **ровно один** supervisor-вызов (finalize) — структурный инвариант, baseline не нужен; доля supervisor в Claude-input прогона **≤ 20%** (исторические ~70%; на тех же числах один finalize даёт 20–40 тыс. при не-supervisor части ~206 тыс., то есть 9–16% — 20% оставляет запас на дисперсию); blocking-issue не пропущены.

## Тесты под замену/добавление

- `tests/core/test_supervisor.py` — cadence по режимам (`all`/`selected`/`events`/`none`) и раздельные observe/finalize настройки.
- `tests/core/test_flow_engine.py` — post-node: событие → observation в `events`; обычный шаг → только детерминированная запись.
- `tests/config/` — round-trip нового блока, валидация mode/triggers/reasoning/provider, поведение на legacy-плоском блоке.
- Packaged flows — snapshot дефолтов (content=none, implementation=events).

## Вне объёма P1 (→ P2)

- Вынос детерминированной step-записи в `core/flow/recorder.py`, per-function usage/cost и supervisor-отчёт в summary — см. [P2](supervisor-responsibility-split-p2.md) (его объём урезан решением P2-D1: раздельных бюджетов handoff/skill там нет).

## Вероятные области реализации

- `src/wastech_orchestrator/config/schema.py` — вложенные observe/finalize/handoff, bump версии.
- `src/wastech_orchestrator/config/loader.py`, `config/validation.py` — парсинг и валидация нового блока.
- `src/wastech_orchestrator/core/flow/schema.py` — `observe.mode` во flow-local narrowing.
- `src/wastech_orchestrator/core/orchestrator.py` — детерминированная step-запись + условный observe + event-детекция в post-node hook (бюджетов нет — решение P1-D6).
- `src/wastech_orchestrator/core/supervisor.py` — раздельные observe/finalize маршруты и (опц.) fresh observe digest.
- `src/wastech_orchestrator/packaged/config.example.yaml`, `packaged/flows/*`, `packaged/guide/` — operator-facing дефолты и доки.
- Derived `docs/configuration.md` / `docs/worc_architecture.md` — на `dev` отсутствуют: канон cadence реконструируется отдельной задачей на `main`, здесь только doc-impact note в PR (X2).
