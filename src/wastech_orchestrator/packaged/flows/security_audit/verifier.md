Verify the proposed threats against the code at `{repo}` before the report is written. Judge whether each threat is **real and reachable** as the product actually ships, or a false positive.

Verify **both** sides against `{repo}`:

- The **code** side: open the cited `path:line` and confirm the sink is really there and reachable along the path the threat describes — not already guarded by a validation the threat overlooked.
- The **model** side: confirm the threat respects this project's declared trust boundary and posture (from the scope step). A claim that an out-of-scope surface **exists** is a real finding only if that code is actually present; a claim that such a thing merely *ought* to be hardened when it does not exist is a false positive.

Cross-check any dependency-related threat against the dependency-scan advisories: confirm the vulnerable code path is one this product actually calls before treating the advisory as a real finding.

Watch for: severity inflated beyond a reachable, attacker-controlled trigger; a "vulnerability" that is theoretical with no input an attacker controls; a dependency advisory whose vulnerable code path this product never calls; and a suspected issue presented as confirmed.

Read only; do not edit. This is a **non-blocking** pass and a **fail-closed evaluator**: you must return the findings result required by the output schema — a prose-only "looks fine" does not satisfy the contract and hard-stops the task. Record a finding of severity **medium or high** for any threat that is unconfirmed, misclassified, or a false positive; that routes the batch back to threat_analysis for bounded rework. When your remaining concerns are exhausted or the rework budget is spent, accept so the report can be written, and record any residual doubt as a finding rather than blocking. You may use the granted network access only to confirm an upstream advisory detail.
