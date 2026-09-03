import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from herdr_turn import (
    ReceiptPlan,
    choose_split,
    clear_startup_gates,
    contains_new_prompt,
    emit,
    gate_blocking_reason,
    match_startup_gate,
    parse_timeout,
    receipt_arg,
    receipt_snapshot,
    requires_manual_setup,
    startup_agent_args,
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

    def verify(self, baseline, started, expected=()):
        return verify_receipt(
            self.receipt, baseline, {str(item) for item in expected}, started
        )

    def test_accepts_a_fresh_completed_receipt_with_no_artifacts(self):
        started = time.time_ns()
        self.write(self.receipt, {"status": "completed", "artifacts": [], "reason": "done"})
        report = self.verify(None, started)
        self.assertTrue(report["accepted"])
        self.assertTrue(report["parsable"])

    def test_reports_a_missing_receipt_rather_than_accepting_a_quiet_pane(self):
        report = self.verify(None, time.time_ns())
        self.assertFalse(report["accepted"])
        self.assertFalse(report["present"])
        self.assertEqual(report["problem"], "missing")

    def test_rejects_a_receipt_left_over_from_an_earlier_attempt(self):
        self.write(self.receipt, {"status": "completed", "artifacts": [], "reason": "done"})
        baseline = receipt_snapshot(self.receipt)
        started = time.time_ns()
        report = self.verify(baseline, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "stale")

    def test_rejects_an_unparsable_receipt(self):
        started = time.time_ns()
        self.receipt.write_text("{not json", encoding="utf-8")
        report = self.verify(None, started)
        self.assertTrue(report["present"])
        self.assertFalse(report["parsable"])
        self.assertEqual(report["problem"], "unparsable")

    def test_rejects_a_completed_receipt_naming_an_absent_artifact(self):
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "completed",
            "artifacts": [str(self.root / "never-written.md")],
            "reason": "done",
        })
        report = self.verify(None, started, [self.root / "never-written.md"])
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "artifact_unverified")

    def test_rejects_an_artifact_that_predates_this_turn(self):
        stale_artifact = self.root / "old.md"
        stale_artifact.write_text("written earlier", encoding="utf-8")
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "completed",
            "artifacts": [str(stale_artifact)],
            "reason": "done",
        })
        report = self.verify(None, started, [stale_artifact])
        self.assertFalse(report["accepted"])
        self.assertFalse(report["artifacts"][0]["fresh"])
        self.assertEqual(report["problem"], "artifact_unverified")

    def test_keeps_a_partial_receipt_out_of_acceptance_but_reports_remaining(self):
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "partial",
            "artifacts": [],
            "remaining": ["section 3"],
            "reason": "ran out of budget",
        })
        report = self.verify(None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "not_completed")
        self.assertEqual(report["remaining"], ["section 3"])

    def test_rejects_an_expected_artifact_the_worker_never_reported(self):
        started = time.time_ns()
        owed = self.root / "report.md"
        self.write(self.receipt, {"status": "completed", "artifacts": [], "reason": "done"})
        report = self.verify(None, started, [owed])
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "artifact_unverified")
        self.assertEqual(report["missing_expected"], [os.path.realpath(owed)])

    def test_accepts_an_expected_artifact_written_this_turn(self):
        started = time.time_ns()
        owed = self.root / "report.md"
        owed.write_text("findings", encoding="utf-8")
        self.write(self.receipt, {"status": "completed", "artifacts": [str(owed)], "reason": "done"})
        report = self.verify(None, started, [owed])
        self.assertTrue(report["accepted"])
        self.assertEqual(report["missing_expected"], [])

    def test_matches_an_expected_artifact_through_a_symlinked_prefix(self):
        started = time.time_ns()
        owed = self.root / "report.md"
        owed.write_text("findings", encoding="utf-8")
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        self.write(self.receipt, {
            "status": "completed", "artifacts": [str(link / "report.md")], "reason": "done",
        })
        report = self.verify(None, started, [owed])
        self.assertTrue(report["accepted"])

    def test_rejects_a_completed_receipt_that_still_lists_remaining_work(self):
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "completed", "artifacts": [], "remaining": ["section 3"], "reason": "done",
        })
        report = self.verify(None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "inconsistent")

    def test_rejects_a_stopped_receipt_that_gives_no_reason(self):
        started = time.time_ns()
        self.write(self.receipt, {"status": "blocked", "artifacts": []})
        report = self.verify(None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "inconsistent")

    def test_rejects_a_completed_receipt_that_gives_no_reason(self):
        started = time.time_ns()
        self.write(self.receipt, {"status": "completed", "artifacts": []})
        report = self.verify(None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "inconsistent")

    def test_rejects_a_receipt_whose_artifacts_are_not_a_list(self):
        started = time.time_ns()
        self.write(self.receipt, {
            "status": "completed", "artifacts": "report.md", "reason": "done",
        })
        report = self.verify(None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "inconsistent")

    def test_rejects_a_status_outside_the_schema(self):
        started = time.time_ns()
        self.write(self.receipt, {"status": "done", "artifacts": []})
        report = self.verify(None, started)
        self.assertFalse(report["accepted"])
        self.assertEqual(report["problem"], "inconsistent")

    def test_requires_an_absolute_receipt_path(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            receipt_arg("relative/receipt.json")
        self.assertEqual(receipt_arg("/tmp/r.json"), Path("/tmp/r.json"))


class ReceiptEmitTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.receipt = Path(self.dir.name) / "receipt.json"

    def emit_once(self, status, receipt, started_ns=None, expected=()):
        buffer = io.StringIO()
        started = time.time_ns() if started_ns is None else started_ns
        plan = None if receipt is None else ReceiptPlan(
            path=receipt, baseline=None,
            expected={str(item) for item in expected}, started_ns=started,
        )
        with self.assertRaises(SystemExit) as exit_info, redirect_stdout(buffer):
            emit(status, "p1", "worker", "native_wait", "text", plan)
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
            json.dumps({"status": "completed", "artifacts": [], "reason": "done"}), encoding="utf-8"
        )
        payload, code = self.emit_once("done", self.receipt, started)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["receipt"]["accepted"])
        self.assertEqual(code, 0)

    def test_a_settled_turn_reports_it_was_not_cut_off(self):
        # `timed_out` is what separates a worker that gave up from one the
        # ceiling cut short; the two need opposite recoveries.
        payload, _ = self.emit_once("done", None)
        self.assertFalse(payload["timed_out"])


