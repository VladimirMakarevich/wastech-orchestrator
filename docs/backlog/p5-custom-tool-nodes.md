# P5 — Кастомные tool-узлы (операторский исполняемый файл через subprocess)

Статус: **accepted — готово к реализации.** Предусловие (P1–P4 зелены и стабильны + подсистема памяти в `main`) выполнено на 2026-07-08. Спека была заложена 2026-06-17 (отложена до стабилизации палитры) и доведена до accepted 2026-07-08. Владелец: Vladimir Makarevich.

Цель: дать оператору объявлять во Flow **кастомный узел**, вызывающий его собственную программу (любой исполняемый файл — Python частый случай, но не требование), встроенную в пайплайн конкретного Flow — по аналогии с hooks в Claude Code (внешняя программа: контекст на stdin → результат через exit-код и/или JSON на stdout). tool исполняется **out-of-process под тем же потолком доверия, что и агенты** (через `run_process`), а не привилегированным in-process кодом. Это осознанно **вариант B** из обсуждения (in-process-вариант A не строим — CPython нельзя засендбоксить в процессе ядра).

Эта фаза **расширяет палитру** новым видом узла `tool`. Поэтому она идёт **после** доказанной абстракции (P3) и операторской поверхности с валидатором-в-бою (P4): палитру расширяем только когда базовая абстракция стабильна.

## Резолюция развилок (2026-07-08)

Все «Открытые вопросы» исходной спеки закрыты, плюс уточнён контракт исхода и исправлены три неточно описанных шва. Ниже — итоговые решения, они авторитетны для реализации.

| # | Решение | Обоснование |
| --- | --- | --- |
| Инфра-сбой (timeout / launch-error) | → `NodeManualRequired` → задача встаёт на `manual_action_required`. **НЕ** quality-fail и **не** тратит fix-итерацию. | Зеркалит единственный существующий subprocess-гейт (`checks` command_profile). Fail-safe, без новой retry-машинерии. Фраза исходной спеки «ретрай по политике» удалена — она ни на что существующее не ложилась. |
| `data`/`findings` | **Только записать** (вариант A): stdout → артефакт (после redaction), `findings` → `NodeOutcome.findings`, `data` → `NodeOutcome.structured_output`. Персистятся и видны человеку/супервизору. **НЕ** вливаются автоматически в fix-loop. | Ядро остаётся генерик-ом (не знает про форму графа). Проводка findings в downstream-агента — операторская, через `{<node_id>_path}` (см. следующую строку). Тот же механизм обслуживает и «tool как чек с возвратом на доработку», и «tool просто производит файл». Авто-вброс в fix-loop — отложенный follow-up. |
| Выход tool как prompt-переменная | **Да**: stdout-артефакт tool-узла экспонируется как `{<node_id>_path}` для последующих узлов, симметрично agent-узлам. | Дёшево (расширение `node_output_vars`), делает tools композируемыми: tool-чек → `fail`-ребро → `fix`-агент читает `{<node_id>_path}` и исправляет; либо tool-продюсер → downstream-агент читает его выход. |
| Формат `args` | Плоский `Mapping[str, scalar]` (str/int/float/bool), без секретов. | Дефолт исходной спеки. Согласуется с allowlisted-статикой из flow. |
| Язык tool | Language-agnostic: любой исполняемый файл по контракту stdin/exit-код/stdout. | argv-без-shell не зависит от языка. |
| Гранулярность исхода | `pass`/`fail` + `route:*`, без нового поля-режима. | Ложится в тот же edge-механизм, что `checks`; движок без спец-кейса. |
| Реестр | Файловый `.worc/tools/` (доверие — файловое, как flows и config.yaml). Подпись/хеш-реестр отложены. | То же решение, что для flow (security-ceiling.md §8). |

### Контракт исхода (уточнён относительно исходной спеки)

Гейт ведёт **exit-код** (как `command_profile`), JSON на stdout **необязателен**. Точный приоритет:

