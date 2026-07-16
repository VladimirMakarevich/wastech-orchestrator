# Варианты оптимизации supervisor

- **Основание:** прогон `blog-review-happy-in-my-misfortunes-4`
- **Связанный отчёт:** [полный анализ токенов](2026-07-16-blog-review-happy-in-my-misfortunes-4-token-analysis.md)
- **Зона изменений:** `SupervisorConfig`, constant supervisor layer, post-node hook, summary finalization
- **Статус:** proposal, код и конфигурация пока не изменены. Этот документ — трекер отдельной задачи «оптимизация supervisor»; P0-срез оформлен как отдельный backlog-документ [supervisor-finalize-packet-and-cadence.md](../backlog/supervisor-finalize-packet-and-cadence.md) (packet → fresh finalize → пропуск tool/checks), а сопутствующая задача по нормализации usage вынесена в [normalized-usage-accounting.md](../backlog/normalized-usage-accounting.md).

---

## Проверено по коду (2026-07-16)

Первый шаг (Вариант A / §8 P0 — не наблюдать детерминированные ноды) подтверждён по актуальному коду и является самым дешёвым безопасным изменением:

- observe вызывается из post-node hook после каждой executed non-publish ноды; единственная фильтрация сегодня — `node.kind != "publish"` (`core/orchestrator.py:2484`). `length` — это `tool`-нода (`packaged/flows/blog_article_revise.yaml`), и `node.kind` в точке вызова уже доступен, поэтому пропуск kind ∈ {`tool`, `checks`} — правка **одного условия**, без новой машинерии.
- Пропущенные (`when`-false / disabled) ноды и так не наблюдаются — post-node hook движка срабатывает только для executed нод (`core/flow/engine.py`), так что менять cadence безопасно.
- Supervisor advisory-по-построению: все записи `verdict="advisory"`, движок их не читает для роутинга (`core/supervisor.py`), у него нет `route`/`rework`. Значит изменение cadence не влияет на стейт-машину.
- model/reasoning/provider supervisor **уже** конфигурируемы (`SupervisorConfig`, `packaged/config.example.yaml`), но cadence / `observation_mode` / раздельные observe-vs-finalize настройки — нет; их добавление (§8 P1) трогает ~5 точек: `config/schema.py`, `config/loader.py`, `config/validation.py`, `packaged/config.example.yaml` и условие в hook.
- Механизм fresh-finalize из digest (Вариант E) уже реализован как recovery-путь `Supervisor._finalize_digest` — задача в том, чтобы сделать его основным.

CODEX WARNING остаётся в силе: для Codex-supervisor resume кумулятивен, поэтому точный token-budget (Вариант I) требует нормализованного usage из [normalized-usage-accounting.md](../backlog/normalized-usage-accounting.md) как предпосылки.

CODEX WARNING: Важно заметить, что для Codex проблема еще более губительная, так как там при resume команде заново передается весь предыдущий контектс, из за чего потребление увеличивается по экспоненте!

Далее анализ идет на примере Claude=Supervisor.

## Краткий вывод

Supervisor сейчас является самым тяжёлым потребителем Claude-контекста. В исследованной задаче его семь вызовов потратили **480 293 input tokens**, **12 706 output tokens**, **$0.7694** и около **177 секунд provider duration**. Это 70,3 % всего Claude input задачи.

При этом шесть вызовов из семи были промежуточными наблюдениями. Они потратили **375 726 input tokens**, **5 060 output tokens** и **$0.4375**. Supervisor по архитектурному контракту advisory-only. Он не может исправить файл, остановить flow, отправить ноду на rework или изменить route. Его notes сохраняются для человека и затем используются при подготовке финального summary.

Для content flow, где уже есть отдельный blocking `tone_style` evaluator, постоянное LLM-наблюдение за каждым шагом почти полностью дублирует существующие проверки. Самый разумный целевой режим здесь — **deterministic event ledger + один fresh finalize из компактного artifact packet**. LLM-observation следует запускать только по событиям, где note действительно нужен человеку или следующему агенту — rework, failure, HITL, опасный diff или граница subtask.

Рекомендуемая последовательность улучшений выглядит так.

