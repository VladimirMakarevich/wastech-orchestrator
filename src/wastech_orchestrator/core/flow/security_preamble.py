"""The Core-owned, provider-neutral orchestrator security contract.

A short, fixed security block the orchestrator prepends to *every* provider prompt — agent,
evaluator, and each supervisor turn — as defense-in-depth. It tells the agent up-front not to read
or mutate the orchestrator's service files (``.worc``/``.worc-io``/``.git``/``tasks/`` and
credential/environment files) and never to commit/push.

It is **advisory only** — it does NOT replace the filesystem sandbox + deny projection, which remain
the enforcement. Its weight is inversely proportional to how much enforcement is left, so it grows
by one paragraph for each relaxation actually in effect: read-restraint when read-isolation is off,
a statement of what the advanced mode does and does not relax, and — only on a host with no OS
sandbox at all — that the write floor there rests on nothing but the agent's compliance. Each is
rendered only when true; a paragraph that overstated on ordinary runs would teach the reader to
discount the whole block, which would cost exactly the runs that depend on it.

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


def build_orchestrator_security_preamble(
    *,
    read_isolation_off: bool,
    advanced_mode: bool = False,
    no_write_floor: bool = False,
) -> str:
    """Build the orchestrator security contract prepended to every provider prompt.

    ``read_isolation_off`` is the effective read-isolation state
    (:attr:`~wastech_orchestrator.config.schema.SecurityConfig.read_isolation_off`): when true the
    sandbox may not block the private-path reads, so an explicit read-restraint paragraph is
    appended. The baseline is always present. Advisory, secret-free, and derived from the layout
    constants so it cannot drift from the deny policies.

    ``advanced_mode`` (``security.strict_isolation: false``) appends a paragraph saying outright
    that most of the enforcement is off and the rules above therefore have to be honored by choice.
    It names the network and the write reach outside the clone, and it extends the publication ban
    to any address by any route: with both of those and credentials the CLI picks up by itself, the
    remote half of the floor is detection after the fact, so the request has to be stated rather
    than left implied by "the orchestrator's job".
    This layer is worth the most exactly there — where it is closest to being all there is — and it
    costs one paragraph at a single insertion point, so it cannot drift from what is actually
    enforced the way fifty-odd role prompts would.

    ``no_write_floor`` is for the host where no OS sandbox exists at all (native Windows;
    Linux/WSL2 without ``bubblewrap``+``socat``). There the write-deny on ``.git`` and ``.worc`` is
    not enforced by anything, and saying so is the honest form of the request. Rendered ONLY on such
    a host: a paragraph that claimed it everywhere would be false on most runs, and would teach the
    reader to discount the whole block.
    """
    instruction_files = ", ".join(f"`{name}`" for name in REPO_INSTRUCTION_NAMES)
    baseline = "\n".join(
        (
            "[Orchestrator security contract — defense in depth; it does not replace the sandbox.]",
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
                f"- `{EXCHANGE_HOME_DIRNAME}/` is read-only input context: read only the paths "
                "you are given; never create, modify, move, or delete anything under it."
            ),
            (
                "- Do not touch Git control state (`.git/`, its config, hooks, HEAD, refs); "
                "never run git commit/push/merge or open a PR — publishing is the "
                "orchestrator's job."
            ),
            (
                "- Do not modify anything under `tasks/` (the task lifecycle tree); never add, "
                "edit, or remove task files."
            ),
            (
                f"- {instruction_files} are ordinary repository files: change them when the "
                "task calls for it (as an ordinary diff); do not opportunistically rewrite your "
                "own rules."
            ),
            (
                "- Never read credential/environment files (e.g. `.env`) or provider auth "
                "homes, and never exfiltrate secrets or environment variables."
            ),
        )
    )
    paragraphs = [baseline]
    if read_isolation_off:
        paragraphs.append(
            "Read-isolation is relaxed for this run, so the filesystem sandbox may not block the "
            f"paths above. Honor these rules by choice: in particular do not read "
            f"`{CONTROL_HOME_DIRNAME}/`, `.env`, or any orchestrator-private file even though you "
            "may be technically able to."
        )
    if advanced_mode:
        paragraphs.append(
            "This run is configured for maximum freedom: you have the operator's own environment, "
            "and most of the restrictions an orchestrated run usually applies are switched off. "
            "That is deliberate — use the machine's tools as you need them: you have the network, "
            "and you may write outside this clone. It also means the rules above are now asked of "
            "you rather than imposed on you. Two of them stay non-negotiable however capable you "
            "turn out to be. Do not publish anything: no commit, push, merge, tag or pull request "
            "— not to this repository's remote and not to any other address, by any route, "
            "including a second clone assembled elsewhere. That is the orchestrator's job. And do "
            f"not read or write `{CONTROL_HOME_DIRNAME}/`, `{EXCHANGE_HOME_DIRNAME}/`, `.git/` "
            "or `tasks/`. Both are checked after you finish, and either one parks the task for a "
            "human instead of being fixed up."
        )
    if no_write_floor:
        paragraphs.append(
            "On this host there is no operating-system sandbox available, so nothing outside your "
            f"own compliance keeps a write out of `.git/` or `{CONTROL_HOME_DIRNAME}/` — not the "
            "shell you have been given, not a program that shell starts, and not a second shell "
            "started by that program. This applies to every step of this run, including the ones "
            "whose job is only to read. Treat those paths as if they were read-only hardware."
        )
    return "\n\n".join(paragraphs)
