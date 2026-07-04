# TEST-FINDINGS — сквозные находки прогона оркестратора на wastech-mdlint

Журнал находок сквозного тестирования оркестратора на реальном проекте `wastech-mdlint`. Нумерация `F<N>` **сквозная**. Находки F1–F18 (проходы 1–5, задачи P0) заархивированы в [docs/analysis/archive/TEST-FINDINGS.md](docs/analysis/archive/TEST-FINDINGS.md) и остаются в силе; этот root-файл — активный журнал кампании P4, продолжающий нумерацию с **F19** (проход 6). Ссылки на F1–F18 валидны и в `TEST-CHECKLIST.md`.

Формат записи: **категория · серьёзность · уверенность · доказательство · корневая причина · рычаг (file:line) · зона (target/orchestrator) · статус**.

---

## Проход 6 — задача `p4-01-context-graph-model-v2` (2026-07-04)

Прогон: `worc run` → `done`, PR [#8](https://github.com/VladimirMakarevich/wastech-mdlint/pull/8) (не смержен, `auto_merge:false`), `fix_iterations=0`. Флоу `implementation`: refinement (skip, per-task) → planning (claude opus-4-8/high) → implementation (claude) → testing (checks) → review (codex gpt-5.4/xhigh) → documentation (claude) → publish. Отчёт: [docs/analysis/p4-01-context-graph-model-v2-run-analysis.md](docs/analysis/p4-01-context-graph-model-v2-run-analysis.md).

---

### F19 · review-evaluator фактически no-op: прозаические находки теряются, вердикт fail-open в `accept` · **CRITICAL** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04)**

**Доказательство.**

- `state.db evaluations` id=85: `node_id=review, kind=in_flow_verdict, verdict=accept, findings_json=[]`. `node_runs.review.outcome=accept`. `tasks.review_fix_cycles=0`.
- А в `stages/review/run-000053/1-codex/last-message.txt` codex-ревьюер написал **ТРИ `high` blocking**-находки (все верифицированы в фактическом коде PR #8):
  1. `build-context-graph.ts:141-148` — тип ребра `link.anchor?.length>0 ? "anchor" : "link"` без валидации фрагмента по heading-slug целевого документа → `[x](b.md#missing)` создаёт ложное anchor-ребро (нарушает AC «anchor = heading-slug match»);
  2. `build-context-graph.ts:68-72,80` — `extractDefinedIds` берёт ID только из table-колонок; «(+ headings)» из AC #3 осознанно отложено в P4.04 (спорное scope-суждение) → heading-объявленные ID теряются;
  3. `build-context-graph.ts:125` — идентичность узлов из `[...documents.keys()]`, а рёбра из `document.path` (стр. 111/142) → при абсолютных ключах `loadDocuments()` узлы/рёбра разъезжаются, ломая repo-relative POSIX-инвариант.
- Флоу пропустил `fixing` и ушёл review → documentation → publish; PR #8 открыт как `done` с 3 известными багами (false-green). Супервайзер (evaluations id=86) прочитал `accept` и выдал уверенный синтез «clean run, no interventions needed, each green on the first pass» — то есть отмыл false-green в убедительное резюме.

**Корневая причина.** Рассинхрон контракта «промпт↔экстрактор». Роль-промпт ревью ([packaged/flows/roles/…] / target `.worc/flows/roles/`) просит находки **прозой** («Your findings are consumed by a downstream LLM agent… one entry per finding: severity, path, what's wrong, fix») — без JSON/схемы. Но evaluator извлекает находки **только из `structured_output`**, которого при `output_schema=None` не бывает ни у одного провайдера, и прозы-парсера нет → находок ноль → вердикт `accept`. Проходит в fake-CLI юнит-тестах (они кладут `structured_output.findings`), мёртв на живых провайдерах.

**Рычаг.**

- [core/flow/nodes/evaluator.py:178](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L178) — `output_schema=None` («evaluators parse findings directly; no provider schema enforced»).
- [core/flow/nodes/evaluator.py:98,138,268-275](src/wastech_orchestrator/core/flow/nodes/evaluator.py#L268) — `_extract_findings` читает только `structured_output["findings"]`.
- Оба провайдера пишут схему и заполняют `structured_output` только при заданном `output_schema`: [providers/claude.py:260,330](src/wastech_orchestrator/providers/claude.py#L260), [providers/codex.py:165,246,325](src/wastech_orchestrator/providers/codex.py#L165).
- Варианты фикса (любой fail-**closed**): (1) передать evaluator'у findings-схему (severity/path/what/fix) и требовать её в роль-промпте — оба провайдера умеют `--json-schema`/`--output-schema`; (2) добавить прозы-парсер по `final_message`/`last-message.txt` при пустом `structured_output`, а при невозможности распарсить — `rework`/`manual`, никогда не тихий `accept`.

**Smoke-тест (2026-07-04, codex 0.139.0).** Выбран путь (1) structured-схема, fail-closed. Smoke подтвердил: codex под `--output-schema` при **активных tools** (`command_execution` `cat sample.md`) отдаёт валидный `{"findings":[…]}` (#15451 НЕ воспроизводится, gpt-5.4 не режется), НО через **last-message-файл**, а не через поле `output` терминального события → `codex.py:204-246` надо доучить парсить last-message как JSON в `structured_output`. Детали и решение — в ADR [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

**Влияние.** Единственный качественный гейт после checks — no-op; кросс-провайдерное ревью ловит реальные баги, а оркестратор их выбрасывает и публикует. Это самый крупный рычаг прогона.

**Исправлено (2026-07-04).** `evaluator.py` теперь задаёт обязательную findings-схему (`_FINDINGS_SCHEMA`) в `output_schema`; отсутствующий/неразобранный `findings`-массив — fail-**closed** (`EvaluatorInfraError` → `manual_action_required`), никогда тихий `accept`. `codex.py` доучен парсить last-message-файл как JSON, когда терминальное событие не несёт `output` (подтверждено smoke-тестом на 0.139.0). Роль-промпт ревью обновлён под схему. Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

---

### F20 · `current.diff` неполон: выпадают untracked-новые файлы и файлы с NUL рендерятся «Binary files differ» · **HIGH** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04)**

**Доказательство.**

- `current.diff` артефакта: 7 файлов, **тест-файла нет**; ядро `build-context-graph.ts` показано как `Binary files differ` (содержимое скрыто).
- Фактический коммит ветки (`git diff ce946f6..feat/p4-01-context-graph-model-v2 --stat`): **8 файлов, включая `packages/core/test/build-context-graph.test.ts` (+163 строки)**; `build-context-graph.ts | Bin 5412 -> 11342 bytes`.
- Причина «binary»: базовый blob P3 (`58dbb7f`) содержит **3 NUL-байта** (`` `${document.path}\x00${target}` `` — NUL-делимитер в легаси-dedup-ключе); новый blob NUL-free (задача сняла dedup). git видит NUL в базовой стороне → весь дифф файла бинарный.

**Корневая причина.** [git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173) `write_current_diff` = `git diff <base_branch>` (база vs рабочее дерево). До publish в недекомпозированном прогоне ничего не закоммичено, а `git diff <base>` **(а)** не включает untracked-файлы → новый тест-файл выпадает; **(б)** без `--text` рендерит NUL-детектированные файлы как «Binary files differ». Ревью-узел получает этот `{diff_path}` (evaluator.py:175) ДО publish — то есть с неполным диффом (в этот раз codex компенсировал чтением рабочего дерева read-only-песочницей). Тот же неполный дифф идёт в тело PR и failure-report.

**Рычаг.** [git_manager.py:1173](src/wastech_orchestrator/git_manager.py#L1173): включать untracked-файлы (напр. `git add -N`/intent-to-add перед diff, либо явно перечислять untracked) и передавать `--text`, чтобы NUL-содержащие файлы всё равно рендерились текстом. Прецедент уже есть — комментарий 1159-1168 фиксирует прошлый фикс understatement (`HEAD`→`base`). Зона orchestrator (каждый репо).

**Влияние.** Ревью, зависящее строго от `{diff_path}`, не видит НИ ОДНОГО нового файла (для этой задачи — весь тест-файл + новую логику) и не видит содержимое файлов с NUL. Деградирует и человеко-обзор PR.

**Исправлено (2026-07-04).** `write_current_diff` временно `git add --intent-to-add` untracked-файлы перед diff и сразу `git reset` (без постоянной мутации индекса), плюс `--text` на самом diff. Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

---

### F21 · planning в `--permission-mode plan` уводит развилки в `AskUserQuestion`/`ExitPlanMode` мимо оркестраторского `human_input` · **MEDIUM** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04, live-подтверждение allowlist-гейта — follow-up)**

**Доказательство.**

- `stages/planning/…/request.json` argv: `--permission-mode plan --allowedTools Read,Glob,Grep` (в `--disallowedTools` только git/gh/secrets; `AskUserQuestion`/`ExitPlanMode` НЕ запрещены).
- `stages/planning/…/result.json` `structured_output.human_input = null` (при том что схема содержит контракт `human_input`).
- `final_message`: «The plan is complete and submitted… awaiting your approval via ExitPlanMode»; `content`/`plan.md`: «written to `~/.claude/plans/…`», «AskUserQuestion went unanswered; recommended defaults applied».
- Две реальные развилки (замороженные имена полей `path/from/to` vs спека; id-ref без колонки references) агент вынес через нативный `AskUserQuestion`, ответа в headless нет → применил свои дефолты, а `human_input` оставил null → оркестратор не встал на MANUAL_ACTION_REQUIRED.

**Корневая причина.** [providers/claude.py:74](src/wastech_orchestrator/providers/claude.py#L74) — профиль `read-only → ("plan", …)`. Plan mode Claude Code активирует интерактивный UX (ExitPlanMode/AskUserQuestion/`~/.claude/plans/`), который перехватывает кларификации мимо структурного `human_input`.

**Рычаг.** [providers/claude.py:74](src/wastech_orchestrator/providers/claude.py#L74): либо режим `default` + whitelist `Read,Glob,Grep` (правки всё равно заблокированы отсутствием Edit/Write в allowlist, но plan-UX не активируется → развилки идут в `human_input`); либо оставить plan mode, но добавить `AskUserQuestion`/`ExitPlanMode` в `--disallowedTools` (стр. ~249). Проверить, что `default`+allowlist действительно блокирует Write/Edit. Зона orchestrator.

**Влияние.** HITL-гейт planning де-факто недостижим для claude → реальные операторские развилки авто-дефолтятся молча. В этот раз дефолты вышли разумные (урона нет), но механизм не сработал.

**Исправлено (2026-07-04).** `providers/claude.py`: `read-only → ("default", ("Read","Glob","Grep"))` (было `"plan"`). Юнит-тест подтверждает Edit/Write отсутствуют в `--allowedTools`; живая проверка, что реальный `claude`-процесс действительно отказывает в записи под этим режимом, вынесена в follow-up (не блокирует статус находки). Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md), [follow_ups.md](docs/backlog/follow_ups.md) 2026-07-04.

---

### F22 · codex-evaluator не отдаёт usage/токены (`0` в result.json) — пробел cost-наблюдаемости · **LOW** · уверенность HIGH · зона **orchestrator** · статус **RESOLVED (2026-07-04)**

**Доказательство.** `stages/review/…/result.json` codex: `usage` пуст, суммарно `in=0 out=0` при 510s работы; claude-узлы usage отдают (planning out=34266, impl out=36810 и т.д.). Итог по прогону считается без стоимости самого дорогого по времени узла.

**Корневая причина / рычаг.** Парсинг usage в [providers/codex.py:211-255](src/wastech_orchestrator/providers/codex.py#L211) не извлекает токены из codex `--json`-потока (в отличие от claude.py:333). Рычаг: снять usage из терминального события codex. Зона orchestrator.

**Влияние.** Нельзя оценить стоимость codex-узлов (review/любой codex-node) — слепое пятно в бюджете/аналитике.

**Исправлено (2026-07-04).** `parse_events` теперь читает `usage` прямо из терминального события (`turn.completed`/`result`), по аналогии с `claude.py`. Детали — [run-quality-gating-hardening.md](docs/backlog/archive/done/run-quality-gating-hardening.md).

---

### F23 · пре-существующий NUL-делимитер в P3 `build-context-graph.ts` делал файл git-binary · **LOW (informational)** · уверенность HIGH · зона **target (mdlint)** · статус RESOLVED-BY-TASK

**Доказательство.** Базовый blob `58dbb7f` (из PR #7, P3): 3 NUL-байта в строковом ключе `` `${document.path}\x00${target}` ``. Задача p4-01-v2 сняла старый `(from,to)`-dedup → новый файл NUL-free. То есть проблема пре-существующая (не вина прогона) и **попутно устранена** этой задачей; после мержа #8 будущие диффы файла станут текстовыми. Связано с F20 (следствие для артефакта диффа). Отдельного действия по оркестратору не требует; отмечено как контекст к F20.

---
