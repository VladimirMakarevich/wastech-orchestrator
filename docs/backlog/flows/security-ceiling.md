# Потолок прав операторских flow и фатальный валидатор (C)

Статус: **backlog / детальный дизайн (не запланировано)** Дата: 2026-06-17 Владелец: Vladimir Makarevich

Документ задаёт, как обеспечивается инвариант безопасности из [index.md](index.md) §3 для **операторских flow** (YAML+MD, описанных оператором). Реализует «потолок прав»: данные flow выбирают граф и параметры узлов в пределах потолка, но никогда не могут расширить права и никогда не могут переопределить core-owned действие. Опирается на палитру и поля из [flow-contract.md](flow-contract.md).

Ключевой тезис: **операторский flow — это полу-доверенный конфиг, а не код и не недоверенный текст.** Он доверен в той же мере, что и нынешний `config.yaml` (его пишет оператор, не агент и не задача), но движок обязан гарантировать, что **никакое** значение в нём не пробивает потолок. Задача (`task_type`, frontmatter) не выбирает и не меняет flow — она лишь диспетчеризуется в уже доверенный flow.

## 1. Модель угроз

Что должно быть невозможно через flow-данные (даже злонамеренные или ошибочные):

| Угроза | Через что | Чем закрыто |
| --- | --- | --- |
| Эскалация прав агента | `permission_profile` выше потолка; запрещённые `extra_args` | clamp к `permission_ceiling` + `forbidden_args` |
| Обход sandbox/approvals | `--dangerously*`, `--yolo`, `danger-full-access`, bypass-режимы | `forbidden_args` (два слоя) + профиль-маппинг |
| Прямая запись в Git мимо PR | `publish` в base, обход идемпотентности | publish core-owned; защита base-ветки |
| Отключение security-гейтов | убрать dangerous-diff / сделать `checks` неавторитетным | guard'ы core-owned, не объявляются и не отключаются flow |
| Запись вне разрешённых путей | узел пишет вне `output_policy` | path-containment + scoped staging + after-stage сравнение |
| Утечка секретов | секреты в артефакты/логи/PR | redaction core-owned; env-allowlist; raw session-id только в state.db |
| Эксфильтрация по сети | `network_policy` шире разрешённого | сетевой потолок, read-only по умолчанию |
| Бесконечный цикл | `rework`-рёбра без бюджета | валидатор требует бюджет + глобальную страховку |
| Path traversal | `*_file`/`task_id`/пути с `..` | нормализация + containment, fail-closed |
| Произвольный код на узле | узел = инлайн-код/shell | палитра типизирована; нет вида узла «код» (контраст с crewAI CVE) |

Каталог угроз подтверждён investigate crewAI (см. [index.md](index.md), приложение): их `allow_code_execution` + `code_execution_mode="unsafe"` дают конфигом ослабить безопасность → задокументированы CVE на sandbox escape и RCE через prompt injection. Наш потолок неослабляем по построению.

## 2. Принцип потолка и порядок authority

Порядок разрешения прав (из [foundation](../outdated/workflow_execution_foundation.md) §6, переиспользуется дословно):

```text
твёрдые канонические/security-инварианты
  -> потолок прав/публикации flow (permission_ceiling, output_policy, publishing)
  -> доверенная операторская конфигурация (config.yaml: провайдеры, allowlist, discovery)
  -> валидированные task-оверрайды, разрешённые flow
  -> рантайм-решение маршрутизации/affinity в этих пределах
  -> инфраструктурный fallback
```

Правила: flow и задача **никогда** не расширяют FS/сеть/output/публикацию; route/model/reasoning-оверрайды ограничены allowlist провайдеров и стадий; affinity выбирает только уже разрешённого провайдера; fallback не меняет flow/output/permission/publishing; резолюция и валидация завершаются **до** создания ветки и любого запуска провайдера.

## 3. Allowlist полей YAML

