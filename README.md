# Herdr Turn Coordinator

Run one interactive coding-agent turn in Herdr without spending parent-model tokens on status polling.

The plugin keeps the downstream agent's native TUI, creates a dedicated visible pane, waits in a local supervisor process, reads the final output once, and leaves the pane open for human takeover.

## Why

`herdr agent prompt --wait` can report `agent_prompt_stalled` even when a prompt was delivered and the agent continues working. A parent model that recovers by repeatedly calling `agent get`, `agent read`, or `agent wait` pays for every observation as another model turn.

Turn Coordinator moves that recovery loop into an ordinary local process. On a false stall it confirms delivery from a new visible prompt anchor or a recovered working state, then waits for lifecycle settlement. If only a 15-second pane-revision quiet period is available, it returns `unknown` for human takeover instead of guessing that the turn completed. It never resends the same prompt.

## Requirements

- Herdr 0.8.0 or newer
- Python 3.9 or newer
- macOS or Linux
- A Herdr-supported interactive agent CLI

## Install

Recommended Herdr plugin installation:

```sh
herdr plugin install KarthusLorin/herdr-turn-coordinator
herdr plugin action invoke karthuslorin.turn-coordinator.install-cli
herdr plugin log list --plugin karthuslorin.turn-coordinator --limit 1
herdr-turn doctor
```

Plugin actions are asynchronous. Confirm the install action log says `succeeded` before running `doctor`, and ensure `~/.local/bin` is on `PATH`.

Alternatively, install the CLI from npm:

```sh
npm install --global herdr-turn-coordinator --registry=https://registry.npmjs.org/
herdr-turn doctor
```

## Usage

Start a new interactive agent in a dedicated visible pane:

```sh
herdr-turn run \
  --kind kimi \
  --name reviewer \
  --prompt "Review the current diff and report only actionable findings."
```

Continue an existing settled agent:

```sh
herdr-turn prompt \
  --target reviewer \
  --prompt "Now summarize the top three risks."
```

Both commands print one JSON result. The created pane stays open and the agent remains fully interactive.

## Suggested agent instruction

```md
Inside Herdr, preserve downstream agents' native interactive TUIs. Use one
blocking `herdr-turn run --kind <kind> --name <name> --prompt <prompt>` call,
then consume its single final JSON result. Do not poll Herdr from model turns.
```

## Behavior

- Preserves the native interactive TUI; it never substitutes `kimi -p`, `grok --single`, or another batch mode.
- Rejects prompts to agents reported as `working`, `blocked`, or `unknown`.
- Uses Herdr's native blocking wait first.
- On `agent_prompt_stalled`, falls back only after a revision advance plus a new prompt anchor or a recovered `working` state.
- Uses native lifecycle waiting once Herdr reports `working`; otherwise a local 15-second revision-quiet heuristic returns `unknown` without declaring success.
- Reads history once only after confirmed completion. Blocked or uncertain turns read the visible screen without scrolling the live TUI.
- Never resends a stalled prompt automatically.

## Local A/B result

One parent-Codex-to-Kimi review task produced the following result on macOS with Herdr 0.8.0:

| Metric | Before | With plugin | Change |
| --- | ---: | ---: | ---: |
| Input tokens | 357,074 | 124,276 | -65.2% |
| Parent tool calls | 15 | 4 | -73.3% |
| Output tokens | 2,950 | 1,638 | -44.5% |
| Wall time | 108 s | 62 s | -42.6% |

This is a single local comparison, not a universal performance guarantee.

## Check

```sh
python3 -m unittest -v
```

## Scope

This plugin coordinates interactive Herdr agents. Outside Herdr, use the downstream CLI's normal blocking non-interactive mode. It does not modify Herdr, replace agent TUIs, or require an npm package.

## Uninstall

```sh
herdr plugin action invoke karthuslorin.turn-coordinator.uninstall-cli
herdr plugin log list --plugin karthuslorin.turn-coordinator --limit 1
herdr plugin uninstall karthuslorin.turn-coordinator
```

For an npm installation:

```sh
npm uninstall --global herdr-turn-coordinator --registry=https://registry.npmjs.org/
```

## License

MIT
