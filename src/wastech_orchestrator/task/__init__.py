"""Task layer: the normalized task model (P1).

The Task Parser and the §19 validation gate are deferred to P5 (they need the State Store + ledger).
"""

from __future__ import annotations

from wastech_orchestrator.task.model import (
    ALLOWED_TASK_KEYS,
    REQUIRED_TASK_FIELDS,
    TASK_ID_PATTERN,
    NormalizedTask,
    is_valid_task_id,
)

__all__ = [
    "ALLOWED_TASK_KEYS",
    "REQUIRED_TASK_FIELDS",
    "TASK_ID_PATTERN",
    "NormalizedTask",
    "is_valid_task_id",
]
