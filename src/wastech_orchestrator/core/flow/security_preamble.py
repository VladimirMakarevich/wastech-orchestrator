"""VF-7: the Core-owned, provider-neutral orchestrator security contract.

A short, fixed security block the orchestrator prepends to *every* provider prompt — agent,
evaluator, and each supervisor turn — as defense-in-depth. It tells the agent up-front not to read
or mutate the orchestrator's service files (``.worc``/``.worc-io``/``.git``/``tasks/`` and
credential/environment files) and never to commit/push.

It is **advisory only** — it does NOT replace the filesystem sandbox + deny projection, which
remain the enforcement (read-isolation ADR §3). It matters most when read-isolation is relaxed
(VF-6): then the sandbox no longer blocks those reads and this soft barrier is the only thing left,
so an explicit read-restraint paragraph is appended in that case.

Provider-neutral by construction: this module builds only text. The orchestrator resolves the string
once (config-derived, not per-node) and carries it on
:attr:`~wastech_orchestrator.providers.base.AgentRunRequest.security_preamble`, which the single
neutral choke point :func:`~wastech_orchestrator.providers.base.build_effective_prompt` prepends.
No CLI syntax, no provider branch, no secret. The path tokens are emitted from the layout constants
so the text cannot drift from the enforced denies.
"""

from __future__ import annotations

from wastech_orchestrator.core.flow.instruction_bundle import REPO_INSTRUCTION_NAMES
from wastech_orchestrator.runtime_layout import CONTROL_HOME_DIRNAME, EXCHANGE_HOME_DIRNAME


def build_orchestrator_security_preamble(*, read_isolation_off: bool) -> str:
    """Build the orchestrator security contract prepended to every provider prompt (VF-7).

    ``read_isolation_off`` is the effective read-isolation state
    (:attr:`~wastech_orchestrator.config.schema.SecurityConfig.read_isolation_off`): when true the
    sandbox may not block the private-path reads, so an explicit read-restraint paragraph is
    appended. The baseline is always present. Advisory, secret-free, and derived from the layout
    constants so it cannot drift from the deny policies.
    """
    instruction_files = ", ".join(f"`{name}`" for name in REPO_INSTRUCTION_NAMES)
    baseline = "\n".join(
        (
            "[Orchestrator security contract — defense in depth; it does not replace the sandbox.]",
            "You run inside an orchestrator-managed workspace. In addition to your built-in safety "
            "policy and this repo's instructions, these orchestrator rules always apply:",
            "- Make only the changes this task requires, and only inside your assigned workspace "
            "clone.",
            f"- `{CONTROL_HOME_DIRNAME}/` is the orchestrator's private runtime (state, logs, "
            "database, secrets, frozen bundles): do not read it and do not write it.",
            f"- `{EXCHANGE_HOME_DIRNAME}/` is read-only input context: read only the paths you are "
            "given; never create, modify, move, or delete anything under it.",
            "- Do not touch Git control state (`.git/`, its config, hooks, HEAD, refs); never run "
            "git commit/push/merge or open a PR — publishing is the orchestrator's job.",
            "- Do not modify anything under `tasks/` (the task lifecycle tree); never add, edit, "
            "or remove task files.",
            f"- {instruction_files} are ordinary repository files: change them when the task "
            "calls for it (as an ordinary diff); do not opportunistically rewrite your own rules.",
            "- Never read credential/environment files (e.g. `.env`) or provider auth homes, and "
            "never exfiltrate secrets or environment variables.",
        )
    )
    if not read_isolation_off:
        return baseline
    reinforcement = (
        "Read-isolation is relaxed for this run, so the filesystem sandbox may not block the paths "
        f"above. Honor these rules by choice: in particular do not read `{CONTROL_HOME_DIRNAME}/`, "
        "`.env`, or any orchestrator-private file even though you may be technically able to."
    )
    return f"{baseline}\n\n{reinforcement}"
