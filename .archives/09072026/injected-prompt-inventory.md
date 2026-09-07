# Инъектируемый текст промптов — полная опись

Status: **reference** Date: 2026-09-05 Owner: Vladimir Makarevich

Написано по-русски (запрошено оператором). Все инъектируемые тексты приведены дословно в оригинале — они уходят модели именно в таком виде, и перевод сделал бы опись бесполезной; технические имена (пути, ключи конфигурации, идентификаторы) тоже оставлены как есть.

Здесь собрано всё, что оркестратор добавляет к промпту роли **сам**, без участия автора флоу или оператора. Это справочник, а не предложение: ничего из перечисленного не является запланированной работой. Источник истины — код; каждый блок ниже воспроизведён из него дословно, с указанием `file:line`, который его порождает.

Границы: текст, который доходит до **модели**. Собственный `role_file` ноды сюда не входит (его пишет оператор); файл задачи оператора тоже — он передаётся байт в байт, если не считать редакции секретов. Текст для человека (Telegram, консоль, тело PR) вне границ, кроме отдельно отмеченного случая, а файлы, которые модель читает по пути, вынесены в §3 — это инъекция содержимого, хотя и не текста промпта.

## Как собирается промпт

Один вызов провайдера получает ровно один текст на stdin. Он собирается в единственном нейтральном шве, [`build_effective_prompt`](../../src/wastech_orchestrator/providers/base.py) (`providers/base.py:362`):

```text
<security preamble>          §1 — строится в Core, приклеивается спереди только на ходе, открывающем сессию
<пустая строка>
<body>                       role_file ноды (или её resume_role_file), отрендеренный
<пустая строка>
<context files footer>       §2 — строится в Core, приклеивается сзади
```

Для хода супервизора телом служит не role-файл ноды флоу, а собственная линза супервизора плюс секции, дописанные кодом, — весь этот слой описан в §4.

Через всё это проходят два свойства:

- Рендерер подставляет только разрешённые списком токены `{name}`, и только **пути** ([`core/prompts.py:22`](../../src/wastech_orchestrator/core/prompts.py)), — никогда тела задач, диффы, логи, переменные окружения или секреты. Содержимое всегда приходит файлом, который модель открывает сама.
- Ни один адаптер не добавляет прозы. `--system-prompt` / `--append-system-prompt` и их `-file`-варианты входят в список отклоняемых аргументов адаптера Claude ([`providers/claude.py:626-629`](../../src/wastech_orchestrator/providers/claude.py)); структурированный вывод передаётся нативным флагом схемы, а не инструкцией в тексте.

## 1. Security-контракт оркестратора (преамбула)

Строится функцией [`build_orchestrator_security_preamble`](../../src/wastech_orchestrator/core/flow/security_preamble.py) (`security_preamble.py:34`), разрешается **один раз на прогон** в [`orchestrator.py:4858`](../../src/wastech_orchestrator/core/orchestrator.py), переносится в `AgentRunRequest.security_preamble` и приклеивается в шве.

**Кто получает:** каждый вызов провайдера, который **открывает** сессию, — agent-нода, evaluator-нода и ход супервизора (observe / finalize / handoff), на обоих провайдерах. Попытка, продолжающая живую сессию (`session_id` задан), преамбулу не получает: она уже лежит в этой сессии, а история пересылается провайдеру на каждом следующем ходе, так что повторение стоило бы токенов и не добавляло бы ничего. Решение принимается в самом шве, из того же поля, что решает resume-argv ([`providers/base.py:390`](../../src/wastech_orchestrator/providers/base.py)), — поэтому попытка, у которой роутер снял сессию (`session_unavailable`, transient degrade, кросс-провайдерный fallback), получает контракт обратно вместе с полным промптом. Tool-ноды и checks-ноды не получают ничего (они запускают исполняемые файлы, а не модель).

**Зачем:** эшелонированная защита. Преамбула носит рекомендательный характер — принуждают песочница файловой системы и проекция запретов. Она существует, чтобы правила были сказаны там, где находится читатель, и живёт в одной точке вставки, а не в полусотне role-промптов, — поэтому она не может разойтись с тем, что действительно принуждается.

