"""The Hub: one place where every request is validated, checked against policy, run and audited.

The MCP server, the HTTP server and the CLI are thin layers over this class.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from agenthub import worktree
from agenthub.adapters import Adapter, Registry, RunSpec
from agenthub.config import PERMISSION_MODES, Config, load_config
from agenthub.errors import HubError, InvalidArgument, NotFound, PolicyError, QuotaExhausted, Unavailable
from agenthub.health import HealthStore, detect_quota
from agenthub.process import child_env, git, kill_group, run_capture, truncate_tail
from agenthub.security import (
    AuditLog,
    check_depth,
    ensure_private_dir,
    resolve_file,
    resolve_workdir,
    validate_id,
    validate_model,
    validate_text,
)
from agenthub.tasks import RUNNING, TERMINAL, TaskStore

TASK_STATUSES = ("running", *sorted(TERMINAL))
ISOLATION_MODES = ("none", "worktree")
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/~^@{}-]{0,127}$")
MAX_SCHEMA_CHARS = 20_000

REVIEW_PROMPT = """Review the code change below. Do not modify any files.
Report only real problems: bugs, security issues, data loss, race conditions, broken error handling,
and missing tests for risky logic. For each finding give the file and line, a severity
(high/medium/low), what goes wrong, and a concrete fix. If the change looks correct, say so briefly.
{extra}
```diff
{patch}
```"""


def _bounded(value: Any, field: str, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise InvalidArgument(f"{field} must be an integer between {lo} and {hi}")
    return value


def _str_list(value: Any, field: str, max_items: int = 10) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value) or len(value) > max_items:
        raise InvalidArgument(f"{field} must be a list of at most {max_items} strings")
    return value


class Hub:
    def __init__(self, config: Optional[Config] = None, registry: Optional[Registry] = None):
        self.config = config or load_config()
        ensure_private_dir(self.config.home)
        self.registry = registry or Registry(custom_dir=self.config.custom_agents_dir)
        self.audit = AuditLog(self.config.audit_path, self.config.audit_prompt_preview_chars)
        self.tasks = TaskStore(
            self.config.tasks_dir,
            self.config.max_concurrent_tasks,
            self.config.task_timeout_seconds,
            self.config.task_retention_days,
        )
        self.health = HealthStore(self.config.health_path, self.config.quota_backoff_seconds)
        self._scratch = os.path.join(self.config.home, "tmp")
        ensure_private_dir(self._scratch)
        # Process groups of in-flight `ask` runs, killed on shutdown so nothing keeps running
        # (and spending) after the client goes away. Background tasks are meant to outlive us.
        self._active: set = set()
        self._active_lock = threading.Lock()
        self._closing = False

    # -- lifecycle -------------------------------------------------------------

    def shutdown(self) -> None:
        self._closing = True
        with self._active_lock:
            groups = list(self._active)
        for pgid in groups:
            kill_group(pgid, grace=1.0)

    def prune(self) -> int:
        def drop_worktree(meta: Dict[str, Any]) -> None:
            iso = meta.get("isolation")
            if iso and iso.get("state") == "active":
                worktree.remove(iso)

        return self.tasks.prune(on_remove=drop_worktree)

    def _track(self, pgid: int) -> None:
        with self._active_lock:
            self._active.add(pgid)

    def _untrack(self, pgid: int) -> None:
        with self._active_lock:
            self._active.discard(pgid)

    # -- helpers ---------------------------------------------------------------

    def _adapter(self, name: Any) -> Adapter:
        adapter = self.registry.get(name) if isinstance(name, str) else None
        if adapter is None:
            raise NotFound(f"Unknown agent {name!r}. Available: {', '.join(self.registry.names())}")
        return adapter

    def _env(self, adapter: Adapter, extra: Dict[str, str]) -> Dict[str, str]:
        allow = None if self.config.inherit_env else (*adapter.env_allow, *self.config.env_passthrough)
        return child_env(extra, allow)

    def _mode(self, adapter: Adapter, requested: Any) -> str:
        mode = requested or self.config.default_permission_mode
        if mode not in PERMISSION_MODES:
            raise InvalidArgument(f"permission_mode must be one of {', '.join(PERMISSION_MODES)}")
        if mode == "full" and not self.config.allow_full_access:
            raise PolicyError(
                "permission_mode 'full' turns off the agent's sandbox and approvals, and it is disabled. "
                f'A human can enable it by setting "allow_full_access": true in {self.config.config_path}.'
            )
        if mode not in adapter.modes:
            raise Unavailable(
                f"{adapter.display_name} cannot enforce '{mode}'. Supported modes: {', '.join(adapter.modes)}"
            )
        return mode

    def _check_health(self, adapter: Adapter) -> None:
        h = self.health.get(adapter.name)
        if h["status"] == "quota_exhausted":
            raise QuotaExhausted(
                f"{adapter.display_name} hit a quota/rate limit: {h.get('message')}. "
                f"Retry in about {h['retry_after_seconds']}s, or use another agent.",
                h["retry_after_seconds"],
            )

    def _prepare(self, agent: Any, prompt: Any, opts: Dict[str, Any]) -> Tuple[Adapter, RunSpec]:
        if self._closing:
            raise Unavailable("AgentHub is shutting down")
        adapter = self._adapter(agent)
        spec = RunSpec(
            prompt=validate_text(prompt, "prompt", self.config.max_prompt_chars),
            mode=self._mode(adapter, opts.get("permission_mode")),
            workdir="",
        )
        if opts.get("model") is not None:
            spec.model = validate_model(opts["model"])
        if opts.get("session_id") is not None:
            spec.session_id = validate_id(opts["session_id"], "session_id")
            if not adapter.supports_resume:
                raise Unavailable(f"{adapter.display_name} cannot resume sessions")
        add_dirs = _str_list(opts.get("add_dirs"), "add_dirs")
        if add_dirs and not adapter.supports_add_dirs:
            raise Unavailable(f"{adapter.display_name} does not support add_dirs")
        images = _str_list(opts.get("images"), "images")
        if images and not adapter.supports_images:
            raise Unavailable(f"{adapter.display_name} does not support images")
        schema = opts.get("output_schema")
        if schema is not None:
            if not isinstance(schema, dict) or len(json.dumps(schema)) > MAX_SCHEMA_CHARS:
                raise InvalidArgument(f"output_schema must be a JSON Schema object under {MAX_SCHEMA_CHARS} chars")
            if not adapter.supports_output_schema:
                raise Unavailable(f"{adapter.display_name} does not support output_schema")
        check_depth(self.config)
        spec.workdir = resolve_workdir(opts.get("workdir"), self.config)
        spec.add_dirs = [resolve_workdir(d, self.config) for d in add_dirs]
        spec.images = [resolve_file(p, self.config, "image") for p in images]
        adapter.require_executable()
        self._check_health(adapter)
        return adapter, spec

    def _write_schema(self, schema: Optional[Dict[str, Any]], path: str) -> Optional[str]:
        if schema is None:
            return None
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(schema, f)
        return path

    @staticmethod
    def _structured(reply: str, parsed_structured: Any) -> Dict[str, Any]:
        if parsed_structured is not None:
            return {"structured": parsed_structured}
        text = reply.strip()
        if text.startswith("```"):
            text = text.strip("`").partition("\n")[2]
        try:
            return {"structured": json.loads(text)}
        except ValueError as e:
            return {"structured": None, "structured_error": f"Reply is not valid JSON: {e}"}

    def _with_fallback(self, agents: List[Any], run) -> Dict[str, Any]:
        """Try agents in order. Move on only when nothing ran: quota exhausted or agent unavailable."""
        attempts: List[Dict[str, Any]] = []
        last_exc: Optional[HubError] = None
        for i, name in enumerate(agents):
            more = i < len(agents) - 1
            try:
                result = run(name)
            except (QuotaExhausted, Unavailable) as e:
                attempts.append({"agent": name, "error": e.to_dict()})
                last_exc = e
                if more:
                    continue
                if len(agents) > 1:
                    raise type(e)(f"All agents failed: {json.dumps(attempts)}") from e
                raise
            if more and result.get("error_code") == "quota_exhausted":
                attempts.append({"agent": name, "error": {"code": "quota_exhausted", "message": result.get("error")}})
                continue
            if attempts:
                result["fallback_attempts"] = attempts
            return result
        raise last_exc or Unavailable("No agent available")  # pragma: no cover

    # -- discovery -------------------------------------------------------------

    def list_agents(self) -> Dict[str, Any]:
        agents = []
        for a in self.registry.all():
            info = a.info()
            info["health"] = self.health.get(a.name)
            agents.append(info)
        return {
            "agents": agents,
            "default_permission_mode": self.config.default_permission_mode,
            "full_access_allowed": self.config.allow_full_access,
            "trusted_workspaces": self.config.trusted_workspaces,
            "custom_agent_errors": self.registry.load_errors,
        }

    def list_models(self, agent: Any) -> Dict[str, Any]:
        adapter = self._adapter(agent)
        return {"agent": adapter.name, "models": adapter.list_models()}

    # -- ask -------------------------------------------------------------------

    def ask(
        self,
        agent: Any,
        prompt: Any,
        *,
        fallback_agents: Any = None,
        source: str = "api",
        **opts: Any,
    ) -> Dict[str, Any]:
        """Run a prompt and wait. opts: workdir, permission_mode, model, session_id, add_dirs, images,
        output_schema."""
        agents = [agent, *_str_list(fallback_agents, "fallback_agents", 4)]
        return self._with_fallback(agents, lambda name: self._ask_one(name, prompt, opts, source))

    def _ask_one(self, agent: Any, prompt: Any, opts: Dict[str, Any], source: str) -> Dict[str, Any]:
        adapter, spec = self._prepare(agent, prompt, opts)
        token = secrets.token_hex(8)
        spec.output_file = os.path.join(self._scratch, f"ask-{token}.out")
        spec.schema_file = self._write_schema(
            opts.get("output_schema"), os.path.join(self._scratch, f"ask-{token}.schema.json")
        )
        self.audit.record(
            source,
            "ask",
            "started",
            adapter.name,
            workdir=spec.workdir,
            permission_mode=spec.mode,
            model=spec.model,
            session_id=spec.session_id,
            prompt=self.audit.describe_prompt(spec.prompt),
        )
        started = time.time()
        try:
            cmd = adapter.build_command(spec)
            try:
                res = run_capture(
                    cmd.argv,
                    spec.workdir,
                    self.config.ask_timeout_seconds,
                    self._env(adapter, cmd.env),
                    self.config.max_output_chars,
                    on_spawn=self._track,
                    on_exit=self._untrack,
                )
            except OSError as e:
                self.audit.record(source, "ask", "failed", adapter.name, error=str(e))
                raise Unavailable(f"Could not start {adapter.display_name}: {e}") from e
            file_reply = ""
            if cmd.output_file and os.path.exists(cmd.output_file):
                with open(cmd.output_file, encoding="utf-8", errors="replace") as f:
                    file_reply = f.read().strip()
        finally:
            for p in (spec.output_file, spec.schema_file):
                if p:
                    try:
                        os.unlink(p)
                    except FileNotFoundError:
                        pass

        parsed = adapter.parse_output(res.stdout)
        reply = truncate_tail(file_reply or parsed.reply or res.stdout.strip(), self.config.max_output_chars)
        elapsed = round(time.time() - started, 1)
        ok = res.returncode == 0 and not res.timed_out and not parsed.error
        status = "timed_out" if res.timed_out else ("succeeded" if ok else "failed")
        result: Dict[str, Any] = {
            "ok": ok,
            "status": status,
            "agent": adapter.name,
            "permission_mode": spec.mode,
            "workdir": spec.workdir,
            "elapsed_seconds": elapsed,
            "exit_code": res.returncode,
            "reply": reply,
            "session_id": parsed.session_id,
            "usage": parsed.usage,
            "cost_usd": parsed.cost_usd,
        }
        if self._closing and not ok:
            result.update(status="cancelled", error="AgentHub shut down while the agent was running")
        elif res.timed_out:
            result["error"] = f"Timed out after {self.config.ask_timeout_seconds}s. Use start_task for long work."
        elif not ok:
            err_text = "\n".join(filter(None, [parsed.error, res.stderr.strip(), res.stdout.strip()[-2000:]]))
            result["error"] = truncate_tail(parsed.error or res.stderr.strip() or "Agent exited with an error", 4000)
            quota = detect_quota(err_text)
            if quota:
                wait = self.health.mark_quota(adapter.name, quota[0], quota[1])
                result.update(error_code="quota_exhausted", retry_after_seconds=wait, retryable=True)
        if ok:
            self.health.clear(adapter.name)
            if opts.get("output_schema") is not None:
                result.update(self._structured(reply, parsed.structured))
        self.audit.record(
            source,
            "ask",
            status,
            adapter.name,
            exit_code=res.returncode,
            elapsed_seconds=elapsed,
            session_id=parsed.session_id,
            error_code=result.get("error_code"),
        )
        return result

    # -- background tasks ---------------------------------------------------------

    def start_task(
        self,
        agent: Any,
        prompt: Any,
        *,
        isolation: Any = None,
        fallback_agents: Any = None,
        source: str = "api",
        **opts: Any,
    ) -> Dict[str, Any]:
        """opts: workdir, permission_mode, model, session_id, add_dirs, images, output_schema."""
        if isolation is not None and isolation not in ISOLATION_MODES:
            raise InvalidArgument(f"isolation must be one of {', '.join(ISOLATION_MODES)}")
        agents = [agent, *_str_list(fallback_agents, "fallback_agents", 4)]
        return self._with_fallback(agents, lambda name: self._start_one(name, prompt, isolation, opts, source))

    def _start_one(self, agent: Any, prompt: Any, isolation: Any, opts: Dict[str, Any], source: str):
        adapter, spec = self._prepare(agent, prompt, opts)
        task_id = TaskStore.new_id(adapter.name)
        spec.output_file = self.tasks.output_path(task_id)
        spec.schema_file = self._write_schema(
            opts.get("output_schema"), os.path.join(self.tasks.dir, f"{task_id}.schema")
        )
        extra: Dict[str, Any] = {"requested_workdir": spec.workdir, "wants_structured": spec.schema_file is not None}
        iso = None
        if isolation == "worktree":
            iso = worktree.create(spec.workdir, os.path.join(self.config.worktrees_dir, task_id))
            spec.workdir = iso["cwd"]
            extra["isolation"] = iso
        try:
            cmd = adapter.build_command(spec)
            prompt_info = self.audit.describe_prompt(spec.prompt)
            task = self.tasks.start(
                adapter.name,
                cmd.argv,
                spec.workdir,
                prompt_info,
                spec.mode,
                spec.model,
                env=self._env(adapter, cmd.env),
                output_file=cmd.output_file,
                task_id=task_id,
                extra=extra,
            )
        except BaseException:
            if iso:
                worktree.remove(iso)
            for leftover in (spec.schema_file, spec.output_file):  # the task record never existed
                if leftover:
                    try:
                        os.unlink(leftover)
                    except OSError:
                        pass
            raise
        self.audit.record(
            source,
            "start_task",
            "started",
            adapter.name,
            task_id=task_id,
            workdir=spec.workdir,
            permission_mode=spec.mode,
            model=spec.model,
            session_id=spec.session_id,
            isolation=isolation,
            prompt=prompt_info,
        )
        return task

    def get_task(self, task_id: Any) -> Dict[str, Any]:
        task_id = validate_id(task_id, "task_id")
        task = self.tasks.get(task_id, self.config.max_output_chars)
        if task["status"] not in TERMINAL:
            return task
        raw = self.tasks.raw(task_id)
        adapter = self.registry.get(task["agent"])
        if adapter is None:
            return task
        log = self.tasks.read_log(task_id, 2_000_000)
        parsed = adapter.parse_output(log)
        out = raw.get("output_file")
        has_file_reply = bool(out and os.path.exists(out) and os.path.getsize(out))
        if parsed.reply and not has_file_reply:
            task["final_output"] = truncate_tail(parsed.reply, self.config.max_output_chars)
        task.update(session_id=parsed.session_id, usage=parsed.usage, cost_usd=parsed.cost_usd)
        if task["status"] == "succeeded" and raw.get("wants_structured"):
            task.update(self._structured(task.get("final_output") or "", parsed.structured))
            if task.get("structured") is not None:
                # The parsed object already carries the reply: sending the raw text too doubles the tokens.
                task.pop("final_output", None)
        if task["status"] == "failed":
            quota = detect_quota("\n".join(filter(None, [parsed.error, log[-4000:]])))
            if quota:
                if not raw.get("quota_recorded"):
                    self.health.mark_quota(adapter.name, quota[0], quota[1])
                    self.tasks.update(task_id, quota_recorded=True)
                task.update(error_code="quota_exhausted", retryable=True)
        return task

    def wait_task(self, task_id: Any, timeout_seconds: Any = None) -> Dict[str, Any]:
        """Block until the task leaves 'running' or the timeout passes. Saves the caller from polling."""
        task_id = validate_id(task_id, "task_id")
        timeout = _bounded(timeout_seconds, "timeout_seconds", 60, 1, 600)
        deadline = time.time() + timeout
        delay = 0.2
        while True:
            task = self.tasks.get(task_id, 0)
            if task["status"] != RUNNING:
                return {**self.get_task(task_id), "finished": True}
            if time.time() >= deadline or self._closing:
                return {**task, "finished": False, "hint": "Still running; call wait_task again."}
            time.sleep(min(delay, max(0.0, deadline - time.time())))
            delay = min(delay * 1.5, 2.0)

    def task_logs(self, task_id: Any, tail_lines: Any = None) -> Dict[str, Any]:
        task_id = validate_id(task_id, "task_id")
        lines = _bounded(tail_lines, "tail_lines", 100, 1, 5000)
        meta = self.tasks.raw(task_id)
        adapter = self.registry.get(meta["agent"])
        return self.tasks.logs(task_id, lines, self.config.max_output_chars, adapter.format_log if adapter else None)

    def cancel_task(self, task_id: Any, source: str = "api") -> Dict[str, Any]:
        task_id = validate_id(task_id, "task_id")
        result = self.tasks.cancel(task_id)
        self.audit.record(source, "cancel_task", result["status"], result.get("agent"), task_id=task_id)
        return result

    def list_tasks(self, limit: Any = None, status: Any = None) -> Dict[str, Any]:
        n = _bounded(limit, "limit", 20, 1, 500)
        if status is not None and status not in TASK_STATUSES:
            raise InvalidArgument(f"status must be one of {', '.join(TASK_STATUSES)}")
        return {"tasks": self.tasks.list(limit=n, status=status)}

    # -- worktree isolation ---------------------------------------------------------

    def _isolation(self, task_id: Any) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        task_id = validate_id(task_id, "task_id")
        meta = self.tasks.raw(task_id)
        iso = meta.get("isolation")
        if not iso:
            raise InvalidArgument(f"Task '{task_id}' did not run with isolation='worktree'")
        return task_id, meta, iso

    def get_task_diff(self, task_id: Any, max_chars: Any = None) -> Dict[str, Any]:
        task_id, meta, iso = self._isolation(task_id)
        limit = _bounded(max_chars, "max_chars", self.config.max_output_chars, 1000, 1_000_000)
        return {"task_id": task_id, "status": meta["status"], **worktree.diff(iso, limit)}

    def apply_task(self, task_id: Any, keep_worktree: bool = False, source: str = "api") -> Dict[str, Any]:
        task_id, meta, iso = self._isolation(task_id)
        if meta["status"] == RUNNING:
            raise InvalidArgument("Task is still running; wait for it or cancel it first")
        result = worktree.apply(iso)
        if result["applied"] and not keep_worktree:
            worktree.remove(iso)
            iso["state"] = "applied"
            self.tasks.update(task_id, isolation=iso)
        self.audit.record(
            source,
            "apply_task",
            "succeeded" if result["applied"] else "noop",
            meta["agent"],
            task_id=task_id,
            repo=iso["repo"],
            method=result.get("method"),
        )
        return {"task_id": task_id, "repo": iso["repo"], **result}

    def discard_task(self, task_id: Any, source: str = "api") -> Dict[str, Any]:
        task_id, meta, iso = self._isolation(task_id)
        if meta["status"] == RUNNING:
            self.tasks.cancel(task_id)
        worktree.remove(iso)
        iso["state"] = "discarded"
        self.tasks.update(task_id, isolation=iso)
        self.audit.record(source, "discard_task", "succeeded", meta["agent"], task_id=task_id)
        return {"task_id": task_id, "discarded": True}

    # -- review and compare ----------------------------------------------------------

    def review(
        self,
        agent: Any,
        *,
        workdir: Any = None,
        base: Any = None,
        task_id: Any = None,
        instructions: Any = None,
        model: Any = None,
        output_schema: Any = None,
        fallback_agents: Any = None,
        source: str = "api",
    ) -> Dict[str, Any]:
        """Have an agent review a diff: your uncommitted work vs `base`, or another task's worktree."""
        if task_id is not None:
            _, _, iso = self._isolation(task_id)
            d = worktree.diff(iso, 10**9)
            patch, stat, review_dir = d["patch"], d["stat"], iso["repo"]
        else:
            review_dir = resolve_workdir(workdir, self.config)
            root = worktree.repo_root(review_dir)
            ref = base or "HEAD"
            if not isinstance(ref, str) or not _REF_RE.match(ref) or ".." in ref:
                raise InvalidArgument(f"base must be a git ref like HEAD, main or HEAD~3 (got {ref!r})")
            patch = git(["diff", ref], root)
            stat = git(["diff", "--stat", ref], root).strip()
        if not patch.strip():
            raise InvalidArgument("There are no changes to review")
        room = self.config.max_prompt_chars - 3000
        if len(patch) > room:
            raise InvalidArgument(
                f"The diff is {len(patch)} chars; the limit is {room}. "
                "Review a smaller range or raise max_prompt_chars."
            )
        if instructions is not None:
            validate_text(instructions, "instructions", 4000)
        extra = f"\nExtra instructions from the requester: {instructions}\n" if instructions else ""
        result = self.ask(
            agent,
            REVIEW_PROMPT.format(extra=extra, patch=patch),
            workdir=review_dir,
            permission_mode="read-only",
            model=model,
            output_schema=output_schema,
            fallback_agents=fallback_agents,
            source=source,
        )
        result["diff_stat"] = stat
        return result

    def compare(
        self,
        agents: Any,
        prompt: Any,
        *,
        workdir: Any = None,
        output_schema: Any = None,
        source: str = "api",
    ) -> Dict[str, Any]:
        """Same prompt to several agents in parallel (read-only), answers side by side."""
        names = _str_list(agents, "agents", 5)
        if len(names) < 2 or len(set(names)) != len(names):
            raise InvalidArgument("agents must list 2 to 5 different agents")
        for n in names:
            self._adapter(n)
        validate_text(prompt, "prompt", self.config.max_prompt_chars)

        def one(name: str) -> Dict[str, Any]:
            try:
                return self.ask(
                    name,
                    prompt,
                    workdir=workdir,
                    permission_mode="read-only",
                    output_schema=output_schema,
                    source=source,
                )
            except HubError as e:
                return {"ok": False, "agent": name, "error": e.message, "error_code": e.code}

        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            results = list(pool.map(one, names))
        return {"agents": names, "succeeded": sum(1 for r in results if r.get("ok")), "results": results}

    # -- live sessions and history ------------------------------------------------------

    def send_message(self, agent: Any, session_id: Any, message: Any, source: str = "api") -> Dict[str, Any]:
        adapter = self._adapter(agent)
        session_id = validate_id(session_id, "session_id")
        message = validate_text(message, "message", self.config.max_prompt_chars)
        cmd = adapter.build_send_message(session_id, message)
        res = run_capture(cmd.argv, os.path.expanduser("~"), 60, self._env(adapter, cmd.env), 4000)
        ok = res.returncode == 0 and not res.timed_out
        self.audit.record(
            source,
            "send_message",
            "succeeded" if ok else "failed",
            adapter.name,
            session_id=session_id,
            message=self.audit.describe_prompt(message),
        )
        out: Dict[str, Any] = {"ok": ok, "agent": adapter.name, "session_id": session_id}
        if not ok:
            out["error"] = res.stderr.strip() or res.stdout.strip() or "send failed"
        return out

    def list_sessions(self, agent: Any, limit: Any = None) -> Dict[str, Any]:
        adapter = self._adapter(agent)
        return {"agent": adapter.name, "sessions": adapter.list_sessions(_bounded(limit, "limit", 20, 1, 200))}

    def search_sessions(self, agent: Any, query: Any, limit: Any = None) -> Dict[str, Any]:
        adapter = self._adapter(agent)
        query = validate_text(query, "query", 500)
        return {
            "agent": adapter.name,
            "query": query,
            "results": adapter.search_sessions(query, _bounded(limit, "limit", 10, 1, 100)),
        }

    def get_transcript(self, agent: Any, session_id: Any, max_steps: Any = None) -> Dict[str, Any]:
        adapter = self._adapter(agent)
        return adapter.get_transcript(
            validate_id(session_id, "session_id"), _bounded(max_steps, "max_steps", 50, 1, 500)
        )