1. Перестать вызывать supervisor после deterministic tool nodes. На этом run один только `length` стоил 44 107 input tokens.
2. Finalize всегда запускать в fresh session из сохранённых step records. Recovery-код для такого режима уже существует в `Supervisor._finalize_digest`; сейчас он используется только после потери warm session.
3. Добавить `observation_mode: all | events | none`, где для `blog_article_revise` выбрать `events` или `none`.
4. Разделить настройки observation и finalize. Наблюдения запускать на дешёвой модели с low/medium reasoning, финальный summary оставить на более сильной конфигурации.
5. Ограничить число наблюдений и размер rolling digest, чтобы fix loops не давали неограниченный рост.

---

## 1. Что supervisor делает сейчас

Supervisor находится над flow и создаётся для каждой задачи. После завершения любой выполненной ноды, кроме `publish`, engine вызывает `Supervisor.observe`. Пропущенные ноды не наблюдаются.

```text
flow node completed
        ↓
post_node hook
        ↓
Supervisor.observe
        ↓
отдельный read-only LLM turn
        ↓
supervisor_step row в evaluations
        ↓
resume той же supervisor session на следующем шаге
```

При закрытии задачи `Supervisor.finalize` продолжает эту же warm session и пишет `summary.md`, `summary.json`, optional memory delta и follow-ups.

Текущие свойства закреплены прямо в коде и тестах.

- Supervisor наблюдает каждый completed non-publish step.
- Каждый observation является отдельным provider run.
- Session сохраняется в `node_lineage` под `__supervisor__` и возобновляется между шагами и после restart.
- Permission profile принудительно равен `read-only`.
- Все записи имеют verdict `advisory`.
- У supervisor отсутствуют методы `route` и `rework`.
- Ошибка supervisor никогда не ломает задачу.
- Финальный summary имеет deterministic fallback, если LLM недоступен.

Основные точки реализации.

- [Supervisor](../../src/wastech_orchestrator/core/supervisor.py) — observe, warm lineage, finalize и recovery digest.
- [Post-node hook](../../src/wastech_orchestrator/core/orchestrator.py) — безусловный вызов observe для любой non-publish ноды.
- [SupervisorConfig](../../src/wastech_orchestrator/config/schema.py) — сейчас содержит только общий `role_file`, `provider`, `model`, `reasoning`.
- [Supervisor tests](../../tests/core/test_supervisor.py) — прямо фиксируют контракт «one LLM call per completed step».

### Что supervisor всё-таки делает полезного

Фраза «ничего не делает» верна в отношении управления flow. Однако внутри supervisor сейчас объединены четыре разные функции.

1. Per-step observer пишет advisory notes.
2. Finalizer создаёт PR/commit summary, memory delta и follow-ups.
3. Skill proposer может один раз предложить node-to-skill map.
4. Handoff synthesizer может подготовить interpretive brief между subtasks.

Последние три функции имеют конкретного потребителя. Summary читает человек, skill map используется Core после deterministic validation, handoff получает следующий subtask. Самая спорная и дорогая часть — именно постоянный per-step observer.

---

## 2. Расход на исследованной задаче

Полный Claude input рассчитан как сумма `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`.

| Вызов | Total input | Output | Cost, USD | Что дал |
| --- | --: | --: | --: | --- |
| after context | 34 215 | 1 604 | 0.1137 | Повторно подтвердил качество scout brief |
| after research | 37 891 | 215 | 0.0356 | Подтвердил, что research правильно ничего не искал |
| after revise | 122 857 | 1 924 | 0.1891 | Повторно проверил четыре diff-hunk и отсутствие drift |
| after length | 44 107 | 175 | 0.0255 | Прокомментировал обычный deterministic pass |
| after tone_style | 44 458 | 527 | 0.0233 | Отметил, что evaluator вернул мало пояснений |
| after polish | 92 198 | 615 | 0.0505 | Повторно проверил одну замену `became → was` |
| finalize | 104 567 | 7 646 | 0.3319 | Создал итоговый summary и memory delta |
| **Итого** | **480 293** | **12 706** | **0.7694** | — |

Разделение observation и finalize показывает, где находится основной необязательный расход.

| Слой | Calls | Input | Output | Cost, USD | Provider duration |
| --- | --: | --: | --: | --: | --: |
| Per-step observations | 6 | 375 726 | 5 060 | 0.4375 | 100.5 s |
| Finalize | 1 | 104 567 | 7 646 | 0.3319 | 76.7 s |
| **Supervisor total** | **7** | **480 293** | **12 706** | **0.7694** | **177.2 s** |

