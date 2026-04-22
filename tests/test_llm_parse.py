from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jitcatch import llm  # noqa: E402


STRICT = '{"tests":[{"name":"a","code":"x=1","rationale":"r"}]}'

FENCED_WITH_PROSE = (
    "Looking at the diff, the mutation changes Y.\n\n"
    "```json\n"
    '{"tests":[{"name":"n","code":"assert 1==1","rationale":"r"}]}\n'
    "```\n"
    "Hope this helps!\n"
)

TRUNCATED_MID_SECOND_TEST = (
    '{"tests":[\n'
    '  {"name":"first","code":"const a = 1;","rationale":"ok"},\n'
    '  {"name":"second","code":"const b'  # chopped mid-string
)

TRUNCATED_AFTER_FIRST_COMPLETE = (
    '{"tests":[\n'
    '  {"name":"first","code":"const a = 1;","rationale":"ok"}\n'
    # missing trailing ]}
)

PROSE_NO_FENCE = (
    "Here are tests:\n"
    '{"tests":[{"name":"p","code":"x","rationale":"r"}]} That\'s it.'
)


class StripCodeFenceTest(unittest.TestCase):
    def test_strips_fenced_json(self) -> None:
        out = llm._strip_code_fence(FENCED_WITH_PROSE)
        self.assertTrue(out.startswith("{"))
        self.assertIn('"tests"', out)

    def test_non_fenced_returns_stripped(self) -> None:
        self.assertEqual(llm._strip_code_fence(STRICT), STRICT)

    def test_tolerates_missing_close_fence(self) -> None:
        bad = "```json\n" + STRICT  # no closing ```
        out = llm._strip_code_fence(bad)
        self.assertIn('"tests"', out)


class ParseTestsTest(unittest.TestCase):
    def test_strict(self) -> None:
        tests = llm._parse_tests(STRICT)
        self.assertEqual(len(tests), 1)
        self.assertEqual(tests[0].name, "a")

    def test_fenced_with_prose(self) -> None:
        tests = llm._parse_tests(FENCED_WITH_PROSE)
        self.assertEqual(len(tests), 1)

    def test_prose_no_fence(self) -> None:
        tests = llm._parse_tests(PROSE_NO_FENCE)
        self.assertEqual(len(tests), 1)

    def test_recovers_truncated_after_first(self) -> None:
        tests = llm._parse_tests(TRUNCATED_AFTER_FIRST_COMPLETE)
        self.assertEqual(len(tests), 1)
        self.assertEqual(tests[0].name, "first")

    def test_recovers_when_second_truncated_mid_body(self) -> None:
        tests = llm._parse_tests(TRUNCATED_MID_SECOND_TEST)
        # First test completed. Salvage returns at least that one.
        self.assertGreaterEqual(len(tests), 1)
        self.assertEqual(tests[0].name, "first")

    def test_empty_on_garbage(self) -> None:
        self.assertEqual(llm._parse_tests("no json at all here"), [])


class RecoverTruncatedTest(unittest.TestCase):
    def test_returns_none_when_no_tests_key(self) -> None:
        self.assertIsNone(llm._recover_truncated_tests_json('{"other":[1,2]}'))

    def test_returns_completed_entries(self) -> None:
        salvaged = llm._recover_truncated_tests_json(TRUNCATED_MID_SECOND_TEST)
        self.assertIsNotNone(salvaged)
        self.assertIn('"first"', salvaged)


