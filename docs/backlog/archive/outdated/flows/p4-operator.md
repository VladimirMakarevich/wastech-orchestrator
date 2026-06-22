# P4 — Операторская поверхность (валидатор C в бою)

Статус: **✓ Выполнено (2026-06-21)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

> **Реализовано (2026-06-21).** P4.1 (приём операторских flow на боевом пути + `validate_all` на install/preflight), P4.2 (`validate_flow_against_config` + модель угроз как тесты `tests/core/test_flow_threat_model.py` + recovery-перепроверка), P4.3 (docs). **Две зафиксированные правки объёма по факту реализации** (см. [follow_ups.md](../follow_ups.md) строки config-aware / budgets): (1) `budgets ≤ config-cap` НЕ делается фатальной проверкой — авторитет у рантайм-клампа `min(flow, cap)` в `engine.py` (фатальная проверка забрикала бы встроенный flow при сужении cap'а оператором — а сужение безопасно); (2) `publishing != none → git` НЕ фатально — `create_pull_request: false` = поддержанный local-commit режим. `model`-allowlist не вводится (в конфиге один `model` на провайдер, не список). Остальное реализовано как описано ниже.

Детализация фазы P4 из [plan.md](plan.md). Цель: оператор пишет свой flow; **потолок держится**. Адаптирует [security-ceiling.md](security-ceiling.md). База проверки фазы: каждая угроза из [security-ceiling §1](security-ceiling.md) отбита тестом; потолок не пробивается ни данными flow, ни задачей.

Вход: реестр ([registry.py](../../../src/wastech_orchestrator/core/flow/registry.py)) уже умеет два слоя (operator→packaged); валидатор ([validator.py](../../../src/wastech_orchestrator/core/flow/validator.py)) уже проверяет граф + базовый потолок (clamp профиля, forbidden_args, path-traversal `role_file`); fail-closed на неизвестных полях добавлен в P0.5.

---

## P4.1 — Приём операторских flow

Реестр/раскладка операторских flow, `task_type`-диспетчеризация в них (поверх P0.4).

### Touchpoints

- [`core/flow/registry.py`](../../../src/wastech_orchestrator/core/flow/registry.py) — `FlowRegistry(operator_flows_dir)` уже резолвит `<operator_flows_dir>/<task_type>.yaml` с приоритетом над packaged (≈76 `_find`). P4.1 подключает `operator_flows_dir = <repo>/.worc/flows/` к боевому пути (install/watch/run), которого в P0.4 ещё не было (реестр существует, но не вызывается из orchestrator).
- Точка вызова: где задача резолвится в flow (после валидационного гейта, до branch-prep) — `FlowRegistry.resolve(task.task_type)` возвращает валидированный снапшот; снапшот персистится с `flow_fingerprint` (P1.2).
- `install`/`preflight` — фатальная валидация **всех** flow-файлов (packaged + операторские) до запуска ([happy-path.md](happy-path.md) §1).

### Поведение

- Задача несёт только `id`/`title`/`task_type`/`contacts`/`prompt_audit` ([flow-contract.md](flow-contract.md) §10); `task_type` диспетчеризуется в flow, **не выбирает flow из прозы** и **не патчит граф** (валидатор реестра уже отвергает несоответствие `task_type` в YAML ↔ имени файла, [registry.py](../../../src/wastech_orchestrator/core/flow/registry.py) ≈68).
- Операторский flow с тем же `task_type`, что встроенный, **переопределяет** встроенный (operator-приоритет).

### Тесты

- `test_operator_flow_resolves_and_executes` — кастомный flow из `.worc/flows/` резолвится, валидируется, исполняется.
- `test_operator_flow_overrides_builtin` — операторский `implementation.yaml` побеждает packaged.
- `test_preflight_validates_all_flows_fatally` — битый/небезопасный операторский flow роняет preflight до запуска.

### Exit

Кастомный flow запускается.

---

## P4.2 — Полный валидатор C на боевом пути

Deny-list в бою; модель угроз как тесты; recovery-перепроверка потолка (security только сужается). **Здесь закрывается отложенный из P0.5 пункт** — согласованность с `config.yaml`.

### Touchpoints

- [`core/flow/validator.py`](../../../src/wastech_orchestrator/core/flow/validator.py) — расширить `validate_flow` до **config-aware**: новая сигнатура `validate_flow(snapshot, *, config)` (или отдельный слой `_check_config_consistency`). Сейчас валидатор не видит конфиг.
- Переиспользует существующий потолок (не изобретает, [security-ceiling.md](security-ceiling.md) §6): [`security/forbidden_args.py`](../../../src/wastech_orchestrator/security/forbidden_args.py) `find_forbidden_args` (уже вызывается, [validator.py](../../../src/wastech_orchestrator/core/flow/validator.py) ≈231), профиль-маппинг в [`providers/claude.py`](../../../src/wastech_orchestrator/providers/claude.py)/[codex.py](../../../src/wastech_orchestrator/providers/codex.py), [`security/isolation.py`](../../../src/wastech_orchestrator/security/isolation.py) `check_isolation`, [`security/env.py`](../../../src/wastech_orchestrator/security/env.py) `build_child_env`, [`security/profiles.py`](../../../src/wastech_orchestrator/security/profiles.py) `is_same_or_stricter` (уже вызывается, ≈223).
- Recovery-перепроверка ([security-ceiling.md](security-ceiling.md) §7): на рестарте пересчитать `flow_fingerprint`, проверить существование видов узлов, **перепроверить потолок — security может только сузиться** (P1.2 recovery).

### Что добавляется (config-consistency, отложено из P0.5 #5)

- `model`/`reasoning`/провайдеры узлов ∈ операторского allowlist (`agents.allowed`, `agents.providers.<p>.{model,reasoning}` дефолты).
- `permission_ceiling` flow ≤ возможностей сконфигурированных провайдеров (`agents.providers.<p>.permission_profile`/`sandbox`).
- для `publishing != none` — git-конфиг пригоден (`git.create_pull_request`, `pr_base`).
- flow-`budgets` ≤ config-cap (`agents.max_fix_cycles`/`max_total_fix_iterations`) — наследие [p1-engine.md](p1-engine.md) §P1.1 OQ (flow не задаёт бюджет выше config-страховки).

### Модель угроз как тесты ([security-ceiling §1](security-ceiling.md))

Каждая строка таблицы угроз — отдельный тест, доказывающий, что вектор закрыт **независимо от содержимого YAML/MD/задачи**:

- `test_threat_privilege_escalation_profile_clamped` — `permission_profile` > ceiling → фатально (есть с P0.3).
- `test_threat_sandbox_bypass_forbidden_args` — `--dangerously*`/`--yolo`/`danger-full-access` в `extra_args` → фатально (есть с P0.3).
- `test_threat_direct_base_commit_blocked` — publish в base / обход идемпотентности невозможен (publish core-owned).
- `test_threat_disable_security_gate_impossible` — нельзя убрать dangerous-diff / сделать `checks` неавторитетным / дать evaluator write (evaluator read-only — есть с P0.3).
- `test_threat_write_outside_output_policy` — запись вне `output_policy` → отказ (P3.2 path-containment).
- `test_threat_secret_exfiltration_redacted` — секреты в артефакты/логи/PR невозможны (redaction + env-allowlist + raw session-id только в state.db).
- `test_threat_network_exfiltration_above_ceiling` — `network_policy` шире разрешённого → фатально.
- `test_threat_infinite_loop_requires_budget` — rework-ребро без budget → фатально (есть с P0.3).
- `test_threat_path_traversal_fail_closed` — `..`/абсолютный путь в `role_file`/`task_id` → фатально (есть с P0.3).
- `test_threat_arbitrary_code_node_impossible` — нет вида узла «код/shell» (палитра типизирована, fail-closed на unknown kind/field — P0.5).
- `test_threat_unknown_field_fail_closed` — неизвестное поле любого уровня → фатально (P0.5).
- `test_recovery_ceiling_only_narrows` — recovery не позволяет потолку расшириться через изменённый конфиг.

### Exit

Операторский flow не может расширить права или переопределить core-действие.

### Решения (зафиксировано 2026-06-17)

- **Config-aware валидация — отдельная функция, зовёт реестр.** `validate_flow(snapshot)` остаётся config-free (граф + потолок, юнит-тестируется без конфига); новый `validate_flow_against_config(snapshot, config)` вызывается `FlowRegistry` после структурной валидации. Почему: реестр — естественная точка резолюции, где конфиг уже доступен; слои граф/потолок/конфиг не смешиваются в одной сигнатуре.

---

## P4.3 — Docs + housekeeping

Обновить configuration / how-it-works / функциональную карту / likec4 / follow-ups; перенести поглощённые пять backlog-доков в `docs/backlog/archive/outdated/` со ссылкой на `flows/*`.

### Touchpoints

- [`docs/functional/`](../../functional/) — функциональная карта (flow-движок как блок; узлы/рёбра/потолок).
- `docs/likec4/` — C4-модель (новый блок flow-engine, его границы с ядром).
- `docs/configuration.md` — шринкнутая схема конфига (граница config↔flow, [index.md](index.md) §14.3); `.worc/flows/` раскладка.
- how-it-works / [happy-path.md](happy-path.md) — синхронизировать с реализованным.
- [`docs/backlog/follow_ups.md`](../follow_ups.md) — снять закрытые P0.5-пункты; зафиксировать оставшиеся deferred (network per-host allowlist, signature/hash-реестр flow — [security-ceiling.md](security-ceiling.md) §8).
- Перенести пять программ ([foundation](../archive/outdated/workflow_execution_foundation.md) и др.) — **уже** в `docs/backlog/archive/outdated/`; добавить заголовочную ссылку на `flows/*` как канонический преемник.

### Exit

Docs синхронны; старые доки помечены устаревшими.

---

## Сквозной обзор зависимостей P4

```text
P3 (абстракция доказана тремя flow)
   └─> P4.1 приём операторских flow (подключить registry к боевому пути)
        └─> P4.2 config-aware валидатор + модель угроз как тесты + recovery-перепроверка
             └─> P4.3 docs + housekeeping
```

## Что P4 НЕ строит (зафиксированные отказы)

- Подпись/хеш-реестр операторских flow — доверие файловое ([security-ceiling.md](security-ceiling.md) §8); отложено.
- `network_policy` per-host allowlist — бинарные уровни в v1; отложено.
- Версионируемый registry flow — файловая раскладка в v1.
- Per-task оверрайды графа/узлов — убраны навсегда ([flow-contract.md](flow-contract.md) §10).