Внешнее wall time supervisor немного выше provider duration из-за запуска CLI и оркестратора. По timestamps семь вызовов заняли около 198 секунд, то есть больше трёх минут из общей задачи длительностью примерно 12,5 минуты.

### Почему короткий note стоит десятки тысяч tokens

Prompt observation после `length` занимает всего 86 слов и 570 bytes. Однако Claude invocation обработал 44 107 input tokens. Сам текст запроса здесь почти ничего не определяет. Основной объём дают agent runtime, tool schemas, предыдущие сообщения и накопленная warm supervisor session.

Session растёт после каждого шага. В неё попадают предыдущие notes, tool calls, результаты чтения diff и новые step reports. Prompt cache снижает денежную цену повторяющегося prefix, но raw token usage и pressure на лимиты сохраняются.

`finalize` продолжает ту же историю и поэтому получает уже 104 567 input tokens. Между тем все шесть сохранённых notes вместе занимают около 7,1 тыс. chars. Их можно передать fresh finalizer как компактный digest вместо полной conversational lineage.

---

## 3. Почему текущая модель плохо окупается

### Advisory note не влияет на текущую задачу

Supervisor может заметить реальную проблему, но engine не читает его verdict для route. Если blocking evaluator пропустил дефект, supervisor только упомянет его позднее в summary. К этому моменту файл уже прошёл flow.

Если observation должен защищать качество, ему нужен явный потребитель — human gate, evaluator или route decision. Без этого дорогой review-like turn выполняет работу, результат которой не меняет текущий outcome.

### Он повторяет специализированные ноды

В `blog_article_revise` уже существуют роли с более точным назначением.

- `context` анализирует статью и rules.
- `research` проверяет необходимость внешнего материала.
- `tone_style` является blocking evaluator.
- `polish` делает финальную языковую правку.

Supervisor после каждой из них снова читает результат и иногда diff. Его lens шире, но практическая проверка пересекается со scout и tone evaluator.

### Он наблюдает deterministic узлы

`length` возвращает простой pass/fail. LLM не добавляет новых фактов к успешному числовому gate. В этом run supervisor потратил 44 107 input tokens, чтобы сказать, что pass выглядит правдоподобно.

### Warm lineage используется там, где уже есть durable artifacts

Итоги нод сохранены в `node_runs`, `evaluations`, stage artifacts, `current.diff`, findings и summary inputs. Разговорная память supervisor дублирует этот ledger. Более надёжный источник для finalization уже лежит на диске и в SQLite.

### Один config управляет разными задачами

`SupervisorConfig.model` и `reasoning` одновременно применяются к дешёвому observation и сложному finalize. Нельзя задать low для notes и medium/high для финальной synthesis. Нельзя выключить observations, сохранив summary.

---

## 4. Варианты улучшения

### Вариант A. Не наблюдать deterministic node kinds

Supervisor пропускает `tool`, `checks` и другие ноды, чьи результаты полностью описываются structured outcome и artifacts.

```python
if node.kind not in {"tool", "checks", "publish"}:
    supervisor.observe(...)
```

**Эффект на этот run:** минимум 44 107 input tokens, 175 output tokens и $0.0255 за `length`.

**Плюсы:** маленькое изменение, почти нет quality risk.

**Минусы:** agent/evaluator observations по-прежнему вызываются всегда, warm session продолжает расти.

**Вердикт:** обязательный P0, но сам по себе проблему не решает.

### Вариант B. Configurable observation allowlist

Flow или global config задаёт, какие ноды supervisor наблюдает.

```yaml
supervisor:
  observe:
    mode: selected
    include_nodes: [revise, polish]
```

Для generic implementation flow список может включать `implementation`, `review`, `fixing`. Для content flow достаточно author nodes либо вообще одного finalize.

**Плюсы:** прозрачный контроль для оператора, разные flows получают разный cadence.

**Минусы:** статический список плохо учитывает loops и outcome конкретного run.

**Вердикт:** полезно как ручной режим, но event policy гибче.

### Вариант C. Event-triggered observations

LLM-observation запускается только по событиям с информационной ценностью.

Рекомендуемые triggers.

- `outcome in {fail, rework, manual_action_required}`;
- HITL question/answer;
- dangerous diff approval;
- новый blocking/high finding;
- provider fallback или повторная попытка;
- subtask boundary;
- существенное изменение diff fingerprint после fixing.

