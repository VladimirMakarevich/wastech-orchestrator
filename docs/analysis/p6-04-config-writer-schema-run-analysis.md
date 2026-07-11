# Пост-мортем прогона: `p6-04-config-writer-schema` (фаза 6, задача 4)

**STATUS:** `manual_action_required` — ПЕРВЫЙ терминал НЕ-`done` за фазу 6. Узел `review`, петля `review_fix`, `limit_exhausted=max_fix_cycles`, `fix_iterations=15` (потолок), `finished_at=2026-07-09T22:55:15Z`. Ветка `feat/p6-init` (`branch_mode: existing`), **PR не создан** (`pr_url=null`). Фоновый `worc run` вернул exit 2 — это НЕ «FAILED», итог подтверждён через `worc status` + ledger.

## Короткий вывод и главный рычаг

Имплементация задачи прошла успешно (реальная фича: `config-writer.ts` + 11 файлов, 929 вставок, локально `npm test` 428 passed). Прогон убила НЕ логика задачи, а инфраструктурная мисклассификация: на третьем фикс-цикле claude-подписка упёрлась в **пятичасовой session-limit (HTTP 429 / `out_of_credits`)**, оркестратор принял это за «агент не справился с задачей» (`task_failure`), и петля `review_fix` продолжила крутиться с fixing-узлом, ставшим мгновенным no-op'ом. Итог — **12 «мёртвых» фикс-циклов** дожгли потолок `max_fix_cycles=15`, при этом codex-review честно отрабатывал каждый из них по 4-7 минут (14.4M input сожжено).

