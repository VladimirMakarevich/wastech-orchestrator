# AGENTS.md — инструкции для Codex CLI в этом репозитории

Ты работаешь над **wastech-orchestrator** — оркестратором, который запускает кодинг-агенты (Codex / Claude Code) для выполнения задач разработки и публикации результата в Git.

Этот файл — для Codex. Полный набор правил совпадает с [CLAUDE.md](CLAUDE.md) и [docs/rules/](docs/rules/); ниже — суть.

## Прежде чем писать код

1. Прочитай **[orchestrator_final_plan.md](orchestrator_final_plan.md)** — канонический спек (источник истины).
2. Соблюдай правила в **[docs/rules/](docs/rules/)**: `architecture.md`, `coding-style.md`, `security.md`, `git-workflow.md`, `testing.md`.

## Жёсткие инварианты (нарушать нельзя)

- **Ядро не знает синтаксис CLI.** Provider-специфика — только в `src/wastech_orchestrator/providers/`. Core вызывает только `AgentProvider`.
- **Commit / push / PR выполняет только оркестратор**, не провайдер.
- **Fallback — только для инфраструктурных ошибок** провайдера. Ошибки тестов/ревью → стадия `fixing`.
- **Security policy нельзя ослабить** через задачу или `extra_args`; никаких флагов обхода sandbox/approvals.
- **Секреты** не попадают в логи, SQLite и артефакты. Процессам — только allowlisted env.
- **CLI вызывать списком аргументов**, без shell-интерполяции пользовательских строк.

## Канонические имена

- Провайдеры: `codex`, `claude`.
- Стадии: `planning`, `implementation`, `testing`, `review`, `fixing`, `publishing`.
- Префикс веток: `agent/<task-id>-<slug>`.

## Команды проверок

```bash
pip install -e ".[dev]"
ruff check .
mypy src
pytest
```

## Definition of Done для изменения

- код проходит `ruff`, `mypy`, `pytest`;
- добавлены/обновлены тесты при изменении поведения;
- не нарушены инварианты выше;
- не выполнен переход к следующей стадии реализации без DoD предыдущей (см. спек §15–16).
