from __future__ import annotations

import json
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .config import GeneratedTest, ReviewFinding


STRICT_JSON_SUFFIX = (
    "\n\nOutput MUST be the raw JSON object only. Do not wrap in code "
    "fences. Do not add commentary before or after. Start your response "
    "with the opening brace/bracket and end with the closing one."
)


RISK_TAXONOMY_CLAUSE = (
    "Examine the diff across these risk classes explicitly. Do not skip any:\n"
    "  - security:    auth/authz guards weakened or removed, CORS misconfig, "
    "secrets leakage, env-var defaults flipped (e.g. development -> production), "
    "loosened HTTP status checks (== -> >=, inverting ok/error), catch blocks that "
    "silently next()/return instead of rejecting.\n"
    "  - concurrency: race conditions, ordering of await / cleanup, shared mutable "
    "state across concurrent calls, resource-lifecycle violations (delete-before-end).\n"
    "  - validation:  relaxed limits (size/count/timeout numeric caps bumped), weaker "
    "regex, bypassed input checks, removed null guards, removed apiKey/tenantId checks.\n"
    "  - arithmetic:  off-by-one in <, <=, >=; sign / direction flips; operand swaps "
    "that invert overdue-vs-remaining / earlier-vs-later; constants bumped silently.\n"
    "  - contract:    changed return shape, HTTP method swaps (DELETE -> GET), "
    "enum value swaps (open/closed), identifier/casing swaps that desync call sites, "
    "trailing-token bugs (slice off-by-one in query builders).\n"
    "For each risk return an object: "
    '{"file": str, "line": int|null, "class": one of the classes above, "risk": str}. '
    "Aim for one entry per independent risk. Do not emit multiple entries for the "
    "same underlying issue across call sites."
)

RISKS_SYSTEM = (
    "You are a senior software engineer reviewing a code change. "
    "Given a diff, identify risks. Concrete ways the change could introduce a bug. "
    + RISK_TAXONOMY_CLAUSE
    + " Return a JSON array of such risk objects."
    + STRICT_JSON_SUFFIX
)

RISKS_SYSTEM_BUNDLE = (
    "You are a senior software engineer reviewing a code change that spans multiple files. "
    "Given per-file parent sources, diffs, and optional usage-context files, identify risks - "
    "concrete ways the change could introduce a bug, including cross-file inconsistencies. "
    + RISK_TAXONOMY_CLAUSE
    + " Set `file` to the repo-relative path where the risk lives. "
    "Return a JSON array of such risk objects."
    + STRICT_JSON_SUFFIX
)

REAL_SOURCE_CLAUSE = (
    " CRITICAL: Tests MUST exercise the real source by `require()`/`import` of the changed file "
    "at its repo-relative path (e.g. `require('./middleware/authentication')`, "
    "`from app.utils import index`). Do NOT reimplement, paraphrase, or stub the function under "
    "test inside the test body. A test that defines `parentBehavior()` and `changedBehavior()` "
    "as local functions and asserts on those passes identically on parent and child and is "
    "useless.\n\n"
    "You MAY and SHOULD mock TRANSITIVE dependencies when needed to drive the "
    "function under test into the failing branch. For example: HTTP clients "
    "(axios / request / node-fetch), JWT libraries (force `jwt.verify` to "
    "throw), DB drivers, SQS clients, filesystem operations. This is required "
    "for middleware, route handlers, and any function whose branch coverage "
    "depends on an external call's outcome. Mocking a dependency of the module "
    "under test is not the same as stubbing the module under test. The "
    "distinction is: the function you assert on must be the real one imported "
    "from the changed file.\n\n"
    "For express/connect middleware specifically: build a `req`, `res`, `next` "
    "trio inline (plain objects with `jest`-style spies or simple arrays of "
    "calls), invoke the exported middleware directly, and assert on what it "
    "did to `res` / whether it called `next`. For route handlers tied to an "
    "app instance, use `supertest` if available, otherwise invoke the handler "
    "directly with a stubbed `req` / `res`.\n\n"
    "If the module genuinely cannot be required (missing native dep, top-level "
    "side effect that throws, env coupling you cannot neutralize), say so in "
    "the rationale and skip that risk rather than emit a self-stubbed "
    "comparison that will pass on both revisions."
)

TESTS_SYSTEM_INTENT = (
    "You are a test-generation engine. Given source code at the PARENT revision and a diff, "
    "generate unit tests that (a) pass on the parent, and (b) exercise the risks listed. "
    "A test that passes on the parent but fails on the child indicates the diff introduced a "
    "regression. Return strict JSON: {\"tests\":[{\"name\":str,\"code\":str,\"rationale\":str}]}. "
    "Keep tests hermetic and self-contained."
    + REAL_SOURCE_CLAUSE
    + STRICT_JSON_SUFFIX
)

TESTS_SYSTEM_DODGY = (
    "You are a test-generation engine. Treat the provided diff as a suspected bug mutation of "
    "the parent. Generate unit tests against the PARENT code that assert its observable behavior "
    "precisely enough that the mutated version would fail. Return strict JSON: "
    "{\"tests\":[{\"name\":str,\"code\":str,\"rationale\":str}]}."
    + REAL_SOURCE_CLAUSE
    + STRICT_JSON_SUFFIX
)

TESTS_SYSTEM_BUNDLE_INTENT = (
    "You are a test-generation engine. You receive a bundle of changed files (with parent source "
    "and diffs) plus optional usage-context files, plus a list of RISKS each prefixed with "
    "`[file:line] (class)`. Generate unit tests that (a) pass on the parent, and (b) exercise "
    "the risks. A test may import/require any listed CHANGED file by its repo-relative path. "
    "Do not import usage-context files as the subject of assertions. They are there for "
    "comprehension only. For each risk in the input, emit at least one test whose "
    "`target_files` includes the risk's file. If a risk is genuinely untestable in isolation "
    "(e.g. CORS middleware), state the reason in that test's `rationale` and still emit the "
    "entry so the risk is not silently dropped. Return strict JSON: "
    "{\"tests\":[{\"name\":str,\"code\":str,\"rationale\":str,\"target_files\":[str, ...]}]}. "
    "Keep tests hermetic."
    + REAL_SOURCE_CLAUSE
    + STRICT_JSON_SUFFIX
)

TESTS_SYSTEM_BUNDLE_DODGY = (
    "You are a test-generation engine. Treat each diff in the bundle as a suspected bug mutation "
    "of its parent. Generate unit tests against the PARENT code that assert observable behavior "
    "precisely enough that the mutated versions would fail. Tests may import/require any CHANGED "
    "file by repo-relative path. Return strict JSON: "
    "{\"tests\":[{\"name\":str,\"code\":str,\"rationale\":str,\"target_files\":[str, ...]}]}."
    + REAL_SOURCE_CLAUSE
    + STRICT_JSON_SUFFIX
)

JUDGE_SYSTEM = (
    "You classify whether a failing test reveals a true bug in a code diff. "
    "Return strict JSON: {\"tp_prob\":float in [-1,1], \"bucket\":\"High\"|\"Medium\"|\"Low\", "
    "\"rationale\":str}. tp_prob=1 means certainly a real bug; -1 means certainly a false positive.\n\n"
    "CRITICAL. Weak-catch semantics: a regression-detection test asserts the "
    "PARENT's observable behavior. It PASSES on parent and FAILS on child by "
    "design. That is how regressions are detected. Do NOT mark a test as FP "
    "merely because it \"asserts what the parent looked like\" or \"encodes the "
    "old behavior\". That is the intended pattern. Only mark FP when one of:\n"
    "  - the failure is a runtime/import/syntax error unrelated to the behavior\n"
    "    change (ModuleNotFoundError, NameError, ReferenceError);\n"
    "  - the test is non-deterministic (time, random, network, ordering);\n"
    "  - the test reimplements parent and child logic as local stubs and "
    "compares those stubs to themselves (useless tautology);\n"
    "  - the assertion targets something the diff did NOT change;\n"
    "  - the behavior change is intentional, documented, and clearly not a bug "
    "(e.g. an added feature, an API version bump).\n"
    "Source-grep tests (read a file, assert a token/operator is present) are "
    "brittle but still valid TP signal when the diff truly changed that token - "
    "bucket them Medium, do not reject."
    + STRICT_JSON_SUFFIX
)

RETRY_SUFFIX = (
    "\n\nRetry: previous response was not parseable. Return ONLY the raw "
    "JSON object / array. No prose. No code fences. No trailing commentary."
)


