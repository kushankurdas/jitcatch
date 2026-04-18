from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from .config import GeneratedTest


RISKS_SYSTEM = (
    "You are a senior software engineer reviewing a code change. "
    "Given a diff, identify risks — concrete ways the change could introduce a bug. "
    "Return a JSON array of short risk descriptions. No prose outside the JSON."
)

TESTS_SYSTEM_INTENT = (
    "You are a test-generation engine. Given source code at the PARENT revision and a diff, "
    "generate unit tests that (a) pass on the parent, and (b) exercise the risks listed. "
    "A test that passes on the parent but fails on the child indicates the diff introduced a "
    "regression. Return strict JSON: {\"tests\":[{\"name\":str,\"code\":str,\"rationale\":str}]}. "
    "Keep tests hermetic and self-contained."
)

TESTS_SYSTEM_DODGY = (
    "You are a test-generation engine. Treat the provided diff as a suspected bug mutation of "
    "the parent. Generate unit tests against the PARENT code that assert its observable behavior "
    "precisely enough that the mutated version would fail. Return strict JSON: "
    "{\"tests\":[{\"name\":str,\"code\":str,\"rationale\":str}]}."
)

JUDGE_SYSTEM = (
    "You classify whether a failing test reveals a true bug in a code diff. "
    "Return strict JSON: {\"tp_prob\":float in [-1,1], \"bucket\":\"High\"|\"Medium\"|\"Low\", "
    "\"rationale\":str}. tp_prob=1 means certainly a real bug; -1 means certainly a false positive."
)


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


class AnthropicClient(LLMClient):
    def __init__(self, model: str = "claude-sonnet-4-6", max_tokens: int = 2048) -> None:
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError("anthropic SDK not installed; `pip install anthropic`") from e
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=key)
        self._model = model
        self._max_tokens = max_tokens

    def _complete(self, system: str, user: str) -> str:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        parts = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        return "".join(parts)

    def infer_risks(self, diff: str, parent_source: str, lang: str) -> List[str]:
        user = f"Language: {lang}\n\n--- DIFF ---\n{diff}\n\n--- PARENT SOURCE ---\n{parent_source}"
        out = self._complete(RISKS_SYSTEM, user)
        return _parse_json_array(out)

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
        out = self._complete(system, user)
        return _parse_tests(out)

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
        out = self._complete(JUDGE_SYSTEM, user)
        return _parse_judge(out)


class StubClient(LLMClient):
    """Reads canned responses from `.jitcatch_stub.json` at repo root.

    Schema:
    {
      "risks": ["..."],
      "intent_tests": [{"name":"...","code":"...","rationale":"..."}],
      "dodgy_tests":  [{"name":"...","code":"...","rationale":"..."}],
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
        return [
            GeneratedTest(
                name=t.get("name", f"stub_{i}"),
                code=t["code"],
                rationale=t.get("rationale", ""),
            )
            for i, t in enumerate(raw)
            if "code" in t
        ]

    def judge(self, test_code, parent_source, diff, failure, lang) -> dict:
        d = self._data.get("judge") or {}
        return {
            "tp_prob": float(d.get("tp_prob", 0.0)),
            "bucket": d.get("bucket", "Low"),
            "rationale": d.get("rationale", "stub"),
        }


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n(.*?)\n```$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _parse_json_array(text: str) -> List[str]:
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [str(x) for x in data if isinstance(x, (str, int, float))]
    if isinstance(data, dict) and isinstance(data.get("risks"), list):
        return [str(x) for x in data["risks"]]
    return []


def _parse_tests(text: str) -> List[GeneratedTest]:
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError:
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
    try:
        data = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError:
        return {"tp_prob": 0.0, "bucket": "Low", "rationale": "unparseable judge output"}
    return {
        "tp_prob": float(data.get("tp_prob", 0.0)),
        "bucket": str(data.get("bucket", "Low")),
        "rationale": str(data.get("rationale", "")),
    }
