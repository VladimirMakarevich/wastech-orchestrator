"""Provider layer.

The only place where the syntax of a specific CLI lives. Core interacts
with providers ONLY through the `AgentProvider` contract from `base.py`.
Providers do not perform fallback and do not change the state machine.
"""
