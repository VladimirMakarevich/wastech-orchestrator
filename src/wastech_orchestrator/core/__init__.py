"""The deterministic Orchestrator Core (spec–).

This package holds the components that drive a task end to end: the state machine, loop control,
decomposition decision, recovery reconciliation, and the orchestrator pipeline itself. The Core
never builds CLI commands — it talks only to the Agent Router and the component interfaces
(State Store, Git Manager, Check Runner, ledger).
"""

from __future__ import annotations
