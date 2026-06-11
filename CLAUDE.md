# CLAUDE.md — инструкции для Claude Code в этом репозитории

Ты работаешь над **wastech-orchestrator** — оркестратором, который запускает кодинг-агенты (Codex / Claude Code) для выполнения задач разработки и публикации результата в Git.

## Прежде чем писать код

1. Прочитай **[orchestrator_final_plan.md](orchestrator_final_plan.md)** — это канонический спек. При любых расхождениях он главнее architecture.md.
2. Свериться с правилами в **[docs/rules/](docs/rules/)** — они обязательны:
   - [architecture.md](docs/rules/architecture.md) — инварианты, которые нельзя нарушать
   - [coding-style.md](docs/rules/coding-style.md) — стиль Python
   - [security.md](docs/rules/security.md) — security policy
   - [git-workflow.md](docs/rules/git-workflow.md) — ветки, коммиты, PR
   - [testing.md](docs/rules/testing.md) — что и как тестировать

## Жёсткие инварианты (нарушать нельзя)

- **Ядро не знает синтаксис CLI.** Вся provider-специфика живёт только в `src/wastech_orchestrator/providers/`. Core вызывает только интерфейс `AgentProvider`.
- **Commit / push / PR делает только оркестратор**, не агент-провайдер. Провайдеры не выполняют fallback и не меняют state machine.
- **Fallback — только для инфраструктурных ошибок** (`binary_not_found`, `timeout`, `rate_limited`, …). Ошибки тестов/ревью идут в стадию `fixing`, а не на другого провайдера.
- **Security policy нельзя ослабить** через задачу или `extra_args`. Никаких флагов обхода sandbox/approvals.
- **Никаких секретов** в логах, в SQLite, в артефактах. Передавать процессам только allowlisted env-переменные.
- **CLI запускать без shell-интерполяции** пользовательских строк (список аргументов, не строка).

## Канонические имена (не выдумывать свои)

- Провайдеры: `codex`, `claude`.
- Стадии: `planning`, `implementation`, `testing`, `review`, `fixing`, `publishing`.
- Префикс веток: `agent/<task-id>-<slug>`.
- Статусы state machine: см. orchestrator_final_plan.md §8.

## Команды

```bash
pip install -e ".[dev]"   # установка
ruff check .              # линт
mypy src                  # типы
pytest                    # тесты
```

Для прогона всех проверок есть skill: `/run-checks`.

## Стиль работы

- Делай минимальные, сфокусированные изменения; следуй стилю окружающего кода.
- Для новых компонентов сверяйся с контрактами из orchestrator_final_plan.md (§4, §7, §8).
- Добавляя/меняя поведение — добавляй или обновляй тесты (см. docs/rules/testing.md).
- Перед коммитом прогоняй `ruff`, `mypy`, `pytest`.
- Не переходи к следующей стадии реализации, пока не выполнен DoD текущей (§15–16 спеки).
