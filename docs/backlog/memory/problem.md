# Problem

Status: **stable** Date: 2026-06-28 — [task hub](index.md)

## The problem

Today each orchestrator task is an island. Nothing a task learns survives into the next task against the same repository. This has three concrete, recurring costs:

- **Repo re-discovery.** Every task re-derives the target repo's structure, conventions, build/test commands, and fragile areas from scratch. The cost is paid in tokens and wall-clock, and it recurs identically on a repo the orchestrator has already worked on many times.
- **Lost lessons.** Decisions, gotchas, and conventions surfaced during one task — what broke, what the reviewer flagged, which approach was rejected and why, which command actually works — do not carry into the next task. The next agent rediscovers them, or re-violates them.
- **No entity knowledge.** There is no durable representation of the things the orchestrator reasons about across runs (key modules, files, tests, dependencies, owners, prior tasks). Cross-task reasoning like "we touched this module last week and its tests are fragile" is impossible.

## Why now

These costs compound the more the orchestrator is used on the same repository — exactly the steady-state we are building toward (autonomous `watch` over a queue of tasks on one repo). The longer we run without memory, the more tokens and review loops we burn re-learning the same things.

## The predictable failure mode

Memory bloat — duplicates, stale entries, contradictions, prompt noise — is **not** a present pain (there is no memory yet). It is the predictable failure mode once memory exists, and the empirical evidence is blunt that naive "just add memory / bigger context files" makes agents _worse_, not better (see the research). So curation, bounding, and safety are part of the problem statement from the start, not a later concern.

## What success looks like (one sentence)

The orchestrator stops losing expensive, repo-specific lessons between independent runs — **without** turning into an opaque, slowly-rotting, or attackable context dump.

## Sources

The full problem framing, landscape, and evidence are in [research/](research/index.md) (the two deep-research reports and the consolidated [blueprint](research/memory-architecture-blueprint.md)). This task supersedes the exploratory predecessor [../archive/done/orchestrator-memory.md](../archive/done/orchestrator-memory.md).
