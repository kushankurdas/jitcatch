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

from .config import GeneratedTest


STRICT_JSON_SUFFIX = (
    "\n\nOutput MUST be the raw JSON object only. Do not wrap in code "
    "fences. Do not add commentary before or after. Start your response "
    "with the opening brace/bracket and end with the closing one."
)


RISK_TAXONOMY_CLAUSE = (
    "Examine the diff across these risk classes explicitly — do not skip any:\n"
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
    "Aim for one entry per independent risk — do not emit multiple entries for the "
    "same underlying issue across call sites."
)

RISKS_SYSTEM = (
    "You are a senior software engineer reviewing a code change. "
    "Given a diff, identify risks — concrete ways the change could introduce a bug. "
    + RISK_TAXONOMY_CLAUSE
    + " Return a JSON array of such risk objects."
    + STRICT_JSON_SUFFIX
)

RISKS_SYSTEM_BUNDLE = (
    "You are a senior software engineer reviewing a code change that spans multiple files. "
    "Given per-file parent sources, diffs, and optional usage-context files, identify risks — "
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
    "test inside the test body — a test that defines `parentBehavior()` and `changedBehavior()` "
    "as local functions and asserts on those passes identically on parent and child and is "
    "useless.\n\n"
    "You MAY and SHOULD mock TRANSITIVE dependencies when needed to drive the "
    "function under test into the failing branch — for example: HTTP clients "
    "(axios / request / node-fetch), JWT libraries (force `jwt.verify` to "
    "throw), DB drivers, SQS clients, filesystem operations. This is required "
    "for middleware, route handlers, and any function whose branch coverage "
    "depends on an external call's outcome. Mocking a dependency of the module "
    "under test is not the same as stubbing the module under test — the "
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
    "Do not import usage-context files as the subject of assertions — they are there for "
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
    "CRITICAL — weak-catch semantics: a regression-detection test asserts the "
    "PARENT's observable behavior. It PASSES on parent and FAILS on child by "
    "design — that is how regressions are detected. Do NOT mark a test as FP "
    "merely because it \"asserts what the parent looked like\" or \"encodes the "
    "old behavior\" — that is the intended pattern. Only mark FP when one of:\n"
    "  - the failure is a runtime/import/syntax error unrelated to the behavior\n"
    "    change (ModuleNotFoundError, NameError, ReferenceError);\n"
    "  - the test is non-deterministic (time, random, network, ordering);\n"
    "  - the test reimplements parent and child logic as local stubs and "
    "compares those stubs to themselves (useless tautology);\n"
    "  - the assertion targets something the diff did NOT change;\n"
    "  - the behavior change is intentional, documented, and clearly not a bug "
    "(e.g. an added feature, an API version bump).\n"
    "Source-grep tests (read a file, assert a token/operator is present) are "
    "brittle but still valid TP signal when the diff truly changed that token — "
    "bucket them Medium, do not reject."
    + STRICT_JSON_SUFFIX
)

RETRY_SUFFIX = (
    "\n\nRetry: previous response was not parseable. Return ONLY the raw "
    "JSON object / array. No prose. No code fences. No trailing commentary."
)


