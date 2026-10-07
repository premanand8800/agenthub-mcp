"""Background tasks. One JSON file per task under ~/.agenthub/tasks (mode 0700).

Per-task files avoid the lost updates a single shared state file suffers when the MCP server,
the HTTP server and the CLI touch tasks at the same time.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from agenthub import runner as _runner_module
from agenthub.errors import LimitExceeded, NotFound, Unavailable
from agenthub.process import child_env, kill_group, truncate_tail
from agenthub.security import atomic_write_json, ensure_private_dir, validate_id

RUNNING, SUCCEEDED, FAILED, TIMED_OUT, CANCELLED, LOST = (
    "running",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "lost",
)
# runner.py imports nothing from agenthub, so it runs as a plain script even when AgentHub
# is not on the child's sys.path.
RUNNER = os.path.abspath(_runner_module.__file__)

TERMINAL = {SUCCEEDED, FAILED, TIMED_OUT, CANCELLED, LOST}

# Longest wait for the task-store lock before failing with a clear error instead of hanging.
LOCK_TIMEOUT_SECONDS = 30.0


def _proc_start_ticks(pid: int) -> Optional[int]:
    """Process start time from /proc. Guards against PID reuse. None where /proc is missing."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            data = f.read().decode(errors="replace")
        # Field 2 (comm) may contain spaces; parse after the closing paren.
        rest = data[data.rindex(")") + 2 :].split()
        return int(rest[19])
    except (OSError, ValueError, IndexError):
        return None


def _alive(pid: int, start_ticks: Optional[int]) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    if start_ticks is not None:
        now = _proc_start_ticks(pid)
        if now is not None and now != start_ticks:
            return False  # PID was reused by an unrelated process
        # A zombie still answers kill(pid, 0). Treat it as finished.
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                state = f.read().decode(errors="replace")
            if state[state.rindex(")") + 2 :].startswith("Z"):
                return False
        except (OSError, ValueError):
            pass
    return True


