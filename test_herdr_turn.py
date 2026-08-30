import argparse
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from herdr_turn import (
    choose_split,
    contains_new_prompt,
    parse_timeout,
    requires_manual_setup,
    submit,
    wait_for_quiet,
)


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

    def test_protects_the_caller_when_largest_panes_tie(self):
        layout = {
            "area": {"width": 200, "height": 60},
            "panes": [
                {"pane_id": "child", "rect": {"width": 100, "height": 60}},
                {"pane_id": "caller", "rect": {"width": 100, "height": 60}},
            ],
        }
        self.assertEqual(choose_split(layout, "caller"), ("child", "down"))

    def test_splits_the_caller_when_it_is_the_only_pane(self):
        layout = {
            "area": {"width": 200, "height": 60},
            "panes": [
                {"pane_id": "caller", "rect": {"width": 200, "height": 60}},
            ],
        }
        self.assertEqual(choose_split(layout, "caller"), ("caller", "right"))


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

    def test_native_wait_keeps_the_remaining_long_turn_budget(self):
        working = subprocess.CompletedProcess(
            [], 0,
            stdout='{"result":{"pane":{"agent_status":"working","revision":1}}}',
            stderr="",
        )
        done = subprocess.CompletedProcess(
            [], 0,
            stdout='{"result":{"agent":{"agent_status":"done"}}}', stderr="",
        )
        with (
            patch("herdr_turn.call", side_effect=[working, done]) as call,
            patch("herdr_turn.time.monotonic", side_effect=[100, 100, 100.5, 101]),
        ):
            self.assertEqual(
                wait_for_quiet("reviewer", "pane-1", 0, 1800000),
                ("done", "native_wait"),
            )
        call.assert_called_with("agent", "wait", "reviewer", "--timeout", "1794000")


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

    def test_submits_queued_grok_prompt_with_one_enter_then_waits(self):
        prompt = "Review this change and report actionable findings."
        results = [
            subprocess.CompletedProcess([], 0, stdout="old screen", stderr=""),
            subprocess.CompletedProcess(
                [], 1,
                stdout='{"error":{"code":"agent_prompt_stalled"}}', stderr="",
            ),
            subprocess.CompletedProcess(
                [], 0,
                stdout=(
                    '{"result":{"agent":{"agent":"grok","agent_status":"idle",'
                    '"name":"reviewer","pane_id":"pane-1","revision":0}}}'
                ), stderr="",
            ),
            subprocess.CompletedProcess([], 0, stdout=prompt, stderr=""),
            subprocess.CompletedProcess([], 0, stdout='{"result":{"type":"ok"}}', stderr=""),
        ]
        with (
            patch("herdr_turn.call", side_effect=results) as call,
            patch("herdr_turn.wait_for_quiet", return_value=("done", "native_wait")) as wait,
            patch("herdr_turn.read_once", return_value="PASS"),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(SystemExit, "0"):
                submit("reviewer", prompt, 1800000, 40, 0)

        call.assert_any_call("agent", "send-keys", "reviewer", "enter")
        self.assertEqual(
            [item for item in call.call_args_list if item.args[-1:] == ("enter",)],
            [unittest.mock.call("agent", "send-keys", "reviewer", "enter")],
        )
        wait.assert_called_once_with("reviewer", "pane-1", 0, 1800000)

    def test_does_not_press_enter_for_another_agent(self):
        prompt = "Review this change and report actionable findings."
        results = [
            subprocess.CompletedProcess([], 0, stdout="old screen", stderr=""),
            subprocess.CompletedProcess(
                [], 1,
                stdout='{"error":{"code":"agent_prompt_stalled"}}', stderr="",
            ),
            subprocess.CompletedProcess(
                [], 0,
                stdout=(
                    '{"result":{"agent":{"agent":"codex","agent_status":"idle",'
                    '"name":"reviewer","pane_id":"pane-1","revision":0}}}'
                ), stderr="",
            ),
            subprocess.CompletedProcess([], 0, stdout=prompt, stderr=""),
        ]
        with (
            patch("herdr_turn.call", side_effect=results) as call,
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaisesRegex(SystemExit, "1"):
                submit("reviewer", prompt, 1800000, 40, 0)

        self.assertNotIn(
            unittest.mock.call("agent", "send-keys", "reviewer", "enter"),
            call.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