REVIEWER_SYSTEM = (
    "You are an aggressive PR reviewer. Your job is to find EVERY plausibly-"
    "buggy change in the diff. Even ones without a failing test. Err on the "
    "side of flagging. Downstream validation will drop false positives.\n\n"
    "Examine every hunk across these classes. Do not skip any:\n"
    "  - security: auth/authz guards weakened or removed, CORS misconfig, "
    "secrets leaked, env-var defaults flipped (dev->prod), loosened HTTP "
    "status checks (==200 -> >=200), catch blocks that silently next()/return "
    "instead of rejecting, apiKey/tenantId guards removed.\n"
    "  - concurrency: race conditions, ordering of await/cleanup, resource-"
    "lifecycle violations (delete-before-close, end-after-unref).\n"
    "  - validation: relaxed numeric caps (size/count/timeout bumped), weaker "
    "regex (e.g. `1[0-2]` -> `1[0-3]` accepts month 13), bypassed input "
    "checks, removed null guards.\n"
    "  - arithmetic: off-by-one in <,<=,>=; sign / direction flips; operand "
    "swaps inverting overdue-vs-remaining; counting-loop bounds flipped "
    "(<3 -> <=3 double-counts boundary).\n"
    "  - contract: changed return shape, HTTP method swaps (DELETE -> GET), "
    "enum value swaps (open/closed), identifier/casing swaps desyncing call "
    "sites, trailing-token bugs (slice off-by-one in query builders).\n\n"
    "For EACH finding return an object:\n"
    '  {"file": str, "line": int|null, "title": str (<=80 chars), '
    '"rationale": str (WHY this is a bug, reference old vs new token),'
    ' "severity": "Critical"|"High"|"Medium"|"Low", '
    '"category": one of the classes above, "confidence": float 0..1}.\n'
    "Rules:\n"
    "  - One finding per independent mutation. Do not split one mutation "
    "across call sites.\n"
    "  - Title MUST name the symbol and the change ("
    "'isInvalidDate regex accepts month 13', not 'regex changed').\n"
    "  - Rationale must cite the specific old->new token (e.g. '1[0-2] -> 1[0-3]').\n"
    "  - Include findings even if you're unsure. Mark confidence <= 0.5.\n"
    "Return strict JSON: {\"findings\": [...]}."
    + STRICT_JSON_SUFFIX
)


VALIDATOR_SYSTEM = (
    "You are a strict PR-review validator. Given a batch of reviewer findings "
    "and the same diff, decide which to keep. Drop only if CLEARLY a false "
    "positive (finding references code not in the diff, misreads old vs new, "
    "describes a non-bug such as a formatting change, or claims a guard was "
    "removed when it wasn't). When uncertain, keep.\n\n"
    "For each input finding return an object with the SAME index ordering:\n"
    '  {"index": int (0-based), "verdict": "keep"|"drop"|"downgrade", '
    '"severity": "Critical"|"High"|"Medium"|"Low" (only if downgrade), '
    '"note": str (<= 160 chars)}.\n'
    "Return strict JSON: {\"validations\": [...]}."
    + STRICT_JSON_SUFFIX
)


TESTS_SYSTEM_RETRY = (
    "You are a test-generation engine given a SECOND chance. Prior tests for "
    "this risk either passed on BOTH parent and child (didn't detect the "
    "mutation) or failed on parent (test was buggy). You now receive:\n"
    "  - the original risk (with file:line and class)\n"
    "  - the FAILED-TO-CATCH test's name and code\n"
    "  - the failure mode (both_passed | both_failed | parent_failed)\n"
    "  - the parent source and diff.\n\n"
    "Write ONE NEW test that avoids the prior failure. Key tactics:\n"
    "  - both_passed: your prior test stubbed something or bypassed the "
    "changed branch. Call the actual changed function with an input at the "
    "mutation boundary (e.g. regex 1[0-2]->1[0-3]: pass month 13, not month "
    "5). Import the real module.\n"
    "  - parent_failed: test relied on an API/export the parent doesn't have "
    "- pick assertions on symbols the parent actually exposes.\n"
    "  - both_failed: the failure was an import or setup error. Fix "
    "scaffolding (mock transitive deps, not the module under test).\n"
    "Return strict JSON: "
    "{\"tests\":[{\"name\":str,\"code\":str,\"rationale\":str,\"target_files\":[str,...]}]}. "
    "Emit 1-2 tests max."
    + REAL_SOURCE_CLAUSE
    + STRICT_JSON_SUFFIX
)


# Compact system prompts used by small local models (gemma4:e*, qwen2.5-coder:7b,
# llama3.2:3b, phi4:*). The full prompts above exceed effective attention budgets
# of <=7B class models once bundled parent sources are appended, producing prose
# summaries instead of JSON. These preserve schema + a single JSON example and
# drop the exposition. Large/paid models always use the full prompts via
# _system_for_label default passthrough. See OpenAICompatClient override.
RISKS_SYSTEM_BUNDLE_COMPACT = (
    "Find bugs the diff could introduce. Output a JSON array. "
    "Each item: {\"file\": str, \"line\": int|null, \"class\": "
    "\"security\"|\"concurrency\"|\"validation\"|\"arithmetic\"|\"contract\", "
    "\"risk\": str}. Consider: auth/cors weakened, env defaults flipped, "
    "HTTP method swaps, off-by-one, regex bounds loosened, removed guards. "
    "One entry per independent bug.\n"
    "Example: [{\"file\":\"config/cors.js\",\"line\":11,\"class\":\"security\","
    "\"risk\":\"origin:'*' with credentials:true exposes cross-origin cookies\"}]\n"
    "Return ONLY the JSON array. No prose. No fences."
)

TESTS_SYSTEM_BUNDLE_INTENT_COMPACT = (
    "Generate unit tests that pass on PARENT code and fail on the diffed child. "
    "Tests MUST import the real changed module by repo-relative path. Do NOT "
    "re-implement the function under test inside the test. Mock transitive deps "
    "(axios, jwt, db) to drive failing branches. For each input RISK emit at "
    "least one test whose target_files includes the risk's file.\n"
    "Return strict JSON: {\"tests\":[{\"name\":str,\"code\":str,\"rationale\":"
    "str,\"target_files\":[str,...]}]}\n"
    "Example: {\"tests\":[{\"name\":\"rejects_origin_star_with_creds\","
    "\"code\":\"const cors=require('./config/cors');expect(cors.origin)"
    ".not.toBe('*');\",\"rationale\":\"cors.js flipped to wildcard\","
    "\"target_files\":[\"config/cors.js\"]}]}\n"
    "Return ONLY the JSON object. No prose. No fences."
)

TESTS_SYSTEM_BUNDLE_DODGY_COMPACT = (
    "Treat each diff as a suspected bug mutation. Generate unit tests against "
    "the PARENT code that assert observable behavior precisely enough that the "
    "mutated child fails. Import real modules; mock transitive deps only.\n"
    "Return strict JSON: {\"tests\":[{\"name\":str,\"code\":str,\"rationale\":"
    "str,\"target_files\":[str,...]}]}\n"
    "Example: {\"tests\":[{\"name\":\"delete_endpoint_uses_DELETE\",\"code\":"
    "\"const r=require('./routes');expect(r.stack.find(l=>l.route.path==="
    "'/x').route.methods.delete).toBe(true);\",\"rationale\":\"routes.js "
    "flipped DELETE -> GET\",\"target_files\":[\"routes/index.js\"]}]}\n"
    "Return ONLY the JSON object. No prose. No fences."
)

REVIEWER_SYSTEM_COMPACT = (
    "Aggressive PR reviewer. Flag every plausibly-buggy change. Check: security "
    "(auth/cors/env flips), concurrency (race/ordering), validation (relaxed "
    "limits/regex), arithmetic (off-by-one, sign), contract (method swap, enum "
    "swap, return shape).\n"
    "Return strict JSON: {\"findings\":[{\"file\":str,\"line\":int|null,"
    "\"title\":str,\"rationale\":str,\"severity\":\"Critical\"|\"High\"|"
    "\"Medium\"|\"Low\",\"category\":str,\"confidence\":float}]}\n"
    "Rationale must cite old->new token. Title names the symbol + change. "
    "Include low-confidence findings (confidence<=0.5).\n"
    "Example: {\"findings\":[{\"file\":\"routes/index.js\",\"line\":49,"
    "\"title\":\"delete-vulnerability route DELETE -> GET\",\"rationale\":"
    "\"method changed from DELETE to GET; GET of a mutating endpoint is "
    "CSRF-exploitable\",\"severity\":\"High\",\"category\":\"contract\","
    "\"confidence\":0.9}]}\n"
    "Return ONLY the JSON object. No prose. No fences."
)


