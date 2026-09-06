<h1 align="center">Ilan CLI</h1>

<p align="center">
  Run a swarm of coding agents from your terminal, and check on them from your phone.
</p>

<p align="center">
  <a href="https://github.com/Shi-Dong/ilan-tools/actions/workflows/tests.yml"><img src="https://github.com/Shi-Dong/ilan-tools/actions/workflows/tests.yml/badge.svg" alt="Tests"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/backends-Claude%20Code%20%7C%20Codex-555" alt="Backends: Claude Code and Codex">
</p>

Ilan turns a to-do list into a fleet of autonomous coding agents. Describe a task in one line and Ilan hands it to [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or [Codex](https://github.com/openai/codex) in the background, right away. Come back whenever you like: read what the agent did, answer its questions, and move on to the next task.

## Why Ilan

- **No waiting, no babysitting.** A task starts the moment you add it or reply to it. There is no queue and no cap on how many agents run at once; a background server spawns them, reaps them, and remembers everything.
- **Every task is a conversation.** Agents report back and stop when they are done or blocked. Reply to unblock them, `tap` a busy one for a progress report, `cancel` a message you regret, or tell an idle agent to `sleep` and check in later.
- **Two backends, one workflow.** Run Claude Code and Codex tasks side by side, and move a task between them at any time. Each backend keeps its own session and is caught up on whatever it missed.
- **Handles you can type.** Active tasks get a two-letter alias (`ilan re sd "try v2"`), closed tasks get a permanent number, and unnamed `xxx-` tasks delete themselves when closed.
- **Branch instead of repeating yourself.** Fork a task into a child that inherits the whole conversation, try two approaches in parallel, and draw the family tree with `ilan tree`.
- **Everything at a glance.** `ilan ls` and a live `ilan dashboard` show each task's status, an unread marker, and a one-line AI summary of the agent's latest reply.
- **Wherever you are.** A phone-first web app ships with the server, and every conversation can be mirrored to a secret GitHub Gist for a clean, shareable read.
- **More brain when it matters.** Pin a hard task to the biggest model its backend has with `ilan max` — Claude's Fable, Codex's Astra — and drop back to the default with `ilan unmax`.

## Installation

Ilan needs Python 3.11+, [uv](https://github.com/astral-sh/uv), [tmux](https://github.com/tmux/tmux), and at least one agent CLI ([`claude`](https://docs.anthropic.com/en/docs/claude-code) or [`codex`](https://github.com/openai/codex)) that is installed and logged in.

```bash
git clone git@github.com:Shi-Dong/ilan-tools.git
cd ilan-tools
uv venv && uv pip install -e .
ilan --install-completion fish      # or bash / zsh
```

The `ilan` binary lands in `.venv/bin/`; add that directory to your `PATH`. Later on, `ilan update` followed by `ilan server restart` brings a running install up to date.

## Quick start

```bash
ilan add -n fix-bug -d "Fix the null-pointer crash in auth.py"   # the agent starts now
ilan ls                                     # who is doing what
ilan tail fix-bug                           # read the agent's latest reply
ilan re fix-bug "Use the OAuth2 flow"       # answer it (re = reply)
ilan done fix-bug                           # close the task
```

Long prompts can come from a file (`-f tasks/refactor.md`). Leave out `-n` and you get a throwaway task with a generated name such as `xxx-cat-likes-fin`, which is deleted rather than kept when you close it.

The first command starts a background server on port 4526. It stays up, spawns agents, reaps finished ones, and serves the web app. To drive a server on another machine, point the CLI at it with `ILAN_SERVER_URL=http://my-server:4526` (see [remote usage](docs/reference.md#remote-usage)).

> **Warning:** agents run with `--dangerously-skip-permissions`. Use at your own discretion.

## How a task lives

| Status | Meaning |
|---|---|
| `WORKING` | An agent is on it. |
| `AGENT_FINISHED` | The agent thinks it is done. Review the result. |
| `NEEDS_ATTENTION` | The agent is blocked and waiting for you. |
| `ERROR` | The agent process died or was killed. A reply revives it. |
| `DONE` / `DISCARDED` | Closed by you. `undone` / `undiscard` bring a task back. |

A task is `WORKING` from the second it is created, and any reply to a finished, blocked, or errored task re-spawns its agent at once. Agents report their own state: Ilan asks every prompt to end with a `[STATUS: DONE]` or `[STATUS: NEEDS_ATTENTION]` marker.

Listings are in creation order with pinned tasks first, and a task you have not read since its last reply carries a `!!` marker. `ilan ls -a -c` looks like this:

```
→ (as) fix-bug !! AGENT_FINISHED
(sd) big-refactor WORKING
1 write-docs DONE
2 (hf) old-idea DISCARDED
```

- **Names** are at least three characters of letters, digits, `-`, and `_`.
- **Aliases** such as `as` or `sd` are handed to every active task and accepted wherever a name is (`ilan tail sd`). Pick your own with `ilan alias fix-bug fg`.
- **Numbers** are minted when a task is closed and never reused while it exists. They are the handle for `ilan undone 1` and `ilan undiscard 2`.
- **Burnable tasks** are the ones named `xxx-…`: `done` and `discard` delete them outright. Rename a task into or out of the prefix to change your mind.

The fine print on [aliases](docs/reference.md#task-aliases), [numbers](docs/reference.md#task-numbers), and [burnable tasks](docs/reference.md#burnable-tasks-xxx-) is in the reference.

## Commands

Every task command has a top-level shorthand, so `ilan task reply` is just `ilan reply` (or `ilan re`). The tables below use the short form; `show`, `path`, and `kill` exist only as `ilan task …`. Any `NAME` may be a task's alias. The [full command reference](docs/reference.md#commands) lists every flag.

### Create and read

| Command | What it does |
|---|---|
| `ilan add [-n NAME] (-d "prompt" \| -f FILE) [--claude \| --codex] [--max]` | Add a task and start it. Omit `-n` for a burnable task; `--max` starts it on its backend's max model. |
| `ilan ls [-a] [-c]` | List active tasks. `-a` includes closed ones, `-c` prints one plain line per task. |
| `ilan search PATTERN` | Filter the `ilan ls -a -c` lines by a case-insensitive substring. |
| `ilan tail NAME [-n N]` | Show the latest reply with the prompt behind it, then which model wrote it, at what effort and cost, and the token counts. `-n` shows the last N replies. |
| `ilan task show NAME` | Print the task's full prompt. |
| `ilan task path NAME` | Print the path of the task's Claude Code session log. |
| `ilan check-model NAME` | Print the model that wrote the last reply. |
| `ilan log NAME [-p]` | Open the whole conversation in your editor; `-p` prints the log path instead. |
| `ilan open NAME` | Open the task's Gist history in the browser. |
| `ilan tree NAME` | Draw the branch tree the task belongs to. |
| `ilan dashboard` | Live full-screen table, refreshed every second. `q` quits, `r` refreshes. |

### Talk to the agent

| Command | What it does |
|---|---|
| `ilan reply NAME ["msg"] [--max \| --unmax] [-t DURATION]` | Send a message; `ilan re` is the same. Without a message it shows the tail. `-t 1h` re-sends the message every hour until you write to the task again. |
| `ilan tap NAME` | Ask the agent for a status update. |
| `ilan cancel NAME` | Retract your last message and tell the agent to stop acting on it. |
| `ilan sleep NAME DURATION` | Tell an idle agent to wait (`300`, `5m`, `1.5h`) and then report back. |
| `ilan branch OLD [-n NEW] (-d "msg" \| -f FILE)` | Fork a child task that inherits the whole conversation and starts on `msg`. |
| `ilan attach NAME` | Drop into the agent's native session (`claude --resume` or `codex resume`). |

### Organize

| Command | What it does |
|---|---|
| `ilan rename OLD NEW [-d "msg"]` | Rename a task, optionally sending `msg` right after. |
| `ilan alias NAME XY` | Choose the task's two-letter alias (letters from `asdfghjkl`). |
| `ilan pin NAME` / `ilan unpin NAME` | Keep a task at the top of every listing, even after it is closed. |
| `ilan unread NAME…` | Put the `!!` marker back on tasks. |

### Change how a task runs

| Command | What it does |
|---|---|
| `ilan max NAME` / `ilan unmax NAME` | Run the task on its backend's max model — Fable (`claude-fable-5-1`) on `claude`, Astra (`gpt-6-astra`) on `codex` — or return to the configured default. Takes effect on the next reply. |
| `ilan switch-backend NAME` | Move an idle task between Claude Code and Codex. Maxed tasks stay maxed (FABLE ↔ ASTRA). The new backend catches up on its first turn. |
| `ilan task kill NAME` | Stop a `WORKING` agent. The task moves to `ERROR` until you reply. |

### Close and clean up

| Command | What it does |
|---|---|
| `ilan done NAME…` / `ilan discard NAME…` | Close tasks. A burnable `xxx-` task is deleted instead. |
| `ilan undone N` / `ilan undiscard N` | Reopen a closed task by name, alias, or number. |
| `ilan remove NAME…` | Delete tasks and all their data. Children survive and are re-parented. |
| `ilan clean DURATION` | Delete tasks untouched for longer than `DURATION` (`5h`, `3d`). Tasks with children are kept. |
| `ilan clear-everything` | Delete every task. Always asks first. |

### Server and settings

| Command | What it does |
|---|---|
| `ilan server status` / `restart` / `stop` | Inspect, restart (after an update), or stop the background server. |
| `ilan ping [-c N]` | Measure the round trip to a remote server. |
| `ilan config show` | Print the server-side and client-side settings. |
| `ilan config set [-y] KEY VALUE` | Change a setting; `-y` skips the "server or client?" confirmation. |
| `ilan update` | Pull the latest ilan-tools and reinstall. |

## Configuration

Settings live in `~/.config/ilan/config.json`. Most are **server-side** and apply wherever the server runs; `time-zone`, `editor`, `dashboard-interval`, `line-number`, `markdown`, and `one-line-summary` are **client-side** and belong to the machine running the CLI. `ilan config set` says which side it is about to write and asks before doing so. Secrets are masked in `ilan config show`.

| Key | Default | What it does |
|---|---|---|
| `workdir` | `~/.ilan` | Where tasks, conversation logs, and agent output are stored. |
| `default-backend` | `claude` | Backend for new tasks, `claude` or `codex`. |
| `model-claude` | `claude-opus-4-7` | Exact model id for Claude tasks. Aliases such as `opus` are rejected. |
| `model-codex` | `gpt-5.6-sol` | Exact model id for Codex tasks. |
| `effort` | `max` | Reasoning effort for both backends: `low`, `medium`, `high`, `xhigh`, or `max`. |
| `api-key-mode` | `false` | `true` bills agents to the API keys below; `false` uses each CLI's own login. |
| `api-key-claude` | *(empty)* | Anthropic key used while `api-key-mode` is on. |
| `api-key-codex` | *(empty)* | OpenAI key used while `api-key-mode` is on. When set, it also produces the one-line summaries. |
| `github-token` | *(empty)* | Token with the `gist` scope. Setting it turns on Gist mirroring. |
| `time-zone` | `US/Pacific` | Time zone for timestamps. Friendly aliases such as `tokyo` or `london` work. |
| `editor` | `emacs` | Editor used by `ilan log`. |
| `push-contact` | `mailto:ilan@example.com` | Contact address a push service may use about this server's web-app notifications. Apple accepts only a `mailto:` with a dotted host. |
| `dashboard-interval` | `1` | Seconds between dashboard refreshes. |
| `line-number` | `false` | Number the lines of `ilan tail`, so a reply can quote line 12 as `@12`. |
| `markdown` | `false` | Render replies as Markdown in the terminal. |
| `one-line-summary` | `true` | Show the AI one-line summary of each latest reply in `ilan ls` and the dashboard. |

## Two backends, one conversation

```bash
ilan config set default-backend codex     # new tasks go to Codex
ilan add -n fix-bug -d "…" --claude       # except this one
ilan switch-backend fix-bug               # move it later
```

Task names are tinted by backend in every listing: orange for Claude, light blue for Codex. Each backend keeps its own native session for the task, and alongside them Ilan keeps one unified log of prompts and replies, so a switch never loses history: the incoming backend resumes its own session and is briefed on the turns it missed. The two CLIs read their standing instructions from different files, `CLAUDE.md` and `AGENTS.md`; [CLAUDE_VS_CODEX.md](CLAUDE_VS_CODEX.md) shows how to keep them in sync.

## Web app

The server serves a phone-first web app at `/app` (`http://127.0.0.1:4526/app/`), with nothing extra to install. It covers the everyday commands: the task list with search, reading and replying, tap, sleep, done, pin, max, switch-backend, branch, and settings — and, once the app is on an iPhone's Home Screen, push notifications when a task finishes. Point a phone at the server over your LAN, a VPN, or an SSH tunnel, and on iOS use Share, then Add to Home Screen, to install it as an app.

The web app has the same access model as the server, which is none: anyone who can reach the port can drive your agents. Expose it only on a network you trust, or behind an authenticating proxy.

## Gist mirroring

Set a `github-token` and every task's conversation is mirrored to its own **secret GitHub Gist**: one comment per message, with timestamps and the model that wrote each reply, so the whole exchange reads as a chat thread in the browser. Mirroring runs in the background and never slows a reply, existing tasks are back-filled on their next message, and `ilan ls` gains a `History` link per task.

```bash
ilan config set github-token ghp_xxxxxxxxxxxxxxxxxxxx
```

## How it works

```
 ilan CLI · web app ──HTTP/JSON──▶ ilan server (localhost:4526)
                                       │  every ~3 s: reap finished agents,
                                       │  spawn `claude -p` / `codex exec`
                                       ▼
                                   ~/.ilan/  tasks.json · logs/*.jsonl · output/*.json
```

The CLI never touches agents directly; every action goes through the server, so the scheduler sees it immediately. The server survives restarts by re-reading task state from the workdir. Agents run with the workdir as their `cwd` so their sessions can always be resumed, and each agent's terminal work happens in tmux sessions tagged with its task's hash, which are cleaned up when the task closes.

## Further reading

- [docs/reference.md](docs/reference.md): the complete behavioural reference. Every flag, the duration and alias rules, burnable tasks, line-number mode, exact model ids, cost and token attribution, time-zone aliases, the max models, the branch tree, remote use, the web app, and the Gist mirror in full.
- [CLAUDE_VS_CODEX.md](CLAUDE_VS_CODEX.md): sharing project context between Claude Code and Codex.
- [AGENTS.md](AGENTS.md): guide for agents (and humans) working on this repo.
