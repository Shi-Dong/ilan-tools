# Agent guide — ilan-tools

Instructions for coding agents (Claude Code, Codex, …) working in this repo.
`CLAUDE.md` is a symlink to this file, so every backend reads the same guidance.

## What this is

A client–server CLI that manages a swarm of coding agents. The CLI talks to a
background HTTP server (`localhost:4526`) that spawns agent processes the
moment a task is created or replied to, reaps them when they finish, and
persists state to `~/.ilan/`. Each task runs on a pluggable
**backend** — Claude Code (`claude -p`) or Codex (`codex exec`). See `README.md`
for the user-facing overview and `docs/reference.md` for the full behavioural
reference.

## Layout

- `src/ilan/cli.py` — Click commands (client side).
- `src/ilan/client.py` — thin HTTP client to the server.
- `src/ilan/server.py` — HTTP routes + reaper loop.
- `src/ilan/runner.py` — spawns / kills / reaps agents; builds prompts.
- `src/ilan/backends/` — one adapter per engine (`base.py`, `claude.py`, `codex.py`).
- `src/ilan/models.py` — `Task`, `TaskStatus`, engine constants.
- `src/ilan/store.py` — JSON task store + per-task JSONL logs.
- `tests/` — pytest suite (`pyproject.toml` sets `pythonpath=["src"]`).
- `docs/reference.md` — every command, flag, and config key in full; `README.md`
  is the short overview.

## Working here

- Environment is managed with `uv`. Run the test suite with `uv run pytest -q`.
- Keep imports at the top of the module; annotate function signatures.
- Add a backend by subclassing `Backend` in `src/ilan/backends/` and registering
  it in the runner's engine map; give it its own entry in `VALID_ENGINES`.
- A task keeps a *separate* native session per engine plus a unified conversation
  log; never assume a single session. When touching switch/catch-up logic, keep
  the per-engine `log_cursors` and `awaiting_catchup` invariants intact.
- A task whose name starts with `BURNABLE_PREFIX` (`xxx-`) is deleted by
  `done`/`discard` instead of closed. Burnability is derived from the current
  name and stored on the task nowhere, which is what lets `rename` move a task
  in and out of it; keep it that way rather than caching a flag.

## Changes

- Small, single-purpose commits with a clear "why". When behavior changes,
  update `docs/reference.md` (always) and `README.md` (only if the overview,
  a command table row, or a config key is affected).
- Don't add parallel shorthand commands for a canonical one.