1. **launch-error / timeout** → инфра-путь → `NodeManualRequired`. exit_code при этом `None` (см. [`providers/errors.py`](../../src/wastech_orchestrator/providers/errors.py) `classify`). Наивысший приоритет.
2. Иначе, если stdout парсится как JSON-объект с ключом `outcome` → он **авторитетен**; значение ∈ `{pass, fail, route:<label>}`, иначе **fail-closed** (ошибка узла → объявленное `fail`-ребро/manual). `findings`/`data` из того же объекта читаются.
3. Иначе (нет JSON-`outcome`) → решает **exit-код**: `0` → `pass`, `≠0` → `fail`. Если stdout всё же распарсился в объект — `findings`/`data` берутся для обогащения (но исход уже определён кодом).

Так оператор получает два эргономичных стиля, оба покрывают приведённые кейсы:

- **Линтер-стиль** (кейс «regex/char-count чекер `.md`»): просто `exit 0/≠0`, stdout игнорируется как исход, но сохраняется как артефакт. Минимум усилий.
- **Богатый стиль**: JSON `{outcome, findings, data}` — нужен для `route:*` и структурных findings.

## Согласованность с инвариантом «нет узла-кода» и модель доверия

index.md §1 и security-ceiling.md §1 отвергают **узел = произвольный код в процессе ядра / inline** (crewAI-дыра: RCE через `allow_code_execution`). P5 **не вводит** такой узел. `tool`-узел — типизированный узел, чья «механика» = запуск внешней программы **out-of-process**, ровно как `agent`-узел запускает CLI-провайдера. Инвариант удерживают:

- запуск через [`providers/process.py`](../../src/wastech_orchestrator/providers/process.py) `run_process`: **argv-без-shell**, **обязательный timeout**, `env` = ровно переданная allowlist-карта (родительское окружение не наследуется);
- **git/state пишет только ядро**: tool **возвращает** outcome/findings/data, а commit/push/PR/запись в `state.db` делает исключительно оркестратор (инвариант «git только оркестратор»);
- tool получает на stdin **только allowlisted-контекст** (пути/метаданные), **никогда** секреты/сырой session-id;
- tool **объявлен под валидатором** (allowlist зарегистрированных имён из `ToolRegistry`, разрешён набор `outcome`); неизвестный tool → fail-closed **до** запуска.

**Честная граница v1 (важно, не переобещать):** произвольный исполняемый файл **не** песочится на уровне ФС/сети так, как codex/claude (те сами себя ограничивают собственными флагами — `--sandbox`, permission-mode). Реальный «потолок» tool в v1 = **файловое доверие** (оператор владеет `.worc/tools/`, как владеет flows/role-prompts/config.yaml — где он и так мог бы навредить) + `env`-allowlist (секреты не утекают в процесс) + redaction артефактов + обязательный timeout + отсутствие в ядре кода, применяющего git/state по результату tool. Следствия:

- **Сеть не является жёстким гейтом для tool** — technically tool может открыть сокет. Поэтому `network_policy` на tool в v1 **не форсится** (в отличие от агентов). OS-уровневая песочница (container/seccomp/netns) для tool — **отложено**; заявлять «сеть не шире flow» для произвольного бинаря было бы ложью.
- **«side-effect-free относительно git/state» — это свойство контракта, а не песочницы:** ядро не даёт tool git-креды (env-allowlist) и не имеет пути, где возвращённые tool-ом значения триггерят git/state-запись. Malicious tool не блокируется движком — блокируется файловым доверием (та же модель, что для role-prompt агента).

Итог: палитра остаётся типизированной и закрытой; `tool` — ещё один **trust-bound** вид под тем же процессным потолком, что агенты, а не лазейка в процесс ядра.

---

## P5.1 — Вид узла `tool` (схема + загрузчик + валидатор)

### Touchpoints

