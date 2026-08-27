import argparse
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from herdr_turn import choose_split, contains_new_prompt, parse_timeout, requires_manual_setup, submit


class PaneLayoutTest(unittest.TestCase):
    def test_splits_the_largest_pane_to_form_a_grid(self):
        layout = {
            "area": {"width": 200, "height": 60},
            "panes": [
                {"pane_id": "caller", "rect": {"width": 100, "height": 30}},
                {"pane_id": "child-1", "rect": {"width": 100, "height": 60}},
                {"pane_id": "child-2", "rect": {"width": 100, "height": 30}},
            ],
        }
        self.assertEqual(choose_split(layout, "caller"), ("child-1", "down"))

    def test_prefers_the_caller_when_largest_panes_tie(self):
        layout = {
            "area": {"width": 200, "height": 60},
            "panes": [
                {"pane_id": "child", "rect": {"width": 100, "height": 60}},
                {"pane_id": "caller", "rect": {"width": 100, "height": 60}},
            ],
        }
        self.assertEqual(choose_split(layout, "caller"), ("caller", "down"))


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