class TaskStore:
    def __init__(self, directory: str, max_concurrent: int, timeout_seconds: int, retention_days: int):
        self.dir = directory
        self.max_concurrent = max_concurrent
        self.timeout_seconds = timeout_seconds
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._held = threading.local()
        self._children: Dict[str, subprocess.Popen] = {}
        ensure_private_dir(self.dir)

    # -- paths -------------------------------------------------------------

    def _p(self, task_id: str, ext: str) -> str:
        return os.path.join(self.dir, f"{task_id}.{ext}")

    # -- lifecycle ---------------------------------------------------------

    def start(
        self,
        agent: str,
        argv: List[str],
        cwd: str,
        prompt_info: Dict[str, Any],
        permission_mode: str,
        model: Optional[str],
        env: Optional[Dict[str, str]] = None,
        output_file: Optional[str] = None,
        task_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # The file lock makes the limit hold across every AgentHub process (one per Claude Code
        # session), not only within this one.
        with self._lock, self._file_lock():
            running = sum(1 for t in self.list(limit=10_000) if t["status"] == RUNNING)
            if running >= self.max_concurrent:
                raise LimitExceeded(
                    f"{running} tasks already running (max_concurrent_tasks={self.max_concurrent}). "
                    "Wait for one to finish or cancel one."
                )
            task_id = task_id or self.new_id(agent)
            log_path = self._p(task_id, "log")
            log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                proc = subprocess.Popen(
                    [sys.executable, RUNNER, self._p(task_id, "exit"), str(self.timeout_seconds), "--", *argv],
                    cwd=cwd,
                    env=env if env is not None else child_env(),
                    stdin=subprocess.DEVNULL,
                    stdout=log_fd,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            finally:
                os.close(log_fd)
            self._children[task_id] = proc
            meta = {
                "task_id": task_id,
                "agent": agent,
                "status": RUNNING,
                "pid": proc.pid,
                "pid_start_ticks": _proc_start_ticks(proc.pid),
                "workdir": cwd,
                "permission_mode": permission_mode,
                "model": model,
                "prompt": prompt_info,
                "output_file": output_file,
                "created_at": time.time(),
                "finished_at": None,
                "exit_code": None,
                **(extra or {}),
            }
            atomic_write_json(self._p(task_id, "json"), meta)
            return self._public(meta)

    def _file_lock(self):
        """Cross-process lock on <dir>/.lock. Re-entrant per thread: start() holds it while list()
        refreshes tasks, and a second flock() on a new file descriptor would wait on itself forever."""
        import contextlib

        @contextlib.contextmanager
        def held():
            if getattr(self._held, "depth", 0):
                self._held.depth += 1
                try:
                    yield
                finally:
                    self._held.depth -= 1
                return
            fd = os.open(os.path.join(self.dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise Unavailable(
                                f"task store busy: could not lock {self.dir} within {LOCK_TIMEOUT_SECONDS:g}s. "
                                "Another AgentHub process may be stuck."
                            ) from None
                        time.sleep(0.02)
                self._held.depth = 1
                try:
                    yield
                finally:
                    self._held.depth = 0
            finally:
                os.close(fd)  # closing releases the lock

        return held()

    def update(self, task_id: str, **fields: Any) -> Dict[str, Any]:
        with self._file_lock():
            meta = self._load(task_id)
            meta.update(fields)
            atomic_write_json(self._p(task_id, "json"), meta)
        return meta

    def raw(self, task_id: str) -> Dict[str, Any]:
        """Full metadata including internal fields (output_file, isolation)."""
        return self._refresh(self._load(task_id))

    def read_log(self, task_id: str, max_bytes: int) -> str:
        path = self._p(task_id, "log")
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - max_bytes))
            return f.read().decode("utf-8", errors="replace")

    @staticmethod
    def new_id(agent: str) -> str:
        return f"{agent}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def output_path(self, task_id: str) -> str:
        return self._p(task_id, "out")

    def _load(self, task_id: str) -> Dict[str, Any]:
        validate_id(task_id, "task_id")
        path = self._p(task_id, "json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            raise NotFound(f"Task '{task_id}' not found") from None

    def _refresh(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        if meta["status"] != RUNNING:
            return meta
        tid = meta["task_id"]
        child = self._children.get(tid)
        if child is not None and child.poll() is not None:
            self._children.pop(tid, None)  # reaped; no zombie left behind
        exit_path = self._p(tid, "exit")
        if os.path.exists(exit_path):
            try:
                with open(exit_path, encoding="utf-8") as f:
                    ex = json.load(f)
            except (OSError, ValueError):
                return meta  # being written right now; read again next time
            if ex.get("timed_out"):
                status = TIMED_OUT
            else:
                status = SUCCEEDED if ex.get("exit_code") == 0 else FAILED
            return self._finish(tid, status, ex.get("exit_code"), ex.get("finished_at"))
        if not _alive(meta["pid"], meta.get("pid_start_ticks")):
            return self._finish(tid, LOST, None, time.time())
        return meta

    def _finish(self, task_id: str, status: str, exit_code: Optional[int], finished_at: Optional[float]):
        with self._file_lock():
            meta = self._load(task_id)
            if meta["status"] == RUNNING:  # another process may have finished it first
                meta.update(status=status, exit_code=exit_code, finished_at=finished_at or time.time())
                atomic_write_json(self._p(task_id, "json"), meta)
        return meta

    def _public(self, meta: Dict[str, Any]) -> Dict[str, Any]:
        d = {k: v for k, v in meta.items() if k not in ("pid_start_ticks", "output_file")}
        if meta.get("created_at"):
            end = meta.get("finished_at") or time.time()
            d["elapsed_seconds"] = round(end - meta["created_at"], 1)
        return d

    def get(self, task_id: str, max_output_chars: int = 60_000) -> Dict[str, Any]:
        meta = self._refresh(self._load(task_id))
        d = self._public(meta)
        if meta["status"] in TERMINAL:
            d["final_output"] = self._final_output(meta, max_output_chars)
        return d

    def _final_output(self, meta: Dict[str, Any], max_chars: int) -> str:
        out = meta.get("output_file")
        if out and os.path.exists(out):
            with open(out, encoding="utf-8", errors="replace") as f:
                text = f.read().strip()
            if text:
                return truncate_tail(text, max_chars)
        return self._tail_text(meta["task_id"], 40, max_chars)

    def _tail_text(self, task_id: str, lines: int, max_chars: int) -> str:
        path = self._p(task_id, "log")
        if not os.path.exists(path):
            return ""
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max(max_chars * 4, 4096)))
            data = f.read().decode("utf-8", errors="replace")
        return truncate_tail("\n".join(data.splitlines()[-lines:]), max_chars)

    def logs(self, task_id: str, tail_lines: int, max_chars: int, formatter=None) -> Dict[str, Any]:
        meta = self._refresh(self._load(task_id))
        raw = self.read_log(task_id, max(max_chars * 8, 65536))
        text = formatter(raw) if formatter else raw
        return {
            "task_id": task_id,
            "status": meta["status"],
            "tail_lines": tail_lines,
            "logs": truncate_tail("\n".join(text.splitlines()[-tail_lines:]), max_chars),
        }

    def cancel(self, task_id: str) -> Dict[str, Any]:
        meta = self._refresh(self._load(task_id))
        if meta["status"] != RUNNING:
            return {**self._public(meta), "message": f"Task already {meta['status']}"}
        if _alive(meta["pid"], meta.get("pid_start_ticks")):
            kill_group(meta["pid"])
        child = self._children.pop(task_id, None)
        if child is not None:
            child.poll()
        return self._public(self._finish(task_id, CANCELLED, None, time.time()))

    def list(self, limit: int = 50, status: Optional[str] = None) -> List[Dict[str, Any]]:
        metas = []
        for name in os.listdir(self.dir):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.dir, name), encoding="utf-8") as f:
                    metas.append(self._refresh(json.load(f)))
            except (OSError, ValueError, KeyError):
                continue
        metas.sort(key=lambda m: m.get("created_at", 0), reverse=True)
        if status:
            metas = [m for m in metas if m["status"] == status]
        return [self._public(m) for m in metas[:limit]]

    def prune(self, on_remove=None) -> int:
        """Delete finished tasks older than retention_days. Returns how many were removed."""
        if self.retention_days <= 0:
            return 0
        cutoff = time.time() - self.retention_days * 86400
        removed = 0
        for t in self.list(limit=1_000_000):
            if t["status"] in TERMINAL and (t.get("finished_at") or t.get("created_at", 0)) < cutoff:
                if on_remove:
                    on_remove(self.raw(t["task_id"]))
                for ext in ("json", "log", "out", "exit", "schema"):
                    try:
                        os.unlink(self._p(t["task_id"], ext))
                    except FileNotFoundError:
                        pass
                removed += 1
        removed += self._sweep_orphans(cutoff)
        return removed

    def _sweep_orphans(self, cutoff: float) -> int:
        """Delete .schema/.out/.exit files whose task record never got written (a failed start)."""
        swept = 0
        for name in os.listdir(self.dir):
            task_id, _, ext = name.rpartition(".")
            if ext not in ("schema", "out", "exit") or not task_id:
                continue
            path = os.path.join(self.dir, name)
            try:
                if os.path.exists(self._p(task_id, "json")) or os.path.getmtime(path) >= cutoff:
                    continue
                os.unlink(path)
                swept += 1
            except OSError:
                continue
        return swept