# Models that benefit from COMPACT prompts. Matched by prefix against the model
# tag so `gemma4:e4b-instruct-q4_K_M` still resolves. Ordered by common Ollama
# naming. Large local models (gemma4:26b+, qwen2.5-coder:14b+) keep full prompts.
SMALL_MODEL_PREFIXES: Tuple[str, ...] = (
    "gemma4:e2b",
    "gemma4:e4b",
    "gemma3:1b",
    "gemma3:4b",
    "qwen2.5-coder:3b",
    "qwen2.5-coder:7b",
    "llama3.1:8b",
    "llama3.2:1b",
    "llama3.2:3b",
    "deepseek-coder-v2:16b",  # MoE with 2.4B active. Behaves like a small model
    "deepseek-r1:1.5b",
    "deepseek-r1:7b",
    "phi4:mini",
    "phi3:",
)


def _is_small_model(model: str) -> bool:
    tag = (model or "").strip().lower()
    return any(tag.startswith(p) for p in SMALL_MODEL_PREFIXES)


MODEL_MAX_OUTPUT_TOKENS = {
    "claude-opus-4-7": 32000,
    "claude-opus-4-6": 32000,
    "claude-sonnet-4-6": 64000,
    "claude-sonnet-4-5": 64000,
    "claude-haiku-4-5": 64000,
    # Common Ollama / local-served tags. Conservative output caps. 7B-class
    # models trip their context budget fast. Override with --max-tokens.
    "qwen2.5-coder:3b": 2048,
    "qwen2.5-coder:7b": 4096,
    "qwen2.5-coder:14b": 8192,
    "qwen2.5-coder:32b": 8192,
    "llama3.1:8b": 4096,
    "llama3.1:70b": 8192,
    "llama3.2:3b": 2048,
    "deepseek-coder-v2:16b": 8192,
    "deepseek-r1:7b": 4096,
    "deepseek-r1:14b": 8192,
    "gpt-oss:20b": 8192,
    # Gemma 4 (Google, 2026). E2B/E4B are edge-sized; 26B/31B target coding
    # assistants and IDE agent workflows.
    "gemma4:e2b": 2048,
    "gemma4:e4b": 4096,
    "gemma4:26b": 8192,
    "gemma4:31b": 8192,
}
DEFAULT_MODEL_MAX_OUTPUT_TOKENS = 32000
# Smaller fallback for OpenAI-compat endpoints (Ollama, LM Studio, vLLM, ...)
# where unknown model tags almost always point at local 7B-14B models with
# modest output budgets rather than frontier-sized context.
OPENAI_COMPAT_DEFAULT_CAP = 4096
OLLAMA_DEFAULT_BASE_URL = "http://localhost:11434/v1"
OLLAMA_DEFAULT_MODEL = "qwen2.5-coder:7b"
# Context window for Ollama-native transport. 4096 (Ollama default) truncates
# both bundle source and the compact prompt's JSON example, producing silent
# prose fallback on small models. 16K fits typical bundle + response budget.
OLLAMA_DEFAULT_NUM_CTX = 16384


def _clamp_max_tokens(model: str, requested: int) -> Tuple[int, bool]:
    ceiling = MODEL_MAX_OUTPUT_TOKENS.get(model, DEFAULT_MODEL_MAX_OUTPUT_TOKENS)
    if requested > ceiling:
        return ceiling, True
    return requested, False


@dataclass
class CallMeta:
    label: str
    stop_reason: str
    input_tokens: int
    output_tokens: int
    log_path: Optional[Path]