- [`core/flow/schema.py`](../../src/wastech_orchestrator/core/flow/schema.py) — новый `ToolNode` (frozen/slots dataclass); добавить в Union `FlowNode` (schema.py:126).
- [`core/flow/snapshot.py`](../../src/wastech_orchestrator/core/flow/snapshot.py) — `_parse_tool_node` + `_TOOL_FIELDS` allowlist; новая ветка `elif kind == "tool"` в `_parse_node` (snapshot.py:428). `args` парсится как плоский scalar-mapping (не-скаляр → `FlowLoadError`).
- [`core/flow/validator.py`](../../src/wastech_orchestrator/core/flow/validator.py) — новая таблица `_TOOL_OUTCOMES = frozenset({"pass", "fail"})` + ветка `tool` в kind-диспетче `_check_graph` (validator.py:213); `route:*` уже всегда разрешён (validator.py:220). Проверка `node.tool ∈ ToolRegistry` — в config-aware `validate_flow_against_config` (validator.py:103), рядом с проверками provider/reasoning (это шов P4.2).
- [`core/flow/engine.py`](../../src/wastech_orchestrator/core/flow/engine.py) — ветка `tool → "pass"` в `skip_outcome` (engine.py:97), чтобы пропущенный по `when` tool-узел давал корректный skip-исход. Диспетч рантайма (`_runners.get(node.kind)`, engine.py:328) уже kind-агностичен — правки не требует.
- [`config/schema.py`](../../src/wastech_orchestrator/config/schema.py) — новый опциональный блок `tools` с полем `default_timeout_seconds: int = 3600` (1 час). Блок необязателен: при его отсутствии применяется дефолт. Требует bump версии config-схемы + правку `config_writer` (`build_config_mapping`) и shipped-доков (`config.example.yaml`, `guide/`).

### Новый тип

```python
@dataclass(frozen=True, slots=True)
class ToolNode:
    id: str
    kind: Literal["tool"]
    tool: str                                   # имя зарегистрированного tool (.worc/tools/<tool>) — НЕ путь; резолвит ToolRegistry
    args: Mapping[str, str | int | float | bool] = field(default_factory=dict)  # плоский allowlisted-скаляр, без секретов
    timeout_seconds: int | None = None          # None → config tools.default_timeout_seconds (по умолч. 3600)
    when: WhenPredicate | None = None
```

Отличие от `ChecksNode.checker`: поле `checker` — закрытый `Literal` (core-owned словарь), а `tool` — **свободная строка** (операторский открытый набор, как имя flow), валидируемая против `ToolRegistry` при config-aware валидации. Path-containment обеспечивает сам реестр (имя → файл внутри `.worc/tools/`), а не проверка строки на `..`.

**Резолюция таймаута (два уровня контроля у оператора):** приоритет `node.timeout_seconds` → `config.tools.default_timeout_seconds` → встроенный фолбэк `3600` c (1 час). То есть: узел может переопределить точечно; глобальный дефолт для всех tool-узлов оператор задаёт одним ключом в `config.yaml`; если ни то, ни другое не задано — 1 час. Ключ `tools.default_timeout_seconds` **опционален** (блок `tools` можно не писать). Резолвится один раз в runner-е и передаётся в `run_process` как обязательный `int` (в `run_process` timeout всегда обязателен).

### Тесты

- `test_tool_node_parses_and_validates`.
- `test_unknown_tool_rejected_fail_closed` — `tool` вне реестра → фатально в `validate_flow_against_config`, до запуска.
- `test_tool_outcome_subset` — исход ∈ `{pass, fail, route:*}`; невалидное значение в JSON `outcome` → fail-closed.
- `test_tool_args_must_be_flat_scalars` — вложенный/не-скалярный `args` → `FlowLoadError`.
- `test_reject_unknown_tool_field` — неизвестное поле узла → fail-closed (`_reject_unknown`).
- `test_tool_timeout_resolution_precedence` — node override > `config.tools.default_timeout_seconds` > фолбэк 3600; отсутствие блока `tools` → 3600.