**Главный рычаг (P0):** [`parse_stream_json` в `providers/claude.py:366-390`](../../src/wastech_orchestrator/providers/claude.py#L366-L390) должен распознавать rate-limit из stdout (`api_error_status:429` / `rate_limit_event`), а не только из stderr-сигнатуры → отдавать `ErrorClass.RATE_LIMITED`. Это разблокирует уже существующие ветки fallback→codex и infra-park. **Это точь-в-точь P0 F1 из AUDIT-content-rework-run-2026-07-10.md — предложено, но НЕ построено; теперь есть вторая независимая репродукция вне контент-флоу.**

## Как прошёл прогон (хронология)

Восстановлено из `state.db` (`node_runs`, `provider_attempts`) и per-node артефактов.

| Время (UTC) | Узел / run | Итог | Что произошло |
| --- | --- | --- | --- |
| 21:2x | planning#62, implementation#63 | done | Реальная сборка фичи; `implementation.out.md`: `config-writer.ts` + CLI init-write, 428 тестов green локально |
| 21:23–21:26 | review#65 (codex) | rework | Blocking-findings |
| 21:27–21:31 | **fixing#66 (claude)** | **succeeded** | Реальная правка, 322.7K events (~4 мин) |
| 21:32–21:39 | review#68 (codex) | rework | Blocking-findings |
| 21:40–21:47 | **fixing#69 (claude)** | **succeeded** | Реальная правка (~7 мин) — последний реальный фикс |
| 21:47–21:53 | review#71 (codex) | rework | Blocking-findings |
| **21:54:16–21:54:18** | **fixing#72 (claude)** | **failed ~2с** | **⚡ SESSION-LIMIT (429/`five_hour`/`out_of_credits`)** — 0 токенов, баннер «You've hit your session limit» |
| 21:54–22:55 | review#74…#107 ↔ fixing#75…#105 | rework ↔ failed | **12 циклов**: codex-review (4-7 мин) переоткрывает те же findings, claude-fixing мгновенно падает (2-3с) |
| 22:55:15 | — | `manual_action_required` | Потолок `max_fix_cycles=15` дожжён → `failure_report.json`/`stuck.md` |

Reset-время лимита (`resetsAt=1783639800` ≈ 23:30Z) наступало ПОЗЖЕ конца прогона (22:55Z) — восстановления в рамках прогона быть не могло.

**Побочные эффекты на target (проверено).** Работа p6-04 осталась **staged, но не закоммичена** в рабочем дереве `feat/p6-init` (12 файлов, 929 вставок); `terminal_cleanup=blocked` (`last_error="working tree has unaccounted changes: ..."`); task-файл остался в `tasks/pending/`; HEAD ветки на p6-03 (`788e9f2`), PR #12 не тронут. Всё это ожидаемо для `manual_action_required` (оркестратор не коммитит непроверенную работу), но **грязное дерево заблокирует p6-05, пока оператор не разрешит его вручную**.

## Находки по влиянию

### P0 — F48 · session-limit классифицируется как `task_failure` вместо `RATE_LIMITED` (рецидив content-rework F1)

- **Доказательство.** `stages/fixing/run-000105/1-claude/result.json`: `error_class="task_failure"`, `failure_subtype="success"`, `output_tokens=0`; `stdout.log`: `rate_limit_event {status:"rejected", rateLimitType:"five_hour", overageDisabledReason:"out_of_credits"}` + `result {subtype:"success", is_error:true, api_error_status:429}`. **`stderr.log` = 0 байт.**
- **Корень.** claude-адаптер ловит rate-limit только stderr-регексом ([claude.py:102-103](../../src/wastech_orchestrator/providers/claude.py#L102-L103)), а CLI отдаёт лимит структурно в stdout. [`parse_stream_json` (claude.py:366-390)](../../src/wastech_orchestrator/providers/claude.py#L366-L390) вычисляет `succeeded=False, failure_subtype="success"` и **игнорирует `api_error_status`/`rate_limit_event`** → generic quality-`task_failure`. `RATE_LIMITED` в [`FALLBACK_ELIGIBLE` (base.py:66-79)](../../src/wastech_orchestrator/providers/base.py#L66-L79), `task_failure` — нет.
- **Рычаг / зона.** `providers/claude.py` (orchestrator, все репо): распознавать 429/`rate_limit_event` в stdout → `RATE_LIMITED`.
- **Влияние.** Любой claude-узел, поймавший session-limit подписки, помечается как провал качества → нет fallback, нет park, петля крутится дальше. См. [TEST-FINDINGS.md#F48].

### P0 — F49 · провалившийся `fixing` протекает как `done` и дожигает петлю (усилитель F48)

- **Доказательство.** `node_runs`: fixing#72..#105 `status=failed, outcome=done`, каждый ~2-3с; review#74..#107 `succeeded, outcome=rework`. 12 фикс-циклов = 0 работы, но каждый засчитан петлёй.
- **Корень.** agent-узел поднимает `NodeInfraError`→park **только** при `outcome.result is None` ([agent.py:333-337](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L333-L337)); session-limit вернул НЕ-None result → инфра-park не взведён; [`_agent_outcome` (agent.py:694-703)](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L694-L703) безусловно мапит в `done`; review находит blocking → [`_charge_rework` (engine.py:282-299)](../../src/wastech_orchestrator/core/flow/engine.py#L282-L299) ×15.
- **Рычаг / зона.** Первично — через F48 (переклассификация включает fallback+park). Прямо — `core/flow/nodes/agent.py`: терминально-провалившийся fix-узел не отдаёт productive `done`. Это P0 F5/F6 content-rework (park + пауза очереди) — предложено, не построено.
- **Влияние.** Одна упершаяся попытка fix → детерминированный прожёг всех оставшихся фикс-циклов с полноценными codex-review на каждом. См. [TEST-FINDINGS.md#F49].

### P2 — F50 · `failure_report.json`/`stuck.md` прячут реальную причину

- **Доказательство.** `stuck.md`: «Last blocking review findings: (none)», «Final diff: (пусто)». Реально: review#107 вернул 3 findings (2 blocking), staged diff = 929 вставок.
- **Корень.** [`FlowRecorder.write_failure_report` (recorder.py:48-70)](../../src/wastech_orchestrator/core/flow/recorder.py#L48-L70) хардкодит `last_review_findings=None, final_diff=""`, хотя [`ledger.write_failure_report` (ledger.py:147-215)](../../src/wastech_orchestrator/ledger.py#L147-L215) принимает их. Данные есть в `evaluations.findings_json` и рабочем дереве.
- **Рычаг / зона.** `core/flow/recorder.py` (orchestrator, DX). Родственник F45.
- **Влияние.** Разбор терминала требует ручного восстановления findings/diff (как в этом пост-мортеме). См. [TEST-FINDINGS.md#F50].

### P1 (континуитет) — F42 рецидив: review/fix-петля не сходится на judgment-call

Даже 2 РЕАЛЬНЫХ фикс-цикла (66, 69) не сошлись. Горячая точка — CI-workflow template в `config-writer.ts`: имплементатор **явно пометил его как judgment-call** («shipped self-contained npm form; let me know if you'd prefer the `uses:` placeholder» — `implementation.out.md`), фикс-цикл перекрутил его в `uses: VladimirMakarevich/wastech-mdlint@v1` (проверено — текущий staged файл содержит именно `uses:`), а review#107 требует вернуть self-contained (scope drift на P9.03). Классический флип-флоп на спорной точке, которую стоило вынести на HITL-решение, а не гонять по review-блокировке. Session-limit сделал это терминальным, но незакрытие видно и в «здоровых» циклах. Рычаг — тот же, что у F42: глубина/строгость блокирующего review (роль `review`, ceiling `max_fix_cycles`), плюс дизайн-вопрос «спорные design-развилки → HITL, не rework».

## Пробелы в данных

- **`prompt_audit`** — каталог `prompt-audit/` присутствует, но детально не разбирался (не требовалось: причина — инфра, не промпт). Для промпт-находок F42 следующий шаг — сверить `stages/review/rendered-prompt.md` с тем, как роль формулирует «blocking».
- **Точная граница сходимости review** неизвестна: session-limit ударил на 3-м фикс-цикле, поэтому нельзя сказать, сошлась бы петля к 4-5 циклу (как p6-03) или нет. F42-рецидив здесь — MEDIUM-уверенность именно поэтому.
- **F50 замаскировал** штатный источник (`failure_report`), из-за чего весь диагноз восстановлен из per-node `result.json` + `state.db` вручную.

## Что хорошо (проверено — работает)

- **Истинный итог отделён от exit-code.** exit 2 фонового `worc run` корректно разошёлся с реальным `manual_action_required` — `status`+ledger дали верную картину.
- **Terminal cleanup безопасен.** При исчерпании петли оркестратор НЕ закоммитил непроверенную работу, не тронул PR/HEAD, оставил task-файл в pending и честно доложил «unaccounted changes».
- **Имплементация и planning отработали чисто** (0 фоллбэков/крашей на этих узлах; фича собрана, локальные тесты 428 green).
- **supervisor остаётся дёшев** на claude/opus-4-8/medium: 46 вызовов / 2 786 input даже на 29-узловой петле — F46/F47 подтверждены 4-й раз, baseline держится.
- **codex-review стабилен как провайдер** — не rate-limited, отрабатывал каждый цикл, находки предметные и воспроизводимые.

## План действий

**P0 (до перезапуска фазы; блокируют корректную обработку rate-limit во ВСЕХ прогонах):**

1. **F48** — `providers/claude.py`: распознавать session-limit из stdout (`api_error_status==429` / `rate_limit_event.status=="rejected"`) → `ErrorClass.RATE_LIMITED`. Keystone: чинит корень, включает существующий fallback+defer.
2. **F49 / F5-F6** — при `RATE_LIMITED`: fallback на второго провайдера узла, а при исчерпании — park + пауза очереди до `resetsAt` (не крутить петлю на no-op fixing).

**P1:**

3. **F42** — пересмотреть глубину блокирующего review + правило «спорная design-развилка → HITL-вопрос, а не бесконечный rework» (роль `review` + `max_fix_cycles`). Проверять на следующих задачах, сходится ли петля без session-limit.

**P2:**

4. **F50** — `core/flow/recorder.py`: писать в `failure_report.json`/`stuck.md` реальные последние findings (`evaluations`) и diff.

**Оперативно (target, решение оператора, не тест-дорожка):**

5. Разрешить грязное staged-дерево `feat/p6-init` (закоммитить/откатить/довести p6-04 вручную) ДО запуска p6-05 — иначе следующая задача стартует на непроверенных изменениях.
6. Развилка по самому p6-04: перезапускать (после сброса лимита + фикса F48/F49, не вслепую) или сначала чинить оркестратор.

## Сводная таблица находок

| ID | Категория | Severity | Уверен. | Зона | Рычаг (file:line) | Статус |
| --- | --- | --- | --- | --- | --- | --- |
| F48 | infra / provider | P0 | HIGH | orchestrator | `providers/claude.py:366-390` | OPEN (рецидив content-rework F1) |
| F49 | flow / infra | P0 | HIGH | orchestrator | `core/flow/nodes/agent.py:333-337,694-703`; router fallback | OPEN (усилитель F48; F5/F6) |
| F50 | DX / диагностика | P2 | HIGH | orchestrator | `core/flow/recorder.py:48-70` | OPEN (родственник F45) |
| F42 | prompt / flow | P1 | MEDIUM | orchestrator | роль `review` + `max_fix_cycles` | OPEN (рецидив) |
| F46/F47 | cost / provider | — | HIGH | target-config | supervisor `claude/medium` | подтверждены (континуитет) |