class StartupGateMatchTest(unittest.TestCase):
    CLAUDE_TRUST = (
        " Accessing workspace:\n"
        " /Users/x/Code/repo\n"
        " Quick safety check: Is this a project you created or one you trust?\n"
        " No, exit\n"
        " Yes, I trust this folder\n"
        " Enter to confirm - Esc to cancel"
    )
    # Captured verbatim from a live kimi pane in an untrusted git root.
    KIMI_TRUST = (
        " Trust this folder?\n"
        " up-down navigate - Enter select - Esc exit\n"
        " /Users/x/Code/repo\n"
        " Project-level MCP servers are disabled until you explicitly choose Trust.\n"
        "    Trust this folder\n"
        "    Enable project MCP servers. Remembered for this folder.\n"
        "  > Don't trust\n"
        "    Exit Kimi Code. Asked again next launch."
    )

    def test_matches_the_claude_directory_trust_dialog(self):
        gate = match_startup_gate(self.CLAUDE_TRUST, "claude")
        self.assertIsNotNone(gate)
        self.assertEqual(gate["keys"], ("Down", "Enter"))

    def test_matches_the_claude_external_imports_dialog(self):
        screen = (
            " This project's CLAUDE.md imports files outside the current working directory.\n"
            " External imports:\n"
            "   /Users/x/Code/AGENTS.md\n"
            " No, disable external imports\n"
            " Yes, allow external imports\n"
            " Enter to confirm - Esc to cancel"
        )
        self.assertIsNotNone(match_startup_gate(screen, "claude"))

    def test_matches_the_kimi_trust_dialog_with_its_own_key_order(self):
        gate = match_startup_gate(self.KIMI_TRUST, "kimi")
        self.assertIsNotNone(gate)
        # Kimi parks the pointer on "Don't trust", one row BELOW the accepting
        # option, so it moves Up where Claude moves Down.
        self.assertEqual(gate["keys"], ("Up", "Enter"))

    def test_never_answers_a_gate_with_another_clis_key_layout(self):
        # Both dialogs say "trust this folder"; sending Claude's Down into kimi's
        # would move away from Trust, and Enter would then exit the CLI.
        self.assertIsNone(match_startup_gate(self.KIMI_TRUST, "claude"))
        self.assertIsNone(match_startup_gate(self.CLAUDE_TRUST, "kimi"))

    def test_an_unidentified_cli_matches_nothing(self):
        self.assertIsNone(match_startup_gate(self.CLAUDE_TRUST, None))
        self.assertIsNone(match_startup_gate(self.KIMI_TRUST, "gemini"))

    def test_does_not_match_a_working_composer(self):
        self.assertIsNone(match_startup_gate("Ask anything\n? for shortcuts", "claude"))

    def test_needs_both_the_confirm_label_and_its_marker(self):
        # The label alone -- e.g. quoted in a worker's own report -- must not match.
        self.assertIsNone(
            match_startup_gate("the pane asked: Yes, I trust this folder", "claude"))