### Exit

`tool`-узел грузится/валидируется как полноправный вид узла; неизвестный tool отвергается до ветки графа.

---

## P5.2 — Реестр и discovery операторских tools

### Touchpoints

- **Новый** `core/flow/tools_registry.py` — `ToolRegistry(tools_dir)` **по образцу [`FlowRegistry`](../../src/wastech_orchestrator/core/flow/registry.py)** (не по образцу checker-механизма — реестра checker-ов не существует, это закрытый Literal + хардкод). Операторский слой `<repo>/.worc/tools/`. Резолвит `tool`-имя → путь к исполняемому файлу; fail-closed если не найден / не исполняемый / вне `tools_dir`.
- `install`/`preflight` — валидирует реестр tools вместе с flow-файлами (всё фатально до запуска, как сейчас валидируются flow через `validate_all`).

### Поведение

- Доверие — **файловое** (как операторский flow и `config.yaml`): tool лежит в `.worc/tools/`, владелец — оператор. Подпись/хеш-реестр — отложено (то же решение, что для flow, security-ceiling.md §8).
- Реестр отдаёт **только** зарегистрированные имена; flow не может сослаться на произвольный путь (containment внутри `tools_dir` + allowlist имён).
- Кросс-платформенность: «исполняемость» проверяется по-разному на POSIX (бит `x`) и Windows (расширение/загрузчик). Реестр хранит и сравнивает пути через `Path.as_posix()`.

### Тесты

- `test_tool_registry_resolves_operator_tool`.
- `test_tool_registry_unknown_fails_before_side_effects`.
- `test_tool_registry_rejects_path_outside_dir` — traversal/симлинк наружу → fail-closed.
- `test_preflight_validates_tools`.

### Exit

Операторские tools обнаруживаются и валидируются как часть операторской поверхности.

---

## P5.3 — Runner `tool`-узла (исполнение под потолком)

### Touchpoints

