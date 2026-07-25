# Codex: orchestrator-controlled provider home (deferred hardening)

Status: **deferred — future hardening** Date: 2026-07-21 Owner: Vladimir Makarevich Source: read-isolation design review decision F7 ([review](../agent-worc-read-isolation/design-review-2026-07-21.md)); split out of [WRI-003](../agent-worc-read-isolation/wri-003-codex-permission-profile-isolation.md).

## Context

The agent-worc-read-isolation cluster isolates the Codex configuration surface **by layers** while keeping authentication in the operator's own `CODEX_HOME`: `--ignore-user-config` neutralizes the user `config.toml`, the project `.codex` layer is forced untrusted, generated execpolicy rules are supplied through an orchestrator-owned input, and every remaining layer (rules, hooks, MCP/apps/plugins, features) is inventoried with strict isolation failing on anything that cannot be proven safe. That was chosen over a dedicated provider home because it costs the operator nothing: no re-login, no lost sessions, and the repository policy "credentials and auth stay outside the orchestrator" holds trivially.

The layer-isolation approach has a structural residual: the operator home remains the root of the config stack, so its neutralization must be re-proven surface-by-surface on every host and after every Codex release. A missed or newly added layer (a new rules location, a new hook kind, a new plugin discovery path) silently joins autonomous runs until the inventory learns about it.

## Deferred task

Run autonomous Codex attempts from a dedicated, orchestrator-controlled `CODEX_HOME` under the private runtime state (the [out-of-tree relocation](relocate-private-home-out-of-tree.md) location, itself deferred), so that personal/user configuration cannot influence an autonomous run **by construction** rather than by enumeration:

- The controlled home holds auth/session state and the orchestrator-generated config/rules only; nothing else exists to inventory.
- No silent credential copying: install/preflight checks the controlled home's auth and, when missing, prints the exact one-time operator command (`CODEX_HOME=<path> codex login`) — the orchestrator itself never performs login and never copies credential material. File credentials stay owner-only on POSIX and ACL-restricted on Windows.
- Preflight fails closed with an actionable message while the controlled home is unauthenticated; headless/daemon setups document the login step as part of repository onboarding.
- Existing operator-home sessions are not migrated (greenfield); resume lineage starts fresh in the controlled home.
- The controlled home joins the internal provider deny set exactly as the operator home does today; sandboxed commands cannot read it.

## Preconditions

- WRI-003 (permission profiles, layer isolation, capability canaries) landed, and the [out-of-tree private-state relocation](relocate-private-home-out-of-tree.md) (deferred — see that record) is in place.
- A decision on multi-repo ergonomics: one controlled home per repository versus one per operator, and how `codex login` friction is presented in `install`/`preflight`.

## Why deferred

The operational cost (an interactive login per controlled home, orphaned sessions, friction for every existing install) buys defense-in-depth on top of a layer-isolation contract that WRI-003 must prove anyway — the canary/inventory evidence is required with or without the controlled home. Revisit when the layer inventory proves brittle in practice (e.g., a Codex release adds a config surface the preflight misses) or when multi-operator hosts make personal-config contamination a live problem.