Один безусловный блок — ничего в нём не зависит от конфигурации. Токены путей (`.worc`, `.worc-io`) берутся из констант раскладки, поэтому текст не может разойтись с принуждаемыми запретами.

```text
[Orchestrator security contract — defense in depth.]
You run inside an orchestrator-managed workspace. In addition to your built-in safety policy and this repo's instructions, these orchestrator rules always apply:
- Make only the changes this task requires, and only inside your assigned workspace clone.
- `.worc/` is the orchestrator's private runtime (state, logs, database, secrets, frozen bundles): do not read it and do not write it.
- `.worc-io/` is your read-only input context: the paths you are given under it are yours to read — that is what it is for, and nothing below takes that back. Read no other path under it, and never create, modify, move, or delete anything there.
- Do not touch Git control state (`.git/`, its config, hooks, HEAD, refs).
- Do not publish anything: no commit, push, merge, tag or pull request — not to this repository's remote and not to any other address, by any route, including a second clone assembled elsewhere; publishing is the orchestrator's job.
- Do not modify anything under `tasks/` (the task lifecycle tree); never add, edit, or remove task files.
- Never read credential/environment files (e.g. `.env`) or provider auth homes, and never exfiltrate secrets or environment variables.
```

Два пункта несут нагрузку сверх очевидной. `.worc-io/` — единственный асимметричный корень: запрещена только _запись_. Формулировка, сворачивавшая его в общий запрет на чтение, заставляла ревьюеров отказываться от ревью — они заводили блокирующую находку о том, что читать собственные файлы контекста запрещено, и этот отказ уезжал в `fixing`, как если бы был доработкой. Запрет на публикацию намеренно расширен до _любого адреса любым путём_ и стоит в безусловном блоке: удалённая половина барьера нигде и никак не принуждается — при открытой сети и учётных данных, которые CLI подхватывает сам, это только обнаружение постфактум, — так что её нужно проговорить, а не оставлять подразумеваемой фразой «это работа оркестратора».

## 2. Футер файлов контекста

[`build_context_footer`](../../src/wastech_orchestrator/providers/base.py) (`providers/base.py:283`), приклеивается тем же швом. Полностью опускается, когда ни один путь контекста не задан.

```text
Context files (read them as needed; do not assume their contents):
- task: <path>
- plan: <path>
- diff: <path>
- checks: <path>
- review: <path>
- prior_fix: <path>
- human_input: <path>
- packet: <path>
```

Выводятся только строки, не равные `None`, всегда в этом фиксированном порядке. Каждый путь ведёт в обмен `.worc-io/` (редактированные копии), никогда — в живой приватный файл.

**Зачем:** пути, а не содержимое — это и есть механизм, удерживающий большие данные вне промпта. «Do not assume their contents» — антигаллюцинационная половина: модели сказано, что файл существует и что его надо открыть. `prior_fix` — это собственный отчёт предыдущей авторской ноды при повторном заходе на доработку, чтобы ревьюер судил «устранена ли находка», имея на руках объяснение исполнителя, а не переставил диагноз по диффу.

## 3. Файлы, написанные оркестратором, которые модель читает по пути

Не текст промпта, но содержимое, которое оркестратор пишет и передаёт ноде, — поэтому оно входит в опись. Каждый файл редактируется перед попаданием в `.worc-io/`: известные значения секретов заменяются литералом `[REDACTED]` ([`providers/redaction.py:33`](../../src/wastech_orchestrator/providers/redaction.py)).

### 3.1 Заголовок memory-пакета

[`memory/packet.py:283`](../../src/wastech_orchestrator/memory/packet.py). Пишется, только если подсистема памяти включена **и** role-промпт ноды действительно ссылается на `{memory_path}`; когда подходящих записей нет, файл не создаётся вовсе.

```text
# Repository memory (advisory — verify against the code)
```

