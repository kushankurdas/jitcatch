# Use case 07. Local, private models via Ollama

**Provider:** `--provider ollama`
**Default endpoint:** `http://localhost:11434/v1` (overridable via `OLLAMA_BASE_URL`)
**Default model:** `qwen2.5-coder:7b`

---

## When to reach for this

You want JitCatch to work **without sending your code to a third party**. Ollama runs models locally on your machine; JitCatch talks to it directly. This is the zero-config default when `ANTHROPIC_API_KEY` is not set, and the right choice for privacy-sensitive codebases, offline workflows, and spend-capped experimentation.

Reach for this use case when:

- Your codebase cannot leave the local machine.
- You want to iterate on prompts and workflows without paying per-token.
- You are evaluating JitCatch and want a free, fast feedback loop.
- You are running JitCatch in CI on a self-hosted runner that already has Ollama warm.

Do **not** reach for this use case when:

- You need the strongest reasoning available for risk inference or LLM-as-judge. Cloud frontier models still edge out most 7B–14B local models. Use [08-anthropic-claude.md](./08-anthropic-claude.md) or [09-openai-compatible-providers.md](./09-openai-compatible-providers.md) for those stages, optionally with a local model for bulk test generation.

---

## Prerequisites

1. [Install Ollama](https://ollama.com/download) and start the daemon.
2. Pull a code-oriented model:
   ```bash
   ollama pull qwen2.5-coder:7b
   ```
   Recommended alternates:
   - `qwen2.5-coder:14b`. Stronger reasoning, slower.
   - `deepseek-coder-v2:16b`. Strong on JSON-schema prompts (JitCatch uses the native `/api/chat` endpoint so `format: "json"` is honored; see below).
   - `llama3.1:8b-instruct-q8_0`. Solid generalist baseline.

---

## Command

```bash
# Zero config. Picks up localhost:11434, default model qwen2.5-coder:7b.
jitcatch pr . --provider ollama

# Explicit model.
jitcatch pr . --provider ollama --model qwen2.5-coder:14b

# Custom host (remote Ollama box on your network).
OLLAMA_BASE_URL=http://ollama.lan:11434 jitcatch pr . --provider ollama

# Longer HTTP read timeout for big models on slow hardware.
jitcatch pr . --provider ollama --model qwen2.5-coder:14b --llm-timeout 300
```

---

## What happens under the hood

- JitCatch routes Ollama through its **native `/api/chat`** endpoint (not the OpenAI-compatible `/v1` shim).
- This is deliberate: the native endpoint honors `format: "json"` and `num_ctx`; the `/v1` shim **silently drops both**. Without them, many local models (notably DeepSeek-Coder-V2) return prose instead of the strict JSON that JitCatch's parsers expect.
- HTTP read timeout defaults to 120 s and is raised via `--llm-timeout`. Local 14B+ models on CPU can exceed 60 s per call.
- Every stage (`risks`, `tests`, `judge`, `review`) goes through the same `OllamaClient`. Override per-stage models with `--model-risks / --model-tests / --model-judge / --model-review`.

---

## Reading the output

Shape is identical across providers. What changes with a local model is **signal quality**:

- **Risk lists** from smaller models tend to be less specific. Expect more generic "null check missing" entries and fewer with precise `[file:line]` citations.
- **Generated tests** are often fine. Test code is a bulk-output task, which smaller models handle well.
- **Judge rationales** are noisier. Do not over-index on `tp_prob` from a 7B judge; lean on `rule_flags` and your own reading.

A useful recipe: use a 14B local model for `--model-risks` and `--model-judge`, and the default 7B for `--model-tests`.

---

## Tips

- **Pull the model before JitCatch runs.** If Ollama has to pull mid-run, the first LLM call will time out. `ollama pull <model>` ahead of time.
- **Check `ollama ps` if calls hang.** The daemon may be loading the model into VRAM. Common on first call after boot.
- **If you see prose where JSON should be**, you are almost certainly not using JitCatch's native Ollama path. Check that `--provider ollama` is set (not `openai-compat --base-url http://localhost:11434/v1`).
- **`num_ctx` matters on large bundles.** Some models ship with a small default context window. Use a pulled variant with a larger context, or lower `--max-bytes`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Connection refused` on `127.0.0.1:11434` | Ollama daemon is not running. | `ollama serve` (or relaunch the app). |
| Model returns prose instead of JSON | You're on the `/v1` shim, not the native path. | Use `--provider ollama`, **not** `--provider openai-compat --base-url http://localhost:11434/v1`. |
| `httpx.ReadTimeout` after 120 s | Big model on slow hardware. | `--llm-timeout 300` or more; consider a smaller quant. |
| `truncated (max_tokens): N > 0` | Local model hit its output ceiling. | Raise `--max-tokens`, lower `--max-bytes`, or switch `--model-tests` to a bigger quant. |
