# Анализ токенов `blog-review-happy-in-my-misfortunes-4`

- **Целевой прогон:** `C:\Users\Vladimir Makarevich\Obsidian\WastimeApp\.worc\logs\blog-review-happy-in-my-misfortunes-4`
- **Окно выполнения:** 2026-07-15 17:48:21–18:00:53 UTC
- **Флоу:** `blog_article_revise`
- **Оркестратор при анализе:** commit `53ade3c` от 2026-07-16
- **Codex CLI в исходном rollout:** `0.144.4`
- **Результат задачи:** успешно, без retry и fallback; Codex изменил четыре места на `revise`, затем одно слово на `polish`
- **Метод:** `request.json`, `result.json`, `events.jsonl`, prompt audit, исходный локальный Codex rollout с промежуточными `token_count`, активный flow и текущий код провайдеров

---

## Краткий вердикт

Ощущение, что Codex съел аномально много токенов, возникло сразу по двум причинам. Первая относится к измерению. При `codex exec resume` поле `turn.completed.usage` содержит накопительный счётчик всей Codex-сессии. Оркестратор сохраняет его как расход отдельного node-run. Поэтому `polish/result.json` показывает `282 699 input tokens`, хотя первые `141 464` уже были учтены на `revise`. Простое сложение двух `result.json` даёт `424 163`, завышая реальный расход Codex на 50 %.

После исправления арифметики Codex потратил **282 699 input tokens за обе стадии вместе**, из них **187 904 cached** и **94 795 uncached**. Выход составил **9 364 tokens**, включая **6 066 reasoning tokens**. Это всё равно много для четырёх редакторских правок и одной замены `became → was`.

Вторая причина уже реальная. `polish` объявлен с `lineage_affinity: revise`, поэтому запускается через `codex exec resume` и наследует всю историю `revise`, включая правила, две статьи, task, scout brief, research, три копии редактируемой статьи и diff. Четыре модельных вызова `polish` заново прогнали контекст размером 31–37 тыс. tokens каждый. Реальный прирост этой стадии составил **141 235 input tokens**, хотя финальная правка заняла одно слово.

Сравнение с Claude в текущем виде некорректно. Codex кладёт cached tokens внутрь `input_tokens` и дополнительно сообщает их subset в `cached_input_tokens`. Claude CLI делит ввод на три непересекающихся поля. Его полный ввод равен `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. В этом прогоне у Claude видны всего **35 `input_tokens`**, но ещё **122 738 cache creation** и **560 305 cache read**. Полный объём Claude составил **683 078 input tokens**, то есть в 2,42 раза больше исправленного Codex-total по всей задаче. На три рабочие Claude-ноды без supervisor пришлось 202 785, что меньше Codex на 39 %, но это разные роли и разное число модельных ходов.

Самые сильные рычаги выглядят так.

1. Исправить учёт накопительного Codex usage. Это уберёт 33,3 % из уже отображаемого Codex-total, не меняя реальное потребление.
2. Не продолжать `revise`-сессию в терминальной ноде `polish`. Свежая компактная сессия должна сэкономить ориентировочно 60–80 тыс. input tokens на таком проходе. Если запускать `polish` только при реальной необходимости, верхняя граница экономии равна всему фактическому приросту стадии — 141 235 input и 1 035 output tokens.
3. Научить `context` машинно возвращать `needs_research`. Здесь исследователь пришёл к выводу, что искать ничего не нужно, но его запуск вместе со следующим supervisor-observation уже потратил 80 430 Claude input tokens и $0.1670.
4. Сократить cadence supervisor. Семь advisory-вызовов составили 480 293 Claude input tokens, 70,3 % всего Claude input. Один observation после детерминированного `length` обошёлся в 44 107 input tokens ради ответа на 175 output tokens.
5. Убедиться, что будущие задачи запускаются текущим hardened-адаптером Codex. Анализируемый run произошёл до commit `687e2ff` и не имел `--strict-config`, `--ignore-user-config` и явного отключения plugins/MCP/hooks. Текущий код это уже исправляет, но установленный runtime нужно проверить по следующему `request.json`.

---

## 1. Как читать метрики двух CLI

### Codex

В Codex CLI поле `input_tokens` является полным input за модельные запросы, а `cached_input_tokens` входит в него как subset. Полезные формулы для этого прогона выглядят так.

```text
codex_uncached_input = input_tokens - cached_input_tokens
codex_non_reasoning_output = output_tokens - reasoning_output_tokens
```

OpenAI описывает ту же структуру на уровне API. `prompt_tokens` содержит весь ввод, а `cached_tokens` находится внутри details и показывает, какая часть была прочитана из cache. Кэширование применяется автоматически к достаточно длинным одинаковым prefix и снижает цену/latency, хотя cached tokens всё равно участвуют в rate limits. См. [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching).

### Claude

В Claude Code CLI категории разделены. Для сравнения полного контекста нужно складывать три поля.

```text
claude_total_input = input_tokens
                   + cache_creation_input_tokens
                   + cache_read_input_tokens