Дальше идут секции `## Lessons` и `## Entities`, по одному буллету на запись.

**Зачем:** пакет несёт извлечённые утверждения разного уровня доверия, поэтому заголовок сразу объявляет их статус — рекомендательный, проверяй, — вместо того чтобы позволить буллету читаться как истина.

### 3.2 Brief передачи между сабтасками

[`orchestrator.py:3370`](../../src/wastech_orchestrator/core/orchestrator.py) пишет заголовок; [`orchestrator.py:454`](../../src/wastech_orchestrator/core/orchestrator.py) форматирует детерминированный фундамент; [`supervisor.py:364`](../../src/wastech_orchestrator/core/supervisor.py) добавляет интерпретирующую секцию. Доходит до ноды следующего сабтаска как `{predecessor_context}`; для первого сабтаска отсутствует.

```text
# Predecessor context for subtask NN: <title>

### Subtask NN: <title> (declared dependency)
- Commit: <sha>
- Spec: <path>
- Acceptance criteria:
  - ...
- Changed files:
  - ...

## Interpretive handoff brief

### New surface area
...
### Locked decisions
...
### Open edges
...
```

**Зачем:** два слоя намеренно. Фундамент строится из того, что реально легло в ветку (каждый предыдущий сабтаск с коммитом, а не `depends_on`), не стоит ни одного вызова модели и присутствует даже когда супервизор недоступен. Пометка `(declared dependency)` передаёт акцент автора, не делая его источником фактов: чтение `depends_on` вместо этого скрывало от преемника закоммиченных соседей, которых он не объявлял.

### 3.3 Сгенерированная спецификация сабтаска

[`decomposition.py:248`](../../src/wastech_orchestrator/core/decomposition.py). Пишется один раз на сабтаск и никогда не перезаписывается; доходит до ноды как `{subtask_spec_path}`. Тело сабтаска, написанное оператором, вместо этого материализуется дословно.

```text
# Subtask NN: <title>

slug: <slug>
depends_on: <n, m | none>

## Acceptance criteria

- ...
```

## 4. Слой супервизора

Супервизор — это постоянный слой над флоу, а не нода графа. Его ходы получают преамбулу §1 и футер §2, как любой другой вызов; его **тело** — это линза (role-файл) плюс секции, дописанные кодом. Разделение намеренное: автор флоу переформулирует текст через свои prompt-файлы, но никогда — машинный контракт, который парсит оркестратор.

### 4.1 Встроенные линзы — используются, только если ни один role-файл не разрешился

Каждая линза — это цепочка, проваливающаяся во встроенный текст: observe — флоу `role_file` → глобальный `supervisor.role_file` → встроенный; finalize — флоу `finalize_role_file` → встроенный; handoff — флоу `handoff_role_file` → встроенный (`supervisor.py:1349-1400`).

`supervisor.py:194` — observe:

```text
You are a read-only supervisor observing a software task. Do not edit code.
```

`supervisor.py:195-203` — finalize:

```text
You are a read-only supervisor closing out a software task. Do not edit code.

Synthesize a plain-language summary of the whole task: what was done, how it works, how it integrates, and why, grounded in the actual committed change. In a closing section list any advisory caveats or follow-ups you noted across the steps.

Answer with real prose. A one-line, placeholder or probe summary is discarded as a failed generation and replaced by a mechanical report of the run, so it costs the whole synthesis.
```

Последний абзац находится именно в _запасном_ варианте, потому что порог деградации принуждается для каждого флоу, а флоу без собственной finalize-линзы читает только этот текст.

`supervisor.py:205-209` — handoff:

```text
You are a read-only supervisor briefing the next subtask in a decomposed task. Do not edit code. You have observed the predecessor subtask(s); write a focused handoff for the agent about to implement the successor.
```

### 4.2 Ход observe — дописанные секции

`supervisor.py:1208-1224`. Срабатывает на завершённый шаг, с разрешённой каденцией наблюдения (`none` / `events` / `selected` / `all` — см. [`observe_cadence.py`](../../src/wastech_orchestrator/core/observe_cadence.py)).

