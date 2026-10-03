"""Adapter interface. An adapter turns a request into argv for one agent CLI and reads its output.

Adapters never run anything themselves. `agenthub.hub.Hub` validates input, applies policy,
runs the command and writes the audit log, so every agent gets the same protections.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agenthub.errors import Unavailable


@dataclass
class RunSpec:
    """Everything an adapter needs to build one agent invocation. Already validated by the Hub."""

    prompt: str
    mode: str
    workdir: str
    model: Optional[str] = None
    session_id: Optional[str] = None
    # File the agent should write its final answer to (if the CLI supports it).
    output_file: Optional[str] = None
    # JSON Schema file the final answer must follow (if supports_output_schema).
    schema_file: Optional[str] = None
    add_dirs: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)


@dataclass
class Command:
    argv: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    output_file: Optional[str] = None


@dataclass
class Parsed:
    """What an adapter could extract from the agent's stdout."""

    reply: Optional[str] = None
    session_id: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None
    cost_usd: Optional[float] = None
    error: Optional[str] = None
    structured: Any = None


class Adapter:
    name: str = "base"
    display_name: str = "Base"
    description: str = ""
    binary: str = ""
    extra_search_paths: Sequence[str] = ()
    install_hint: str = ""
    # Permission modes this agent can enforce. See README "Permission modes".
    modes: Tuple[str, ...] = ()
    supports_sessions: bool = False
    supports_resume: bool = False
    supports_send_message: bool = False
    supports_output_schema: bool = False
    supports_add_dirs: bool = False
    supports_images: bool = False
    # Environment variables (glob patterns) this agent needs on top of process.BASE_ENV_ALLOW.
    env_allow: Tuple[str, ...] = ()

    # -- discovery -----------------------------------------------------------

    def executable(self) -> Optional[str]:
        found = shutil.which(self.binary)
        if found:
            return found
        for p in self.extra_search_paths:
            p = os.path.expanduser(p)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None

    def require_executable(self) -> str:
        exe = self.executable()
        if not exe:
            raise Unavailable(f"{self.display_name} is not installed. {self.install_hint}".strip())
        return exe

    def info(self) -> Dict[str, Any]:
        exe = self.executable()
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "installed": exe is not None,
            "path": exe,
            "install_hint": None if exe else self.install_hint,
            "permission_modes": list(self.modes),
            "supports": {
                "sessions": self.supports_sessions,
                "resume": self.supports_resume,
                "send_message": self.supports_send_message,
                "output_schema": self.supports_output_schema,
                "add_dirs": self.supports_add_dirs,
                "images": self.supports_images,
            },
        }

    # -- execution -----------------------------------------------------------

    def build_command(self, spec: RunSpec) -> Command:
        raise NotImplementedError

    def parse_output(self, stdout: str) -> Parsed:
        """Extract reply, session ID and usage from stdout. Default: stdout is the reply."""
        return Parsed()

    def format_log(self, text: str) -> str:
        """Make a raw task log readable. Default: unchanged."""
        return text

    def build_send_message(self, session_id: str, message: str) -> Command:
        raise Unavailable(f"{self.display_name} does not support sending messages to a live session")

    # -- history (read-only) ---------------------------------------------------

    def list_sessions(self, limit: int) -> List[Dict[str, Any]]:
        return []

    def search_sessions(self, query: str, limit: int) -> List[Dict[str, Any]]:
        return []

    def get_transcript(self, session_id: str, max_steps: int) -> Dict[str, Any]:
        raise Unavailable(f"{self.display_name} does not expose session transcripts")

    def list_models(self) -> List[Dict[str, Any]]:
        return []


def clip(text: Any, limit: int = 4000) -> Any:
    """Keep single transcript fields from flooding the caller's context window."""
    if isinstance(text, str) and len(text) > limit:
        return text[:limit] + f" [... {len(text) - limit} chars truncated]"
    return text