Обычные `done`, `pass` и `accept` записываются deterministic event row без LLM.

```yaml
supervisor:
  observe:
    mode: events
    triggers:
      [rework, failure, hitl, dangerous_diff, fallback, subtask_boundary]
```

На этой задаче не было rework, fallback или HITL. Per-step LLM observations можно было полностью пропустить.

**Плюсы:** расход растёт вместе с реальными отклонениями, а не с количеством обычных шагов.

**Минусы:** summary packet должен содержать deterministic records нормальных шагов.

**Вердикт:** рекомендуемый default для большинства flows.

### Вариант D. Fresh observation + rolling digest

Каждый нужный observation стартует в fresh session. Вместо resume он получает короткий bounded digest предыдущих notes.

```text
Current event
+ last material observations
+ current diff/findings paths
+ compact task facts
```

После ответа Core сохраняет note и обновляет rolling digest. Старые notes схлопываются, размер digest имеет жёсткий предел, например 2–4 тыс. tokens.

**Оценка:** первый supervisor run в этой задаче обработал 34 215 input tokens. Шесть независимых observations с ограниченным packet ориентировочно дали бы 180–240 тыс. input вместо 375 726. Это оценка, точное число требует A/B.

**Плюсы:** нет неограниченного роста lineage, restart ничего не меняет.

**Минусы:** базовый overhead полноценного Claude Code CLI остаётся на каждом вызове.

**Вердикт:** использовать вместе с event-triggering, не как самостоятельное решение.

### Вариант E. Finalize всегда fresh из digest

Сейчас этот механизм уже реализован для recovery. Если warm supervisor session недоступна, `Supervisor.finalize` читает сохранённые `supervisor_step` rows через `_finalize_digest` и запускает fresh turn.

Предлагается сделать этот путь основным.

```text
deterministic task packet
+ material supervisor notes
+ final diff/checks/findings
        ↓
fresh finalize turn
```

Warm session после observations не нужна для summary. Notes сохраняются в SQLite, поэтому fresh finalize остаётся crash-safe и аудируемым.

В этом run warm finalize потратил 104 567 input tokens. Компактный digest из notes занимает около 7,1 тыс. chars; вместе с task packet fresh finalizer, вероятно, уложится в 20–40 тыс. input tokens. Оценка экономии — 65–85 тыс., которую нужно подтвердить A/B.

**Плюсы:** значительное снижение input, recovery и normal path становятся одинаковыми.

**Минусы:** final packet должен явно содержать нужные артефакты, иначе модель начнёт заново исследовать repo.

**Вердикт:** рекомендуемый P0.

### Вариант F. Finalize-only supervisor

На каждом шаге Core пишет deterministic record, но LLM не вызывается. В конце один fresh finalizer получает bounded packet.

Для этой задачи можно было убрать все шесть observations.

```text
Точная историческая верхняя граница:
375 726 input tokens
5 060 output tokens
$0.4375
100.5 seconds provider duration
```

Финальный вызов тоже стал бы дешевле после перехода на fresh digest.

**Плюсы:** лучший баланс для flows с собственными evaluators; остаётся качественный PR summary.

**Минусы:** supervisor больше не накапливает интерпретацию по ходу задачи. Для decomposed tasks handoff должен остаться отдельной функцией.

**Вердикт:** рекомендуемый режим для `blog_article`, `blog_article_revise` и других коротких content flows.

### Вариант G. Полностью deterministic summary

PR body строится шаблоном из task title, changed paths, node final messages, checks и findings. LLM supervisor не запускается.

**Максимальная экономия на этом run:** 480 293 input tokens, 12 706 output tokens, $0.7694 и около 177 секунд provider duration.

**Плюсы:** нулевая LLM-стоимость, стабильный формат, отсутствие hallucination.

**Минусы:** summary будет заметно суше, memory delta и interpretive follow-ups исчезнут либо потребуют отдельного механизма.

**Вердикт:** optional `supervisor.mode: deterministic` для cost-sensitive runs, не основной default.

### Вариант H. Разные model/reasoning для observe и finalize

```yaml
supervisor:
  observe:
    provider: claude
    model: claude-sonnet-5
    reasoning: low
  finalize:
    provider: claude
    model: claude-sonnet-5
    reasoning: medium
```