Тело = линза + блок истории + блок наблюдения.

Блок истории (`supervisor.py:1264-1271`), опускается, когда предыдущих шагов нет:

```text
## What the earlier steps reported
Oldest first. You have not been shown most of these: a turn is spent only on a deviation, so a step that finished cleanly passed without one. Judge a loop from this sequence rather than from an absence of visible work in it — a step that deliberately changed nothing says so here.
- <node> (<outcome>): <bounded message | (no closing message)>
```

Блок наблюдения (`supervisor.py:1217-1222`):

```text
## Step observed
Node: <node id>
Outcome: <outcome kind>

Findings it recorded:
- [<severity>] <reason> (<paths>)

The step reported:
<the node's closing message, truncated to the per-step cap>
```

**Зачем вообще нужен блок истории:** при поставляемой каденции `events` ход тратится только на отклонение, поэтому чистый раунд `fixing` никогда не наблюдается и его отчёт не попадает в сессию. Без этого блока наблюдение за ревьюером, который затем отправил на доработку, сделало вывод «the implementer produced nothing at all … check whether the implementation node is erroring or timing out» о шаге, который отработал 474 с, вышел с кодом 0 и записал ровно то, почему ничего не изменил. Прочтение отсутствия как провала — единственная ошибка, ради которой существует эта секция. Обе поверхности с сообщениями ограничены тем же лимитом, что и пакет: без ограничения болтливая нода раздувала каждый ход наблюдения, и каждый раунд доработки платил за это заново.

### 4.3 Ход finalize — дописанные секции

`supervisor.py:1273-1372`. Один ход в конце задачи, на **свежей** сессии.

Добавляется всегда (`supervisor.py:1288-1290`):

```text
## Task under review
The task specification is provided as the task packet referenced in the context below — read it there; it is not inlined here.
```

Когда записаны вердикты гейтов (`supervisor.py:1296-1304`):

```text
## Gate verdicts recorded for this task
Every in-flow evaluator's final verdict, with the findings it recorded. Ground each statement you make about verification in this list: a gate that recorded findings did **not** simply pass — say what it found and that the flow accepted it with those findings open. Do not name a gate that is absent from this list, and do not describe a check as something you performed yourself.

<rendered gate digest>
```

**Зачем:** ход finalize, описывавший гейты по одной лишь памяти сессии, написал «three independent verification gates … all of which passed», пока четыре находки критика лежали в `state.db`. Это записанные вердикты, поэтому «passed» неприменимо к гейту, выдавшему находки.

Когда существует детерминированный пакет фактов (`supervisor.py:1310-1319`):

```text
## Run facts (the packet)
This is a fresh session: you are NOT continuing an earlier conversation about this task, so do not write from memory of one. Read the `packet` file referenced in the context below — it is the deterministic record of this run (the changed paths and diff stat with a pointer to the full diff, every executed step with its outcome and what it reported, the checks that ran, and your own recorded per-step observations) — and ground every statement you make in it. Open the artifacts it points at when you need more detail than it carries. If something is absent from the packet, say so plainly rather than inferring it.
```

**Зачем:** ход по замыслу идёт на свежей сессии, так что памяти разговора попросту нет, — абзац говорит, где лежат факты и что они являются истиной. Указание на пакет вместо встраивания и есть весь смысл: JSON читается один раз как файл, а не пересылается как вход промпта на каждом ходе.

Когда флоу подписался на follow-ups (`supervisor.py:1322-1334`):

```text
## Technical debt / follow-ups
Also record concrete technical debt and refactor follow-ups you observed, as the structured `follow_ups` array. Each record is minimal and **evidence-gated**: a `title`, a short `rationale`, the `paths` it concerns, `evidence` pointers (files/lines/commits/checks that substantiate it), a `severity` (low/medium/high), and an optional `action_hint`. The `title` is an independent imperative label (aim for 80 characters or fewer) that reads on its own in a work queue — never a prefix or restatement of `rationale`. Propose only debt grounded in what actually happened this run — never speculative ideas; a record without evidence is dropped.
**Always emit the `follow_ups` key** — an empty array when nothing qualifies. Omitting it fails the response schema and costs the whole synthesis.
```

