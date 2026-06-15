---
id: task-pagination
title: "Add page parameter to the users list endpoint"
---

## Description

The `GET /users` endpoint currently returns every user in one response. Add an optional `page` query parameter that returns a single page using the pagination metadata the API already exposes elsewhere. Keep the default (no `page`) behavior unchanged.

## Acceptance criteria

- [ ] `GET /users?page=2` returns the second page using the existing pagination metadata shape.
- [ ] Invalid `page` values (non-numeric, `< 1`) return HTTP 400 with the existing error shape.
- [ ] `GET /users` with no `page` keeps its current behavior.
- [ ] Add unit tests for a valid page, an invalid page, and the no-parameter default.

## Constraints

- Do not change the response shape of existing fields.
- No new dependencies.
