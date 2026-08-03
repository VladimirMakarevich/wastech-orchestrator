# Supervisor P3: полное отключение слоя (`supervisor.enabled`) + детерминированный finalize

**Статус:** **accepted** (2026-08-03; предложено в тот же день, все четыре развилки закрыты в «[Решения приёмки](#решения-приёмки-2026-08-03)» — Q1 решён **не** по рекомендации документа, Q4 найден при сверке с кодом и отменяет часть D6) **Приоритет:** P3 (не на критическом пути экономии — P0/P1/P2 уже её дали; это крайний режим «слой не нужен вообще» плюс закрытие известной дырки p0-2) **Источник:** запрос оператора 2026-08-03 + известная дырка из [deep-research-postmortem/p0-2](../deep-research-postmortem/p0-2-evaluator-findings-surfacing.md) («деградировавший finalize теряет находки эвалюаторов из тела PR»).

**Дорожная карта:** [P0 — packet + fresh finalize](supervisor-finalize-packet-and-cadence.md) → [P1 — cadence](supervisor-observation-cadence-p1.md) → [P2 — разделение обязанностей и telemetry](supervisor-responsibility-split-p2.md) → **P3 (этот документ)**.

## Зависимости

- **Требует P0.** Детерминированный отчёт этой задачи рендерится из тех же `PacketFacts`, которые P0 уже собирает ([`core/supervisor_packet.py`](../../../src/wastech_orchestrator/core/supervisor_packet.py)). Без P0 фактуры для отчёта просто нет.
- **Требует P1.** `observe.mode` и правило «flow может только сузить каденцию» — та самая ловушка, которую `enabled: false` обязан обойти (см. риск R1). Без P1 обходить нечего, но и ловушки нет.
- **Требует P2.** Шаги/чеки в отчёте берутся из `collect_step_facts` во flow-recorder — это P2 вынес их из LLM-класса. Отчёт читает ту же одну деривацию, что и пакет.
- **Не требует изменений схемы БД.** Признак «находка гейтила» дописывается в существующий JSON-блоб `evaluations.findings_json` (решение P3-Q4), новых колонок нет, `rework_exhausted`-колонка не нужна. То есть **никаких «удалите свой `state.db`»** — в отличие от P2.

## Проблема

Три отдельные, но связанные проблемы.

**(1) Слой нельзя выключить.** `supervisor` — константный слой над любым flow: он строится на каждую задачу и работает всегда. `observe.mode: none` (P1) снимает только per-step наблюдения; `finalize`, `handoff` и `skills.dynamic`-предложение остаются. Оператору, которому слой не нужен вообще (например, весь контроль качества уже держит blocking-гейт, а тело PR его не интересует), выключить его нечем. Попытка выразить «выключено» глобальным `observe.mode: none` **падает валидацией** на `implementation.yaml`, который объявляет `events` — и падает уже **после** claim'а задачи, то есть задача уходит в терминальный `failed` и её надо перезакидывать руками, а `watch` сожжёт так всю очередь (это уже зафиксировано в [follow_ups.md](follow_ups.md#observemode-none-globally-is-refused-for-a-flow-that-declares-events-p1) и в шипнутом доке — [`guide/config/reference.md:213-215`](../../../src/wastech_orchestrator/packaged/guide/config/reference.md)).

**(2) Память без supervisor'а обесценивается, но продолжает стоить.** Сверка кода 2026-08-03 показала, что связь **сильнее**, чем было написано в первой редакции этого документа: без слоя на успешном прогоне не пишется не только «выученное», а **весь SUCCESS-эпизод** — вызов `_write_memory(..., WriteSource.SUCCESS)` целиком лежит внутри ветки `if self._supervisor is not None` ([`orchestrator.py:3027`](../../../src/wastech_orchestrator/core/orchestrator.py)). Добавьте к этому два факта: эпизодический тир **write-only** (никогда не подмешивается в пакет — [`config/schema.py:657`](../../../src/wastech_orchestrator/config/schema.py)), а операторского пути записи не существует (`worc memory` — только `show`/`validate`/`compact`/`restore`/`clear`). Итог: «supervisor выключен + память включена» = память **физически не может получить новый читаемый контент**, но продолжает подмешивать пакеты в каждый промпт (токены) и держать на себе фоновую чистку. Оператор платит и не получает.

**(3) Детерминированный fallback-summary — заглушка, а не отчёт.** `write_minimal_summary` ([`ledger.py:223`](../../../src/wastech_orchestrator/ledger.py)) выдаёт четыре поля (`What/How/Integration/Why`) плюс `git diff --stat`, где `## How` буквально гласит _«No provider-authored summary was available»_. Как редкий аварийный артефакт это приемлемо; как **штатное тело PR** при постоянно выключенном слое — нет. И у него нет параметра `follow_ups`, поэтому уже сегодня деградировавший finalize сохраняет находки эвалюаторов в `summary.json`, но **теряет их в теле PR** — это известная, задокументированная дырка p0-2. Вся детерминированная механика для их сбора при этом уже написана — но живёт **внутри** `Supervisor.finalize` ([`_evaluator_finding_follow_ups`](../../../src/wastech_orchestrator/core/supervisor.py), [`_render_gate_digest`](../../../src/wastech_orchestrator/core/supervisor.py)) и умирает вместе со слоем, хотя ни одной строкой не зависит от LLM.

Отсюда порядок: **(3) — предпосылка для (1)**, а не отдельное улучшение. Выключатель без настоящего детерминированного отчёта ухудшит каждый PR, который его включит.

## Требуемый результат

- Один ключ `supervisor.enabled: false` полностью снимает слой: ни одного provider-вызова с `supervisor_function`, ни одной строки `supervisor_step`/`supervisor_final`, ни пакета, ни `supervisor_usage`.
- Память при выключенном слое не остаётся в полу-режиме (решение P3-Q1).
- `summary.md` (тело PR) при выключенном слое — **настоящий детерминированный отчёт**: изменения, шаги, чеки, вердикты гейтов, follow-ups, пропущенные ноды. Не заглушка и не «⚠️ fallback».
- Находки эвалюаторов, которые были **пропущены** (не гейтили), доходят до тела PR **в любом режиме** — со слоем, без слоя и при деградировавшем finalize. Обязательные (гейтящие) находки в follow-ups не дублируются, потому что они уже прошли через rework.
- **Поведение по умолчанию не меняется ни в одной точке** — кроме двух осознанных исключений, перечисленных ниже (состав follow-ups и новый ключ `gating` в аудите находок).

## Главный инвариант задачи

> **`enabled: true` — дефолт, и при нём весь слой работает ровно как сегодня.** Ни одна из четырёх фаз (`observe`, `finalize`, `handoff`, `propose_skill_map`) не переписывается по логике, не меняет промпты, схемы структурированного вывода, порядок вызовов, содержимое пакета и состав аудита. Отключение — это **новая ветка «не строить слой»**, а не рефакторинг существующей.

Практические следствия, которые нужно держать в голове при реализации:

- Шаг 1 (вынос follow-ups из `supervisor.py` в отдельный модуль) — **чистый перенос**: критерий успеха в том, что существующие тесты проходят без изменения ассертов. Меняются только импорты — и они изменятся обязательно: `tests/core/test_supervisor.py:31-40` импортирует `_FINDING_TITLE_MAX`, `_evaluator_finding_follow_ups`, `_finding_to_follow_up`, `_merge_follow_ups`, `FollowUp`, `parse_follow_ups` из `core.supervisor`, а после переноса кросс-модульные имена перестают быть приватными (см. P3-D10).
- Шаг 2 — **единственные два изменения поведения при `enabled: true`**: (а) состав follow-ups (гейтившие находки больше в них не попадают) и (б) новый ключ `gating` в каждой находке `evaluations.findings_json` (аудит богаче, читатели старого формата не ломаются). Отдельный коммит с отдельными тестами, чтобы это можно было откатить, не откатывая выключатель.
- Шаг 3 (детерминированный отчёт) при `enabled: true` на успешном пути **не задействуется** — там по-прежнему пишет finalize. Он подключается только когда `summary.md` не появился (деградация), слой выключен, или терминал не даёт прозы вообще (`failed` / `manual_action_required`).
- Никакой миграции: проект greenfield, backward-compat машинерии не заводим.

## Инвентарь: что именно завязано на supervisor

Всё, что перестаёт происходить или требует внимания при выключении. Проверено по коду 2026-08-03 (повторно — при приёмке).

### Фазы слоя (исчезают)

| # | Что | Точка вызова | Уже `None`-safe? | Что остаётся без слоя |
| --- | --- | --- | --- | --- |
| 1 | `observe` — per-step заметки, строки `supervisor_step` в `evaluations`, они же `material_observations` пакета | [`orchestrator.py:3271`](../../../src/wastech_orchestrator/core/orchestrator.py) | ✅ `self._supervisor is not None and …` | Ничего. Факты шагов и так в `node_runs`. |
| 2 | `finalize` — `summary.md` (тело PR) + `summary.json`, плюс на том же одном вызове: `memory_delta`, `follow_ups` от модели, `supervisor_usage` | [`orchestrator.py:3017`](../../../src/wastech_orchestrator/core/orchestrator.py) | ✅ `if … is not None` | Детерминированный отчёт (шаг 3). |
| 3 | `handoff` — интерпретирующий бриф между сабтасками | [`orchestrator.py:2931`](../../../src/wastech_orchestrator/core/orchestrator.py) | ✅ `if … is not None` | **Детерминированный «фактический пол»** (файлы предшественника, коммит, критерии приёмки, указатель на спеку) — уже реализован в [`_assemble_predecessor_context`](../../../src/wastech_orchestrator/core/orchestrator.py). Работы ноль. |
| 4 | `propose_skill_map` — карта «нода → скиллы» раз на задачу (`skills.dynamic`) | [`orchestrator.py:3512`](../../../src/wastech_orchestrator/core/orchestrator.py) | ✅ `if … is None: return {}` | Только пины оператора во flow YAML. |

**Это и есть причина, по которой выключатель дёшев:** все четыре точки уже написаны так, что терпят `None`. Достаточно не построить объект.

### Обвязка (требует внимания)

| # | Что | Где | Что делать при `enabled: false` |
| --- | --- | --- | --- |
| 5 | Заморозка трёх flow-local supervisor-промптов в control-bundle | [`control_bundle.py:123`](../../../src/wastech_orchestrator/core/flow/control_bundle.py) | Ничего (безвредно, но можно пропустить — решение D8). |
| 6 | **Правило «flow может только сузить каденцию»** | [`validator.py:563`](../../../src/wastech_orchestrator/core/flow/validator.py) | **Замкнуть накоротко** — иначе `implementation.yaml` падает. См. R1. |
| 7 | Containment-проверка путей supervisor-промптов + prompt-var lint | [`validator.py:464`](../../../src/wastech_orchestrator/core/flow/validator.py), [`validator.py:684`](../../../src/wastech_orchestrator/core/flow/validator.py) | Оставить как есть: это проверка **авторского контента flow**, она не должна зависеть от конфига оператора. |
| 8 | Валидация конфига слоя (provider ∈ `agents.allowed`, reasoning ∈ allowlist, вендорный mismatch, containment `role_file`) | [`validation.py:228`](../../../src/wastech_orchestrator/config/validation.py) | Заменить на одно предупреждение «ключи слоя инертны» — решение D3. |
| 9 | Аудит `provider_attempts.supervisor_function` + роллап `summarize_spend` | [`supervisor_usage.py:55`](../../../src/wastech_orchestrator/core/supervisor_usage.py) | Ничего: `summarize_spend` возвращает `None`, когда помеченных вызовов нет, и блок просто отсутствует. Работы ноль. |
| 10 | Флаг `degraded` и его callout «⚠️ Fallback summary» | [`orchestrator.py:3039`](../../../src/wastech_orchestrator/core/orchestrator.py), [`ledger.py:277`](../../../src/wastech_orchestrator/ledger.py) | Уже корректно: `degraded` выставляется **внутри** ветки `if supervisor is not None`, поэтому при выключенном слое остаётся `False` и предупреждения нет. Проверить тестом, не менять. |

### Память (связь сильнее, чем казалось — исправлено при приёмке)

| Путь памяти | Зависит от слоя? | Где |
| --- | --- | --- |
| **Весь** SUCCESS-путь: эпизод задачи + `candidate_delta` («уроки»/сущности) | ✅ **Да, целиком** — вызов лежит внутри supervisor-ветки | [`orchestrator.py:3027`](../../../src/wastech_orchestrator/core/orchestrator.py) |
| Эпизод при провале (`WriteSource.FAILURE`) | ❌ Нет, детерминированно | [`orchestrator.py:3116`](../../../src/wastech_orchestrator/core/orchestrator.py) |
| Чтение — пакеты в промпты нод (`{memory_path}`) | ❌ Нет | [`memory/packet.py`](../../../src/wastech_orchestrator/memory/packet.py) |
| Фоновая чистка в idle-паузе `watch` | ❌ Нет | [`cli.py:1648`](../../../src/wastech_orchestrator/cli.py) |
| `worc memory show/validate/compact/restore/clear` | ❌ Нет (и **записи среди них нет**) | [`cli.py:2537`](../../../src/wastech_orchestrator/cli.py) |

### Детерминированный код, живущий под слоем (переезжает)

Ни одна из этих функций не делает provider-вызовов, но все они умирают вместе со слоем:

| Функция | Что делает |
| --- | --- |
| `FollowUp`, `parse_follow_ups` | тип + defensive-парсер (evidence-gated) |
| `_last_verdict_per_node`, `_row_findings` | последний вердикт по `(node_id, subtask_order)` и его находки из `evaluations` |
| `_finding_to_follow_up`, `_evaluator_finding_follow_ups` | находка эвалюатора → follow-up |
| `_merge_follow_ups`, `_follow_up_key` | дедуп по нормализованному тексту + путям |
| `_render_follow_ups_section` | секция `## Technical debt / follow-ups` |
| `_render_gate_digest` | вердикты гейтов + их находки (нужно и отчёту, и промпту finalize) |
| `_FINDING_TITLE_MAX` | общая граница длины заголовка (120) |

Сюда же — но отдельным решением (P3-D11) — сборка `PacketFacts`: она тоже полностью детерминированная, но живёт методом класса `Supervisor` ([`_build_packet`](../../../src/wastech_orchestrator/core/supervisor.py)), поэтому без слоя недоступна.

## Решения

Помечено, что рекомендуется и что остаётся развилкой для оператора.

### D1 — отдельный ключ `supervisor.enabled`, а не «выключение через `observe.mode`»

Новый `SupervisorConfig.enabled: bool = True`. Именно отдельный ключ, потому что «выключено» — это не самый узкий режим наблюдений, а отсутствие всех четырёх фаз: `finalize`, `handoff` и `skills.dynamic` к `observe.mode` не относятся вообще (`should_observe` это прямо документирует). И потому что попытка выразить это через `mode: none` натыкается на правило «flow может только сузить» — см. R1.

Схема:

```yaml
supervisor:
  enabled: false # false = слой не строится вообще: ни наблюдений, ни finalize, ни handoff, ни skill-предложения
  # остальные ключи при enabled: false инертны (валидатор скажет об этом одним warning'ом)
```

`CONFIG_SCHEMA_VERSION` 33 → 34: новый ключ добавляется в packaged-шаблон, как в v27 для `supervisor.provider`. Точный номер пересчитать при реализации (по образцу решения P1-D3); на 2026-08-03 в [`config/schema.py:180`](../../../src/wastech_orchestrator/config/schema.py) стоит 33.

### D2 — при выключении правило «flow сужает каденцию» не применяется

Проверка 2b в [`validator.py:563`](../../../src/wastech_orchestrator/core/flow/validator.py) замыкается накоротко: flow не может расширить то, что вообще не запускается. Без этого `enabled: false` воспроизводит уже известную боль — `implementation.yaml` объявляет `observe.mode: events`, и задача падает в терминальный `failed` после claim'а.

### D3 — валидация ключей слоя при выключении: один warning вместо набора issues

`_validate_supervisor` при `enabled: false` не проверяет provider/model/reasoning/role_file, а добавляет **одно предупреждение**: «`supervisor.enabled: false` — `role_file`/`provider`/`observe`/`finalize`/`handoff` инертны». Иначе оператор, выключивший слой и не почистивший блок, получает ошибки про модель, которая никогда не будет вызвана. Containment-проверка **flow-local** промптов (пункт 7 инвентаря) при этом остаётся: это валидация авторского контента, она от конфига оператора не зависит.

### D4 — `skills.dynamic: true` при выключенном слое: warning + детерминированная деградация

Предложение карты скиллов выполнить нечем. Fail-open, а не fatal: слой `dynamic` и так фейл-открытый по дизайну (нерезолвленный токен в предложении просто отфильтровывается), поэтому «остались только пины оператора» — корректная деградация, а не тихая потеря гарантии. Один warning при валидации конфига, чтобы это не было незаметным.

### D5 — память при выключенном слое: связь обязательна, форма — по решению P3-Q1

Первая редакция рекомендовала фатальный issue конфига (вариант A). **Оператор выбрал вариант B — тихое гашение с warning'ом**; точная механика, цена и способ сделать её видимой описаны в [P3-Q1](#p3-q1--память-гасится-в-загрузчике-с-warningом-вариант-b). Общая часть решения от выбора не зависит: «слой выключен + память включена» — это не ортогональные ключи, а один режим, который платит и не получает (проблема (2) выше).

**Вариант C** (оставить ортогональными, только задокументировать) отвергнут: он и просьбу не выполняет, и оставляет ловушку.

### D6 — «пропущенные, кроме обязательных»: правило и данные (источник признака заменён решением P3-Q4)

Сегодня в follow-ups попадают **все** находки последнего вердикта по ноде; гейтящий статус не спрашивается. Новое правило: **в follow-ups идут только не-гейтившие (пропущенные) находки.**

Что осталось верным из первой редакции:

- `_to_finding` ([`nodes/evaluator.py:546`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)) нормализует находку при записи: `blocking: true` → `high`, `critical`/`high` → `high`, `medium` → `medium`, остальное → `low`. В `evaluations` лежит только эта триада — сырой `blocking`-флаг и сырой токен severity в БД не сохраняются.
- Порядок строгости — `SEVERITY_ORDER = ("blocking", "critical", "high", "medium", "low")`, дефолт гейта `high` ([`flow/schema.py:29,32`](../../../src/wastech_orchestrator/core/flow/schema.py)).
- Находки промежуточных раундов rework отбрасываются по дизайну (`_last_verdict_per_node`). Это правильно — `medium`, который фикс закрыл, не должен всплыть в PR.

Что **отменено**: считать гейтящесть на чтении (`gate_severity_by_node` из снапшота + публичный `severity_rank`). Из-за нормализации выше этот расчёт не воспроизводит решение гейта при `gate_severity ∈ {blocking, critical}`. Вместо него признак **персистится там, где решение принимается** — см. [P3-Q4](#p3-q4--признак-гейтила-персистится-эвалюатором-а-не-пересчитывается-на-чтении). Как следствие риск R7 снят, а `severity_rank` остаётся приватным в `nodes/evaluator.py`.

### D7 — обязательная находка, оставшаяся открытой: включать, но отдельной формулировкой

Подтверждено при приёмке ([P3-Q2](#p3-q2--открытая-гейтившая-находка-включается-отдельной-формулировкой)). Если гейтящая находка присутствует в **финальном `accept`** — значит неблокирующий эвалюатор исчерпал `max_rework_per_stage` и пошёл дальше (`rework_exhausted`). Формально она «обязательная» и по правилу D6 исключается, но потерять её хуже всего остального: **включаем**, с отличающимся `evidence` (`"<node> evaluator finding still open — rework budget exhausted"`), не смешивая с обычными пропущенными.

### D8 — что делать с заморозкой промптов при выключении: ничего (YAGNI)

Три flow-local supervisor-промпта продолжают морозиться в бандл. Это несколько килобайт и никакого поведения; специальный случай в `control_bundle` ради них — лишняя ветка в security-чувствительном месте. Если когда-нибудь окажется важным — придёт со своим кейсом.

### D9 — детерминированный отчёт: полноценный

Подтверждено при приёмке и расширено до «один писатель тела PR» — см. [P3-Q3](#p3-q3--полноценный-отчёт-и-один-писатель-тела-pr). Ключевой момент по трудозатратам: вся фактура уже собрана и детерминирована, нужен другой сериализатор (сейчас там `json.dumps`).

| Секция отчёта | Откуда берётся | Состояние |
| --- | --- | --- |
| Changes: изменённые файлы + diff stat + указатель на `logs/<id>/current.diff` | `_changes(diff_text, diff_path)` в `supervisor_packet.py` | ✅ есть (без инлайна diff — см. P3-Q3) |
| Steps: нода / kind / status / outcome / провайдер / fallback / skip | `collect_step_facts(node_runs, …)` во flow-recorder | ✅ есть |
| Checks: passed / failed / **skipped** (skipped отдельно, не в failed) | `_checks(check_runs)` | ✅ есть |
| Gates: вердикт каждого эвалюатора + его находки | `_render_gate_digest(evaluations)` | ✅ есть, переезжает (шаг 1) |
| Follow-ups | шаг 2 | 🔨 |
| Pipeline nodes skipped | `_skip_section_md(p)` | ✅ есть |

То есть это ~150–250 строк рендерера плюс тесты, а не новая подсистема.

## Решения приёмки (2026-08-03)

Четыре развилки. Q1–Q3 стояли в документе; **Q4 найден при обязательной сверке с кодом** и отменяет часть D6. Перед каждым вопросом код перечитывался — так же, как это делалось при приёмке P0/P1/P2.

### P3-Q1 — память гасится в загрузчике с warning'ом (вариант B)

**Решение оператора: тихое гашение + warning**, а не фатальный issue (вариант A, который рекомендовал документ).

Где именно, чтобы это не расползлось по рантайму:

1. Гашение — в `_parse` ([`config/loader.py:930`](../../../src/wastech_orchestrator/config/loader.py)), единственном месте, где обе секции уже собраны: если `supervisor.enabled is False` и `memory.enabled is True` → `memory` заменяется на `replace(memory, enabled=False)`.
2. Warning — в **уже существующий, но мёртвый** канал `ConfigLoadResult.warnings` (объявлен [`config/loader.py:87-91`](../../../src/wastech_orchestrator/config/loader.py), `_parse` получает список `warnings` и ни разу в него не пишет).
3. Чтобы warning было видно, канал нужно подключить: `_load_config` ([`cli.py:934`](../../../src/wastech_orchestrator/cli.py)) сегодня делает `load_config(path).config` и **выбрасывает** `warnings`. Добавить их логирование рядом с warning'ами `validate_config` — это единственный seam, через который проходит каждая команда (`load_config_for` → `_load_config`).

Почему именно так, а не через `validate_config`: та функция возвращает warning'и, которые точно видны, но конфиг у неё frozen — погасить она не может. Тогда `memory.enabled` осталось бы `true`, и каждый путь памяти пришлось бы обвешивать вторым условием «а слой-то включён?» — то есть ровно те новые ветки в путях памяти, которых это решение и избегает. После гашения в загрузчике **ни один** путь памяти не меняется: `_memory_service`, `_packet_builder`, `_build_cleanup_hook` и `cmd_memory` уже читают `config.memory.enabled`.

**Цена, которую решение принимает осознанно** (и которая была аргументом варианта A): расхождение между файлом и рантаймом. В `config.yaml` написано `memory.enabled: true`, а `worc memory show` печатает «disabled in config (memory.enabled: false)». Это против «no silent drift» ([`config/upgrade.py:45`](../../../src/wastech_orchestrator/config/upgrade.py)), поэтому текст warning'а обязан закрывать разрыв, а не просто существовать: назвать **оба** ключа, сказать, что память выключена для этого прогона, и как сделать файл честным. Формулировка, зафиксированная в приёмке:

> `supervisor.enabled: false` turns memory off for this run (`memory.enabled: true` in the config is ignored): the supervisor's finalize turn is the only path that adds anything memory can later read back, so with the layer off memory would keep adding packets to every prompt without ever learning. Set `memory.enabled: false` to make the config say what runs.

Дополнительно: `worc upgrade-config` ([`cli.py:1052`](../../../src/wastech_orchestrator/cli.py)) валидирует отрендеренный конфиг через `loads_config`, поэтому тоже должен печатать этот warning — иначе оператор увидит его в `run`/`watch`, но не в команде, которая конфиг переписывает.

### P3-Q2 — открытая гейтившая находка включается отдельной формулировкой

**Решение: включать** — D7 в силе без изменений. С решением Q4 этот случай к тому же становится тривиально детектируемым: находка с `gating: true` внутри ряда `verdict = accept` и есть `rework_exhausted` (никакой отдельной колонки не нужно). Обычные пропущенные и эта одна не смешиваются: у неё своё `evidence`.

### P3-Q3 — полноценный отчёт и один писатель тела PR

**Решение: полный рендерер из `PacketFacts`, и он вытесняет `write_minimal_summary` во всех терминалах.** Три части.

**(а) Один писатель.** У `write_minimal_summary` в коде ровно один вызов — `_summary_md_body` ([`orchestrator.py:3770`](../../../src/wastech_orchestrator/core/orchestrator.py)), и через него проходят все терминалы (`done`, деградировавший `done`, `failed`, `manual_action_required`). Поэтому «оставить заглушку для инфра-терминалов» не экономит ничего и создаёт ровно то, что запрещает R5 — два расходящихся формата тела PR. Новый рендерер занимает это место целиком; `write_minimal_summary` удаляется. Провалившийся прогон при этом получает отчёт **лучше** нынешнего: шаги, упавшие чеки и открытые находки — именно то, что нужно в `failed`.

**(б) `⚠️`-callout остаётся параметром, а не исчезает.** Три состояния должны читаться по-разному: слой выключен конфигом (нормальный артефакт, никакого предупреждения) — слой ожидался, но прозы не выдал (`degraded=True`, callout на месте) — терминал без прозы by design (`failed`/`manual`, предупреждения нет). Флаг `degraded` уже выставляется только внутри supervisor-ветки, так что механика не меняется, меняется только тело.

**(в) `summary.json` унифицируется, а не «доливается ключом».** Контракты сегодня расходятся: `{what, how, integration, why, [degraded]}` у детерминированного писателя против `{what, summary, [supervisor_usage], [follow_ups]}` у finalize. Сверка показала, что **в коде `summary.json` не читает никто** — расхождение держится только тестами (`tests/core/test_ledger.py`, `tests/core/test_supervisor.py`, `tests/core/test_cli_pipeline.py`), поэтому унификация стоит правки тестов и ничего больше. Единый контракт: `{what, summary, [follow_ups], [supervisor_usage], [degraded]}`, где `summary` — LLM-проза (пустая строка на детерминированном пути; сам отчёт живёт в `summary.md`). Триада `how/integration/why` удаляется: `how` был буквально фразой «No provider-authored summary was available», `integration`/`why` — константы. Отдельный маркер «кто писал» не вводится: наличие `supervisor_usage` уже отвечает «слой работал», `degraded` — «работал и не смог».

**(г) Полный diff в тело PR не встраивается.** В `PacketFacts` малый diff инлайнится (≤ 4 000 симв., P0-D3) — это оправдано для промпта, где иначе модель сделает лишний tool-round. В теле PR это бессмысленно (PR **и есть** diff) и воспроизводит ту самую проблему, из-за которой заглушка перестала инлайнить патч: реальный прогон дал ~580-строчный summary, почти целиком из diff. Отчёт печатает изменённые пути + stat + указатель `logs/<id>/current.diff`.

**(д) Наблюдения (`material_observations`) в отчёт не идут.** Это LLM-авторские заметки; в выключенном режиме их нет вообще, а в деградировавшем секция появлялась бы «через раз», делая формат неодинаковым между режимами. Они остаются в `packet.json` и в `evaluations` для разбора.

### P3-Q4 — признак «гейтила» персистится эвалюатором, а не пересчитывается на чтении

**Решение: эвалюатор дописывает `gating: true|false` в каждую находку в `evaluations.findings_json`; читатель делает `not f.get("gating")`.**

Почему деривация на чтении (как было в D6) не работает: `_to_finding` схлопывает `blocking: true`, `critical` и `high` **в одно значение `high`** ([`nodes/evaluator.py:554-560`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)), а гейт считает по сырой находке — `_is_blocking` смотрит на сырой флаг `blocking` и сырой токен severity ([`nodes/evaluator.py:534-545`](../../../src/wastech_orchestrator/core/flow/nodes/evaluator.py)). При `gate_severity: critical` три разных исходных случая (`blocking: true` — гейтит, `critical` — гейтит, `high` — не гейтит) лежат в БД **одинаково**, поэтому сравнение рангов на чтении их не различает. Ошибка была бы в благоприятную сторону (лишнее попадание в follow-ups), но она есть, и она молчаливая.

Что даёт персист:

- решение принимается один раз, там, где на руках и сырая находка, и `gate_severity` ноды — `_is_blocking` там уже вызывается по каждой находке;
- ноль проводки: `gate_severity_by_node` из снапшота в `Supervisor.finalize` и во второй путь отчёта не нужен;
- `severity_rank` не поднимается в публичный хелпер → второго ранжировщика шкалы не появляется (**R7 снят**);
- схема БД не двигается: `findings_json` — JSON-блоб, миграции нет; ряды без ключа читаются как `gating: false` (доброкачественная сторона — лишний follow-up, а не потерянный).

Инвариант реализации: флаги считаются **одним** проходом рядом с вердиктом и передаются и в `_verdict_for`, и в `_findings_json` — так вердикт и персистированные флаги физически не могут разойтись. Сегодня `any(self._is_blocking(f, gate_rank) for f in raw_findings)` короткозамыкается, поэтому список флагов вычисляется явно до `any(...)`.

Сырой артефакт находок (`findings.json`, который читает `fixing`) не меняется — он и так хранит сырую форму.

### P3-D10 — переехавшие имена становятся публичными

`core/follow_ups.py` — кросс-модульный API (его читают `supervisor.py` и `summary_report.py`), поэтому имена, которые пересекают границу модуля, теряют подчёркивание: `FollowUp`, `parse_follow_ups`, `evaluator_finding_follow_ups`, `merge_follow_ups`, `render_follow_ups_section`, `render_gate_digest`, `FINDING_TITLE_MAX`. Внутренние остаются приватными: `_finding_to_follow_up`, `_last_verdict_per_node`, `_row_findings`, `_follow_up_key`. `_render_findings_digest` (observe-промпт) остаётся в `supervisor.py` и импортирует `FINDING_TITLE_MAX`. Практическое следствие: строки импорта в `tests/core/test_supervisor.py:31-40` меняются обязательно — критерий «без правки ассертов» относится к ассертам, а не к импортам.

### P3-D11 — сборка `PacketFacts` выносится из класса `Supervisor`

Найдено при сверке: `PacketFacts` формируется методом `Supervisor._build_packet` ([`core/supervisor.py:978`](../../../src/wastech_orchestrator/core/supervisor.py)) и завязана на `self._store`, `self._artifacts_root`, `self._task_type`, `self._flow_name`, `self._exchange_root`, `self._repo_dir`. При выключенном слое объекта нет — значит «отчёт из тех же `PacketFacts`» без выноса физически не собирается. Это не было записано в плане и добавляется шагом 3.

Куда: модульная функция `build_packet_facts(...)` в **`core/supervisor_packet.py`** — там уже живёт `PacketFacts` и уже импортированы `state_store.CheckRunRow` и `recorder.StepFacts`, так что новых зависимостей и рисков для контрактов импорта нет. `Supervisor._build_packet` становится тонким вызовом; оркестратор вызывает ту же функцию напрямую. `material_observations` остаётся параметром: слой передаёт свой digest, путь отчёта — `None` (решение P3-Q3(д)).

Отдельно: пути внутри фактов (`diff_path`, `findings_path`) — **exchange**-относительные (`.worc-io/<task-id>/…`), потому что это единственные копии, которые может читать провайдер. Тело PR коммитится в репозиторий, поэтому в отчёте эти пути не печатаются: указатель на diff — `logs/<id>/current.diff`, как в нынешней заглушке.

### P3-D12 — doc-impact сужен до утверждений «слой всегда работает»

Первая редакция перечислила файлы по фразе «constant layer». Сверка показала, что фраза «the supervisor is a constant layer above the flow, **not a node**» после этой задачи остаётся **верной** — она различает слой и ноду, а не «всегда» и «опционально». Править нужно только те места, где утверждается, что слой работает **безусловно**. Список ниже переписан по этому критерию, с проверенными файлами.

## План реализации

Порядок не произвольный: шаги 1–3 полезны сами по себе и закрывают дырку p0-2 **даже если выключатель никогда не включат**, а шаг 4 после них становится тривиальным.

### Шаг 1 — вынести механику follow-ups из-под слоя (чистый перенос)

Новый модуль `core/follow_ups.py`, в него переезжают: `FollowUp`, `parse_follow_ups`, `_finding_to_follow_up`, `_last_verdict_per_node`, `_row_findings`, `_evaluator_finding_follow_ups`, `_merge_follow_ups`, `_follow_up_key`, `_render_follow_ups_section`, `_render_gate_digest`, `_FINDING_TITLE_MAX` — с переименованием по P3-D10. `supervisor.py` импортирует их оттуда.

Плюс контракт в [`.importlinter`](../../../.importlinter) по образцу уже существующего `step-record-below-supervisor`:

```ini
[importlinter:contract:follow-ups-below-supervisor]
name = Follow-up derivation does not depend on the supervisor layer
type = forbidden
source_modules =
    wastech_orchestrator.core.follow_ups
forbidden_modules =
    wastech_orchestrator.core.supervisor
    wastech_orchestrator.core.supervisor_packet
```

Критерий успеха шага: `pytest tests/core/test_supervisor.py` зелёный **без правки ассертов** (импорты меняются — P3-D10).

### Шаг 2 — правило «пропущенные, кроме обязательных»

- `nodes/evaluator.py`: посчитать флаги гейтящести одним проходом по сырым находкам и передать их и в `_verdict_for`, и в `_findings_json`, который дописывает `gating` в каждую персистируемую находку (P3-Q4). `severity_rank` остаётся приватным.
- `evaluator_finding_follow_ups(evaluations)` фильтрует по `gating` (D6) и помечает открытые гейтящие отдельным `evidence` (D7/P3-Q2). Никаких новых параметров — снапшот и `gate_severity` не нужны.

Отдельный коммит: это единственный шаг, меняющий поведение при `enabled: true` (состав follow-ups + новый ключ в аудите находок).

### Шаг 3 — детерминированный отчёт

- Вынести сборку фактов: `build_packet_facts(...)` в `core/supervisor_packet.py`, `Supervisor._build_packet` — тонкий вызов (P3-D11).
- Новый `core/summary_report.py`: `render_summary_report(facts: PacketFacts, *, follow_ups, gates, skipped_nodes, task_ref, degraded) -> str`. Тот же вход, что у `render_packet`, другой выход. Запись — с `newline=""` (дом. паттерн для коммитимых/шаблонных файлов; нынешний `write_minimal_summary` пишет без него).
- `_summary_md_body` вызывает отчёт вместо `write_minimal_summary`; `write_minimal_summary` удаляется (P3-Q3(а)).
- `summary.json` унифицируется на `{what, summary, [follow_ups], [supervisor_usage], [degraded]}` (P3-Q3(в)); детерминированный путь пишет его сам, потому что раньше это делал `write_minimal_summary`.
- Follow-ups на детерминированном пути собирает оркестратор (`evaluator_finding_follow_ups` по `get_evaluations`) — это и есть закрытие дырки p0-2 в деградировавшем режиме.

### Шаг 4 — сам выключатель

- `SupervisorConfig.enabled: bool = True` в [`config/schema.py:577`](../../../src/wastech_orchestrator/config/schema.py). Все существующие конструкции `SupervisorConfig(...)` — на ключевых аргументах (проверено: `loader.py:796`, `tests/core/test_supervisor.py:142,1197`), позиционные вызовы не сломаются.
- Загрузчик: `enabled=_bool(m, "enabled", True, where, issues)` в `_build_supervisor` ([`config/loader.py:769`](../../../src/wastech_orchestrator/config/loader.py)) и `"enabled"` в `_check_keys`.
- Загрузчик, `_parse`: гашение памяти + warning в канал `ConfigLoadResult.warnings` (P3-Q1); `cli.py:_load_config` начинает эти warning'и логировать.
- Оркестратор, [`orchestrator.py:2581`](../../../src/wastech_orchestrator/core/orchestrator.py) — одна строка:

```python
self._supervisor = (
    self._build_supervisor(p, snapshot, flow_dir=bundle.flow_dir)
    if self._config.supervisor.enabled
    else None
)
```

- Валидатор flow: замкнуть проверку 2b при `enabled: false` (D2).
- Валидатор конфига: warning вместо issues (D3), warning про `skills.dynamic` (D4).
- `install/config_writer.py`: писать `"enabled": True` явно — по той же логике, по которой там явно пишутся `skills.dynamic: false` и `observe.mode`.
- `CONFIG_SCHEMA_VERSION` → 34, ключ доливается шаблоном в `upgrade-config` (в `_REMOVED_KEYS` ничего не добавляется — ключ новый, а не удалённый).

### Шаг 5 — документация (в том же изменении, `/sync-docs`)

Перечислено в разделе «Doc-impact» ниже, по критерию P3-D12.

## Критерии приёмки

Поведение по умолчанию:

- [ ] Конфиг без ключа `enabled` и конфиг с `enabled: true` дают идентичный набор provider-вызовов, строк `evaluations` (кроме нового ключа `gating` внутри `findings_json`), содержимое пакета и формат `summary.md`. Регресс-тест ловит именно это, а не только «слой построился».
- [ ] `pytest` проходит без правки ассертов существующих supervisor-тестов после шага 1 (импорты — меняются, P3-D10).

Выключение:

- [ ] `enabled: false` → ни одного `provider_attempts`-ряда с `supervisor_function`, ни строки `supervisor_step`/`supervisor_final`, нет `packet.json`, нет блока `supervisor_usage` в `summary.json`.
- [ ] `enabled: false` + `implementation.yaml` (объявляет `observe.mode: events`) → задача **проходит валидацию** и выполняется; `worc validate-flow --all` тоже зелёный (D2 — проверка 2b замкнута; команда прогоняет тот же config-aware валидатор).
- [ ] `enabled: false` на декомпозирующем flow → бриф предшественника всё равно записан (детерминированный «пол»), файл `logs/<id>/subtasks/NN-slug.handoff.md` существует и непуст.
- [ ] `enabled: false` + `skills.dynamic: true` → warning, задача выполняется, эффективный набор скиллов = только пины оператора.
- [ ] `enabled: false` → `summary.md` не содержит callout «⚠️ Fallback summary» (это не деградация); `enabled: true` + finalize без прозы → callout есть.
- [ ] `enabled: false` + `memory.enabled: true` → задача выполняется; резолвнутый конфиг имеет `memory.enabled is False`; warning с текстом из P3-Q1 напечатан на каждой команде, которая грузит конфиг (включая `upgrade-config`); за прогон не создано ни одной записи памяти и ни один промпт не получил `{memory_path}`-пакет; `worc memory show` печатает «disabled».

Follow-ups и отчёт:

- [ ] Эвалюатор вернул `accept` c `medium` при `gate_severity: high` → находка в `summary.json` **и** в секции `## Technical debt / follow-ups` тела PR при **всех трёх** режимах: слой включён, слой выключен, слой включён но finalize не выдал прозы (последний — закрытие дырки p0-2).
- [ ] Находка `{severity: "low", blocking: true}` → персистится как `severity: "high"`, `gating: true`, гейтит и в follow-ups **не** попадает (кроме случая D7).
- [ ] Нода с `gate_severity: medium`: `medium` персистится с `gating: true` и не попадает в follow-ups; `low` попадает.
- [ ] Нода с `gate_severity: critical`: сырой `critical` → `gating: true` (не в follow-ups), сырой `high` → `gating: false` (в follow-ups) — тот самый случай, который деривация на чтении не различала (P3-Q4).
- [ ] `rework_exhausted`: находка с `gating: true` в ряду `verdict = accept` попадает в follow-ups с `evidence`, отличающимся от обычных пропущенных (D7).
- [ ] Дедуп: находка, о которой supervisor уже написал сам, не дублируется (существующее поведение `merge_follow_ups` сохранено).
- [ ] Детерминированный отчёт содержит шаги, чеки (с отдельным `skipped`), вердикты гейтов, follow-ups и пропущенные ноды; полного diff в теле нет, есть указатель на `logs/<id>/current.diff`; два прогона над одним и тем же `state.db` дают **байт-идентичный** отчёт (тот же контракт детерминизма, что у пакета — P0-D2).
- [ ] `write_minimal_summary` в коде отсутствует: тело PR во всех терминалах (`done` / degraded `done` / `failed` / `manual_action_required`) пишет один рендерер (P3-Q3(а)).
- [ ] `summary.json` имеет один набор ключей на всех путях (P3-Q3(в)); триады `how/integration/why` нет нигде.
- [ ] `lint-imports` подтверждает, что `core.follow_ups` не зависит от `core.supervisor`.

## Тесты под добавление

- `tests/core/test_follow_ups.py` — новый: правило «кроме обязательных» по матрице `severity × gate_severity` **включая `gate_severity: critical`**, `blocking: true` на низкой severity, `rework_exhausted`, дедуп, `(node_id, subtask_order)`-ключ (регресс уже исправленного в p0-2 бага с декомпозицией), ряд без ключа `gating` читается как не-гейтивший.
- `tests/core/test_flow_node_runners.py` (там уже живут `gate_severity`-тесты эвалюатора) — `gating` персистится и совпадает с вердиктом на всех значениях `gate_severity`.
- `tests/core/test_summary_report.py` — новый: байт-идентичность двух рендеров, наличие каждой секции, отсутствие секции при пустых данных (не пустой заголовок), отсутствие инлайн-diff, `newline=""`.
- `tests/core/test_orchestrator.py` — `enabled: false`: ноль supervisor-вызовов; отчёт как тело PR; handoff-пол на месте; отсутствие callout'а; `skills.dynamic` деградирует. Плюс отчёт (а не заглушка) на `failed`/`manual_action_required`.
- `tests/core/test_ledger.py` — тесты `write_minimal_summary` заменяются тестами рендерера; `summary.json`-контракт переписан на единый набор ключей.
- `tests/core/test_flow_threat_model.py` — flow с `observe.mode: events` при глобальном `enabled: false` проходит валидацию (D2).
- `tests/config/test_validation.py` — warning про инертные ключи, warning про `skills.dynamic`.
- `tests/config/test_loader.py` — гашение памяти при `supervisor.enabled: false`: резолвнутый `memory.enabled is False` + warning в `ConfigLoadResult.warnings` (P3-Q1).
- `tests/config/test_upgrade.py` + `tests/install/test_config_writer.py` — новый ключ доливается и пишется явно.
- `tests/test_cli_memory.py` — `worc memory` при выключенном слое (уже существующий no-op-путь, но теперь достижимый через `supervisor.enabled: false`).

## Риски и ловушки

**R1 — правило «flow может только сузить каденцию».** Главная ловушка. Если выключение попытаться выразить глобальным `observe.mode: none`, `implementation.yaml` (объявляет `events`) упадёт валидацией **после** claim'а: задача уходит в терминальный `failed`, её надо перезакидывать руками, а `watch` перемалывает так всю очередь. Ловушка уже описана и в шипнутом доке (`guide/config/reference.md:213-215`). Отсюда D1 (отдельный ключ) и D2 (замкнуть проверку).

**R2 — инвариант в правилах.** [`.agents/rules/architecture.md:44`](../../../.agents/rules/architecture.md) объявляет «**Constant supervisor layer** (not a node): it observes every completed step…». Формулировку обязательно править в том же изменении, иначе задача формально нарушает hard invariant проекта (заодно там уже устарело «every completed step» — после P1 наблюдения каденцированы).

**R3 — два ранее записанных deliberate non-goal частично реверсируются.** В [follow_ups.md](follow_ups.md) есть «No config toggle for fresh finalize (P0-D4)» и «No `finalize.enabled` / `handoff.enabled` keys (P1)». Обе записи уже обновлены указателем на этот документ (2026-08-03): пофазные ключи остаются non-goal, а возражение «запуск без summary» снимается шагом 3, а не объявляется ошибочным.

**R4 — регресс качества тела PR.** Без шага 3 включение выключателя ухудшает каждый PR. Поэтому шаги 1–3 обязаны ехать вместе с шагом 4, а не «потом».

**R5 — контракт `summary.json`** (понижен при приёмке). В коде его не читает никто — расхождение форматов держится только тестами, поэтому унификация из P3-Q3(в) стоит правки тестов, а не поиска потребителей. Риск остаётся ровно в одном: «подпихнуть ключ» вместо унификации.

**R6 — объём тестовой поверхности.** `supervisor` упоминается в 25 тестовых файлах (в `tests/core/test_supervisor.py` — 222 раза). Дефолт `enabled: true` означает, что почти ничего не переписывается, но новых тестов нужно ~15–20.

**R7 — где живёт `severity_rank`** — **снят** решением P3-Q4: гейтящесть персистится, второй ранжировщик шкалы не появляется, `severity_rank` остаётся приватным в `nodes/evaluator.py`.

**R8 — тихое расхождение конфига и рантайма** (новый, следствие P3-Q1). Оператор читает `memory.enabled: true` в файле, а память выключена. Митигация — только текст warning'а и его видимость на каждой команде, включая `upgrade-config`. Если после реализации это окажется источником путаницы, откат к варианту A (фатальный issue) — одна строка, потому что обе ветки живут в одном месте загрузчика.

## Doc-impact

Критерий (P3-D12): править только утверждения «слой работает **безусловно**». Фраза «constant layer above the flow, **not a node**» остаётся верной и не трогается — она различает слой и ноду. По этому критерию на `dev`:

- [`.agents/rules/architecture.md:44`](../../../.agents/rules/architecture.md) — «Constant supervisor layer» → «константный по умолчанию, может быть выключен целиком» (R2).
- [`src/wastech_orchestrator/packaged/config.example.yaml:286`](../../../src/wastech_orchestrator/packaged/config.example.yaml) — блок `supervisor:` подписан «constant oversight layer above any flow — **always active**»; добавить `enabled` с объяснением и убрать «always».
- [`src/wastech_orchestrator/packaged/guide/config/reference.md`](../../../src/wastech_orchestrator/packaged/guide/config/reference.md) — строка таблицы для `supervisor.enabled`; заголовок «`supervisor` — the constant oversight layer» (:174) и абзац под ним; абзац «Switching observations off does not cost you the summary» (:211) — теперь про два уровня (наблюдения и весь слой); связь с `memory.enabled` (P3-Q1) в разделе про память.
- [`src/wastech_orchestrator/packaged/guide/README.md:25`](../../../src/wastech_orchestrator/packaged/guide/README.md) — «a constant supervisor writes the summary at the end» — при выключенном слое summary пишет детерминированный рендерер.
- [`src/wastech_orchestrator/packaged/flows/implementation.yaml:21`](../../../src/wastech_orchestrator/packaged/flows/implementation.yaml) — «the constant supervisor layer writes the summary at whole-task close» (объяснение, почему во flow нет `summary`-ноды): дополнить тем, что без слоя тело PR пишется детерминированно.
- [`README.md`](../../../README.md) — упоминания слоя как безусловного (проверить при реализации; на 2026-08-03 фраз «always active» там нет).
- [follow_ups.md](follow_ups.md) — обе записи из «Deliberate non-goals» уже обновлены (R3); дополнить их результатом приёмки, если формулировки решений изменят смысл.
- [README.md кампании](README.md) — строка в таблице items, статус кампании и таблица решений P3.

**Не трогаем** (проверено, утверждение остаётся верным): `guide/flows/README.md:17`, `guide/flows/prompt-variables.md:30`, `guide/flows/roles.md:77`, комментарии «constant layer above the flow (not a node)» в `blog_article.yaml`, `blog_article_revise.yaml`, `content_chapter.yaml`, `content_translate.yaml`, `implementation.yaml:170`.

Для `main` (одна строка в [main-docs-reconstruction-notes.md](../main-docs-reconstruction-notes.md)): затронуты `configuration.md` (новый ключ + связь с памятью), `worc_architecture.md` (слой перестаёт быть безусловным), `operations.md` / `cookbook.md` (режим «работаем без supervisor'а»).

## Вне объёма

- Пофазные `finalize.enabled` / `handoff.enabled` / `observe.enabled` — один ключ на весь слой; пофазные остаются non-goal (P1).
- Любая миграция или backward-compat машинерия — проект greenfield.
- Изменение схемы БД: не требуется (P3-Q4 — ключ в JSON-блобе, не колонка).
- Изменение логики самих фаз слоя, их промптов и схем структурированного вывода — прямо запрещено главным инвариантом этой задачи.
- CLI-поверхность для чтения supervisor-usage (`worc usage`) — остаётся non-goal (P0/P2).
- LLM-авторские `follow_ups` (`emit_follow_ups`) — не трогаем: это отдельный источник, который при выключенном слое просто отсутствует.
- Перенос SUCCESS-эпизода памяти из-под supervisor-ветки (чтобы эпизоды писались и без слоя) — не делаем: эпизодический тир write-only, читаемого выигрыша нет, а P3-Q1 гасит память целиком.

## Вероятные области реализации

`config/schema.py`, `config/loader.py` (ключ + гашение памяти + warning), `config/validation.py`, `config/upgrade.py` (шаблон), `cli.py` (подключение `ConfigLoadResult.warnings`), `install/config_writer.py`, `core/orchestrator.py`, `core/supervisor.py` (импорты + тонкий `_build_packet`), новые `core/follow_ups.py` и `core/summary_report.py`, `core/supervisor_packet.py` (`build_packet_facts`), `core/flow/nodes/evaluator.py` (персист `gating`), `core/flow/validator.py`, `ledger.py` (удаление `write_minimal_summary`), `.importlinter`, packaged `config.example.yaml` + `guide/`.