Каждое поле flow относится к одному из трёх классов: **operator-settable** (можно задавать, валидируется/клампится), **core-fixed** (механика ядра, во flow не объявляется) или **forbidden** (отвергается фатально). Неизвестное поле — **fail-closed** (отвергается, не игнорируется).

Уровень flow:

| Поле | Класс | Ограничение |
| --- | --- | --- |
| `name`, `task_type` | settable | уникальность; `task_type` не из task-прозы |
| `permission_ceiling` | settable | ≤ возможностей провайдеров в `config.yaml`; не `danger-full-access` |
| `output_policy` | settable | из словаря foundation; задаёт разрешённые пути записи |
| `publishing` | settable | из словаря; механика — ядро |
| `network_policy` | settable | ≤ сетевого потолка оператора |
| идемпотентность, git-механика, single-slot, redaction | core-fixed | не объявляется во flow |

Уровень узла `agent`:

| Поле | Класс | Ограничение |
| --- | --- | --- |
| `role_file`, `model`, `reasoning`, `timeout_seconds`, `output_schema`, `optional`, `hitl`, `session_scope`, `lineage_affinity` | settable | `model`/`reasoning` ∈ allowlist; путь `role_file` — containment |
| `permission_profile` | settable | **clamp** к `permission_ceiling`; никогда выше |
| `extra_args` | settable | проходит `find_forbidden_args`; иначе фатально |
| dangerous-diff guard | core-fixed | авто после `workspace-write`; **нельзя отключить** |

Уровень узла `evaluator`:

| Поле | Класс | Ограничение |
| --- | --- | --- |
| `role`, `role_file`, `evaluation_kind`, `blocking`, `max_rework_per_stage`, `model`, `reasoning` | settable | бюджет ≥ 0; `model`/`reasoning` ∈ allowlist |
| `permission_profile` | core-fixed | всегда `read-only` (evaluator не пишет workspace) |
| `session_scope` | settable | только `fresh_disposable` / `resume_own_lineage`; **никогда** `editing_lineage` автора |

Уровень узла `checks`:

| Поле | Класс | Ограничение |
| --- | --- | --- |
| `checker` | settable | из ядрового набора (`command_profile`/`citation`/`dependency_scan`) |
| `discovery` (mode, approve_command_changes) | settable | разрешённые режимы; гейт одобрения смены команд — core |
| конкретные команды, mutation guard, авторитет exit-кодов | core-fixed | flow команды не задаёт; ими правит discovery+approval |

Уровень узла `publish`:

| Поле | Класс | Ограничение |
| --- | --- | --- |
| `policy` | settable | из словаря publishing |
| commit/push/PR/merge, scoped staging, защита base | core-fixed | целиком ядро |

Уровень узла `hitl`: `signal` (`question`/`approval`), `timeout_s` — settable; транспорт, durable-артефакты, redaction ответов — core-fixed.

## 4. Фатальный валидатор (load-time, до ветки)

Валидатор запускается при загрузке flow и резолюции снапшота — **до** создания ветки и любого запуска провайдера. Любая из проверок проваливается → flow отвергнут фатально (не варнинг — контраст с намеренно поверхностным `validate_contract()` crewAI).

**Целостность графа:**

- все рёбра резолвятся в существующие узлы; нет висячих `from`/`to`;
- объявленные исходы узла покрывают то, что узел может вернуть; рантайм-выбор ∈ объявленного набора (аналог `emit ⊆`);
- достижимость: каждый узел достижим от входа; есть путь к терминалу;
- каждое `rework`/`fail`-ребро имеет `budget` или `loop`; существует глобальный `fix_iterations`-cap → нет недостижимого/бесконечного цикла;
- ровно один вход; терминалы согласованы с lifecycle.

**Потолок и поля:**

