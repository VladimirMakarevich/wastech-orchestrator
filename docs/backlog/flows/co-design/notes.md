# Co-design (Шаг 0): результат и зафиксированные формы

Статус: **выполнено 2026-06-17**. Гейт пройден.

Цель Шага 0 ([plan.md](../plan.md) → Ближайшие шаги): записать три эталонных flow как данные, составить черновую `flow.schema.json`, реально прогнать файлы через неё (без исполнения движка) и подтвердить, что палитра выражает три flow **без доменного знания в движке**.

## Артефакты

- [flow.schema.json](flow.schema.json) — структурный контракт (JSON Schema draft 2020-12), fail-closed (`additionalProperties: false` везде).
- [implementation.yaml](implementation.yaml), [deep_research.yaml](deep_research.yaml), [security_audit.yaml](security_audit.yaml) — три flow в финальной форме.
- [validate.py](validate.py) — валидатор: слой 1 структурный (по схеме) + слой 2 графовые семантические проверки (зерно фатального валидатора P0.3).

## Результат (гейт)

```
✓ implementation.yaml: PASS (structural + graph)
✓ deep_research.yaml:   PASS (structural + graph)
✓ security_audit.yaml:  PASS (structural + graph)
✓ schema is generic (no flow/stage/role names hard-coded)
RESULT: ALL PASS
```

Валидатор не пустой — негативные прогоны отклоняются (доказательство «зубов»):

| Нарушение | Кем поймано |
| --- | --- |
| unknown-field на узле | schema (`additionalProperties: false`) |
| dangling-edge (ребро в несуществующий узел) | graph |
| unbounded-rework (rework-ребро без budget/loop) | graph |
| evaluator emits `pass` (исход не из набора kind) | graph (выбор ⊆ объявленного) |
| evaluator с `workspace-write` | schema (`const: read-only`) |
| два entry-узла / недостижимость | graph |

**Вывод**: палитра (`agent`/`evaluator`/`checks`/`hitl`/`publish` + рёбра + decomposition) выражает все три flow данными; добавлять виды узлов не потребовалось; схема не содержит ни одного flow/stage/role-имени → доменного знания в движке нет. **Можно идти в P0.1.**

## Зафиксированные формы контракта

- **Корень** `{ flow: {...} }`; `additionalProperties: false` на каждом уровне (unknown поле → фатальный отказ).
- **Узел** дискриминируется по `kind` (`oneOf` из пяти `*Node`-дефиниций, каждая со своим `additionalProperties: false`).
- **`when`** (условный пропуск, детерминированный, не агентом): `{ fact: <имя>, equals: <bool=true> }`.
- **Ребро**: `{ from, to, outcome?, budget?, loop? }`. `outcome ∈ accept|rework|pass|fail|route:<label>`; отсутствует = безусловное.
- **Allowed-outcome по виду узла** (это и есть «выбор ⊆ объявленного»): `evaluator`/stage_output → `accept|rework`; `evaluator`/final_handoff → безусловное; `checks` → `pass|fail`; `agent`/`hitl`/`publish` → безусловное.
- **Бюджеты циклов**: `rework`/`fail`-ребро обязано нести `budget` (инлайн int) или `loop` (имя счётчика, объявленного в `budgets`). Движок гарантирует терминальность.
- **`decomposition`**: `{ proposed_by, gate{min,max,linear_depends_on}, sub_flow:[node-ids], commit_each_subtask, shared_budget }`.
- **evaluator** жёстко `permission_profile: read-only` (`const`) и `session_scope ∈ {fresh_disposable, resume_own_lineage}` (никогда `editing_lineage` автора).
- **`lineage_affinity`** обязан указывать на `agent`-узел с `editing_lineage`.

## Находки (пробелы, выявленные и закрытые)

1. **`on` как ключ ребра — YAML 1.1 bool-ловушка.** Подтверждено на pyyaml 6.0.3: `on: 1` → `{True: 1}`, `off` → `False`. Ключ исхода переименован в **`outcome`**. Применено к [flow-contract.md](../flow-contract.md) и [index.md](../index.md).
2. **Enum-токены, совпадающие с YAML-булевыми/null** (`on`/`off`/`yes`/`no`) — избегаем. `network_policy` = `advisories`/`research` (без `off`; отсутствие = нет сети). `none` в pyyaml — безопасная строка (проверено), поэтому `publishing: none` валиден; но загрузчику P0 стоит явно нормализовать enum-токены (или парсить YAML 1.2), чтобы исключить класс ловушек.
3. **`dependency_scan` эмитит `outcome: pass`** (скан выполнился), а не «безусловное ребро». Так `checks` остаётся единообразно pass/fail, и в движке нет спец-кейса «этот checker не гейтит». Уточняет формулировку [flow-contract.md](../flow-contract.md) §8.2.
4. **Allowed-outcome зависит от `evaluation_kind`** (`final_handoff` → безусловное, `stage_output` → accept/rework) — это семантика вида узла, generic, не доменное знание.
5. **Палитра не потребовала расширения** — `route:`-метка в контракте есть, но тремя примерами не задействована (ни одному flow пока не нужен явный роутер).

## Тулинг

- `jsonschema 4.26.0` добавлен в `.venv` (валидация). В P0 станет зависимостью flow-валидатора.
- Запуск: `.venv/bin/python docs/backlog/flows/co-design/validate.py` (exit 0 = всё прошло).

## Остаётся на P0 (не блокирует, downstream)

- **Namespace фактов для `when`**: какие `derived.*` (напр. `needs_refinement`) и `config.*` (напр. `hybrid_testing`, `summary_enabled`, `external_research`) разрешены — небольшой allowlist, фиксируется в P0.2 вместе с шринкнутой схемой конфига.
- **Формализация схемы в код** (`core/flow/schema.py`) + прогон этого валидатора в CI — P0.2/P0.3. Графовый слой `validate.py` — прямое зерно фатального валидатора P0.3.
- **`flow.schema.json` — черновик**; точные правила потолка (clamp профиля, `forbidden_args`, path-containment) добавляются в P0.3 поверх структурного слоя ([security-ceiling.md](../security-ceiling.md) §4).
