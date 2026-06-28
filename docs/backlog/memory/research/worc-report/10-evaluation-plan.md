# 10. Evaluation plan

[Previous](./09-evolution-path.md) | [Next](./11-final-recommendation-with-trade-offs-and-rejected-alternatives.md)

### 10.1 Core metrics

| Category | Metrics |
| --- | --- |
| Quality | task success rate, first-pass test pass rate, review pass rate, fix-loop count, manual-action rate |
| Efficiency | tokens per successful task, wall-clock per task, number of retrieval reads, cleanup time |
| Retrieval quality | memory brief precision, memory brief recall on known-relevant facts, unused-brief rate |
| Memory quality | promotion precision, duplicate rate, stale fact rate, contradiction rate, quarantine rate |
| Safety | secret leak count, poisoned-write acceptance rate, rollback frequency, external-only promotion count |
| Maintainability | file/db growth, cleanup churn, operator edits needed, audit restore success |

### 10.2 Recommended experiments

1. **Replay benchmark**
   Run historical tasks with:
   - no memory
   - long-term only
   - long-term + episodic
   - long-term + episodic + entity memory

2. **Read-path ablation**
   Compare:
   - no memory
   - raw memory root path
   - stage brief path

3. **Write-path ablation**
   Compare:
   - finalize-only writes
   - step-level writes
   - finalize-only + cleanup compaction

4. **Retrieval ablation**
   Compare:
   - lexical/entity retrieval
   - lexical/entity + FTS
   - lexical/entity + embeddings

5. **Safety eval**
   Inject:
   - secret-like strings in artifacts
   - malicious external hints
   - stale file references
   - contradictory memory entries

6. **Long-horizon eval**
   Use sequential task batches over same repo to measure whether memory helps over time instead of only one task.

STATE-Bench is especially useful conceptually because it breaks agent memory into fundamental operations like update, locate, preserve and use across stateful workflows. [STATE-Bench](https://opensource.microsoft.com/blog/2026/05/19/introducing-state-bench-a-benchmark-for-ai-agent-memory/)

### 10.3 Success criteria

Разумные initial success criteria for WORC:

- `>= 10%` reduction in tokens or wall-clock for repeated-repo tasks;
- `>= 10%` improvement in first-pass review/test success on repeated hotspots;
- stale contradiction rate `< 5%`;
- secret leak rate `0`;
- external-only long-term promotions `0`;
- cleanup overhead small enough to stay outside critical path.

### 10.4 Failure indicators

Красные флаги:

- memory brief often ignored or irrelevant;
- memory grows faster than cleanup can control;
- many promotions later rolled back;
- agents start following stale rules more often than without memory;
- operator trust drops because memory becomes opaque or noisy;
- vector/graph infra added without measurable recall or quality lift.

