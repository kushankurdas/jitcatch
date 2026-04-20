from __future__ import annotations

import argparse
import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx

from jitcatch import cli, llm


def _ns(**overrides) -> argparse.Namespace:
    """Build the minimal argparse.Namespace that _make_llm inspects."""
    ns = argparse.Namespace(
        stub=False,
        provider="auto",
        base_url=None,
        model=None,
        model_risks=None,
        model_tests=None,
        model_judge=None,
        model_review=None,
        max_tokens=None,
        verbose=False,
        log_dir=None,
        llm_timeout=120.0,
    )
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class ResolveProviderTest(unittest.TestCase):
    def test_explicit_provider_passed_through(self) -> None:
        self.assertEqual(cli._resolve_provider("anthropic"), "anthropic")
        self.assertEqual(cli._resolve_provider("ollama"), "ollama")
        self.assertEqual(cli._resolve_provider("openai-compat"), "openai-compat")

    def test_auto_picks_anthropic_when_key_set(self) -> None:
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            self.assertEqual(cli._resolve_provider("auto"), "anthropic")

    def test_auto_falls_back_to_ollama_when_no_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(cli._resolve_provider("auto"), "ollama")


class MakeLlmDispatchTest(unittest.TestCase):
    def test_stub_short_circuits(self) -> None:
        client = cli._make_llm(_ns(stub=True), Path("/tmp"))
        self.assertIsInstance(client, llm.StubClient)

    def test_auto_anthropic_uses_claude_default_model(self) -> None:
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=False):
            with mock.patch.object(cli, "AnthropicClient") as mock_cls:
                mock_cls.return_value = mock.sentinel.anthropic
                got = cli._make_llm(_ns(), Path("/tmp"))
        self.assertIs(got, mock.sentinel.anthropic)
        kwargs = mock_cls.call_args.kwargs
        self.assertEqual(kwargs["model"], "claude-sonnet-4-6")
        self.assertEqual(kwargs["stage_models"]["risks"], "claude-sonnet-4-6")

    def test_auto_ollama_defaults_when_no_api_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "OLLAMA_BASE_URL", "OPENAI_API_KEY")}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "OpenAICompatClient") as mock_cls:
                mock_cls.return_value = mock.sentinel.ollama
                got = cli._make_llm(_ns(), Path("/tmp"))
        self.assertIs(got, mock.sentinel.ollama)
        kwargs = mock_cls.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "http://localhost:11434/v1")
        self.assertEqual(kwargs["model"], "qwen2.5-coder:7b")
        self.assertIsNone(kwargs["api_key"])

    def test_explicit_ollama_respects_ollama_base_url_env(self) -> None:
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("ANTHROPIC_API_KEY", "OLLAMA_BASE_URL", "OPENAI_API_KEY")
        }
        env["OLLAMA_BASE_URL"] = "http://10.0.0.5:11434/v1"
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "OpenAICompatClient") as mock_cls:
                cli._make_llm(_ns(provider="ollama"), Path("/tmp"))
        self.assertEqual(
            mock_cls.call_args.kwargs["base_url"],
            "http://10.0.0.5:11434/v1",
        )

    def test_openai_compat_requires_base_url(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--base-url"):
            cli._make_llm(_ns(provider="openai-compat"), Path("/tmp"))

    def test_openai_compat_with_explicit_base_url_and_key(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "OPENAI_API_KEY"}
        env["OPENAI_API_KEY"] = "sk-xxx"
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "OpenAICompatClient") as mock_cls:
                cli._make_llm(
                    _ns(
                        provider="openai-compat",
                        base_url="https://api.groq.com/openai/v1",
                        model="llama3.1:70b",
                    ),
                    Path("/tmp"),
                )
        kwargs = mock_cls.call_args.kwargs
        self.assertEqual(kwargs["base_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(kwargs["api_key"], "sk-xxx")
        self.assertEqual(kwargs["model"], "llama3.1:70b")

    def test_explicit_model_wins_over_provider_default(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(cli, "OpenAICompatClient") as mock_cls:
                cli._make_llm(_ns(model="deepseek-r1:14b"), Path("/tmp"))
        self.assertEqual(mock_cls.call_args.kwargs["model"], "deepseek-r1:14b")


def _make_mock_compat_client(transport: httpx.MockTransport) -> llm.OpenAICompatClient:
    """Build an OpenAICompatClient wired to a mock transport. Sidesteps
    __init__ (which would open a real httpx.Client) so tests never touch
    the network."""
    c = llm.OpenAICompatClient.__new__(llm.OpenAICompatClient)
    c._http = httpx.Client(transport=transport, timeout=10.0)
    c._base_url = "http://localhost:11434/v1"
    c._api_key = None
    c._timeout = 10.0
    c._model = "qwen2.5-coder:7b"
    c._stage_models = {}
    c._max_tokens = None
    c._verbose = False
    c._log_dir = None
    c._call_seq = 0
    c.total_calls = 0
    c.truncated_calls = 0
    return c


class OpenAICompatCompleteTest(unittest.TestCase):
    def test_posts_chat_completions_with_system_and_user(self) -> None:
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["path"] = req.url.path
            body = req.read()
            captured["body"] = body
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "[]"}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                },
            )

        c = _make_mock_compat_client(httpx.MockTransport(handler))
        text, meta = c._complete("sys", "user", label="risks")

        self.assertEqual(text, "[]")
        self.assertEqual(meta.stop_reason, "stop")
        self.assertEqual(meta.input_tokens, 5)
        self.assertEqual(meta.output_tokens, 2)
        self.assertEqual(c.total_calls, 1)
        self.assertEqual(captured["path"], "/v1/chat/completions")
        # Body contains both roles and the model.
        body_text = captured["body"].decode()
        self.assertIn('"role":"system"', body_text)
        self.assertIn('"role":"user"', body_text)
        self.assertIn("qwen2.5-coder:7b", body_text)

    def test_maps_finish_reason_length_to_max_tokens(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": "x"}, "finish_reason": "length"}
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        ))
        c = _make_mock_compat_client(transport)
        _, meta = c._complete("sys", "user", label="tests")
        self.assertEqual(meta.stop_reason, "max_tokens")
        self.assertEqual(c.truncated_calls, 1)

    def test_http_error_becomes_runtime_error_with_endpoint(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(500, text="boom"))
        c = _make_mock_compat_client(transport)
        with self.assertRaises(RuntimeError) as ctx:
            c._complete("sys", "user", label="tests")
        self.assertIn("OpenAI-compat call failed", str(ctx.exception))
        self.assertIn("localhost:11434", str(ctx.exception))

    def test_empty_choices_raises(self) -> None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"choices": []}))
        c = _make_mock_compat_client(transport)
        with self.assertRaisesRegex(RuntimeError, "no choices"):
            c._complete("sys", "user", label="tests")

    def test_authorization_header_sent_when_api_key_set(self) -> None:
        captured: dict = {}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["auth"] = req.headers.get("authorization")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": ""}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                },
            )

        c = _make_mock_compat_client(httpx.MockTransport(handler))
        c._api_key = "sk-secret"
        c._complete("sys", "user", label="tests")
        self.assertEqual(captured["auth"], "Bearer sk-secret")


if __name__ == "__main__":
    unittest.main()
