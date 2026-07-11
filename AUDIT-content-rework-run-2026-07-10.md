# Аудит автономного прогона content-rework (ch07–ch18) — 2026-07-10

> Пост-мортем большого автономного запуска флоу `content_chapter` на репозитории контента `wastime-app-content`. Проанализированы все артефакты под `.worc/` целевого репозитория (леджер, `state.db`, per-node логи, `prompt-audit`, рендер-промпты, диффы) и перекрёстно сверены с исходниками оркестратора (пакетные флоу, ролевые промпты, провайдерские адаптеры, роутер, движок флоу). Отчёт read-only: он находит причины и предлагает рычаги, но код не меняет.

---

## 1. Короткий итог (TL;DR)

**Первопричина одна: во время прогона был исчерпан лимит подписки Claude** (`"You've hit your session limit · resets 6:30am (Europe/Warsaw)"`). С ~02:57 UTC каждый вызов `claude` возвращался мгновенно, с нулём токенов, без реальной работы. Всё остальное — это **не отдельные баги, а цепочка неверных реакций оркестратора на это одно событие**, из-за которой 13 задач не «подождали и повторились», а превратились в терминальные `manual_action_required` с пустым результатом.

Ключевая техническая ошибка: сообщение о лимите подписки **не распознаётся как `RATE_LIMITED`**, поэтому классифицируется как `TASK_FAILURE` («агент отработал, но не решил задачу»). Это отключает всё, что должно было спасти прогон: нет fallback на `codex`, нет отложенного повтора (park), нет паузы очереди. Оркестратор просто маршировал по остатку очереди раз в 5 минут, «сжигая» задачи об стену, которую не мог пробить.

**Хорошая новость:** ни одна задача не потеряна. Все 13 в статусе `manual_action_required` (не `failed`), файлы задач целы, дифф в основном пуст — прогон почти ничего не успел испортить. После сброса лимита их можно просто перезапустить.

---

## 2. Что произошло: факты прогона

Всего в логах **19 прогонов** (главы ch01–ch18 плюс ch08b). Из них:

- **ch01–ch06 — успех.** Легли чистыми коммитами `feat(...)` в `tasks/done/` (ветка `worc/1783624809-rework-ch01-...`, `branch_mode: current`, локальный коммит без PR).
- **ch07–ch18 + ch08b — 13 задач в `manual_action_required`.** Именно они лежат сейчас в `.worc/tasks/rejected/`.

(Вы упомянули «16 штук» — фактически неудачных именно 13; расхождение, вероятно, из-за ch08b и способа подсчёта. На выводы это не влияет.)

### Хронология (UTC)

| Время (UTC) | Событие |
| --- | --- |
| 2026-07-09 19:20 | Старт прогона (ch01) |
| 19:20 → 02:28 (10-го) | ch01–ch06 отрабатывают нормально (с паузами `poll_interval` = 5 мин и ожиданиями) |
| 02:29 → 03:00 | **ch07** — переходная задача: контекст/ревайз/фиксинг/оценщики отрабатывают **успешно** до 02:55, затем в **02:57:35** `product_accuracy` и `story_critic` внезапно падают в `task_failure` |
| **~02:57 UTC (04:57 Warsaw)** | **Исчерпан лимит подписки Claude.** С этого момента каждый вызов `claude` возвращается за ~2 секунды с текстом `"You've hit your session limit · resets 6:30am"`, `output_tokens: 0` |
| 03:05 → 04:08 | ch08, ch08b, ch09…ch18 — авто-режим подхватывает следующую задачу каждые ~5 мин и каждую доводит до терминального `manual_action_required` |
| **06:30 Warsaw (04:30 UTC)** | Время сброса лимита, указанное в сообщении |

Сообщение о лимите встречается в артефактах прогона **171 раз**. `provider_used`: `claude` — 140 узловых прогонов, `codex` — **0** (fallback не сработал ни разу).

### ch07 — точка перелома (доказательство первопричины)

Таймлайн ch07 показывает момент, когда стена упёрлась в середину задачи:

