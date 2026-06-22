# Per-node `network_access` — operator-owned, opt-in, default off

Status: **accepted** Date: 2026-06-22 Owner: Vladimir Makarevich

Detail file for the [follow_ups.md](follow_ups.md) item "Flow-engine: per-node `network_access` override". Adds an optional `network_access: true|false` field to a flow's agent and evaluator nodes so the operator can grant (or withhold) network for a single node — e.g. only the implementation or testing node — instead of the whole flow. Default is unchanged behavior: a node with no `network_access` inherits the flow-level grant, and a flow that grants nothing leaves every node offline.

## Problem

Network access is a single flow-wide switch today. A node may reach the network **iff** the flow document declares `network_policy` ([schema.py:168](../../src/wastech_orchestrator/core/flow/schema.py#L168)); the value is the same for every node in the flow. Both node runners compute it identically:

- agent nodes — `network_access=ctx.snapshot.doc.network_policy is not None` ([agent.py:398](../../src/wastech_orchestrator/core/flow/nodes/agent.py#L398))
- evaluator nodes — same ([evaluator.py:187](../../src/wastech_orchestrator/core/flow/nodes/evaluator.py#L187))

So an operator cannot say "let only the implementation node fetch packages, but keep refinement/planning/review offline." It is all-or-nothing per flow. The only flows that grant network are the two packaged research flows ([security_audit.yaml:9](../../src/wastech_orchestrator/core/flow/packaged/security_audit.yaml#L9) `advisories`, [deep_research.yaml:9](../../src/wastech_orchestrator/core/flow/packaged/deep_research.yaml#L9) `research`); the standard `implementation` flow grants none, so no implementation/testing node can reach the network at all.

## What the operator asked for

A per-node field, opt-in, that the operator sets on the specific node that needs it:

```yaml
nodes:
  - id: implementation
    kind: agent
    role_file: roles/implementation.md
    permission_profile: workspace-write
    network_access: true # only this node may reach the network
    output_artifact: plan
```

Stated constraints: **the operator owns the decision** (they author and run the flow file), and **the default is `network_access: false`** (no field ⇒ no node-level grant).

## Decisions (locked)

1. **Coexist with `network_policy`, do not replace it.** Per-node `network_access` is an **override** of the flow-derived default, not a new gate. Resolution per node: an explicit node value wins; absent it (`None`), the node inherits the existing `doc.network_policy is not None` default. This keeps every existing flow byte-for-byte identical in behavior (all nodes parse to `network_access=None` ⇒ inherit), and the two packaged research flows keep granting network to their nodes with no edit.
2. **`network_policy` is reframed from "hard ceiling" to "flow-wide default."** A node-level `network_access: true` in a flow that declares no `network_policy` **does** grant that node network (the operator's example case). This is a deliberate, documented relaxation of the old "ceiling" framing: the flow file is operator-authored and preflight-validated, so node-level network is an operator-owned posture, consistent with how the operator already owns the whole flow's network stance. (It never touches the filesystem sandbox/permission ceiling — see Risks.)
3. **Default off, tri-state field.** `network_access: bool | None`, default `None` = inherit. `true`/`false` = explicit per-node grant/deny. A node-level `false` is a real opt-**out**: it forces the node offline even in a flow whose `network_policy` would otherwise grant it.
4. **Applies to agent and evaluator nodes only.** Those are the two node kinds that launch an agent process carrying `network_access` (the field already exists on both runners). `checks`/`hitl`/`publish` nodes run no networked agent process and get no field.

## How it works today (the chain being extended)

`network_policy` (flow doc) → `_parse_flow_doc` resolves it ([snapshot.py:474-486](../../src/wastech_orchestrator/core/flow/snapshot.py#L474-L486)) → each node runner reads `ctx.snapshot.doc.network_policy is not None` and sets `AgentRunRequest.network_access` ([base.py:125](../../src/wastech_orchestrator/providers/base.py#L125), default `False`). The adapters map the bool onto their sandbox, never relaxing the filesystem ceiling:

- **Claude** — adds `WebFetch, WebSearch` to `--allowedTools` when true; omits them otherwise ([claude.py:224-228](../../src/wastech_orchestrator/providers/claude.py#L224)).
- **Codex** — adds `-c sandbox_workspace_write.network_access=true` when true ([codex.py:150-154](../../src/wastech_orchestrator/providers/codex.py#L150)).

The change inserts a per-node override between the flow default and the request: `request.network_access = resolve(node.network_access, doc.network_policy)`.

## Target design

A single resolution rule, applied in both runners (extract a helper to keep it DRY):

```python
def resolve_network_access(node_value: bool | None, policy: NetworkPolicy | None) -> bool:
    """Per-node override of the flow-wide network grant; None inherits the flow default."""
    if node_value is not None:
        return node_value
    return policy is not None
```

Place it next to `NetworkPolicy` in `core/flow/contracts.py` (pure, no IO) and call it from both node runners. Backward compatible: every current flow yields `node_value=None` ⇒ identical result.

## Change list

### A. Flow schema

- `core/flow/schema.py` — add `network_access: bool | None = None` to `AgentNode` (alongside the other optional fields) and to `EvaluatorNode`. All existing fields already carry defaults, so appending a defaulted field keeps the frozen-dataclass field ordering valid.

### B. Flow loader / snapshot

- `core/flow/snapshot.py` — add `"network_access"` to `_AGENT_FIELDS` ([snapshot.py:74-93](../../src/wastech_orchestrator/core/flow/snapshot.py#L74-L93)) and `_EVALUATOR_FIELDS` ([snapshot.py:94-109](../../src/wastech_orchestrator/core/flow/snapshot.py#L94-L109)), or `_reject_unknown` fails the flow closed. In `_parse_agent_node` ([snapshot.py:257-298](../../src/wastech_orchestrator/core/flow/snapshot.py#L257-L298)) and `_parse_evaluator_node` ([snapshot.py:301-327](../../src/wastech_orchestrator/core/flow/snapshot.py#L301-L327)) parse it tri-state: `na = raw.get("network_access"); network_access = None if na is None else bool(na)` — must guard `None` explicitly (`bool(None)` is `False`, which would silently turn "inherit" into "deny").

### C. Node runners (resolution)

- `core/flow/contracts.py` — add the `resolve_network_access` helper.
- `core/flow/nodes/agent.py:396-398` — replace `network_access=ctx.snapshot.doc.network_policy is not None` with `network_access=resolve_network_access(node.network_access, ctx.snapshot.doc.network_policy)`; update the comment (network is now a per-node override on top of the flow default, not purely a flow-ceiling dimension).
- `core/flow/nodes/evaluator.py:186-187` — same change.

### D. Packaged flows / examples

- **No change required** to `implementation.yaml` / `security_audit.yaml` / `deep_research.yaml` (the default `None` preserves their current behavior). Optionally add a commented `network_access` example to the operator flow-authoring doc and/or a packaged role-less sample so operators discover the field.

### E. JSON schema mirror

- The `flow.schema.json` referenced in `schema.py`'s docstring lives under [archive/outdated/flows/co-design/](archive/outdated/flows/co-design/) and is **archival, not a live source of truth** — do not update it as part of this task. If a live flow JSON schema is ever reinstated, add `network_access` there.

## Test impact

- **Loader (`tests/.../test_flow_snapshot.py` or the snapshot loader tests):** `network_access: true` on an agent node ⇒ `AgentNode(network_access=True)`; `false` ⇒ `False`; omitted ⇒ `None`. Same three for an evaluator node. Regression: a genuinely-unknown field is still rejected fail-closed (guards the allowed-set additions).
- **Resolution unit tests** for `resolve_network_access`: `(True, None)→True` (the operator's case — grant on a flow with no policy), `(False, RESEARCH)→False` (per-node opt-out beats a granting flow), `(None, RESEARCH)→True` and `(None, None)→False` (inherit, both branches).
- **End-to-end through the runners:** a node with `network_access=True` in a policy-less flow produces an `AgentRunRequest` with `network_access=True`; assert it reaches Claude `--allowedTools` (WebFetch/WebSearch present) and Codex `-c sandbox_workspace_write.network_access=true` — reuse the existing provider-argv tests that already exercise the network path, driven via the per-node value.
- Evaluator-node parity for the same.

## Docs impact

- Operator flow-authoring guidance ([docs/operations.md](../operations.md) / [docs/configuration.md](../configuration.md) wherever flow nodes are documented) — document the `network_access` node field: opt-in, default off (inherit), per-node `false` is an explicit opt-out, and the operator owns the grant. State the reframing: `network_policy` is the flow-wide default; a node value overrides it (a node can be granted network even when the flow declares no `network_policy`).
- Note the **Codex read-only asymmetry** (see Risks) wherever the field is documented.
- Functional map — fold the per-node override into [B30-flow-node-runners.md](../functional/blocks/B30-flow-node-runners.md), [B29-flow-definition-and-validation.md](../functional/blocks/B29-flow-definition-and-validation.md), and the network note in [B25-security-policy.md](../functional/blocks/B25-security-policy.md) during the already-pending "Full re-sync of functional-map `file:line` refs" pass; no separate doc sweep needed.

## Risks / out of scope

- **Codex read-only + network is a no-op (pre-existing, documented).** Codex enables sandbox network only for the `workspace-write` sandbox, so `network_access: true` on a **read-only** node has effect for Claude (the WebFetch/WebSearch tools are added) but is silently ineffective when Codex runs that node. This is the existing limitation tracked in the "Codex read-only + network sandbox combo" follow-up; this task does not fix it. The provider that actually runs a node is route-/fallback-dependent and not known at flow-load time, so this cannot be validated up front — document it as an asymmetry, do not add a validation rule.
- **Network only — never the filesystem ceiling.** As today, the resolved bool toggles only the network dimension; it never relaxes the permission profile / sandbox (`base.py` comment; claude "this only adds network tools — it never relaxes the filesystem permission mode"; codex "this toggles ONLY network"). A `read-only` node granted network stays read-only on the filesystem.
- **Relationship to the deferred per-host allowlist.** A node-level grant is still all-or-nothing for that node (full network, not a host allowlist). The per-host `network_policy` allowlist remains the separate deferred item ("Signature/hash flow registry + `network_policy` per-host allowlist"); the two compose later (per-node grant × per-host allowlist) without conflict.
- **No config-version bump.** This is a flow-schema (flow YAML) change with a backward-compatible default, not a `config.yaml` schema change; `schema_version` is untouched and existing flows load unchanged.
- **Out of scope:** changing which packaged flows grant network, per-host allowlisting, and the Codex read-only+network fix.

## Acceptance

- `ruff`, `mypy`, `pytest` green.
- An agent node with `network_access: true` in a flow that declares no `network_policy` (e.g. the standard `implementation` flow) runs with network enabled; sibling nodes without the field stay offline.
- A node with `network_access: false` in a flow that declares `network_policy` runs offline (explicit opt-out honored).
- A flow with no `network_access` on any node behaves exactly as today (the two packaged research flows still grant network to their nodes with no edit).
- An unknown node field is still rejected fail-closed.