Observation обычно должен извлечь один короткий риск из уже подготовленного event packet. Finalize выполняет synthesis и может получить больший reasoning budget.

**Плюсы:** простая экономия output/reasoning без изменения cadence.

**Минусы:** почти не уменьшает повторный input warm session; снижать reasoning без исправления cadence недостаточно.

**Вердикт:** P1 после event/fresh-session изменений.

### Вариант I. Token и call budget

Supervisor получает жёсткие ограничения.

```yaml
supervisor:
  observe:
    max_calls: 3
    max_digest_tokens: 4000
    on_budget_exhausted: deterministic_only
```

После исчерпания budget Core продолжает записывать events, а finalizer получает digest. Deep fix loop больше не может породить десятки supervisor calls.

**Плюсы:** предсказуемый верхний предел.

**Минусы:** для точного token budget сначала нужна provider-aware usage normalization из связанного отчёта.

**Вердикт:** P1/P2, особенно важен для implementation flows с длинными review loops.

### Вариант J. Асинхронный supervisor

Наблюдения выполняются после завершения flow либо вне critical path.

**Плюсы:** задача не ждёт три минуты advisory-работы.

**Минусы:** token usage не уменьшается; усложняются lifecycle, cleanup и гарантии summary-before-publish.

**Вердикт:** latency optimization после token optimization. Самостоятельно проблему не решает.

### Вариант K. Сделать дорогую проверку actionable

Если от supervisor ожидается реальная защита качества, его material finding должен иметь потребителя. Возможные действия — отправить flow на rework, открыть human gate или остановить publish до решения.

Превращать constant supervisor целиком в blocking router не рекомендуется. Это нарушит его простой best-effort контракт, создаст второй конкурирующий evaluator и сделает любой flow зависимым от одного общего prompt. Более чистый путь — вынести ценный lens в flow-local evaluator.

```text
Recurring supervisor concern
        ↓
явный evaluator node с typed findings
        ↓
accept / rework edge с bounded loop
```

В `blog_article_revise` это уже сделано через `tone_style`. Поэтому supervisor review после `revise` и `polish` не нужно повышать до blocking — его следует удалить или оставить только в финальном summary. В flow без evaluator повторяющуюся полезную проверку стоит оформить отдельной нодой, где её результат действительно меняет outcome.

**Плюсы:** каждый дорогой review-вызов получает реальный эффект на качество.

**Минусы:** новые loops и риск дублирования, если lens уже покрыт существующим evaluator.

**Вердикт:** не режим supervisor, а правило проектирования flow — полезную проверку переносить туда, где она может повлиять на route.

---

## 5. Что не даст ожидаемой экономии

### Server-managed conversation вместо CLI resume

Хранение conversation на сервере упрощает передачу состояния, но модели всё равно нужен предыдущий контекст. Исторические input tokens продолжают учитываться. Это улучшение transport/state management, не решение token growth.

### Prompt cache

Cache уже работает. Из 480 293 supervisor input tokens 404 488 пришли из cache read. Благодаря этому денежная цена ниже raw объёма. Однако cached tokens остаются в usage и влияют на rate limits. Cache смягчает симптом, не убирает лишние вызовы.

### Только снижение reasoning

Reasoning влияет прежде всего на output. Главная статья supervisor — 480 тыс. input tokens. `high → medium` полезно, но warm lineage и cadence останутся.

### Более короткий role prompt

Prompt после `length` уже занимает 86 слов. Его сокращение на несколько строк незаметно рядом с 44 тыс. input tokens agent/session context.

### RTK и сокращение shell output

Supervisor почти не запускал shell tools на обычных notes. Главный множитель здесь — число LLM turns и resumed history. RTK полезен для тяжёлых agent tool loops, но не является первым рычагом supervisor.

---

## 6. Рекомендуемая целевая архитектура

### Разделить монолитный supervisor на четыре обязанности

```text
StepRecorder          deterministic, всегда
ObservationAdvisor    optional, event-triggered
TaskFinalizer         один fresh LLM turn
SubtaskHandoff        только на реальной границе subtasks
SkillProposer         только при dynamic skills и непустом inventory
```

`StepRecorder` становится источником правды. Он сохраняет bounded facts без LLM.

- node id, kind и outcome;
- run/attempt/provider/model;
- changed paths и diff fingerprint;
- checks summary;
- evaluator verdict и severity counts;
- bounded final message;
- HITL/fallback/retry facts;
- artifact references.