```

Поэтому `Claude input_tokens = 35` в этой задаче не означает, что Claude обработал 35 tokens. Это только небольшой хвост, не попавший в cache-write или cache-read.

### Почему старое сравнение вводит в заблуждение

Если сложить Codex `result.json` буквально и сравнить с одним полем Claude, получится `424 163 против 35`, то есть разница больше 12 тысяч раз. Здесь одновременно смешаны две ошибки.

- Codex-total дважды включает `revise` из-за cumulative snapshot на `polish`.
- Claude-total теряет 683 043 tokens из cache creation/read.

После нормализации получается другая картина.

| Срез | Codex | Claude | Комментарий |
| --- | ---: | ---: | --- |
| Буквальная сумма `result.usage.input_tokens` | 424 163 | 35 | Некорректно с обеих сторон |
| Исправленный полный input | 282 699 | 683 078 | Claude включает 10 запусков, Codex — 2 |
| Cache-read / cached subset | 187 904 | 560 305 | У Codex subset, у Claude отдельная категория |
| Uncached / cache-created input | 94 795 | 122 773 | Tokenizer и cache semantics провайдеров различаются |
| Output | 9 364 | 33 337 | Claude не выделяет reasoning отдельным полем в этих artifacts |
| Рабочие ноды без supervisor | 282 699 | 202 785 | Codex: revise+polish; Claude: context+research+tone_style |

Суммировать tokens разных tokenizer допустимо только как операционный ориентир. Это не бухгалтерская стоимость. Claude CLI сохранил фактическую стоимость своих десяти запусков — **$1.8364949**. Codex работал через ChatGPT subscription и денежную стоимость в artifacts не записал.

---

## 2. Исправленный отчёт Codex

### 2.1. Что записано в node artifacts

| Нода | Режим | Model / effort | `input_tokens` | `cached_input_tokens` | `output_tokens` | `reasoning_output_tokens` |
| --- | --- | --- | ---: | ---: | ---: | ---: |
| `revise` | fresh | `gpt-5.4` / `xhigh` | 141 464 | 76 288 | 8 329 | 5 935 |
| `polish` | resume `revise` | `gpt-5.4` / `medium` | 282 699 | 187 904 | 9 364 | 6 066 |
| **Наивная сумма** | — | — | **424 163** | **264 192** | **17 693** | **12 001** |

`polish` здесь является вторым cumulative snapshot той же сессии. У обеих нод совпадает нормализованный `session_id`, а исходный rollout показывает монотонный `total_token_usage` от первого модельного запроса до последнего.

### 2.2. Правильная дельта

| Нода | Input | Cached | Uncached | Output | Reasoning | Output без reasoning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `revise` | 141 464 | 76 288 | 65 176 | 8 329 | 5 935 | 2 394 |
| `polish`, реальная дельта | 141 235 | 111 616 | 29 619 | 1 035 | 131 | 904 |
| **Codex session total** | **282 699** | **187 904** | **94 795** | **9 364** | **6 066** | **3 298** |

Дельта `polish` получена вычитанием snapshot после `revise` из финального snapshot сессии.

```text
polish.input     = 282 699 - 141 464 = 141 235
polish.cached    = 187 904 -  76 288 = 111 616
polish.output    =   9 364 -   8 329 =   1 035
polish.reasoning =   6 066 -   5 935 =     131
```

Следовательно, параметр `model_reasoning_effort="medium"` на `polish` сработал. Видимые в `result.json` 6 066 reasoning tokens почти целиком пришли из предыдущего `xhigh revise`; собственная дельта `polish` равна 131.

### 2.3. Каждый модельный запрос внутри Codex-сессии

Исходный rollout содержит `last_token_usage`, которого нет в task-level `events.jsonl`. Он позволяет увидеть реальную механику расхода.

| Нода | Модельный ход | Input | Cached | Uncached | Output | Reasoning |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| revise | 1 | 11 841 | 0 | 11 841 | 897 | 392 |
| revise | 2 | 16 944 | 11 264 | 5 680 | 568 | 94 |
| revise | 3 | 23 546 | 16 384 | 7 162 | 847 | 564 |
| revise | 4 | 25 854 | 0 | 25 854 | 4 459 | 3 715 |
| revise | 5 | 30 367 | 25 600 | 4 767 | 332 | 136 |
| revise | 6 | 32 912 | 23 040 | 9 872 | 1 226 | 1 034 |
| **revise total** | **6** | **141 464** | **76 288** | **65 176** | **8 329** | **5 935** |
| polish | 1 | 31 436 | 8 576 | 22 860 | 449 | 31 |
| polish | 2 | 35 962 | 31 104 | 4 858 | 335 | 100 |
| polish | 3 | 36 351 | 35 712 | 639 | 109 | 0 |
| polish | 4 | 37 486 | 36 224 | 1 262 | 142 | 0 |
| **polish delta** | **4** | **141 235** | **111 616** | **29 619** | **1 035** | **131** |

У `revise` cache hit составил 53,9 %, у `polish` — 79,0 %, по всей сессии — 66,5 %. Первый ход после resume оказался особенно дорогим. Из 31 436 input только 8 576 пришли из cache, поэтому 22 860 tokens были обработаны как uncached. Следующие ходы уже почти полностью попадали в cache.

### 2.4. Что делал Codex

| Нода | Модельных ходов | Shell-команд | Вывод команд | Agent messages | File changes |
| --- | ---: | ---: | ---: | ---: | ---: |
| `revise` | 6 | 15 | 56 123 chars | 7 | 1 patch |
| `polish` | 4 | 6 | 20 794 chars | 4 | 1 patch |

Параллельные чтения файлов не создавали отдельный модельный запрос на каждую команду. Однако весь вывод команд оставался в transcript и входил в следующие модельные ходы.

На `revise` редактируемая статья попала в transcript трижды — обычное чтение 5 279 chars, версия с номерами строк 5 718 chars и полное чтение после patch 5 361 chars. Затем туда же добавился diff на 4 122 chars. Нода также перечитала task, scout brief, research, product idea, README, пять rules-файлов и voice-reference article.

На `polish` были прочитаны task, текущая статья, текущий diff, `findings.json`, signature phrases и финальный diff. Tone evaluator уже дал точную замену `once I became the one walking it → once I was the one walking it`, поэтому значительная часть этого чтения не повлияла на решение.

### 2.5. Стартовый overhead Codex CLI

Первый модельный ход `revise` уже имел 11 841 input tokens до появления tool outputs. При этом rendered prompt ноды занимает 345 слов и 2 506 bytes. В историческом session metadata базовые Codex instructions занимают 2 292 слова и 14 732 chars, world state — ещё 4 235 JSON chars. Сюда добавляются AGENTS, tool schemas и служебные сообщения.

Значит маленький role prompt не превращается в маленький первый request. Для этого запуска базовый prefill порядка 11–12 тыс. tokens является ценой Codex CLI как полноценного coding agent. Настройки timeout, sandbox и approval не объясняют аномалию.

---

## 3. Полный отчёт Claude

В таблице `Total input` уже рассчитан по трём Claude-полям. `Supervisor after X` обозначает advisory-вызов после соответствующей ноды.

| Нода | Model / effort | Direct input | Cache create | Cache read | Total input | Output | Cost, USD |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| context | `claude-opus-4-8` / xhigh | 5 | 21 611 | 61 221 | 82 837 | 10 580 | 0.5127 |
| supervisor after context | `claude-sonnet-5` / high | 2 | 13 444 | 20 769 | 34 215 | 1 604 | 0.1137 |
| research | `claude-opus-4-8` / xhigh | 4 | 8 127 | 34 408 | 42 539 | 1 279 | 0.1315 |
| supervisor after research | `claude-sonnet-5` / high | 2 | 3 676 | 34 213 | 37 891 | 215 | 0.0356 |
| supervisor after revise | `claude-sonnet-5` / high | 6 | 21 646 | 101 205 | 122 857 | 1 924 | 0.1891 |
| supervisor after length | `claude-sonnet-5` / high | 2 | 1 690 | 42 415 | 44 107 | 175 | 0.0255 |
| tone_style | `claude-opus-4-8` / xhigh | 6 | 17 215 | 60 188 | 77 409 | 8 772 | 0.4229 |
| supervisor after tone_style | `claude-sonnet-5` / high | 1 | 352 | 44 105 | 44 458 | 527 | 0.0233 |
| supervisor after polish | `claude-sonnet-5` / high | 3 | 2 379 | 89 816 | 92 198 | 615 | 0.0505 |
| finalize supervisor | `claude-sonnet-5` / high | 4 | 32 598 | 71 965 | 104 567 | 7 646 | 0.3319 |
| **Итого Claude** | **10 запусков** | **35** | **122 738** | **560 305** | **683 078** | **33 337** | **1.8365** |

Разбивка по назначению показывает настоящий центр расходов Claude.

| Группа | Runs | Total input | Cache read | Output | Cost, USD |
| --- | ---: | ---: | ---: | ---: | ---: |
| context + research + tone_style | 3 | 202 785 | 155 817 | 20 631 | 1.0671 |
| supervisor | 7 | 480 293 | 404 488 | 12 706 | 0.7694 |

Supervisor получил 70,3 % всего Claude input. Он advisory-only и не влияет на route. Особенно мало окупились observations после `research`, который вернул «ничего искать не нужно», и после детерминированного `length`.

Большие `output_tokens` у `context` и `tone_style` также не равны размеру видимого финального текста. При extended thinking CLI учитывает внутреннюю генерацию в output usage, хотя отдельное поле reasoning в сохранённом Claude result отсутствует. `tone_style` вернул один low finding, но потратил 8 772 output tokens на Opus/xhigh.

---

## 4. Корневые причины

### F1. Codex cumulative usage ошибочно трактуется как per-run usage

**Severity:** high для аналитики и лимитов. **Confidence:** high.

[Codex parser](../../src/wastech_orchestrator/providers/codex.py) прямо копирует `event["usage"]` из terminal `turn.completed` в `AgentRunResult.usage`. В модели данных отсутствует признак `usage_scope`, нет baseline предыдущего snapshot и нет теста на два `turn.completed` одной resumed-сессии.

Свежий Codex run не показывает проблему, поскольку session cumulative совпадает с run usage. На resume второй snapshot включает первый. Любая аналитика, которая суммирует `result.json` по нодам, повторно считает все предыдущие turns durable-сессии. На длинной lineage ошибка растёт как сумма накопительных snapshot.

В этой задаче завышение точно равно первой стадии.

| Метрика | Наивный total | Правильный total | Завышение |
| --- | ---: | ---: | ---: |
| Input | 424 163 | 282 699 | 141 464 |
| Cached input | 264 192 | 187 904 | 76 288 |
| Output | 17 693 | 9 364 | 8 329 |
| Reasoning | 12 001 | 6 066 | 5 935 |

Направление старой находки `TEST-FINDINGS F47` про рост Codex transcript остаётся верным. Её количественное сравнение `Codex input_tokens` с маленьким Claude `input_tokens` нужно пересчитать. Там смешаны cumulative Codex snapshots и Claude direct-input без cache fields.

### F2. `polish` продолжает тяжёлую редакторскую lineage без практической необходимости

**Severity:** high для raw token usage. **Confidence:** high.

В `WastimeApp/.worc/flows/blog_article_revise.yaml` нода `polish` объявлена так.

```yaml
session_scope: editing_lineage
lineage_affinity: revise
provider: codex
model: gpt-5.4
reasoning: medium
```

[Agent runner](../../src/wastech_orchestrator/core/flow/nodes/agent.py) получает persisted session `revise`, после чего adapter штатно строит `codex exec resume <SESSION_ID>`. Синтаксис запуска правильный и соответствует [Codex CLI reference](https://developers.openai.com/codex/cli/reference/). Ошибка находится на уровне flow-семантики. Финальный независимый редактор унаследовал author transcript, хотя уже получил diff и reviewer findings как artifacts.

Накопленная история увеличила каждый из четырёх вызовов `polish` до 31–37 тыс. input tokens. Cache смягчил 79 % объёма, но raw usage и rate-limit pressure сохранились.

> **Documentation follow-up.** В документации по session scopes стоит явно предупредить пользователя, что `resume`, `editing_lineage` и `lineage_affinity` передают историю общей сессии следующим стадиям. Это сохраняет контекст, но увеличивает input token usage на каждом последующем модельном ходе. Общую сессию следует использовать осознанно, особенно между семантически разными стадиями, и заранее учитывать рост cumulative/cached tokens.

### F3. Агент повторно читает полные документы, а transcript растёт после каждого tool round

**Severity:** medium-high. **Confidence:** high.

`revise` прочитал 56 123 chars shell output и сделал шесть модельных ходов. Полная статья вошла трижды. `polish` прочитал 20 794 chars и сделал четыре хода ради точечной замены. Каждый следующий ход включает предыдущие tool calls и outputs, поэтому лишнее чтение оплачивается несколько раз.

Корневой `AGENTS.md` требует читать product idea и rules перед задачей. Это разумно для качества, но каждая orchestrator node воспринимается CLI как отдельная задача. Возобновление сессии избавляет от повторного чтения файлов, одновременно тащит гораздо более широкий transcript. Для терминального `polish` компактный handoff обычно дешевле полной lineage.

### F4. Исторический запуск Codex ещё не имел controlled invocation boundary

**Severity:** medium, влияние на tokens требует A/B. **Confidence:** high по факту запуска, medium по величине эффекта.

В `request.json` анализируемых нод были `--ask-for-approval never`, `exec`, sandbox, JSON output, disabled web search, model и reasoning. Там отсутствовали `--strict-config`, `--ignore-user-config` и список `--disable` для apps/plugins/MCP/hooks. Исторический session metadata подтверждает, что была применена пользовательская personality `pragmatic`. Нельзя точно восстановить, какой объём tool schemas пришёл из пользовательских plugins на момент запуска.

Commit `687e2ff` от 2026-07-16 добавил controlled invocation после этой задачи. Текущий [Codex provider](../../src/wastech_orchestrator/providers/codex.py) игнорирует user config, помечает project config untrusted и отключает внешние capabilities без typed grant. Следующий реальный run должен иметь эти флаги в `request.argv` и `capabilities.json` рядом с provider artifacts.

### F5. `xhigh` на `revise` и на простых Claude-оценках завышает output usage

**Severity:** medium. **Confidence:** high по usage, medium по безопасному уровню снижения.

На `revise` reasoning занял 5 935 из 8 329 output tokens, то есть 71,3 %. На `polish@medium` reasoning delta равна 131 из 1 035, поэтому medium уже работает ожидаемо.

`context` и `tone_style` наследуют `claude-opus-4-8/xhigh`. Первая нода подготовила редакторский brief, вторая вернула один low finding. Вместе они потратили 19 352 output tokens и $0.9356. Для статьи такого размера Sonnet/high или Opus/medium стоит проверить через A/B quality rubric.

### F6. Необязательные flow-ноды и supervisor cadence доминируют в полном бюджете

**Severity:** high для полной стоимости pipeline. **Confidence:** high.

Scout прямо написал, что внешнее исследование не требуется. Flow всё равно запустил `research`, потому что решение не является machine-readable branch outcome. Сам исследователь потратил 42 539 input, следующий supervisor — ещё 37 891.

Supervisor запускается после каждого non-skipped шага и затем ещё раз на finalize. Семь вызовов потребили 480 293 input tokens. Cache-read достиг 404 488, поэтому денежный эффект меньше raw объёма, но cadence остаётся главным источником Claude tokens.

### Что причиной не было

- Retry и fallback отсутствовали. Все provider attempts завершились с первой попытки.
- Auto-compaction не срабатывал. Максимальный единичный Codex context был 37 486 при model context window 258 400.
- `timeout_seconds: 7200` не влияет на token budget.
- Web search у writing nodes был отключён, сетевых запросов не было.
- `extra_args` и `config_extra_args` были пустыми.
- Параметр `medium` на `polish` передан корректно и дал всего 131 новых reasoning tokens.
- Rendered prompts компактны — 345 слов на `revise` и 252 на `polish`. Главный объём пришёл из agent runtime, tool outputs и session history.

---

## 5. Рекомендации

### P0. Нормализовать usage и перестать суммировать cumulative snapshots

**Зона:** orchestrator.

Нужен явный контракт usage вместо свободного `dict[str, Any]` без семантики. Минимальный безопасный вариант должен хранить два представления.

- `provider_usage_raw` сохраняет terminal payload без изменений для аудита.
- `normalized_usage` содержит `scope`, `input_total`, `cache_read`, `cache_write`, `uncached_input`, `output_total`, `reasoning_output`, `cost`.
- Для Codex fresh run scope можно считать `session_cumulative`, где baseline равен нулю.
- Для Codex resume runner вычитает последний snapshot этой же provider session. Baseline следует хранить рядом с durable lineage в SQLite, не в логах с raw session id.
- Если новый snapshot меньше baseline из-за reset/compaction/version drift, система сохраняет raw значения, помечает delta unknown и пишет warning вместо отрицательных tokens.
- Claude usage нормализуется как per-invocation с суммой трёх input categories.

Тест должен воспроизводить эту задачу.

```text
fresh turn.completed:  input=141464, cached=76288, output=8329, reasoning=5935
resume turn.completed: input=282699, cached=187904, output=9364, reasoning=6066