```
91  context          claude  succeeded   02:29:00
92  revise           claude  succeeded   02:33:47
94  fixing           claude  succeeded   02:39:18
96  product_accuracy claude  succeeded   02:40:36
97  story_critic     claude  succeeded   02:44:24     ← реальный critic_fix-цикл
98  fixing           claude  succeeded   02:47:05
100 product_accuracy claude  succeeded   02:50:24
101 story_critic     claude  succeeded   02:52:56
102 fixing           claude  succeeded   02:55:22
104 product_accuracy claude  FAILED task_failure 02:57:35   ← лимит подписки
105 story_critic     claude  FAILED task_failure 03:00:32
```

То есть флоу работал **правильно** (полноценные циклы правок), пока не кончилась квота. Это исключает версию «плохие промпты / плохой флоу» как первопричину.

---

## 3. Как одно событие превратилось в 13 отказов

Флоу `content_chapter` линеен: `context → revise → constraints(tool) → product_accuracy(eval) → story_critic(eval) → style → publish`, петли правок `fixing` с бюджетами `constraint_fix: 12`, `accuracy_fix: 6`, `critic_fix: 6` ([content_chapter.yaml](src/wastech_orchestrator/packaged/flows/content_chapter.yaml)).

Когда `claude` перестал работать, **узел `revise` тоже возвращал ноль** — значит файл главы оставался неизменным, а дальше задача падала там, где стена била первой. Отсюда два внешне разных «симптома», но одна причина:

**Категория A — оценщик fail-closed (10 задач: ch07 `story_critic`; ch08, ch08b, ch11–ch17 `product_accuracy`).** Оценщик — это агент `claude`, обязанный вернуть `structured_output` с массивом `findings`. Лимит → пустой вывод → узел «закрывается» (fail-closed) → `manual_action_required`. Отчёт врёт про причину: пишет `"structured_output did not include a parseable 'findings' array (schema not honored)"`, хотя схема ни при чём — агент просто не запускался.

**Категория B — `constraint_fix` выжигает бюджет (3 задачи: ch09, ch10, ch18).** Здесь детерминированный инструмент `check_journey` нашёл **реальное, тривиально правимое** нарушение. Пример из ch09:

```json
{
  "outcome": "fail",
  "violations": {
    "09_...ru.md": [
      "page '9.5.2 Скуку тоже можно потерять': AI antithesis pattern \"не …, а …\" — rephrase without the cliché"
    ]
  }
}
```

Одну фразу-антитезу нужно было переписать. Но агент `fixing` каждый раз возвращал `"You've hit your session limit"` / `output_tokens: 0`, нарушение не устранялось, и петля впустую отмотала **все 12 циклов** (~38 узловых прогонов на задачу).

Оба пути привели в один и тот же тупик — `manual_action_required` с пустым `current.diff`.

---

## 4. Полный список проблем (findings)

Порядок — по важности для стабилизации. У каждой: симптом → механизм (с точной ссылкой на код) → рычаг.

### F1 — [P0] Адаптер не распознаёт лимит подписки Claude как `RATE_LIMITED`

