#!/usr/bin/env python3
"""Co-design validator (Step 0).

Two layers, mirroring the eventual P0 design:
  1. structural — JSON Schema (`flow.schema.json`): node kinds, required fields,
     enums, fail-closed unknown fields.
  2. graph-semantic — checks JSON Schema cannot express and that the P0.3 fatal
     validator will own: edges resolve, outcome subset of node-kind set,
     bounded loops, single entry + reachability, terminal exists,
     lineage_affinity target, decomposition references.

Run:  .venv/bin/python docs/backlog/flows/co-design/validate.py
Exits non-zero on any failure.
"""

from __future__ import annotations

import json
import pathlib
import sys

import yaml
from jsonschema import Draft202012Validator

HERE = pathlib.Path(__file__).parent
SCHEMA = json.loads((HERE / "flow.schema.json").read_text())
FILES = ["implementation.yaml", "deep_research.yaml", "security_audit.yaml"]


def graph_checks(doc: dict) -> list[str]:
    errs: list[str] = []
    flow = doc["flow"]
    nodes = flow["nodes"]
    edges = flow.get("edges", [])
    budgets = flow.get("budgets", {})

    by_id: dict[str, dict] = {}
    for n in nodes:
        if n["id"] in by_id:
            errs.append(f"duplicate node id: {n['id']}")
        by_id[n["id"]] = n

    for e in edges:
        if e["from"] not in by_id:
            errs.append(f"edge.from unknown node: {e['from']}")
        if e["to"] not in by_id:
            errs.append(f"edge.to unknown node: {e['to']}")

    # outcome subset of node-kind allowed set ("выбор ⊆ объявленного набора")
    out: dict[str, list[dict]] = {}
    for e in edges:
        out.setdefault(e["from"], []).append(e)
    for nid, es in out.items():
        n = by_id.get(nid)
        if not n:
            continue
        kind = n["kind"]
        if kind == "evaluator":
            allow = {None} if n.get("evaluation_kind") == "final_handoff" else {"accept", "rework"}
        elif kind == "checks":
            allow = {"pass", "fail"}
        else:  # agent / hitl / publish proceed unconditionally
            allow = {None}
        for e in es:
            oc = e.get("outcome")
            if oc and oc.startswith("route:"):
                continue
            if oc not in allow:
                shown = sorted("∅" if a is None else a for a in allow)
                errs.append(f"node {nid} ({kind}): outgoing outcome {oc!r} not in allowed {shown}")

    # bounded loops: rework/fail edges must carry budget or loop
    for e in edges:
        if e.get("outcome") in ("rework", "fail") and "budget" not in e and "loop" not in e:
            errs.append(f"edge {e['from']}->{e['to']} ({e['outcome']}) unbounded: needs budget or loop")

    # named loops must be declared in budgets
    for e in edges:
        lp = e.get("loop")
        if lp and lp not in budgets:
            errs.append(f"edge loop {lp!r} not declared in budgets")

    # exactly one entry node (no incoming) + reachability
    incoming = {n["id"]: 0 for n in nodes}
    for e in edges:
        if e["to"] in incoming:
            incoming[e["to"]] += 1
    entries = [i for i, c in incoming.items() if c == 0]
    if len(entries) != 1:
        errs.append(f"expected exactly one entry node, got {sorted(entries)}")
    else:
        adj: dict[str, list[str]] = {}
        for e in edges:
            adj.setdefault(e["from"], []).append(e["to"])
        seen: set[str] = set()
        stack = [entries[0]]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            stack.extend(adj.get(x, []))
        unreached = set(by_id) - seen
        if unreached:
            errs.append(f"unreachable nodes: {sorted(unreached)}")

    # at least one terminal node (path can end)
    has_out = {e["from"] for e in edges}
    if not (set(by_id) - has_out):
        errs.append("no terminal node (every node has an outgoing edge)")

    # lineage_affinity must target an agent with editing_lineage
    for n in nodes:
        la = n.get("lineage_affinity")
        if la:
            t = by_id.get(la)
            if not t or t.get("kind") != "agent" or t.get("session_scope") != "editing_lineage":
                errs.append(f"node {n['id']}: lineage_affinity -> {la!r} must be an agent with editing_lineage")

    # decomposition references
    dec = flow.get("decomposition")
    if dec:
        if dec.get("proposed_by") not in by_id:
            errs.append(f"decomposition.proposed_by unknown node: {dec.get('proposed_by')}")
        for sid in dec.get("sub_flow", []):
            if sid not in by_id:
                errs.append(f"decomposition.sub_flow unknown node: {sid}")
        sb = dec.get("shared_budget")
        if sb and sb not in budgets:
            errs.append(f"decomposition.shared_budget {sb!r} not in budgets")

    return errs


def main() -> None:
    validator = Draft202012Validator(SCHEMA)
    ok = True

    for fname in FILES:
        doc = yaml.safe_load((HERE / fname).read_text())
        serrs = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
        gerrs = graph_checks(doc)
        if serrs or gerrs:
            ok = False
            print(f"✗ {fname}: FAIL")
            for e in serrs:
                print(f"    [schema] {list(e.path)}: {e.message}")
            for m in gerrs:
                print(f"    [graph]  {m}")
        else:
            print(f"✓ {fname}: PASS (structural + graph)")

    # generality gate: the schema must not hard-code any flow/stage/role name
    leaked = [
        w
        for w in ("implementation", "deep_research", "security_audit", "refinement", "supervisor", "planning")
        if w in json.dumps(SCHEMA)
    ]
    if leaked:
        ok = False
        print(f"✗ schema leaks flow-specific tokens (domain knowledge in engine): {leaked}")
    else:
        print("✓ schema is generic (no flow/stage/role names hard-coded)")

    print("\nRESULT:", "ALL PASS" if ok else "FAILURES")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