MODEL_MAX_OUTPUT_TOKENS = {
    "claude-opus-4-7": 32000,
    "claude-opus-4-6": 32000,
    "claude-sonnet-4-6": 64000,
    "claude-sonnet-4-5": 64000,
    "claude-haiku-4-5": 64000,
}
DEFAULT_MODEL_MAX_OUTPUT_TOKENS = 32000


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
            raise RuntimeError("ANTHROPIC_API_KEY not set")
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
                f"[jitcatch] call={seq} label={label} clamped max_tokens "
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
                f"[jitcatch] call={seq} label={label} model={model} "
                f"stop_reason={stop_reason} in={in_tok} out={out_tok} "
                f"cap={cap} log={log_path or '-'}",
                file=sys.stderr,
            )
            if stop_reason == "max_tokens":
                print(
                    f"[jitcatch] WARNING: call {seq} hit max_tokens cap "
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

    def _debug_dump(self, label: str, payload: str) -> None:
        """Write full payload (no truncation) to log dir or stderr."""
        if self._log_dir:
            ts = time.strftime("%Y%m%d-%H%M%S")
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)
            p = self._log_dir / f"{ts}_dbg_{safe}.log"
            p.write_text(payload)
            if self._verbose:
                print(f"[jitcatch] debug {label} -> {p}", file=sys.stderr)
            return
        if self._verbose:
            print(f"[jitcatch][{label}] {payload}", file=sys.stderr)

    def infer_risks(self, diff: str, parent_source: str, lang: str) -> List[str]:
        user = f"Language: {lang}\n\n--- DIFF ---\n{diff}\n\n--- PARENT SOURCE ---\n{parent_source}"
        out, _ = self._complete(RISKS_SYSTEM, user, label="risks")
        risks = _parse_json_array(out)
        if not risks:
            out2, _ = self._complete(RISKS_SYSTEM, user + RETRY_SUFFIX, label="risks.retry")
            risks = _parse_json_array(out2)
            if not risks:
                self._debug_dump("risks.empty", out + "\n---retry---\n" + out2)
        return risks

    def infer_risks_bundle(self, bundle: str, lang: str) -> List[str]:
        user = f"Language: {lang}\n\n--- BUNDLE ---\n{bundle}"
        out, _ = self._complete(RISKS_SYSTEM_BUNDLE, user, label="risks.bundle")
        risks = _parse_json_array(out)
        if not risks:
            out2, _ = self._complete(RISKS_SYSTEM_BUNDLE, user + RETRY_SUFFIX, label="risks.bundle.retry")
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
        system = TESTS_SYSTEM_INTENT if mode == "intent" else TESTS_SYSTEM_DODGY
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
        system = TESTS_SYSTEM_BUNDLE_INTENT if mode == "intent" else TESTS_SYSTEM_BUNDLE_DODGY
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
        out, _ = self._complete(JUDGE_SYSTEM, user, label="judge", max_tokens=MODEL_MAX_OUTPUT_TOKENS.get(
                self._model_for("judge"), DEFAULT_MODEL_MAX_OUTPUT_TOKENS
            ))
        parsed = _parse_judge(out)
        if parsed.get("_unparseable"):
            retry_out, _ = self._complete(
                JUDGE_SYSTEM, user + RETRY_SUFFIX, label="judge.retry", max_tokens=MODEL_MAX_OUTPUT_TOKENS.get(
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
    (truncation — caller should try _recover_truncated_json)."""
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
    can regex out the metadata for report grouping."""
    if isinstance(obj, (str, int, float)):
        return str(obj)
    if not isinstance(obj, dict):
        return None
    risk = obj.get("risk") or obj.get("description") or obj.get("text")
    if not risk:
        return None
    file = obj.get("file") or obj.get("path") or ""
    line = obj.get("line")
    cls = obj.get("class") or obj.get("category") or ""
    head = ""
    if file:
        head = f"[{file}:{line}]" if line is not None else f"[{file}]"
    if cls:
        head = (head + " " if head else "") + f"({cls})"
    return f"{head} {risk}".strip() if head else str(risk)


def _parse_json_array(text: str) -> List[str]:
    candidates: List[str] = [text.strip(), _strip_code_fence(text)]
    # also try to find a bare [...] in the text
    m = re.search(r"\[[\s\S]*?\]", text)
    if m:
        candidates.append(m.group(0))
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
        if isinstance(data, dict) and isinstance(data.get("risks"), list):
            out = []
            for x in data["risks"]:
                formatted = _format_risk_entry(x)
                if formatted:
                    out.append(formatted)
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

    arr = data.get("tests") if isinstance(data, dict) else data
    if not isinstance(arr, list):
        return []
    out: List[GeneratedTest] = []
    for i, t in enumerate(arr):
        if not isinstance(t, dict) or "code" not in t:
            continue
        out.append(
            GeneratedTest(
                name=str(t.get("name", f"t_{i}")),
                code=str(t["code"]),
                rationale=str(t.get("rationale", "")),
            )
        )
    return out


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
