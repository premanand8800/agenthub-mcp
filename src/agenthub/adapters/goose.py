"""Goose (`goose`)."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from agenthub.adapters.base import Adapter, Command, RunSpec, clip
from agenthub.errors import NotFound


def _sessions_dir() -> str:
    return os.path.expanduser("~/.local/share/goose/sessions")


class GooseAdapter(Adapter):
    name = "goose"
    display_name = "Goose"
    description = "Block's Goose agent, run non-interactively with `goose run`."
    binary = "goose"
    install_hint = "Install: https://block.github.io/goose/docs/getting-started/installation"
    # Goose has no sandbox. GOOSE_MODE=chat disables tools; auto approves everything.
    modes = ("read-only", "full")
    supports_sessions = True
    env_allow = ("GOOSE_*", "*_API_KEY", "OPENAI_*", "ANTHROPIC_*", "OLLAMA_*")

    _MODE_ENV = {"read-only": "chat", "full": "auto"}

    def build_command(self, spec: RunSpec) -> Command:
        env = {"GOOSE_MODE": self._MODE_ENV[spec.mode]}
        if spec.model:
            env["GOOSE_MODEL"] = spec.model
        return Command(argv=[self.require_executable(), "run", f"--text={spec.prompt}"], env=env)

    def list_sessions(self, limit: int) -> List[Dict[str, Any]]:
        d = _sessions_dir()
        if not os.path.isdir(d):
            return []
        entries = sorted(os.scandir(d), key=lambda e: e.stat().st_mtime, reverse=True)
        return [{"session_id": e.name, "title": e.name, "updated_at": e.stat().st_mtime} for e in entries[:limit]]

    def search_sessions(self, query: str, limit: int) -> List[Dict[str, Any]]:
        d, ql, results = _sessions_dir(), query.lower(), []
        if not os.path.isdir(d):
            return results
        for e in os.scandir(d):
            try:
                with open(e.path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if ql in line.lower():
                            results.append({"session_id": e.name, "snippet": clip(line.strip(), 200)})
                            break
            except OSError:
                continue
            if len(results) >= limit:
                break
        return results

    def get_transcript(self, session_id: str, max_steps: int) -> Dict[str, Any]:
        path = os.path.join(_sessions_dir(), session_id)
        if not os.path.isfile(path):
            raise NotFound(f"No Goose session '{session_id}'")
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return {
            "session_id": session_id,
            "total_lines": len(lines),
            "content": clip("".join(lines[-max_steps * 5 :]), 20000),
        }
