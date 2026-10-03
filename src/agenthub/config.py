"""Paths and user configuration (~/.agenthub/config.json)."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List

from agenthub.errors import ConfigError

PERMISSION_MODES = ("read-only", "workspace-write", "full")

DEFAULTS: Dict[str, Any] = {
    # Agents may only run inside these directories (symlinks are resolved first).
    "trusted_workspaces": ["~"],
    # ...and never inside these, even when they sit under a trusted workspace.
    "denied_paths": ["~/.ssh", "~/.gnupg", "~/.aws", "~/.config/gcloud", "~/.kube", "~/.agenthub"],
    # Used when a caller does not pass permission_mode.
    "default_permission_mode": "workspace-write",
    # "full" disables the agent's own sandbox and approvals. Off unless the human opts in here.
    "allow_full_access": False,
    "ask_timeout_seconds": 900,
    "task_timeout_seconds": 4 * 60 * 60,
    "max_concurrent_tasks": 4,
    "max_prompt_chars": 100_000,
    "max_output_chars": 60_000,
    # Stops agent -> hub -> agent -> hub ... loops.
    "max_delegation_depth": 2,
    # How many prompt characters the audit log keeps. 0 stores only length and SHA-256.
    "audit_prompt_preview_chars": 80,
    "task_retention_days": 7,
    # Browser origins the HTTP API accepts. Empty means requests carrying an Origin header are refused.
    "http_allowed_origins": [],
    # Agents get a minimal environment (PATH, HOME, locale, proxies, toolchains, their own API keys).
    # Add glob patterns here to pass more, e.g. ["GH_TOKEN", "MY_*"]. Cloud credentials are not passed by default.
    "env_passthrough": [],
    # true = pass your entire environment to agents (old behavior). Not recommended.
    "inherit_env": False,
    # After an agent reports a quota/rate limit with no reset time, skip it for this long.
    "quota_backoff_seconds": 900,
}


def home_dir() -> str:
    """AgentHub state directory. Override with AGENTHUB_HOME."""
    return os.path.realpath(os.path.expanduser(os.environ.get("AGENTHUB_HOME", "~/.agenthub")))


def _expand(paths: List[str]) -> List[str]:
    return [os.path.realpath(os.path.expanduser(p)) for p in paths]


@dataclass
class Config:
    home: str
    trusted_workspaces: List[str]
    denied_paths: List[str]
    default_permission_mode: str
    allow_full_access: bool
    ask_timeout_seconds: int
    task_timeout_seconds: int
    max_concurrent_tasks: int
    max_prompt_chars: int
    max_output_chars: int
    max_delegation_depth: int
    audit_prompt_preview_chars: int
    task_retention_days: int
    http_allowed_origins: List[str]
    env_passthrough: List[str]
    inherit_env: bool
    quota_backoff_seconds: int

    @property
    def config_path(self) -> str:
        return os.path.join(self.home, "config.json")

    @property
    def token_path(self) -> str:
        return os.path.join(self.home, "auth.token")

    @property
    def audit_path(self) -> str:
        return os.path.join(self.home, "audit.log")

    @property
    def tasks_dir(self) -> str:
        return os.path.join(self.home, "tasks")

    @property
    def worktrees_dir(self) -> str:
        return os.path.join(self.home, "worktrees")

    @property
    def health_path(self) -> str:
        return os.path.join(self.home, "health.json")

    @property
    def custom_agents_dir(self) -> str:
        return os.path.join(self.home, "agents")

    def to_public_dict(self) -> Dict[str, Any]:
        return {
            "home": self.home,
            "trusted_workspaces": self.trusted_workspaces,
            "denied_paths": self.denied_paths,
            "default_permission_mode": self.default_permission_mode,
            "allow_full_access": self.allow_full_access,
            "ask_timeout_seconds": self.ask_timeout_seconds,
            "task_timeout_seconds": self.task_timeout_seconds,
            "max_concurrent_tasks": self.max_concurrent_tasks,
            "max_prompt_chars": self.max_prompt_chars,
            "max_output_chars": self.max_output_chars,
            "max_delegation_depth": self.max_delegation_depth,
            "audit_prompt_preview_chars": self.audit_prompt_preview_chars,
            "task_retention_days": self.task_retention_days,
            "http_allowed_origins": self.http_allowed_origins,
            "env_passthrough": self.env_passthrough,
            "inherit_env": self.inherit_env,
            "quota_backoff_seconds": self.quota_backoff_seconds,
        }


_TYPES = {
    "trusted_workspaces": list,
    "denied_paths": list,
    "default_permission_mode": str,
    "allow_full_access": bool,
    "ask_timeout_seconds": int,
    "task_timeout_seconds": int,
    "max_concurrent_tasks": int,
    "max_prompt_chars": int,
    "max_output_chars": int,
    "max_delegation_depth": int,
    "audit_prompt_preview_chars": int,
    "task_retention_days": int,
    "http_allowed_origins": list,
    "env_passthrough": list,
    "inherit_env": bool,
    "quota_backoff_seconds": int,
}


def load_config(home: str | None = None) -> Config:
    home = home or home_dir()
    raw = copy.deepcopy(DEFAULTS)
    path = os.path.join(home, "config.json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                user = json.load(f)
        except (OSError, ValueError) as e:
            raise ConfigError(f"Cannot read {path}: {e}") from e
        if not isinstance(user, dict):
            raise ConfigError(f"{path} must contain a JSON object")
        unknown = sorted(set(user) - set(DEFAULTS))
        if unknown:
            raise ConfigError(f"Unknown key(s) in {path}: {', '.join(unknown)}")
        raw.update(user)

    for key, typ in _TYPES.items():
        value = raw[key]
        # bool is a subclass of int; reject it where a number is expected.
        if not isinstance(value, typ) or (typ is int and isinstance(value, bool)):
            raise ConfigError(f"config.{key} must be of type {typ.__name__}")
        if typ is int and value < 0:
            raise ConfigError(f"config.{key} must be >= 0")
        if typ is list and not all(isinstance(v, str) for v in value):
            raise ConfigError(f"config.{key} must be a list of strings")

    if raw["default_permission_mode"] not in PERMISSION_MODES:
        raise ConfigError(f"config.default_permission_mode must be one of {', '.join(PERMISSION_MODES)}")
    if raw["default_permission_mode"] == "full" and not raw["allow_full_access"]:
        raise ConfigError("config.default_permission_mode is 'full' but allow_full_access is false")
    if not raw["trusted_workspaces"]:
        raise ConfigError("config.trusted_workspaces must not be empty")
    if raw["max_concurrent_tasks"] < 1:
        raise ConfigError("config.max_concurrent_tasks must be >= 1")

    return Config(
        home=home,
        trusted_workspaces=_expand(raw["trusted_workspaces"]),
        denied_paths=_expand(raw["denied_paths"]),
        default_permission_mode=raw["default_permission_mode"],
        allow_full_access=raw["allow_full_access"],
        ask_timeout_seconds=raw["ask_timeout_seconds"],
        task_timeout_seconds=raw["task_timeout_seconds"],
        max_concurrent_tasks=raw["max_concurrent_tasks"],
        max_prompt_chars=raw["max_prompt_chars"],
        max_output_chars=raw["max_output_chars"],
        max_delegation_depth=raw["max_delegation_depth"],
        audit_prompt_preview_chars=raw["audit_prompt_preview_chars"],
        task_retention_days=raw["task_retention_days"],
        http_allowed_origins=list(raw["http_allowed_origins"]),
        env_passthrough=list(raw["env_passthrough"]),
        inherit_env=raw["inherit_env"],
        quota_backoff_seconds=raw["quota_backoff_seconds"],
    )
