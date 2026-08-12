"""Security primitives shared across the orchestrator.

Holds the cross-cutting, provider-agnostic security helpers — the environment allowlist
(:mod:`wastech_orchestrator.security.env`) and the bypass-flag detector
(:mod:`wastech_orchestrator.security.forbidden_args`). Reused by the provider adapters and
the Check Runner.
"""

from __future__ import annotations