# USD per 1M tokens, (input, output). Missing models = free (local) or
# unknown (shown as $0.00 in the footer; raw token counts still reported).
MODEL_PRICING_USD_PER_M: dict = {
    "claude-opus-4-7": (15.0, 75.0),
    "claude-opus-4-6": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _stage_of(label: str) -> str:
    """`tests.bundle.intent.retry` -> `tests`. Keeps per-stage buckets
    small. Retry/bundle variants all roll up to their parent stage so
    cost reporting stays readable at 4 rows."""
    return (label or "unknown").split(".", 1)[0]


def _cost_usd(model: str, in_tok: int, out_tok: int) -> float:
    rate = MODEL_PRICING_USD_PER_M.get(model)
    if not rate:
        return 0.0
    in_rate, out_rate = rate
    return (in_tok * in_rate + out_tok * out_rate) / 1_000_000.0


class UsageStats:
    """Per-run token + cost accounting. Aggregates by stage and by model
    so the footer can show where spend went. Zero cost for local models
    (Ollama / unpriced tags). Raw tokens still tracked."""

    def __init__(self) -> None:
        self.calls: int = 0
        self.input_tokens: int = 0
        self.output_tokens: int = 0
        self.cost_usd: float = 0.0
        self.by_stage: dict = {}
        self.by_model: dict = {}

    def add(self, label: str, model: str, in_tok: int, out_tok: int) -> None:
        cost = _cost_usd(model, in_tok, out_tok)
        self.calls += 1
        self.input_tokens += in_tok
        self.output_tokens += out_tok
        self.cost_usd += cost
        stage = _stage_of(label)
        s = self.by_stage.setdefault(
            stage, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        s["calls"] += 1
        s["input_tokens"] += in_tok
        s["output_tokens"] += out_tok
        s["cost_usd"] += cost
        m = self.by_model.setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        m["calls"] += 1
        m["input_tokens"] += in_tok
        m["output_tokens"] += out_tok
        m["cost_usd"] += cost

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "by_stage": {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in self.by_stage.items()
            },
            "by_model": {
                k: {**v, "cost_usd": round(v["cost_usd"], 6)}
                for k, v in self.by_model.items()
            },
        }


class LLMClient(ABC):
    @abstractmethod
    def infer_risks(self, diff: str, parent_source: str, lang: str) -> List[str]: ...

    @abstractmethod
    def generate_tests(
        self,
        parent_source: str,
        diff: str,
        lang: str,
        hints: str,
        risks: Optional[List[str]] = None,
        mode: str = "intent",
    ) -> List[GeneratedTest]: ...

    @abstractmethod
    def judge(
        self,
        test_code: str,
        parent_source: str,
        diff: str,
        failure: str,
        lang: str,
    ) -> dict: ...

    def infer_risks_bundle(self, bundle: str, lang: str) -> List[str]:
        return self.infer_risks(diff="", parent_source=bundle, lang=lang)

    def generate_tests_bundle(
        self,
        bundle: str,
        lang: str,
        hints: str,
        risks: Optional[List[str]] = None,
        mode: str = "intent",
    ) -> List[GeneratedTest]:
        return self.generate_tests(
            parent_source=bundle,
            diff="",
            lang=lang,
            hints=hints,
            risks=risks,
            mode=mode,
        )

    def review_diff(self, bundle: str, lang: str) -> List[ReviewFinding]:
        """Agentic review: flag every suspicious change in the diff without
        running tests. Default implementation returns empty; concrete
        clients override."""
        return []

    def validate_findings(
        self,
        findings: List[ReviewFinding],
        bundle: str,
        lang: str,
    ) -> List[ReviewFinding]:
        """Second-pass filter on reviewer output. Default keeps all."""
        return findings

    def retry_tests(
        self,
        bundle: str,
        lang: str,
        hints: str,
        gap: dict,
    ) -> List[GeneratedTest]:
        """Regenerate a test after the first attempt failed to catch the
        mutation. `gap` carries prior_test, failure_mode, risk. Default
        returns empty; concrete clients override."""
        return []

    def chat(
        self,
        system: str,
        messages: List[dict],
        label: str = "explain.chat",
    ) -> str:
        """Multi-turn conversational completion used by `jitcatch explain`.
        `messages` is a list of {"role": "user"|"assistant", "content": ...}
        entries. The caller owns history and keeps it across turns. Concrete
        clients override; default is a no-op for stubs/tests."""
        raise NotImplementedError("chat not implemented for this client")


class AnthropicClient(LLMClient):
    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_tokens: Optional[int] = None,
        verbose: bool = False,
        log_dir: Optional[Path] = None,
        stage_models: Optional[dict] = None,
    ) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError("anthropic SDK not installed; `pip install anthropic`") from e
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY not set. Export it in your shell "
                "(`export ANTHROPIC_API_KEY=sk-ant-...`), or switch "
                "providers with --provider ollama / --provider openai-compat."
            )
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model
        # stage_models maps stage prefix ("risks"/"tests"/"judge") to model id.
        # Falls back to self._model when a stage isn't listed.
        self._stage_models: dict = dict(stage_models or {})
        self._max_tokens = max_tokens
        self._verbose = verbose
        self._log_dir = Path(log_dir) if log_dir else None
        if self._log_dir:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        self._call_seq = 0
        self.total_calls = 0
        self.truncated_calls = 0
        self.usage = UsageStats()

    def _model_for(self, label: str) -> str:
        stage = label.split(".", 1)[0]
        return self._stage_models.get(stage, self._model)

    def _complete(
        self,
        system: str,
        user: str,
        label: str,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, CallMeta]:
        self._call_seq += 1
        self.total_calls += 1
        seq = self._call_seq
        model = self._model_for(label)
        # Priority: explicit per-call > client default (--max-tokens) > model ceiling.
        requested = (
            max_tokens
            or self._max_tokens
            or MODEL_MAX_OUTPUT_TOKENS.get(model, DEFAULT_MODEL_MAX_OUTPUT_TOKENS)
        )
        cap, clamped = _clamp_max_tokens(model, requested)
        if clamped and self._verbose:
            print(
                f"[JitCatch] call={seq} label={label} clamped max_tokens "
                f"{requested} -> {cap} (model {model} ceiling)",
                file=sys.stderr,
            )
        # Use streaming so large caps don't trip the SDK's 10-min
        # non-streaming guard. get_final_message() yields the same
        # Message shape the non-streaming path returned.
        with self._client.messages.stream(
            model=model,
            max_tokens=cap,
            system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            msg = stream.get_final_message()
        parts: List[str] = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        text = "".join(parts)
        stop_reason = str(getattr(msg, "stop_reason", "") or "")
        usage = getattr(msg, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        if stop_reason == "max_tokens":
            self.truncated_calls += 1
        self.usage.add(label, model, in_tok, out_tok)

        log_path: Optional[Path] = None
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            log_path = self._log_dir / f"{ts}_{seq:03d}_{safe}.log"
            log_path.write_text(
                f"# label: {label}\n"
                f"# seq: {seq}\n"
                f"# model: {model}\n"
                f"# max_tokens_cap: {cap}\n"
                f"# stop_reason: {stop_reason}\n"
                f"# input_tokens: {in_tok}\n"
                f"# output_tokens: {out_tok}\n"
                f"\n===== SYSTEM =====\n{system}\n"
                f"\n===== USER =====\n{user}\n"
                f"\n===== RESPONSE =====\n{text}\n"
            )

        if self._verbose:
            print(
                f"[JitCatch] call={seq} label={label} model={model} "
                f"stop_reason={stop_reason} in={in_tok} out={out_tok} "
                f"cap={cap} log={log_path or '-'}",
                file=sys.stderr,
            )
            if stop_reason == "max_tokens":
                print(
                    f"[JitCatch] WARNING: call {seq} hit max_tokens cap "
                    f"({cap}). Raise --max-tokens or shrink the bundle.",
                    file=sys.stderr,
                )

        return text, CallMeta(
            label=label,
            stop_reason=stop_reason,
            input_tokens=in_tok,
            output_tokens=out_tok,
            log_path=log_path,
        )

    def _system_for_label(self, label: str, default: str) -> str:
        """Hook: map a stage label to a system prompt. Base returns the
        default unchanged. Paid Anthropic models always get the full
        prompts. Subclasses (OpenAICompatClient) may swap to compact
        variants when the target model cannot keep the long prompts in
        its effective attention budget."""
        return default

    def _debug_dump(self, label: str, payload: str) -> None:
        """Write full payload (no truncation) to log dir or stderr."""
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            p = self._log_dir / f"{ts}_dbg_{safe}.log"
            p.write_text(payload)
            if self._verbose:
                print(f"[JitCatch] debug {label} -> {p}", file=sys.stderr)
            return
        if self._verbose:
            print(f"[JitCatch][{label}] {payload}", file=sys.stderr)

    def infer_risks(self, diff: str, parent_source: str, lang: str) -> List[str]:
        user = f"Language: {lang}\n\n--- DIFF ---\n{diff}\n\n--- PARENT SOURCE ---\n{parent_source}"
        system = self._system_for_label("risks", RISKS_SYSTEM)
        out, _ = self._complete(system, user, label="risks")
        risks = _parse_json_array(out)
        if not risks:
            out2, _ = self._complete(system, user + RETRY_SUFFIX, label="risks.retry")
            risks = _parse_json_array(out2)
            if not risks:
                self._debug_dump("risks.empty", out + "\n---retry---\n" + out2)
        return risks

    def infer_risks_bundle(self, bundle: str, lang: str) -> List[str]:
        user = f"Language: {lang}\n\n--- BUNDLE ---\n{bundle}"
        system = self._system_for_label("risks.bundle", RISKS_SYSTEM_BUNDLE)
        out, _ = self._complete(system, user, label="risks.bundle")
        risks = _parse_json_array(out)
        if not risks:
            out2, _ = self._complete(system, user + RETRY_SUFFIX, label="risks.bundle.retry")
            risks = _parse_json_array(out2)
            if not risks:
                self._debug_dump("risks.bundle.empty", out + "\n---retry---\n" + out2)
        return risks

    def generate_tests(
        self,
        parent_source: str,
        diff: str,
        lang: str,
        hints: str,
        risks: Optional[List[str]] = None,
        mode: str = "intent",
    ) -> List[GeneratedTest]:
        default = TESTS_SYSTEM_INTENT if mode == "intent" else TESTS_SYSTEM_DODGY
        system = self._system_for_label(f"tests.{mode}", default)
        risk_block = ""
        if risks:
            risk_block = "\n--- RISKS ---\n" + "\n".join(f"- {r}" for r in risks) + "\n"
        user = (
            f"Language: {lang}\n\n"
            f"--- FRAMEWORK HINTS ---\n{hints}\n\n"
            f"--- PARENT SOURCE ---\n{parent_source}\n\n"
            f"--- DIFF ---\n{diff}\n"
            f"{risk_block}"
        )
        out, meta = self._complete(system, user, label=f"tests.{mode}")
        tests = _parse_tests(out)
        if tests:
            return tests
        # Retry with strict suffix.
        out2, _ = self._complete(system, user + RETRY_SUFFIX, label=f"tests.{mode}.retry")
        tests = _parse_tests(out2)
        if not tests:
            self._debug_dump(f"tests.{mode}.empty", out + "\n---retry---\n" + out2)
        return tests

    def generate_tests_bundle(
        self,
        bundle: str,
        lang: str,
        hints: str,
        risks: Optional[List[str]] = None,
        mode: str = "intent",
    ) -> List[GeneratedTest]:
        default = TESTS_SYSTEM_BUNDLE_INTENT if mode == "intent" else TESTS_SYSTEM_BUNDLE_DODGY
        system = self._system_for_label(f"tests.bundle.{mode}", default)
        risk_block = ""
        if risks:
            risk_block = "\n--- RISKS ---\n" + "\n".join(f"- {r}" for r in risks) + "\n"
        user = (
            f"Language: {lang}\n\n"
            f"--- FRAMEWORK HINTS ---\n{hints}\n\n"
            f"--- BUNDLE ---\n{bundle}\n"
            f"{risk_block}"
        )
        out, meta = self._complete(system, user, label=f"tests.bundle.{mode}")
        tests = _parse_tests(out)
        if tests:
            return tests
        out2, _ = self._complete(system, user + RETRY_SUFFIX, label=f"tests.bundle.{mode}.retry")
        tests = _parse_tests(out2)
        if not tests:
            self._debug_dump(f"tests.bundle.{mode}.empty", out + "\n---retry---\n" + out2)
        return tests

    def review_diff(self, bundle: str, lang: str) -> List[ReviewFinding]:
        user = f"Language: {lang}\n\n--- BUNDLE ---\n{bundle}"
        system = self._system_for_label("review", REVIEWER_SYSTEM)
        out, _ = self._complete(system, user, label="review")
        findings = _parse_findings(out)
        if not findings:
            out2, _ = self._complete(system, user + RETRY_SUFFIX, label="review.retry")
            findings = _parse_findings(out2)
            if not findings:
                self._debug_dump("review.empty", out + "\n---retry---\n" + out2)
        return findings

    def validate_findings(
        self,
        findings: List[ReviewFinding],
        bundle: str,
        lang: str,
    ) -> List[ReviewFinding]:
        if not findings:
            return findings
        batch = [
            {
                "index": i,
                "file": f.file,
                "line": f.line,
                "title": f.title,
                "rationale": f.rationale,
                "severity": f.severity,
                "category": f.category,
                "confidence": f.confidence,
            }
            for i, f in enumerate(findings)
        ]
        user = (
            f"Language: {lang}\n\n"
            f"--- BUNDLE ---\n{bundle}\n\n"
            f"--- FINDINGS ---\n{json.dumps(batch, indent=2)}"
        )
        system = self._system_for_label("review.validate", VALIDATOR_SYSTEM)
        out, _ = self._complete(system, user, label="review.validate")
        verdicts = _parse_validations(out)
        if not verdicts:
            # Keep all on unparseable. Aligns with BugBot's "err on flagging".
            for f in findings:
                f.validator_verdict = "keep"
                f.validator_note = "validator output unparseable. Kept by default"
            return findings
        by_idx = {v["index"]: v for v in verdicts if isinstance(v.get("index"), int)}
        kept: List[ReviewFinding] = []
        for i, f in enumerate(findings):
            v = by_idx.get(i)
            if not v:
                f.validator_verdict = "keep"
                f.validator_note = "no validator verdict. Kept by default"
                kept.append(f)
                continue
            verdict = str(v.get("verdict", "keep")).lower()
            note = str(v.get("note", ""))
            if verdict == "drop":
                f.validator_verdict = "drop"
                f.validator_note = note
                continue
            if verdict == "downgrade":
                new_sev = str(v.get("severity", f.severity))
                f.severity = new_sev
                f.validator_verdict = "downgrade"
                f.validator_note = note
            else:
                f.validator_verdict = "keep"
                f.validator_note = note
            kept.append(f)
        return kept

    def retry_tests(
        self,
        bundle: str,
        lang: str,
        hints: str,
        gap: dict,
    ) -> List[GeneratedTest]:
        user = (
            f"Language: {lang}\n\n"
            f"--- FRAMEWORK HINTS ---\n{hints}\n\n"
            f"--- BUNDLE ---\n{bundle}\n\n"
            f"--- UNCAUGHT RISK ---\n{gap.get('risk', '')}\n\n"
            f"--- FAILURE MODE ---\n{gap.get('failure_mode', 'unknown')}\n\n"
            f"--- PRIOR TEST NAME ---\n{gap.get('prior_test_name', '')}\n\n"
            f"--- PRIOR TEST CODE ---\n{gap.get('prior_test_code', '')}\n\n"
            f"--- PRIOR FAILURE OUTPUT ---\n{gap.get('failure_output', '')[:2000]}"
        )
        system = self._system_for_label("tests.retry", TESTS_SYSTEM_RETRY)
        out, _ = self._complete(system, user, label="tests.retry")
        tests = _parse_tests(out)
        if tests:
            return tests
        out2, _ = self._complete(system, user + RETRY_SUFFIX, label="tests.retry.retry")
        tests = _parse_tests(out2)
        if not tests:
            self._debug_dump("tests.retry.empty", out + "\n---retry---\n" + out2)
        return tests

    def judge(
        self,
        test_code: str,
        parent_source: str,
        diff: str,
        failure: str,
        lang: str,
    ) -> dict:
        user = (
            f"Language: {lang}\n\n"
            f"--- PARENT SOURCE ---\n{parent_source}\n\n"
            f"--- DIFF ---\n{diff}\n\n"
            f"--- TEST CODE ---\n{test_code}\n\n"
            f"--- FAILURE OUTPUT ---\n{failure}"
        )
        system = self._system_for_label("judge", JUDGE_SYSTEM)
        out, _ = self._complete(system, user, label="judge", max_tokens=MODEL_MAX_OUTPUT_TOKENS.get(
                self._model_for("judge"), DEFAULT_MODEL_MAX_OUTPUT_TOKENS
            ))
        parsed = _parse_judge(out)
        if parsed.get("_unparseable"):
            retry_out, _ = self._complete(
                system, user + RETRY_SUFFIX, label="judge.retry", max_tokens=MODEL_MAX_OUTPUT_TOKENS.get(
                self._model_for("judge"), DEFAULT_MODEL_MAX_OUTPUT_TOKENS
            )
            )
            retry_parsed = _parse_judge(retry_out)
            if not retry_parsed.get("_unparseable"):
                retry_parsed["raw"] = retry_out
                return retry_parsed
            parsed["raw"] = out + "\n---retry---\n" + retry_out
            self._debug_dump("judge.unparseable", parsed["raw"])
            return parsed
        parsed["raw"] = out
        return parsed

    def chat(
        self,
        system: str,
        messages: List[dict],
        label: str = "explain.chat",
    ) -> str:
        self._call_seq += 1
        self.total_calls += 1
        seq = self._call_seq
        model = self._model_for(label)
        cap = (
            self._max_tokens
            or MODEL_MAX_OUTPUT_TOKENS.get(model, DEFAULT_MODEL_MAX_OUTPUT_TOKENS)
        )
        cap, _ = _clamp_max_tokens(model, cap)
        with self._client.messages.stream(
            model=model,
            max_tokens=cap,
            system=system,
            messages=messages,
        ) as stream:
            msg = stream.get_final_message()
        parts: List[str] = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        text = "".join(parts)
        usage = getattr(msg, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        self.usage.add(label, model, in_tok, out_tok)
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            log_path = self._log_dir / f"{ts}_{seq:03d}_{safe}.log"
            log_path.write_text(
                f"# label: {label}\n# seq: {seq}\n# model: {model}\n"
                f"# input_tokens: {in_tok}\n# output_tokens: {out_tok}\n"
                f"\n===== SYSTEM =====\n{system}\n"
                f"\n===== MESSAGES =====\n{json.dumps(messages, indent=2)}\n"
                f"\n===== RESPONSE =====\n{text}\n"
            )
        return text


class OpenAICompatClient(AnthropicClient):
    """OpenAI-compatible chat-completions client for Ollama, LM Studio,
    vLLM, LocalAI, Groq, OpenRouter, Together, Fireworks, etc. Reuses
    AnthropicClient's higher-level methods (infer_risks, generate_tests,
    judge, review_diff, validate_findings, retry_tests). Only the
    transport in `_complete` differs. Subclassing AnthropicClient is a
    Phase-1 pragmatic shortcut to avoid duplicating ~150 lines; Phase 2
    will extract a shared base."""

    def __init__(
        self,
        model: str,
        base_url: str = OLLAMA_DEFAULT_BASE_URL,
        api_key: Optional[str] = None,
        max_tokens: Optional[int] = None,
        verbose: bool = False,
        log_dir: Optional[Path] = None,
        stage_models: Optional[dict] = None,
        timeout: float = 120.0,
    ) -> None:
        # Skip super().__init__: that path imports the anthropic SDK and
        # requires ANTHROPIC_API_KEY. Set shared state directly.
        import httpx
        self._http = httpx.Client(timeout=timeout)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout
        self._model = model
        self._stage_models = dict(stage_models or {})
        self._max_tokens = max_tokens
        self._verbose = verbose
        self._log_dir = Path(log_dir) if log_dir else None
        if self._log_dir:
            self._log_dir.mkdir(parents=True, exist_ok=True)
        self._call_seq = 0
        self.total_calls = 0
        self.truncated_calls = 0
        self.usage = UsageStats()

    def _complete(
        self,
        system: str,
        user: str,
        label: str,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, "CallMeta"]:
        self._call_seq += 1
        self.total_calls += 1
        seq = self._call_seq
        model = self._model_for(label)
        ceiling = MODEL_MAX_OUTPUT_TOKENS.get(model, OPENAI_COMPAT_DEFAULT_CAP)
        requested = max_tokens or self._max_tokens or ceiling
        cap = min(requested, ceiling)
        clamped = requested > ceiling
        if clamped and self._verbose:
            print(
                f"[JitCatch] call={seq} label={label} clamped max_tokens "
                f"{requested} -> {cap} (model {model} ceiling)",
                file=sys.stderr,
            )
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": cap,
            "temperature": 0,
            "stream": False,
        }
        url = f"{self._base_url}/chat/completions"
        try:
            resp = self._http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"OpenAI-compat call failed at {url}: {type(e).__name__}: {e}"
            ) from e
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI-compat response had no choices: {data!r}")
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "")
        finish_reason = str(choices[0].get("finish_reason") or "")
        # Normalize OpenAI's "length" -> Anthropic's "max_tokens" so the
        # rest of the pipeline's truncation-aware logic works unchanged.
        stop_reason = "max_tokens" if finish_reason == "length" else finish_reason
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        if stop_reason == "max_tokens":
            self.truncated_calls += 1
        self.usage.add(label, model, in_tok, out_tok)

        log_path: Optional[Path] = None
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            log_path = self._log_dir / f"{ts}_{seq:03d}_{safe}.log"
            log_path.write_text(
                f"# label: {label}\n"
                f"# seq: {seq}\n"
                f"# model: {model}\n"
                f"# endpoint: {url}\n"
                f"# max_tokens_cap: {cap}\n"
                f"# stop_reason: {stop_reason}\n"
                f"# input_tokens: {in_tok}\n"
                f"# output_tokens: {out_tok}\n"
                f"\n===== SYSTEM =====\n{system}\n"
                f"\n===== USER =====\n{user}\n"
                f"\n===== RESPONSE =====\n{text}\n"
            )

        if self._verbose:
            print(
                f"[JitCatch] call={seq} label={label} model={model} "
                f"stop_reason={stop_reason} in={in_tok} out={out_tok} "
                f"cap={cap} log={log_path or '-'}",
                file=sys.stderr,
            )
            if stop_reason == "max_tokens":
                print(
                    f"[JitCatch] WARNING: call {seq} hit max_tokens cap "
                    f"({cap}). Raise --max-tokens or shrink the bundle.",
                    file=sys.stderr,
                )

        return text, CallMeta(
            label=label,
            stop_reason=stop_reason,
            input_tokens=in_tok,
            output_tokens=out_tok,
            log_path=log_path,
        )

    # Label -> compact system prompt map. Only bundle paths have compact
    # variants today. Single-file paths are shorter and less pressured.
    _COMPACT_SYSTEM_BY_LABEL = {
        "risks.bundle": RISKS_SYSTEM_BUNDLE_COMPACT,
        "tests.bundle.intent": TESTS_SYSTEM_BUNDLE_INTENT_COMPACT,
        "tests.bundle.dodgy": TESTS_SYSTEM_BUNDLE_DODGY_COMPACT,
        "review": REVIEWER_SYSTEM_COMPACT,
    }

    def _system_for_label(self, label: str, default: str) -> str:
        """Swap to compact prompt when the stage model is small. Paid/large
        models (Anthropic, gemma4:26b+, qwen2.5-coder:14b+) stay on full
        prompts via the default passthrough on non-small models."""
        if not _is_small_model(self._model_for(label)):
            return default
        # Normalize retry labels ("risks.bundle.retry" -> "risks.bundle").
        key = label[: -len(".retry")] if label.endswith(".retry") else label
        return self._COMPACT_SYSTEM_BY_LABEL.get(key, default)

    def chat(
        self,
        system: str,
        messages: List[dict],
        label: str = "explain.chat",
    ) -> str:
        self._call_seq += 1
        self.total_calls += 1
        seq = self._call_seq
        model = self._model_for(label)
        ceiling = MODEL_MAX_OUTPUT_TOKENS.get(model, OPENAI_COMPAT_DEFAULT_CAP)
        cap = min(self._max_tokens or ceiling, ceiling)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        full_messages = [{"role": "system", "content": system}] + list(messages)
        body = {
            "model": model,
            "messages": full_messages,
            "max_tokens": cap,
            "temperature": 0.3,
            "stream": False,
        }
        url = f"{self._base_url}/chat/completions"
        try:
            resp = self._http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"OpenAI-compat chat failed at {url}: {type(e).__name__}: {e}"
            ) from e
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI-compat chat had no choices: {data!r}")
        text = str((choices[0].get("message") or {}).get("content") or "")
        usage = data.get("usage") or {}
        in_tok = int(usage.get("prompt_tokens") or 0)
        out_tok = int(usage.get("completion_tokens") or 0)
        self.usage.add(label, model, in_tok, out_tok)
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            log_path = self._log_dir / f"{ts}_{seq:03d}_{safe}.log"
            log_path.write_text(
                f"# label: {label}\n# seq: {seq}\n# model: {model}\n"
                f"# endpoint: {url}\n"
                f"# input_tokens: {in_tok}\n# output_tokens: {out_tok}\n"
                f"\n===== MESSAGES =====\n{json.dumps(full_messages, indent=2)}\n"
                f"\n===== RESPONSE =====\n{text}\n"
            )
        return text


