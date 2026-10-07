# AgentHub bug report: `start_task` self-deadlock (agenthub-gateway 1.2.2)

Found 4–7 Oct 2026 while driving Codex through the AgentHub MCP. Written for the AgentHub author (Prem).

**Status: fixed in 1.2.3.** The lock is re-entrant per thread and times out after 30 seconds with a "task store busy" error. Failed starts clean up their `.schema`/`.out` files, `prune` sweeps old orphans, `get_task`/`wait_task` drop `final_output` when `structured` parsed, and the tool descriptions mention the Codex `add_dirs` + `session_id` limit. Regression tests are in `tests/test_features.py` (`TaskStoreRegressionTests`).

## Symptom
`start_task` sometimes never returns. The MCP client reports "still running after 120s" and moves the call to the background. Nothing is launched, no task record is created, and the audit log has no entry for it. The only trace is an orphan `~/.agenthub/tasks/<task_id>.schema` file when `output_schema` was passed (found two: `codex-20261004-015947-6683252e.schema` and `codex-20261004-024352-fb91e43a.schema`).

It happened twice, always right after several earlier tasks had finished and nobody had called `get_task`/`wait_task` on them. Earlier `start_task` calls worked because every finished task had been read first.

## Root cause
`TaskStore.start()` (`agenthub/tasks.py`) holds `self._lock` and the file lock (`flock` on `tasks/.lock`), and then calls `self.list(limit=10_000)`:

1. `list()` calls `_refresh()` for each task whose stored status is `running`.
2. If such a task has already finished (its `.exit` file exists), `_refresh()` calls `_finish()`.
3. `_finish()` calls `_file_lock()` again. That opens a **second file descriptor** on `.lock` and calls `flock(LOCK_EX)`.
4. `flock` locks belong to the open file description, so the second descriptor in the same process conflicts with the first one and waits forever. Deadlock.

While deadlocked, the process also holds the file lock, so `start_task`, `update` and `_finish` calls from **other** AgentHub processes (one runs per Claude Code session) block as well until the stuck process is killed.

## Reproduction (deterministic, 10 lines)
`repro_start_task_deadlock.py` in this folder. It creates a temporary task store with one task marked `running` whose `.exit` file already exists, then calls `start()`:

```
$ ~/.local/share/uv/tools/agenthub-gateway/bin/python repro_start_task_deadlock.py
DEADLOCK: start() did not return within 5 s
```

## Suggested fix
Make `_file_lock()` re-entrant within a thread (count nested acquisitions and take the `flock` only at depth 0), or call `self.list()` before taking the lock in `start()` and re-check the running count under the lock. The re-entrant version was tested against the repro:

```python
_local = threading.local()


def _file_lock(self):
    @contextlib.contextmanager
    def held():
        if getattr(_local, "depth", 0) > 0:  # this thread already holds the flock
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        fd = os.open(os.path.join(self.dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _local.depth = 1
            yield
        finally:
            _local.depth = 0
            os.close(fd)

    return held()
```

Result with the patch: `start() returned (no bug)`.

A user-side workaround until it is fixed: refresh every finished task before the next `start_task`, for example with `agenthub tasks --limit 50` (the CLI refreshes statuses without holding the lock). Verified on 7 Oct: 16 tasks in four waves of four ran without any hang. `get_task` and `wait_task` also refresh, but they print the whole output.

## Smaller issues
- **Duplicated output.** With `output_schema`, `wait_task` returns the result twice: once as the `final_output` string and once as the parsed `structured` object. For a 25-item JSON result that doubles the tokens the client has to read. Suggest returning only `structured` when a schema was given (plus a pointer to the `.out` file).
- **Orphan `.schema` files** are created before the task record and are never cleaned up if `start()` fails or hangs.
- **No timeout on the file lock.** A blocking `flock(LOCK_EX)` with no timeout turns any lock bug into a silent hang. A timed non-blocking loop with a clear error ("task store busy, held by pid N") would have made this easy to diagnose.
- **`add_dirs` with `session_id`** for Codex returns "Codex cannot add directories when resuming a session". That is correct, but the tool description for `start_task` does not say it; worth a line in the docs.
- Not an AgentHub bug, but a trap: running `codex exec` by hand with stdin attached to a pipe prints "Reading additional input from stdin..." and waits. AgentHub avoids this correctly with `stdin=subprocess.DEVNULL`.
