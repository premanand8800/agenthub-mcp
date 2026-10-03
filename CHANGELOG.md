# Changelog

## 1.2.1

- The CLI is also installed as `agenthub-gateway`, so `uvx agenthub-gateway mcp` runs AgentHub with no install step.
  Package-aware tools such as HOL Guard can then identify the server by its PyPI name.
- The Claude Code plugin starts the server with `uvx agenthub-gateway mcp`.

## 1.2.0

Published on PyPI as `agenthub-gateway` (the name `agenthub-mcp` was too similar to an existing project). The repository name is unchanged.

### Added
- **Worktree isolation:** `start_task(isolation="worktree")` runs the agent in a private git checkout of your current state. New tools `get_task_diff`, `apply_task` (with 3-way fallback) and `discard_task`. Build artifacts are left out of diffs.
- **`review`:** read-only review of `git diff <base>` or of a worktree task's changes by another model.
- **`compare`:** the same prompt to 2–5 agents in parallel, answers side by side.
- **Structured output:** `output_schema` (Codex, Antigravity, Claude, custom agents); the parsed result comes back in `structured`.
- **`add_dirs`** and **`images`** inputs.
- **Claude Code adapter** (`claude -p`): other agents and scripts can delegate to Claude. It runs with `--strict-mcp-config`.
- **Claude Code plugin** with `/second-opinion`, `/cross-review`, `/delegate`, `/agent-tasks`, plus a marketplace manifest.
- CLI: `wait`, `diff`, `apply`, `discard`, `review`, `compare`, plus `--add-dir`, `--image`, `--schema` and `--fallback`.
- HTTP: `/v1/review`, `/v1/compare`, `/v1/tasks/{id}/wait|diff|apply|discard`.
- Custom agent specs: `add_dir_args`, `output_schema_args`, `json_output`, `env_passthrough`.

## 1.1.0

### Reliability
- **Fixed:** `max_concurrent_tasks` was only enforced per process; it now holds across all sessions (file lock). Task status changes are lock-protected too.
- **Fixed:** an `ask` that was still running when the client disconnected kept running. Shutdown (EOF, SIGTERM, SIGHUP) now kills in-flight runs. Background tasks still survive, by design.
- **Quota awareness:** quota and rate-limit errors are detected (with the reset time when given), shared across processes, shown in `list_agents`, and returned as `quota_exhausted` with `retry_after_seconds`.
- **`fallback_agents`:** try the next agent when one is out of quota or not installed. Agent runs are never retried automatically, because a failed run may already have edited files.
- **`wait_task`:** long-poll until a task finishes instead of polling `get_task`.
- MCP progress notifications for long calls.

### Usability
- `ask`, `start_task` and `get_task` return `session_id` (continue with `session_id=`), token `usage` and `cost_usd` where the agent reports them.
- Codex runs with `--json`; task logs are rendered as readable lines (commands, edits, messages).

### Security
- **Minimal environment for agents:** only the basics, toolchains and each agent's own API-key variables. Cloud credentials are no longer passed. New config keys `env_passthrough` and `inherit_env`.
- Lazy imports for a smaller startup footprint.

## 1.0.0

First public release. A rewrite of the 0.x prototype.

### Security
- Agents are sandboxed by default (`workspace-write`). `full` mode needs a human opt-in in config.
- Removed the model-callable `approve_tools`. Use `agenthub approve` from a terminal.
- Working directories are checked against `trusted_workspaces` and `denied_paths`.
- Prompts can no longer be read as CLI options. Session, task and model IDs are validated.
- Agents no longer inherit the MCP server's stdin.
- Task state moved from world-readable `/tmp` to `~/.agenthub/tasks` (0700/0600).
- Hash-chained audit log covering every action, with `agenthub audit --verify`.
- Delegation-depth limit is now enforced.
- HTTP API: no `Access-Control-Allow-Origin: *`, no `?token=`, Host/Origin checks, auth before body, size limit, rate limit.
- `install-claude` uses `claude mcp add` instead of rewriting `~/.claude.json`.

### Reliability
- One bad tool call can no longer crash the MCP server. Errors come back as `isError` results; unknown methods get JSON-RPC errors; `ping` is supported.
- Tool calls run concurrently, so a long `ask` no longer blocks other tools.
- Timeouts for `ask` and background tasks; cancel kills the whole process tree.
- Task status works across processes (MCP, HTTP, CLI) and survives PID reuse.
- A task ID is now the same in the response, the audit log and the files.
- Fixed the crash in the legacy `ask_codex` / `ask_antigravity` aliases (the aliases were removed).

### Usability
- 12 clearly named tools (no duplicate legacy aliases) with strict input schemas and MCP annotations.
- Protocol versions 2024-11-05 through 2025-06-18; server `instructions` for the model.
- New commands: `ask`, `run`, `tasks`, `task`, `logs`, `cancel`, `config`, `approve`, `doctor`.
- Model lists come from the agents themselves instead of hard-coded, outdated lists.
- Installable package (`pipx install agenthub-gateway`), stdlib only, CI on Linux and macOS.
