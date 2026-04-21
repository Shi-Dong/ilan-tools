You are summarizing the conversation log of a Claude Code agent that was
dispatched by the `ilan` CLI to work on a software engineering task.

Write the summary as a concise markdown document that a human teammate can
skim in 30 seconds to understand what happened. The document will be opened
in a text editor, so use plain markdown — no HTML, no emoji.

Your summary MUST include the following sections, in this order:

## Summary

A 2–5 sentence plain-English description of what the task was about and what
the agent ended up doing. Lead with the outcome (e.g. "PR opened", "Blocked
waiting on user", "Job launched on k8s"). If the task is still in progress
or blocked, say so clearly.

## PRs

A bullet list of every GitHub pull request URL the agent produced or
referenced as a direct output of this task. Use the full
`https://github.com/<org>/<repo>/pull/<n>` form. If the PR URL cannot be
recovered from the log but a branch was pushed, list the branch URL
instead. If no PR was produced, write a single line: `- (none)`.

## Wandb runs

A bullet list of every Weights & Biases run or job URL the agent launched
or monitored. Include the run name next to the URL when available, e.g.
`- 260420-glm47-flash — https://wandb.ai/<entity>/<project>/runs/<id>`. If
no wandb run was touched, write a single line: `- (none)`.

## Key actions

A short bullet list (3–8 bullets) of the most important things the agent
did — files changed, commands run, decisions made, blockers hit. Be
specific and use backticks for paths, commands, and identifiers. Skip
trivia like `ls` or `git status`.

## Open threads

A bullet list of anything left unfinished: unanswered questions, follow-up
work, unresolved errors, or user decisions the agent is waiting on. If
there is nothing open, write `- (none)`.

---

Rules:

- Extract links verbatim from the log — do not invent URLs.
- Prefer quoting the agent's own words for decisions and blockers, rather
  than paraphrasing.
- Do not copy large code blocks from the log. Reference the file path and
  describe the change instead.
- Keep the whole summary under ~400 words.
- Output only the markdown summary itself. Do not wrap it in code fences
  and do not add a preamble like "Here is the summary:".
