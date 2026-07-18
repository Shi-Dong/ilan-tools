# CLAUDE.md vs AGENTS.md — sharing project context across backends

`ilan` runs each task on a pluggable **agent backend**: Claude Code (`claude -p`)
or Codex (`codex exec`). A single task can even switch between the two
(`ilan task switch-backend`). The two CLIs read their "project context" — the
standing instructions you want the agent to follow — from **different files**:

| Backend      | File it reads        | Global file                |
| ------------ | -------------------- | -------------------------- |
| Claude Code  | `CLAUDE.md`          | `~/.claude/CLAUDE.md`      |
| Codex        | `AGENTS.md`          | `~/.codex/AGENTS.md`       |

Out of the box, **neither backend reads the other's file.** If you only ship a
`CLAUDE.md`, a Codex task starts with no project context, and vice versa. Because
`ilan` lets both backends work the same task, you usually want the *same*
guidance to reach whichever backend happens to be running.

This document explains how the two conventions work and the concrete ways to make
your personalized context transfer between them. It is written to be
project-agnostic — none of it is specific to any one user or repo.

> **How `ilan` fits in.** `ilan` spawns each backend with the working directory
> set to the task's workdir and does **not** inject or rewrite context files. Each
> CLI performs its *own* file discovery from that directory. So "making context
> transfer" is entirely about how you lay out `CLAUDE.md` / `AGENTS.md` on disk
> (and, for Codex, one config key) — not about anything `ilan` does at spawn time.

## How each backend discovers its context

Both tools merge instructions from several locations, from broad to specific.
Later (more specific) files layer on top of earlier ones.

**Claude Code** reads, in order:

1. `~/.claude/CLAUDE.md` — your global, cross-project instructions.
2. `CLAUDE.md` files from the repo root down to the working directory (each
   directory's file applies to everything under it).
3. `CLAUDE.local.md` — an optional, personal, usually git-ignored override that
   lives next to a `CLAUDE.md` (use it for machine-specific or private notes you
   don't want committed).

**Codex** reads, in order:

1. `~/.codex/AGENTS.md` — your global instructions.
2. `AGENTS.md` files from the repo root down to the working directory. More
   deeply nested files take precedence on conflicts. (Codex also honors an
   `AGENTS.override.md` next to an `AGENTS.md` as a local override.)

The mental model is symmetric: **global file in the tool's home dir**, then
**per-repo / per-subtree files merged from the root down to the cwd**. To share
context you make each level resolve to the same content.

## Transfer strategy 1 — symlink one file to the other (recommended)

Keep a single source of truth and symlink the other name to it. This is what the
`ilan-tools` repo itself does: `AGENTS.md` is the real file and `CLAUDE.md` is a
symlink to it, so both backends read identical guidance.

```bash
# At the repo root — make AGENTS.md the source of truth:
ln -s AGENTS.md CLAUDE.md
# …or the other way around if you prefer CLAUDE.md as the source:
ln -s CLAUDE.md AGENTS.md
```

Do the same for the global files if you want cross-project context to reach both:

```bash
mkdir -p ~/.codex
ln -s ~/.claude/CLAUDE.md ~/.codex/AGENTS.md
```

**Pros:** one file to edit; the two can never drift. **Caveats:**

- **Commit the symlink to git.** Git stores symlinks natively as long as
  `core.symlinks=true` (the default on macOS/Linux). Collaborators then get the
  link automatically.
- **Windows / restricted checkouts.** If `core.symlinks=false` (common on
  Windows without Developer Mode, or some CI checkouts), git materializes the
  symlink as a *text file containing the target path* — which is not what either
  backend wants to read. On such setups prefer strategy 2 or 3.
- Pick a single direction repo-wide so contributors know which file to edit.

## Transfer strategy 2 — tell Codex to fall back to CLAUDE.md

If your repos are already standardized on `CLAUDE.md` and you don't want to add
`AGENTS.md` files everywhere, point Codex at the Claude filename via its config.
In `~/.codex/config.toml`:

```toml
# Codex looks for these names when no AGENTS.md is present in a directory:
project_doc_fallback_filenames = ["CLAUDE.md"]
```

Now a Codex task will pick up an existing `CLAUDE.md` at each level when there is
no `AGENTS.md` beside it. This requires **no per-repo files** and no symlinks — a
single global setting covers every project.

**Caveats:**

- It is a *fallback*: if a directory has both, `AGENTS.md` wins and the
  `CLAUDE.md` is ignored for that directory.
- This is a Codex-side setting only; Claude Code has no matching "also read
  AGENTS.md" option, so it does not make the transfer bidirectional. If you have
  `AGENTS.md`-only repos and want Claude to read them, use a symlink (strategy 1)
  or a copy (strategy 3) instead.
- It configures *your* machine. Teammates need the same key in their own
  `~/.codex/config.toml`, so for shared repos a committed symlink is more
  portable.

## Transfer strategy 3 — keep two copies in sync

Commit both `CLAUDE.md` and `AGENTS.md` as real files with identical content.
This is the most portable option (no symlinks, no per-user config) but the two
files can drift, so guard it:

- Add a small check (pre-commit hook or CI step) that fails when the two files
  differ, e.g. `diff -q CLAUDE.md AGENTS.md`.
- Or generate one from the other in your build/docs pipeline and treat the
  generated file as read-only.

Reach for this only when strategies 1 and 2 are ruled out (e.g. Windows
contributors plus repos that must work for both backends without extra config).

## Things to watch out for

- **Size cap.** Codex truncates an over-long project doc (`project_doc_max_bytes`,
  ~32 KB by default). Very large context files may be silently cut off — split
  guidance into nested `AGENTS.md` files, or raise the limit in
  `~/.codex/config.toml`.
- **Switching backends mid-task.** After `ilan task switch-backend`, the incoming
  backend re-discovers context from disk on its next turn. If `CLAUDE.md` and
  `AGENTS.md` disagree, the agent's behavior can visibly change across the switch.
  Keeping them identical (strategy 1 or a synced copy) avoids surprises.
- **Precedence differs per tool.** Both merge root→cwd, but the exact ordering and
  the extra override files (`CLAUDE.local.md` vs `AGENTS.override.md`) are
  tool-specific. Keep shared, committed guidance in the primary file and reserve
  the override files for the machine-local bits that genuinely should differ.
- **MCP servers are configured separately.** Tool integrations (e.g. a shared
  memory server) are *not* part of `CLAUDE.md` / `AGENTS.md`. Claude reads MCP
  config from its own settings (`~/.claude.json` / a project `.mcp.json`); Codex
  reads `[mcp_servers.*]` from `~/.codex/config.toml` (or `codex mcp add …`).
  If you want the same MCP server available to both backends, register it in both
  places.

## Quick recommendation

- **One repo, you control the checkout:** symlink `CLAUDE.md` ⇄ `AGENTS.md`
  (strategy 1). Do the same for the global files.
- **Many `CLAUDE.md`-standardized repos, just your machine:** set
  `project_doc_fallback_filenames = ["CLAUDE.md"]` in `~/.codex/config.toml`
  (strategy 2).
- **Shared repos that must work everywhere without per-user setup:** commit both
  files and add a drift check (strategy 3).
