"""Agent registry: built-in adapters plus custom JSON specs from ~/.agenthub/agents/."""

from __future__ import annotations

import glob
import json
import os
from typing import Dict, List, Optional

from agenthub.adapters.aider import AiderAdapter
from agenthub.adapters.antigravity import AntigravityAdapter
from agenthub.adapters.base import Adapter, Command, Parsed, RunSpec
from agenthub.adapters.claude import ClaudeAdapter
from agenthub.adapters.codex import CodexAdapter
from agenthub.adapters.generic import GenericAdapter, SpecError
from agenthub.adapters.goose import GooseAdapter

__all__ = ["Adapter", "Command", "Parsed", "Registry", "RunSpec"]


def _builtin() -> List[Adapter]:
    return [ClaudeAdapter(), CodexAdapter(), AntigravityAdapter(), AiderAdapter(), GooseAdapter()]


class Registry:
    def __init__(self, custom_dir: Optional[str] = None, adapters: Optional[List[Adapter]] = None):
        self.adapters: Dict[str, Adapter] = {a.name: a for a in (adapters if adapters is not None else _builtin())}
        # Problems found while loading custom specs. Shown by `agenthub doctor` and list_agents.
        self.load_errors: List[str] = []
        if custom_dir:
            self._load_custom(custom_dir)

    def _load_custom(self, directory: str) -> None:
        if not os.path.isdir(directory):
            return
        for path in sorted(glob.glob(os.path.join(directory, "*.json"))):
            st = os.stat(path)
            # A spec chooses which binary runs, so it is code. Only trust files only you can edit.
            if st.st_uid != os.getuid() or st.st_mode & 0o022:
                self.load_errors.append(
                    f"{path}: skipped, must be owned by you and not group/world-writable (fix: chmod 600 {path})"
                )
                continue
            try:
                with open(path, encoding="utf-8") as f:
                    adapter = GenericAdapter(json.load(f))
            except (OSError, ValueError, SpecError) as e:
                self.load_errors.append(f"{path}: {e}")
                continue
            if adapter.name in self.adapters:
                self.load_errors.append(f"{path}: name '{adapter.name}' is already taken")
                continue
            self.adapters[adapter.name] = adapter

    def get(self, name: str) -> Optional[Adapter]:
        return self.adapters.get(name.lower()) if isinstance(name, str) else None

    def names(self) -> List[str]:
        return sorted(self.adapters)

    def all(self) -> List[Adapter]:
        return [self.adapters[n] for n in self.names()]
