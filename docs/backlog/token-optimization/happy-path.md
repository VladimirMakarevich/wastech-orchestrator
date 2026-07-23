# Наглядный путь — токены supervisor до и после оптимизации

Companion к [README кампании](README.md) и дорожной карте [P0](supervisor-finalize-packet-and-cadence.md) → [P1](supervisor-observation-cadence-p1.md) → [P2](supervisor-responsibility-split-p2.md). Прослеживает один реальный прогон — `blog-review-happy-in-my-misfortunes-4` (задача под flow `blog_article_revise`) — через слой supervisor: что происходит **сейчас** и что будет **после** реализации всех требований кампании. Цель документа — увидеть глазами, что мы меняем. Цифры взяты из [токен-анализа 2026-07-16](2026-07-16-blog-review-happy-in-my-misfortunes-4-token-analysis.md); исторические (наблюдения) известны точно, целевые — оценки, подтверждаются A/B.

## Пример прогона

| Что | Значение |
| --- | --- |
| Flow | packaged `blog_article_revise` |
| Ноды по порядку | `context` → `research` → `revise` → `length` (tool) → `tone_style` (evaluator, blocking) → `polish` → `publish` |
| Supervisor | Claude, `read-only`, **advisory-only** — не правит код, не останавливает flow, не отправляет на rework, не роутит |
| Что он делает | после каждой завершённой ноды пишет заметку для человека; в конце — общий `summary.md` (тело PR) + memory delta + follow-ups |

## Простыми словами

**Кто такой supervisor.** Это «наблюдатель» над всем flow. Он ничего не может исправить или остановить — только записать заметку и в конце собрать итоговый summary. Качество текста в этом flow держит отдельный блокирующий эвалюатор `tone_style`, а не supervisor.

### Сейчас (ДО)

- После **каждой** ноды (кроме `publish`) запускается **отдельный LLM-вызов** — «посмотреть, что произошло» и записать заметку.
- Все вызовы идут в **одной «тёплой» сессии, которая разрастается**: на каждом следующем шаге заново пересылается вся накопленная история (прошлые заметки, прочитанный diff, вызовы инструментов).
- В конце `finalize` продолжает **ту же разросшуюся сессию**, чтобы написать summary.
- Из-за этого даже короткая заметка «всё ок» стоит десятки тысяч токенов — платим не за текст заметки, а за весь накопленный контекст.

```text
context    ─▶ 🧠 observe   34 215 ток ┐
research   ─▶ 🧠 observe   37 891 ток │  ОДНА «тёплая» сессия,
revise     ─▶ 🧠 observe  122 857 ток │  растёт с каждым шагом —
length     ─▶ 🧠 observe   44 107 ток │  вся история пересылается
tone_style ─▶ 🧠 observe   44 458 ток │  заново на каждом вызове
polish     ─▶ 🧠 observe   92 198 ток ┘
                               │
               🧠 finalize   104 567 ток  ◀── та же разросшаяся сессия
                               ▼
                          summary.md
        ────────────────────────────────────────────
        ИТОГО: 7 LLM-вызовов · 480 293 input-токенов · $0.77 · ~198 c
```

| Вызов | Input, ток | Что реально дал |
| --- | --: | --- |
| after `context` | 34 215 | Повторно подтвердил качество scout brief |
| after `research` | 37 891 | Подтвердил, что research правильно ничего не искал |
| after `revise` | 122 857 | Повторно проверил 4 diff-хунка |
| after `length` | 44 107 | Прокомментировал обычный детерминированный pass |
| after `tone_style` | 44 458 | Отметил, что эвалюатор дал мало пояснений |
| after `polish` | 92 198 | Повторно проверил одну замену `became → was` |
| `finalize` | 104 567 | Итоговый summary + memory delta |

Шесть из семи вызовов — промежуточные наблюдения (**375 726 токенов, $0.44**), которые не изменили исход задачи.

### После (ПОСЛЕ — когда сделаны P0 + P1 + P2)

- **Факты о каждом шаге пишутся детерминированно, без LLM** (StepRecorder → ledger): нода, исход, изменённые пути, diff-fingerprint, checks, вердикт эвалюатора. Это «источник правды».
- **LLM-наблюдение — только когда есть что сказать человеку** (`observation_mode`): для content-flow — `none` (ни одного промежуточного вызова), для implementation — `events` (только `rework`/`failure`/`hitl`/`dangerous_diff`/`fallback`/`subtask_boundary`). Обычные `done`/`pass`/`accept` → только детерминированная запись.
- **`tool`/`checks` вообще не наблюдаются** (P0.3) — `length` перестаёт стоить 44k.
- **`finalize` всегда стартует свежим** и получает компактный `SupervisorPacket` (собран из ledger + diff + findings; передаётся **по пути как frozen-артефакт**, паттерн WRI-011) — он больше не тащит разросшуюся сессию.
- **Раздельные бюджеты**: дешёвая модель + low для заметок, сильнее + medium для summary; жёсткие потолки `max_calls` + `max_digest_tokens` (в реальных токенах).