class OllamaClient(OpenAICompatClient):
    """Ollama-native transport using `/api/chat`. Unlocks features the
    OpenAI-compat shim silently drops: `format: "json"` for strict JSON
    output and `options.num_ctx` for extended context. Small models that
    ignore strict-JSON instructions on the `/v1` shim produce reliable
    JSON here because the server enforces the schema at sample time.

    The base_url accepts either the Ollama native root
    (`http://localhost:11434`) or the OpenAI-compat root
    (`http://localhost:11434/v1`). The trailing `/v1` is stripped."""

    def __init__(
        self,
        model: str,
        base_url: str = OLLAMA_DEFAULT_BASE_URL,
        api_key: Optional[str] = None,
        max_tokens: Optional[int] = None,
        verbose: bool = False,
        log_dir: Optional[Path] = None,
        stage_models: Optional[dict] = None,
        timeout: float = 120.0,
        num_ctx: int = OLLAMA_DEFAULT_NUM_CTX,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key=api_key,
            max_tokens=max_tokens,
            verbose=verbose,
            log_dir=log_dir,
            stage_models=stage_models,
            timeout=timeout,
        )
        # Strip trailing /v1 if caller passed the OpenAI-compat URL.
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url[: -len("/v1")]
        self._num_ctx = num_ctx

    def _complete(
        self,
        system: str,
        user: str,
        label: str,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, "CallMeta"]:
        self._call_seq += 1
        self.total_calls += 1
        seq = self._call_seq
        model = self._model_for(label)
        ceiling = MODEL_MAX_OUTPUT_TOKENS.get(model, OPENAI_COMPAT_DEFAULT_CAP)
        requested = max_tokens or self._max_tokens or ceiling
        cap = min(requested, ceiling)
        clamped = requested > ceiling
        if clamped and self._verbose:
            print(
                f"[JitCatch] call={seq} label={label} clamped max_tokens "
                f"{requested} -> {cap} (model {model} ceiling)",
                file=sys.stderr,
            )
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        # All jitcatch stages expect JSON output. `format: "json"` forces
        # the model to emit parseable JSON at sample time. The single
        # biggest reliability win for small models that otherwise drop to
        # prose summaries.
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
                "num_ctx": self._num_ctx,
                "num_predict": cap,
            },
        }
        url = f"{self._base_url}/api/chat"
        try:
            resp = self._http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Ollama call failed at {url}: {type(e).__name__}: {e}"
            ) from e
        message = data.get("message") or {}
        text = str(message.get("content") or "")
        done_reason = str(data.get("done_reason") or "")
        # Normalize to Anthropic's stop_reason vocabulary so the rest of
        # the pipeline's truncation logic is identical to the other
        # clients. Ollama uses "length" when num_predict was the binding
        # constraint.
        stop_reason = "max_tokens" if done_reason == "length" else done_reason
        in_tok = int(data.get("prompt_eval_count") or 0)
        out_tok = int(data.get("eval_count") or 0)
        if stop_reason == "max_tokens":
            self.truncated_calls += 1
        self.usage.add(label, model, in_tok, out_tok)

        log_path: Optional[Path] = None
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            log_path = self._log_dir / f"{ts}_{seq:03d}_{safe}.log"
            log_path.write_text(
                f"# label: {label}\n"
                f"# seq: {seq}\n"
                f"# model: {model}\n"
                f"# endpoint: {url}\n"
                f"# num_ctx: {self._num_ctx}\n"
                f"# num_predict: {cap}\n"
                f"# done_reason: {done_reason}\n"
                f"# stop_reason: {stop_reason}\n"
                f"# input_tokens: {in_tok}\n"
                f"# output_tokens: {out_tok}\n"
                f"\n===== SYSTEM =====\n{system}\n"
                f"\n===== USER =====\n{user}\n"
                f"\n===== RESPONSE =====\n{text}\n"
            )

        if self._verbose:
            print(
                f"[JitCatch] call={seq} label={label} model={model} "
                f"stop_reason={stop_reason} in={in_tok} out={out_tok} "
                f"cap={cap} ctx={self._num_ctx} log={log_path or '-'}",
                file=sys.stderr,
            )
            if stop_reason == "max_tokens":
                print(
                    f"[JitCatch] WARNING: call {seq} hit num_predict cap "
                    f"({cap}). Raise --max-tokens or shrink the bundle.",
                    file=sys.stderr,
                )

        return text, CallMeta(
            label=label,
            stop_reason=stop_reason,
            input_tokens=in_tok,
            output_tokens=out_tok,
            log_path=log_path,
        )

    def chat(
        self,
        system: str,
        messages: List[dict],
        label: str = "explain.chat",
    ) -> str:
        self._call_seq += 1
        self.total_calls += 1
        seq = self._call_seq
        model = self._model_for(label)
        ceiling = MODEL_MAX_OUTPUT_TOKENS.get(model, OPENAI_COMPAT_DEFAULT_CAP)
        cap = min(self._max_tokens or ceiling, ceiling)
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        full_messages = [{"role": "system", "content": system}] + list(messages)
        body = {
            "model": model,
            "messages": full_messages,
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_ctx": self._num_ctx,
                "num_predict": cap,
            },
        }
        url = f"{self._base_url}/api/chat"
        try:
            resp = self._http.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                f"Ollama chat failed at {url}: {type(e).__name__}: {e}"
            ) from e
        text = str((data.get("message") or {}).get("content") or "")
        in_tok = int(data.get("prompt_eval_count") or 0)
        out_tok = int(data.get("eval_count") or 0)
        self.usage.add(label, model, in_tok, out_tok)
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            log_path = self._log_dir / f"{ts}_{seq:03d}_{safe}.log"
            log_path.write_text(
                f"# label: {label}\n# seq: {seq}\n# model: {model}\n"
                f"# endpoint: {url}\n"
                f"# input_tokens: {in_tok}\n# output_tokens: {out_tok}\n"
                f"\n===== MESSAGES =====\n{json.dumps(full_messages, indent=2)}\n"
                f"\n===== RESPONSE =====\n{text}\n"
            )
        return text


