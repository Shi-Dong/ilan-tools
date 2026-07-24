# Ilan CLI

A CLI tool that manages and runs a swarm of coding agents on a list of user-defined tasks. Each task is dispatched to an agent backend — [Claude Code](https://docs.anthropic.com/en/docs/claude-code) (`claude -p`) or [Codex](https://github.com/openai/codex) (`codex exec`) — in the background, starting the instant the task is created or replied to. A task can be moved between backends at any time without losing its conversation — see [Agent backends](#agent-backends).

## Installation

Requires Python 3.11+, [uv](https://github.com/astral-sh/uv), and [tmux](https://github.com/tmux/tmux).

Each agent's terminal work runs inside tmux sessions prefixed with a unique task hash, so that sessions can be tracked and automatically cleaned up when a task is completed, discarded, or killed.

```bash
git clone git@github.com:Shi-Dong/ilan-tools.git
cd ilan-tools
uv venv && uv pip install -e .
```

The `ilan` binary is now at `.venv/bin/ilan`. Add it to your `PATH` or invoke it directly.

### Shell completion

```bash
ilan --install-completion zsh   # or bash / fish
```

Once installed, Tab completes task names, config keys, sub-commands, and options.

## Quick start

```bash
# Add a task (inline)
ilan add -n fix-bug -d "Fix the null-pointer crash in auth.py"

# Add a task (from file)
ilan add -n big-refactor -f tasks/refactor.md

# See what's running
ilan ls

# Read the latest agent output
ilan tail fix-bug

# Reply to a blocked agent
ilan reply fix-bug "Use the OAuth2 flow instead"
# or even shorter:
ilan re fix-bug "Use the OAuth2 flow instead"

# Omit the message to see the tail first
ilan re fix-bug

# Mark a task as done
ilan done fix-bug
```

A background server starts automatically on the first command (port 4526). It polls every ~3 seconds, reaping finished agents and spawning new ones up to the concurrency cap.

> **Warning:** `--dangerously-skip-permissions` is always on. Use at your own discretion.

### Remote usage

To manage tasks on a centralized host from another machine, set `ILAN_SERVER_URL` on the client machine:

```bash
export ILAN_SERVER_URL=http://my-server:4526
ilan task ls          # queries the remote server
ilan task add -n fix-bug -d "Fix the crash"   # task runs on the remote host
```

When the env var is unset, `ilan` starts and talks to a local server as usual.

When connecting to a remote server, the CLI automatically checks whether the local and server ilan code are built from the same git commit. If they differ, a warning is printed with both commit hashes so you can decide whether to update.

## Task aliases

Every non-terminal task is automatically assigned a two-letter alias (e.g. `aa`, `sd`, `kl`) drawn from the characters `asdfghjkl`. Aliases are displayed in bold magenta in `ilan ls` and can be used in place of the full task name in any command:

```bash
ilan tail sd          # instead of: ilan tail fix-bug
ilan re sd "try v2"   # instead of: ilan re fix-bug "try v2"
ilan done sd
```

Aliases are assigned when a task is created. A `DONE` task releases its alias back to the pool; moving it back out with `undone` mints a fresh alias. A `DISCARDED` task instead *keeps* its alias — a discard is a recycle-bin entry you can restore, so it stays reachable by its short alias (e.g. `ilan undiscard sd`), and `undiscard` brings it back under that same handle. To pick a specific alias for an active task, use `ilan task alias NAME NEW_ALIAS` (or the `ilan alias` shorthand); the new alias must be two letters from `asdfghjkl` and not already taken by another task.

Task names must be at least 3 characters long (to avoid ambiguity with aliases) and may only contain letters, digits, hyphens (`-`), and underscores (`_`). Aliases are not included in shell tab-completion.

## Commands

### Tasks

| Command | Description |
|---|---|
| `ilan task add -n NAME -d "prompt"` | Add a task (or use `-f file`; name must be ≥ 3 chars, letters/digits/`-`/`_` only). Pass `--claude` or `--codex` to pick the backend for this task (default: the `default-backend` config value) — see [Agent backends](#agent-backends). Pass `--max` to create the task already on the [Fable model](#fable-model-ilan-max--ilan-unmax) (implies `--claude`) |
| `ilan task ls [-a] [NAME]` | List active tasks (`-a` includes `DONE`/`DISCARDED`); if `NAME` is given, show its tail instead |
| `ilan task show NAME` | Print the full prompt of a task |
| `ilan task path NAME` | Print the Claude Code session log path for a task |
| `ilan task check-model NAME` | Print the model name (e.g. `claude-opus-4-7`) that generated the last assistant message in the task's Claude Code session log |
| `ilan task tail NAME` | Show the last assistant message together with the user prompt that elicited it and any user replies after it; ends with a line naming the model (and the reasoning effort) that generated the last assistant message |
| `ilan task reply NAME ["msg"]` | Send a reply to an agent (omit message to show tail). Pass `--max` to switch the task to the [Fable model](#fable-model-ilan-max--ilan-unmax) before posting the reply (persists for all subsequent messages; on a `codex` task a warning is shown and the reply is posted with the model unchanged), or `--unmax` to reset the model to the config default before posting |
| `ilan task tap NAME` | Ask for a status update (nudges `WORKING` agents; re-prompts `AGENT_FINISHED`/`NEEDS_ATTENTION`/`ERROR` tasks) |
| `ilan task sleep NAME DURATION` | Re-prompt a `NEEDS_ATTENTION` / `AGENT_FINISHED` task to sleep for DURATION and report back. DURATION is an integer or decimal with an optional unit suffix — no whitespace — e.g. `300`, `300s`, `5m`, `2h`, `1.5h`. Units: `s`/`sec`/`second`/`seconds`, `m`/`min`/`mins`/`minute`/`minutes`, `h`/`hr`/`hrs`/`hour`/`hours`; bare numbers are seconds. The task goes back to `WORKING` and shows `(sleeping for Xs)` in `ilan ls` / `ilan dashboard`. |
| `ilan task log [-p] NAME` | Open the full conversation log in your editor (`-p` prints the log file path instead) |
| `ilan task open NAME` | Open the task's GitHub Gist history page in your default browser, deep-linked to the most recent comment. Warns and does nothing if the task has no Gist yet (see [Gist conversation mirroring](#gist-conversation-mirroring)) |
| `ilan task rename OLD NEW [-d "msg"]` | Rename a task; with `-d`, immediately send `"msg"` as a reply to the renamed task |
| `ilan task alias NAME NEW_ALIAS` | Change the two-letter alias of an active task (`NEW_ALIAS` must be two letters from `asdfghjkl` and not already in use) |
| `ilan task branch OLD -n NEW [-d "msg" \| -f FILE]` | Branch a new task from `OLD`, inheriting its full Claude Code context (both tasks stay repliable and diverge from there) |
| `ilan task kill NAME` | Kill a `WORKING` agent, move task to `ERROR` |
| `ilan task attach NAME` | Attach to a task's Claude Code session interactively |
| `ilan task done NAME [NAME...]` | Mark task(s) as `DONE` |
| `ilan task discard NAME [NAME...]` | Mark task(s) as `DISCARDED` |
| `ilan task undone NAME` | Move a `DONE` task back to `NEEDS_ATTENTION` |
| `ilan task undiscard NAME` | Move a `DISCARDED` task back to `NEEDS_ATTENTION` |
| `ilan task unread NAME [NAME...]` | Restore the unread marker on task(s) |
| `ilan task max NAME` | Run this task on the Fable model (`claude-fable-5`) instead of the default; a red `FABLE` tag shows beneath the task name in `ilan ls` / `ilan dashboard`. Takes effect on the task's next agent spawn. Fable is Claude-only, so this is a no-op (with a warning) on a `codex` task — switch it to `claude` first. The tag is hidden while the task is on `codex` (Fable is inactive there) and reappears when it is switched back to `claude`. |
| `ilan task unmax NAME` | Reset the task's model back to the `model-claude` config default |
| `ilan task switch-backend NAME` | Toggle the task's agent backend (`claude` ↔ `codex`). Lazy: takes effect on the task's next spawn, and the new backend catches up on its next turn. Not allowed on a `WORKING` task (warns and does nothing) — wait for the agent to finish or kill it first. See [Agent backends](#agent-backends) |
| `ilan task rm [-f] NAME [NAME...]` | Delete task(s) and all their data (refuses if any has an active descendant; `-f` overrides) |

### Shorthands

Frequently used task commands have top-level aliases to save typing:

| Shorthand | Equivalent |
|---|---|
| `ilan add` | `ilan task add` |
| `ilan ls [-a] [NAME]` | `ilan task ls [-a] [NAME]` |
| `ilan tail NAME` | `ilan task tail NAME` |
| `ilan reply NAME ["msg"]` | `ilan task reply NAME ["msg"]` |
| `ilan re NAME ["msg"]` | `ilan task reply NAME ["msg"]` |
| `ilan rename OLD NEW [-d "msg"]` | `ilan task rename OLD NEW [-d "msg"]` |
| `ilan alias NAME NEW_ALIAS` | `ilan task alias NAME NEW_ALIAS` |
| `ilan branch OLD -n NEW` | `ilan task branch OLD -n NEW` |
| `ilan tap NAME` | `ilan task tap NAME` |
| `ilan sleep NAME DURATION` | `ilan task sleep NAME DURATION` |
| `ilan attach NAME` | `ilan task attach NAME` |
| `ilan log [-p] NAME` | `ilan task log [-p] NAME` |
| `ilan open NAME` | `ilan task open NAME` |
| `ilan check-model NAME` | `ilan task check-model NAME` |
| `ilan done NAME [NAME...]` | `ilan task done NAME [NAME...]` |
| `ilan discard NAME [NAME...]` | `ilan task discard NAME [NAME...]` |
| `ilan undone NAME` | `ilan task undone NAME` |
| `ilan undiscard NAME` | `ilan task undiscard NAME` |
| `ilan unread NAME [NAME...]` | `ilan task unread NAME [NAME...]` |
| `ilan max NAME` | `ilan task max NAME` |
| `ilan unmax NAME` | `ilan task unmax NAME` |
| `ilan switch-backend NAME` | `ilan task switch-backend NAME` |

### Dashboard

```bash
ilan dashboard
```

Full-screen, real-time task table (like `htop`). Polls the server at the configured `dashboard-interval` (default: every 1 second). Keybindings: **q** quit, **r** force-refresh. The "refreshed at" timestamp uses the configured `time-zone`.

Each row's `Status` cell carries a Haiku-generated one-line summary of the agent's most recent reply (≤ 20 words). The summary is refreshed only on `WORKING → NEEDS_ATTENTION` / `AGENT_FINISHED` transitions. The server produces it via Anthropic's API when `api-key-claude` is set, otherwise it falls back to the server's local `claude` CLI (Claude Code subscription, requires `claude` installed and logged in). Toggle whether the client renders it with `ilan config set one-line-summary true|false` (default `true`); if the toggle is on but the server has no `api-key-claude`, the client prints a note about the CLI fallback above the table. A thin separator is drawn between every task row in both `ilan ls` and `ilan dashboard`; branched (child) tasks remain visually nested under their parent via the existing tree-prefix indentation. When a `github-token` is configured, a `History` column links each row to its secret-Gist conversation mirror — see [Gist conversation mirroring](#gist-conversation-mirroring).

Both `ilan ls` and `ilan dashboard` adapt to the terminal width: on a window narrower than 120 columns they drop the `Created` column so the remaining `Name` / `Status` / `Last Changed` / `History` columns stay legible.

### Server

| Command | Description |
|---|---|
| `ilan server status` | Show whether the background server is running |
| `ilan server restart` | Restart the server (picks up code changes) |
| `ilan server stop` | Stop the background server |
| `ilan ping [-c N]` | Measure round-trip latency to the server. With `ILAN_SERVER_URL` set, sends `N` health requests (default 3) and prints the rounded average round-trip time in ms. Without it, just notes that the server is local. |

### Config

| Command | Description |
|---|---|
| `ilan config show` | Print current configuration |
| `ilan config set KEY VALUE` | Set a config value |
| `ilan clean DURATION` | Delete tasks whose last change is older than DURATION (e.g. `5h`, `3d`); never touches tasks that have children |
| `ilan clear-everything` | Delete all tasks, logs, and data (requires confirmation) |
| `ilan update` | Pull the latest ilan-tools from remote and reinstall |

### Configuration keys

Configuration is stored at `~/.config/ilan/config.json` (created with defaults on first run).

| Key | Default | Description |
|---|---|---|
| `workdir` | `~/.ilan` | Where all ilan data is stored |
| `default-backend` | `claude` | Default agent backend for newly added tasks (`claude` or `codex`). Override per task with `ilan add --claude`/`--codex`, or flip an existing task with `ilan task switch-backend` — see [Agent backends](#agent-backends) |
| `time-zone` | `US/Pacific` | Time zone for displayed timestamps (client-side: set on each machine running the CLI). Accepts friendly aliases — see [Time-zone aliases](#time-zone-aliases) |
| `model-claude` | `opus` | Model passed to `claude -p` for tasks on the `claude` backend. Accepts Claude Code aliases (`opus`, `sonnet`, `haiku`) or a full Claude model id |
| `model-codex` | `gpt-5.6-sol` | Model passed to `codex exec --model` for tasks on the `codex` backend. Accepts any model name the Codex CLI recognizes — see [Agent backends](#agent-backends) |
| `effort` | `xhigh` | Reasoning-effort level for spawned agents, applied to both backends: passed as `--effort` to `claude` and as `-c model_reasoning_effort=...` to `codex exec`. Accepts `low`, `medium`, `high`, or `xhigh` (the subset both CLIs support); other values are rejected |
| `editor` | `emacs` | Editor used by `ilan task log` |
| `api-key-claude` | _(empty)_ | Anthropic API key passed as `ANTHROPIC_API_KEY` to spawned agents; also used to call Haiku for the one-line status summary in `ilan ls` and `ilan dashboard`. When empty, the one-line summary falls back to the server's local `claude` CLI (Claude Code subscription) instead. Masked in `ilan config show` (only the last five characters are displayed, preceded by `**`) |
| `api-key-codex` | _(empty)_ | OpenAI API key passed as `OPENAI_API_KEY` to spawned Codex agents. When empty, Codex falls back to the server's inherited environment (`codex login`, or an `OPENAI_API_KEY` already present in the server's env). Masked in `ilan config show` (only the last five characters are displayed, preceded by `**`) |
| `github-token` | _(empty)_ | GitHub personal-access token (needs the `gist` scope). Setting it turns on [Gist conversation mirroring](#gist-conversation-mirroring); leaving it empty keeps the feature off. Masked in `ilan config show` (only the last five characters are displayed, preceded by `**`) |
| `dashboard-interval` | `1` | Seconds between automatic refreshes in `ilan dashboard` |
| `line-number` | `false` | When `true`, `ilan tail` prefixes each assistant line with a yellow `[N]` marker and `ilan reply` / `ilan task branch` expand `@N` into the Nth line, double-quoted |
| `one-line-summary` | `true` | Client-side: render the Haiku-generated one-line summary in the Status column of `ilan ls` and `ilan dashboard` (`ilan ls -a` never shows it, to keep the longer all-tasks listing compact). The summary is produced by the server: via Anthropic's API when `api-key-claude` is set, otherwise via the server's local `claude` CLI (Claude Code subscription). This flag only controls whether the client shows it. If on while the server has no `api-key-claude` set, the client prints a note about the CLI fallback. |

### Time-zone aliases

`time-zone` accepts friendly, case-insensitive aliases so you don't have to
remember IANA names. A raw IANA name (e.g. `Australia/Sydney`) still works.

```bash
ilan config set time-zone tokyo     # → Asia/Tokyo
ilan config set time-zone china     # → Asia/Shanghai
ilan config set time-zone pacific   # → US/Pacific
```

| Alias(es) | Resolves to |
|---|---|
| `china`, `beijing`, `shanghai`, `wuhan` | `Asia/Shanghai` |
| `japan`, `tokyo` | `Asia/Tokyo` |
| `korea`, `seoul` | `Asia/Seoul` |
| `uk`, `london` | `Europe/London` |
| `pacific`, `west`, `western` | `US/Pacific` |
| `atlantic`, `east`, `eastern` | `US/Eastern` |

### Line-number mode

Turn it on with:

```bash
ilan config set line-number true
```

Then `ilan tail <task>` prints each line of every shown assistant message with
a yellow `[N]` prefix; numbering is continuous across all assistant messages
when `-n` surfaces more than one. `ilan reply` (and its `re` shorthand) then
looks up the most recent tail for that task and replaces every `@N` in your
reply with the Nth line wrapped in double quotes — so `ilan reply my-task "about @12, ..."`
becomes `about "the 12th line ...", ...` before being sent. Out-of-range
references pass through unchanged.

`ilan task branch` (and its `branch` shorthand) does the same expansion on its
first reply (`-d "..."` or `-f FILE`), looking up the cached tail of the parent
task — so you can `ilan tail parent` and then
`ilan task branch parent -n child -d "redo @12 differently"` and the child task
starts with the parent's 12th line already quoted.

You can override the config for a single invocation with the
`--line-number` / `--no-line-number` flags on `ilan tail` (and on `ilan re` /
`ilan ls` when only a task name — no response message — is given):

```bash
ilan tail my-task --line-number       # force line numbers on
ilan re my-task --no-line-number      # tail without line numbers
```

`--no-line-number` also drops the Rich Panel border around each entry —
just a plain `Assistant` / `User` header followed by the message body —
so you can copy the content straight out of the terminal without
sweeping up any box-drawing characters. `--line-number` keeps the full
boxed rendering since the `[N]` prefixes need a panel-relative width to
not wrap.

Passing the flag together with a response message
(`ilan re my-task "some response" --no-line-number`) is rejected, since
the flag only affects how tail output is rendered.

`line-number` is a **client-side** config: `ilan config set line-number true`
writes to the local `~/.config/ilan/config.json` instead of going through the
server, so the toggle works the same way whether you're driving a local or
remote `ilan` server (set it on the machine you're running the CLI on).

### Fable model (`ilan max` / `ilan unmax`)

```bash
ilan max my-task      # run my-task on claude-fable-5
ilan unmax my-task    # back to the default model
ilan add -n my-task -d "…" --max   # create a task already on Fable
ilan reply my-task "…" --max       # switch to Fable, then post the reply
ilan reply my-task "…" --unmax     # back to the default model, then reply
```

`ilan max` pins a single task to Anthropic's Mythos-class **Fable** model
(`claude-fable-5`), leaving every other task on the configured `model-claude`
default. While a task is maxed, a red `FABLE` tag is rendered on its own
line beneath the task name in `ilan ls` and `ilan dashboard` (hidden while
the task is on the `codex` backend, since Fable is Claude-only and inactive
there; it reappears on switching back to `claude`). The override is per
task and persists across replies until you run `ilan unmax`, which clears
it back to the `model-claude` config default. A change takes effect on the task's
next agent spawn (the next reply), not on an already-running agent.

`ilan reply` (and its `re` / `ilan task reply` forms) accepts `--max` /
`--unmax` to combine the two steps: the model is switched first, then the
reply is posted, so the reply's own turn already runs on the new model and
the switch persists for every message after it. If the model is already in
the requested state (`--max` on a task already running Fable, `--unmax` on
a task already on the default model), the switch is silently skipped and
the reply is posted as usual. `--max` on a `codex` task prints the same
warning as `ilan max` and posts the reply with the model untouched. Both
flags require a reply message and are mutually exclusive.

## Agent backends

Every task runs on an **agent backend** (its *engine*): Claude Code (`claude -p`)
or Codex (`codex exec`). The backend is chosen per task, so a Claude task and a
Codex task can run side by side in the same swarm.

```bash
ilan config set default-backend codex  # default backend for new tasks
ilan add -n fix-bug -d "…" --claude    # override the default for one task
ilan task switch-backend fix-bug   # flip an existing task's backend
```

- **Per-task selection.** New tasks use the `default-backend` config value unless `ilan add`
  is given `--claude` or `--codex`. The task's name is tinted by its engine in `ilan ls` /
  `ilan dashboard` — orange for Claude, light blue for Codex — so the running
  backend is legible at a glance.
- **No lost context on switch.** Each backend keeps its *own* native session for
  the task, so switching away and back resumes that backend's conversation rather
  than starting over. On top of the native sessions, ilan keeps a unified
  conversation log (the user prompts + final assistant replies, spanning both
  backends).
- **Catch-up on switch.** `ilan task switch-backend` is *lazy* — it only
  rewires which backend the task uses on its next spawn. A `WORKING` task
  cannot be switched: the command warns and does nothing, so an in-flight
  turn is never interrupted (wait for the agent to finish, or `ilan task
  kill` it, then switch). On the incoming backend's next turn it is caught
  up on everything it missed: its native session is resumed and the interim
  turns are injected, or — if that backend has never run this task — a
  fresh session is seeded with the full transcript.
- **Catch-up prompts are size-capped.** A very long history is truncated to the
  newest ~500K characters when it is rendered into the catch-up prompt (Codex
  hard-rejects inputs over 1 MiB); the prompt notes how many earlier turns were
  omitted. The truncation only affects what the incoming backend is shown on the
  switch turn — the full history is always preserved in the task's unified log
  (`ilan log`) and its Gist mirror.

Codex tasks run on `gpt-5.6-sol` (OpenAI's flagship model); the Codex model is
currently fixed (`ilan max` pins the Claude-only Fable model and has no effect on
a Codex task).

Codex authenticates with the configured `api-key-codex`, passed as `OPENAI_API_KEY`
to spawned agents; when it is empty, Codex falls back to the server's inherited
environment (`codex login`, or an `OPENAI_API_KEY` present in the server's env). See
the `api-key-codex` note under [Configuration keys](#configuration-keys).

The two backends read their project context from different files — Claude Code
from `CLAUDE.md`, Codex from `AGENTS.md`. To keep the same standing instructions
reaching whichever backend runs a task, see
[CLAUDE_VS_CODEX.md](CLAUDE_VS_CODEX.md).

## Task lifecycle

```
WORKING ──▶ AGENT_FINISHED ──▶ DONE
   │               │
   │               ▼
   │         NEEDS_ATTENTION ◀── undone
   │               │
   ▼               ▼
 ERROR        (reply) ──▶ WORKING
   │
   ▼
(reply) ──▶ WORKING

             DISCARDED ◀── discard
                 │
                 ▼
           (undiscard) ──▶ NEEDS_ATTENTION
```

A task starts `WORKING` the moment it is created; a reply to a finished,
blocked, or errored task immediately re-spawns its agent. There is no cap on
the number of concurrently running agents.

Agents self-report their status via a `[STATUS: DONE]` or `[STATUS: NEEDS_ATTENTION]` marker injected into every prompt. The injected convention also requires the agent to provide a substantive answer before emitting the marker.

All `claude -p` processes are spawned with `cwd` set to the configured workdir so that Claude Code stores sessions under a consistent project directory. This ensures `--resume` can always locate prior sessions.

## Architecture

```
┌─────────────┐         HTTP/JSON           ┌──────────────────┐
│  ilan CLI   │ ◀─────────────────────────▶ │  ilan server     │
│  (client)   │    localhost:4526           │  (background)    │
└─────────────┘                             │                  │
                                            │  ┌────────────┐  │
                                            │  │ reaper     │  │ ── poll every 3s
                                            │  └────────────┘  │
                                            │        │         │
                                            │        ▼         │
                                            │  ┌────────────┐  │
                                            │  │ runner     │  │ ── spawns claude -p / codex exec
                                            │  └────────────┘  │
                                            │        │         │
                                            └────────┼─────────┘
                                                     ▼
                                            ┌────────────────┐
                                            │  ~/.ilan/      │
                                            │  tasks.json    │
                                            │  logs/*.jsonl  │
                                            │  output/*.json │
                                            └────────────────┘
```

The server auto-starts on the first CLI command and recovers gracefully on restart by reading task state and agent output files from the workdir. The runner drives each task through a pluggable backend adapter (`src/ilan/backends/`: `claude.py`, `codex.py`) selected by the task's engine — see [Agent backends](#agent-backends).

## Gist conversation mirroring

Set a `github-token` (a GitHub personal-access token with the `gist` scope) to mirror every task's conversation to a **secret GitHub Gist**, giving you a clean web view of the whole user ↔ agent exchange:

```bash
ilan config set github-token ghp_xxxxxxxxxxxxxxxxxxxx
```

- Each task gets **one** secret Gist. Its landing file is just a title card — the conversation itself is posted as Gist **comments**, one message per comment, so GitHub renders the `User` and `Assistant` turns as separate Markdown bubbles. Messages are already Markdown, so they are posted as-is — except that LaTeX math delimiters (`\(…\)` and `\[…\]`), which GitHub Markdown does not render, are rewritten to GitHub's math syntax (`` $`…`$ `` inline and ` ```math ` blocks; code blocks and inline code are left untouched). Assistant comments end with an italic attribution line naming the model that generated the message and the reasoning effort it ran at.
- Mirroring is fully **asynchronous**: a background syncer thread does all GitHub I/O off the hot path, so it never blocks the reaper or a client reply. As soon as a message is appended to a task's log, the task is enqueued and mirrored shortly after.
- **Existing tasks are back-filled**: the first new message on any pre-existing task creates its Gist and posts the entire prior history in order.
- `ilan ls` and `ilan dashboard` gain a **History** column: a `history` hyperlink (over the Gist URL) for tasks that have been mirrored, or a dim `-` placeholder otherwise.

Leaving `github-token` empty keeps the feature off; nothing is sent to GitHub. GitHub access uses the REST API directly over `urllib` (no `gh` dependency), so it works the same on a laptop or a remote server.
