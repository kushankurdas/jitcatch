# Use case 08. Cloud inference via Anthropic Claude

**Provider:** `--provider anthropic`
**Auth:** `ANTHROPIC_API_KEY` environment variable.
**Default model:** `claude-sonnet-4-6`.

---

## When to reach for this

You want the strongest out-of-the-box signal JitCatch can produce. Claude is the default cloud provider and the only one with its own dedicated client in the JitCatch codebase. When `ANTHROPIC_API_KEY` is set, `--provider auto` resolves to Anthropic automatically. No additional flags needed.

Reach for this use case when:

- You are doing a serious PR review and want the best available risk list + judge rationale.
- Your organization already has an Anthropic contract and observability for it.
- You are running JitCatch in CI where latency matters less than signal quality.

Do **not** reach for this use case when:

- Your codebase cannot leave your machine. Use [07-local-ollama.md](./07-local-ollama.md) instead.
- You want to split spend across multiple providers. Use [09-openai-compatible-providers.md](./09-openai-compatible-providers.md).

---

## Prerequisites

1. An Anthropic API key. Get one at [console.anthropic.com](https://console.anthropic.com/).
2. Export it:
   ```bash
   export ANTHROPIC_API_KEY=sk-ant-...
   ```
3. The `anthropic` Python package, installed automatically by `pip install -e .[dev]`.

---

## Command

```bash
# Auto-resolved when ANTHROPIC_API_KEY is set.
jitcatch pr .

# Explicit.
jitcatch pr . --provider anthropic

# Pin a specific model.
jitcatch pr . --provider anthropic --model claude-sonnet-4-6

# Mix: premium reasoning for risks + judge, cheaper for bulk test-gen.
jitcatch pr . \
  --provider anthropic \
  --model-risks claude-sonnet-4-6 \
  --model-tests claude-haiku-4-5-20251001 \
  --model-judge claude-sonnet-4-6 \
  --model-review claude-sonnet-4-6
```

---

## What happens under the hood

- `_make_llm` constructs an `AnthropicClient` pinned to the configured model and per-stage overrides.
- `--max-tokens` defaults to the model's output ceiling (see the top-level README for current ceilings). Lower it to cap spend at the risk of `truncated (max_tokens)` in stderr.
- All four stages (`risks`, `tests`, `judge`, `review`) route through the same client with per-stage model selection.
- Per-call transcripts are written to `.jitcatch/logs/` under `--verbose` or `--log-dir`. These are untruncated. Convenient for debugging, spendy to keep around.

---

## Reading the output

Same artifacts as other providers. What changes with Claude:

- **Risk lists** tend to be precise and cite `[file:line]` accurately.
- **Generated tests** are high-quality, which increases the odds that a weak catch reflects a real regression.
- **Judge rationales** are specific enough to paste into a PR comment.

Expect to triage fewer false positives with Claude than with a small local model, but watch the `rule_flags` anyway. Deterministic signals catch cases LLMs rationalize away.

---

## Tips

- **Cost lever #1 is `--max-files`.** A 20-file bundle with a 200 KB cap is a substantial prompt. Lower `--max-files` for quick iteration.
- **Cost lever #2 is per-stage models.** See [10-per-stage-model-routing.md](./10-per-stage-model-routing.md). Test-gen is almost always the volume stage; Haiku there saves real money.
- **Cost lever #3 is retries.** `--max-retries 0` skips all retry rounds; `--no-retry` does the same. Retries are the quietest source of extra spend.
- **`--verbose` is valuable on PR runs.** A full transcript of every stage makes it trivial to explain a weak catch to a reviewer.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `AuthenticationError` or 401 | `ANTHROPIC_API_KEY` missing, expired, or wrong key. | Regenerate the key; re-export; verify with `echo ${ANTHROPIC_API_KEY:0:8}`. |
| Rate-limit errors (429) | Parallel runs exceeding your org's throughput. | Reduce concurrency, retry with backoff, or request a quota bump. |
| `truncated (max_tokens): N > 0` | Prompt + output exceeded the model's cap. | Lower `--max-bytes`, raise `--max-tokens` up to the ceiling, or switch the affected stage to a higher-ceiling model. |
| Judge `bucket` marks everything "uncertain" | Prompt was bulky and the judge thinned its rationale. | Reduce `--max-files` or narrow the run (`last`/`staged` instead of `pr`). |