class ManualSetupTest(unittest.TestCase):
    def test_an_answerable_gate_is_not_manual_setup(self):
        screen = "Accessing workspace: /repo\nNo, exit\nYes, I trust this folder"
        self.assertFalse(requires_manual_setup(screen, "claude"))
        self.assertIsNone(gate_blocking_reason(screen, "claude"))

    def test_a_hook_review_panel_needs_a_human(self):
        screen = "2 hooks need review\nPress t to trust all"
        self.assertTrue(requires_manual_setup(screen, "codex"))
        self.assertEqual(gate_blocking_reason(screen, "codex"), "unverified_gate")

    def test_an_unknown_confirmation_prompt_needs_a_human(self):
        screen = "Something happened\nEnter to confirm - Esc to cancel"
        self.assertTrue(requires_manual_setup(screen, "claude"))
        self.assertEqual(gate_blocking_reason(screen, "claude"), "unknown_confirmation")

    def test_a_trust_dialog_from_an_unverified_cli_needs_a_human(self):
        # Same wording, no captured key layout for this kind: fail, never guess.
        screen = "Trust this folder?\nup-down navigate - Enter select"
        self.assertEqual(gate_blocking_reason(screen, "gemini"), "unverified_gate")

    def test_a_login_screen_reports_authentication(self):
        screen = (
            " Claude Code can be used with your Claude subscription or billed"
            " based on API usage.\n"
            " Select login method:\n"
            "   Claude account with subscription\n"
            "   Anthropic Console account"
        )
        self.assertEqual(gate_blocking_reason(screen, "claude"), "authentication")

    def test_an_expired_token_reports_authentication(self):
        self.assertEqual(
            gate_blocking_reason("Session expired, cannot refresh. Please log in again.",
                                 "traecli"),
            "authentication",
        )
        self.assertEqual(
            gate_blocking_reason("TraeCode is not signed in on this machine.", "traecli"),
            "authentication",
        )

    def test_a_device_code_login_reports_authentication(self):
        screen = "Follow these steps to sign in with ChatGPT using device code authorization:"
        self.assertEqual(gate_blocking_reason(screen, "codex"), "authentication")

    def test_a_working_composer_is_not_a_gate(self):
        self.assertIsNone(gate_blocking_reason("Ask anything\n? for shortcuts", "claude"))


class ClearStartupGatesTest(unittest.TestCase):
    CLAUDE_TRUST = "Accessing workspace: /repo\nNo, exit\nYes, I trust this folder"
    KIMI_TRUST = (
        "Trust this folder?\nEnable project MCP servers. Remembered for this folder.\n"
        "  > Don't trust"
    )

    def screens(self, *texts):
        return [subprocess.CompletedProcess([], 0, stdout=t, stderr="") for t in texts]

    def ok(self, n):
        return [subprocess.CompletedProcess([], 0, stdout="", stderr="") for _ in range(n)]

    def run_clear(self, results, kind):
        with patch("herdr_turn.call", side_effect=results) as call:
            with patch("herdr_turn.time.sleep"):
                cleared = clear_startup_gates("w1:p1", kind)
        keys = [c.args[3] for c in call.call_args_list if c.args[1] == "send-keys"]
        return cleared, keys, call

    def test_clears_a_known_gate_and_verifies_it_went_away(self):
        results = (self.screens(self.CLAUDE_TRUST) + self.ok(2)
                   + self.screens("Ask anything", "Ask anything"))
        cleared, keys, _ = self.run_clear(results, "claude")
        self.assertEqual(cleared, 1)
        self.assertEqual(keys, ["Down", "Enter"])

    def test_clears_the_kimi_gate_with_up_not_down(self):
        results = (self.screens(self.KIMI_TRUST) + self.ok(2)
                   + self.screens("Ask anything", "Ask anything"))
        cleared, keys, _ = self.run_clear(results, "kimi")
        self.assertEqual(cleared, 1)
        self.assertEqual(keys, ["Up", "Enter"])

    def test_sends_no_keys_at_an_unrecognized_gate(self):
        screen = "Select login method:\n  Claude account with subscription"
        cleared, keys, _ = self.run_clear(self.screens(screen), "claude")
        self.assertEqual(cleared, 0)
        self.assertEqual(keys, [])

    def test_sends_no_keys_when_the_cli_kind_is_unknown(self):
        # A gate we could answer for claude, on a kind we have not captured.
        cleared, keys, _ = self.run_clear(self.screens(self.CLAUDE_TRUST), "gemini")
        self.assertEqual(cleared, 0)
        self.assertEqual(keys, [])

    def test_sends_no_keys_at_a_login_screen(self):
        screen = "TraeCode is not signed in on this machine. Run `traecli login`"
        cleared, keys, _ = self.run_clear(self.screens(screen), "traecli")
        self.assertEqual(cleared, 0)
        self.assertEqual(keys, [])

    def test_gives_up_after_one_attempt_when_the_gate_stays(self):
        results = (self.screens(self.CLAUDE_TRUST) + self.ok(2)
                   + self.screens(self.CLAUDE_TRUST))
        cleared, keys, _ = self.run_clear(results, "claude")
        self.assertEqual(cleared, 0)
        self.assertEqual(keys, ["Down", "Enter"])


class StartupAgentArgsTest(unittest.TestCase):
    def test_passes_the_official_bypass_to_the_clis_that_have_it(self):
        self.assertEqual(startup_agent_args("codex"), ("--dangerously-bypass-hook-trust",))
        self.assertEqual(startup_agent_args("traecli"), ("--dangerously-bypass-hook-trust",))

    def test_passes_nothing_to_a_cli_without_that_flag(self):
        self.assertEqual(startup_agent_args("claude"), ())
        self.assertEqual(startup_agent_args("kimi"), ())
