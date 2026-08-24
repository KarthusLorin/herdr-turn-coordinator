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


def requires_manual_setup(text):
    text = " ".join(text.lower().split())
    return "trust this folder?" in text and (
        "don't trust" in text or "enable project mcp servers" in text
    )


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
            remaining = min(max(int((deadline - time.monotonic()) * 1000), 3001), 300000)
            settled = call("agent", "wait", target, "--timeout", str(remaining))
            if settled.returncode:
                fail("agent_wait_failed", detail=payload(settled))
            return (
                payload(settled).get("result", {}).get("agent", {}).get("agent_status", "idle"),
                "native_wait",
            )
        current = info.get("revision", revision)
        if current != revision:
            revision = current
            last_change = time.monotonic()
        if time.monotonic() - last_change >= 15:
            return "unknown", "output_quiet"
        time.sleep(0.5)
    fail("agent_quiet_timeout", pane_id=pane_id, revision=revision)


def submit(target, prompt, timeout, lines, baseline_revision):
    before = call("agent", "read", target, "--source", "visible")
    if before.returncode:
        fail("agent_preflight_read_failed", detail=payload(before))
    if requires_manual_setup(before.stdout):
        fail("agent_requires_manual_setup", target=target)
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
        if (
            prompt_error.get("error", {}).get("code") == "agent_prompt_stalled"
            and agent.get("revision", baseline_revision) > baseline_revision
            and (status_confirms_delivery or screen_confirms_delivery)
        ):
            status, wait_mode = wait_for_quiet(target, agent["pane_id"], agent["revision"], timeout)
            final_text = read_once(target, lines) if wait_mode == "native_wait" and status in {"idle", "done"} else read_visible(target)
            print(json.dumps({
                "ok": status in {"idle", "done"},
                "pane_id": agent["pane_id"],
                "agent_name": agent.get("name"),
                "agent_status": status,
                "wait_mode": wait_mode,
                "text": final_text,
            }, ensure_ascii=False))
            raise SystemExit(0 if status in {"idle", "done"} else 2)
        fail(
            "agent_prompt_failed",
            prompt=prompt_error,
            state=payload(state),
            before=payload(before),
            text=payload(text),
        )

    info = payload(result).get("result", {}).get("agent", {})
    status = info.get("agent_status")
    text = read_once(target, lines) if status in {"idle", "done"} else read_visible(target)
    output = {
        "ok": status in {"idle", "done"},
        "pane_id": info.get("pane_id"),
        "agent_name": info.get("name"),
        "agent_status": status,
        "text": text,
    }
    print(json.dumps(output, ensure_ascii=False))
    raise SystemExit(0 if output["ok"] else 2)


def start_agent(kind, name, timeout):
    layout = call("pane", "layout", "--pane", os.environ["HERDR_PANE_ID"])
    if layout.returncode:
        fail("pane_layout_failed", detail=payload(layout))
    area = payload(layout)["result"]["layout"]["area"]
    direction = "right" if area["width"] >= 120 and area["width"] >= area["height"] * 2 else "down"

    split = call(
        "pane", "split", "--current", "--direction", direction,
        "--cwd", os.getcwd(), "--no-focus",
    )
    if split.returncode:
        fail("pane_split_failed", detail=payload(split))
    pane_id = payload(split)["result"]["pane"]["pane_id"]

    start_timeout = str(min(max(timeout, 3001), 300000))
    started = call("agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", start_timeout)
    if started.returncode and payload(started).get("error", {}).get("code") == "agent_pane_busy":
        process = call("pane", "process-info", "--pane", pane_id)
        # ponytail: one bounded retry covers shell startup; use a shell-ready event if Herdr adds one.
        if process.returncode == 0:
            time.sleep(0.2)
            started = call("agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", start_timeout)
    if started.returncode:
        call("pane", "close", pane_id)
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
    run.add_argument("--timeout", type=int, default=300000)
    run.add_argument("--lines", type=int, default=160)

    prompt = sub.add_parser("prompt")
    prompt.add_argument("--target", required=True)
    prompt.add_argument("--prompt", required=True)
    prompt.add_argument("--timeout", type=int, default=300000)
    prompt.add_argument("--lines", type=int, default=160)

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
        _, revision = start_agent(args.kind, name, args.timeout)
        submit(name, args.prompt, args.timeout, args.lines, revision)

    state = call("agent", "get", args.target)
    if state.returncode:
        fail("agent_get_failed", detail=payload(state))
    agent = payload(state).get("result", {}).get("agent", {})
    if agent.get("agent_status") not in {"idle", "done"}:
        fail("agent_not_settled", agent_status=agent.get("agent_status"))
    submit(args.target, args.prompt, args.timeout, args.lines, agent.get("revision", 0))


if __name__ == "__main__":
    main()
