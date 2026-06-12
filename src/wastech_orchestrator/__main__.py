"""Enable ``python -m wastech_orchestrator`` (mirrors the ``wastech-orchestrator`` script)."""

from __future__ import annotations

from wastech_orchestrator.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
