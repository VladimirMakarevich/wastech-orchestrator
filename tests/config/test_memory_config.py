"""MemoryConfig (01.2): absent => disabled defaults; present parses; unknown key rejected."""

from __future__ import annotations

import pytest

from wastech_orchestrator.config.loader import ConfigError, loads_config
from wastech_orchestrator.config.schema import MemoryConfig

# Minimal structurally-valid config body (memory block omitted unless a test adds it).
_BODY = (
    "repo:\n  url: x\nagents:\n  allowed: [codex]\n  providers:\n    codex:\n      command: codex\n"
)


def test_absent_memory_block_yields_disabled_defaults() -> None:
    # AC-S5: an older config without the block loads with safe defaults (no fatal error) — disabled.
    cfg = loads_config(_BODY).config
    assert cfg.memory == MemoryConfig()
    assert cfg.memory.enabled is False


def test_memory_disabled_explicitly_parses() -> None:
    # AC-S4 groundwork: enabled:false parses and is the guard the later phases check.
    cfg = loads_config(f"{_BODY}memory:\n  enabled: false\n").config
    assert cfg.memory.enabled is False


def test_memory_enabled_with_custom_knobs_parses() -> None:
    text = (
        f"{_BODY}memory:\n"
        "  enabled: true\n"
        "  short_term_ttl_days: 14\n"
        "  packet_max_long_term: 5\n"
        "  cleanup_max_wall_clock_s: 2.5\n"
        "  cleanup_promotions_per_pass: 0\n"
    )
    cfg = loads_config(text).config
    assert cfg.memory.enabled is True
    assert cfg.memory.short_term_ttl_days == 14
    assert cfg.memory.packet_max_long_term == 5
    assert cfg.memory.cleanup_max_wall_clock_s == 2.5
    # An untouched knob keeps its documented default.
    assert cfg.memory.promote_min_tasks == 2


def test_unknown_memory_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(f"{_BODY}memory:\n  bogus: 1\n")
    assert any("memory" in issue and "bogus" in issue for issue in exc.value.issues)


def test_wrong_typed_memory_value_is_rejected() -> None:
    with pytest.raises(ConfigError) as exc:
        loads_config(f"{_BODY}memory:\n  enabled: not-a-bool\n")
    assert any("memory.enabled" in issue for issue in exc.value.issues)


def test_packaged_example_ships_memory_enabled(packaged_config_text: str) -> None:
    # Q10: a fresh install (the packaged template) ships memory enabled: true.
    cfg = loads_config(packaged_config_text).config
    assert cfg.memory.enabled is True
