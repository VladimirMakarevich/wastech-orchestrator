# P5 — Кастомные tool-узлы (операторский Python через subprocess)

Статус: **backlog / инженерная спека, ОТЛОЖЕНО — не v1.** Не начинать до стабилизации P1–P4 (движок + целевой implementation + research/audit + операторская поверхность). Дата: 2026-06-17 Владелец: Vladimir Makarevich

Цель: дать оператору объявлять во Flow **кастомный узел**, вызывающий его собственную логику (Python или любой исполняемый файл), встроенную в пайплайн конкретного Flow — по аналогии с hooks в Claude Code (отдельная программа: контекст JSON на stdin → результат JSON на stdout). Это **вариант B** из обсуждения: tool исполняется **out-of-process под тем же потолком, что и агенты** (через `run_process`), а не привилегированным in-process кодом.

Эта фаза **расширяет палитру** новым видом узла `tool`. Поэтому она идёт **после** доказанной абстракции (P3) и операторской поверхности с валидатором-в-бою (P4): палитру расширяем только когда базовая абстракция стабильна.

## Зачем отдельной фазой и почему «заложить сейчас»

«Заложить сейчас» = **не закрыть дверь** при реализации P1–P4, а не писать код P5 сейчас. Конкретно P1–P4 должны сохранить швы (см. §«Швы, которые P1–P4 обязаны сохранить»), чтобы P5 «вставился» добавлением вида узла, а не переписыванием движка/валидатора. Сам код P5 — после стабилизации.

## Согласованность с инвариантом «нет узла-кода»

index.md §1 и security-ceiling.md §1 отвергают **узел = произвольный код в процессе ядра / inline** — это crewAI-дыра (RCE/sandbox-escape через `allow_code_execution`). P5 **не вводит** такой узел. `tool`-узел — это **типизированный узел, чья «механика» = запуск внешней программы под потолком** (как `agent`-узел запускает CLI-провайдера под потолком). Отличия, удерживающие инвариант:

- tool исполняется **out-of-process** через [`providers/process.py`](../../src/wastech_orchestrator/providers/process.py) `run_process`: argv-без-shell, **обязательный timeout**, **env-allowlist**, redaction, isolation-preflight — тот же потолок, что у агентов;
- tool **side-effect-free** относительно git/state: он **возвращает** outcome/данные/findings; commit/push/PR/запись в `state.db` применяет **только ядро** (инвариант «git только оркестратор»);
- tool получает **только allowlisted-контекст** (пути/метаданные), **никогда** секреты/env/raw session-id;
- tool **объявлен под валидатором** (allowlist зарегистрированных tool, path-containment, объявленный набор `outcome`); неизвестный tool → fail-closed.

Итог: палитра остаётся типизированной и закрытой; `tool` — ещё один **ceiling-bound** вид, не лазейка. In-process-вариант (A) сознательно **не** строится (CPython нельзя засендбоксить в процессе → это и была бы дыра).

---

## P5.1 — Вид узла `tool` (схема + загрузчик + валидатор)

### Touchpoints

- [`core/flow/schema.py`](../../src/wastech_orchestrator/core/flow/schema.py) — новый `ToolNode` (frozen dataclass); добавить в Union `FlowNode`.
- [`core/flow/snapshot.py`](../../src/wastech_orchestrator/core/flow/snapshot.py) — `_parse_tool_node` + `_TOOL_FIELDS` allowlist; ветка в `_parse_node`.
- [`core/flow/validator.py`](../../src/wastech_orchestrator/core/flow/validator.py) — allowed-outcome для `tool` (см. ниже); `tool` ∈ зарегистрированного allowlist (config-aware, через `validate_flow_against_config` из P4.2); path-containment ссылки на tool.

### Новый тип

```python
@dataclass(frozen=True, slots=True)
class ToolNode:
    id: str
    kind: Literal["tool"]
    tool: str                       # имя зарегистрированного tool (.worc/tools/<tool>...) — НЕ произвольный путь
    args: Mapping[str, object] = ...  # статические параметры из flow (allowlisted, без секретов)
    timeout_seconds: int | None = None
    when: WhenPredicate | None = None
```

### Allowed-outcome (без спец-кейса в движке)