**Это корневой баг.** Сообщение `"You've hit your session limit · resets 6:30am"` приходит в терминальном событии `result` (в `final_message` на **stdout**), с `is_error=true`, `subtype="success"`. В [claude.py:364-372](src/wastech_orchestrator/providers/claude.py#L364-L372) это даёт `succeeded=False`, `failure_subtype="success"`, `structured_output=None`. Далее [_adapter_base.py:411-421](src/wastech_orchestrator/providers/_adapter_base.py#L411-L421) превращает любой «распарсенный, но неуспешный» результат в `NormalizedError(ErrorClass.TASK_FAILURE)` — **не заглядывая в `final_message`**.

Даже если бы заглядывал — сигнатура `RATE_LIMITED` в [claude.py:102-103](src/wastech_orchestrator/providers/claude.py#L102-L103) это `rate limit|429|too many requests|quota exceeded|overloaded`, и она **не содержит** `session limit` / `hit your` / `resets`. Плюс сигнатуры применяются только к **stderr**, а сообщение пришло в stdout-конверте.

**Рычаг:** (а) расширить регекс `RATE_LIMITED` в [claude.py](src/wastech_orchestrator/providers/claude.py) (и симметрично в [codex.py:79](src/wastech_orchestrator/providers/codex.py#L79)) паттернами лимита подписки: `session limit|usage limit|hit your (session|usage) limit|limit .* resets|resets \d+[:.]\d+`; (б) в пути финализации неуспешного терминального события сверять **`final_message`** (а не только stderr) с сигнатурой лимита и **поднимать `ProviderError(RATE_LIMITED)`**, а не возвращать `TASK_FAILURE`. Разница «поднять vs вернуть» критична для F2.

### F2 — [P0] Fallback на `codex` не сработал ни разу

`provider_used`: claude 140×, codex 0×. Причина прямо задокументирована в [router.py:17](src/wastech_orchestrator/routing/router.py#L17): _«качественный `AgentRunResult(status=failed)` никогда не триггерит fallback; только поднятый `ProviderError`»_. Поскольку лимит вернулся как `TASK_FAILURE`-**результат**, а не поднятая ошибка, роутер даже не рассматривал запасного провайдера. При этом `RATE_LIMITED` **входит** в `FALLBACK_ELIGIBLE` ([base.py:64-75](src/wastech_orchestrator/providers/base.py#L64-L75)).

**Рычаг:** прямое следствие F1. Как только лимит будет **подниматься** как `RATE_LIMITED`, роутер получит право уйти на `codex`. `codex` использует отдельную квоту (OpenAI), не связанную с подпиской Claude, — то есть fallback с большой вероятностью **доработал бы эти задачи прямо ночью**. Нужно проверить, что путь из F1 именно _raise_, а не _return_.

### F3 — [P1] Оценщик диагностирует infra-падение агента как «схема не соблюдена»

В [evaluator.py:135-152](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L135-L152) ветка `if outcome.result is None:` ловит случай «агент не выдал результата». Но **упавший** запуск (`status=FAILED`, `structured_output=None`) — это не `None`; он проваливается в ветку `raw_findings is None` → fail-closed с формулировкой `"schema not honored"`. Оценщик **не проверяет `outcome.result.status`/`error_class`** перед тем, как обвинить схему.

**Рычаг:** в `evaluator.run` перед веткой «schema not honored» проверять, не упал ли сам запуск агента (FAILED / есть terminal error), и трактовать это как infra-случай (как `result is None`). Тогда диагностика будет честной, а маршрутизация — правильной даже для нераспознанных infra-сбоев.

### F4 — [P1] Петля `constraint_fix` выжигает весь бюджет на infra-noop’ах

ch09/ch10/ch18: 12 циклов, ~38 узловых прогонов, ~100 c впустую — каждый `fixing` был session-limit-noop’ом (`output_tokens: 0`). Петля считает только номер цикла и снова заходит в `constraints`; она **не замечает, что фиксер ни разу не запустился** (повторный infra-сбой с нулевым выводом), и не выходит раньше.

**Рычаг:** в обработке fix-петли (движок флоу / узел `fixing`) — если агент петли падает по infra-причине или N раз подряд даёт нулевой прогресс, **прерывать петлю как infra-сбой** (park/fallback), а не докручивать бюджет. Частично закрывается F1 (тогда `fixing` поднимет `RATE_LIMITED` и петля встанет), но защита от «нулевого прогресса» нужна независимо.

### F5 — [P0] Нет «предохранителя» на уровне очереди auto_mode

После стены в ~02:57 авто-режим (`auto_mode.enabled: true`, `confirm_next_task: false`, `poll_interval_seconds: 300`) подхватывал следующую задачу **каждые 5 минут** и доводил её до терминального состояния — так сгорели 12 задач подряд. Сигнал об исчерпании квоты на задаче N никак не влияет на подхват задачи N+1.

**Рычаг (главный для ночной автономии):** ввести предохранитель на уровне очереди — при терминале по `RATE_LIMITED` / повторных провайдерских infra-сбоях **приостанавливать подхват новых задач** до истечения cooldown или до времени сброса, указанного в сообщении. Это то, что превращает «сожгли 12 задач» в «подождали и продолжили».

### F6 — [P1] У `RATE_LIMITED` нет пути отложенного повтора (park); при лимите на обоих провайдерах — терминал

`_park` (resumable-пауза) срабатывает только для `error_class in TRANSIENT_RETRYABLE (={PROVIDER_UNAVAILABLE, NETWORK_UNAVAILABLE}) or CANCELLED` ([orchestrator.py:1770](src/wastech_orchestrator/core/orchestrator.py#L1770)). `RATE_LIMITED` намеренно исключён из `TRANSIENT_RETRYABLE` ([base.py:80-88](src/wastech_orchestrator/providers/base.py#L80-L88)) в расчёте на «долгий defer», **но самого defer-пути нет** — вся надежда на fallback. Если запасной провайдер тоже в лимите, роутер исчерпается и поднимет `NodeInfraError(RATE_LIMITED)`, который не попадёт в `_park` → `self._fail` → терминал.

**Рычаг:** направлять исчерпание `RATE_LIMITED` в `_park` (resumable) с потолком `agents.retry.max_blocked_s`, в идеале — до времени сброса, распарсенного из сообщения. F5 останавливает подхват **новых** задач, F6 держит **текущую** возобновляемой.

### F7 — [P2] Терминал `manual_action_required` с пустым диффом вводит в заблуждение

12 из 13 задач — `manual_action_required` с пустым `current.diff`: ревьюить нечего, `revise` не запускался. Путь `EvaluatorInfraError → _fail(status=MANUAL_ACTION_REQUIRED)` ([orchestrator.py:1757-1766](src/wastech_orchestrator/core/orchestrator.py#L1757-L1766)) задуман, чтобы **не выбрасывать уже готовый зелёный дифф**. Но при **пустом** диффе «требуется ручное действие» — неверный ярлык: это infra-обрыв, замаскированный под «нужен ревью».

**Рычаг:** при деградации evaluator-infra в manual проверять наличие диффа; пустой дифф + infra-причина → park / infra-fail, а не manual. Приоритет ниже — вопрос чистоты статусов и операторской ясности.

### F8 — [P2] Операционка: ночная автономия без учёта окна подписки

Батч стартовал в 19:20 и шёл всю ночь, упёршись в скользящее 5-часовое окно подписки в ~02:57. Дополнительно: `agents.retry.max_blocked_s: 3600` (1 час) **меньше**, чем ожидание до сброса (до ~1.5 ч), — даже корректный park мог не дожить до сброса.

**Рычаг (операционный):** либо меньшие батчи / запуск с учётом окна подписки, либо (лучше) опора на F5+F6, делающие прогон самотормозящим. Для ночной автономии рассмотреть повышение `max_blocked_s`.

### F9 — [P2] Задачи в валидационном карантине `.worc/tasks/rejected/`, хотя это runtime-manual

Все 13 в `state.db` имеют статус `manual_action_required`; `tasks/failed/` и `tasks/pending/` пусты; при этом файлы задач лежат в `.worc/tasks/rejected/` — это `validation.quarantine_folder`, куда штатно попадают провалы **валидации**, а не runtime-исходы. Либо файлы перенесены вручную, либо есть несоответствие в жизненном цикле для `manual_action_required`.

**Рычаг:** подтвердить, кто переместил файлы. Если это делает оркестратор — развести «карантин валидации» и «место для runtime-manual/parked» задач. На восстановление не влияет (файлы целы).

---

## 5. Разбивка по задачам

| Задача | Упавший узел | Петля / лимит | fix-циклы | Терминал | Первопричина |
| --- | --- | --- | --- | --- | --- |
| ch07-bonus-timers | story_critic | critic_fix → infra fail-closed | 3 | manual_action_required | Лимит с 02:57 (частичная работа сделана) |
| ch08-live-calculator | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch08b-bonus-topic-seeding | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch09-wasted-time-calculator | constraints | constraint_fix / `max_fix_cycles` | 12 | manual_action_required | Реальное нарушение (антитеза), не пофикшено из-за лимита |
| ch10-bonus-life-timers | constraints | constraint_fix / `max_fix_cycles` | 12 | manual_action_required | То же |
| ch11-other-calculators | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch12-bonus-data-backup | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch13-convertor | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch14-bonus-publish-calendar | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch15-overview | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch16-bonus-questions | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch17-bonus-reset-delete-account | product_accuracy | infra fail-closed | 0 | manual_action_required | Лимит подписки |
| ch18-final-words | constraints | constraint_fix / `max_fix_cycles` | 12 | manual_action_required | Реальное нарушение, не пофикшено из-за лимита |

---

## 6. План исправления и стабилизации

Цель: следующий автономный прогон, упершись в лимит, должен **сам приостановиться и возобновиться после сброса**, не теряя и не «сжигая» задачи. Три P0-пункта дают это в связке.

### P0 — обязательно до следующего автономного прогона

1. **F1 — распознавать лимит подписки как `RATE_LIMITED`.** Расширить сигнатуру и сверять `final_message`, поднимая `ProviderError(RATE_LIMITED)`. Симметрично для `codex`. _Тесты:_ юнит на строку `"You've hit your session limit · resets 6:30am"` → `RATE_LIMITED` (fake-CLI фикстура, есть скилл `/fake-cli`).
2. **F5 — предохранитель очереди.** При терминале по `RATE_LIMITED` / повторных провайдерских infra-сбоях приостановить подхват новых задач авто-режимом до cooldown / времени сброса.
3. **F6 — park вместо терминала при `RATE_LIMITED`.** Исчерпание лимита на всех провайдерах → resumable park с потолком `max_blocked_s`, в идеале до распарсенного времени сброса.

### P1 — надёжность исполнения

4. **F2 — проверить, что fallback на `codex` реально включается** после F1 (путь именно _raise_). Это даёт устойчивость даже в середине окна лимита.
5. **F3 — оценщик отличает падение агента от «схема не соблюдена».** Честная диагностика + правильная маршрутизация infra-сбоя на оценщике.
6. **F4 — fix-петля выходит при infra/нулевом прогрессе**, а не докручивает бюджет `constraint_fix`.

### P2 — чистота статусов и операционка

7. **F7** — пустой дифф + infra-причина → park/infra-fail, не `manual_action_required`.
8. **F8** — операционная политика ночных батчей + пересмотр `max_blocked_s` для автономии.
9. **F9** — подтвердить/развести жизненный цикл `rejected/` (карантин валидации) vs runtime-manual.

> Формализовать P0/P1 как ADR удобно через скилл `/adr` (в `docs/backlog/`), а реализовывать — через `/implement` с прогоном `/run-checks`.

---

## 7. Немедленное восстановление (можно делать сейчас)

Лимит подписки давно сброшен (было указано 06:30 Warsaw). Все 13 задач **целы и возобновляемы** — это `manual_action_required`, не `failed`, дифф в основном пуст.

1. **Перезапустить в свежем окне подписки** (или с проверенным `codex` как fallback/primary), лучше **малым батчем**, чтобы не упереться в окно снова.
2. **Перенести** 13 `.md` из `.worc/tasks/rejected/` обратно в `tasks/pending/` и запустить прогон заново (для сохранения частичной работы — `rerun --continue`).
3. **По задачам:**
   - **ch07** — есть частичные правки в коммите `chore(...): manual action required`; сверить их перед перезапуском.
   - **ch08, ch08b, ch11–ch17** — дифф пуст, чистый повтор.
   - **ch09, ch10, ch18** — у каждой одно реальное нарушение `check_journey` (паттерн-антитеза `"не …, а …"`); после сброса лимита `fixing` устранит его штатно.
4. **До P0-фиксов** — не запускать большой автономный батч без присмотра: сейчас повторное исчерпание лимита снова даст те же терминалы, а не паузу.

---

## Приложение: доказательная база

- **Сообщение о лимите:** 171 вхождение `"You've hit your session limit · resets 6:30am (Europe/Warsaw)"` в `result.json` прогона.
- **Пример (ch13 `product_accuracy`):** `final_message` = лимит, `usage.output_tokens: 0`, `error.failure_subtype: "success"`, `structured_output: null`.
- **Пример (ch09 `constraints`):** `check_journey` → `outcome: fail`, нарушение `page '9.5.2': AI antithesis pattern "не …, а …"`; сопутствующий `fixing` → лимит, `output_tokens: 0`.
- **ch07 таймлайн:** узлы `succeeded` до 02:55:22, затем `task_failure` с 02:57:35 (точка исчерпания квоты).
- **Провайдеры:** `provider_used` claude 140×, codex 0×; ветка `worc/1783624809-rework-ch01-...`, `branch_mode: current`, `create_pull_request: false`.
- **Артефакты:** `wastime-app-content/.worc/logs/<task>/{failure_report.json, summary.json, current.diff, prompt-audit/timeline.jsonl, stages/**/result.json}`; статусы — `wastime-app-content/.worc/state.db` (таблица `tasks`).
