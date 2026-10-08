# AgentHub

[![CI](https://github.com/premanand8800/agenthub-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/premanand8800/agenthub-mcp/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-0-brightgreen)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/premanand8800/agenthub-mcp/blob/main/LICENSE)

[![AgentHub demo: Codex fixes a bug in an isolated worktree, Claude reviews the patch, it is applied and tests pass](https://raw.githubusercontent.com/premanand8800/agenthub-mcp/main/docs/demo.gif)](https://github.com/premanand8800/agenthub-mcp/releases/download/v1.2.4/agenthub-demo-vo.mp4)

▶ **[Watch the demo in full quality (MP4, 34 s, with voiceover)](https://github.com/premanand8800/agenthub-mcp/releases/download/v1.2.4/agenthub-demo-vo.mp4)**: a real run, not a mock-up.

**Let Claude Code hand work to other coding agents (Codex, Antigravity, Claude, Aider, Goose, or any CLI) without handing them your whole machine.**

AgentHub is a small MCP server, CLI and optional HTTP API. Use it to:

- **Get a second opinion.** Send one question to several agents and compare the answers (`compare`).
- **Get a cross-model review** of your diff before you commit (`review`).
- **Delegate safely.** An agent works in its own git worktree. You see the diff, then apply or discard it.
- **Use the subscriptions you already pay for.** ChatGPT through Codex, Google through Antigravity, all from one place, with automatic fallback when one runs out of quota.
- **Run long jobs in the background,** wait for them, read readable logs.
- **Search other agents' past sessions,** or continue them.

Every request goes through one policy layer: trusted folders, permission modes, a minimal environment, timeouts, quota tracking and a hash-chained audit log.

Pure Python standard library. No dependencies. Linux and macOS. About 20 MB of RAM.

---

## Quick start

```bash
pipx install agenthub-gateway       # or: uv tool install agenthub-gateway
                                     # or run without installing: uvx agenthub-gateway doctor
agenthub config --init               # writes ~/.agenthub/config.json
$EDITOR ~/.agenthub/config.json      # set "trusted_workspaces": ["~/code"]
agenthub doctor
```

Then connect it to Claude Code in **one** of two ways:

```bash
# A) Plugin: MCP server plus slash commands (/second-opinion, /cross-review, /delegate, /agent-tasks)
#    Starts the server with `uvx agenthub-gateway mcp`, so it needs uv (https://docs.astral.sh/uv/).
claude plugin marketplace add premanand8800/agenthub-mcp
claude plugin install agenthub@agenthub

# B) MCP server only
agenthub install-claude
```

Restart Claude Code and try:

> /cross-review codex

> /delegate codex add input validation to the signup handler and run its tests

> Use agenthub to compare codex and claude on: what's the safest way to migrate this table?

---

## How a delegated change works

```text
start_task(isolation="worktree")  →  agent edits a private checkout (your files are untouched)
wait_task                         →  blocks until done; returns final output, session_id, token usage
get_task_diff                     →  file list + patch
review(task_id=…)                 →  optional: another model reviews that patch
apply_task  |  discard_task       →  patch lands in your working tree (uncommitted), or is thrown away
```

The worktree starts from your **current** state, including uncommitted changes to tracked files. Build junk (`__pycache__`, `node_modules`, …) is left out of the diff. If your files changed in the meantime, `apply_task` tries a 3-way merge. If that conflicts, it keeps the worktree so you can resolve it by hand.

---

## Permission modes

| Mode | Meaning | Codex | Claude | Antigravity | Aider | Goose |
|---|---|---|---|---|---|---|
| `read-only` | Inspect only | `-s read-only` | `plan` | `--mode plan --sandbox` | `--dry-run` | `GOOSE_MODE=chat` |
| `workspace-write` **(default)** | Edit files in `workdir` | `-s workspace-write` | `acceptEdits` | `--mode accept-edits --sandbox` | no shell commands | – |
| `full` | No sandbox, no approvals | `--dangerously-bypass-…` | `bypassPermissions` | `--dangerously-skip-permissions` | `--yes-always` | `GOOSE_MODE=auto` |

- `full` is **off** until a human sets `"allow_full_access": true`. A model cannot turn it on.
- `review` and `compare` always run `read-only`.
- If an agent can't enforce a mode, AgentHub refuses the request instead of quietly running with weaker settings.
- A delegated Claude runs with `--strict-mcp-config`, so it can't use your other MCP servers (email, chat, AgentHub itself).

---

## MCP tools

| Tool | What it does | Read-only |
|---|---|---|
| `list_agents` | Installed agents, modes, features, **quota health** | yes |
| `ask` | Run a prompt and wait. Returns `reply`, `session_id`, `usage`, `cost_usd` | no |
| `start_task` | Background job, optionally `isolation:"worktree"` | no |
| `wait_task` | Block until a task finishes (up to 10 min per call) | yes |
| `get_task` / `get_task_logs` / `list_tasks` | Status, final output, readable logs | yes |
| `cancel_task` | Kill a task and all its child processes | no |
| `get_task_diff` | What a worktree task changed | yes |
| `apply_task` / `discard_task` | Land or drop a worktree task's changes | no |
| `review` | Read-only review of `git diff <base>` or of a task's diff | yes* |
| `compare` | Same prompt to 2–5 agents in parallel | yes* |
| `list_sessions` / `search_sessions` / `get_transcript` | Other agents' history | yes |
| `list_models` | Model IDs per agent | yes |
| `send_message` | Message a live session | no |

\* These don't change your files, but they do send code to the agents' vendors and use quota.

**Options for `ask` and `start_task`:** `workdir`, `permission_mode`, `model`, and `session_id` (continue a conversation). Also:

- `add_dirs`: extra directories
- `images`: Codex only
- `output_schema`: a JSON Schema; the answer comes back parsed in `structured`
- `fallback_agents`: other agents to try if this one is out of quota or not installed

**Long calls** send MCP progress notifications every 10 s when the client asks for them.

To skip permission prompts for tools that only read:

```jsonc
// ~/.claude/settings.json
{ "permissions": { "allow": [
  "mcp__agenthub__list_agents", "mcp__agenthub__wait_task", "mcp__agenthub__get_task",
  "mcp__agenthub__get_task_logs", "mcp__agenthub__list_tasks", "mcp__agenthub__get_task_diff",
  "mcp__agenthub__list_sessions", "mcp__agenthub__search_sessions", "mcp__agenthub__get_transcript",
  "mcp__agenthub__list_models"
] } }
```

---

## Reliability

- **Quota-aware.** When a provider reports a quota or rate limit, AgentHub records it, including the reset time when the message gives one ("Resets in 137h"). That agent then fails fast with `quota_exhausted` until the reset time. `fallback_agents` moves on to the next agent automatically. `list_agents` shows each agent's health.
- **No unsafe retries.** An agent run that failed partway may already have edited files, so AgentHub never retries it automatically. Falling back to another agent only happens when nothing ran.
- **Survives restarts.** Background tasks keep running if Claude Code closes, and their status still works from any process. In-flight `ask` calls are killed when the client disconnects, so nothing keeps spending.
- **Global limits.** `max_concurrent_tasks` holds across every Claude Code session, enforced with a file lock.

---

## CLI (also built for agents)

Every command is non-interactive, has `--json`, and uses stable exit codes. Other agents can call AgentHub from their own terminals.

```text
agenthub agents [--json]
agenthub ask codex "explain main.py" -m read-only [--session ID] [--schema s.json] [--fallback claude]
agenthub run codex "add tests" --isolation worktree      → prints task ID
agenthub wait ID [--timeout 600]      exit 0 succeeded · 1 failed · 3 still running
agenthub diff ID | apply ID | discard ID
agenthub review codex [--base main | --task ID]
agenthub compare codex,claude "which approach is safer?"
agenthub tasks | task ID | logs ID | cancel ID
agenthub audit [--verify] · config [--init] · token [--rotate] · serve · doctor
agenthub approve antigravity "Bash(npm test)"     human-only
```

Exit codes: `0` ok · `1` the agent or task failed · `2` usage error · `3` still running (for `wait`).

---

## Configuration

`~/.agenthub/config.json`. Every key is optional. Unknown keys are an error, so typos don't fail silently. Set `AGENTHUB_HOME` to move the whole directory.

| Key | Default | |
|---|---|---|
| `trusted_workspaces` | `["~"]` | Agents may only run inside these. **Narrow this.** |
| `denied_paths` | `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.config/gcloud`, `~/.kube`, `~/.agenthub` | Always refused |
| `default_permission_mode` | `workspace-write` | |
| `allow_full_access` | `false` | Allows `full` mode |
| `env_passthrough` | `[]` | Extra env vars for agents (globs), e.g. `["GH_TOKEN"]` |
| `inherit_env` | `false` | `true` passes your entire environment (not recommended) |
| `ask_timeout_seconds` / `task_timeout_seconds` | `900` / `14400` | |
| `max_concurrent_tasks` | `4` | Across all sessions |
| `max_prompt_chars` / `max_output_chars` | `100000` / `60000` | |
| `max_delegation_depth` | `2` | Stops agent → hub → agent loops |
| `quota_backoff_seconds` | `900` | How long to skip an agent after a quota error with no reset time |
| `audit_prompt_preview_chars` | `80` | `0` logs only prompt length and SHA-256 |
| `task_retention_days` | `7` | Old tasks and their worktrees are deleted |
| `http_allowed_origins` | `[]` | Browser origins allowed to call the HTTP API |

**Environment:** agents get only what they need:

- `PATH`, `HOME`, locale, proxies, CA bundles
- toolchain variables (nvm, cargo, go, java, venv, …)
- their own API-key variables (`OPENAI_*` for Codex, `ANTHROPIC_*` for Claude, …)

Cloud credentials such as `AWS_*` and `GH_TOKEN` are **not** passed unless you list them in `env_passthrough`.

---

## Custom agents

Put a JSON file in `~/.agenthub/agents/` (mode `600`). See [`examples/custom-agent.json`](https://github.com/premanand8800/agenthub-mcp/blob/main/examples/custom-agent.json).

```json
{
  "name": "myagent",
  "command": "myagent",
  "args": ["run", "--prompt={prompt}"],
  "model_args": ["--model", "{model}"],
  "add_dir_args": ["--add-dir", "{dir}"],
  "output_schema_args": ["--schema", "{schema_file}"],
  "json_output": { "reply": "result", "session_id": "session_id", "cost_usd": "cost" },
  "env_passthrough": ["MYAGENT_API_KEY"],
  "modes": { "read-only": ["--no-write"], "workspace-write": ["--sandbox"] }
}
```

- `modes` lists only what your CLI can really enforce.
- `json_output` tells AgentHub where to find the reply and session ID when your CLI prints JSON.
- Specs that are group- or world-writable are refused, and so are specs that reuse a built-in name. `agenthub doctor` reports why.

---

## HTTP API (optional)

```bash
agenthub serve                                   # 127.0.0.1:8765
TOKEN=$(agenthub token)
curl -s localhost:8765/v1/agents/codex/ask -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' -d '{"prompt":"hi","permission_mode":"read-only"}'
```

| Method | Path |
|---|---|
| GET | `/health` (no auth) · `/v1/agents` · `/v1/agents/{agent}/models` |
| POST | `/v1/agents/{agent}/ask` · `/v1/agents/{agent}/tasks` · `/v1/review` · `/v1/compare` |
| GET | `/v1/agents/{agent}/sessions` · `…/sessions/search?q=` · `…/sessions/{id}` |
| POST | `/v1/agents/{agent}/sessions/{id}/messages` |
| GET | `/v1/tasks` · `/v1/tasks/{id}` · `/v1/tasks/{id}/wait?timeout=` · `/v1/tasks/{id}/logs?tail=` · `/v1/tasks/{id}/diff` |
| POST | `/v1/tasks/{id}/cancel` · `/v1/tasks/{id}/apply` · `/v1/tasks/{id}/discard` |

Errors are always `{"ok": false, "error": {"code", "message"}}`. A `quota_exhausted` error also includes `retry_after_seconds`.

---

## Security model

See [SECURITY.md](https://github.com/premanand8800/agenthub-mcp/blob/main/SECURITY.md). In short:

- **The model is untrusted.** Defaults assume a prompt injection will reach AgentHub. Every agent run is sandboxed, limited to trusted folders, given a minimal environment, time-limited and audited. A model can't enable `full` mode or approve its own tools.
- **No shell, ever.** Prompts can't become CLI options.
- **Private state.** All state lives in `~/.agenthub` (0700), never in `/tmp`.
- **Tamper-evident audit log.** `agenthub audit --verify`.

## Development

```bash
git clone https://github.com/premanand8800/agenthub-mcp && cd agenthub-mcp
PYTHONPATH=src python -m unittest discover -s tests -t tests -v
```

Tests use a fake agent: no API keys, no quota. To add a built-in adapter, subclass `agenthub.adapters.base.Adapter`, then:

- implement `build_command(spec)`, and `parse_output` if your CLI prints JSON
- declare `modes` and `env_allow`
- register it in `adapters/__init__.py`

## Contributing

Issues and PRs welcome — see [CONTRIBUTING.md](https://github.com/premanand8800/agenthub-mcp/blob/main/CONTRIBUTING.md). Security reports: [SECURITY.md](https://github.com/premanand8800/agenthub-mcp/blob/main/SECURITY.md).

## License

MIT