expected resume delta: input=141235, cached=111616, output=1035, reasoning=131
expected session total: latest snapshot only, input=282699
```

До реализации агрегатор отчётов может временно группировать Codex artifacts по нормализованному `session_id` и брать последний component-wise cumulative snapshot. Это исправляет whole-session total, но не даёт честный per-node usage при reset, поэтому годится только как переходная мера.

### P0. Разорвать lineage между `revise` и `polish`

**Зона:** target flow Wastime.

Рекомендуемая базовая конфигурация для независимого финального редактора выглядит так.

```yaml
- id: polish
  kind: agent
  session_scope: fresh_disposable
  permission_profile: workspace-write
  provider: codex
  model: gpt-5.4
  reasoning: medium
```

`lineage_affinity: revise` стоит оставить у `fixing`, где история предыдущих исправлений действительно полезна в цикле. У `polish` нет downstream rework и нет причины владеть долговечной lineage.

По разнице первого запроса fresh revise и первого resumed polish наследуемая часть выглядит как 19–20 тыс. tokens. Она повторяется в четырёх ходах. Консервативная оценка экономии от fresh polish — **60–80 тыс. raw input tokens**. Точное число требует A/B на одной и той же статье, поскольку свежая сессия может перечитать обязательные rules.

Ещё сильнее работает условный запуск. Если `tone_style` вернул пустые findings и `revise` уже прошёл quality gate, `polish` можно пропустить. В текущем run верхняя граница такой экономии равна **141 235 input + 1 035 output tokens и 40,7 секунды**.

### P0. Добавить machine-readable `needs_research`

**Зона:** target flow + flow contract.

`context` должен возвращать structured field вроде `needs_research: true|false` и короткий `research_question`. Edge выбирает `research` только при `true`. Для этой задачи точная экономия составила бы.

| Убираемый вызов | Input | Output | Cost, USD |
| --- | ---: | ---: | ---: |
| research | 42 539 | 1 279 | 0.1315 |
| supervisor after research | 37 891 | 215 | 0.0356 |
| **Итого** | **80 430** | **1 494** | **0.1670** |

Ручное `nodes: { research: { enabled: false } }` уже возможно, но для general quality pass оператор не обязан заранее знать вывод scout. Структурное ветвление надёжнее.

### P1. Уменьшить supervisor cadence и reasoning

**Зона:** orchestrator config/design.

Минимальный шаг — не наблюдать deterministic tool nodes, где нет нового авторского решения. В этой задаче пропуск observation после `length` сохранил бы **44 107 input, 175 output и $0.0255**.

Следующий шаг — свести несколько близких observations в один batch или перейти к artifact-based finalize. Supervisor может получить immutable краткие записи о node outcome, changed paths и findings без тёплой conversational lineage. Полный потенциальный бюджет группы равен 480 293 input и $0.7694, хотя удалять весь слой без quality evaluation не следует.

Текущий `claude-sonnet-5/high` стоит A/B-проверить против `medium`. Advisory summary редко требует high на каждом шаге. Finalize можно оставить сильнее observation calls.

### P1. Снизить число Codex model rounds и объём tool outputs

**Зона:** role prompts.

Для `revise` достаточно одного parallel read batch, одного patch и одной проверки diff. Полное чтение статьи с номерами строк и повторное полное чтение после patch можно заменить адресным `rg`/`sed` и одним diff. Для `polish` reviewer finding и diff уже содержат точное место, поэтому task и полная signature library обычно избыточны.

Полезно закрепить в role prompt операционный бюджет.

```text
Read the supplied brief, target article and findings in one parallel batch.
Do not reread a full file already present in this session.
Apply one focused patch, inspect one final diff, then stop.
```

Это не hard token limit, но уменьшает число tool loops. В текущем `revise` переход с шести модельных ходов к четырём убрал бы два повторных prefill растущего transcript.

### P1. Пересмотреть model/effort для content pipeline

**Зона:** target flow.

Предлагаемый порядок A/B, чтобы не потерять авторский голос.

1. `revise`: `xhigh → high`, модель оставить `gpt-5.4`.
2. `context`: pin `claude-sonnet-5/high` либо `claude-opus-4-8/medium` вместо наследуемого Opus/xhigh.
3. `research`: Sonnet/medium, поскольку задача ноды состоит в сборе конкретного материала и часто заканчивается empty result.
4. `tone_style`: Sonnet/high или Opus/medium; сохранить structured findings и тот же rubric.
5. `polish`: medium оставить. Его реальный reasoning расход уже мал.

Quality gate для A/B должен сравнивать число нужных ручных исправлений, соблюдение signature voice, отсутствие запрещённых паттернов и количество evaluator findings. Считать только tokens недостаточно.

### P1. Проверить, что runtime использует hardened Codex adapter

**Зона:** deployment/operator.

Текущий исходный код появился после анализируемого запуска. После обновления/reinstall в новом `request.json.argv` должны присутствовать как минимум `--strict-config`, `--ignore-user-config` и controlled `--disable` flags. Рядом должен появиться `capabilities.json`, где `user_config=ignored`, project config/rules ограничены политикой, plugins/MCP/hooks выключены.

Если этих признаков нет, команда `worc`, которой запускается задача, использует старую установленную копию, даже если рабочий checkout `wastech-orchestrator` уже обновлён.

### P2. Реализовать Phase 0 из token-optimization backlog

В [token optimization backlog](../backlog/archive/token_optimization.md) уже записана идея persist tokens/cost per attempt. Этот прогон показывает, что одного произвольного `usage` JSON мало. Phase 0 должна включать provider-aware normalization и session scope, иначе baseline будет систематически ошибочным.

Рекомендуемые dimensions для отчётов.

- task, node, run, attempt, provider, model, reasoning;
- session scope и lineage key без raw session id;
- full input, cache read, cache write, uncached input;
- output, reasoning output, tool/model turns;
- cost и wall time;
- cumulative snapshot и normalized delta;
- retries/fallbacks и terminal status.

---

## 6. Приоритетный план реализации

| Приоритет | Изменение | Файл/слой | Эффект на этот run | Риск |
| --- | --- | --- | ---: | --- |
| P0 | Нормализация cumulative Codex usage | provider contract + lineage persistence + tests | Отчётный Codex total `424 163 → 282 699` | Низкий, если raw usage сохраняется |
| P0 | Fresh/conditional `polish` | `WastimeApp/.worc/flows/blog_article_revise.yaml` | Оценка 60–80k input; максимум 141 235 | Средний, проверить rereads и voice |
| P0 | Conditional `research` | context schema + flow edge | 80 430 Claude input, $0.1670 | Низкий при явном флаге |
| P1 | Не наблюдать deterministic `length` | supervisor cadence | 44 107 Claude input, $0.0255 | Низкий |
| P1 | Supervisor medium/batching | config + supervisor design | Часть из 480 293 input, $0.7694 | Средний, нужен quality A/B |
| P1 | Один read batch / один verify | revise/polish role prompts | Меньше 10 model rounds и duplicate context | Низкий |
| P1 | `revise xhigh → high` | target flow | Снижение reasoning из текущих 5 935 | Средний, нужен voice A/B |
| P1 | Pin cheaper Claude roles | target flow | Снижение 19 352 output у context+tone | Средний, нужен evaluator A/B |
| P1 | Deploy current controlled adapter | installation/runtime | Убирает user-config/tool leakage; точный token win требует A/B | Низкий |

Рекомендуемый порядок экспериментов.

1. Сначала починить accounting, иначе A/B снова будет измерен неправильно.
2. Прогнать ту же задачу с единственным изменением `polish → fresh_disposable`.
3. Добавить `needs_research` и проверить, что empty research действительно пропускает ноду и supervisor observation.
4. Убрать supervisor observation после tool nodes.
5. После стабилизации метрик отдельно тестировать reasoning/model, по одному изменению за запуск.

---

## 7. Контрольные критерии следующего прогона

Следующий запуск считается подтверждением оптимизации, если выполняются все условия.

- Codex whole-session total равен последнему cumulative snapshot, а per-node usage хранит delta.
- `polish` получает новый session id или пропускается по условию.
- Первый input `polish` заметно ниже текущих 31 436 либо нода отсутствует.
- В `request.argv` есть current controlled-invocation flags и рядом записан `capabilities.json`.
- Полный Claude input считается суммой direct + cache creation + cache read.
- Supervisor не запускается после deterministic `length` или этот вызов осознанно сохранён с обоснованием.
- Research отсутствует, когда scout возвращает `needs_research=false`.
- Итоговая статья проходит тот же tone/style rubric и не требует дополнительных ручных правок.

Целевой ориентир для аналогичного простого quality pass после P0/P1 — убрать ошибочные 141 464 tokens из отчёта, сократить фактический Codex input хотя бы ниже 220 тыс. и сократить Claude input минимум на 124 тыс. за счёт empty research path и observation после `length`. Это достижимо без смены основной авторской модели и без ослабления quality gate.

---

## 8. Ограничения анализа

- Для Codex доступны точные per-model-call counters из локального rollout. Для Claude task artifacts дают totals и cache categories, но не отдельное поле reasoning.
- Денежная стоимость Codex отсутствует, поскольку CLI работал через subscription. Сравнение в долларах возможно только после отдельной telemetry или API-priced shadow calculation.
- Оценка экономии fresh `polish` основана на размере исторического контекста. Точное число даст только A/B, потому что свежий агент может перечитать обязательные project rules.
- Controlled invocation был добавлен на следующий день после run. Его влияние на tokens этой исторической задачи нельзя измерить задним числом.
- Tokens разных провайдеров используют разные tokenizer и ценовые коэффициенты. Raw totals полезны для rate-limit pressure и относительного анализа внутри провайдера; межпровайдерная стоимость требует нормализации по billing rates.

---

## Источники

- Логи задачи `WastimeApp/.worc/logs/blog-review-happy-in-my-misfortunes-4`.
- Активный flow `WastimeApp/.worc/flows/blog_article_revise.yaml`.
- [Codex provider и parser](../../src/wastech_orchestrator/providers/codex.py).
- [Выбор и сохранение editing lineage](../../src/wastech_orchestrator/core/flow/nodes/agent.py).
- [Flow authoring — session scopes](../flow-authoring.md).
- [Token optimization backlog](../backlog/archive/token_optimization.md).
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference/).
- [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching).
