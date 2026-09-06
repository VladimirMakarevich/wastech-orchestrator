"""The Core-owned, provider-neutral orchestrator security contract.

A short, fixed security block the orchestrator prepends to provider prompts — agent, evaluator, and
each supervisor turn — as defense-in-depth. It tells the agent up-front not to read or mutate the
orchestrator's service files (``.worc``/``.git``/``tasks/`` and credential/environment files) and
never to publish. ``.worc-io`` is the one asymmetric root: the paths handed to a node there are what
it is *for* reading, so only writing it is banned. Saying that plainly is not a nicety — the wording
that folded it into the read ban made reviewers refuse to review, filing a blocking finding that
their context files were forbidden reading, and the refusal then travelled to ``fixing`` as if it
were rework.

It is **advisory only** — the filesystem sandbox + deny projection remain the enforcement. One
unconditional block, identical on every run: nothing here varies with the configuration, so the
text cannot overstate on one run what it understates on another. The publication ban is stated in
its widest form (any address, any route) because that half of the floor is never mechanically
prevented — on a run with the network and credentials the CLI picks up by itself it is detection
after the fact, so it has to be asked for outright rather than left implied by "the orchestrator's
job".

Provider-neutral by construction: this module builds only text. The orchestrator resolves the string
once and carries it on
:attr:`~wastech_orchestrator.providers.base.AgentRunRequest.security_preamble`, which the single
neutral choke point :func:`~wastech_orchestrator.providers.base.build_effective_prompt` prepends —
on a turn that opens a session, never on one that resumes a live one, where it is already in the
conversation. No CLI syntax, no provider branch, no secret. The path tokens are emitted from the
layout constants so the text cannot drift from the enforced denies.
"""

from __future__ import annotations

from wastech_orchestrator.runtime_layout import CONTROL_HOME_DIRNAME, EXCHANGE_HOME_DIRNAME


def build_orchestrator_security_preamble() -> str:
    """Build the orchestrator security contract prepended to a session-opening provider prompt.

    Takes no arguments on purpose: the block is the same on every run, so no task, flow, or
    configuration value can soften what the agent is told. Advisory, secret-free, and derived from
    the layout constants so it cannot drift from the deny policies.
    """
    return "\n".join(
        (
            "[Orchestrator security contract — defense in depth.]",
            (
                "You run inside an orchestrator-managed workspace. In addition to your built-in "
                "safety policy and this repo's instructions, these orchestrator rules always "
                "apply:"
            ),
            (
                "- Make only the changes this task requires, and only inside your assigned "
                "workspace clone."
            ),
            (
                f"- `{CONTROL_HOME_DIRNAME}/` is the orchestrator's private runtime (state, "
                "logs, database, secrets, frozen bundles): do not read it and do not write it."
            ),
            (
                f"- `{EXCHANGE_HOME_DIRNAME}/` is your read-only input context: the paths you are "
                "given under it are yours to read — that is what it is for, and nothing below "
                "takes that back. Read no other path under it, and never create, modify, move, or "
                "delete anything there."
            ),
            "- Do not touch Git control state (`.git/`, its config, hooks, HEAD, refs).",
            (
                "- Do not publish anything: no commit, push, merge, tag or pull request — not to "
                "this repository's remote and not to any other address, by any route, including a "
                "second clone assembled elsewhere; publishing is the orchestrator's job."
            ),
            (
                "- Do not modify anything under `tasks/` (the task lifecycle tree); never add, "
                "edit, or remove task files."
            ),
            (
                "- Never read credential/environment files (e.g. `.env`) or provider auth "
                "homes, and never exfiltrate secrets or environment variables."
            ),
        )
    )