```text
context    ─▶ 📝 запись (без LLM) ┐
research   ─▶ 📝 запись (без LLM) │  детерминированный ledger
revise     ─▶ 📝 запись (без LLM) │  = источник правды
length     ─▶ (skip — tool)       │  (tool/checks не наблюдаются)
tone_style ─▶ 📝 запись (без LLM) │
polish     ─▶ 📝 запись (без LLM) ┘
                               │
                    ┌──────────────────────┐
                    │   SupervisorPacket    │ ◀── собран из ledger + diff + findings,
                    │  (frozen, по пути)    │     bounded-выжимка, без тёплой сессии
                    └──────────────────────┘
                               │
               🧠 finalize (fresh)  ~20–40k ток  ◀── ОДИН свежий вызов
                               ▼
                 summary.md + follow_ups + memory_delta
        ────────────────────────────────────────────
        ИТОГО: 1 LLM-вызов · < 60 000 input-токенов · ~$0.15–0.30 · заметно быстрее
```

Для **implementation-flow** картина та же, но при событии (`rework`/`HITL`/`fallback`/опасный diff) в нужной точке появляется один 🧠-note — расход растёт вместе с реальными отклонениями, а не с числом обычных шагов.

## Цифры на исследованном прогоне (наглядно)

| Метрика | ДО | ПОСЛЕ (цель) |
| --- | --: | --- |
| Вызовов supervisor | 7 | 1 (content-flow) |
| Промежуточных наблюдений | 6 | 0 |
| **Supervisor input, ток** | **480 293** | **< 60 000** |
| — из них наблюдения | 375 726 | 0 |
| — finalize | 104 567 | ~20 000–40 000 |
| Output, ток | 12 706 | ~8 000 (только finalize) |
| Стоимость | $0.77 | ~$0.15–0.30 (≥ $0.44 снимается на одних наблюдениях) |
| Wall time | ~198 c | заметно меньше (цель < 60 c) |
| Пропущенные blocking-issue | 0 | 0 (их держит `tone_style`) |
| Полнота summary | baseline | не хуже baseline |

## Что НЕ теряется (качество и функциональность)

- **Качество прозы** держит блокирующий эвалюатор `tone_style` — он остаётся; поэтому content-flow можно ставить `none` без риска для качества.
- **`follow_ups` и `memory_delta`** по-прежнему рождаются в том же **одном** finalize-turn (0 дополнительных LLM-вызовов).
- **Полнота summary** проверяется по 4 пунктам — что изменено / почему / какие проверки прошли / какие caveats — и должна быть **не хуже** baseline.
- **Supervisor остаётся read-only и advisory** — контракт «Core решает» не нарушается.
- **Детерминированная запись пишется всегда** (даже при `none`), поэтому ledger и packet полны, а **restart даёт тот же результат, что и обычный прогон**.
- **Implementation-flow берёт `events`, а не `none`**: там `emit_follow_ups: true`, и заметки на реальных отклонениях сохраняют ценность памяти и техдолга.

## Карта требований → эффект

| Фаза | Что добавляет | Наглядный эффект |
| --- | --- | --- |
| [P0](supervisor-finalize-packet-and-cadence.md) | детерминированный `SupervisorPacket` → fresh finalize → skip `tool`/`checks` | finalize перестаёт тащить тёплую сессию; `length` перестаёт стоить 44k |
| [P1](supervisor-observation-cadence-p1.md) | `observation_mode` + event-триггеры + раздельные observe/finalize + бюджеты | убираются все 6 промежуточных наблюдений (исторические 375 726 токенов) |
| [P2](supervisor-responsibility-split-p2.md) | вынос `StepRecorder` + раздельные бюджеты handoff/skill + per-function telemetry | видно, сколько стоила каждая функция; предупреждение, если supervisor снова доминирует |

## Дефолты по типам flow (после P1)

| Тип flow | `observation_mode` | Почему |
| --- | --- | --- |
| content (`blog_article*`) | `none` | качество держит блокирующий `tone_style`; finalize получает всё пакетом |
| implementation | `events` | нужны заметки на `rework`/`HITL`/`fallback`; там включён `emit_follow_ups` |

## Сквозные утверждения (как это проверяется)

- `tool`/`checks` не порождают LLM-вызов supervisor, но задача завершается штатно.
- Обычный (non-revive) `finalize` стартует на fresh-сессии (не получает warm session id); вход — `SupervisorPacket` по пути.
- `SupervisorPacket` идентичен на обычном прогоне и после restart/revive (детерминизм из durable-состояния) → summary воспроизводим.
- `SupervisorPacket` передаётся по пути к frozen-артефакту (context-footer, паттерн WRI-011), а не инлайн-JSON; секреты/сырой diff в промпт не попадают.
- `follow_ups` (когда flow включил `emit_follow_ups`) и `memory_delta` (когда `memory.enabled`) производятся тем же одним finalize-turn.
- `none` не создаёт ни одного наблюдения, но finalize и summary сохраняются; `events` создаёт заметку только на событии, а `done`/`pass`/`accept` — только детерминированную запись.
- Бюджеты `max_calls`/`max_digest_tokens` соблюдаются; при исчерпании observer переключается в deterministic-only, задача не падает.
- Supervisor остаётся read-only и advisory; handoff и skill-proposal работают независимо от `observation_mode`.
- 0 пропущенных blocking-issue — их держит `tone_style`; полнота summary не хуже baseline.
