Write the security-audit report to **exactly one file** and write nothing anywhere else:

`{repo}/.worc/security-reports/{task_id}/report.md`

## The Write Constraint (Do Not Violate)

Under this flow's `private_control_workspace_report` output policy, `{repo}/.worc/security-reports/{task_id}/` is the **only** writable directory, and `report.md` is the only file you create in it. Any write outside it fails validation — do **not** create a scratch, notes, or draft file anywhere else in the repository, and do not modify any source file. This is a private control-workspace report: it stays under the gitignored `.worc/` home and is **never committed**. Keep everything you need to say inside the single `report.md`.

## What To Write

Draw on the verified threats and the verifier's verdicts:{?threat_analysis_path} the proposed threats at {threat_analysis_path},{/threat_analysis_path}{?review_path} the verification findings at {review_path} (which threats were confirmed, misclassified, or dismissed),{/review_path} and any dependency-scan advisories. Report only what verification confirmed; carry nothing the verifier rejected into the confirmed set.

For each **verified** finding cover: the location as `path:line` (or the dependency + advisory id), the severity, whether it is **exploitable or theoretical** (with the concrete trigger for an exploitable one), the impact, and a concrete remediation that fits this project's own security posture and points at where the fix belongs. Order findings by severity. Keep a separate section for threats **dismissed as false positives**, each with the reason, so a reader sees what was considered and cleared. If nothing was confirmed, say so plainly — an empty confirmed set is a valid, honest result, not a gap to pad.

Keep the report deterministic and bounded to the analyzed repository state; do not dump secrets, environment variables, or unrelated local filesystem data into it. Return the typed structured result required by the output schema.