И, только если вердикты гейтов тоже были отрендерены (`supervisor.py:1341-1347`):

```text
Do **not** restate the evaluator findings listed under the gate verdicts above: every finding a gate accepted is carried into this list deterministically, so repeating one in your own words produces a duplicate entry (often at a contradictory severity). Record only debt that is NOT already in that list.
```

**Почему второй абзац условный:** принятые находки сливаются в тот же список детерминированно, а слияние отбрасывает дубли только по точному совпадению текста — поэтому пересказ выживает вторым буллетом. Измерено: 10 буллетов на ~6 проблем, две пары расходятся в severity. Самое дешёвое здравое решение — прекратить пересказ, а не угадывать, какие почти-дубли на самом деле одно и то же.

Когда включена память (`supervisor.py:1349-1367`):

```text
## Candidate memory delta
Also propose what is worth REMEMBERING for future tasks on this repo, as the structured `memory_delta`: durable `lessons` — repeatable PATTERNS and PRINCIPLES worth internalizing (recurring reviewer expectations, procedural gotchas, stable conventions/commands, architecture invariants, fragile areas), each with `kind`, `subject`, `statement`, and `evidence` pointers to repo files, docs, or named checks; recurring `failures` (signature + remedy); and important `entities` (files/modules with their paths). Put WHAT a file or module is or does in an `entity` card (with `risk_notes`), NOT in a lesson — a lesson captures a repeatable practice or principle, not a description. Anchor every `evidence` ref on something durable and resolvable — a repo path, a doc, a named check — NOT a commit SHA or a task id, which rot after merge. Do NOT narrate which task did what; capture durable knowledge, not this run's history. Propose only what repeats, stays true, or saves rediscovery — never secrets, raw diffs, or one-off details; every lesson needs evidence. Leave a list empty when nothing qualifies.
```

### 4.4 Ход handoff — дописанная секция

`supervisor.py:1382-1397`. Один ход на границу сабтасков в декомпозированной задаче; его структурированный результат становится интерпретирующей секцией из §3.2.

```text
## Handoff to subtask <N>
Every subtask already committed on this branch, oldest first — the ones this subtask declares as dependencies are marked, the rest landed before it and are facts it still has to live with:

<the deterministic floor from §3.2>

Write a focused brief for the agent implementing this next subtask, as the structured output's three sections: `new_surface_area` (what the predecessor(s) built that the successor should use), `locked_decisions` (contracts not to revisit, with brief rationale), and `open_edges` (what was deferred or must not be touched). Ground every claim in the facts above; be concise. Do not edit code.
```

### 4.5 Поля `description` в схемах — единственный инъектируемый текст вне тела промпта

Супервизор — единственное место, где JSON-схема, передаваемая провайдеру, несёт прозу. Эти строки доходят до модели как часть контракта структурированного вывода.

`supervisor.py:246-251` — свойство `summary` у finalize:

