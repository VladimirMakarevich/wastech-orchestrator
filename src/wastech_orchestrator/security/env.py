"""Environment allowlist.

A child process started by the orchestrator receives **only** the environment variables named in
``security.allowed_environment`` that are present in the parent environment — never the parent's
full environment. No secret or token is ever forwarded implicitly; git/agent credentials are
configured outside the orchestrator (.agents/rules/security.md).

This module has no provider knowledge and is reused by every adapter (P2/P3) and the Check
Runner (P5).
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence


def build_child_env(
    allowed_keys: Sequence[str],
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the child environment from only the allowlisted keys present in the parent.

    :param allowed_keys: the ``security.allowed_environment`` allowlist, in order.
    :param parent_env: the environment to draw from; defaults to the live ``os.environ``.
    :returns: a fresh dict containing exactly the allowlisted keys that exist in ``parent_env``,
        in allowlist order. A key absent from the parent is skipped (never added as empty).
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    return {key: source[key] for key in allowed_keys if key in source}