`ObservationAdvisor` вызывается только по configured events. Его note добавляется к ledger, но не заменяет факты.

`TaskFinalizer` всегда начинает fresh session и получает один `SupervisorPacket`, собранный из ledger и финальных artifacts. Он не исследует repo с нуля и не резюмится на многотурновую историю.

`SubtaskHandoff` сохраняется отдельно, потому что его результат реально получает следующий агент. Для него warm continuity может быть оправдана, но пакет predecessor facts всё равно должен оставаться deterministic floor.

### Предлагаемая конфигурация

Это новая схема, текущий loader её пока не поддерживает.

```yaml
supervisor:
  provider: claude

  observe:
    mode: events # all | selected | events | none
    triggers:
      - rework
      - failure
      - hitl
      - dangerous_diff
      - fallback
      - subtask_boundary
    model: claude-sonnet-5
    reasoning: low
    session: fresh_digest # warm | fresh_digest
    max_calls: 3
    max_digest_tokens: 4000

  finalize:
    enabled: true
    model: claude-sonnet-5
    reasoning: medium
    session: fresh_packet

  handoff:
    enabled: true
    reasoning: medium
```

Flow-local block должен уметь сужать global policy.

```yaml
flow:
  supervisor:
    observation_mode: none
    role_file: blog_article_revise/supervisor.md
    finalize_role_file: blog_article_revise/summary.md
```

Для `blog_article_revise` рекомендуется `observation_mode: none`. Blocking `tone_style` evaluator уже отвечает за качество, а finalizer получает task, diff и findings напрямую.

Для implementation flow рекомендуется `observation_mode: events`. Обычные pass/accept не требуют LLM; rework, HITL, fallback и dangerous diff сохраняют interpretive note.

---

## 7. SupervisorPacket для fresh finalize

Чтобы fresh finalizer не начал повторно читать весь repo, Core должен передать bounded packet.

```json
{
  "task": {
    "id": "...",
    "title": "...",
    "type": "..."
  },
  "flow": {
    "name": "blog_article_revise",
    "final_status": "done"
  },
  "changes": {
    "paths": ["blog/article.md"],
    "diff_path": ".../current.diff",
    "diff_stats": "+4/-4"
  },
  "steps": [
    { "node": "revise", "outcome": "done", "message": "bounded text" },
    { "node": "tone_style", "outcome": "accept", "findings": 1 },
    { "node": "polish", "outcome": "done", "message": "bounded text" }
  ],
  "checks": { "passed": 1, "failed": 0 },
  "findings_path": ".../findings.json",
  "material_observations": []
}
```

Packet должен ссылаться на полные artifacts и включать только bounded summary. Full diff можно читать лишь когда он достаточно мал; иначе finalizer получает changed paths и deterministic diff stats с path к оригиналу.

Преимущества packet-first подхода.

- Normal run и restart используют одинаковый finalize path.
- Summary воспроизводим из durable state.
- Нет зависимости от живой provider session.
- Input имеет измеримый верхний предел.
- Можно тестировать packet без LLM.
- Provider можно менять перед finalize без cross-session resume.

---

## 8. План реализации

### P0. Безопасные изменения

1. Добавить policy, которая пропускает `tool` и `checks` nodes.
2. Изменить `finalize`, чтобы он всегда использовал fresh session и digest/packet.
3. Сохранить raw observations и deterministic fallback без изменений.
4. Добавить тесты на отсутствие session id у normal finalize.

Ожидаемый эффект на исследованной задаче — минимум 44 107 input tokens от `length` плюс ориентировочно 65–85 тыс. на fresh finalize.

### P1. Управляемый cadence

1. Расширить `SupervisorConfig` отдельными `observe` и `finalize` settings.
2. Добавить `observation_mode` и event triggers.
3. В post-node hook всегда писать deterministic step record и условно вызывать LLM observer.
4. Для content flows установить finalize-only.
5. Для implementation flows установить event mode.
6. Добавить max calls и bounded digest.

Для этой задачи finalize-only убирает исторические 375 726 observation input tokens. С учётом fresh finalizer общий supervisor input реалистично сократить с 480 тыс. до порядка 30–60 тыс. Это целевой диапазон, а не обещание до A/B.

### P2. Разделение обязанностей и telemetry