class StubClient(LLMClient):
    """Reads canned responses from `.jitcatch_stub.json` at repo root.

    Schema:
    {
      "risks": ["..."],
      "intent_tests": [{"name":"...","code":"...","rationale":"..."}],
      "dodgy_tests":  [{"name":"...","code":"...","rationale":"..."}],
      "bundle_intent_tests": [...],
      "bundle_dodgy_tests": [...],
      "judge": {"tp_prob": 0.8, "bucket": "High", "rationale": "..."}
    }
    """

    def __init__(self, repo: Path) -> None:
        self._data: dict = {}
        self.usage = UsageStats()
        stub = repo / ".jitcatch_stub.json"
        if stub.exists():
            try:
                self._data = json.loads(stub.read_text())
            except json.JSONDecodeError:
                self._data = {}

    def infer_risks(self, diff: str, parent_source: str, lang: str) -> List[str]:
        return list(self._data.get("risks", []))

    def infer_risks_bundle(self, bundle: str, lang: str) -> List[str]:
        return list(self._data.get("bundle_risks", self._data.get("risks", [])))

    def generate_tests(
        self,
        parent_source: str,
        diff: str,
        lang: str,
        hints: str,
        risks: Optional[List[str]] = None,
        mode: str = "intent",
    ) -> List[GeneratedTest]:
        key = "intent_tests" if mode == "intent" else "dodgy_tests"
        raw = self._data.get(key, []) or []
        return _materialize_tests(raw)

    def generate_tests_bundle(
        self,
        bundle: str,
        lang: str,
        hints: str,
        risks: Optional[List[str]] = None,
        mode: str = "intent",
    ) -> List[GeneratedTest]:
        if mode == "intent":
            key_primary, key_fallback = "bundle_intent_tests", "intent_tests"
        else:
            key_primary, key_fallback = "bundle_dodgy_tests", "dodgy_tests"
        raw = self._data.get(key_primary)
        if raw is None:
            raw = self._data.get(key_fallback, [])
        return _materialize_tests(raw or [])

    def judge(self, test_code, parent_source, diff, failure, lang) -> dict:
        d = self._data.get("judge") or {}
        return {
            "tp_prob": float(d.get("tp_prob", 0.0)),
            "bucket": d.get("bucket", "Low"),
            "rationale": d.get("rationale", "stub"),
            "raw": json.dumps(d) if d else "",
        }

    def review_diff(self, bundle: str, lang: str) -> List[ReviewFinding]:
        raw = self._data.get("review_findings", []) or []
        out: List[ReviewFinding] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            out.append(
                ReviewFinding(
                    file=str(entry.get("file") or ""),
                    line=entry.get("line"),
                    title=str(entry.get("title") or ""),
                    rationale=str(entry.get("rationale") or ""),
                    severity=str(entry.get("severity") or "Medium"),
                    category=str(entry.get("category") or ""),
                    confidence=float(entry.get("confidence", 0.8)),
                )
            )
        return out

    def retry_tests(self, bundle, lang, hints, gap) -> List[GeneratedTest]:
        raw = self._data.get("retry_tests", []) or []
        return _materialize_tests(raw)

    def chat(
        self,
        system: str,
        messages: List[dict],
        label: str = "explain.chat",
    ) -> str:
        """Canned reply for tests. Schema:
            {"chat_reply": "fixed string"}
          or {"chat_replies": ["first", "second", ...]}  # cycled by turn.
        Falls back to a deterministic echo when neither is set."""
        replies = self._data.get("chat_replies")
        if isinstance(replies, list) and replies:
            turns = sum(1 for m in messages if m.get("role") == "assistant")
            return str(replies[turns % len(replies)])
        reply = self._data.get("chat_reply")
        if reply is not None:
            return str(reply)
        last_user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        return f"stub reply to: {last_user}"


