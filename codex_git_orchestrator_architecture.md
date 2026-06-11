# Архитектура lean-оркестратора для кодинг-агентов (Codex / Claude) + Git

Дата: 2026-06-11 (обновлено)
Цель: описать архитектуру консольного приложения, которое работает на Windows/macOS/Linux, следит за папкой задач, прогоняет задачу через детерминированный пайплайн стадий силами внешних кодинг-агентов (Codex CLI и/или Claude Code CLI) и публикует результат в отдельную Git-ветку.

Документ переработан после изучения [crewAI](https://github.com/crewAIInc/crewAI). Решение: **писать свой lean-оркестратор** (без зависимости от фреймворка), но заимствовать у crewAI 5 проверенных паттернов. Где именно — отмечено маркером `[← crewAI]`.

---

## 1. Идея в одном абзаце

Приложение не заменяет кодинг-агента и Git. Оно выступает как **оркестратор**: следит за папкой задач, парсит задачу, обновляет репозиторий, создаёт отдельную ветку и прогоняет задачу через детерминированный пайплайн стадий (план → реализация → ревью → тесты → исправления → коммит → push). Тяжёлую работу с кодом делает **внешний кодинг-агент** — Codex CLI или Claude Code CLI — за которым стоит абстракция с возможностью глобально включать/выключать провайдера и фолбэка на оставшегося. Над стадиями стоит тонкий **supervisor**, который планирует, маршрутизирует и при необходимости задаёт уточняющие вопросы человеку (через Telegram). Доступ к Git — обычными средствами (SSH-ключ, GitHub token, `gh auth login`). Подписка ChatGPT/Codex или Claude используется для доступа к агенту, а не как механизм Git-аутентификации.

---

## 2. Ключевые архитектурные принципы

1. **Детерминированный Flow, а не эмерджентное поведение.** Пайплайн стадий задан явно (как crewAI Flow), агенты не «договариваются» между собой свободно. Предсказуемость важнее автономии.
2. **Тонкий supervisor поверх стадий** `[← crewAI hierarchical]`. Supervisor планирует и маршрутизирует, но не подменяет кодинг-агента.
3. **Кодинг-агент за абстракцией.** `Codex CLI` и `Claude Code CLI` — взаимозаменяемые реализации одного интерфейса с фолбэком.
4. **Чекпоинты на каждой стадии** `[← crewAI @persist]`. После каждой стадии состояние атомарно пишется в SQLite, чтобы пережить падение и продолжить с места.
5. **Guardrails в два слоя** `[← crewAI guardrails]`. Запрет на действия (sandbox/approval) + валидация выхода (diff) перед коммитом.
6. **Свежий контекст на каждую задачу** `[← crewAI kickoff]`. Supervisor и контекст агентов пересобираются из YAML-шаблонов под каждую новую задачу — никакого общего состояния между задачами.
7. **Человек в цикле через Telegram** `[← crewAI AskQuestion/human_input]`. Уточняющие вопросы и одобрение опасных действий идут человеку и блокируют стадию до ответа.

---

## 3. Общая схема

```text
┌────────────────────┐
│  tasks/pending/    │
│  task-001.md       │
└─────────┬──────────┘
          │ новая задача
          ▼
┌─────────────────────────────────────────┐
│            ORCHESTRATOR                   │
│  watcher → parser → supervisor            │
└─────────┬─────────────────────────────────┘
          │ пересоздать supervisor + контекст под задачу
          ▼
   ┌──────────────────── Flow стадий ────────────────────┐
   │  git pull/fetch → checkout -b codex/task-001         │
   │  ├─ STAGE plan       (CodingAgent.run, no edits)      │
   │  ├─ STAGE implement  (CodingAgent.run, edits)         │
   │  ├─ STAGE review     (CodingAgent.run, no edits)      │
   │  ├─ STAGE test       (Test Runner)                    │
   │  ├─ STAGE fix        (retry-цикл, лимит попыток)       │
   │  ├─ GUARDRAILS       (action blacklist + diff-checks)  │
   │  ├─ git commit                                        │
   │  └─ git push / gh pr create                           │
   └───────────────────────────────────────────────────────┘
          │            ▲                       │
          │            │ уточняющий вопрос /    │ результат
          ▼            │ одобрение действия     ▼
   ┌─────────────┐  ┌──┴──────────┐      ┌──────────────┐
   │ State Store │  │  Telegram   │      │ tasks/done/  │
   │  (SQLite,   │  │  (HITL +    │      │ tasks/failed/│
   │ чекпоинты)  │  │ уведомления)│      └──────────────┘
   └─────────────┘  └─────────────┘

   CodingAgent (абстракция) ──┬── CodexCLI   (enable/disable)
                              └── ClaudeCLI  (enable/disable)  ← фолбэк
```

---

## 4. Основные компоненты

### 4.1. File Watcher + Task Parser `(#9)`

Следит за папкой задач:

```text
tasks/
  pending/    task-001.md
  processing/
  done/
  failed/
```

При появлении нового `.md`/`.json` файл переносится в `processing/` и парсится. Парсер достаёт из задачи структурированные поля — из YAML-frontmatter и/или из заголовка:

```markdown
---
id: task-001
title: "Add login validation"
repo: my-service              # привязка к репе/проекту (#8)
reasoning: high               # уровень reasoning (#7)
complexity: medium            # влияет на выбор модели и лимиты
provider: auto                # auto | codex | claude
contacts: ["@team-lead"]      # кого пинговать в Telegram
commands:                     # доп. команды/хинты для агента
  - "не трогать модуль billing"
---

## Описание
Добавить валидацию формы логина ...
```

Распарсенные поля подставляются в YAML-шаблоны промптов как `{переменные}` `[← crewAI inputs]`.

### 4.2. Git Manager `(#8)`

Привязка к конкретному проекту/репе задаётся в `config.yaml` (см. §5) и/или в поле `repo` задачи. Перед задачей:

```bash
git fetch origin
git checkout main
git pull origin main
git checkout -b codex/task-001-add-login-validation
```

После:

```bash
git add .
git commit -m "Implement task-001: add login validation"
git push origin codex/task-001-add-login-validation
gh pr create --title "Task 001" --body-file logs/task-001/pr.md --base main --head codex/task-001-add-login-validation
```

Параллельные задачи — через `git worktree` (v2), чтобы не смешивать в одном clone.

### 4.3. CodingAgent — абстракция провайдера + фолбэк `(#1)`

Ключевое место, которого **нет в crewAI** (там нет нативного failover) — проектируем сами.

```python
class CodingAgent(Protocol):
    name: str
    enabled: bool                     # глобальное вкл/выкл (#1)
    def run(self, prompt: str, cwd: str, stage: str,
            reasoning: str, allow_edits: bool) -> Result: ...

class CodexCLI(CodingAgent):  ...     # обёртка над `codex exec`
class ClaudeCLI(CodingAgent): ...     # обёртка над `claude` CLI

def run_with_fallback(prompt, **kw):
    providers = [p for p in (CodexCLI(), ClaudeCLI()) if p.enabled]
    if not providers:
        raise NoProviderEnabled()
    last_err = None
    for p in providers:               # порядок = приоритет; auto → весь список
        try:
            return p.run(prompt, **kw)
        except ProviderError as e:
            last_err = e               # фолбэк на следующего включённого
            log.warning(f"{p.name} failed, fallback: {e}")
    raise AllProvidersFailed(last_err)
```

- Глобально отключить провайдера — флаг в `config.yaml` (`providers.codex.enabled: false`).
- В задаче можно зафиксировать `provider: claude` (без фолбэка) или `provider: auto` (с фолбэком).

### 4.4. Supervisor `(#4, #5)` `[← crewAI hierarchical manager]`

Тонкий слой над стадиями. Отвечает за:

- планирование: какие стадии нужны для задачи (например, простому фиксу не нужна полная цепочка);
- маршрутизацию: какой провайдер/модель/reasoning на каждой стадии;
- эскалацию: когда задать уточняющий вопрос человеку (`AskQuestion`-контракт) или запросить одобрение действия;
- решение go/no-go после guardrails и тестов.

**Пересоздание на каждой задаче `(#5)`:** supervisor и контекст агентов собираются заново из YAML-шаблонов под конкретную распарсенную задачу. Между задачами состояние не шарится — это устраняет «протёкший» контекст. (В crewAI это `kickoff()` со свежим Crew; у нас — просто конструирование объекта на задачу.)

Защита от петель делегирования `[← crewAI]`: только supervisor имеет право эскалировать/маршрутизировать; стадийные вызовы агента — «листовые», переадресовывать дальше не могут.

### 4.5. Stage Pipeline (Flow) `[← crewAI Flow]`

Стадии фиксированы и детерминированы:

```python
STAGES = ["plan", "implement", "review", "test", "fix", "guardrails", "commit", "push"]
```

Каждая стадия — отдельная функция с входом из состояния и атомарным чекпоинтом на выходе (см. §6). `fix` исполняется в retry-цикле с лимитом попыток.

### 4.6. Guardrails — blacklist действий + проверка выхода `(#3)` `[← crewAI guardrails]`

Два слоя (в crewAI guardrails валидируют только output — мы расширяем до действий):

**Слой 1 — запрет действий (enforce, до выполнения):**
- запускать агента только в `workspace/repo`, sandbox `workspace-write`, approval `on-request`;
- глобальный blacklist опасных команд (`rm -rf`, `git push --force`, доступ к `~`, к секретам);
- запрет push в `main` напрямую.

**Слой 2 — валидация выхода (перед коммитом):**

```python
GUARDRAILS = [no_secrets_in_diff, only_allowed_paths, no_unexpected_files, no_push_to_main]

def validate(diff):
    result = diff
    for g in GUARDRAILS:               # цепочка, как в crewAI
        ok, out = g(result)
        if not ok:
            return False, out          # ошибка уходит обратно агенту на исправление
        result = out
    return True, result
```

При провале — ошибка возвращается в стадию `fix`, ретрай до `guardrail_max_retries` (по умолчанию 3).

### 4.7. Human-in-the-Loop через Telegram `(#2, #10)` `[← crewAI AskQuestion / human_input]`

Единый механизм для уточняющих вопросов и одобрения действий. Блокирует текущую стадию до ответа (или таймаута).

```python
def ask_human(question: str, context: str, task_id: str,
              kind: str = "question", timeout=...) -> str:
    # kind: "question" (свободный ответ) | "approval" (да/нет на действие)
    send_telegram(task_id, question, context, contacts=task.contacts)
    return wait_for_reply(task_id, timeout)   # ответ возвращается в контекст стадии
```

- Уточняющий вопрос агента → `kind="question"`.
- Опасное/необратимое действие (push, удаление, новая зависимость) → `kind="approval"`.
- Telegram также используется для финальных уведомлений о результате (`done`/`failed` + ссылка на PR).

### 4.8. Test Runner

Команды из конфига, не хардкод:

```yaml
checks:
  commands:
    - "npm test"
    - "npm run lint"
```

Вывод тестов при падении передаётся в стадию `fix`.

### 4.9. State Store с чекпоинтами `[← crewAI @persist]`

SQLite. Статус задачи = маркер последней успешно завершённой стадии → resume после падения.

```sql
CREATE TABLE tasks (
  id           TEXT PRIMARY KEY,
  file_path    TEXT NOT NULL,
  repo         TEXT,
  branch_name  TEXT,
  stage        TEXT NOT NULL,     -- последняя завершённая стадия (чекпоинт)
  status       TEXT NOT NULL,     -- running | done | failed | waiting_human
  provider     TEXT,
  reasoning    TEXT,
  attempts     INTEGER DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  last_error   TEXT
);
```

```python
def run_task(task):
    state = load_or_init(task.id)                 # resume: с какой стадии продолжать
    for stage in STAGES[state.stage_index:]:
        checkpoint(task.id, stage, "running")     # перед шагом
        run_stage(stage, task)
        checkpoint(task.id, stage, "done")        # после — атомарно
```

### 4.10. Reasoning / Complexity `(#7)` `[← crewAI reasoning_effort]`

Поля `reasoning` и `complexity` из задачи маппятся на:
- флаги/бюджет модели кодинг-агента (`reasoning_effort` low/med/high, thinking-budget для Claude);
- выбор модели, число fix-итераций и таймауты под сложность.

Стадия `plan` = аналог crewAI `reasoning=True` (отрефлексировать и составить план до правок).

### 4.11. Заготовки AGENTS / CLAUDE / SKILLS `(#6)` `[← crewAI agents.yaml/tasks.yaml]`

Промпты стадий, роли и правила хранятся в YAML-шаблонах с подстановкой `{переменных}` из задачи. Сюда же кладутся заготовки для кодинг-агентов, помещаемые в репозиторий перед запуском:

```text
templates/
  AGENTS.md            # инструкции для Codex (помещается в репо)
  CLAUDE.md            # инструкции для Claude Code (помещается в репо)
  skills/              # переиспользуемые навыки/процедуры
  prompts/
    plan.md            # шаблон промпта стадии plan c {variables}
    implement.md
    review.md
    fix.md
```

---

## 5. Пример `config.yaml`

```yaml
repos:                                # привязка к проектам/репам (#8)
  my-service:
    url: "git@github.com:OWNER/my-service.git"
    local_path: "./workspace/my-service"
    base_branch: "main"
    branch_prefix: "codex"

tasks:
  pending_folder:    "./tasks/pending"
  processing_folder: "./tasks/processing"
  done_folder:       "./tasks/done"
  failed_folder:     "./tasks/failed"

providers:                            # глобальное вкл/выкл + фолбэк (#1)
  default_order: ["codex", "claude"]  # приоритет для provider: auto
  codex:
    enabled: true
    working_dir: "./workspace/repo"
    approval_mode: "on-request"
    sandbox: "workspace-write"
  claude:
    enabled: true
    sandbox: "workspace-write"

reasoning:                            # уровни сложности (#7)
  default: "medium"
  map:                                # complexity → лимиты
    small:  { fix_attempts: 1, timeout_s: 600 }
    medium: { fix_attempts: 2, timeout_s: 1200 }
    large:  { fix_attempts: 3, timeout_s: 2400 }

guardrails:                           # blacklist (#3)
  forbidden_commands: ["rm -rf", "git push --force", "sudo"]
  forbidden_paths:    ["~", ".env", "secrets/"]
  block_push_to_main: true
  max_retries: 3

checks:
  commands:
    - "npm test"
    - "npm run lint"

git:
  create_pull_request: true
  pr_base: "main"

telegram:                             # HITL + уведомления (#2, #10)
  enabled: true
  bot_token_env: "TELEGRAM_BOT_TOKEN"
  chat_id_env:   "TELEGRAM_CHAT_ID"
  ask_timeout_s: 1800
```

---

## 6. Поток обработки задачи

```text
1.  Watcher нашёл новую задачу в tasks/pending/ → перенёс в processing/
2.  Parser распарсил frontmatter/заголовок (repo, reasoning, provider, contacts...) (#9)
3.  Supervisor пересоздан под задачу из YAML-шаблонов (#5)
4.  Проверить доступность репы; git fetch/pull (#8)
5.  Создать ветку codex/<task-id>
6.  Поместить заготовки AGENTS/CLAUDE/SKILLS в репо (#6)
7.  STAGE plan      → CodingAgent.run (no edits) → logs/<id>/plan.md   [checkpoint]
8.  STAGE implement → CodingAgent.run (edits)                          [checkpoint]
9.  STAGE review    → CodingAgent.run (no edits)                       [checkpoint]
10. STAGE test      → Test Runner
11. STAGE fix       → если тесты упали: вывод → CodingAgent → повтор,
                      лимит = reasoning.map[complexity].fix_attempts
12. На любой стадии: агент задал уточняющий вопрос
                      → ask_human(question) через Telegram, ждём ответ (#2, #10)
13. STAGE guardrails → blacklist действий + проверка diff;
                       провал → вернуть в fix (до max_retries) (#3)
14. Опасное действие (push/удаление/новая зависимость)
                      → ask_human(approval) (#2)
15. STAGE commit    → git add / commit                                [checkpoint]
16. STAGE push      → git push; опц. gh pr create                     [checkpoint]
17. Перенести задачу в done/ (или failed/), уведомить в Telegram (#10)
18. (resume) при падении на любом шаге — продолжить со следующей незавершённой стадии
```

Фолбэк провайдера `(#1)` срабатывает прозрачно внутри любого `CodingAgent.run`.

---

## 7. State Machine + чекпоинты

```text
new → planning → implementing → reviewing → testing
        │                                      │
        │                                 (fail) ├──→ fixing ──┐
        │                                      │   (loop ≤ N)  │
        │                                      ▼               │
        └────────────────────────→ guardrails ◄───────────────┘
                                        │ (fail → fixing)
                                        ▼
                                    committing → pushing → done
                                        │
                          (любой шаг)   ▼
                                     failed
                          (вопрос/одобрение) → waiting_human → (ответ) → продолжить
```

Каждый переход = атомарная запись `stage`+`status` в SQLite. На рестарте оркестратор грузит последний `stage=done` и продолжает со следующей стадии.

---

## 8. Безопасность и blacklist `(#3)`

1. Кодинг-агент запускается только внутри `workspace/repo`, sandbox `workspace-write`, approval `on-request`.
2. Не использовать полный обход sandbox/approvals на основной машине.
3. Глобальный blacklist команд и путей (см. `config.yaml: guardrails`).
4. Не давать доступ к домашней папке и секретам.
5. Запрет прямого push в `main`; только через PR.
6. Минимальные права GitHub token; желательно Docker/VM/отдельный пользователь ОС.
7. Логировать все команды и вывод агента.
8. Необратимые действия требуют одобрения человека (`ask_human(approval)`).

---

## 9. MVP-версия

```text
Python CLI
  ├─ watchdog            — отслеживание файлов (#9)
  ├─ subprocess          — git / codex / claude / тесты
  ├─ sqlite3             — статусы + чекпоинты
  ├─ PyYAML              — конфиг и шаблоны
  ├─ python-telegram-bot — HITL + уведомления (#2, #10)
  └─ logging             — логи на задачу
```

Минимальная логика:

```python
while True:
    task = find_new_task()
    if not task:
        sleep(5); continue

    parse_task(task)                 # (#9)
    supervisor = build_supervisor(task)   # пересоздание под задачу (#5)
    create_branch(task)
    seed_agent_templates(task)       # AGENTS/CLAUDE/SKILLS (#6)

    run_stage("plan", task)
    run_stage("implement", task)
    run_stage("review", task)
    if run_tests(task).failed:
        for _ in range(fix_attempts(task)):
            run_stage("fix", task)
            if not run_tests(task).failed:
                break

    if guardrails_ok(task):          # (#3)
        commit_and_push(task)        # с фолбэком провайдера (#1)
    notify_telegram(task)            # (#10)
```

---

## 10. Пример промптов для стадий

### План
```text
Ты работаешь внутри репозитория. Прочитай файл задачи.
Составь краткий план реализации:
1. какие файлы изменить; 2. какие функции/модули затронуть;
3. какие тесты добавить/обновить; 4. какие риски.
Не изменяй код. Если что-то неоднозначно — задай уточняющий вопрос.
```

### Реализация
```text
Реализуй задачу по плану. Ограничения:
- меняй только необходимые файлы; - не делай commit;
- не добавляй лишние зависимости без одобрения;
- при изменении поведения — добавь/обнови тесты.
```

### Ревью
```text
Проведи ревью изменений: соответствие задаче, баги, edge cases,
стиль, тестовое покрытие, отсутствие лишних/случайных файлов.
```

### Исправление после тестов
```text
Тесты упали. Ниже вывод. Проанализируй ошибку, исправь код,
кратко объясни, что изменено.
<ВСТАВИТЬ ВЫВОД ТЕСТОВ>
```

---

## 11. Как закрыты 10 пунктов из open_questions.md

| # | Пункт | Где реализовано |
|---|-------|-----------------|
| 1 | Глобально вкл/выкл Codex/Claude + фолбэк | §4.3 CodingAgent + `run_with_fallback`, §5 `providers` |
| 2 | Ответы на уточняющие вопросы + одобрение действий | §4.7 `ask_human(question/approval)`, §6 шаги 12/14 |
| 3 | Глобальный blacklist запрещённого | §4.6 guardrails (2 слоя), §5 `guardrails`, §8 |
| 4 | Supervisor управляет агентами | §4.4 Supervisor (планирование/маршрутизация/эскалация) |
| 5 | Пересоздавать supervisor+агентов на новой задаче | §4.4 + §6 шаг 3 + §9 `build_supervisor(task)` |
| 6 | Заготовки AGENTS/CLAUDE/SKILLS | §4.11 `templates/`, §6 шаг 6 |
| 7 | Уровень reasoning/сложности | §4.10 + §5 `reasoning`, поля задачи |
| 8 | Привязка к проекту/репе | §4.2 Git Manager + §5 `repos`, поле `repo` задачи |
| 9 | Парсинг задач (заголовки, доп.инфо, команды) | §4.1 Task Parser, frontmatter-схема |
| 10 | Интеграция с Telegram | §4.7 + §5 `telegram` (HITL + уведомления) |

---

## 12. Что добавить во второй версии

- Параллельные задачи через `git worktree`.
- Очередь с приоритетами.
- Web UI для задач и логов.
- PR с шаблоном, интеграция с GitHub Issues.
- Авто-retry при сетевых ошибках, лимит бюджета на задачу.
- Авто-закрытие stale-задач, dry-run без push.
- (Опц.) лёгкая память между задачами по проекту — только если потребуется (vector-memory из crewAI для этого кейса избыточна).

---

## 13. Порядок разработки

1. CLI на один файл задачи: `python -m orchestrator run tasks/pending/task-001.md`.
2. Парсер задачи + создание ветки + одна стадия через `CodingAgent` (один провайдер).
3. Полный пайплайн стадий + тесты + retry-цикл `fix`.
4. Абстракция `CodingAgent` с фолбэком (Codex + Claude).
5. Guardrails (blacklist действий + проверка diff).
6. Telegram HITL + уведомления.
7. Watcher папки.
8. SQLite-чекпоинты + resume после падения.
9. Push + PR.

---

## 14. Источники

- crewAI: https://github.com/crewAIInc/crewAI — концепции Flows, hierarchical process, guardrails, reasoning, memory, persistence (источник заимствованных паттернов).
- OpenAI Codex CLI Reference: https://developers.openai.com/codex/cli/reference
- OpenAI Codex Agent Approvals & Security: https://developers.openai.com/codex/agent-approvals-security
- OpenAI Codex GitHub Action: https://developers.openai.com/codex/github-action
- GitHub CLI `gh pr create`: https://cli.github.com/manual/gh_pr_create

---

## 15. Короткий вывод

Концептуально оркестратор = **crewAI Flow** (детерминированный пайплайн стадий) + тонкий **supervisor** в роли менеджера, но **без зависимости от фреймворка**. Из crewAI заимствованы 5 паттернов: checkpoint-state, контракт делегирования/уточнений, guardrails, reasoning-уровни и YAML-шаблоны с подстановкой. Три вещи crewAI не закрывает и они спроектированы самостоятельно: фолбэк провайдеров (#1), привязка к репе (#8) и watcher+парсер задач (#9). Кодинг-агент делает работу с кодом, Git хранит изменения, CI/PR остаются контрольным слоем, человек подключается через Telegram, а оркестратор связывает всё в повторяемый, устойчивый к падениям процесс.
