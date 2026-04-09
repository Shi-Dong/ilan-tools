# Ilan CLI

Ilan CLI is a tool that manages and runs a swarm of Claude Code agents on a list of user-defined tasks. The Claude Code sessions are launched and resumed with the non-interactive `-p` flag.

The CLI maintains a concurrently running agents. When an agent stops working on a task (either is blocked or finishes the task), it can claim the next unclaimed task and start working on it.

The CLI backend should be implemented in Python as much as possible. There should be a top-level `pyproject.toml` file that helps `uv` to install the dependencies.

The name of the runnable binary is `ilan`, and it is stored in `.venv/bin/` after the user runs `uv` to install the dependencies

## How it works

1. The user can define tasks for Claude Code agents. The tasks are passed to `ilan` either through a file (e.g. task.md), or a simple string on the command line. The `ilan` CLI maintains the set of tasks.  Each task is associated with a short name (e.g. `my-task`).
2. Whenever the current number of running agents is smaller than `num-agents`, `ilan` spawns a new agent by running `claude -p` in the background, passing in the essential flags (e.g. `--dangerously-skip-permissions`) as well as the task prompt.
3. The `ilan` CLI keeps track of the status of each task. Each task can have one of the following statuses:
  - `UNCLAIMED`: it hasn't been worked on yet, due to the number of running agents being at maximum.
  - `WORKING`: an agent is actively working on the task.
  - `NEEDS_ATTENTION`: an agent has worked on the task and has decided to stop because it requires the user to step in, either to make a decision or to provide suggestions. A task cannot be claimed by an agent in this status.
  - `AGENT_FINISHED`: an agent has stopped working on the task because it thinks that the task has finished, and has (optionally) generated a deliverable. A task cannot be claimed by an agent in this status.
  - `DONE`: the user reviewed the final deliverable of a finished task and has decided that there is nothing more for the agent to work on the task. The task can no longer be claimed by an agent.
  - `DISCARDED`: the user has decided to terminate the task for whatever reason. The task can no longer be claimed by an agent.
  - `ERROR`: something happened to the `claude -p` process (e.g. the process is killed, or times out). The user can still check the conversation history of the task and revive it for an agent to claim (essentially moving the status to `UNCLAIMED`).
4. The logs of each task are essentially an interleaved conversation between the user and the Claude Code agent. Note that since `-p` runs Claude Code in a non-interactive way, the intermediate thinking of the agent will not be displayed.  So the logs should look like
```
User: <task description>

Assistant: <I-am-blocked-because-I-need-something>

User: <trying-to-deblock>

User: <some-intermediate-response>

Assistant: <ok-I-am-done>
```
Note that you can use whatever format to store the logs (e.g. `json` or `jsonl`). Also note that there could be multiple consecutive User messages since the user is allowed to provide responses at any time.
5. Each task is associated with a unique Claude Code session id. The user can respond to an ongoing task and the user's response will be passed into the Claude Code session through the `--resume` flag of Claude Code, together with the Claude Code session id.
6. Always run `claude` with `--dangerously-skip-permissions`, `--model opus`, and `--effort high` flags.

## Architecture

`ilan` uses a client-server model.  A lightweight HTTP server runs in the background on `127.0.0.1` (ephemeral port) and owns two responsibilities:

1. **Scheduling loop** — a background thread that runs every ~3 seconds: reaps finished `claude -p` processes, updates task statuses, and spawns new agents for unclaimed tasks up to the `num-agents` limit.
2. **API server** — handles requests from the CLI over HTTP/JSON.  All state mutations (add task, reply, mark done, etc.) go through the server so the scheduler sees changes immediately.

The server is started **automatically** the first time any `ilan` command runs.  Its PID and port are written to `<workdir>/server.pid`.  Subsequent commands read this file, verify the process is alive, and connect.  If the server is gone, the next command restarts it transparently.

## Desired behaviors

* Important configs of `ilan`:
    * `workdir`: where all data of `ilan` should be saved (tasks, task trajectories, etc)
    * `num-agents`: the number of maximally allowed concurrent agents (default to 5)
    * `time-zone`: the time zone for all displayed timestamps (default to Pacific Time)
    * `editor`: the editor for viewing logs (default to `emacs`)

* `ilan config set`: set the value of an `ilan` config

* `ilan task add`: add a task to the task list. 
    * `--name`/`-n`: required flag: the short name of the task.
    * `--file`/`-f`: optional flag: the path to a file that stores the prompt of the task.
    * `--description`/`-d`: optional flag: the string description of the task.
    * Exactly one of `-f` and `-d` should be set.
    * If the task short name has already been used (even if the task with the same name is `DONE` or `DISCARDED`), prompt a warning to the user with the existing task's name and status, and reject the new task creation.

* `ilan task ls`: show the list of tasks, including their short names, status, and creation time.
    * By default this shows all tasks whose status is neither `DONE` nor `DISCARDED`.
    * If `-a` flag is passed in, show all tasks regardless of status.

* `ilan task show <task-short-name>`: show the task prompt defined by the given short name.
    * Always show the full prompt of the task, even if the prompt was passed in through a file.
    * Prompt a warning if task not found.

* `ilan task tail <task-short-name>`: show the logs of the given task after (and including) the last Assistant message.
    * If there is no Assistant message, prompt a warning to the user.
    * If the final message is from Assistant, then just show the final message.
    * If the final message is from User, then show the final Assistant message together with all User messages after it.
    * Prompt a warning if task not found.

* `ilan task reply <task-short-name> "user response"`: pass the user's response to a task and make the task claimable again.
    * Prompt a warning if the task is not found, or is in `DONE` or `DISCARDED` status. Do nothing.
    * If the task is `UNCLAIMED`, prompt a warning and append the user's response to the cache.
    * If the task is `WORKING`, immediately run `claude -p --resume` with the task session id and the user's response to interrupt the current session.
    * Otherwise, cache the user's response and set the task status to `UNCLAIMED`.  Next time an agent is available, prompt it with all cached user responses joined together through double linebreaks.

* `ilan task log <task-short-name>`: Open the logs of a task in read-only mode through the configured editor.
    * `ilan task logs` should function in exactly the same way as `ilan task log`.

* `ilan task rm <task-1> <task-2>`: Remove one or more tasks from the task list, free the task name(s), and delete all information of the task (include logs).
    * Prompt a confirmation request unless a `-y` flag is set.

* `ilan task done <task-1>`: Mark a task as `DONE`.

* `ilan task discard <task-1>`: Mark a task as `DISCARDED`.

* `ilan task undone <task-1>`: Move the status of the task from `DONE` to `NEEDS_ATTENTION`.
    * Prompt a warning and do nothing if the status of the task is not `DONE`.

* `ilan task undiscard <task-1>`: Same as `ilan task undone <task-1>` except for discarded tasks.

* `ilan task kill <task-1>`: Kill the running agent of a `WORKING` task and move the task to `ERROR`.
    * Prompt a warning and do nothing if the task is not in `WORKING` status.

* `ilan server stop`: Stop the background ilan server.  Running agents are left alive (they are independent processes) but no new agents will be spawned and no reaping will occur until the server restarts.

* `ilan server status`: Show whether the ilan server is currently running, and if so, its PID and port.

* `ilan clear-everything`: Remove all tasks and their information, start from scratch.
    * Prompt a confirmation.  This cannot be sidestepped by `-y` flag.