`tool` эмитит `pass`/`fail` (детерминированный гейт, как `checks`) либо `route:<label>`; `route:*` всегда разрешён. Так движок (P1.1) не получает спец-логики: tool ложится в тот же edge-механизм. Узел-«просто произвести данные, всегда продолжить» эмитит `pass` при единственном `pass`-ребре. Fail-closed на неизвестном поле (`_reject_unknown`) и неизвестном `tool`.

### Тесты

- `test_tool_node_parses_and_validates`.
- `test_unknown_tool_rejected_fail_closed` — `tool` вне allowlist → фатально до запуска.
- `test_tool_outcome_subset` — исход ∈ `{pass, fail, route:*}`.
- `test_tool_path_containment` — ссылка с traversal → фатально.

### Exit

`tool`-узел грузится/валидируется как полноправный вид узла; неизвестный/небезопасный tool отвергается до ветки.

---

## P5.2 — Реестр и discovery операторских tools

### Touchpoints

- **Новый** `core/flow/tools_registry.py` — `ToolRegistry(tools_dir)` по образцу [`FlowRegistry`](../../src/wastech_orchestrator/core/flow/registry.py): operator-слой `<repo>/.worc/tools/`. Резолвит `tool`-имя → исполняемый файл; fail-closed если не найден/не исполняемый.
- `install`/`preflight` — валидирует реестр tools вместе с flow-файлами (всё фатально до запуска, как сейчас валидируются flow).

### Поведение

- Доверие — **файловое** (как операторский flow и `config.yaml`): tool лежит в `.worc/tools/`, владелец — оператор. Подпись/хеш-реестр — отложено (то же решение, что для flow, security-ceiling.md §8).
- Реестр отдаёт **только** зарегистрированные имена; flow не может сослаться на произвольный путь (path-containment + allowlist).

### Тесты

- `test_tool_registry_resolves_operator_tool`.
- `test_tool_registry_unknown_fails_before_side_effects`.
- `test_preflight_validates_tools`.

### Exit

Операторские tools обнаруживаются и валидируются как часть операторской поверхности.

---

## P5.3 — Runner `tool`-узла (исполнение под потолком)

### Touchpoints

- **Новый** `core/flow/nodes/tool.py` — реализует `NodeRunner` (протокол из P1.1).
- Переиспользует: [`providers/process.py`](../../src/wastech_orchestrator/providers/process.py) `run_process` (argv-без-shell, timeout, stdout→файл); [`security/env.py`](../../src/wastech_orchestrator/security/env.py) `build_child_env`; [`security/isolation.py`](../../src/wastech_orchestrator/security/isolation.py); [`providers/redaction.py`](../../src/wastech_orchestrator/providers/redaction.py); [`providers/errors.py`](../../src/wastech_orchestrator/providers/errors.py) `classify`.

### JSON-контракт (как у CC-hooks)

**Вход (stdin)** — только allowlisted-контекст (тот же принцип, что `ALLOWED_PROMPT_VARS`):

```json
{
  "task_id": "...",
  "node_id": "...",
  "subtask_order": null,
  "paths": {
    "repo": "...",
    "task_path": "...",
    "plan_path": "...",
    "diff_path": "...",
    "checks_path": "...",
    "review_path": "..."
  },
  "args": { "<из flow node.args>": "..." }
}
```

Никаких секретов/env/raw session-id в контексте.

**Выход (stdout)**:

```json
{ "outcome": "pass" | "fail" | "route:<label>",
  "data": { ... }, "findings": [ { "severity": "...", "reason": "...", "paths": ["..."] } ],
  "message": "..." }
```

**Семантика исхода/ошибок**:

- чистый exit (0) + парсимый stdout → `outcome` авторитетен;
- timeout / launch-error → **инфра-путь** (как `checks` launch_failed: не quality-fail; ретрай по политике, не тратит fix-итерацию);
- чистый exit, но невалидный/отсутствующий stdout → **fail-closed** (узел = ошибка → объявленное `fail`-ребро или manual);
- `data`/`findings` — **возвращаются ядру**; tool их **не** применяет (никакого git/FS-сайд-эффекта вне политики).

### Поведение

