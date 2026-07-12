# Баг-репорт: `worc rerun ... --continue --from review`

Репозиторий: `wastech-mdlint`
Задача: `p6-04-config-writer-schema`
Команда: `worc rerun p6-04-config-writer-schema --continue --from review`

## Случай A — запуск из `worc shell`

```
worc> rerun p6-04-config-writer-schema --continue --from review
Rerun p6-04-config-writer-schema [continue] from base 'main'? [y/N] rerun: note: uncommitted changes (README.md, docs/mdlint_v2/P6-init/04-config-writer-schema.md, docs/mdlint_v2/P6-init/index.md, docs/mdlint_v2/glossary.md, packages/cli/src/commands.ts, packages/cli/src/init-command.ts, packages/cli/src/init-prompter.ts, packages/cli/src/program.ts, packages/cli/test/init.e2e.test.ts, packages/core/src/discovery/config-writer.ts, packages/core/src/index.ts, packages/core/test/config-writer.test.ts) will be committed into the task
rerun: note: the flow changed since the checkpoint; --from 'review' will resume using the current on-disk flow
```

Наблюдаемое поведение: строка с вопросом `[y/N]` и последующие `rerun: note:` строки выводятся слитно, одна за другой, без видимого ответа на вопрос. Дальнейшего вывода не было (ни завершения командой, ни возврата к `worc>`).

## Случай B — тот же запуск, но напрямую в терминале (не через `worc shell`)

```
a1234@1234s-MacBook-Pro wastech-mdlint % worc rerun p6-04-config-writer-schema --continue --from review
rerun: note: uncommitted changes (README.md, docs/mdlint_v2/P6-init/04-config-writer-schema.md, docs/mdlint_v2/P6-init/index.md, docs/mdlint_v2/glossary.md, packages/cli/src/commands.ts, packages/cli/src/init-command.ts, packages/cli/src/init-prompter.ts, packages/cli/src/program.ts, packages/cli/test/init.e2e.test.ts, packages/core/src/discovery/config-writer.ts, packages/core/src/index.ts, packages/core/test/config-writer.test.ts) will be committed into the task
Rerun p6-04-config-writer-schema [continue] from base 'main'? [y/N] y
ts=2026-07-12 18:21:12,379 level=info task_id=p6-04-config-writer-schema reset_fix_budget=false from_node=review fix_iterations=15 msg="rerun --continue: applied controls"
ts=2026-07-12 18:21:12,379 level=info task_id=p6-04-config-writer-schema node=review msg="rerun --continue: revived"
ts=2026-07-12 18:21:13,014 level=info task_id=p6-04-config-writer-schema msg="terminal cleanup started"
ts=2026-07-12 18:21:13,040 level=info task_id=p6-04-config-writer-schema duration_seconds=0.025 msg="terminal cleanup completed"
ts=2026-07-12 18:21:13,094 level=info task_id=p6-04-config-writer-schema from_status=running to_status=failed msg="status changed"
ts=2026-07-12 18:21:13,875 level=info task_id=p6-04-config-writer-schema final_status=failed pr_url=None cleanup_safe=false msg=terminal
p6-04-config-writer-schema: failed (rerun/continue)
```

Здесь на вопрос был дан ответ `y`, команда продолжила выполнение и в итоге завершилась статусом `failed`.

## Уведомление в Telegram (пришло после случая B)

```
[p6-04-config-writer-schema] status=failed reason=git checkout main failed (exit=1): error: Your local changes to the following files would be overwritten by checkout:
 README.md
 docs/mdlint_v2/P6-init/index.md
 docs/mdlint_v2/glossary.md
 packages/cli/src/commands.ts
 packages/cli/src/init-command.ts
 packages/cli/src/init-prompter.ts
 packages/cli/src/program.ts
 packages/cli/test/init.e2e.test.ts
 packages/core/src/index.ts
Please commit your changes or stash them before you switch branches.
Aborting
```

Список файлов в этом сообщении не полностью совпадает со списком из `rerun: note: uncommitted changes (...)` выше (например, здесь отсутствуют `docs/mdlint_v2/P6-init/04-config-writer-schema.md`, `packages/cli/test/init.e2e.test.ts`, `packages/core/src/discovery/config-writer.ts`, `packages/core/test/config-writer.test.ts`).