class ParseRisksTaxonomyTest(unittest.TestCase):
    def test_legacy_string_array(self) -> None:
        out = llm._parse_json_array('["risk A", "risk B"]')
        self.assertEqual(out, ["risk A", "risk B"])

    def test_structured_object_array(self) -> None:
        raw = (
            '[{"file":"a.js","line":12,"class":"security","risk":"auth bypass"},'
            '{"file":"b.js","line":null,"class":"arithmetic","risk":"off-by-one"}]'
        )
        out = llm._parse_json_array(raw)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], "[a.js:12] (security) auth bypass")
        self.assertEqual(out[1], "[b.js] (arithmetic) off-by-one")

    def test_mixed_forms_tolerated(self) -> None:
        raw = '["plain string", {"file":"c.js","line":3,"class":"validation","risk":"loose regex"}]'
        out = llm._parse_json_array(raw)
        self.assertEqual(out[0], "plain string")
        self.assertEqual(out[1], "[c.js:3] (validation) loose regex")

    def test_object_without_file_renders_bare_risk(self) -> None:
        out = llm._parse_json_array('[{"risk":"something bad"}]')
        self.assertEqual(out, ["something bad"])

    def test_format_risk_entry_rejects_unknowns(self) -> None:
        self.assertIsNone(llm._format_risk_entry(None))
        self.assertIsNone(llm._format_risk_entry({"no_risk_field": True}))


class ParseJudgeTest(unittest.TestCase):
    def test_strict(self) -> None:
        out = llm._parse_judge('{"tp_prob":0.9,"bucket":"High","rationale":"r"}')
        self.assertEqual(out["bucket"], "High")
        self.assertFalse(out.get("_unparseable"))

    def test_prose_preamble(self) -> None:
        text = 'I think: {"tp_prob":0.3,"bucket":"Low","rationale":"meh"}'
        out = llm._parse_judge(text)
        self.assertEqual(out["bucket"], "Low")

    def test_unparseable(self) -> None:
        out = llm._parse_judge("not json")
        self.assertTrue(out.get("_unparseable"))


class SmallModelClassifierTest(unittest.TestCase):
    """Guards the compact-prompt selection. Large/paid models MUST NOT
    match. That guarantees the OpenAICompatClient._system_for_label
    override returns the default (full) prompt for them."""

    def test_paid_anthropic_models_not_small(self) -> None:
        for m in ("claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"):
            self.assertFalse(llm._is_small_model(m), m)

    def test_large_local_models_not_small(self) -> None:
        for m in ("gemma4:26b", "gemma4:31b", "qwen2.5-coder:14b",
                  "qwen2.5-coder:32b", "llama3.1:70b"):
            self.assertFalse(llm._is_small_model(m), m)

    def test_small_local_models_match(self) -> None:
        for m in ("gemma4:e2b", "gemma4:e4b", "qwen2.5-coder:7b",
                  "llama3.2:3b", "deepseek-coder-v2:16b", "phi3:mini"):
            self.assertTrue(llm._is_small_model(m), m)

    def test_quantization_suffixes_still_match(self) -> None:
        # Ollama appends quantization tags like `-instruct-q4_K_M`. Prefix
        # matching keeps classification stable across those variants.
        self.assertTrue(llm._is_small_model("gemma4:e4b-instruct-q4_K_M"))
        self.assertTrue(llm._is_small_model("qwen2.5-coder:7b-instruct"))


class TolerantRiskParsingTest(unittest.TestCase):
    """Small models produce non-canonical wrapper keys and field names.
    These guards lock in the tolerant behavior so paid-model outputs
    (which use the canonical `risks`/`risk` shape) still parse via the
    canonical path first."""

    def test_wrapper_key_changes_accepted(self) -> None:
        raw = '{"changes":[{"file":"r.js","line":1,"class":"contract","risk":"DELETE->GET"}]}'
        out = llm._parse_json_array(raw)
        self.assertEqual(len(out), 1)
        self.assertIn("DELETE->GET", out[0])

    def test_wrapper_key_issues_accepted(self) -> None:
        raw = '{"issues":[{"file":"a.js","risk":"loose regex"}]}'
        self.assertEqual(llm._parse_json_array(raw), ["[a.js] loose regex"])

    def test_field_change_aliased_to_risk(self) -> None:
        # deepseek-coder style output: `change` key instead of `risk`.
        raw = '[{"file":"config.js","change":"CORS origin flipped to *"}]'
        out = llm._parse_json_array(raw)
        self.assertEqual(out, ["[config.js] CORS origin flipped to *"])

    def test_canonical_still_wins(self) -> None:
        # Canonical `risks` key takes precedence over aliases. Paid
        # model output must hit the fast path.
        raw = '{"risks":[{"risk":"canonical"}],"changes":[{"change":"alias"}]}'
        out = llm._parse_json_array(raw)
        self.assertEqual(out, ["canonical"])


