# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's **Report a vulnerability** button (Security → Advisories). Do not open a public issue. You should get a reply within 7 days.

## Threat model

AgentHub lets one AI agent start other AI agents on your computer. Those agents can read files, edit files and run commands. That is the point, and it is also the risk.

**Trusted:** the human at the keyboard, the files they own, `~/.agenthub/config.json`, the agent CLIs they installed.

**Untrusted:** everything a model says. Claude Code reads web pages, issues, emails and chat messages. Any of these can carry a prompt injection that makes it call AgentHub. Agent output is untrusted too.

### What AgentHub enforces

| Risk | Control |
|---|---|
| Model runs an agent with no sandbox | `permission_mode` defaults to `workspace-write`; `full` needs `allow_full_access: true`, which only a human can set |
| Model grants itself more permissions | No tool edits agent permissions. `agenthub approve` is CLI-only |
| Agent works outside the project | `workdir` must resolve (symlinks included) inside `trusted_workspaces` and outside `denied_paths` |
| Argument / option injection | argv only, never a shell; prompts go after `--` or as `--flag=value`; IDs and model names checked against strict patterns |
| Agent reads the MCP pipe | Child stdin is `/dev/null` |
| Agent → hub → agent loops | `AGENTHUB_DEPTH` env var, capped by `max_delegation_depth` |
| Runaway processes | Hard timeouts; each task runs in its own process group, and cancel/timeout kills the whole group |
| Resource exhaustion | `max_concurrent_tasks`, prompt/output/body size limits |
| Data leaks via shared temp dirs | All state in `~/.agenthub` (0700 dir, 0600 files) |
| Silent misuse | Every call is audited; the hash chain detects edited or deleted entries (`agenthub audit --verify`) |
| Browser attacks on the HTTP API | Loopback only by default, Host allow-list (DNS rebinding), Origin allow-list (CSRF), bearer token in the header only, JSON-only POSTs, failed-auth rate limit |
| Agent reads your cloud credentials | Minimal environment by default: `AWS_*`, `GH_TOKEN` etc. are only passed if listed in `env_passthrough` |
| Delegated agent clobbers your files | `isolation:"worktree"`: the agent edits a private checkout, and nothing reaches your tree until `apply_task` |
| Delegated Claude uses your other MCP servers | Claude runs with `--strict-mcp-config` (no MCP servers) |
| Agent keeps running after the client leaves | In-flight `ask` runs are killed on shutdown; background tasks have a hard timeout |
| Malicious custom agent spec | Specs must be owned by you and not group/world-writable; built-in names can't be overridden |

### What AgentHub does not do

- **It is not a sandbox.** `read-only` and `workspace-write` are enforced by each agent's own sandbox (Codex's Landlock/Seatbelt sandbox, Antigravity's `--sandbox`, …). Aider and Goose have no OS sandbox; their modes limit what the tool is allowed to do, not what the OS permits.
- **The audit log is tamper-evident, not tamper-proof.** Anyone who can write the file can rebuild the chain. Forward it elsewhere if you need more.
- **It does not filter prompts or outputs.** Treat agent replies as untrusted data.
- **The stdio MCP server has no authentication.** It trusts its parent process, as every stdio MCP server does.

### Recommendations

1. Set `trusted_workspaces` to your code folders, not `~`.
2. Leave `allow_full_access` off unless agents run in a throwaway VM or container.
3. Auto-approve only the read-only tools in Claude Code (see README). Keep confirmation prompts for `ask`, `start_task`, `apply_task`, `review`, `compare` and `send_message`.
4. Use `isolation:"worktree"` (or `/delegate`) for any delegated change.
5. Run `agenthub doctor` after upgrades.
