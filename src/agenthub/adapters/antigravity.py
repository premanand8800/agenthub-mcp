"""Google Antigravity CLI (`agy`)."""

from __future__ import annotations

import glob
import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

from agenthub.adapters.base import Adapter, Command, RunSpec, clip
from agenthub.errors import NotFound
from agenthub.process import child_env, run_capture
from agenthub.security import safe_positional


def _agy_home() -> str:
    return os.path.expanduser(os.environ.get("AGY_HOME", "~/.gemini/antigravity-cli"))


class AntigravityAdapter(Adapter):
    name = "antigravity"
    display_name = "Google Antigravity"
    description = "Google Antigravity CLI, run non-interactively with `agy --print`."
    binary = "agy"
    extra_search_paths = ("~/.local/bin/agy",)
    install_hint = "Install from https://antigravity.google"
    modes = ("read-only", "workspace-write", "full")
    supports_sessions = True
    supports_resume = True
    supports_send_message = True
    supports_output_schema = True
    supports_add_dirs = True
    env_allow = ("GEMINI_*", "GOOGLE_API_KEY", "GOOGLE_GENAI_*", "AGY_*", "ANTIGRAVITY_*")

    _MODE_FLAGS = {
        "read-only": ["--mode", "plan", "--sandbox"],
        "workspace-write": ["--mode", "accept-edits", "--sandbox"],
        "full": ["--dangerously-skip-permissions"],
    }

    def __init__(self) -> None:
        self._models_cache: Optional[List[Dict[str, Any]]] = None
        self._models_at = 0.0
        self._models_lock = threading.Lock()

    def build_command(self, spec: RunSpec) -> Command:
        argv = [self.require_executable(), *self._MODE_FLAGS[spec.mode], "--output-format", "text"]
        if spec.model:
            argv.append(f"--model={spec.model}")
        if spec.session_id:
            argv.append(f"--conversation={spec.session_id}")
        for d in spec.add_dirs:
            argv.append(f"--add-dir={d}")
        if spec.schema_file:
            argv.append(f"--json-schema={spec.schema_file}")
        # `--print=<text>` keeps a prompt that starts with '-' from being read as a flag.
        argv.append(f"--print={spec.prompt}")
        return Command(argv=argv)

    def build_send_message(self, session_id: str, message: str) -> Command:
        return Command(
            argv=[
                self.require_executable(),
                "agentapi",
                "send-message",
                "--title=From AgentHub",
                session_id,
                safe_positional(message),
            ]
        )

    # -- history -------------------------------------------------------------

    def _db(self):
        import sqlite3  # imported lazily: most calls never touch session history

        path = os.path.join(_agy_home(), "conversation_summaries.db")
        if not os.path.exists(path):
            return None
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)

    @staticmethod
    def _row(r) -> Dict[str, Any]:
        return {
            "session_id": r[0],
            "title": r[1] or "(untitled)",
            "preview": clip(r[2], 300),
            "steps": r[3],
            "updated_at": r[4],
            "status": r[5],
        }

    def list_sessions(self, limit: int) -> List[Dict[str, Any]]:
        con = self._db()
        if con is None:
            return []
        try:
            rows = con.execute(
                "SELECT conversation_id, title, preview, step_count, last_modified_time, status "
                "FROM conversation_summaries ORDER BY last_modified_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            con.close()
        return [self._row(r) for r in rows]

    def search_sessions(self, query: str, limit: int) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen = set()
        con = self._db()
        if con is not None:
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            try:
                rows = con.execute(
                    "SELECT conversation_id, title, preview, step_count, last_modified_time, status "
                    "FROM conversation_summaries WHERE title LIKE ? ESCAPE '\\' OR preview LIKE ? ESCAPE '\\' "
                    "ORDER BY last_modified_time DESC LIMIT ?",
                    (like, like, limit),
                ).fetchall()
            finally:
                con.close()
            for r in rows:
                seen.add(r[0])
                results.append({**self._row(r), "match": "summary"})
        ql = query.lower()
        pattern = os.path.join(_agy_home(), "brain", "*", ".system_generated", "logs", "transcript.jsonl")
        for tf in sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True):
            if len(results) >= limit:
                break
            sid = tf.split(os.sep)[-4]
            if sid in seen:
                continue
            try:
                with open(tf, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if ql in line.lower():
                            seen.add(sid)
                            results.append(
                                {"session_id": sid, "match": "transcript", "snippet": clip(line.strip(), 200)}
                            )
                            break
            except OSError:
                continue
        return results[:limit]

    def get_transcript(self, session_id: str, max_steps: int) -> Dict[str, Any]:
        tf = os.path.join(_agy_home(), "brain", session_id, ".system_generated", "logs", "transcript.jsonl")
        if not os.path.exists(tf):
            raise NotFound(f"No Antigravity session '{session_id}'")
        steps: List[Dict[str, Any]] = []
        with open(tf, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                step = {
                    "step": d.get("step_index"),
                    "source": d.get("source"),
                    "type": d.get("type"),
                    "ts": d.get("created_at"),
                }
                if d.get("content"):
                    step["content"] = clip(d["content"])
                if d.get("thinking"):
                    step["thinking"] = clip(d["thinking"], 400)
                if d.get("tool_calls"):
                    step["tool_calls"] = clip(json.dumps(d["tool_calls"]), 1500)
                steps.append(step)
        return {"session_id": session_id, "total_steps": len(steps), "steps": steps[-max_steps:]}

    def list_models(self) -> List[Dict[str, Any]]:
        with self._models_lock:
            if self._models_cache is not None and time.time() - self._models_at < 3600:
                return self._models_cache
            exe = self.executable()
            if not exe:
                return []
            res = run_capture(
                [exe, "models"], cwd=os.path.expanduser("~"), timeout=30, env=child_env(allow=self.env_allow)
            )
            models = []
            for line in res.stdout.splitlines():
                if "\t" in line:
                    mid, _, name = line.partition("\t")
                    models.append({"id": mid.strip(), "name": name.strip()})
            if models:
                self._models_cache, self._models_at = models, time.time()
            return models


def add_allow_rules(rules: List[str]) -> List[str]:
    """Append permission allow rules to Antigravity's settings.json. Human-only (CLI), never MCP/HTTP."""
    from agenthub.security import atomic_write_json

    path = os.path.join(_agy_home(), "settings.json")
    data: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)  # refuse to overwrite a file we cannot parse
    perms = data.setdefault("permissions", {})
    allow = set(perms.get("allow", []))
    allow.update(rules)
    perms["allow"] = sorted(allow)
    atomic_write_json(path, data)
    return perms["allow"]
