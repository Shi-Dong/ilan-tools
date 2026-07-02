# Ilan CLI

A CLI tool that manages and runs a swarm of [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents on a list of user-defined tasks. Each task is dispatched to `claude -p` in the background; when an agent finishes or gets blocked, the next unclaimed task is picked up automatically.

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

Aliases are assigned when a task is created and released when it transitions to `DONE` or `DISCARDED`. If a task is moved back out of a terminal state (via `undone` / `undiscard`), it receives a new alias. The alias pool supports up to 81 concurrent non-terminal tasks. To pick a specific alias for an active task, use `ilan task alias NAME NEW_ALIAS` (or the `ilan alias` shorthand); the new alias must be two letters from `asdfghjkl` and not already taken by another task.

Task names must be at least 3 characters long (to avoid ambiguity with aliases) and may only contain letters, digits, hyphens (`-`), and underscores (`_`). Aliases are not included in shell tab-completion.

## Commands

### Tasks

| Command | Description |
|---|---|
| `ilan task add -n NAME -d "prompt"` | Add a task (or use `-f file`; name must be ≥ 3 chars, letters/digits/`-`/`_` only) |
| `ilan task ls [-a] [NAME]` | List active tasks (`-a` includes `DONE`/`DISCARDED`); if `NAME` is given, show its tail instead |
| `ilan task show NAME` | Print the full prompt of a task |
| `ilan task path NAME` | Print the Claude Code session log path for a task |
| `ilan task check-model NAME` | Print the model name (e.g. `claude-opus-4-7`) that generated the last assistant message in the task's Claude Code session log |
| `ilan task tail NAME` | Show the last assistant message together with the user prompt that elicited it and any user replies after it; ends with a line naming the model that generated the last assistant message |
| `ilan task reply NAME ["msg"]` | Send a reply to an agent (omit message to show tail) |
| `ilan task tap NAME` | Ask for a status update (nudges `WORKING` agents; re-prompts `AGENT_FINISHED`/`NEEDS_ATTENTION`/`ERROR` tasks) |
| `ilan task sleep NAME DURATION` | Re-prompt a `NEEDS_ATTENTION` / `AGENT_FINISHED` task to sleep for DURATION and report back. DURATION is an integer or decimal with an optional unit suffix — no whitespace — e.g. `300`, `300s`, `5m`, `2h`, `1.5h`. Units: `s`/`sec`/`second`/`seconds`, `m`/`min`/`mins`/`minute`/`minutes`, `h`/`hr`/`hrs`/`hour`/`hours`; bare numbers are seconds. The task transitions to `UNCLAIMED` and shows `(sleeping for Xs)` in `ilan ls` / `ilan dashboard` while `UNCLAIMED` / `WORKING`. |
| `ilan task log [-p] NAME` | Open the full conversation log in your editor (`-p` prints the log file path instead) |
| `ilan task summarize NAME` | Summarize the task's log and print the summary (works on local and remote clients) |
| `ilan task rename OLD NEW` | Rename a task |
| `ilan task alias NAME NEW_ALIAS` | Change the two-letter alias of an active task (`NEW_ALIAS` must be two letters from `asdfghjkl` and not already in use) |
| `ilan task branch OLD -n NEW [-d "msg" \| -f FILE]` | Branch a new task from `OLD`, inheriting its full Claude Code context (both tasks stay repliable and diverge from there) |
| `ilan task kill NAME` | Kill a `WORKING` agent, move task to `ERROR` |
| `ilan task attach NAME` | Attach to a task's Claude Code session interactively |
| `ilan task done NAME [NAME...]` | Mark task(s) as `DONE` |
| `ilan task discard NAME [NAME...]` | Mark task(s) as `DISCARDED` |
| `ilan task undone NAME` | Move a `DONE` task back to `NEEDS_ATTENTION` |
| `ilan task undiscard NAME` | Move a `DISCARDED` task back to `NEEDS_ATTENTION` |
| `ilan task unread NAME [NAME...]` | Restore the unread marker on task(s) |
| `ilan task max NAME` | Run this task on the Fable model (`claude-fable-5`) instead of the default; a red `FABLE` tag shows in the Cost column in `ilan ls` / `ilan dashboard`. Takes effect on the task's next agent spawn. |
| `ilan task unmax NAME` | Reset the task's model back to the `model` config default |
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
| `ilan rename OLD NEW` | `ilan task rename OLD NEW` |
| `ilan alias NAME NEW_ALIAS` | `ilan task alias NAME NEW_ALIAS` |
| `ilan branch OLD -n NEW` | `ilan task branch OLD -n NEW` |
| `ilan tap NAME` | `ilan task tap NAME` |
| `ilan sleep NAME DURATION` | `ilan task sleep NAME DURATION` |
| `ilan attach NAME` | `ilan task attach NAME` |
| `ilan log [-p] NAME` | `ilan task log [-p] NAME` |
| `ilan summarize NAME` | `ilan task summarize NAME` |
| `ilan sum NAME` | `ilan task summarize NAME` |
| `ilan check-model NAME` | `ilan task check-model NAME` |
| `ilan done NAME [NAME...]` | `ilan task done NAME [NAME...]` |
| `ilan discard NAME [NAME...]` | `ilan task discard NAME [NAME...]` |
| `ilan undone NAME` | `ilan task undone NAME` |
| `ilan undiscard NAME` | `ilan task undiscard NAME` |
| `ilan unread NAME [NAME...]` | `ilan task unread NAME [NAME...]` |
| `ilan max NAME` | `ilan task max NAME` |
| `ilan unmax NAME` | `ilan task unmax NAME` |

### Dashboard

```bash
ilan dashboard
```

Full-screen, real-time task table (like `htop`). Polls the server at the configured `dashboard-interval` (default: every 1 second). Keybindings: **q** quit, **r** force-refresh. The "refreshed at" timestamp uses the configured `time-zone`.

Each row's `Status` cell carries a Haiku-generated one-line summary of the agent's most recent reply (≤ 20 words). The summary is refreshed only on `WORKING → NEEDS_ATTENTION` / `AGENT_FINISHED` transitions. The server produces it via Anthropic's API when `api-key-claude` is set, otherwise it falls back to the server's local `claude` CLI (Claude Code subscription, requires `claude` installed and logged in). Toggle whether the client renders it with `ilan config set one-line-summary true|false` (default `true`); if the toggle is on but the server has no `api-key-claude`, the client prints a note about the CLI fallback above the table. A thin separator is drawn between every task row in both `ilan ls` and `ilan dashboard`; branched (child) tasks remain visually nested under their parent via the existing tree-prefix indentation. When a `github-token` is configured, a `History` column links each row to its secret-Gist conversation mirror — see [Gist conversation mirroring](#gist-conversation-mirroring).

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
| `num-agents` | `5` | Max concurrent Claude Code agents |
| `time-zone` | `US/Pacific` | Time zone for displayed timestamps (client-side: set on each machine running the CLI). Accepts friendly aliases — see [Time-zone aliases](#time-zone-aliases) |
| `model` | `opus` | Model passed to `claude -p`. Accepts Claude Code aliases (`opus`, `sonnet`, `haiku`) or `glm` / `glm-5-2` to run agents on GLM-5.2 via Z.ai — see [GLM-5.2 model](#glm-52-model) |
| `effort` | `high` | Effort level for the model |
| `summarize-model` | `sonnet` | Claude model used by `ilan task summarize` |
| `summarize-effort` | `medium` | Effort level used by `ilan task summarize` |
| `editor` | `emacs` | Editor used by `ilan task log` |
| `api-key-claude` | _(empty)_ | Anthropic API key passed as `ANTHROPIC_API_KEY` to spawned agents; also used to call Haiku for the one-line status summary in `ilan ls` and `ilan dashboard`. When empty, the one-line summary falls back to the server's local `claude` CLI (Claude Code subscription) instead. Masked in `ilan config show` (only the last five characters are displayed, preceded by `**`) |
| `api-key-glm` | _(empty)_ | Z.ai API key used as the bearer token (`ANTHROPIC_AUTH_TOKEN`) for spawned agents when `model` is `glm` / `glm-5-2`. Ignored for non-GLM models — see [GLM-5.2 model](#glm-52-model). Masked in `ilan config show` (only the last five characters are displayed, preceded by `**`) |
| `github-token` | _(empty)_ | GitHub personal-access token (needs the `gist` scope). Setting it turns on [Gist conversation mirroring](#gist-conversation-mirroring); leaving it empty keeps the feature off. Masked in `ilan config show` (only the last five characters are displayed, preceded by `**`) |
| `dashboard-interval` | `1` | Seconds between automatic refreshes in `ilan dashboard` |
| `line-number` | `false` | When `true`, `ilan tail` prefixes each assistant line with a yellow `[N]` marker and `ilan reply` / `ilan task branch` expand `@N` into the Nth line, double-quoted |
| `one-line-summary` | `true` | Client-side: render the Haiku-generated one-line summary in the Status column of `ilan ls` and `ilan dashboard`. The summary is produced by the server: via Anthropic's API when `api-key-claude` is set, otherwise via the server's local `claude` CLI (Claude Code subscription). This flag only controls whether the client shows it. If on while the server has no `api-key-claude` set, the client prints a note about the CLI fallback. |

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

### Summarize

```bash
ilan summarize fix-bug   # or: ilan sum fix-bug / ilan task summarize fix-bug
```

`ilan summarize` asks the ilan server to feed the task's JSONL log into
a fresh `claude -p` invocation (using `summarize-model` /
`summarize-effort`) and write a markdown summary next to the log at
`<workdir>/logs/<task>.summary.md`. The summary text is then printed on
the command line, so it works the same way from a local shell or from a
client machine connected to a remote server via `ILAN_SERVER_URL`. The
summary includes each PR the task produced (link + one-line description)
and each wandb run (link + current status). Re-running on an unchanged
task skips the claude call and reprints the cached summary.

The prompt template is a plain file at
`src/ilan/prompts/summarize.md` — edit it in place if you want to tweak
what the summary looks like.

### Fable model (`ilan max` / `ilan unmax`)

```bash
ilan max my-task      # run my-task on claude-fable-5
ilan unmax my-task    # back to the default model
```

`ilan max` pins a single task to Anthropic's Mythos-class **Fable** model
(`claude-fable-5`), leaving every other task on the configured `model`
default. While a task is maxed, a red `FABLE` tag is rendered on its own
line in the Cost column (beneath the cost) in `ilan ls` and `ilan dashboard`. The override
is per task and persists across replies until you run `ilan unmax`, which
clears it back to the `model` config default. A change takes effect on
the task's next agent spawn (the next reply / scheduled run), not on an
already-running agent.

### GLM-5.2 model

```bash
ilan config set api-key-glm <your-zai-api-key>
ilan config set model glm        # or: glm-5-2
```

Setting `model` to `glm` (alias for `glm-5-2`) runs every agent on
[Z.ai](https://z.ai)'s **GLM-5.2** instead of an Anthropic model. Because GLM
ships an Anthropic-compatible endpoint, spawned `claude -p` agents talk to it
natively: ilan resolves the alias to the real model id `glm-5.2[1m]` (the
1M-token context variant) and routes the agent to `https://api.z.ai/api/anthropic`,
authenticating with the `api-key-glm` config as the bearer token
(`ANTHROPIC_AUTH_TOKEN`). The Anthropic `api-key-claude` is *not* forwarded for GLM
runs. Switch back with `ilan config set model opus` (or any Claude Code alias)
at any time.

## Task lifecycle

```
UNCLAIMED ──▶ WORKING ──▶ AGENT_FINISHED ──▶ DONE
                │               │
                │               ▼
                │         NEEDS_ATTENTION ◀── undone
                │               │
                ▼               ▼
              ERROR        (reply) ──▶ UNCLAIMED
                │
                ▼
           (reply) ──▶ UNCLAIMED

                        DISCARDED ◀── discard
                            │
                            ▼
                      (undiscard) ──▶ NEEDS_ATTENTION
```

Agents self-report their status via a `[STATUS: DONE]` or `[STATUS: NEEDS_ATTENTION]` marker injected into every prompt. The injected convention also requires the agent to provide a substantive answer before emitting the marker.

All `claude -p` processes are spawned with `cwd` set to the configured workdir so that Claude Code stores sessions under a consistent project directory. This ensures `--resume` can always locate prior sessions.

## Architecture

```
┌─────────────┐         HTTP/JSON           ┌──────────────────┐
│  ilan CLI   │ ◀─────────────────────────▶ │  ilan server     │
│  (client)   │    localhost:4526           │  (background)    │
└─────────────┘                             │                  │
                                            │  ┌────────────┐  │
                                            │  │ scheduler  │  │ ── poll every 3s
                                            │  └────────────┘  │
                                            │        │         │
                                            │        ▼         │
                                            │  ┌────────────┐  │
                                            │  │ runner     │  │ ── spawns claude -p
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

The server auto-starts on the first CLI command and recovers gracefully on restart by reading task state and agent output files from the workdir.

## Gist conversation mirroring

Set a `github-token` (a GitHub personal-access token with the `gist` scope) to mirror every task's conversation to a **secret GitHub Gist**, giving you a clean web view of the whole user ↔ agent exchange:

```bash
ilan config set github-token ghp_xxxxxxxxxxxxxxxxxxxx
```

- Each task gets **one** secret Gist. Its landing file is just a title card — the conversation itself is posted as Gist **comments**, one message per comment, so GitHub renders the `User` and `Assistant` turns as separate Markdown bubbles. Messages are already Markdown, so they are posted as-is.
- Mirroring is fully **asynchronous**: a background syncer thread does all GitHub I/O off the hot path, so it never blocks the scheduler or a client reply. As soon as a message is appended to a task's log, the task is enqueued and mirrored shortly after.
- **Existing tasks are back-filled**: the first new message on any pre-existing task creates its Gist and posts the entire prior history in order.
- `ilan ls` and `ilan dashboard` gain a **History** column: a `history` hyperlink (over the Gist URL) for tasks that have been mirrored, or a dim `-` placeholder otherwise.

Leaving `github-token` empty keeps the feature off; nothing is sent to GitHub. GitHub access uses the REST API directly over `urllib` (no `gh` dependency), so it works the same on a laptop or a remote server.
