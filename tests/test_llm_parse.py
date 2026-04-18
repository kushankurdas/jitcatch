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
        # First test completed — salvage returns at least that one.
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


if __name__ == "__main__":
    unittest.main()