- Контекст собирается ядром (allowlisted-пути) — переиспользует тот же сбор, что prompt-vars (p1-engine.md §P1.3).
- Запуск под `run_process` + env-allowlist + isolation; stdout/stderr → артефакты после redaction; чекпоинт `node_run` (как у любого узла, P1.2).
- Исход → ребро движком (P1.1), без спец-кейса.

### Тесты (контракт + угрозы)

- `test_tool_runs_under_run_process_argv_no_shell`.
- `test_tool_context_has_no_secrets_or_env` — на stdin только allowlisted-пути.
- `test_tool_cannot_perform_git_or_state_writes` — сайд-эффекты git/state применяет только ядро; tool их не делает.
- `test_tool_timeout_is_infra_not_quality_fail`.
- `test_tool_malformed_stdout_fail_closed`.
- `test_tool_runs_under_isolation_and_env_allowlist`.
- `test_tool_network_within_policy` — сеть не шире `network_policy` flow.

### Exit

Операторский tool исполняется как узел под полным потолком; side-effect-free; интегрирован в edge-механизм движка.

---

## P5.4 — Docs + housekeeping

- flow-contract.md §2 — добавить `tool` в палитру (доменный, но ceiling-bound; механика = subprocess ядра).
- security-ceiling.md §1/§3 — `tool` в allowlist полей + модель угроз (tool не пробивает потолок).
- index.md §1 — каузат: «узел-код в процессе» отвергнут; «sandboxed external tool под потолком» = санкционированное расширение (P5).
- configuration / how-it-works — `.worc/tools/` раскладка, JSON-контракт.

---

## Швы, которые P1–P4 обязаны сохранить (чтобы P5 «вставился»)

Это и есть «заложить возможность сейчас» — без кода P5, но не закрывая дверь:

1. **Диспетч движка по `kind` — реестр, не хардкод.** P1.1 держит `dict[str, NodeRunner]`; добавление `tool` = новая запись, не правка ветвлений. (Уже так в спеке.)
2. **Allowed-outcome и field-allowlist — таблицы per-kind.** P0.3/P0.5 держат `_EVAL_*`/`_CHECKS_OUTCOMES`/`*_FIELDS` как таблицы; добавление `tool` = новая запись. (Уже так.)
3. **`NodeOutcome`/`NodeResult` — kind-агностичны.** P1.1 не зашивает в контракт перечень видов.
4. **Сбор allowlisted-контекста — переиспользуемый.** P1.3 prompt-vars и P5 tool-context используют один и тот же ядровой allowlist-сборщик; не дублировать.
5. **Паттерн «checker-реестр» (P3.1) — шаблон для tool-реестра.** Не изобретать второй механизм discovery.
6. **`validate_flow_against_config` (P4.2) — точка, куда добавится tool-allowlist.** Config-aware валидатор уже отделён (P4.2).

Если P1–P4 эти швы сохраняют, P5 — ~неделя работы (оценка из обсуждения: доминирует контракт безопасности + тесты угроз, не механика), без переписывания движка.

## Открытые вопросы (фиксируются при планировании P5, не сейчас)

Преждевременно решать для отложенной фазы; рекомендованные дефолты:

- **Формат `args`**: плоский `Mapping[str, scalar]` (рекоменд.) vs вложенный JSON. Дефолт — плоский allowlisted, без секретов.
- **Язык tool**: контракт **language-agnostic** (любой исполняемый файл по JSON-stdin/stdout); Python — частый случай, не требование. (Согласуется с argv-без-shell.)
- **Гранулярность outcome**: `pass`/`fail` + `route:*` (рекоменд., без нового поля-режима) vs декларируемый режим узла. Дефолт — без режима.
- **Реестр**: файловый `.worc/tools/` (рекоменд., как flow) vs версионируемый реестр. Дефолт — файловый; подпись отложена.

Эти развилки выносятся через вопрос-tool с рекомендациями, когда P5 будет запланирован к исполнению.

## Контракт места в плане

P5 — **пост-стабилизационное** расширение палитры, **вне объёма v1**. Предусловие: P1–P4 зелены и стабильны. Не блокирует и не блокируется P1–P4, кроме сохранения швов выше.
