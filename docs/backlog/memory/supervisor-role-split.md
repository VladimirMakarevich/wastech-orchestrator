# Supervisor Role Split For Memory

Дата: 2026-06-28

Цель этого note: выбрать лучшую роль `supervisor` в memory subsystem не с точки зрения "как меньше спорить с текущей кодовой базой", а с точки зрения:

- эффективности;
- простоты;
- надежности;
- дешевизны по токенам.

## Короткий вывод

Лучший вариант для WORC по этим критериям: **узкий supervisor + отдельные deterministic memory services**.

То есть:

- `Supervisor` наблюдает run и в конце пишет human-facing summary;
- `Supervisor` может, опционально, в том же finalization turn вернуть **candidate memory delta**;
- `MemoryService` владеет canonical memory store, merge, validation, promotion, quarantine, audit;
- `PacketBuilder` готовит stage-specific memory briefs;
- `CleanupJob` делает bounded cleanup между задачами;
- `DerivedIndex` владеет repo map / symbol index / lexical search / later FTS.

Иными словами: **supervisor не должен быть memory control plane**.

## Почему это лучше

### 1. Эффективность

Самый полезный memory effect для coding agents обычно получается не от "умного центрального мыслителя", а от:

- хорошего final distilled write;
- точного stage-specific retrieval;
- строгого stale/conflict handling.

Для этого не нужен runtime-supervisor, который собирает packets перед каждой стадией. Это лучше решается deterministic retrieval rules.

### 2. Простота

Если supervisor одновременно:

- пишет summary,
- извлекает memory,
- принимает promotion decisions,
- собирает planning/review/fixing packets,
- чистит память,

то он становится слишком широким объектом. Его сложно тестировать, объяснять и чинить.

Разделение на `Supervisor`, `MemoryService`, `PacketBuilder`, `CleanupJob` проще и честнее.

### 3. Надежность

Самые рискованные вещи в memory subsystem:

- promotion ложных facts;
- stale entries;
- retrieval нерелевантного memory;
- memory poisoning;
- тихая деградация через cleanup.

Эти вещи лучше обрабатываются **детерминированными правилами и валидаторами**, а не LLM-owned logic внутри supervisor.

### 4. Дешевизна по токенам

Самый дорогой вариант: supervisor-heavy design, где supervisor делает дополнительную LLM-работу:

- перед planning;
- перед implementation;
- перед review;
- перед fixing;
- во время cleanup.

Самый дешевый вариант:

- один existing finalization turn;
- zero extra LLM calls for packet building;
- zero extra LLM calls for cleanup by default.

## Сравнение вариантов

| Вариант | Эффективность | Простота | Надежность | Token cost | Вердикт |
| --- | --- | --- | --- | --- | --- |
| `Supervisor-heavy` | Medium-High | Low | Medium | Low | Не лучший default |
| `Narrow supervisor + MemoryService + PacketBuilder` | High | High | High | High | Лучший вариант |
| `No supervisor involvement at all` | Medium | Medium-High | Medium | Very High | Слишком теряет useful distilled context |

Примечание:

- `Token cost = High` в таблице значит "хорошо по cost", то есть дешево.

## Рекомендуемый split ролей

### `Supervisor`

Оставить:

- observe each completed step;
- produce whole-task summary;
- optionally emit one structured `candidate_memory_delta` at finalization.

Не давать:

- runtime packet assembly;
- direct writes into canonical memory store;
- promotion authority;
- stale/conflict resolution;
- cleanup ownership;
- policy enforcement.

Почему:

- это удерживает supervisor в роли trusted narrator, а не hidden control layer;
- это сохраняет cheap token profile: максимум один useful structured output at finalization.

### `MemoryService`

Должен владеть:

- canonical file layout under `.worc/memory/`;
- append/update/merge semantics;
- trust levels;
- evidence/provenance checks;
- promotion rules;
- quarantine;
- audit log;
- rollback hooks.

Почему:

- это deterministic domain logic;
- это должно unit-testиться без модели;
- это самый критичный reliability/safety seam.

### `PacketBuilder`

Должен владеть:

- stage-aware retrieval;
- brief shaping for `planning` / `implementation` / `review` / `fixing`;
- token budget caps;
- ordering and ranking rules;
- writing per-task packet files.

Почему:

- packets должны быть reproducible;
- packet bugs должны чиниться правилами, а не prompt-tuning supervisor;
- для token economy лучше deterministic top-k selection, а не extra LLM synthesis per stage.

### `CleanupJob`

