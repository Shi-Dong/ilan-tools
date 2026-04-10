# Ilan CLI

A CLI tool that manages and runs a swarm of [Claude Code](https://docs.anthropic.com/en/docs/claude-code) agents on a list of user-defined tasks. Each task is dispatched to `claude -p` in the background; when an agent finishes or gets blocked, the next unclaimed task is picked up automatically.

## Installation

Requires Python 3.11+ and [uv](https://github.com/astral-sh/uv).

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

Aliases are assigned when a task is created and released when it transitions to DONE or DISCARDED. If a task is moved back out of a terminal state (via `undone` / `undiscard`), it receives a new alias. The alias pool supports up to 81 concurrent non-terminal tasks.

To avoid ambiguity between aliases and task names, task names must be at least 3 characters long. Aliases are not included in shell tab-completion.

## Review mark

When a task transitions from WORKING to NEEDS\_ATTENTION or AGENT\_FINISHED, a ⚠️ mark appears after the task name in `ilan ls`. This indicates the agent has new output that hasn't been reviewed yet.

The mark is automatically cleared when you run any of these commands on the task:

- `ilan re NAME "msg"` / `ilan reply NAME "msg"`
- `ilan tail NAME`
- `ilan log NAME` / `ilan logs NAME`

## Commands

### Tasks

| Command | Description |
|---|---|
| `ilan task add -n NAME -d "prompt"` | Add a task (or use `-f file`; name must be ≥ 3 chars) |
| `ilan task ls [-a]` | List active tasks (`-a` includes DONE/DISCARDED) |
| `ilan task show NAME` | Print the full prompt of a task |
| `ilan task path NAME` | Print the Claude Code session log path for a task |
| `ilan task tail NAME` | Show the last assistant message + any user replies after it |
| `ilan task reply NAME "msg"` | Send a reply to an agent |
| `ilan task tap NAME` | Ask a WORKING agent for a status update |
| `ilan task log [-p] NAME` | Open the full conversation log in your editor (`-p` prints the log file path instead) |
| `ilan task kill NAME` | Kill a WORKING agent, move task to ERROR |
| `ilan task done NAME [NAME...]` | Mark task(s) as DONE |
| `ilan task discard NAME [NAME...]` | Mark task(s) as DISCARDED |
| `ilan task undone NAME` | Move a DONE task back to NEEDS\_ATTENTION |
| `ilan task undiscard NAME` | Move a DISCARDED task back to NEEDS\_ATTENTION |
| `ilan task rm NAME [NAME...]` | Delete task(s) and all their data |

### Shorthands

Frequently used task commands have top-level aliases to save typing:

| Shorthand | Equivalent |
|---|---|
| `ilan add` | `ilan task add` |
| `ilan ls [-a]` | `ilan task ls [-a]` |
| `ilan tail NAME` | `ilan task tail NAME` |
| `ilan reply NAME "msg"` | `ilan task reply NAME "msg"` |
| `ilan re NAME "msg"` | `ilan task reply NAME "msg"` |
| `ilan tap NAME` | `ilan task tap NAME` |
| `ilan log [-p] NAME` | `ilan task log [-p] NAME` |
| `ilan done NAME [NAME...]` | `ilan task done NAME [NAME...]` |
| `ilan discard NAME [NAME...]` | `ilan task discard NAME [NAME...]` |

### Server

| Command | Description |
|---|---|
| `ilan server status` | Show whether the background server is running |
| `ilan server restart` | Restart the server (picks up code changes) |
| `ilan server stop` | Stop the background server |

### Config

| Command | Description |
|---|---|
| `ilan config show` | Print current configuration |
| `ilan config set KEY VALUE` | Set a config value |
| `ilan clear-everything` | Delete all tasks, logs, and data (requires confirmation) |

### Configuration keys

Configuration is stored at `~/.config/ilan/config.json` (created with defaults on first run).

| Key | Default | Description |
|---|---|---|
| `workdir` | `~/.ilan` | Where all ilan data is stored |
| `num-agents` | `5` | Max concurrent Claude Code agents |
| `time-zone` | `US/Pacific` | Time zone for displayed timestamps |
| `model` | `opus` | Claude model passed to `claude -p` |
| `effort` | `high` | Effort level for the model |
| `editor` | `emacs` | Editor used by `ilan task log` |

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
