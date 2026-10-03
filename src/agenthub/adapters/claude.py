"""Anthropic Claude Code (`claude -p`). Lets other agents and scripts delegate to Claude."""

from __future__ import annotations

import json

from agenthub.adapters.base import Adapter, Command, Parsed, RunSpec


class ClaudeAdapter(Adapter):
    name = "claude"
    display_name = "Claude Code"
    description = "Anthropic Claude Code, run non-interactively with `claude -p`."
    binary = "claude"
    extra_search_paths = ("~/.local/bin/claude", "~/.claude/local/claude")
    install_hint = "Install: https://docs.claude.com/en/docs/claude-code/setup"
    # Claude Code's permission modes are enforced by its tool layer, not an OS sandbox.
    modes = ("read-only", "workspace-write", "full")
    supports_resume = True
    supports_output_schema = True
    supports_add_dirs = True
    env_allow = ("ANTHROPIC_*", "CLAUDE_CODE_*", "CLAUDE_CONFIG_DIR")

    _MODE = {"read-only": "plan", "workspace-write": "acceptEdits", "full": "bypassPermissions"}

    def build_command(self, spec: RunSpec) -> Command:
        argv = [
            self.require_executable(),
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            self._MODE[spec.mode],
            # No MCP servers in the delegated session: it must not send email, chat, or call AgentHub again.
            "--strict-mcp-config",
        ]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.session_id:
            argv += ["--resume", spec.session_id]
        for d in spec.add_dirs:
            argv += ["--add-dir", d]
        if spec.schema_file:
            with open(spec.schema_file, encoding="utf-8") as f:
                argv += ["--json-schema", f.read()]
        argv += ["--", spec.prompt]
        return Command(argv=argv)

    def parse_output(self, stdout: str) -> Parsed:
        text = stdout.strip()
        start = text.find("{")
        if start < 0:
            return Parsed()
        try:
            d = json.loads(text[start:])
        except ValueError:
            return Parsed()
        p = Parsed(
            reply=d.get("result"),
            session_id=d.get("session_id"),
            usage=d.get("usage"),
            cost_usd=d.get("total_cost_usd"),
            structured=d.get("structured_output"),
        )
        if d.get("is_error"):
            p.error = d.get("result") or d.get("subtype") or "Claude reported an error"
        return p
