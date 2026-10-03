"""OpenAI Codex CLI (`codex`)."""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List

from agenthub.adapters.base import Adapter, Command, Parsed, RunSpec, clip
from agenthub.errors import NotFound, Unavailable


def _codex_home() -> str:
    return os.path.expanduser(os.environ.get("CODEX_HOME", "~/.codex"))


def _rollouts() -> List[str]:
    files = glob.glob(os.path.join(_codex_home(), "sessions", "**", "rollout-*.jsonl"), recursive=True)
    return sorted(files, key=lambda p: os.path.getmtime(p), reverse=True)


def _session_id_from_path(path: str) -> str:
    base = os.path.basename(path)[: -len(".jsonl")]
    return base[-36:] if len(base) >= 36 else base


class CodexAdapter(Adapter):
    name = "codex"
    display_name = "OpenAI Codex"
    description = "OpenAI Codex CLI, run non-interactively with `codex exec`."
    binary = "codex"
    extra_search_paths = ("~/.local/bin/codex",)
    install_hint = "Install: npm install -g @openai/codex"
    modes = ("read-only", "workspace-write", "full")
    supports_sessions = True
    supports_resume = True
    supports_send_message = True
    supports_output_schema = True
    supports_add_dirs = True
    supports_images = True
    env_allow = ("OPENAI_*", "CODEX_*", "AZURE_OPENAI_*")

    def build_command(self, spec: RunSpec) -> Command:
        exe = self.require_executable()
        if spec.session_id:
            if spec.add_dirs:
                raise Unavailable("Codex cannot add directories when resuming a session")
            # `exec resume` has no -s/-C/--color; pass the sandbox through config instead.
            argv = [exe, "exec", "resume", "--skip-git-repo-check", "--json"]
            if spec.mode == "full":
                argv.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                argv += ["-c", f'sandbox_mode="{spec.mode}"', "-c", 'approval_policy="never"']
        else:
            argv = [exe, "exec", "--skip-git-repo-check", "--json", "--color", "never", "-C", spec.workdir]
            if spec.mode == "full":
                argv.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                # approval_policy=never: a command that needs escalation fails instead of hanging.
                argv += ["-s", spec.mode, "-c", 'approval_policy="never"']
            for d in spec.add_dirs:
                argv += ["--add-dir", d]
        if spec.model:
            argv += ["-m", spec.model]
        if spec.output_file:
            argv += ["-o", spec.output_file]
        if spec.schema_file:
            argv += ["--output-schema", spec.schema_file]
        for img in spec.images:
            argv += ["-i", img]
        argv.append("--")  # everything after this is positional, even text starting with '-'
        if spec.session_id:
            argv.append(spec.session_id)
        argv.append(spec.prompt)
        return Command(argv=argv, output_file=spec.output_file)

    @staticmethod
    def _events(text: str):
        for line in text.splitlines():
            if line.startswith("{"):
                try:
                    yield json.loads(line)
                except ValueError:
                    continue

    def parse_output(self, stdout: str) -> Parsed:
        p = Parsed()
        for ev in self._events(stdout):
            t = ev.get("type")
            if t == "thread.started":
                p.session_id = ev.get("thread_id")
            elif t == "item.completed" and (ev.get("item") or {}).get("type") == "agent_message":
                p.reply = ev["item"].get("text")
            elif t == "turn.completed":
                p.usage = ev.get("usage")
            elif t in ("error", "turn.failed"):
                err = ev.get("message") or (ev.get("error") or {}).get("message")
                p.error = err or json.dumps(ev)
        return p

    def format_log(self, text: str) -> str:
        """Turn `codex exec --json` events into short readable lines."""
        out = []
        for line in text.splitlines():
            if not line.startswith("{"):
                out.append(line)
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                out.append(line)
                continue
            t, item = ev.get("type"), ev.get("item") or {}
            kind = item.get("type")
            if t == "thread.started":
                out.append(f"[session] {ev.get('thread_id')}")
            elif t == "item.started" and kind == "command_execution":
                out.append(f"$ {item.get('command')}")
            elif t == "item.completed" and kind == "command_execution":
                out.append(f"  -> exit {item.get('exit_code')}")
            elif t == "item.completed" and kind == "agent_message":
                out.append(f"[assistant] {item.get('text')}")
            elif t == "item.completed" and kind == "file_change":
                paths = ", ".join(c.get("path", "?") for c in item.get("changes", []))
                out.append(f"[edit] {paths}")
            elif t == "item.completed" and kind == "mcp_tool_call":
                out.append(f"[tool] {item.get('server')}.{item.get('tool')}")
            elif t == "turn.completed":
                out.append(f"[usage] {json.dumps(ev.get('usage'))}")
            elif t in ("error", "turn.failed"):
                out.append(f"[error] {ev.get('message') or json.dumps(ev.get('error'))}")
        return "\n".join(out)

    def build_send_message(self, session_id: str, message: str) -> Command:
        exe = self.require_executable()
        return Command(argv=[exe, "queue", "--thread", session_id, f"--message={message}"])

    # -- history -------------------------------------------------------------

    def _index(self) -> List[Dict[str, Any]]:
        path = os.path.join(_codex_home(), "session_index.jsonl")
        out: List[Dict[str, Any]] = []
        if not os.path.exists(path):
            return out
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("id"):
                    out.append(d)
        return out

    def list_sessions(self, limit: int) -> List[Dict[str, Any]]:
        seen, sessions = set(), []
        for d in reversed(self._index()):
            if d["id"] not in seen:
                seen.add(d["id"])
                sessions.append(
                    {
                        "session_id": d["id"],
                        "title": d.get("thread_name") or "(untitled)",
                        "updated_at": d.get("updated_at"),
                    }
                )
        for path in _rollouts():
            sid = _session_id_from_path(path)
            if sid not in seen:
                seen.add(sid)
                sessions.append(
                    {"session_id": sid, "title": f"Session {sid[:8]}", "updated_at": os.path.getmtime(path)}
                )
        sessions.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
        return sessions[:limit]

    def search_sessions(self, query: str, limit: int) -> List[Dict[str, Any]]:
        ql, seen, results = query.lower(), set(), []
        for d in self._index():
            if ql in json.dumps(d).lower() and d["id"] not in seen:
                seen.add(d["id"])
                results.append({"session_id": d["id"], "title": d.get("thread_name"), "match": "title"})
                if len(results) >= limit:
                    return results
        for path in _rollouts():
            sid = _session_id_from_path(path)
            if sid in seen:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if ql in line.lower():
                            seen.add(sid)
                            results.append(
                                {"session_id": sid, "match": "transcript", "snippet": clip(line.strip(), 200)}
                            )
                            break
            except OSError:
                continue
            if len(results) >= limit:
                break
        return results

    def get_transcript(self, session_id: str, max_steps: int) -> Dict[str, Any]:
        matches = glob.glob(
            os.path.join(_codex_home(), "sessions", "**", f"rollout-*-{session_id}.jsonl"), recursive=True
        )
        if not matches:
            raise NotFound(f"No Codex session '{session_id}'")
        turns: List[Dict[str, Any]] = []
        with open(matches[0], encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                p = d.get("payload") or {}
                if p.get("type") == "message" and p.get("role") in ("user", "assistant"):
                    text = "".join(
                        c.get("text", "")
                        for c in p.get("content", [])
                        if isinstance(c, dict)
                        and not c.get("text", "").startswith(("<environment_context>", "<recommended_plugins>"))
                    ).strip()
                    if text:
                        turns.append({"ts": d.get("timestamp"), "role": p["role"], "text": clip(text)})
                elif p.get("type") == "function_call":
                    turns.append(
                        {
                            "ts": d.get("timestamp"),
                            "role": "assistant",
                            "tool_call": p.get("name"),
                            "arguments": clip(p.get("arguments"), 1000),
                        }
                    )
        return {"session_id": session_id, "total_turns": len(turns), "turns": turns[-max_steps:]}

    def list_models(self) -> List[Dict[str, Any]]:
        path = os.path.join(_codex_home(), "models_cache.json")
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return []
        return [
            {
                "id": m.get("slug") or m.get("id"),
                "name": m.get("display_name") or m.get("title") or m.get("slug"),
                "description": m.get("description", ""),
            }
            for m in data.get("models", [])
            if isinstance(m, dict)
        ]
