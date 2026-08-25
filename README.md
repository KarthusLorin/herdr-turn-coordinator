# Herdr Turn Coordinator

Run one interactive coding-agent turn in Herdr without spending parent-model tokens on status polling.

Works with every agent kind supported by your installed Herdr version, including Codex, Claude Code, Grok, Gemini CLI, Kimi, Cursor, OpenCode, and GitHub Copilot.

The plugin keeps the downstream agent's native TUI, splits a dedicated pane without taking focus, waits in a local supervisor process, reads the final output once, and leaves the pane open for human takeover.

## Why

`herdr agent prompt --wait` can report `agent_prompt_stalled` even when a prompt was delivered and the agent continues working. A parent model that recovers by repeatedly calling `agent get`, `agent read`, or `agent wait` pays for every observation as another model turn.

Turn Coordinator moves that wait into a local process. On a false stall it checks for a new prompt on screen or a recovered `working` state, then waits. If it only sees 15 seconds of quiet pane output, it returns `unknown` for human takeover instead of declaring the turn done. It never resends the same prompt.

## Requirements

- Herdr 0.8.0 or newer
- Python 3.9 or newer
- macOS or Linux
- A Herdr-supported interactive agent CLI

## Supported AI CLIs

`--kind` is passed directly to Herdr. Use the Herdr kind ID, not the product name.

| Product | `--kind` | Product | `--kind` |
| --- | --- | --- | --- |
| Codex | `codex` | Claude Code | `claude` |
| Grok | `grok` | Gemini CLI | `gemini` |
| Kimi Code CLI | `kimi` | Cursor Agent CLI | `cursor` |
| OpenCode | `opencode` | GitHub Copilot CLI | `copilot` |
| Cline | `cline` | Kiro CLI | `kiro` |
| Qwen Code | `qwen` | Qoder CLI | `qodercli` |
| Amp | `amp` | Droid | `droid` |

Any other kind accepted by your installed Herdr version also works, including kinds added after this plugin release. Availability and agent detection quality follow Herdr itself. The only extra agent-specific guard is Kimi's first-run folder-trust prompt.

## Install

Recommended Herdr plugin installation:

```sh
herdr plugin install KarthusLorin/herdr-turn-coordinator
herdr plugin action invoke karthuslorin.turn-coordinator.install-cli
herdr plugin log list --plugin karthuslorin.turn-coordinator --limit 1
herdr-turn doctor
```

Plugin actions are asynchronous. Confirm the install action log says `succeeded` before running `doctor`. Plugin install puts `herdr-turn` in `~/.local/bin`, so ensure that directory is on `PATH`.

Alternatively, install the CLI from npm:

```sh
npm install --global herdr-turn-coordinator
herdr-turn doctor
```

Choose one installation method. An npm global install uses the npm prefix bin instead of `~/.local/bin`.

## Usage

`herdr-turn run` and `herdr-turn prompt` must run from a pane inside Herdr (`HERDR_ENV=1`). `herdr-turn doctor` can run outside Herdr.

Start a new interactive agent in a dedicated pane:

```sh
herdr-turn run \
  --kind codex \
  --name reviewer \
  --prompt "Review the current diff and report only actionable findings."
```

For Claude Code, Grok, Gemini, or Kimi, use `--kind claude`, `--kind grok`, `--kind gemini`, or `--kind kimi`.

Continue an existing settled agent:

```sh
herdr-turn prompt \
  --target reviewer \
  --prompt "Now summarize the top three risks."
```

Both commands block until the turn settles or the timeout expires, then print one JSON object. The default timeout is 300000 ms. The created pane stays open and the agent remains fully interactive.

```json
{
  "ok": true,
  "pane_id": "w1:p2",
  "agent_name": "reviewer",
  "agent_status": "idle",
  "text": "..."
}
```

On `ok: false`, a non-zero exit, or a status other than `idle`/`done`, stop for human takeover instead of polling Herdr from model turns.

## Suggested agent instruction

```md
Inside Herdr, preserve downstream agents' native interactive TUIs. Use one
blocking `herdr-turn run --kind <kind> --name <name> --prompt <prompt>` call,
then consume its single final JSON result. Do not poll Herdr from model turns.
```

## Behavior

- Preserves each agent's native interactive TUI by using `herdr agent start`; it never substitutes a batch or non-interactive mode.
- Rejects prompts to agents reported as `working`, `blocked`, or `unknown`.
- Leaves Kimi's first-run folder-trust prompt untouched for manual confirmation.
- Uses Herdr's native blocking wait first.
- On `agent_prompt_stalled`, falls back only after a revision advance plus a new prompt anchor or a recovered `working` state.
- Uses native lifecycle waiting once Herdr reports `working`; otherwise a local 15-second revision-quiet heuristic returns `unknown` without declaring success.
- Reads history once only after confirmed completion. Blocked or uncertain turns read the visible screen without scrolling the live TUI.
- Never resends a stalled prompt automatically.

## Local A/B result

One parent-Codex-to-Kimi review task on macOS with Herdr 0.8.0 (`n=1`) produced the following result:

| Metric | Before | With plugin | Change |
| --- | ---: | ---: | ---: |
| Input tokens | 357,074 | 124,276 | -65.2% |
| Parent tool calls | 15 | 4 | -73.3% |
| Output tokens | 2,950 | 1,638 | -44.5% |
| Wall time | 108 s | 62 s | -42.6% |

Before the plugin, the parent recovered a false stall by polling Herdr. With the plugin, it made one blocking call. This is a single local comparison, not a universal performance guarantee, and it does not measure other agent pairings.

## Tests

```sh
python3 -m unittest -v
```

`npm test` runs the same command.

## Scope

This plugin coordinates interactive Herdr agents. Outside Herdr, use the downstream CLI's normal blocking non-interactive mode. It does not modify Herdr or replace agent TUIs. Plugin installation does not need npm; the npm package is an alternative way to install the same `herdr-turn` CLI.

## Uninstall

```sh
herdr plugin action invoke karthuslorin.turn-coordinator.uninstall-cli
herdr plugin log list --plugin karthuslorin.turn-coordinator --limit 1
herdr plugin uninstall karthuslorin.turn-coordinator
```

For an npm installation:

```sh
npm uninstall --global herdr-turn-coordinator
```

## License

MIT
