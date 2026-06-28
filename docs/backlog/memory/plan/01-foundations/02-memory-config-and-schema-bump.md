# 01.2 — MemoryConfig & schema bump

[phase](index.md) · [design §8](../../design.md) · [acceptance: AC-S4/S5](../../acceptance-criteria.md)

**Goal:** add the `MemoryConfig` block with a global enable/disable and the bounded knobs, wired into the config pipeline, without breaking existing configs.

## Scope

In: dataclass, loader parse, defaults, example yaml, schema-version bump. Out: behavior that consumes the knobs (later phases).

## Approach

- `MemoryConfig` dataclass in `config/schema.py`: `enabled: bool` (default chosen so disabled = today's behavior — see Q10), tier caps/TTLs, packet caps, cleanup budget (scan/edit/wall-clock), promotions-per-pass (default 0).
- Wire into `OrchestratorConfig` with a safe default; parse in `config/loader.py` (tolerate-and-default an absent block).
- Document the block in `packaged/config.example.yaml`.
- Bump `CONFIG_SCHEMA_VERSION` (currently **23**). An older config without the block must load with defaults — **not** a fatal error. Keep any new fatal check only where there is no safe runtime fallback.

## Files

- `src/wastech_orchestrator/config/schema.py`, `config/loader.py`, `packaged/config.example.yaml`.

## Tests

- Absent memory block → defaults, no error (AC-S5).
- `enabled: false` parses and is respected by a guard the later phases check (AC-S4 groundwork).
- Round-trip + upgrade test for the bumped version.

## Done when

Config loads old and new shapes; example documents the block; version bumped; suite green.
