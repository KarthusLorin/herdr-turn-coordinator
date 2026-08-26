import argparse
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from herdr_turn import contains_new_prompt, parse_timeout, requires_manual_setup, submit


class TimeoutParsingTest(unittest.TestCase):
    def test_help_names_the_timeout_unit(self):
        result = subprocess.run(
            [sys.executable, "herdr_turn.py", "run", "--help"],
            text=True, capture_output=True, check=True,
        )
        self.assertIn("bare values are milliseconds", result.stdout)

    def test_accepts_explicit_units(self):
        self.assertEqual(parse_timeout("600ms"), 600)
        self.assertEqual(parse_timeout("600s"), 600000)
        self.assertEqual(parse_timeout("10m"), 600000)

    def test_preserves_unambiguous_legacy_milliseconds(self):
        self.assertEqual(parse_timeout("600000"), 600000)

    def test_rejects_ambiguous_small_bare_value(self):
        with self.assertRaisesRegex(argparse.ArgumentTypeError, "600s"):
            parse_timeout("600")


class PromptDetectionTest(unittest.TestCase):
    def test_wrapped_box_prompt(self):
        prompt = "Review this change and report only actionable findings."
        after = "│ Review this change and report only │\n│ actionable findings.               │"
        self.assertTrue(contains_new_prompt("", after, prompt))

    def test_uses_a_new_later_chunk(self):
        prompt = "Review this change and report only actionable findings."
        before = "Review this change and"
        after = before + " report only actionable findings."
        self.assertTrue(contains_new_prompt(before, after, prompt))

    def test_rejects_unrelated_redraw(self):
        self.assertFalse(contains_new_prompt("old screen", "new unrelated screen", "Review the diff carefully."))

    def test_detects_kimi_trust_prompt(self):
        screen = "Trust this folder?\nEnable project MCP servers.\nDon't trust"
        self.assertTrue(requires_manual_setup(screen))

    def test_does_not_prompt_through_kimi_trust_screen(self):
        screen = "Trust this folder?\nEnable project MCP servers.\nDon't trust"
        result = subprocess.CompletedProcess([], 0, stdout=screen, stderr="")
        with patch("herdr_turn.call", return_value=result) as call, redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                submit("kimi", "must not send", 30000, 40, 0)
        self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
