"""
Regression tests for modules.util.parse_llm_json.

Background (2026-09-05): a "resume_qc:validate" and an "authenticity" call
both returned a response that reasoned through a checklist in prose first,
then put a valid JSON object in a ```json fence later in the text.
parse_llm_json's fence-stripping was anchored to the very start/end of the
string, so it never found that fence and returned parse_error even though a
perfectly valid answer was sitting in the response. This blocked resume
generation on a false positive, not a real content problem.

The fix extracts fenced ```json blocks by their fence delimiters (never by
brace-counting -- `(\\{.*?\\})`-style regexes break on nested objects/arrays,
and on any brace characters that happen to appear inside a string value) and
accepts a result only when exactly one candidate parses -- ambiguity between
multiple valid fenced blocks still fails closed, same as no JSON at all.

No API calls -- pure string/JSON parsing.

Run: python3 -m unittest discover -s tests -v
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modules.util import parse_llm_json


class TestParseLlmJsonPreambleThenFence(unittest.TestCase):
    """The exact failure shape: reasoning prose, then a fenced JSON object."""

    def test_preamble_before_fence_is_recovered(self):
        text = (
            "I'll check each item against the profile before answering.\n\n"
            "**M2/M3 check:** already separate, good.\n"
            "**Em dash check:** none found.\n\n"
            "Here is my answer:\n\n"
            '```json\n{"issues": ["a", "b"]}\n```'
        )
        self.assertEqual(parse_llm_json(text), {"issues": ["a", "b"]})

    def test_trailing_text_after_fence_is_also_fine(self):
        text = '```json\n{"issues": []}\n```\n\nLet me know if you need more detail.'
        self.assertEqual(parse_llm_json(text), {"issues": []})


class TestParseLlmJsonNestedStructures(unittest.TestCase):
    """Fence-delimited extraction must not break on nested braces/brackets,
    including brace characters that appear inside a string value -- exactly
    what a `(\\{.*?\\})` lazy brace-matching regex would truncate on."""

    def test_nested_objects_and_arrays_survive(self):
        payload = {
            "summary": None,
            "experience": [
                {"title": "Engineer", "company": "Acme",
                 "bullets": ["Shipped the widget.", "Cut latency by 12%."]},
                {"title": "Manager", "company": "Beta", "bullets": ["Ran the rollout."]},
            ],
            "skills": ["SQL", "Jira"],
        }
        text = "Reasoning about the draft first.\n\n```json\n" + json.dumps(payload) + "\n```"
        self.assertEqual(parse_llm_json(text), payload)

    def test_brace_characters_inside_a_string_value_do_not_confuse_extraction(self):
        payload = {
            "issues": [
                'bullet describes a config object like {"key": "value"} in prose'
            ]
        }
        text = (
            "Checking the resume for problems.\n\n"
            "```json\n" + json.dumps(payload) + "\n```"
        )
        self.assertEqual(parse_llm_json(text), payload)


class TestParseLlmJsonMalformedFence(unittest.TestCase):
    def test_malformed_json_inside_a_fence_fails_closed(self):
        text = (
            "Here is the result.\n\n"
            '```json\n{"issues": ["unterminated string]\n```'
        )
        result = parse_llm_json(text)
        self.assertIn("parse_error", result)


class TestParseLlmJsonAmbiguousMultipleFences(unittest.TestCase):
    def test_two_valid_fenced_blocks_is_ambiguous_and_fails_closed(self):
        text = (
            "Here is a first draft:\n\n"
            '```json\n{"issues": ["a"]}\n```\n\n'
            "On reflection, here is a revised version instead:\n\n"
            '```json\n{"issues": ["b"]}\n```'
        )
        result = parse_llm_json(text)
        self.assertIn("parse_error", result)

    def test_one_valid_and_one_malformed_fence_is_not_ambiguous(self):
        # Only one candidate actually parses, so it is unambiguous and used.
        text = (
            '```json\n{not: valid\n```\n\n'
            'Actually:\n\n```json\n{"issues": ["a"]}\n```'
        )
        self.assertEqual(parse_llm_json(text), {"issues": ["a"]})


class TestParseLlmJsonNoJsonPresent(unittest.TestCase):
    def test_prose_only_response_fails_closed(self):
        text = "I could not find any issues worth flagging in this resume."
        result = parse_llm_json(text)
        self.assertIn("parse_error", result)

    def test_empty_string_fails_closed(self):
        result = parse_llm_json("")
        self.assertIn("parse_error", result)


class TestParseLlmJsonExistingBehaviorPreserved(unittest.TestCase):
    """Plain JSON and a fence anchored at the true start/end of the response
    -- today's two supported shapes -- must keep working unchanged."""

    def test_plain_json_no_fence(self):
        self.assertEqual(parse_llm_json('{"issues": []}'), {"issues": []})

    def test_fence_anchored_at_start_and_end(self):
        text = '```json\n{"issues": []}\n```'
        self.assertEqual(parse_llm_json(text), {"issues": []})

    def test_bare_fence_without_json_language_tag(self):
        text = '```\n{"issues": []}\n```'
        self.assertEqual(parse_llm_json(text), {"issues": []})

    def test_whitespace_around_plain_json_is_tolerated(self):
        self.assertEqual(parse_llm_json('  \n{"issues": []}\n  '), {"issues": []})


if __name__ == "__main__":
    unittest.main()
