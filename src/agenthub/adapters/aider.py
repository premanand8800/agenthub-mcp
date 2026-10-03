"""Aider (`aider`)."""

from __future__ import annotations

from agenthub.adapters.base import Adapter, Command, RunSpec


class AiderAdapter(Adapter):
    name = "aider"
    display_name = "Aider"
    description = "Aider git-native pair programmer, run non-interactively with `aider --message`."
    binary = "aider"
    install_hint = "Install: pipx install aider-chat"
    # read-only uses --dry-run. Aider has no OS sandbox, so workspace-write means "edit files,
    # never run shell commands", and full means "auto-confirm everything".
    modes = ("read-only", "workspace-write", "full")
    env_allow = ("*_API_KEY", "AIDER_*", "OPENAI_*", "ANTHROPIC_*", "GEMINI_*", "OPENROUTER_*", "OLLAMA_*")

    _MODE_FLAGS = {
        "read-only": ["--dry-run", "--yes-always", "--no-suggest-shell-commands"],
        "workspace-write": ["--yes-always", "--no-suggest-shell-commands"],
        "full": ["--yes-always"],
    }

    def build_command(self, spec: RunSpec) -> Command:
        argv = [
            self.require_executable(),
            *self._MODE_FLAGS[spec.mode],
            "--no-auto-commits",
            "--no-pretty",
            "--no-check-update",
            "--no-show-release-notes",
        ]
        if spec.model:
            argv.append(f"--model={spec.model}")
        argv.append(f"--message={spec.prompt}")
        return Command(argv=argv)
