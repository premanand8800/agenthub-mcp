"""`agenthub` command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from typing import Any, List, Optional

from agenthub import __version__
from agenthub.config import DEFAULTS, PERMISSION_MODES, home_dir, load_config
from agenthub.errors import HubError


def _print(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _hub():
    from agenthub.hub import Hub

    return Hub()


# -- commands ----------------------------------------------------------------


def cmd_mcp(args) -> int:
    from agenthub import mcp_server

    mcp_server.run()
    return 0


def cmd_serve(args) -> int:
    from agenthub import http_server

    http_server.run(args.host, args.port, args.allow_remote)
    return 0


def cmd_agents(args) -> int:
    data = _hub().list_agents()
    if args.json:
        _print(data)
        return 0
    for a in data["agents"]:
        mark = "installed" if a["installed"] else "missing  "
        health = a["health"]
        note = f"  QUOTA EXHAUSTED ~{health['retry_after_seconds']}s" if health["status"] != "ok" else ""
        print(f"  {a['name']:<12} {mark}  modes: {', '.join(a['permission_modes'])}{note}")
        if not a["installed"] and a["install_hint"]:
            print(f"  {'':<12} {a['install_hint']}")
    for err in data["custom_agent_errors"]:
        print(f"  warning: {err}")
    print(
        f"\nDefault permission mode: {data['default_permission_mode']}"
        f" | full access allowed: {data['full_access_allowed']}"
    )
    return 0


def _run_opts(args) -> dict:
    opts = {
        "workdir": args.workdir or os.getcwd(),
        "permission_mode": args.mode,
        "model": args.model,
        "session_id": args.session,
        "add_dirs": [os.path.abspath(d) for d in args.add_dir] or None,
        "images": [os.path.abspath(i) for i in args.image] or None,
        "fallback_agents": args.fallback or None,
    }
    if args.schema:
        with open(args.schema, encoding="utf-8") as f:
            opts["output_schema"] = json.load(f)
    return {k: v for k, v in opts.items() if v is not None}


def _print_reply(res: dict, as_json: bool) -> int:
    if as_json:
        _print(res)
    else:
        print(res.get("reply", ""))
        if not res.get("ok"):
            print(f"\n[{res.get('status')}] {res.get('error', '')}", file=sys.stderr)
        elif res.get("session_id"):
            print(f"\n[session {res['session_id']}]", file=sys.stderr)
    return 0 if res.get("ok") else 1


def cmd_ask(args) -> int:
    return _print_reply(_hub().ask(args.agent, args.prompt, source="cli", **_run_opts(args)), args.json)


def cmd_run(args) -> int:
    task = _hub().start_task(args.agent, args.prompt, isolation=args.isolation, source="cli", **_run_opts(args))
    if args.json:
        _print(task)
    else:
        print(task["task_id"])
    return 0


def cmd_wait(args) -> int:
    res = _hub().wait_task(args.task_id, args.timeout)
    _print(res)
    if not res["finished"]:
        return 3
    return 0 if res["status"] == "succeeded" else 1


def cmd_diff(args) -> int:
    res = _hub().get_task_diff(args.task_id, 1_000_000)
    if args.json:
        _print(res)
    else:
        print(res["stat"] or "(no changes)")
        print()
        print(res["patch"])
    return 0


def cmd_apply(args) -> int:
    _print(_hub().apply_task(args.task_id, args.keep, source="cli"))
    return 0


def cmd_discard(args) -> int:
    _print(_hub().discard_task(args.task_id, source="cli"))
    return 0


def cmd_review(args) -> int:
    res = _hub().review(
        args.agent,
        workdir=args.workdir or os.getcwd(),
        base=args.base,
        task_id=args.task,
        instructions=args.instructions,
        model=args.model,
        fallback_agents=args.fallback or None,
        source="cli",
    )
    return _print_reply(res, args.json)


def cmd_compare(args) -> int:
    res = _hub().compare(args.agents.split(","), args.prompt, workdir=args.workdir or os.getcwd(), source="cli")
    if args.json:
        _print(res)
        return 0 if res["succeeded"] else 1
    for r in res["results"]:
        print(f"===== {r['agent']} ({'ok' if r.get('ok') else r.get('status', 'error')}) =====")
        print(r.get("reply") or r.get("error", ""))
        print()
    return 0 if res["succeeded"] else 1


def cmd_tasks(args) -> int:
    data = _hub().list_tasks(args.limit, args.status)
    if args.json:
        _print(data)
        return 0
    if not data["tasks"]:
        print("No tasks.")
    for t in data["tasks"]:
        print(f"  {t['task_id']:<44} {t['status']:<10} {t.get('elapsed_seconds', 0):>8}s  {t['workdir']}")
    return 0


def cmd_task(args) -> int:
    _print(_hub().get_task(args.task_id))
    return 0


def cmd_logs(args) -> int:
    res = _hub().task_logs(args.task_id, args.tail)
    print(res["logs"])
    return 0


def cmd_cancel(args) -> int:
    _print(_hub().cancel_task(args.task_id, source="cli"))
    return 0


def cmd_token(args) -> int:
    from agenthub.security import ensure_private_dir, load_or_create_token, rotate_token

    cfg = load_config()
    ensure_private_dir(cfg.home)
    tok = rotate_token(cfg.token_path) if args.rotate else load_or_create_token(cfg.token_path)
    print(tok)
    if args.rotate:
        print("Token rotated. Restart `agenthub serve` and update your clients.", file=sys.stderr)
    return 0


def cmd_audit(args) -> int:
    from agenthub.security import AuditLog

    log = AuditLog(load_config().audit_path)
    if args.verify:
        ok, n, problem = log.verify()
        print(f"{'OK' if ok else 'TAMPERED'}: {n} entries checked" + (f"; {problem}" if problem else ""))
        return 0 if ok else 1
    for e in log.tail(args.tail):
        if args.json:
            print(json.dumps(e))
        else:
            d = e.get("details", {})
            extra = " ".join(f"{k}={v}" for k, v in d.items() if k != "prompt")
            print(
                f"{e.get('ts')}  {e.get('source', ''):<4} {e.get('action', ''):<12} "
                f"{(e.get('agent') or '-'):<12} {e.get('status', ''):<10} {extra}"
            )
    return 0


def cmd_config(args) -> int:
    path = os.path.join(home_dir(), "config.json")
    if args.init:
        if os.path.exists(path) and not args.force:
            print(f"{path} already exists (use --force to overwrite).", file=sys.stderr)
            return 1
        from agenthub.security import atomic_write_json, ensure_private_dir

        ensure_private_dir(home_dir())
        atomic_write_json(path, DEFAULTS)
        print(f"Wrote {path}")
        return 0
    _print(load_config().to_public_dict())
    return 0


def cmd_approve(args) -> int:
    """Human-only: add allow rules to Antigravity. Deliberately not exposed over MCP or HTTP."""
    from agenthub.adapters.antigravity import add_allow_rules
    from agenthub.security import AuditLog

    if args.agent != "antigravity":
        print("Only 'antigravity' stores tool allow-rules that AgentHub can edit.", file=sys.stderr)
        return 2
    for rule in args.rules:
        if not rule.strip() or len(rule) > 300 or any(c in rule for c in "\n\r\x00"):
            print(f"Invalid rule: {rule!r}", file=sys.stderr)
            return 2
    allow = add_allow_rules(args.rules)
    AuditLog(load_config().audit_path).record("cli", "approve_rules", "succeeded", "antigravity", rules=args.rules)
    print("Antigravity allow rules now:\n  " + "\n  ".join(allow))
    return 0


def _mcp_command() -> List[str]:
    exe = shutil.which("agenthub")
    if exe and os.path.realpath(exe) == os.path.realpath(sys.argv[0]):
        return [exe, "mcp"]
    return [sys.executable, "-m", "agenthub", "mcp"]


def cmd_install_claude(args) -> int:
    cmd = _mcp_command()
    claude = shutil.which("claude")
    add = ["claude", "mcp", "add", "--scope", args.scope, "agenthub", "--", *cmd]
    if not claude:
        print("Claude Code CLI not found. Run this after installing it:\n  " + " ".join(add))
        return 1
    existing = subprocess.run([claude, "mcp", "get", "agenthub"], capture_output=True, text=True)
    if existing.returncode == 0:
        if not args.force:
            print(
                "An 'agenthub' MCP server is already registered:\n"
                + existing.stdout
                + "\nRe-run with --force to replace it."
            )
            return 1
        subprocess.run([claude, "mcp", "remove", "agenthub", "--scope", args.scope], capture_output=True, text=True)
    res = subprocess.run([claude, *add[1:]], capture_output=True, text=True)
    print(res.stdout or res.stderr)
    if res.returncode == 0:
        print('Done. Restart Claude Code, then try: "use agenthub to list agents".')
    return res.returncode


def cmd_doctor(args) -> int:
    problems = 0

    def check(ok: bool, msg: str, fix: str = "") -> None:
        nonlocal problems
        print(f"  [{'ok' if ok else '!!'}] {msg}" + (f"\n       fix: {fix}" if not ok and fix else ""))
        problems += 0 if ok else 1

    print(f"AgentHub {__version__}  (Python {sys.version.split()[0]})")
    check(sys.version_info >= (3, 10), "Python >= 3.10", "Install Python 3.10 or newer")
    check(os.name == "posix", "POSIX OS (Linux/macOS)", "Windows is not supported; use WSL")
    try:
        cfg = load_config()
        check(True, f"config valid ({cfg.config_path if os.path.exists(cfg.config_path) else 'defaults'})")
    except HubError as e:
        check(False, f"config: {e.message}", "Fix or delete config.json, or run: agenthub config --init --force")
        return 1
    if os.path.isdir(cfg.home):
        mode = os.stat(cfg.home).st_mode & 0o777
        check(mode & 0o077 == 0, f"{cfg.home} permissions {oct(mode)}", f"chmod 700 {cfg.home}")
    if os.path.exists(cfg.token_path):
        mode = os.stat(cfg.token_path).st_mode & 0o777
        check(mode & 0o077 == 0, f"token file permissions {oct(mode)}", f"chmod 600 {cfg.token_path}")
    check(
        not cfg.allow_full_access,
        "allow_full_access is off",
        "Only enable it if agents run in a disposable VM or container",
    )
    home = os.path.realpath(os.path.expanduser("~"))
    check(
        home not in cfg.trusted_workspaces,
        "trusted_workspaces narrower than your home directory",
        f'Set "trusted_workspaces": ["~/code"] (or similar) in {cfg.config_path}',
    )

    hub = _hub()
    installed = [a for a in hub.registry.all() if a.executable()]
    check(
        bool(installed),
        f"agents installed: {', '.join(a.name for a in installed) or 'none'}",
        "Install at least one agent, e.g. npm install -g @openai/codex",
    )
    for err in hub.registry.load_errors:
        check(False, f"custom agent: {err}")
    ok, n, problem = hub.audit.verify()
    check(ok, f"audit log chain ({n} entries)", problem or "")

    claude = shutil.which("claude")
    if claude:
        res = subprocess.run([claude, "mcp", "get", "agenthub"], capture_output=True, text=True)
        check(res.returncode == 0, "registered in Claude Code", "agenthub install-claude")
    else:
        check(False, "Claude Code CLI on PATH", "https://docs.claude.com/claude-code")
    print("\nAll good." if not problems else f"\n{problems} item(s) to look at.")
    return 0 if not problems else 1


# -- parser ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agenthub", description="Secure gateway from Claude Code to local coding agents.")
    p.add_argument("--version", action="version", version=f"agenthub {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(fn=fn)
        return sp

    add("mcp", cmd_mcp, "Run the MCP server on stdio (Claude Code starts this for you)")
    sp = add("serve", cmd_serve, "Run the HTTP API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--allow-remote", action="store_true", help="Allow non-loopback binds (use a TLS proxy)")

    sp = add("agents", cmd_agents, "List agents and what they support")
    sp.add_argument("--json", action="store_true")

    for name, fn, help_ in (
        ("ask", cmd_ask, "Ask an agent and wait for the answer"),
        ("run", cmd_run, "Start a background task; prints its task ID"),
    ):
        sp = add(name, fn, help_)
        sp.add_argument("agent")
        sp.add_argument("prompt")
        sp.add_argument("-C", "--workdir", help="Project directory (default: current directory)")
        sp.add_argument("-m", "--mode", choices=PERMISSION_MODES, help="Permission mode")
        sp.add_argument("--model")
        sp.add_argument("--session", help="Session ID to continue")
        sp.add_argument("--add-dir", action="append", default=[], help="Extra directory (repeatable)")
        sp.add_argument("--image", action="append", default=[], help="Image to attach (repeatable)")
        sp.add_argument("--schema", help="JSON Schema file for structured output")
        sp.add_argument("--fallback", action="append", default=[], help="Agent to try if this one is out of quota")
        sp.add_argument("--json", action="store_true")
        if name == "run":
            sp.add_argument("--isolation", choices=("none", "worktree"), help="worktree = private git checkout")

    sp = add("wait", cmd_wait, "Wait for a task (exit 0 succeeded, 1 failed, 3 still running)")
    sp.add_argument("task_id")
    sp.add_argument("--timeout", type=int, default=600)
    sp = add("diff", cmd_diff, "Show a worktree task's changes")
    sp.add_argument("task_id")
    sp.add_argument("--json", action="store_true")
    sp = add("apply", cmd_apply, "Apply a worktree task's changes to your working tree")
    sp.add_argument("task_id")
    sp.add_argument("--keep", action="store_true", help="Keep the worktree after applying")
    sp = add("discard", cmd_discard, "Throw away a worktree task's changes")
    sp.add_argument("task_id")
    sp = add("review", cmd_review, "Have an agent review your uncommitted changes (or a task's diff)")
    sp.add_argument("agent")
    sp.add_argument("-C", "--workdir")
    sp.add_argument("--base", help="git ref to diff against (default HEAD)")
    sp.add_argument("--task", help="Review this worktree task's changes instead")
    sp.add_argument("--instructions")
    sp.add_argument("--model")
    sp.add_argument("--fallback", action="append", default=[])
    sp.add_argument("--json", action="store_true")
    sp = add("compare", cmd_compare, "Same prompt to several agents, side by side")
    sp.add_argument("agents", help="Comma-separated, e.g. codex,claude")
    sp.add_argument("prompt")
    sp.add_argument("-C", "--workdir")
    sp.add_argument("--json", action="store_true")

    sp = add("tasks", cmd_tasks, "List background tasks")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--status")
    sp.add_argument("--json", action="store_true")
    for name, fn, help_ in (("task", cmd_task, "Show one task"), ("cancel", cmd_cancel, "Cancel a task")):
        sp = add(name, fn, help_)
        sp.add_argument("task_id")
    sp = add("logs", cmd_logs, "Show a task's log")
    sp.add_argument("task_id")
    sp.add_argument("--tail", type=int, default=100)

    sp = add("token", cmd_token, "Print the HTTP API bearer token")
    sp.add_argument("--rotate", action="store_true", help="Create a new token (old one stops working)")

    sp = add("audit", cmd_audit, "Show or verify the audit log")
    sp.add_argument("--tail", type=int, default=20)
    sp.add_argument("--verify", action="store_true", help="Check the hash chain for edits or deletions")
    sp.add_argument("--json", action="store_true")

    sp = add("config", cmd_config, "Show effective config, or write a starter config.json")
    sp.add_argument("--init", action="store_true")
    sp.add_argument("--force", action="store_true")

    sp = add("approve", cmd_approve, "Add Antigravity tool allow-rules (human-only)")
    sp.add_argument("agent")
    sp.add_argument("rules", nargs="+")

    sp = add("install-claude", cmd_install_claude, "Register AgentHub as an MCP server in Claude Code")
    sp.add_argument("--scope", choices=("user", "project", "local"), default="user")
    sp.add_argument("--force", action="store_true", help="Replace an existing 'agenthub' registration")

    add("doctor", cmd_doctor, "Check installation, config and security settings")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "fn", None):
        parser.print_help()
        return 2
    try:
        return args.fn(args) or 0
    except HubError as e:
        print(f"error: {e.message}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