- **Новый** `core/flow/nodes/tool.py` — реализует `NodeRunner` (протокол `run(node, ctx) -> NodeResult`, engine.py:130). Регистрируется новым ключом `"tool"` в `build_node_runners` ([engine_driver.py:114](../../src/wastech_orchestrator/core/flow/engine_driver.py#L114)). Ближайший образец — `ChecksNodeRunner`.
- **Новый** `core/flow/context_paths.py` (шов #4) — вынести сбор allowlisted-путей из приватного `AgentNode._prompt_variables` в чистую standalone-функцию `build_path_context(inputs, repo_dir, node_id, ...) -> dict[str, str | None]`, которую переиспользуют **и** `_prompt_variables` (рефактор, поведение агента не меняется), **и** tool-runner для сборки stdin. Не дублировать allowlist путей.
- Переиспользует: `run_process` (argv-без-shell, mandatory timeout, `stdin_text`, `stdout_path`); [`security/env.py`](../../src/wastech_orchestrator/security/env.py) `build_child_env`; [`security/isolation.py`](../../src/wastech_orchestrator/security/isolation.py) (preflight-гейт, как у всех); [`providers/redaction.py`](../../src/wastech_orchestrator/providers/redaction.py) `redact_text`; [`providers/errors.py`](../../src/wastech_orchestrator/providers/errors.py) `classify`.

### JSON-контракт (как у CC-hooks)

**Вход (stdin)** — только allowlisted-контекст (тот же источник, что prompt-vars, через общий `build_path_context`):

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

Никаких секретов/сырого session-id/полного env в контексте.

**Выход** — exit-код и (необязательно) JSON на stdout, по контракту исхода из «Резолюции развилок»:

```json
{ "outcome": "pass" | "fail" | "route:<label>",
  "data": { ... },
  "findings": [ { "severity": "...", "reason": "...", "paths": ["..."] } ],
  "message": "..." }
```

**Обработка результата** (Q1 + Q2 + контракт исхода):

- launch-error / timeout → `NodeManualRequired` (инфра, не quality-fail, не тратит fix-итерацию);
- иначе исход по приоритету exit-код / JSON-`outcome` (см. выше); неизвестное значение `outcome` → fail-closed;
- `findings` → `NodeOutcome.findings`; `data` → `NodeOutcome.structured_output`; **ядро их не применяет** (никакого git/FS-сайд-эффекта вне политики) — только записывает;
- stdout/stderr → артефакты `tools/<node_id>/` **после redaction**; чекпоинт `node_run` (как у любого узла, P1.2);
- stdout-артефакт регистрируется как `{<node_id>_path}` (Q3) — расширить `node_output_vars`/`valid_prompt_vars` ([core/flow/prompt_vars.py](../../src/wastech_orchestrator/core/flow/prompt_vars.py)) так, чтобы tool-узлы (наравне с agent) отдавали выходной путь downstream-узлам.

### Тесты (контракт + угрозы)

- `test_tool_runs_under_run_process_argv_no_shell`.
- `test_tool_stdin_has_no_secrets_or_full_env` — на stdin только allowlisted-пути + `args`.
- `test_tool_child_env_is_allowlisted` — `build_child_env`, секретов нет.
- `test_core_ignores_tool_git_state_side_effects` — runner не имеет пути, где возвращённые значения триггерят git/state-запись; git-креды в env не передаются.
- `test_tool_timeout_and_launch_error_go_manual` — оба инфра-исхода → `NodeManualRequired`, не quality-fail.
- `test_tool_exit_code_gates_pass_fail` — линтер-стиль без JSON: exit 0 → pass, ≠0 → fail.
- `test_tool_json_outcome_authoritative_and_route` — JSON `outcome` перекрывает exit-код; `route:*` работает.
- `test_tool_malformed_json_outcome_fail_closed`.
- `test_tool_findings_and_data_recorded_not_applied` — findings→NodeOutcome, data→structured_output; в fix-loop не вливаются.
- `test_tool_output_exposed_as_node_path_var` — `{<node_id>_path}` виден downstream-узлу.
- `test_tool_artifacts_redacted`.

Тест `test_tool_network_within_policy` из исходной спеки **исключён** — сеть для произвольного бинаря в v1 не форсится (см. модель доверия). Вместо него — `test_tool_child_env_is_allowlisted`.

### Exit

Операторский tool исполняется как узел под процессным потолком (argv-без-shell, timeout, env-allowlist, redaction); контрактно side-effect-free по git/state; исход интегрирован в edge-механизм движка без спец-кейса; выход композируем через `{<node_id>_path}`.

---

## P5.4 — Docs + housekeeping

- flow-contract.md §2 — добавить `tool` в палитру (доменный, но trust-bound; механика = subprocess ядра; контракт exit-код/JSON).
- security-ceiling.md §1/§3 — `tool` в allowlist полей + модель угроз с **честной границей** (файловое доверие + env-allowlist; сеть/ФС не песочатся на OS-уровне в v1).
- index.md §1 — каузат: «узел-код в процессе» отвергнут; «внешний tool под процессным потолком + файловое доверие» = санкционированное расширение (P5).
- configuration / how-it-works — `.worc/tools/` раскладка, контракт stdin/exit-код/stdout, `{<node_id>_path}`-композиция, ключ `tools.default_timeout_seconds` (опциональный, дефолт 3600 c) и приоритет node-override → config → фолбэк.
- Операторские shipped-доки под `src/wastech_orchestrator/packaged/` (`guide/`, `config.example.yaml`, built-in flows) — пример tool-узла / `.worc/tools/` в quickstart + строка `tools.default_timeout_seconds` в `config.example.yaml`.
- `docs/backlog/follow_ups.md` — занести отложенное: подпись/хеш-реестр tools, авто-вброс findings в fix-loop, OS-уровневая песочница (сеть/ФС) для tool.

---

## Швы, которые P1–P4 сохранили (сверено с кодом 2026-07-08)

Проверено разведкой по коду; статус каждого шва:

1. **Диспетч движка по `kind` — реестр.** ✅ `FlowEngine._runners: Mapping[str, NodeRunner]`, собирается в `build_node_runners` (engine_driver.py:114); рантайм-lookup `_runners.get(node.kind)` (engine.py:328) kind-агностичен. Добавление `tool` = новая запись в этой карте.
2. **Field-allowlist per-kind — таблицы; outcome-выбор и парсер — if/elif.** ⚠️ Честная правка: `*_FIELDS` — frozenset-ы (чисто additive), **но** `_parse_node` (snapshot.py:428), выбор outcome-таблицы в `_check_graph` (validator.py:213) и `skip_outcome` (engine.py:97) — это `if/elif` по `kind`. Добавление `tool` = три маленькие локализованные ветки + `_TOOL_FIELDS` + `_TOOL_OUTCOMES`. Не «одна запись», но и не переписывание движка.
3. **`NodeOutcome`/`NodeResult` — kind-агностичны.** ✅ `NodeOutcome.kind: str`; уже несёт `findings` и `structured_output` — прямые слоты для tool-результата. Движок не свитчится по виду узла.
4. **Сбор allowlisted-контекста — переиспользуемый.** ⚠️ Сейчас реальный сборщик путей — **приватный** `AgentNode._prompt_variables`, завязан на `self._in`/`self._s`. Чтобы «не дублировать», P5.3 **выносит** сбор путей в standalone `core/flow/context_paths.py` (мелкий рефактор без смены поведения агента), общий для prompt-vars и tool-stdin.
5. **Шаблон реестра.** ❌ Исправление исходной спеки: **реестра checker-ов не существует** (это закрытый `Literal` + хардкод if/elif + core-owned функции). Правильный шаблон для `ToolRegistry` — **`FlowRegistry`** (операторский discovery в `.worc/flows/`). Следствие: `tool`-поле — свободная строка, валидируемая против реестра в `validate_flow_against_config`.
6. **`validate_flow_against_config` (P4.2) — точка для tool-allowlist.** ✅ Существует (validator.py:103); проверка `node.tool ∈ ToolRegistry` добавляется рядом с provider/reasoning-проверками.

Итог сверки: движок переписывать не нужно; P5 «вставляется» как заявлено, с поправкой на швы #2/#4/#5. Оценка объёма — доминирует контракт безопасности + тесты угроз, не механика.

## Отложено (follow-ups, вне v1)

- Подпись / хеш-реестр операторских tools (то же решение, что для flow).
- Авто-вброс `findings` tool-а в fix-loop (вариант B) — если появится спрос; в v1 проводка операторская через `{<node_id>_path}`.
- OS-уровневая песочница (container/seccomp/netns) для форсинга сети/ФС произвольного бинаря.

## Контракт места в плане

P5 — пост-стабилизационное расширение палитры. Предусловие (P1–P4 зелены + память в `main`) **выполнено**; фаза переведена в реализацию. Не блокирует и не блокируется другими фазами, кроме уже сохранённых швов выше.

---

## Как это уживётся, переключается и управляется (простыми словами + примеры)

Короткая суть: `tool`-узел — это **ещё один вид узла в твоём flow-графе**, наравне с `agent`/`checks`/`evaluator`/`hitl`/`publish`. Он ничем не «особенный» для движка: соединяется теми же рёбрами, даёт те же исходы (`pass`/`fail`/`route:*`), так же чекпоинтится и переживает resume. Разница только внутри узла: вместо LLM-провайдера ядро запускает **твою программу** из `.worc/tools/`.

### Где живёт и как включается

Никакого глобального рубильника нет. `tool` включается **точечно, в конкретном flow**:

1. Кладёшь исполняемый файл в `.worc/tools/<имя>` (на POSIX — `chmod +x`).
2. Добавляешь в свой flow-YAML узел `kind: tool` с этим именем и соединяешь рёбрами.

Всё. В другом flow этого узла может не быть — ядро ничего не навязывает.

### Как уживается с остальными узлами (когда что брать)

| Вид узла | Кто исполняет | Когда брать |
| --- | --- | --- |
| `agent` | CLI-провайдер (codex/claude), LLM | Нужна «умная» работа: писать код/текст, рассуждать, чинить по ревью. |
| `checks` | ядро, **фиксированный** набор (lint/test/citation/deps) | Стандартный quality-gate, который уже встроен. |
| `tool` | **твоя** программа (любой язык) | Своя детерминированная логика/проверка/продюсер, которой нет в `checks`. |

Правило выбора простое: **не-LLM и не входит в встроенные `checks` → это `tool`.** Умная работа → `agent`. Готовый gate → `checks`.

### Пример 1 — `tool` как чек с возвратом на доработку (твой кейс с `.md`)

`.worc/tools/md-check` (упрощённо; главное — контракт stdin/exit-код/stdout):

```python
#!/usr/bin/env python3
import json, sys, pathlib

ctx = json.load(sys.stdin)                 # контекст от ядра (пути + args), без секретов
args = ctx["args"]                         # {"min_chars": 500, "max_chars": 4000}
repo = pathlib.Path(ctx["paths"]["repo"])

findings = []
for f in changed_md_files(ctx):            # напр. из ctx["paths"]["diff_path"]
    text = (repo / f).read_text(encoding="utf-8")
    if len(text) < args["min_chars"]:
        findings.append({"severity": "error", "reason": "слишком мало символов", "paths": [f]})
    if "\n\n" not in text:
        findings.append({"severity": "error", "reason": "нет абзацев", "paths": [f]})

if findings:
    print(json.dumps({"outcome": "fail", "findings": findings}))
    sys.exit(1)                            # линтер-стиль: даже без JSON exit≠0 дал бы fail
print(json.dumps({"outcome": "pass", "data": {"files_scanned": len(changed_md_files(ctx))}}))
```

Кусок flow-YAML — `fail` уводит на агента-правщика, тот читает findings и правит, потом перепроверка:

```yaml
nodes:
  - id: md-check
    kind: tool
    tool: md-check # → .worc/tools/md-check
    args: { min_chars: 500, max_chars: 4000 }
  - id: fix
    kind: agent
    role_file: roles/fix.md # в промпте есть ссылка на {md-check_path}
edges:
  - { from_node: md-check, to: publish, outcome: pass }
  - { from_node: md-check, to: fix, outcome: fail }
  - { from_node: fix, to: md-check } # безусловно — назад на перепроверку
```

В `roles/fix.md` агент видит **ровно те findings** — через путь к выходу tool-а:

```text
Ниже — замечания автоматической проверки. Исправь перечисленные файлы.
Отчёт проверки: {md-check_path}
```

Именно так «вернуть на доработку агенту, чтобы он исправил» и работает: findings доходят до агента одной строкой-ссылкой (`{md-check_path}`), а не магией ядра.

### Пример 2 — `tool` как продюсер файла для следующего узла

Иногда проверка не нужна — tool просто **готовит данные** для следующего агента:

```yaml
nodes:
  - id: fetch-glossary
    kind: tool
    tool: fetch-glossary # пишет glossary.json в свой выходной артефакт
  - id: write-chapter
    kind: agent
    role_file: roles/author.md # промпт ссылается на {fetch-glossary_path}
edges:
  - { from_node: fetch-glossary, to: write-chapter } # безусловно, идём дальше
```

Тот же механизм `{<node_id>_path}`, только ребро ведёт не в `fix`, а в обычного downstream-агента. Здесь tool просто вернёт `pass` (или exit 0) — граф идёт дальше.

### Пример 3 — `tool` как маршрутизатор (`route:*`, «богатый» стиль)

Когда развилок больше двух, tool печатает JSON с `route:<label>`:

```yaml
edges:
  - { from_node: triage, to: quick-fix, outcome: "route:small" }
  - { from_node: triage, to: deep-work, outcome: "route:large" }
  - { from_node: triage, to: publish, outcome: pass }
```

`.worc/tools/triage` печатает, например, `{"outcome": "route:large"}` — движок уводит по ребру `route:large`. Никакого спец-кейса в ядре: `route:*` — тот же edge-механизм, что и у остальных узлов.

### Как переключать и управлять

- **Включить:** файл в `.worc/tools/<имя>` + узел `kind: tool` в flow.
- **Выключить:** убрать узел из flow. Совсем.
- **Условно включить/пропустить:** `when:` по факту — узел выполнится только при совпадении; пропущенный `tool` даёт skip-исход `pass`, и граф идёт по `pass`-ребру:
  ```yaml
  - id: md-check
    kind: tool
    tool: md-check
    when: { fact: config.strict_prose, equals: true }
  ```
- **Таймаут (два уровня):** `timeout_seconds` на узле переопределяет точечно; глобальный дефолт для всех tool-узлов — опциональный `tools.default_timeout_seconds` в `config.yaml`; если ничего не задано — **1 час**. Timeout ⇒ инфра-путь ⇒ задача встаёт на `manual_action_required` (не «провал качества»).
  ```yaml
  # config.yaml — глобальный дефолт для всех tool-узлов (опционально; без него = 3600)
  tools:
    default_timeout_seconds: 1800 # 30 минут на любой tool, если узел не задал своё
  ```
  ```yaml
  # flow.yaml — точечное переопределение на конкретном узле
  - id: heavy-scan
    kind: tool
    tool: heavy-scan
    timeout_seconds: 7200 # 2 часа только этому узлу
  ```
- **Разные flow — разный набор tool:** в одном flow tool есть, в другом нет. Управление — на уровне выбора/редактирования flow-YAML, как и для всех прочих узлов.
- **Валидация до запуска:** неизвестное имя tool (нет в `.worc/tools/`), неизвестное поле узла или недопустимый исход — **фатально на preflight**, задача даже не стартует.

### Что tool **может** (возможности)

- Быть на **любом языке** — контракт «stdin → exit-код/stdout», а не Python.
- Читать репозиторий и allowlisted-контекст (пути к task/plan/diff/checks/review).
- Гейтить пайплайн: `pass`/`fail` (exit-код или JSON) и разветвлять `route:*`.
- Вернуть **структурные** `findings` и `data` (сохраняются, видны в сводке и супервизору).
- Отдать свой выход downstream-узлам как `{<node_id>_path}` — цепочки tool → agent.
- Работать под обязательным таймаутом и с redaction артефактов.

### Что tool **не может** (границы v1)

- **Делать git** (commit/push/PR) и писать в `state.db` — это исключительно ядро. Tool только **возвращает** результат.
- Получить **секреты / полный env / сырой session-id** — на stdin только allowlisted-пути; в процесс идёт лишь env-allowlist.
- **Патчить граф** — tool не меняет flow, он его узел.
- **Автоматически** влить свои findings в fix-loop — проводка в правщика **операторская**, через `{<node_id>_path}` (авто-вброс — отложенный follow-up).
- Рассчитывать на **OS-песочницу** ФС/сети. Произвольный бинарь **не** сендбоксится, как codex/claude. Безопасность держится на **файловом доверии** (ты владеешь `.worc/tools/`, как владеешь flow и config.yaml) + env-allowlist. `network_policy` на tool в v1 **не форсится** — если нужна жёсткая изоляция сети, это OS/контейнер-уровень (отложено).
