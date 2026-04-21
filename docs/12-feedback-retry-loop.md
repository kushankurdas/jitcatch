# Use case 12 — Feedback-driven retry for uncaught risks

**Flags:** `--no-retry` (disable), `--max-retries <n>` (default 2), `--max-retry-risks <n>` (default 8).

---

## When to reach for this

The first round of test-gen does not always cover every risk the LLM enumerated. A test might not compile, might pass on both revs (not a weak catch), or might assert the wrong thing. The retry loop closes that gap: for every risk with **no weak catch**, JitCatch generates a new test, with the prior test's failure output included as feedback, and evaluates it.

Reach for this use case when:

- You want the highest-signal report JitCatch can produce on a diff.
- You are running in CI where total wall-clock time matters less than signal completeness.
- You noticed in an earlier run that risks were enumerated but never covered by a weak catch.

Reach past this use case (with `--no-retry`) when:

- You want the fastest possible run.
- You are iterating locally and will rerun anyway.

---

## Command

```bash
# Default — up to 2 retry rounds, 8 risks per round.
jitcatch pr .

# Disable the loop entirely.
jitcatch pr . --no-retry

# Equivalent to --no-retry.
jitcatch pr . --max-retries 0

# Be generous — 4 rounds, 12 risks each. Expensive but thorough.
jitcatch pr . --max-retries 4 --max-retry-risks 12

# Shave cost — one round, cap risks tightly.
jitcatch pr . --max-retries 1 --max-retry-risks 4
```

---

## What happens under the hood

The loop runs inside `_evaluate_and_report`, after the first-round evaluations finish:

1. For each adapter group:
   - `find_gaps(risks_all, group_cands)` returns risks with no weak catch.
   - If there are none, the group is skipped.
2. `run_retry_round(llm, bundle, lang, hints, gaps, max_gaps)` sends a follow-up prompt. The prompt includes:
   - the original bundle,
   - the uncaught risks,
   - the prior test's failure output as feedback (so the LLM can see *why* its first attempt didn't catch the risk).
3. Each generated test is evaluated in the parent and child worktrees exactly like first-round tests.
4. The candidate's `workflow` field is set to `retry_r<N>` (for example `retry_r1`, `retry_r2`) so you can trace which retry round produced it.
5. The loop terminates when `--max-retries` is exhausted **or** a round adds zero new candidates.

---

## Reading the output

In the JSON and Markdown reports, retry candidates look like normal `CatchCandidate` entries but with a telltale `workflow`:

- `intent_aware` — first-round risks-first test.
- `dodgy_diff` — first-round mutation-mindset test.
- `retry_r1`, `retry_r2`, ... — retry-round candidates.

The `risks` field on a retry candidate contains exactly the single uncaught risk the retry targeted. This makes it easy to follow the chain "risk → first test → failure → retry test → weak catch".

---

## Tips

- **The loop auto-terminates early.** If a round produces no new weak catches, JitCatch breaks out of the retry loop even if rounds are left. Setting `--max-retries 10` is safe — you will not pay for idle rounds.
- **`--max-retry-risks` is the real cost cap.** Prompts scale with the number of risks per round. Keep this at 8 or lower unless you know you want the spend.
- **Combine with `--verbose`.** The retry prompt contains the failure output of the prior test, which is a goldmine for debugging test-gen. `.jitcatch/logs/` has the full transcript.
- **Retry complements the reviewer, it does not replace it.** Some risks cannot produce a runnable test no matter how many retries you run — those are exactly what the agentic reviewer is for (see [11-agentic-reviewer.md](./11-agentic-reviewer.md)).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Retry rounds keep producing tests that pass on both revs | Risks were over-broad; LLM keeps generating assertions the diff doesn't break. | Strengthen `--model-risks`; or accept that some enumerated risks are not actual behavior changes. |
| Very slow runs on large PRs | Retry budget multiplied by risk count. | Lower `--max-retries` and `--max-retry-risks`; or use `--no-retry` for first-pass triage. |
| `retry error (group N, round M): ...` in stderr | LLM call in the retry stage failed. | Raise `--llm-timeout`; check provider-side rate limits. |
| Retry candidates flagged as `fp:*` by the rule assessor | The LLM "caught" the risk but the test is pathological (reflection, time-sensitivity). | Keep `--no-retry` off but trust the rule flags in the report; don't act on `fp:*` entries. |