1. Вынести deterministic `StepRecorder` из LLM supervisor.
2. Сделать handoff и skill proposal отдельными budgeted capabilities.
3. Persist normalized usage/cost по каждой функции.
4. Добавить отчёт `supervisor calls/input/cache/output/cost/duration` в task summary.
5. Предупреждать, когда supervisor становится крупнейшим потребителем задачи.

---

## 9. Файлы, которые затронет реализация

| Файл/слой | Изменение |
| --- | --- |
| `src/wastech_orchestrator/config/schema.py` | Новые observe/finalize settings и defaults |
| `src/wastech_orchestrator/core/supervisor.py` | Policy, fresh packet finalize, rolling digest, budgets |
| `src/wastech_orchestrator/core/orchestrator.py` | Deterministic step recording и conditional observe |
| `src/wastech_orchestrator/core/flow/schema.py` | Flow-local narrowing policy |
| `src/wastech_orchestrator/state_store.py` | При необходимости отдельный compact step ledger / usage |
| `tests/core/test_supervisor.py` | Cadence, no-resume finalize, budgets, restart parity |
| `tests/core/test_flow_engine.py` | Skip/trigger behavior в post-node lifecycle |
| `src/wastech_orchestrator/packaged/config.example.yaml` | Operator-facing config |
| `src/wastech_orchestrator/packaged/guide/` | Installed documentation |
| `docs/configuration.md` и `docs/worc_architecture.md` | Canonical behavior и rationale |

Изменение нельзя ограничить target flow, потому что текущий код не поддерживает отключение observations отдельно от finalize. Временный target-only рычаг — снизить global supervisor reasoning `high → medium`, но он почти не затронет главный input multiplier.

---

## 10. Тесты и критерии приёмки

### Поведенческие тесты

- `tool/checks` node не создаёт provider request, но появляется в deterministic step ledger.
- `rework/failure/HITL` создаёт observation в `events` mode.
- Обычный `done/pass/accept` не создаёт observation в `events` mode.
- `none` mode сохраняет finalize и summary.
- Finalize никогда не получает warm observation session id в `fresh_packet` mode.
- Restart и обычный run создают одинаковый SupervisorPacket.
- Digest имеет deterministic token/char bound.
- Budget exhaustion переключает observer в deterministic-only без остановки задачи.
- Handoff и skill proposal продолжают работать независимо от observation mode.
- Supervisor остаётся read-only и advisory.

### Метрики A/B

Для повторного `blog_article_revise` нужно сравнить текущий режим и finalize-only.

| Метрика                     | Текущий run |                        Цель |
| --------------------------- | ----------: | --------------------------: |
| Supervisor calls            |           7 |                           1 |
| Observation calls           |           6 |                           0 |
| Supervisor input            |     480 293 |                    < 60 000 |
| Supervisor wall time        |      ~198 s |                      < 60 s |
| Summary completeness        |    baseline |            не хуже baseline |
| Пропущенные blocking issues |           0 | 0, их держит tone evaluator |

Quality comparison должен проверять summary по четырём пунктам — что изменено, почему, какие проверки прошли и какие caveats остались. Красивый текст сам по себе не оправдывает постоянную warm session.

---

## 11. Рекомендуемое решение

Для ближайшей реализации стоит выбрать гибрид.

1. **Всегда сохранять deterministic step facts.** Это дешёвая и надёжная основа аудита.
2. **По умолчанию использовать event-triggered observations.** Нормальный успешный шаг не требует отдельного LLM.
3. **Для content flows использовать finalize-only.** Их blocking evaluator уже отвечает за качество.
4. **Всегда финализировать из fresh bounded packet.** Warm supervisor lineage не должна быть источником правды.
5. **Оставить warm continuity только для subtask handoff, если A/B покажет реальную пользу.** У handoff есть downstream consumer.
6. **Разделить model/reasoning budgets.** Low для редкого note, medium для summary.
7. **Ввести max calls и max digest size.** Deep loops получают предсказуемый потолок.

На исследованной задаче такая схема сократила бы supervisor с семи LLM-вызовов до одного. Исторически удаляемая часть уже известна точно — 375 726 input tokens и $0.4375 на observations. Fresh packet дополнительно должен уменьшить финальный вызов, который сейчас стоит 104 567 input tokens. Именно здесь находится самый крупный и наиболее безопасный резерв оптимизации всего content pipeline.