- неизвестное поле любого уровня → fail-closed;
- `permission_profile` каждого узла ≤ `permission_ceiling`; `evaluator` → строго `read-only`;
- `extra_args` всех узлов проходят `find_forbidden_args`; `permission_ceiling` ≠ запрещённое значение;
- `publishing`/`output_policy`/`network_policy`/`checker`/`session_scope` ∈ разрешённых словарей;
- все пути (`*_file`, output-директории) нормализуются и лежат в своих корнях; traversal через `task_id`/метаданные отвергается;
- `editing_lineage` запрещён для `evaluator`; `lineage_affinity` ссылается на существующий `agent`-узел с `editing_lineage`.

**Согласованность с config.yaml:** `model`/`reasoning`/провайдеры ∈ операторского allowlist; `permission_ceiling` ≤ возможностей сконфигурированных провайдеров; для `publishing != none` git-конфиг пригоден.

## 5. Что невозможно через flow (явный deny)

Сводный список — то, что валидатор и core-owned механика делают невозможным независимо от содержимого YAML/MD/задачи:

- выдать узлу права выше потолка или запрещённые флаги;
- заставить `publish` коммитить в base или обойти идемпотентность;
- отключить dangerous-diff guard, сделать `checks` неавторитетным гейтом, или дать evaluator право писать workspace;
- писать вне путей `output_policy`; протащить артефакт мимо scoped staging;
- эксфильтрировать секреты (env-allowlist + redaction + raw session-id только в state.db — всё core);
- расширить сеть выше потолка;
- создать неограниченный цикл;
- задать узел как произвольный код/shell (нет такого вида узла);
- выбрать/переопределить flow из task-прозы.

## 6. Переиспользование существующего кода

Потолок **уже существует** — валидатор его композирует, не изобретает:

- `security/forbidden_args.py` — `find_forbidden_args` (две точки: load-time + рантайм в адаптере);
- профиль-маппинг в `providers/claude.py` / `providers/codex.py` — отвергают bypass-режимы, клампят профиль;
- `security/isolation.py` — оффлайн preflight, что требуемая изоляция включаема до ветки;
- `security/env.py` — `build_child_env`, env-allowlist;
- `core/dangerous_diff.py` — детерминированная классификация опасного diff;
- `git_manager.py` — scoped staging, защита base, `publish_operations` идемпотентность;
- `providers/redaction.py` — redaction секретов/session-id перед записью артефактов.

Новое в C — только **flow-загрузчик/валидатор**, который применяет эти проверки к декларативному графу и его полям до запуска.

## 7. Recovery

Снапшот разрешённого графа персистится с `flow_fingerprint`. На рестарте recovery (как в [foundation](../outdated/workflow_execution_foundation.md) §2): пересчитывает фингерпринт (целостность), проверяет существование видов узлов, и **перепроверяет потолок — security может только сузиться, никогда не расшириться**. Benign-изменение конфига не инвалидирует задачу; невозможность безопасно исполнить сохранённый flow → `manual_action_required`. Flow из живого конфига не переразрешается.

## 8. Зафиксированные решения

- **Модель доверия — файловая**: операторский flow доверен на уровне `config.yaml` (один владелец, `.worc/flows/`); защита — потолок + фатальный валидатор (§4), не provenance. Подпись/хеш-реестр — отложено.
- **`network_policy` — бинарные уровни** (`off`/`advisories`/`research`); per-host allowlist отложен.
- **Реестр flow — файловая раскладка** (`.worc/flows/` + запакованные встроенные); отдельный версионируемый registry в v1 не вводится.
- **dangerous-diff — целиком core-fixed**: guard всегда после workspace-write `agent`-узла, маршрут одобрения через HITL — ядро; flow его не тюнит.
- **Task-оверрайды поверх flow убраны** ([flow-contract.md](flow-contract.md) §10): задача не патчит граф/узлы, поэтому вопрос «пределов оверрайдов» снят — задача несёт только идентичность/диспетчеризацию/операционные входы под потолком.
- Точный allowlist полей и полные правила валидатора фиксируются на P0.3 из §3–§4.
