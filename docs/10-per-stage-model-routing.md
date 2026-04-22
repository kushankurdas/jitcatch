# Use case 10. Per-stage model routing (cost vs. quality)

**Flags:** `--model`, `--model-risks`, `--model-tests`, `--model-judge`, `--model-review`.

---

## When to reach for this

JitCatch has four LLM stages and each has a different cost/quality profile. Sending every stage to the most capable (most expensive) model is wasteful; sending every stage to the cheapest model drops signal. Per-stage routing lets you pay premium prices only where premium reasoning actually pays off.

Reach for this use case when:

- You want to lower your Anthropic bill without losing weak-catch quality.
- You have a multi-provider contract (Groq + Together + Anthropic, say) and want to route stages by strength.
- You mix a local Ollama model with a cloud model for the reasoning-heavy stages.

Skip this use case for quick, ad-hoc runs. The defaults are fine.

---

## The four stages

| Stage | Flag | What it produces | Cost profile | Quality profile |
|---|---|---|---|---|
| **risks** | `--model-risks` | Ranked list of risks the diff introduces, with `[file:line]` citations | Low volume, 1 call per group | Reasoning-heavy; benefits most from a strong model |
| **tests** | `--model-tests` | Test code for each risk / mutation target | High volume, one call per test | Bulk output; a mid-tier model is usually sufficient |
| **judge** | `--model-judge` | `tp_prob`, `bucket`, rationale per weak catch | Low volume, 1 call per weak catch | Reasoning-heavy; noise here inflates false positives |
| **review** | `--model-review` | Diff-level bug reports (see [11-agentic-reviewer.md](./11-agentic-reviewer.md)) | Low volume, 1 call per group + validator | Reasoning-heavy; surfaces bugs test-gen can't reach |

When a stage flag is omitted, it falls back to `--model`, which in turn falls back to the provider default (`claude-sonnet-4-6` on Anthropic, `qwen2.5-coder:7b` otherwise).

---

## Recipes

### Cheap bulk, premium reasoning (Anthropic)

The single biggest cost saver on a cloud-provider run:

```bash
jitcatch pr . \
  --provider anthropic \
  --model-risks  claude-sonnet-4-6 \
  --model-tests  claude-haiku-4-5-20251001 \
  --model-judge  claude-sonnet-4-6 \
  --model-review claude-sonnet-4-6
```

### Mix local Ollama + cloud Anthropic

Keep your source off the network for the bulk test-gen stage, but let a cloud frontier model do the reasoning. Because JitCatch binds one client per run, this requires two invocations and a manual merge. Useful for forensic deep-dives, not for routine PR runs.

### All-local, tiered by size

Inside a single Ollama install, route reasoning to a bigger quant and bulk to a smaller one:

```bash
jitcatch pr . \
  --provider ollama \
  --model-risks  qwen2.5-coder:14b \
  --model-tests  qwen2.5-coder:7b \
  --model-judge  qwen2.5-coder:14b \
  --model-review qwen2.5-coder:14b
```

### OpenAI-compat gateway with open-weights

Together / Groq / Fireworks model families:

```bash
jitcatch pr . \
  --provider openai-compat --base-url https://api.together.xyz/v1 \
  --model-risks  meta-llama/Meta-Llama-3.1-70B-Instruct \
  --model-tests  meta-llama/Meta-Llama-3.1-8B-Instruct \
  --model-judge  meta-llama/Meta-Llama-3.1-70B-Instruct \
  --model-review meta-llama/Meta-Llama-3.1-70B-Instruct
```

---

## What happens under the hood

- `_make_llm` resolves `--model` (or a provider default) first, then overlays each `--model-<stage>` on top:
  ```python
  model = args.model or default_model
  stage_models = {
      "risks":  args.model_risks  or model,
      "tests":  args.model_tests  or model,
      "judge":  args.model_judge  or model,
      "review": args.model_review or model,
  }
  ```
- The client's `chat_stage(stage, ...)` method picks the right per-stage model on each call.
- All stages share one transport (one base URL, one API key). To route across providers in a single run, you have to orchestrate two runs and merge reports externally.

---

## Tips

- **Measure before optimizing.** Run one full `--verbose` pass; look in `.jitcatch/logs/` at which stage ate the most tokens. Tune that stage first.
- **Test-gen is the volume stage 90% of the time.** Dropping `--model-tests` to a cheaper tier is almost always the right first move.
- **Don't starve the judge.** Judge calls are few but decisive. A bad judge turns every weak catch into noise. Keep `--model-judge` premium.
- **Beware chat-completion prompt-caching differences.** Anthropic's prompt caching is client-aware; open-weights endpoints typically do not cache. That changes the economics of re-running the same PR multiple times.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Stage flag silently ignored | Typo in flag name. | Exact names are `--model-risks / --model-tests / --model-judge / --model-review`. |
| Judge rationales became useless after tuning | `--model-judge` got downgraded too aggressively. | Promote judge back to the premium tier; save costs on tests/review instead. |
| Some stages go to a cheap model, report still expensive | Retry rounds reran the test stage at the default model. | Apply `--model-tests` explicitly; `--max-retries 0` if you want to kill retry spend. |
