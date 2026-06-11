# wastech-orchestrator

Консольный **lean-оркестратор** для автоматического выполнения задач разработки силами внешних кодинг-агентов (**OpenAI Codex CLI** и **Anthropic Claude Code CLI**) с публикацией результата в отдельную Git-ветку / Pull Request.

Оркестратор владеет процессом: принимает задачу → парсит → создаёт ветку → прогоняет детерминированный пайплайн стадий через взаимозаменяемые CLI-агенты → запускает проверки → коммитит и пушит. Агенты работают только с содержимым репозитория и не управляют жизненным циклом Git.

> Статус: **design / pre-MVP**. Архитектура зафиксирована, кодовая база в начальной стадии. Спека ниже — источник истины для реализации.

---

## Документы (источники истины)

| Документ | Роль |
|----------|------|
| [orchestrator_final_plan.md](orchestrator_final_plan.md) | **Канонический build-спек**: контракты, state machine, маршрутизация, fallback, security, DoD, стадии реализации. При расхождениях приоритет у него. |
| [codex_git_orchestrator_architecture.md](codex_git_orchestrator_architecture.md) | Архитектурный обзор и обоснование решений (high-level). |
| [open_questions.md](open_questions.md) | Исходные требования (закрыты в architecture.md §11). |
| [docs/rules/](docs/rules/) | Правила разработки: стиль, архитектурные инварианты, безопасность, git-flow, тесты. |

Для кодинг-агентов: [CLAUDE.md](CLAUDE.md) (Claude Code) и [AGENTS.md](AGENTS.md) (Codex).

---

## Ключевые принципы

1. **Ядро не знает синтаксис конкретного CLI** — только интерфейс `AgentProvider`.
2. **Детерминированный пайплайн стадий**, а не свободная автономия агентов.
3. **Кодинг-агент за абстракцией** — Codex и Claude Code взаимозаменяемы, с per-stage primary/fallback.
4. **Fallback только для инфраструктурных ошибок** провайдера, не для ошибок качества (тесты/ревью).
5. **Commit / push / PR делает только оркестратор** — агентам это запрещено.
6. **Чекпоинты на каждой стадии** → восстановление после падения, идемпотентная публикация.
7. **Security policy нельзя ослабить** через задачу или `extra_args`.

---

## Технологии

- **Python 3.12+**
- `watchdog` — отслеживание папки задач
- `PyYAML` — конфиг и шаблоны
- `python-telegram-bot` — human-in-the-loop и уведомления
- `sqlite3` (stdlib) — state store и чекпоинты
- subprocess — запуск `git` / `codex` / `claude` / проверок
- dev: `ruff`, `mypy`, `pytest`

---

## Структура проекта

```text
wastech-orchestrator/
  README.md
  CLAUDE.md / AGENTS.md          # инструкции для кодинг-агентов в ЭТОМ репо
  pyproject.toml
  config.example.yaml            # пример конфигурации (скопировать в config.yaml)
  docs/
    rules/                       # правила разработки (источник истины для агентов)
  .claude/
    skills/                      # переиспользуемые навыки для разработки
  src/
    wastech_orchestrator/
      cli.py                     # точка входа
      providers/
        base.py                  # контракт AgentProvider (§4.3 спеки)
  tests/                         # unit / integration / e2e (см. docs/rules/testing.md)
```

---

## Быстрый старт (разработка)

```bash
# 1. виртуальное окружение
python -m venv .venv
.\.venv\Scripts\Activate.ps1      # Windows PowerShell
# source .venv/bin/activate       # macOS/Linux

# 2. установка в editable-режиме с dev-зависимостями
pip install -e ".[dev]"

# 3. конфиг
copy config.example.yaml config.yaml   # Windows
# cp config.example.yaml config.yaml    # macOS/Linux

# 4. проверки
ruff check .
mypy src
pytest
```

Запуск (по мере реализации CLI):

```bash
python -m wastech_orchestrator run tasks/pending/task-001.md
python -m wastech_orchestrator watch
```

---

## Дорожная карта реализации

Стадии выполняются строго последовательно (см. [orchestrator_final_plan.md §15](orchestrator_final_plan.md)):

1. Контракты и конфигурация
2. Провайдерный слой и Codex-адаптер
3. Claude Code-адаптер
4. Маршрутизация и fallback
5. Pipeline и восстановление
6. Безопасность и наблюдаемость

Переход к следующей стадии — только после выполнения DoD предыдущей.
