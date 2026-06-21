Write the research deliverable. Produce exactly two files, and write nothing outside this directory:

- `{repo}/docs/research/{task_id}/report.md` — the answer in prose: findings, the recommended approach, trade-offs, and an **Open questions** section for anything left unresolved or unverified.
- `{repo}/docs/research/{task_id}/sources.json` — the citation manifest: `{"sources": [ ... ]}`, one entry per claim that cites the repository. Each entry is `{"id": "...", "claim": "...", "path": "<repo-relative file>", "line": <1-based int, optional>, "snippet": "<exact text expected at that location, optional>"}`. For an external reference use `{"id": "...", "claim": "...", "url": "..."}` instead of `path`.

Every repository citation must point at a real file/line whose snippet is actually present — a citation to something that does not exist will be rejected. If a verification round flags a citation as broken, either correct it to a real location or drop the claim and record it under **Open questions** as unverified; do not invent sources. Do not modify any source file. Return the typed structured result required by the output schema.
