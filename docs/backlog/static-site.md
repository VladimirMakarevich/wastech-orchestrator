# Статический Retro Docs Site для wastech-orchestrator

## Summary

Добавить переносимый статический сайт документации на **MkDocs**, собираемый из Markdown-файлов репозитория. Сайт будет в стиле “old-fashioned retro web”: таблично-документальный вид, системные serif/monospace шрифты, beveled borders, яркие classic-link цвета, пиксельные/bitmap-акценты, но без ухудшения читаемости.

Публикация в GitHub Pages будет выполняться **только для tag push `v*`**, включая prerelease-теги `vX.Y.ZaN`, `vX.Y.ZbN`, `vX.Y.ZrcN`. Сборка сайта будет обычным `mkdocs build`, поэтому результат `site/` можно позже перенести на любой статический хостинг.

## Key Changes

- Добавить `mkdocs.yml`, docs extra в `pyproject.toml`: `mkdocs` + минимальные Markdown extensions для таблиц, fenced code и Mermaid-friendly блоков.
- Добавить `docs/index.md` как главную страницу сайта, основанную на текущем README, без дублирования всего README.
- Добавить hand-curated navigation:
  - Quick start / how it works / operations / cookbook
  - configuration / task authoring / telegram
  - architecture / functional map / system flows / functional blocks
  - LikeC4 guide
- `docs/backlog/**` не выводить в главной навигации; ссылки на backlog оставлять как GitHub source links.
- Добавить `docs/assets/` для retro CSS/JS/images:
  - основной stylesheet `retro.css`
  - небольшие bitmap/pixel assets для first-viewport identity
  - Mermaid init JS, если Mermaid-блоки рендерятся на клиенте
- Добавить маленький build helper, например `tools/stage_site_docs.py`, который перед MkDocs:
  - формирует gitignored staging-директорию `.site-src/`
  - копирует только публикуемые Markdown/assets
  - переписывает ссылки на файлы вне сайта (`src/`, `tests/`, `.agents/`, backlog) в GitHub `blob/<ref>/...`
  - сохраняет обычные внутренние Markdown-ссылки как site-local
- Добавить в `.gitignore`: `site/` и `.site-src/`.
- Обновить README или `docs/operations.md` короткой секцией:
  - локально: `pip install -e ".[docs]"`, `python tools/stage_site_docs.py`, `mkdocs serve`
  - production build: `mkdocs build --strict`
  - Pages source в настройках GitHub должен быть `GitHub Actions`.

## CI / Deploy

- Добавить `.github/workflows/site.yml`.
- Триггеры:
  - `pull_request` и `push` в `main`: только build check, без deploy.
  - `push.tags: ["v*"]`: build + deploy.
- Build job:
  - checkout
  - setup Python 3.12
  - `pip install -e ".[docs]"`
  - `python tools/stage_site_docs.py --ref "$GITHUB_REF_NAME"`
  - `mkdocs build --strict --site-dir site`
  - upload Pages artifact from `site/`
- Deploy job:
  - runs only when `github.ref` starts with `refs/tags/v`
  - permissions: `contents: read`, `pages: write`, `id-token: write`
  - environment: `github-pages`
  - actions: `configure-pages`, `upload-pages-artifact`, `deploy-pages`
- Existing `ci.yml` and `release.yml` remain focused on Python quality gate / GitHub Release; site deploy is separate.

## Test Plan

- Local:
  - `pip install -e ".[dev,docs]"`
  - `python tools/stage_site_docs.py --ref main`
  - `mkdocs build --strict --site-dir site`
  - `ruff check .`
  - `ruff format --check .`
  - `mypy src`
  - `pytest`
- Add focused pytest coverage for the staging helper:
  - internal docs links remain relative/site-local
  - links to `src/`, `tests/`, `.agents/`, `docs/backlog/` become GitHub source links
  - anchors like `#L123` or Markdown headings are preserved
- Manually verify generated `site/index.html`, navigation, Mermaid/code blocks, mobile width, and no unreadable overlap in the retro theme.

## Assumptions

- Generator: **MkDocs**.
- Content scope: user-facing docs and functional architecture docs; backlog stays in repo but not in primary site navigation.
- Publishing: **every `v*` tag** publishes the site.
- No orchestrator runtime behavior, provider contracts, security policy, CLI syntax, or state machine changes.
- Official references used for implementation details:
  - GitHub Pages custom workflows: https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages
  - MkDocs configuration/customization: https://www.mkdocs.org/user-guide/configuration/ and https://www.mkdocs.org/user-guide/customizing-your-theme/
