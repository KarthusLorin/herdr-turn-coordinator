import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from herdr_turn import (
    choose_split,
    contains_new_prompt,
    emit,
    parse_timeout,
    receipt_arg,
    receipt_snapshot,
    requires_manual_setup,
    submit,
    verify_receipt,
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
            patch(
                "herdr_turn.confirm_stable_settled",
                return_value=({"agent_status": "done"}, "stable_settled"),
            ),
        ):
            self.assertEqual(
                wait_for_quiet("reviewer", "pane-1", 0, 1800000),
                ("done", "stable_settled"),
            )
        call.assert_called_with("agent", "wait", "reviewer", "--timeout", "1794000")


class PromptDetectionTest(unittest.TestCase):
    def test_does_not_accept_transient_done_before_agent_resumes_working(self):
        prompt = "Review this change."
        results = [
            subprocess.CompletedProcess([], 0, stdout="ready", stderr=""),
            subprocess.CompletedProcess(
                [], 0,
                stdout=(
                    '{"result":{"agent":{"agent":"kimi","agent_status":"done",'
                    '"name":"reviewer","pane_id":"pane-1","revision":1}}}'
                ), stderr="",
            ),
            subprocess.CompletedProcess(
                [], 0,
                stdout=(
                    '{"result":{"agent":{"agent":"kimi","agent_status":"working",'
                    '"name":"reviewer","pane_id":"pane-1","revision":1}}}'
                ), stderr="",
            ),
            subprocess.CompletedProcess(
                [], 0,
                stdout=(
                    '{"result":{"agent":{"agent":"kimi","agent_status":"done",'
                    '"name":"reviewer","pane_id":"pane-1","revision":1}}}'
                ), stderr="",
            ),
            subprocess.CompletedProcess(
                [], 1,
                stdout='{"error":{"code":"timeout"}}', stderr="",
            ),
            subprocess.CompletedProcess([], 0, stdout="PASS", stderr=""),
        ]
        output = io.StringIO()
        with patch("herdr_turn.call", side_effect=results), redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "0"):
                submit("reviewer", prompt, 30000, 40, 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result["text"], "PASS")
        self.assertEqual(result["wait_mode"], "stable_settled")

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


class ReceiptVerificationTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.receipt = self.root / "receipt.json"

    def write(self, path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_accepts_a_fresh_completed_receipt_with_no_artifacts(self):
        started = time.time_ns()
        self.write(self.receipt, {"status": "completed", "artifacts": []})
        report = verify_receipt(self.receipt, None, started)
        self.assertTrue(report["accepted"])
        self.assertTrue(report["parsable"])

    def test_reports_a_missing_receipt_rather_than_accepting_a_quiet_pane(self):
        report = verify_receipt(self.receipt, None, time.time_ns())
        self.assertFalse(report["accepted"])
        self.assertFalse(report["present"])
        self.assertEqual(report["problem"], "missing")

    def test_rejects_a_receipt_left_over_from_an_earlier_attempt(self):
        self.write(self.receipt, {"status": "completed", "artifacts": []})
        baseline = receipt_snapshot(self.receipt)
        started = time.time_ns()
        report = verify_receipt(self.receipt, baseline, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "stale")

    def test_rejects_an_unparsable_receipt(self):
        started = time.time_ns()
        self.receipt.write_text("{not json", encoding="utf-8")
        report = verify_receipt(self.receipt, None, started)
        self.assertTrue(report["present"])
        self.assertFalse(report["parsable"])
        self.assertEqual(report["problem"], "unparsable")

    def test_rejects_a_completed_receipt_naming_an_absent_artifact(self):
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "completed",
            "artifacts": [str(self.root / "never-written.md")],
        })
        report = verify_receipt(self.receipt, None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "artifact_unverified")

    def test_rejects_an_artifact_that_predates_this_turn(self):
        stale_artifact = self.root / "old.md"
        stale_artifact.write_text("written earlier", encoding="utf-8")
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "completed",
            "artifacts": [str(stale_artifact)],
        })
        report = verify_receipt(self.receipt, None, started)
        self.assertFalse(report["accepted"])
        self.assertFalse(report["artifacts"][0]["fresh"])
        self.assertEqual(report["problem"], "artifact_unverified")

    def test_keeps_a_partial_receipt_out_of_acceptance_but_reports_remaining(self):
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "partial",
            "artifacts": [],
            "remaining": ["section 3"],
        })
        report = verify_receipt(self.receipt, None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "not_completed")
        self.assertEqual(report["remaining"], ["section 3"])

    def test_requires_an_absolute_receipt_path(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            receipt_arg("relative/receipt.json")
        self.assertEqual(receipt_arg("/tmp/r.json"), Path("/tmp/r.json"))


class ReceiptEmitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.receipt = Path(self.dir.name) / "receipt.json"

    def emit_once(self, status, receipt, started_ns=None):
        buffer = io.StringIO()
        started = time.time_ns() if started_ns is None else started_ns
        with self.assertRaises(SystemExit) as exit_info, redirect_stdout(buffer):
            emit(status, "p1", "worker", "native_wait", "text",
                 receipt, None, started)
        return json.loads(buffer.getvalue()), exit_info.exception.code

    def test_omits_the_receipt_field_when_the_caller_did_not_ask_for_one(self):
        payload, code = self.emit_once("idle", None)
        self.assertNotIn("receipt", payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(code, 0)

    def test_a_settled_pane_without_its_receipt_is_not_ok(self):
        payload, code = self.emit_once("idle", self.receipt)
        self.assertTrue(payload["agent_status"] == "idle")
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["receipt"]["problem"], "missing")
        self.assertEqual(code, 2)

    def test_a_settled_pane_with_a_verified_receipt_is_ok(self):
        # The turn starts, then the worker writes its receipt: the order the
        # wrapper actually sees.
        started = time.time_ns()
        self.receipt.write_text(
            json.dumps({"status": "completed", "artifacts": []}), encoding="utf-8"
        )
        payload, code = self.emit_once("done", self.receipt, started)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["receipt"]["accepted"])
        self.assertEqual(code, 0)
