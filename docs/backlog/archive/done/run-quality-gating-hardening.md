# Run-quality & gating hardening (evaluator fail-closed, complete diff, planning HITL, codex usage)

Status: **implemented** (F19–F22, 2026-07-04) Date: 2026-07-04 Owner: Vladimir Makarevich

Группирует четыре run-quality/gating-находки пост-мортема `p4-01-context-graph-model-v2` (проход 6, [отчёт](../../../analysis/p4-01-context-graph-model-v2-run-analysis.md), [TEST-FINDINGS.md](../../../../TEST-FINDINGS.md) F19–F23) в один фикс-скоуп. Центральное решение — сделать in-flow evaluator **fail-closed**; остальные три (полный `current.diff`, planning без plan-mode UX, codex usage) — сопутствующее упрочнение того же прогона, правится вместе. Ранняя запись-решение, не спецификация задачи.

## The problem

**F19 (CRITICAL) — review-evaluator фактически no-op на живых провайдерах.** В прогоне: `evaluations` записал `review / in_flow_verdict / verdict=accept / findings_json=[]`, при том что codex-ревьюер выдал 3 `high` blocking-находки (верифицированы в коде PR #8). Флоу пропустил `fixing` и опубликовал PR как `done` (false-green), а supervisor поверх выдал уверенное «clean run, no interventions needed». Корень: роль-промпт ревью просит находки **прозой**, а `EvaluatorNodeRunner._extract_findings` читает их **только из `structured_output`**, при `output_schema=None` не заполняемого ни одним провайдером → находок ноль → вердикт fail-**open** в `accept`. Проходит в fake-CLI юнит-тестах (они кладут `structured_output.findings`), мёртв на живых claude/codex. Итог: единственный качественный гейт после `checks` не гейтит, провайдер-агностично.

**Сопутствующие пробелы того же прогона:**

- **F20 (HIGH) — `current.diff` неполон.** `git_manager.py:write_current_diff` = `git diff <base>` (база vs рабочее дерево) не включает untracked-файлы (весь новый тест-файл на 163 строки выпал из артефакта) и без `--text` рендерит файлы с NUL как «Binary files differ» (ядро изменения скрыто; NUL-делимитер пришёл из пре-существующего P3-кода). Ревью-`{diff_path}`, тело PR и failure-report видят неполную картину.
- **F21 (MEDIUM) — planning обходит `human_input`.** read-only профиль claude → `--permission-mode plan` активирует plan-mode UX (`AskUserQuestion`/`ExitPlanMode`/`~/.claude/plans`), в который агент увёл 2 реальные развилки; `human_input` остался null → оркестратор не встал на MANUAL_ACTION_REQUIRED → молчаливое угадывание дефолтов.
- **F22 (LOW) — codex usage не снимается.** codex-evaluator вернул `usage` пустым → стоимость codex-узлов невидима, бюджет прогона неполный.

## Constraints

- **Provider abstraction** ([architecture.md](../../../../.agents/rules/architecture.md)): извлечение вердикта/находок остаётся в core (`evaluator.py`), не в адаптерах; адаптеры лишь отдают `structured_output`/`usage`. Core не знает CLI-синтаксис.
- **Только оркестратор коммитит/публикует** — не меняется.
- **Нет секретов в логах/диффах** ([security.md](../../../../.agents/rules/security.md)): включение untracked-файлов в `current.diff` должно проходить через существующий `redact_text` (write_current_diff уже редактирует).
- **Нет per-model allowlist** (прежнее решение) — F21 его не вводит.
- **State machine неизменна**: fail-closed использует существующие исходы `rework`/`manual`, без нового статуса.
- **Кросс-платформенность** ([coding-style.md](../../../../.agents/rules/coding-style.md)): `--text`/intent-to-add и детекция NUL работают на Win/Linux/macOS.

## Alternatives considered

| Находка | Альтернатива | Почему отклонена |
| --- | --- | --- |
| F19 | Do nothing | Гейт остаётся no-op — теряется весь смысл ревью и валидность кампании. |
| F19 | Только prose-parser вердикта | Хрупко (regex по «\*\*blocking\*\*»/severity); `review/findings.json` (его читает `fixing`) всё равно надо собирать в структуру. |
| F19 | Гибрид schema + prose-fallback | Жизнеспособно как запасной путь, если codex не держит схему (см. Open Q1) — но лишняя сложность, пока схема не проверена. |
| **F19** | **Structured findings-схема, fail-closed** | **Выбрано.** Машинно-валидируемо, сразу кормит `findings.json`, гейт снова работает. |
| F21 | Оставить plan mode + disallow интерактивных тулов | Нет гарантии, что plan-mode-встроенные `AskUserQuestion`/`ExitPlanMode` можно запретить через `--disallowedTools`. |
| F21 | Только усилить роль-промпт | Ненадёжно — модель в plan mode тянется к plan-тулам. |
| **F21** | **`plan`→`default` + whitelist `Read,Glob,Grep`** | **Выбрано.** Правки заблокированы отсутствием Edit/Write в allowlist, plan-UX не активируется → развилки идут в `human_input`. |

## Decision

Упрочняем качество/гейтинг прогона одним скоупом: **(F19)** in-flow evaluator становится fail-**closed** и извлекает находки из обязательной **structured findings-схемы** (`severity`/`path`/`what`/`fix`), передаваемой как `output_schema` и требуемой роль-промптом ревью; пустой/неразобранный вердикт **никогда** не даёт `accept`. **(F20)** `write_current_diff` отдаёт полный артефакт (untracked через intent-to-add + `--text`), чтобы ревью, тело PR и failure-report видели всё изменение. **(F21)** read-only профиль claude использует `default`-режим + whitelist `Read,Glob,Grep`, чтобы кларификации шли через `human_input`, а не через plan-mode-тулы. **(F22)** codex-адаптер снимает `usage`. Цена отказа от F19: каждый прогон false-green'ит через ревью, обесценивая review/fixing-сигнал всей тест-кампании. Цена выбора схемы: вывод ревьюера ограничен формой схемы (митигируется — `findings.json` и так кормит `fixing`, а нарратив можно нести отдельным free-text-полем схемы).

## Open questions

1. **RESOLVED (smoke-тест 2026-07-04) — pure-schema валиден для codex 0.139.0.** Вопрос был: надёжно ли codex отдаёт schema-вывод под `--output-schema` при активных tools (риск по [#15451](https://github.com/openai/codex/issues/15451) — схема молча игнорируется при tools/MCP, репро на `-m gpt-5.4`; [#4181](https://github.com/openai/codex/issues/4181) — гард по `model_family`; [#19816](https://github.com/openai/codex/issues/19816)). Прогнан smoke на нашей 0.139.0: `codex … exec --sandbox read-only --json --output-schema s.json -m gpt-5.4 -c model_reasoning_effort=low` с промптом, форсирующим shell-tool. Результат: tools реально активны (`command_execution` `/bin/zsh -lc 'cat sample.md'` completed), и schema **соблюдена** — `--output-last-message` и `agent_message` = валидный `{"findings":[…]}`. То есть **#15451 не воспроизводится на 0.139.0, gpt-5.4 не режется**. **Важный нюанс канала:** structured-вывод приходит через **last-message-файл** (и текст `agent_message`), а терминальное событие `turn.completed` = только `{type, usage}` — **поля `output` НЕТ**. Наш `codex.py:244-246` читает `structured_output` из `event["output"]` → сейчас вернул бы `None`. → см. правку codex-адаптера в Implementation notes (F19). Гибрид/prose-fallback не нужен.
2. **PARTIALLY RESOLVED (unit test 2026-07-04) — argv confirms the gate; live CLI behavior not yet smoke-tested.** `tests/providers/test_claude_command.py::test_read_only_maps_to_default_with_readonly_allowlist` asserts the built argv is `--permission-mode default` with `Edit`/`Write` absent from `--allowedTools`. What is **not** yet confirmed is that a real `claude` process actually refuses a write under that argv (no live smoke run, unlike F19's codex 0.139.0 smoke test). Tracked as a follow-up: [follow_ups.md](../../follow_ups.md) 2026-07-04 "Live-behavioral confirmation of Claude `read-only`…".
3. **RESOLVED — implemented as recommended: straight to `manual`.** A missing/malformed `findings` array raises `EvaluatorInfraError` directly (the same path an evaluator that could not run at all takes), degrading to `manual_action_required` without spending a `rework`/fixing cycle. A well-formed (including empty) `findings` array still routes through the normal `accept`/`rework` outcomes.

## Implementation notes (as built)

- **F19** — `core/flow/nodes/evaluator.py`: `_FINDINGS_SCHEMA` (severity/path/what/fix, `required: [findings]`) is now `_build_request`'s `output_schema` (was `None`). `_findings_or_none` (replaces `_extract_findings`) returns `None` — not `[]` — when `structured_output` is missing or its `findings` key isn't a list; `run()` raises `EvaluatorInfraError` on `None` (fail-closed → manual, see Q3), and only a real list (incl. empty) reaches `_verdict`/`_to_finding`. `packaged/flows/implementation/review.md` now asks for the schema explicitly; the sibling `verifier.md`/`critic.md` roles already referenced "the output schema" and now actually receive one (this was a systemic gap, not review-only). **codex** (`providers/codex.py` `parse_events`) parses `last_message_text` as JSON into `structured_output` when a schema was requested and the terminal event carried no `output` field (verified against codex-cli 0.139.0's real shape — `turn.completed` is `{type, usage}` only); an unparseable last message leaves `structured_output` at `None` (fail-closed, not guessed).
- **F20** — `git_manager.py` `write_current_diff`: untracked files are bracketed with a transient `git add --intent-to-add` / `git reset` (no persistent index mutation — `changed_code_entries`/`changed_code_paths` still see them as `??` afterward) so their full content lands in the diff, and `--text` forces a textual diff even when Git's heuristic misclassifies a file as binary.
- **F21** — `providers/claude.py` `_PROFILE_MAP["read-only"]`: `("plan", …)` → `("default", ("Read", "Glob", "Grep"))`; `_MODE_ORDER`/the `bypassPermissions` ban are unchanged. See Q2 for the live-verification gap.
- **F22** — `providers/codex.py` `parse_events`: the terminal event's own `usage` field is now read directly (mirrors `claude.py`'s `parse_stream_json`), in addition to the existing separate `usage`/`token_count` event types.
