"""Supervisor for one background task: `python -m agenthub.runner <exit_file> <timeout> -- argv...`

It runs the agent, enforces the timeout and writes the exit status to <exit_file>. Any AgentHub
process (MCP, HTTP or CLI) can then read the status without being the parent.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def _write_exit(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def main(argv: list) -> int:
    if len(argv) < 4 or argv[2] != "--":
        print("usage: python -m agenthub.runner <exit_file> <timeout_seconds> -- <argv...>", file=sys.stderr)
        return 2
    exit_file, timeout_s, cmd = argv[0], float(argv[1]), argv[3:]

    # Inherit stdout/stderr (the task log). stdin stays /dev/null from the parent.
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL)

    def forward(signum, _frame):
        try:
            proc.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    timed_out = False
    try:
        code = proc.wait(timeout=timeout_s or None)
    except subprocess.TimeoutExpired:
        timed_out = True
        print(f"\n[agenthub] task exceeded {timeout_s:.0f}s timeout; terminating", flush=True)
        proc.terminate()
        try:
            code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            code = proc.wait()

    _write_exit(exit_file, {"exit_code": code, "timed_out": timed_out, "finished_at": time.time()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
