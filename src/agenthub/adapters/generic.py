"""Custom agents declared in ~/.agenthub/agents/<name>.json. See examples/custom-agent.json."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List

from agenthub.adapters.base import Adapter, Command, Parsed, RunSpec
from agenthub.config import PERMISSION_MODES
from agenthub.security import safe_positional

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_PLACEHOLDER = re.compile(r"\{(prompt|model|workdir|schema_file|dir)\}")


class SpecError(ValueError):
    pass


def _str_list(spec: Dict[str, Any], key: str, default: List[str]) -> List[str]:
    value = spec.get(key, default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SpecError(f"'{key}' must be a list of strings")
    return value


class GenericAdapter(Adapter):
    def __init__(self, spec: Dict[str, Any]):
        if not isinstance(spec, dict):
            raise SpecError("spec must be a JSON object")
        name = spec.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise SpecError(f"'name' must match {_NAME_RE.pattern}")
        command = spec.get("command")
        if not isinstance(command, str) or not command:
            raise SpecError("'command' is required (binary name on PATH or absolute path)")
        self.name = name
        self.display_name = str(spec.get("display_name", name))
        self.description = str(spec.get("description", f"Custom agent '{name}'"))
        self.binary = command
        self.install_hint = str(spec.get("install_hint", f"'{command}' was not found on PATH."))
        self.args = _str_list(spec, "args", ["{prompt}"])
        if not any("{prompt}" in a for a in self.args):
            raise SpecError("'args' must contain the {prompt} placeholder")
        self.model_args = _str_list(spec, "model_args", [])
        self.schema_args = _str_list(spec, "output_schema_args", [])
        self.add_dir_args = _str_list(spec, "add_dir_args", [])
        self.supports_output_schema = bool(self.schema_args)
        self.supports_add_dirs = bool(self.add_dir_args)
        self.env_allow = tuple(_str_list(spec, "env_passthrough", []))
        modes = spec.get("modes")
        if not isinstance(modes, dict) or not modes:
            raise SpecError(
                "'modes' is required: map each supported permission mode to its extra args, "
                'e.g. {"read-only": ["--read-only"]}'
            )
        for mode, extra in modes.items():
            if mode not in PERMISSION_MODES:
                raise SpecError(f"unknown mode '{mode}'; use {', '.join(PERMISSION_MODES)}")
            if not isinstance(extra, list) or not all(isinstance(v, str) for v in extra):
                raise SpecError(f"modes.{mode} must be a list of strings")
        self.mode_args: Dict[str, List[str]] = modes
        self.modes = tuple(m for m in PERMISSION_MODES if m in modes)
        env = spec.get("env", {})
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise SpecError("'env' must map strings to strings")
        self.env = env
        output = spec.get("json_output")
        if output is not None and (
            not isinstance(output, dict) or not all(isinstance(v, str) for v in output.values())
        ):
            raise SpecError("'json_output' must map reply/session_id/cost_usd to top-level JSON keys")
        self.json_output: Dict[str, str] = output or {}
        models = spec.get("models", [])
        if not isinstance(models, list):
            raise SpecError("'models' must be a list")
        self.models = models

    def executable(self):
        if os.path.isabs(self.binary):
            return self.binary if os.path.isfile(self.binary) and os.access(self.binary, os.X_OK) else None
        return super().executable()

    def build_command(self, spec: RunSpec) -> Command:
        values = {
            "prompt": spec.prompt,
            "model": spec.model or "",
            "workdir": spec.workdir,
            "schema_file": spec.schema_file or "",
            "dir": "",
        }

        def render(arg: str, **override: str) -> str:
            if arg == "{prompt}":
                return safe_positional(spec.prompt)
            vals = {**values, **override}
            # One pass, so a prompt that contains "{model}" is never expanded again.
            return _PLACEHOLDER.sub(lambda m: vals[m.group(1)], arg)

        argv = [self.require_executable(), *self.mode_args[spec.mode]]
        if spec.model:
            argv += [render(a) for a in self.model_args]
        if spec.schema_file:
            argv += [render(a) for a in self.schema_args]
        for d in spec.add_dirs:
            argv += [render(a, dir=d) for a in self.add_dir_args]
        argv += [render(a) for a in self.args]
        return Command(argv=argv, env=dict(self.env))

    def parse_output(self, stdout: str) -> Parsed:
        if not self.json_output:
            return Parsed()
        try:
            d = json.loads(stdout.strip() or "null")
        except ValueError:
            return Parsed()
        if not isinstance(d, dict):
            return Parsed()
        keys = self.json_output
        return Parsed(
            reply=d.get(keys.get("reply", "")),
            session_id=d.get(keys.get("session_id", "")),
            cost_usd=d.get(keys.get("cost_usd", "")),
        )

    def list_models(self) -> List[Dict[str, Any]]:
        return self.models
