#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path


HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
CLI_PATH = Path.home() / ".local" / "bin" / "herdr-turn"

# Herdr's `agent prompt --wait` / `agent wait` support waiting indefinitely, so
# the turn timeout is only an upper ceiling, not a polling cadence. 30 minutes
# covers complex multi-step turns; short hangs still surface via the fallback
# revision-quiet detector below.
DEFAULT_TURN_TIMEOUT_MS = 1_800_000
SETTLED_STABILITY_MS = 1_500
# ponytail: a TUI needs a moment to repaint between keys of a gate sequence.
GATE_KEY_SETTLE_S = 0.4

# A settled pane only proves the TUI returned to its prompt box: a worker that
# hit a rate limit, ran out of context, or gave up mid-task settles exactly like
# one that finished. `--receipt` opts a caller into the stronger contract, where
# the worker's final act is writing a JSON receipt the wrapper verifies itself.
RECEIPT_TERMINAL_STATUS = "completed"


def call(*args):
    return subprocess.run(
        [HERDR, *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def payload(result):
    raw = result.stdout.strip() or result.stderr.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw, "exit_code": result.returncode}


def fail(message, **details):
    print(json.dumps({"ok": False, "error": message, **details}, ensure_ascii=False))
    raise SystemExit(1)


def parse_timeout(value):
    value = value.strip().lower()
    match = re.fullmatch(r"(\d+)(ms|s|m)?", value)
    if not match:
        raise argparse.ArgumentTypeError("timeout must look like 300000, 600ms, 600s, or 10m")
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    if unit is None and amount < 1000:
        raise argparse.ArgumentTypeError(
            "bare timeouts are milliseconds; add a unit, e.g. 600s or 10m"
        )
    return amount * {None: 1, "ms": 1, "s": 1000, "m": 60000}[unit]


def install_cli():
    source = Path(__file__).resolve()
    CLI_PATH.parent.mkdir(parents=True, exist_ok=True)
    if CLI_PATH.is_symlink():
        if CLI_PATH.resolve(strict=False) == source:
            print(json.dumps({"ok": True, "cli": str(CLI_PATH)}, ensure_ascii=False))
            return
        CLI_PATH.unlink()
    elif CLI_PATH.exists():
        fail("cli_path_exists", path=str(CLI_PATH))
    CLI_PATH.symlink_to(source)
    print(json.dumps({"ok": True, "cli": str(CLI_PATH)}, ensure_ascii=False))


def uninstall_cli():
    source = Path(__file__).resolve()
    removed = False
    if CLI_PATH.is_symlink() and CLI_PATH.resolve(strict=False) == source:
        CLI_PATH.unlink()
        removed = True
    elif CLI_PATH.is_symlink() and not CLI_PATH.exists():
        CLI_PATH.unlink()
        removed = True
    print(json.dumps({"ok": True, "cli": str(CLI_PATH), "removed": removed}, ensure_ascii=False))


def receipt_snapshot(path):
    """Identity of an existing receipt, or None. Compared before/after the turn
    so a leftover receipt from an earlier attempt can never be read as this
    turn's result."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return [stat.st_mtime_ns, stat.st_size, stat.st_ino]


def check_artifacts(entries, started_ns):
    checked = []
    for entry in entries:
        item = {"path": entry, "exists": False, "fresh": False}
        if isinstance(entry, str) and entry:
            try:
                stat = Path(entry).stat()
            except OSError:
                stat = None
            if stat is not None:
                item["exists"] = True
                item["fresh"] = stat.st_mtime_ns >= started_ns
        checked.append(item)
    return checked


def verify_receipt(path, baseline, started_ns):
    report = {
        "path": str(path),
        "present": False,
        "fresh": False,
        "parsable": False,
        "accepted": False,
    }
    current = receipt_snapshot(path)
    if current is None:
        report["problem"] = "missing"
        return report
    report["present"] = True
    if current == baseline or current[0] < started_ns:
        report["problem"] = "stale"
        return report
    report["fresh"] = True
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        report["problem"] = "unparsable"
        report["detail"] = str(error)
        return report
    if not isinstance(data, dict):
        report["problem"] = "unparsable"
        report["detail"] = "receipt must be a JSON object"
        return report
    report["parsable"] = True
    report["status"] = data.get("status")
    report["remaining"] = data.get("remaining")
    report["reason"] = data.get("reason")
    entries = data.get("artifacts")
    report["artifacts"] = check_artifacts(
        entries if isinstance(entries, list) else [], started_ns
    )
    # An empty artifact list is legitimate: an investigation or review turn may
    # deliver only the receipt. What is never legitimate is a named artifact
    # that is absent or untouched by this turn.
    artifacts_ok = all(item["exists"] and item["fresh"] for item in report["artifacts"])
    report["artifacts_ok"] = artifacts_ok
    if report["status"] != RECEIPT_TERMINAL_STATUS:
        report["problem"] = "not_completed"
    elif not artifacts_ok:
        report["problem"] = "artifact_unverified"
    else:
        report["accepted"] = True
    return report


def receipt_arg(value):
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("receipt path must be absolute")
    return path


def read_once(target, lines):
    result = call("agent", "read", target, "--source", "recent-unwrapped", "--lines", str(lines))
    if result.returncode:
        fail("agent_read_failed", detail=payload(result))
    return result.stdout.rstrip()


def read_visible(target):
    result = call("agent", "read", target, "--source", "visible")
    if result.returncode:
        fail("agent_read_failed", detail=payload(result))
    return result.stdout.rstrip()


def normalize_screen(text):
    return re.sub(r"[\s│┃┆┇┊┋┌┐└┘╭╮╰╯]+", "", text)


def contains_new_prompt(before, after, prompt):
    before = normalize_screen(before)
    after = normalize_screen(after)
    prompt = normalize_screen(prompt)
    for offset in range(0, min(len(prompt), 120), 12):
        anchor = prompt[offset:offset + 24]
        if len(anchor) >= 12 and anchor not in before and anchor in after:
            return True
    return False


# ponytail: startup gates that appear before the composer and whose answer is
# task-independent. Each entry is matched by BOTH its confirm label and a second
# distinctive marker, so an unrelated dialog cannot match by accident, and each is
# scoped to the CLI kinds whose key layout was actually captured -- two CLIs can
# word the same gate identically while ordering its options differently.
#
# Captured from live panes: Claude Code 2.1.258 in an untrusted git root, and Kimi
# Code (dist/main.mjs trust-prompt.ts, confirmed against a rendered pane). Every
# one of these dialogs defaults its focus to the refusing option, so the sequence
# must move the selection before confirming -- never send a bare Enter. Kimi moves
# Up where Claude moves Down, which is why "keys" cannot be shared across kinds.
# Re-capture before widening this table.
STARTUP_GATES = (
    {
        "kinds": ("claude",),
        "confirm": "yes, i trust this folder",
        "marker": "accessing workspace",
        "keys": ("Down", "Enter"),
    },
    {
        "kinds": ("claude",),
        "confirm": "yes, allow external imports",
        "marker": "external imports:",
        "keys": ("Down", "Enter"),
    },
    {
        # Kimi renders "Trust this folder?" with the pointer parked on "Don't
        # trust", whose description is "Exit Kimi Code" -- a bare Enter here quits
        # the CLI, and Claude's Down would move further from the accepting option.
        "kinds": ("kimi",),
        "confirm": "trust this folder",
        "marker": "enable project mcp servers",
        "keys": ("Up", "Enter"),
    },
)


def normalize_gate_screen(text):
    return " ".join(text.lower().split())


def match_startup_gate(text, kind=None):
    """The gate on this screen, or None.

    A gate matches only when `kind` is one it was captured on. Passing kind=None
    means "no CLI identified", which matches nothing: an unidentified pane falls
    through to requires_manual_setup rather than being answered with another
    CLI's key layout.
    """
    screen = normalize_gate_screen(text)
    for gate in STARTUP_GATES:
        if kind not in gate["kinds"]:
            continue
        if gate["confirm"] in screen and gate["marker"] in screen:
            return gate
    return None


# Startup gates we can recognize but will NOT answer, because no key sequence has
# been verified for them. Naming them buys a fast, legible failure instead of a
# prompt typed into a dialog or a silent wait until the turn's timeout.
UNANSWERED_GATE_MARKERS = (
    "don't trust",
    "hooks need review",
    "press t to trust",
    "modified since last trusted",
)

# Sign-in and credential screens. These also sit before the composer, but no key
# answers them -- they need a human, a browser, or a refreshed token. Naming them
# turns a silent wait until the turn's timeout into an immediate, legible failure.
# Strings taken from the shipped binaries: Claude Code 2.1.258, Codex 0.152.0,
# TraeCLI 0.202.1, and Kimi Code.
AUTH_GATE_MARKERS = (
    "select login method",
    "how do you want to sign in?",
    "sign in with chatgpt",
    "sign in with account password",
    "login with personal access token",
    "paste or type your api key",
    "invalid api key",
    "please run /login",
    "is not signed in on this machine",
    "login required",
    "session expired",
    "please log in again",
    "credit balance is too low",
)


def gate_blocking_reason(text, kind=None):
    """Why this screen needs a human, or None when it does not.

    Answerable gates return None: clear_startup_gates handles those. Everything
    else that we can name is reported by category, so the failure says whether to
    click a dialog or to go re-authenticate.
    """
    screen = normalize_gate_screen(text)
    if match_startup_gate(text, kind):
        return None
    if any(marker in screen for marker in AUTH_GATE_MARKERS):
        return "authentication"
    if any(marker in screen for marker in UNANSWERED_GATE_MARKERS):
        return "unverified_gate"
    if "trust this folder?" in screen:
        # Some other CLI's trust dialog, or a kind we have no key layout for.
        return "unverified_gate"
    # An unrecognized confirmation prompt: refuse rather than guess a key.
    if "enter to confirm" in screen and "esc to cancel" in screen:
        return "unknown_confirmation"
    return None


def requires_manual_setup(text, kind=None):
    """True when the pane sits on a gate this coordinator will not answer itself."""
    return gate_blocking_reason(text, kind) is not None


def clear_startup_gates(pane_id, kind=None):
    """Answer known pre-composer gates in a pane, verifying each one cleared.

    Reads the pane rather than the agent: these gates block startup before the
    agent is registered, so `agent read` is not available yet. Bounded,
    closed-loop, and fail-closed -- only gates in STARTUP_GATES that were
    captured on this `kind` are answered, each answer is verified by re-reading
    the screen, and anything unrecognized is left untouched for the caller to
    report. Returns how many it cleared.
    """
    cleared = 0
    for _ in range(len(STARTUP_GATES) + 2):
        read = call("pane", "read", pane_id)
        if read.returncode:
            return cleared
        gate = match_startup_gate(read.stdout, kind)
        if gate is None:
            return cleared
        for key in gate["keys"]:
            sent = call("pane", "send-keys", pane_id, key)
            if sent.returncode:
                return cleared
            time.sleep(GATE_KEY_SETTLE_S)
        after = call("pane", "read", pane_id)
        # ponytail: same gate still on screen means our key sequence no longer
        # fits this CLI version; stop instead of trying other keys.
        if after.returncode or match_startup_gate(after.stdout, kind) is gate:
            return cleared
        cleared += 1
    return cleared


def confirm_stable_settled(target, info, deadline):
    while info.get("agent_status") in {"idle", "done"}:
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            fail("agent_settled_confirmation_timeout", target=target)
        grace = min(SETTLED_STABILITY_MS, remaining)
        resumed = call(
            "agent", "wait", target,
            "--until", "working", "--until", "blocked", "--timeout", str(grace),
        )
        if resumed.returncode:
            if payload(resumed).get("error", {}).get("code") == "timeout":
                return info, "stable_settled"
            fail("agent_settled_confirmation_failed", detail=payload(resumed))

        info = payload(resumed).get("result", {}).get("agent", {})
        if info.get("agent_status") == "blocked":
            return info, "blocked"
        if info.get("agent_status") != "working":
            fail("agent_settled_confirmation_unexpected", detail=payload(resumed))

        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            fail("agent_settled_confirmation_timeout", target=target)
        settled = call("agent", "wait", target, "--timeout", str(remaining))
        if settled.returncode:
            fail("agent_wait_failed", detail=payload(settled))
        info = payload(settled).get("result", {}).get("agent", {})

    return info, "blocked" if info.get("agent_status") == "blocked" else "native_wait"


def wait_for_quiet(target, pane_id, delivered_revision, timeout):
    deadline = time.monotonic() + max(0, timeout - 5000) / 1000
    revision = delivered_revision
    last_change = time.monotonic()
    while time.monotonic() < deadline:
        pane = call("pane", "get", pane_id)
        if pane.returncode:
            fail("pane_get_failed", detail=payload(pane))
        info = payload(pane).get("result", {}).get("pane", {})
        status = info.get("agent_status")
        if status == "blocked":
            return "blocked", "blocked"
        if status == "working":
            # `herdr agent wait` waits indefinitely and advertises no maximum, so
            # only floor the remaining budget; clamping it here truncated long turns.
            remaining = max(int((deadline - time.monotonic()) * 1000), 3001)
            settled = call("agent", "wait", target, "--timeout", str(remaining))
            if settled.returncode:
                fail("agent_wait_failed", detail=payload(settled))
            info = payload(settled).get("result", {}).get("agent", {})
            if info.get("agent_status") in {"idle", "done"}:
                info, wait_mode = confirm_stable_settled(target, info, deadline)
                return info.get("agent_status", "idle"), wait_mode
            return info.get("agent_status", "idle"), "native_wait"
        current = info.get("revision", revision)
        if current != revision:
            revision = current
            last_change = time.monotonic()
        if time.monotonic() - last_change >= 60:
            return "unknown", "output_quiet"
        time.sleep(0.5)
    fail("agent_quiet_timeout", pane_id=pane_id, revision=revision)


def emit(status, pane_id, agent_name, wait_mode, text, receipt, receipt_baseline, started_ns):
    settled = status in {"idle", "done"}
    output = {
        "ok": settled,
        "pane_id": pane_id,
        "agent_name": agent_name,
        "agent_status": status,
        "wait_mode": wait_mode,
        "text": text,
    }
    if receipt is not None:
        report = verify_receipt(receipt, receipt_baseline, started_ns)
        output["receipt"] = report
        # Only a caller that asked for a receipt gets the stronger `ok`. Without
        # `--receipt` the payload keeps its published meaning, so older
        # supervisors are unaffected.
        output["ok"] = settled and report["accepted"]
    print(json.dumps(output, ensure_ascii=False))
    raise SystemExit(0 if output["ok"] else 2)


def submit(target, prompt, timeout, lines, baseline_revision, receipt=None, kind=None):
    receipt_baseline = receipt_snapshot(receipt) if receipt else None
    started_ns = time.time_ns()
    before = call("agent", "read", target, "--source", "visible")
    if before.returncode:
        fail("agent_preflight_read_failed", detail=payload(before))
    reason = gate_blocking_reason(before.stdout, kind)
    if reason:
        fail("agent_requires_manual_setup", target=target, reason=reason)
    result = call("agent", "prompt", target, prompt, "--wait", "--timeout", str(timeout))
    if result.returncode:
        state = call("agent", "get", target)
        text = call("agent", "read", target, "--source", "visible")
        prompt_error = payload(result)
        agent = payload(state).get("result", {}).get("agent", {})
        status_confirms_delivery = agent.get("agent_status") in {"working", "blocked"}
        screen_confirms_delivery = (
            before.returncode == 0
            and text.returncode == 0
            and contains_new_prompt(before.stdout, text.stdout, prompt)
        )
        stalled = prompt_error.get("error", {}).get("code") == "agent_prompt_stalled"
        grok_needs_enter = (
            stalled
            and agent.get("agent") == "grok"
            and agent.get("agent_status") in {"idle", "done"}
            and screen_confirms_delivery
        )
        if grok_needs_enter:
            entered = call("agent", "send-keys", target, "enter")
            if entered.returncode:
                fail("agent_enter_failed", detail=payload(entered))
        if (
            grok_needs_enter
            or (
                stalled
                and agent.get("revision", baseline_revision) > baseline_revision
                and (status_confirms_delivery or screen_confirms_delivery)
            )
        ):
            status, wait_mode = wait_for_quiet(target, agent["pane_id"], agent["revision"], timeout)
            final_text = read_once(target, lines) if wait_mode == "native_wait" and status in {"idle", "done"} else read_visible(target)
            emit(
                status, agent["pane_id"], agent.get("name"), wait_mode, final_text,
                receipt, receipt_baseline, started_ns,
            )
        fail(
            "agent_prompt_failed",
            prompt=prompt_error,
            state=payload(state),
            before=payload(before),
            text=payload(text),
        )

    info = payload(result).get("result", {}).get("agent", {})
    status = info.get("agent_status")
    wait_mode = "native_wait"
    if status in {"idle", "done"}:
        deadline = time.monotonic() + timeout / 1000
        info, wait_mode = confirm_stable_settled(target, info, deadline)
        status = info.get("agent_status")
    text = read_once(target, lines) if status in {"idle", "done"} else read_visible(target)
    emit(
        status, info.get("pane_id"), info.get("name"), wait_mode, text,
        receipt, receipt_baseline, started_ns,
    )


def choose_split(layout, caller_pane_id):
    panes = []
    for pane in layout.get("panes", []):
        rect = pane.get("rect", {})
        try:
            width = float(rect["width"])
            height = float(rect["height"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(pane.get("pane_id"), str) and width > 0 and height > 0:
            panes.append((width * height, pane["pane_id"] == caller_pane_id, pane["pane_id"], width, height))

    # Protect the main viewport: prefer splitting the largest non-caller pane so
    # the caller's pane is only ever split once (when it is the sole pane). This
    # keeps the user's primary viewport at a stable size no matter how many
    # sub-agents are spawned.
    workers = [pane for pane in panes if not pane[1]]
    pool = workers or panes
    if pool:
        _, _, pane_id, width, height = max(pool)
        return pane_id, "right" if width >= height * 2 else "down"

    area = layout["area"]
    return None, "right" if area["width"] >= area["height"] * 2 else "down"


# ponytail: hook-trust prompts block startup before the composer appears; codex and
# traecli expose an official bypass flag for exactly this. Claude Code has no
# equivalent, so it is deliberately absent here.
STARTUP_AGENT_ARGS = {
    "codex": ("--dangerously-bypass-hook-trust",),
    "traecli": ("--dangerously-bypass-hook-trust",),
}


def startup_agent_args(kind):
    return STARTUP_AGENT_ARGS.get(kind, ())


def start_agent(kind, name, timeout):
    layout = call("pane", "layout", "--pane", os.environ["HERDR_PANE_ID"])
    if layout.returncode:
        fail("pane_layout_failed", detail=payload(layout))
    target, direction = choose_split(payload(layout)["result"]["layout"], os.environ["HERDR_PANE_ID"])

    split = call(
        "pane", "split", *(('--pane', target) if target else ('--current',)), "--direction", direction,
        "--cwd", os.getcwd(), "--no-focus",
    )
    if split.returncode:
        fail("pane_split_failed", detail=payload(split))
    pane_id = payload(split)["result"]["pane"]["pane_id"]

    start_timeout = str(min(max(timeout, 3001), 300000))
    extra = startup_agent_args(kind)
    passthrough = ("--", *extra) if extra else ()
    started = call("agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", start_timeout, *passthrough)
    if started.returncode and payload(started).get("error", {}).get("code") == "agent_pane_busy":
        process = call("pane", "process-info", "--pane", pane_id)
        # ponytail: one bounded retry covers shell startup; use a shell-ready event if Herdr adds one.
        if process.returncode == 0:
            time.sleep(0.2)
            started = call("agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", start_timeout, *passthrough)
    if started.returncode and payload(started).get("error", {}).get("code") == "agent_not_ready":
        # ponytail: a startup gate blocks registration itself, so this is the only
        # place the pane can still be rescued -- after `agent start` gave up but
        # before the pane is torn down. The name is already registered by the
        # failed start, so wait for readiness here rather than starting again.
        # Only known gates are answered; an unrecognized one leaves `started`
        # failed and falls through below.
        if clear_startup_gates(pane_id, kind):
            started = call("agent", "wait", name, "--until", "idle", "--timeout", start_timeout)
    if started.returncode:
        screen = call("pane", "read", pane_id)
        reason = gate_blocking_reason(screen.stdout, kind) if screen.returncode == 0 else None
        call("pane", "close", pane_id)
        if reason:
            fail("agent_requires_manual_setup", pane_id=pane_id, reason=reason,
                 detail=payload(started))
        fail("agent_start_failed", pane_id=pane_id, detail=payload(started))
    agent = payload(started).get("result", {}).get("agent", {})
    return pane_id, agent.get("revision", 0)


def main():
    parser = argparse.ArgumentParser(prog="herdr-turn")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--kind", required=True)
    run.add_argument("--prompt", required=True)
    run.add_argument("--name")
    run.add_argument(
        "--timeout", type=parse_timeout, default=DEFAULT_TURN_TIMEOUT_MS, metavar="DURATION",
        help="turn timeout; bare values are milliseconds (default: 1800000); accepts ms, s, or m",
    )
    run.add_argument("--lines", type=int, default=160)
    run.add_argument("--receipt", type=receipt_arg, metavar="PATH", help="absolute path to the JSON receipt the worker must write as its final action; enables receipt verification and folds it into `ok`")

    prompt = sub.add_parser("prompt")
    prompt.add_argument("--target", required=True)
    prompt.add_argument("--prompt", required=True)
    prompt.add_argument(
        "--timeout", type=parse_timeout, default=DEFAULT_TURN_TIMEOUT_MS, metavar="DURATION",
        help="turn timeout; bare values are milliseconds (default: 1800000); accepts ms, s, or m",
    )
    prompt.add_argument("--lines", type=int, default=160)
    prompt.add_argument("--receipt", type=receipt_arg, metavar="PATH", help="absolute path to the JSON receipt the worker must write as its final action; enables receipt verification and folds it into `ok`")

    sub.add_parser("doctor")
    sub.add_parser("install-cli")
    sub.add_parser("uninstall-cli")
    args = parser.parse_args()

    if args.command == "install-cli":
        install_cli()
        return
    if args.command == "uninstall-cli":
        uninstall_cli()
        return

    if args.command == "doctor":
        result = call("--version")
        if result.returncode:
            fail("herdr_unavailable", detail=payload(result))
        source = Path(__file__).resolve()
        cli = shutil.which("herdr-turn")
        installed = bool(cli) and Path(cli).resolve(strict=False) == source
        print(json.dumps({
            "ok": installed,
            "herdr": result.stdout.strip(),
            "cli": cli,
            "installed": installed,
        }, ensure_ascii=False))
        raise SystemExit(0 if installed else 1)

    if os.environ.get("HERDR_ENV") != "1":
        fail("not_inside_herdr")

    if args.command == "run":
        name = args.name or f"{args.kind}turn_{os.getpid()}"
        pane_id, revision = start_agent(args.kind, name, args.timeout)
        # ponytail: answer the known pre-composer gates before the prompt is typed,
        # so a trust dialog never swallows it. Runs only here, at startup -- never
        # around a prompt the worker is already handling.
        clear_startup_gates(pane_id, args.kind)
        submit(name, args.prompt, args.timeout, args.lines, revision, args.receipt,
               kind=args.kind)

    state = call("agent", "get", args.target)
    if state.returncode:
        fail("agent_get_failed", detail=payload(state))
    agent = payload(state).get("result", {}).get("agent", {})
    if agent.get("agent_status") not in {"idle", "done"}:
        fail("agent_not_settled", agent_status=agent.get("agent_status"))
    submit(args.target, args.prompt, args.timeout, args.lines, agent.get("revision", 0), args.receipt)


if __name__ == "__main__":
    main()
