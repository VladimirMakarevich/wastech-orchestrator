---
id: task-001
title: "Пример задачи: добавить валидацию формы логина"
agents:                     # опциональный per-stage override (только из agents.allowed)
  planning: claude
  implementation: claude
  review: codex
  fixing: claude
contacts: ["@team-lead"]    # кого пинговать в Telegram при вопросах
---

## Описание

Кратко и конкретно опиши, что нужно сделать. Этот файл парсится оркестратором:
front matter → нормализованный task manifest, тело → контекст для агента.

## Acceptance criteria

- [ ] что должно работать после реализации;
- [ ] какие тесты добавить/обновить.

## Ограничения

- не трогать модуль billing;
- без новых зависимостей без одобрения.