Должен владеть:

- TTL expiry;
- path/symbol existence checks;
- command revalidation where safe;
- duplicate merge candidates;
- stale marking;
- bounded per-pass budget;
- no-network cleanup default.

Почему:

- cleanup должен быть скучным и безопасным;
- freeform LLM cleanup слишком рискован и слишком дорог.

### `DerivedIndex`

Должен владеть:

- repo map;
- symbol index;
- changed-path/entity lookup;
- later FTS;
- maybe later embeddings.

Почему:

- current codebase structure не надо хранить как human memory;
- это rebuildable index plane, не durable memory truth.

## Конкретные примеры

### Пример 1. Planning packet

`Supervisor-heavy`:

- supervisor читает task, memory, repo hints;
- отдельным LLM turn собирает `planning packet`.

Минусы:

- лишние токены;
- nondeterministic packet quality;
- packet drift between runs.

`Recommended split`:

- `PacketBuilder` берет:
  - touched paths from task/plan hints,
  - top relevant long-term lessons,
  - matching entities,
  - recent related episodic notes;
- пишет `logs/<task-id>/memory/planning.md`.

Плюсы:

- zero extra LLM calls;
- predictable size;
- easier eval.

### Пример 2. Final memory write

`Supervisor-heavy`:

- supervisor пишет summary;
- тут же сам решает, что promoted, что stale, что conflict.

Минусы:

- promotion logic спрятана в LLM behavior;
- сложнее audit;
- больше poisoning surface.

`Recommended split`:

- supervisor пишет summary и `candidate_memory_delta`;
- `MemoryService` валидирует evidence;
- `MemoryService` принимает promotion/quarantine decision.

Плюсы:

- дешевый reuse existing final turn;
- promotion rules testable and explicit.

### Пример 3. Review packet

`Supervisor-heavy`:

- supervisor "думает", какие old reviewer notes важны именно сейчас.

`Recommended split`:

- `PacketBuilder` по touched paths и finding categories вытаскивает:
  - top recurring review rules;
  - same-entity failure patterns;
  - exact verification commands.

Почему split лучше:

- review retrieval можно измерять на precision/recall;
- не надо оплачивать extra LLM turn, чтобы просто отсортировать known records.

### Пример 4. Idle cleanup

`Supervisor-heavy`:

- supervisor/autodream перечитывает memory и "решает", что устарело.

`Recommended split`:

- `CleanupJob`:
  - удаляет expired episodic records;
  - помечает missing paths as stale;
  - предлагает duplicate merges;
  - кладет uncertain cases в quarantine.

Почему split лучше:

- cleanup predictable;
- no hidden semantic drift;
- minimal token burn.

## Где supervisor все-таки полезен

Supervisor полезен там, где deterministic logic слабее:

- коротко дистиллировать whole-task result;
- выделить candidate lessons из mixed evidence;
- подсветить "what mattered" для future runs.

Но даже здесь лучший cost-effective вариант:

- **не отдельный memory-only turn**;
- а **structured appendage to final summary turn**.

## Практическая рекомендация для V1

Для V1 я бы фиксировал такой контракт:

1. `Supervisor.finalize()` возвращает:
   - `summary_md`
   - optional `candidate_memory_delta`

2. `MemoryService.apply_delta()` делает:
   - redaction
   - validation
   - merge
   - promotion/quarantine
   - audit

3. `PacketBuilder.build(stage, task_context)` делает:
   - deterministic retrieval
   - bounded brief generation
   - writes one packet file by path

4. `CleanupJob.run_once()` делает:
   - bounded stale/TTL/duplicate maintenance

## Что не стоит отдавать supervisor

Не стоит делать supervisor ответственным за:

- выбор `top-k` records на каждую стадию;
- ranking policy;
- stale detection rules;
- duplicate merge rules;
- evidence reconciliation;
- trust-level assignment as hidden prompt logic;
- cleanup/autodream semantics.

Все это лучше реализуется как explicit, inspectable, testable code.

## Bottom line

Если приоритеты — **эффективность, простота, надежность и дешевизна по токенам**, то лучший дизайн такой:

- `Supervisor` узкий и cheap;
- `MemoryService` deterministic and authoritative;
- `PacketBuilder` deterministic and stage-aware;
- `CleanupJob` bounded and boring.

Именно это дает лучший компромисс:

- не теряется польза финальной LLM-дистилляции;
- runtime retrieval остается дешевым;
- memory logic остается проверяемой;
- supervisor не превращается в труднообъяснимый второй оркестратор.
