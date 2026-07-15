# Разбор прогона: `p8-01` — Frontmatter schema и unified skill model

- **Целевой репозиторий:** `/Users/a1234/Documents/GitHub/wastech-mdlint`
- **Финальный статус:** `done` · attempt 1 · `fix_iterations: 2` (2 review-реворка, 0 test-fix) · PR #14
- **Окно:** 2026-07-15 12:10 → 12:37 UTC
- **Тип:** код-задача (не авторинг) — новая модель `Skill` + валидация + рендер frontmatter.
- **Контекст фазы:** см. [обзор фазы P8](2026-07-15-p8-skills-phase-review.md) — корневые причины и рычаги общие.

## Рамка

Путь: `planning → implementation → testing(pass) → review(rework) → fixing → testing(pass) → review(rework) → fixing → testing(pass) → review(accept) → documentation → publish`. Все проверки зелёные, инфра чистая, `stage_attempts=1`.

Diff (по замыслу, scope-крипа нет): 7 файлов, +244 / −224 — `packages/core/src/skills/skill-model.ts`, `packages/core/src/index.ts`, `packages/core/src/compile/synthesize.ts`, `packages/core/test/skill-model.test.ts`, плюс `docs/mdlint_v2/glossary.md` и phase-doc.

## Что гоняло круги (оба реворка — про один инвариант)

Из `evaluations.findings_json` (kind `in_flow_verdict`):

- **Round 1 (high):** валидатор проверяет лишь непустоту `path` (`path: z.string().min(1)` в `skill-model.ts`) — абсолютные, `\`-разделённые, `./…` и escaping-за-корень значения проходят валидацию, хотя задача вводит модель именно для static/generated skills, где инвариант — repo-relative POSIX. «Корректность пути оставлена на вызывающего».
- **Round 2 (high):** после первой правки `isRepoRelativePosixPath()` всё ещё принимает ненормализованные формы `skills/./example/SKILL.md`, `skills//example/SKILL.md` — `validateSkill()` возвращает `ok: true` для того, что не является нормализованным repo-relative POSIX.

## Вывод по задаче

Оба круга — **недо-реализация одного инварианта пути**, который дословно перечислен в собственном роль-промпте автора ([`implementation.md`](../../../wastech-mdlint/.worc/flows/implementation/implementation.md) → `## Hard Invariants`: «public data and reports use repository-relative POSIX paths — normalize `\` to `/`»). Автор на `reasoning: low` реализовал инвариант поверхностно (min(1)), затем частично (без нормализации `.`/`//`). Это чистая иллюстрация **F1** из обзора фазы: контекст и требование были на руках, не хватило точности рассуждения.

Ревьюер (Codex @ high) сработал образцово — поймал именно ту дыру в публичном контракте, которую и должен. Это здоровое поведение, менять ревью не нужно.

**Рычаг:** F1 — `implementation.reasoning: low → high`. Задача уложилась бы в 0–1 круг. Отдельного, специфичного для `p8-01` рычага нет.