def _materialize_tests(raw: list) -> List[GeneratedTest]:
    out: List[GeneratedTest] = []
    for i, t in enumerate(raw):
        if not isinstance(t, dict) or "code" not in t:
            continue
        out.append(
            GeneratedTest(
                name=str(t.get("name", f"stub_{i}")),
                code=str(t["code"]),
                rationale=str(t.get("rationale", "")),
            )
        )
    return out


def _strip_code_fence(text: str) -> str:
    """Extract content from the first ```...``` fence. Tolerates prose
    before the fence and a missing closing fence (truncation)."""
    text = text.strip()
    # Non-anchored: allow prose before the opening fence.
    m = re.search(r"```(?:json|javascript|python|py|js)?\s*\n(.*?)(?:\n```|\Z)", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _extract_first_json_object(text: str) -> Optional[str]:
    """Find the first balanced {...} block in text. Skips over string
    literals so braces inside JSON values don't confuse depth counting.
    Returns None if no opening brace, or if EOF reached with depth>0
    (truncation. Caller should try _recover_truncated_json)."""
    s = text
    n = len(s)
    i = 0
    while i < n:
        if s[i] == "{":
            depth = 0
            j = i
            in_str = False
            esc = False
            while j < n:
                c = s[j]
                if in_str:
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == '"':
                        in_str = False
                elif c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        return s[i : j + 1]
                j += 1
            return None
        i += 1
    return None


def _recover_truncated_tests_json(text: str) -> Optional[str]:
    """Salvage at least the completed `tests` entries from a truncated
    response. Strategy: find `"tests"` key, locate the enclosing `[`,
    walk forward collecting top-level objects until the stream ends or
    breaks mid-object, then synthesize a closing `]}`.

    Returns a JSON string `{"tests":[...]}` or None if nothing usable.
    """
    m = re.search(r'"tests"\s*:\s*\[', text)
    if not m:
        return None
    start = m.end()  # position just after '['
    n = len(text)
    i = start
    completed: List[str] = []
    # Skip whitespace/commas between entries.
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if text[i] == "]":
            break  # array closed normally; strict parser would have worked
        if text[i] != "{":
            break
        # Walk a balanced object.
        depth = 0
        j = i
        in_str = False
        esc = False
        closed = False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    completed.append(text[i : j + 1])
                    i = j + 1
                    closed = True
                    break
            j += 1
        if not closed:
            break  # truncated mid-object; everything collected so far is the salvage
    if not completed:
        return None
    return '{"tests":[' + ",".join(completed) + "]}"


def _format_risk_entry(obj: Any) -> Optional[str]:
    """Normalize a risk (dict or scalar) to a single display string.
    Objects become `[file:line] (class) risk` so downstream consumers
    can regex out the metadata for report grouping. Tolerant of
    small-model key variants: `{file, change}` (deepseek-coder),
    `{file, issue}`, `{file, summary}`."""
    if isinstance(obj, (str, int, float)):
        return str(obj)
    if not isinstance(obj, dict):
        return None
    risk = (
        obj.get("risk")
        or obj.get("description")
        or obj.get("text")
        or obj.get("change")
        or obj.get("issue")
        or obj.get("summary")
        or obj.get("title")
    )
    if not risk:
        return None
    file = obj.get("file") or obj.get("path") or obj.get("filename") or ""
    line = obj.get("line")
    cls = obj.get("class") or obj.get("category") or obj.get("type") or ""
    head = ""
    if file:
        head = f"[{file}:{line}]" if line is not None else f"[{file}]"
    if cls:
        head = (head + " " if head else "") + f"({cls})"
    return f"{head} {risk}".strip() if head else str(risk)


# Keys small models commonly pick when they ignore "risks". All map to
# a list of risk-shaped entries. Order matters: prefer the canonical key
# then deepseek/gemma/etc variants. Never includes keys owned by other
# parsers (e.g. "tests", "findings"). Those have dedicated parse paths
# and cross-parser collisions would silently reclassify outputs.
_RISK_ARRAY_KEYS: Tuple[str, ...] = (
    "risks",
    "bundle_risks",
    "changes",
    "issues",
    "items",
    "results",
    "risk_list",
)


def _parse_json_array(text: str) -> List[str]:
    candidates: List[str] = [text.strip(), _strip_code_fence(text)]
    # also try to find a bare [...] in the text
    m = re.search(r"\[[\s\S]*?\]", text)
    if m:
        candidates.append(m.group(0))
    # Small models often wrap the array inside the first balanced {...}
    # object even when asked for a bare array. Include it so the dict
    # branch below has a chance to recognize known wrapper keys.
    extracted = _extract_first_json_object(text)
    if extracted:
        candidates.append(extracted)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            out: List[str] = []
            for x in data:
                formatted = _format_risk_entry(x)
                if formatted:
                    out.append(formatted)
            return out
        if isinstance(data, dict):
            for key in _RISK_ARRAY_KEYS:
                arr = data.get(key)
                if isinstance(arr, list):
                    out = []
                    for x in arr:
                        formatted = _format_risk_entry(x)
                        if formatted:
                            out.append(formatted)
                    if out:
                        return out
    return []


def _parse_tests(text: str) -> List[GeneratedTest]:
    data: Any = None
    # Try strict whole-string first.
    try:
        data = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        data = None
    # Try code-fence content.
    if data is None:
        fenced = _strip_code_fence(text)
        if fenced and fenced != text.strip():
            try:
                data = json.loads(fenced)
            except (json.JSONDecodeError, ValueError):
                data = None
    # Try first balanced {...}.
    if data is None:
        extracted = _extract_first_json_object(text)
        if extracted:
            try:
                data = json.loads(extracted)
            except (json.JSONDecodeError, ValueError):
                data = None
    # Last resort: recover from truncation.
    if data is None:
        salvaged = _recover_truncated_tests_json(text)
        if salvaged:
            try:
                data = json.loads(salvaged)
            except (json.JSONDecodeError, ValueError):
                data = None
    if data is None:
        return []

    arr: Any = None
    if isinstance(data, list):
        arr = data
    elif isinstance(data, dict):
        # Prefer canonical "tests", then small-model variants. Never cross
        # into "findings" / "risks". Those collide with other parsers.
        for key in ("tests", "unit_tests", "cases", "testcases", "results"):
            if isinstance(data.get(key), list):
                arr = data[key]
                break
    if not isinstance(arr, list):
        return []
    out: List[GeneratedTest] = []
    for i, t in enumerate(arr):
        if not isinstance(t, dict):
            continue
        # Small models sometimes use `test_code`, `snippet`, `src`, or
        # `source` instead of `code`. Accept whichever is present.
        code_val = (
            t.get("code")
            or t.get("test_code")
            or t.get("snippet")
            or t.get("src")
            or t.get("source")
        )
        if not code_val:
            continue
        out.append(
            GeneratedTest(
                name=str(t.get("name") or t.get("title") or f"t_{i}"),
                code=str(code_val),
                rationale=str(t.get("rationale") or t.get("reason") or ""),
            )
        )
    return out


def _parse_findings(text: str) -> List[ReviewFinding]:
    """Parse reviewer output to ReviewFinding list. Accepts either a raw
    array or {findings: [...]}. Tolerates code fences."""
    data: Any = None
    try:
        data = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        data = None
    if data is None:
        fenced = _strip_code_fence(text)
        if fenced and fenced != text.strip():
            try:
                data = json.loads(fenced)
            except (json.JSONDecodeError, ValueError):
                data = None
    if data is None:
        extracted = _extract_first_json_object(text)
        if extracted:
            try:
                data = json.loads(extracted)
            except (json.JSONDecodeError, ValueError):
                data = None
    if data is None:
        return []
    arr: Any = None
    if isinstance(data, list):
        arr = data
    elif isinstance(data, dict):
        # Small models reach for "issues" or "items" when they ignore
        # the "findings" instruction. Both are structurally identical -
        # accept them. Entries still need title|rationale to qualify,
        # so stray risk-shaped arrays won't be reclassified as findings.
        for key in ("findings", "issues", "items", "problems", "bugs"):
            if isinstance(data.get(key), list):
                arr = data[key]
                break
    if not isinstance(arr, list):
        return []
    out: List[ReviewFinding] = []
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or entry.get("summary") or "").strip()
        rationale = str(
            entry.get("rationale") or entry.get("reason") or entry.get("description") or ""
        ).strip()
        if not title and not rationale:
            continue
        line_raw = entry.get("line")
        try:
            line = int(line_raw) if line_raw is not None else None
        except (TypeError, ValueError):
            line = None
        confidence_raw = entry.get("confidence", 0.0)
        try:
            conf = float(confidence_raw)
        except (TypeError, ValueError):
            conf = 0.0
        out.append(
            ReviewFinding(
                file=str(entry.get("file") or ""),
                line=line,
                title=title or rationale[:80],
                rationale=rationale or title,
                severity=str(entry.get("severity") or "Medium"),
                category=str(entry.get("category") or ""),
                confidence=max(0.0, min(1.0, conf)),
                raw=json.dumps(entry, sort_keys=True),
            )
        )
    return out