```text
The whole-task summary as Markdown prose: a short lead paragraph, then 2–4 sections with `##` subheadings and real line breaks between them. Plain prose only — do NOT embed follow_ups, memory_delta, or lessons here; return those in their own fields. Do not wrap the whole thing in a code fence.
```

**Зачем:** без этого модель упаковывает весь синтез в одну плоскую строку, и тело PR рендерится безголовой плитой.

`supervisor.py:146-149` — корень массива `follow_ups`:

```text
ALWAYS present — emit this key on every response. When nothing qualifies, emit an empty array (or null); never omit the key, and never drop it to shorten the answer.
```

**Зачем:** это несущая конструкция, а не украшение. Без неё модель, которой нечего сообщить, следует прозаическому «leave the array empty», опускает ключ, отклоняется по отсутствию обязательного свойства и схлопывается до сводки в несколько байт, которая затем уезжает в тело PR. Строка говорит в схеме то же, что промпт говорит прозой, — чтобы они не могли разойтись.

`supervisor.py:159-163` — `follow_ups.items.title`:

```text
A short imperative label (aim for 80 characters or fewer) naming the action, written to be read on its own in a work queue — NOT a prefix, restatement, or truncation of `rationale`.
```

Все остальные схемы, которые выдаёт оркестратор, — контракты HITL `human_input` / `planning` в [`core/hitl.py:97`](../../src/wastech_orchestrator/core/hitl.py), трёхсекционная схема handoff, `DELTA_OUTPUT_SCHEMA` для памяти, схема находок эвалюатора — **не** несут ни одного `description`.

## 5. Для человека, а не для агента

Перечислено один раз, чтобы это не приняли за инъекцию. Гейт продолжения/остановки при исчерпании ходов ([`core/flow/nodes/agent.py:1090`](../../src/wastech_orchestrator/core/flow/nodes/agent.py)) рендерит запрос подтверждения для **оператора**, никогда — для модели:

```text
Turn limit reached — continue this run?
Node '<node id>' hit its turn cap (max_turns). Approve to resume the same agent session with a fresh turn grant, or deny to stop the run.
```

## 6. Что намеренно не инъектируется

Полезно как опись «от противного» — каждый пункт проверен по коду, а не принят на веру.

| Поверхность | Что происходит |
| --- | --- |
| Системный промпт провайдера | Не задаётся ничего. `--system-prompt`, `--system-prompt-file`, `--append-system-prompt`, `--append-system-prompt-file` входят в список отклоняемых аргументов адаптера Claude; у Codex в собираемом argv эквивалента нет. |
| Структурированный вывод | Передаётся нативным файлом схемы (`--output-schema` для Codex, файл схемы для Claude), а не инструкцией, дописанной к тексту. |
| Пакет задачи | Файл задачи оператора, замороженный и отредактированный. Ни заголовка, ни front matter, ни обёртки. |
| Файлы инструкций репозитория | Не инъектируются и не склеиваются. Под изоляцией `--setting-sources ""` отключает нативный автозагруз `CLAUDE.md`; агент читает корневые файлы сам, потому что его role-промпт так велит, и на время прогона эти файлы закрыты на запись. |
| Артефакты обмена (diff, checks, review, prior fix, ответ человека) | Публикуются только как содержимое — редакция, никакой добавленной прозы. Пакет ответа HITL — это четыре ключа JSON (`kind`, `question`, `answer`, `approved`). |
| Tool-ноды | Получают на stdin объект JSON с контекстом (разрешённые пути + `args` флоу). Ни прозы, ни преамбулы — они запускают исполняемый файл, а не модель. |
| Checks-ноды | Вызова модели нет вообще. |
| Переменные промпта | Рендерер подставляет только разрешённые списком токены `{name}` — **пути**, — плюс `{<id>_path}` каждой agent-ноды. Неизвестный `{name}` остаётся дословно, поэтому фигурные скобки кода/JSON в role-файле проходят без изменений. |

## 7. Как увидеть это на реальном прогоне

Каждый текст выше сохраняется на диск, так что ничего из этого не нужно принимать на веру:

- `logs/<task-id>/stages/<node>/run-<NNNNNN>/rendered-prompt.md` — **эффективный** промпт этого прогона, вместе с футером и — на ходе, открывающем сессию, — преамбулой, отредактированный. Пишется для каждого запуска ноды ([`core/flow/observability.py:227`](../../src/wastech_orchestrator/core/flow/observability.py)).
- `logs/<task-id>/prompt-audit/` — документы по шагам плюс таймлайн, включается ключом конфигурации `prompt_audit`. Для ноды с `resume_role_file` рендерятся **оба** текста, так что видно и тот вариант, который попытка не получила; оба собираются тем же швом, поэтому у запроса с живой сессией преамбулы нет ни в одном из них.

Оба идут через тот же шов `build_effective_prompt`, который кормит провайдера, — поэтому то, что лежит на диске, и есть то, что было отправлено.
