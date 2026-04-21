# Use case 11 — Agentic reviewer for bugs tests can't reach

**Flags:** `--no-review` (disable), `--skip-validator` (keep every finding), `--model-review <name>`.

---

## When to reach for this

Some bugs can't be caught by a generated test. A mock swallows the exception path. An environment variable stubs out the broken branch. The buggy function is never called in any test file. Test-gen fundamentally can't exercise these regressions — the signal lives in the code, not in runtime behavior.

JitCatch's **agentic reviewer** reads the diff bundle and flags suspected bugs via LLM reasoning. A second LLM pass — the **validator** — drops obvious false positives or downgrades their confidence. Findings are written to a separate section of the report so they never outrank test-backed weak catches.

Reach for this use case when:

- You want a BugBot-style diff review alongside the executable regression check.
- Your codebase is heavily mocked / env-coupled, where test-gen has low yield.
- You are doing a PR review and want opinion-based signal in addition to weak catches.

---

## Command

The reviewer runs by default on every subcommand. You only touch these flags to turn it off, tune it, or keep raw output:

```bash
# Default — reviewer on, validator on.
jitcatch pr .

# Disable the reviewer entirely (faster, cheaper).
jitcatch pr . --no-review

# Keep every reviewer finding, even ones the validator would drop.
jitcatch pr . --skip-validator

# Use a different model for the reviewer stage only.
jitcatch pr . --model-review claude-sonnet-4-6
```

---

## What happens under the hood

1. After the bundle is built (per adapter group), `run_agentic_reviewer(llm, bundle, lang, skip_validator)` is called **before** test eval starts. This means a run with zero generated tests still gets reviewer coverage.
2. The reviewer emits findings with the shape:
   ```python
   ReviewFinding(
       file, line, title, rationale,
       severity=Critical|High|Medium|Low,
       category=security|concurrency|validation|arithmetic|contract,
       confidence=0..1,
   )
   ```
3. Unless `--skip-validator` is set, a second LLM pass classifies each finding as `keep | drop | downgrade` and writes `validator_verdict` + `validator_note`.
4. Only `keep` and `downgrade` findings land in the report. `drop` findings are discarded silently.
5. Reviewer output is written to a dedicated Markdown section — **below** the test-backed findings and **never ranked against them**.

---

## Reading the output

Markdown structure, in the order you read it:

1. **Test-backed findings (weak catches)** — parent-passes-child-fails evidence.
2. **Reviewer-only findings** — validator-filtered. Each entry includes: file + line, severity, category, rationale, confidence, validator note.
3. **Likely false positives** — collapsed at the bottom.

Cross-reference the two sections:

- A reviewer finding *with* a corresponding weak catch is a high-confidence bug.
- A reviewer finding *without* a corresponding weak catch is the whole reason the reviewer exists: a bug test-gen could not demonstrate.
- A weak catch *without* a corresponding reviewer finding is common — the reviewer tends to flag structural issues; test-gen reaches behavior shifts.

---

## Tips

- **Keep the reviewer on for PR runs.** The marginal cost is low (one or two LLM calls per adapter group) and the marginal signal is high.
- **Use `--skip-validator` only when debugging the reviewer itself.** Without the validator, expect a noticeable FP rate on diffs that look "risky" stylistically but are fine semantically.
- **`--model-review` defaults to `--model`.** If you are on Anthropic with default routing, that is Sonnet, which is the right tier for this stage. Do not downgrade `--model-review` to Haiku — validator quality drops fast.
- **Disable when the diff is trivial.** A one-line typo fix does not need a reviewer pass; `--no-review` saves a few seconds.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Reviewer returns zero findings but the diff obviously has issues | Model too small for diff-reasoning. | Use `--model-review` with a stronger model; confirm on a known-bad diff. |
| Reviewer returns too many findings | Validator disabled (`--skip-validator`) or validator model underpowered. | Re-enable validator; keep `--model-review` premium. |
| Reviewer output references files that do not exist in the diff | LLM hallucinated a file name. | Lower `--max-bytes` so the bundle fits in context with headroom; validator should catch these but doesn't always. |
| `reviewer error (group N): ...` in stderr | LLM call failed (timeout, malformed JSON). | Raise `--llm-timeout`; rerun with `--verbose` and check `.jitcatch/logs/`. |