def _parse_validations(text: str) -> List[dict]:
    data: Any = None
    for cand in (text.strip(), _strip_code_fence(text)):
        try:
            data = json.loads(cand)
            break
        except (json.JSONDecodeError, ValueError):
            data = None
    if data is None:
        extracted = _extract_first_json_object(text)
        if extracted:
            try:
                data = json.loads(extracted)
            except (json.JSONDecodeError, ValueError):
                data = None
    if data is None:
        return []
    arr = data.get("validations") if isinstance(data, dict) else data
    if not isinstance(arr, list):
        return []
    return [v for v in arr if isinstance(v, dict)]


def _parse_judge(text: str) -> dict:
    """Relaxed judge parser. Tries strict whole-string, code-fence,
    first balanced {...}. On total failure sets `_unparseable=True`."""
    candidates: List[str] = [text.strip(), _strip_code_fence(text)]
    extracted = _extract_first_json_object(text)
    if extracted:
        candidates.append(extracted)
    for cand in candidates:
        try:
            data = json.loads(cand)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return {
                "tp_prob": float(data.get("tp_prob", 0.0)),
                "bucket": str(data.get("bucket", "Low")),
                "rationale": str(data.get("rationale", "")),
            }
    return {
        "tp_prob": 0.0,
        "bucket": "Low",
        "rationale": "unparseable judge output",
        "_unparseable": True,
    }
