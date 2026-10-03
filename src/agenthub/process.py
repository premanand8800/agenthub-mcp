"""Running agent processes safely: argv only (never a shell), no inherited stdin, hard timeouts."""

from __future__ import annotations

import fnmatch
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional

from agenthub.security import DEPTH_ENV, current_depth

# What every agent needs to find its tools and behave normally. Credentials are not here:
# each adapter adds its own API-key variables, and users add more with `env_passthrough`.
BASE_ENV_ALLOW = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "COLORTERM",
    "TZ",
    "TMPDIR",
    "EDITOR",
    "LANG",
    "LANGUAGE",
    "LC_*",
    "XDG_*",
    "DBUS_SESSION_BUS_ADDRESS",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
    # Toolchains agents use to build and test code.
    "NVM_*",
    "VOLTA_*",
    "PNPM_HOME",
    "BUN_INSTALL",
    "DENO_*",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "GOPATH",
    "GOROOT",
    "GOBIN",
    "JAVA_HOME",
    "VIRTUAL_ENV",
    "CONDA_*",
    "PYENV_*",
    "UV_*",
    "PIPX_*",
    "NO_COLOR",
    "FORCE_COLOR",
)


@dataclass
class RunResult:
    returncode: Optional[int]
    stdout: str
    stderr: str
    timed_out: bool


def child_env(extra: Optional[Dict[str, str]] = None, allow: Optional[Iterable[str]] = None) -> Dict[str, str]:
    """Environment for an agent. `allow=None` passes everything; otherwise only matching names."""
    if allow is None:
        env = dict(os.environ)
    else:
        patterns = [*BASE_ENV_ALLOW, *allow]
        env = {k: v for k, v in os.environ.items() if any(fnmatch.fnmatchcase(k, p) for p in patterns)}
    env[DEPTH_ENV] = str(current_depth() + 1)
    env.setdefault("NO_COLOR", "1")
    if extra:
        env.update(extra)
    return env


def kill_group(pid: int, grace: float = 2.0) -> None:
    """SIGTERM the process group, then SIGKILL whatever is left after `grace` seconds."""
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(int(grace / 0.1)):
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def truncate_tail(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier characters truncated ...]\n" + text[-limit:]


def run_capture(
    argv: List[str],
    cwd: str,
    timeout: Optional[float],
    env: Optional[Dict[str, str]] = None,
    max_output_chars: int = 0,
    on_spawn: Optional[Callable[[int], None]] = None,
    on_exit: Optional[Callable[[int], None]] = None,
) -> RunResult:
    """Run to completion. stdin is /dev/null: agents must never read the MCP stdio stream.

    `on_spawn`/`on_exit` receive the process-group ID so the caller can kill in-flight runs
    when it shuts down.
    """
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env if env is not None else child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,  # own process group, so a timeout kills grandchildren too
    )
    if on_spawn:
        on_spawn(proc.pid)
    try:
        try:
            out, err = proc.communicate(timeout=timeout or None)
            timed_out = False
        except subprocess.TimeoutExpired:
            kill_group(proc.pid)
            out, err = proc.communicate()
            timed_out = True
    finally:
        if on_exit:
            on_exit(proc.pid)
    return RunResult(
        returncode=proc.returncode,
        stdout=truncate_tail(out or "", max_output_chars),
        stderr=truncate_tail(err or "", max_output_chars),
        timed_out=timed_out,
    )


def git(args: List[str], cwd: str, input_text: Optional[str] = None, check: bool = True) -> str:
    """Run git with a fixed, minimal environment. Raises RuntimeError with git's message on failure."""
    env = child_env({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}, allow=("GIT_AUTHOR_*", "GIT_COMMITTER_*"))
    res = subprocess.run(
        ["git", *args], cwd=cwd, input=input_text, capture_output=True, text=True, env=env, timeout=120
    )
    if check and res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip() or f"git {args[0]} failed")
    return res.stdout