class TolerantTestParsingTest(unittest.TestCase):
    def test_alternate_code_key(self) -> None:
        raw = '{"tests":[{"name":"t","test_code":"expect(x).toBe(1);","rationale":"r"}]}'
        tests = llm._parse_tests(raw)
        self.assertEqual(len(tests), 1)
        self.assertIn("expect(x)", tests[0].code)

    def test_wrapper_key_cases(self) -> None:
        raw = '{"cases":[{"name":"c","code":"x","rationale":"r"}]}'
        tests = llm._parse_tests(raw)
        self.assertEqual(len(tests), 1)

    def test_name_fallback_to_title(self) -> None:
        raw = '{"tests":[{"title":"t1","code":"x"}]}'
        tests = llm._parse_tests(raw)
        self.assertEqual(tests[0].name, "t1")


class TolerantFindingsParsingTest(unittest.TestCase):
    def test_issues_wrapper_accepted(self) -> None:
        raw = (
            '{"issues":[{"file":"r.js","line":49,"title":"method swap",'
            '"rationale":"DELETE -> GET","severity":"High",'
            '"category":"contract","confidence":0.9}]}'
        )
        findings = llm._parse_findings(raw)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "High")

    def test_reason_alias_for_rationale(self) -> None:
        raw = '{"findings":[{"title":"x","reason":"because"}]}'
        findings = llm._parse_findings(raw)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].rationale, "because")


class SystemPromptHookTest(unittest.TestCase):
    """Verifies the _system_for_label hook's contract: paid/large models
    always receive the default (full) prompt; small local models served
    over OpenAI-compat receive the compact variant for bundle labels
    and the default for labels that don't have a compact variant."""

    def _mk_compat(self, model: str) -> llm.OpenAICompatClient:
        c = llm.OpenAICompatClient.__new__(llm.OpenAICompatClient)
        c._model = model
        c._stage_models = {}
        return c

    def test_anthropic_always_passthrough(self) -> None:
        # Build a bare AnthropicClient-shaped object just for the hook.
        c = llm.AnthropicClient.__new__(llm.AnthropicClient)
        c._model = "claude-sonnet-4-6"
        c._stage_models = {}
        for label, default in (
            ("risks.bundle", llm.RISKS_SYSTEM_BUNDLE),
            ("tests.bundle.intent", llm.TESTS_SYSTEM_BUNDLE_INTENT),
            ("review", llm.REVIEWER_SYSTEM),
        ):
            self.assertIs(c._system_for_label(label, default), default)

    def test_compat_large_model_passthrough(self) -> None:
        c = self._mk_compat("qwen2.5-coder:32b")
        self.assertIs(
            c._system_for_label("risks.bundle", llm.RISKS_SYSTEM_BUNDLE),
            llm.RISKS_SYSTEM_BUNDLE,
        )

    def test_compat_small_model_compact(self) -> None:
        c = self._mk_compat("gemma4:e4b")
        picked = c._system_for_label("risks.bundle", llm.RISKS_SYSTEM_BUNDLE)
        self.assertIs(picked, llm.RISKS_SYSTEM_BUNDLE_COMPACT)

    def test_retry_label_maps_to_same_compact(self) -> None:
        c = self._mk_compat("gemma4:e4b")
        picked = c._system_for_label("risks.bundle.retry", llm.RISKS_SYSTEM_BUNDLE)
        self.assertIs(picked, llm.RISKS_SYSTEM_BUNDLE_COMPACT)

    def test_compact_only_defined_for_bundle_paths(self) -> None:
        # Non-bundle single-file path has no compact variant. Falls
        # back to the full system prompt even on small models.
        c = self._mk_compat("gemma4:e4b")
        self.assertIs(
            c._system_for_label("risks", llm.RISKS_SYSTEM),
            llm.RISKS_SYSTEM,
        )


if __name__ == "__main__":
    unittest.main()
