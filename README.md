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

## Quick start

```bash
# Add a task (inline)
ilan task add -n fix-bug -d "Fix the null-pointer crash in auth.py"

# Add a task (from file)
ilan task add -n big-refactor -f tasks/refactor.md

# See what's running
ilan task ls

# Read the latest agent output
ilan task tail fix-bug

# Reply to a blocked agent
ilan task reply fix-bug "Use the OAuth2 flow instead"

# Mark a task as done
ilan task done fix-bug
```

A background server starts automatically on the first command. It polls every ~3 seconds, reaping finished agents and spawning new ones up to the concurrency cap.

## Commands

### Tasks

| Command | Description |
|---|---|
| `ilan task add -n NAME -d "prompt"` | Add a task (or use `-f file`) |
| `ilan task ls [-a]` | List active tasks (`-a` includes DONE/DISCARDED) |
| `ilan task show NAME` | Print the full prompt of a task |
| `ilan task tail NAME` | Show the last assistant message + any user replies after it |
| `ilan task reply NAME "msg"` | Send a reply to an agent |
| `ilan task log NAME` | Open the full conversation log in your editor |
| `ilan task kill NAME` | Kill a WORKING agent, move task to ERROR |
| `ilan task done NAME` | Mark task as DONE |
| `ilan task discard NAME` | Mark task as DISCARDED |
| `ilan task undone NAME` | Move a DONE task back to NEEDS\_ATTENTION |
| `ilan task undiscard NAME` | Move a DISCARDED task back to NEEDS\_ATTENTION |
| `ilan task rm NAME [NAME...]` | Delete task(s) and all their data |

### Server

| Command | Description |
|---|---|
| `ilan server status` | Show whether the background server is running |
| `ilan server stop` | Stop the background server |

### Config

| Command | Description |
|---|---|
| `ilan config show` | Print current configuration |
| `ilan config set KEY VALUE` | Set a config value |
| `ilan clear-everything` | Delete all tasks, logs, and data (requires confirmation) |

### Configuration keys

| Key | Default | Description |
|---|---|---|
| `workdir` | `~/.ilan` | Where all ilan data is stored |
| `num-agents` | `5` | Max concurrent Claude Code agents |
| `time-zone` | `US/Pacific` | Time zone for displayed timestamps |
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

Agents self-report their status via a `[STATUS: DONE]` or `[STATUS: NEEDS_ATTENTION]` marker injected into every prompt.

## Architecture

```
┌─────────────┐         HTTP/JSON          ┌──────────────────┐
│  ilan CLI   │ ◀─────────────────────────▶ │  ilan server     │
│  (client)   │    localhost:ephemeral      │  (background)    │
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
