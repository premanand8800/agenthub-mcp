# Repro: TaskStore.start() deadlocks when another task is still marked "running" but has already finished.
import json
import os
import signal
import tempfile
import time

from agenthub.tasks import TaskStore

d = tempfile.mkdtemp()
os.chmod(d, 0o700)
s = TaskStore(d, max_concurrent=4, timeout_seconds=60, retention_days=30)
# A finished task whose meta still says "running": exit file written by the runner, nobody refreshed the meta yet.
tid = "codex-20260101-000000-deadbeef"
json.dump(
    {
        "task_id": tid,
        "agent": "codex",
        "status": "running",
        "pid": 99999999,
        "pid_start_ticks": None,
        "workdir": d,
        "permission_mode": "read-only",
        "model": None,
        "prompt": {},
        "output_file": None,
        "created_at": time.time(),
        "finished_at": None,
    },
    open(os.path.join(d, tid + ".json"), "w"),
)
json.dump({"exit_code": 0, "finished_at": time.time()}, open(os.path.join(d, tid + ".exit"), "w"))
signal.signal(
    signal.SIGALRM, lambda *a: (print("DEADLOCK: start() did not return within 5 s", flush=True), os._exit(1))
)
signal.alarm(5)
s.start("codex", ["true"], d, {}, "read-only", None)
print("start() returned (no bug)")
