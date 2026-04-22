# Use case 09. OpenAI-compatible endpoints (Groq, Together, OpenRouter, LM Studio, vLLM, LocalAI, …)

**Provider:** `--provider openai-compat`
**Auth:** `OPENAI_API_KEY` if the endpoint requires it.
**Base URL:** required. Pass `--base-url <url>`.
**Default model:** `qwen2.5-coder:7b` (override with `--model`).

---

## When to reach for this

You want a third option between **"local Ollama"** and **"cloud Anthropic"**. Any chat-completions endpoint that speaks the OpenAI schema works. Hosted gateways like Groq, Together, Fireworks, OpenRouter, and self-hosted servers like LM Studio, vLLM, and LocalAI. This is the provider flexibility lever: swap model and vendor without changing any JitCatch internals.

Reach for this use case when:

- You already pay for Groq/Together/Fireworks/OpenRouter and want JitCatch to use that contract.
- You are self-hosting a model on vLLM or LM Studio and want JitCatch to hit it.
- You need a specific open-weights model (Llama 3.1 70B, DeepSeek-V3, Mixtral) that's available through a gateway.
- You want to A/B providers without code changes.

Do **not** reach for this use case when:

- You are on localhost Ollama. Use `--provider ollama` instead (see [07-local-ollama.md](./07-local-ollama.md)). JitCatch's native Ollama path honors `format: "json"` and `num_ctx`; the generic `/v1` shim does not.
- You have an Anthropic key. Use `--provider anthropic` (see [08-anthropic-claude.md](./08-anthropic-claude.md)) for first-class client behavior.

---

## Prerequisites

- The endpoint's base URL (e.g. `https://api.together.xyz/v1`, `https://api.groq.com/openai/v1`, `https://openrouter.ai/api/v1`, `http://localhost:1234/v1` for LM Studio).
- An API key if the endpoint needs one (`export OPENAI_API_KEY=...`).
- A model identifier the endpoint recognizes.

---

## Command

```bash
# Groq. Llama-3.3-70b via Groq's /v1 endpoint.
export OPENAI_API_KEY=gsk_...
jitcatch pr . \
  --provider openai-compat \
  --base-url https://api.groq.com/openai/v1 \
  --model llama-3.3-70b-versatile

# Together. Llama 3.1 70B Instruct.
export OPENAI_API_KEY=...
jitcatch pr . \
  --provider openai-compat \
  --base-url https://api.together.xyz/v1 \
  --model meta-llama/Meta-Llama-3.1-70B-Instruct

# OpenRouter. Any model behind their router.
export OPENAI_API_KEY=sk-or-...
jitcatch pr . \
  --provider openai-compat \
  --base-url https://openrouter.ai/api/v1 \
  --model deepseek/deepseek-chat

# LM Studio. Local, no key needed.
jitcatch pr . \
  --provider openai-compat \
  --base-url http://localhost:1234/v1 \
  --model lmstudio-community/Qwen2.5-Coder-14B-Instruct-GGUF

# Self-hosted vLLM.
jitcatch pr . \
  --provider openai-compat \
  --base-url https://vllm.internal.example.com/v1 \
  --model meta-llama/Meta-Llama-3.1-70B-Instruct
```

---

## What happens under the hood

- `_make_llm` instantiates `OpenAICompatClient` with the base URL, optional `OPENAI_API_KEY`, and per-stage model overrides.
- Requests go to `<base-url>/chat/completions` with the standard OpenAI JSON schema.
- The client does **not** send Ollama-specific fields (`format`, `num_ctx`) because cloud-hosted OpenAI-compat endpoints typically reject unknown fields. That is the whole reason Ollama has its own path.
- `--llm-timeout` applies. Useful for self-hosted endpoints on slow GPUs.

---

## Reading the output

Same artifacts, same ranking. Signal quality is a function of the underlying model, not the endpoint:

- 70B-class open-weights models produce risk lists and judge rationales comparable to cloud frontier models on straightforward diffs.
- Smaller models (8B, 13B) behave like the local equivalents. Expect vaguer rationales and more noise in `rule_flags` vs `tp_prob`.

---

## Tips

- **Gateway pricing varies wildly.** Groq is cheap and fast on Llama-class models. OpenRouter aggregates providers and marks up. Read each provider's pricing page before routing a high-volume stage through it.
- **Mix providers per stage.** Nothing stops you from sending `--model-risks` to Groq and `--model-tests` to Together if both accept a compatible `OPENAI_API_KEY` flow. But `_make_llm` binds a single `base_url` per run. For true multi-provider routing, run the pipeline twice with different flags and merge reports externally, or use [08-anthropic-claude.md](./08-anthropic-claude.md) for the premium stages.
- **Self-hosted endpoints often ignore `max_tokens`.** If you see cut-off responses, confirm with the endpoint's server logs that the cap is honored.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `--base-url required for --provider=openai-compat` | You forgot `--base-url`. | Pass the endpoint URL (must end in `/v1` for most providers). |
| 401/403 from the endpoint | `OPENAI_API_KEY` missing or the provider expects a different env var. | Export the right key; some providers use a custom header - those are not supported by the generic client. |
| 404 on `/chat/completions` | Base URL is off by a path segment. | Most providers expect `.../v1`; double-check their docs. |
| Response is prose, not JSON | Endpoint does not honor the OpenAI JSON mode hints. | Switch to a model known to emit JSON reliably (Llama 3.1 Instruct, Qwen2.5-Coder), or use `--provider ollama` with a local model where JitCatch controls the JSON coercion. |
| `truncated (max_tokens)` | Output ceiling too low for the response. | Raise `--max-tokens`; lower `--max-bytes`. |
