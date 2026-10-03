"""Security primitives: private files, the bearer token, path policy, input validation, audit log."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import sys
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from agenthub.config import Config
from agenthub.errors import InvalidArgument, PolicyError

# ---------------------------------------------------------------------------
# Private files
# ---------------------------------------------------------------------------


def ensure_private_dir(path: str) -> None:
    """Create `path` (mode 0700). Tighten it if it already exists with looser permissions."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if st.st_uid == os.getuid() and st.st_mode & 0o077:
        os.chmod(path, 0o700)


def atomic_write(path: str, data: str, mode: int = 0o600) -> None:
    """Write via a temp file in the same directory and rename, so readers never see a partial file."""
    directory = os.path.dirname(os.path.abspath(path))
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj: Any, mode: int = 0o600) -> None:
    atomic_write(path, json.dumps(obj, indent=2, sort_keys=True) + "\n", mode)


# ---------------------------------------------------------------------------
# Bearer token (HTTP API only; stdio MCP is authenticated by the OS process boundary)
# ---------------------------------------------------------------------------

TOKEN_MIN_LEN = 32


def load_or_create_token(path: str) -> str:
    if os.path.exists(path):
        st = os.stat(path)
        if st.st_mode & 0o077:
            os.chmod(path, 0o600)
        with open(path, encoding="utf-8") as f:
            tok = f.read().strip()
        if len(tok) >= TOKEN_MIN_LEN:
            return tok
    return rotate_token(path)


def rotate_token(path: str) -> str:
    tok = secrets.token_urlsafe(32)
    atomic_write(path, tok + "\n", 0o600)
    return tok


def token_matches(expected: str, authorization_header: Optional[str]) -> bool:
    """Constant-time check of an `Authorization: Bearer <token>` header."""
    if not authorization_header:
        return False
    scheme, _, provided = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not provided:
        return False
    return secrets.compare_digest(expected.encode(), provided.strip().encode())


# ---------------------------------------------------------------------------
# Path policy
# ---------------------------------------------------------------------------


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def resolve_workdir(requested: Optional[str], config: Config, default: Optional[str] = None) -> str:
    """Return the canonical working directory or raise PolicyError.

    Symlinks are resolved before the check, so a link inside a trusted workspace that
    points outside it is refused.
    """
    raw = requested or default or config.trusted_workspaces[0]
    if not isinstance(raw, str) or "\x00" in raw:
        raise InvalidArgument("workdir must be a string without NUL bytes")
    if not os.path.isabs(os.path.expanduser(raw)):
        raise InvalidArgument(f"workdir must be an absolute path, got '{raw}'")
    path = os.path.realpath(os.path.expanduser(raw))
    if not os.path.isdir(path):
        raise InvalidArgument(f"workdir '{path}' does not exist or is not a directory")
    for denied in config.denied_paths:
        if _is_within(path, denied):
            raise PolicyError(f"workdir '{path}' is inside denied path '{denied}'")
    if not any(_is_within(path, root) for root in config.trusted_workspaces):
        raise PolicyError(
            f"workdir '{path}' is outside trusted_workspaces {config.trusted_workspaces}. "
            f"Add it to {config.config_path} to allow it."
        )
    return path


def resolve_file(requested: Any, config: Config, field: str = "file") -> str:
    """Like resolve_workdir, for a single existing file (e.g. an image to attach)."""
    if not isinstance(requested, str) or "\x00" in requested:
        raise InvalidArgument(f"{field} must be a string without NUL bytes")
    if not os.path.isabs(os.path.expanduser(requested)):
        raise InvalidArgument(f"{field} must be an absolute path, got '{requested}'")
    path = os.path.realpath(os.path.expanduser(requested))
    if not os.path.isfile(path):
        raise InvalidArgument(f"{field} '{path}' does not exist or is not a file")
    resolve_workdir(os.path.dirname(path), config)  # same policy as directories
    return path


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")


def validate_id(value: Any, field: str) -> str:
    """IDs end up in argv and file paths. Allow no slashes, no leading dash, no '..'."""
    if not isinstance(value, str) or not _ID_RE.match(value) or ".." in value:
        raise InvalidArgument(f"{field} must match {_ID_RE.pattern} (got {value!r})")
    return value


def validate_model(value: Any) -> str:
    if not isinstance(value, str) or not _MODEL_RE.match(value) or ".." in value:
        raise InvalidArgument(f"model must match {_MODEL_RE.pattern} (got {value!r})")
    return value


def validate_text(value: Any, field: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidArgument(f"{field} must be a non-empty string")
    if "\x00" in value:
        raise InvalidArgument(f"{field} must not contain NUL bytes")
    if len(value) > max_chars:
        raise InvalidArgument(f"{field} is {len(value)} chars; the limit is {max_chars}")
    return value


def safe_positional(text: str) -> str:
    """Stop a prompt from being parsed as a CLI option when an adapter cannot use `--`."""
    return " " + text if text.startswith("-") else text


# ---------------------------------------------------------------------------
# Delegation depth (agent -> hub -> agent loops)
# ---------------------------------------------------------------------------

DEPTH_ENV = "AGENTHUB_DEPTH"


def current_depth() -> int:
    try:
        return max(0, int(os.environ.get(DEPTH_ENV, "0")))
    except ValueError:
        return 0


def check_depth(config: Config) -> int:
    depth = current_depth()
    if depth >= config.max_delegation_depth:
        raise PolicyError(
            f"Delegation depth {depth} reached max_delegation_depth={config.max_delegation_depth}. "
            "An agent started by AgentHub tried to start another agent."
        )
    return depth


# ---------------------------------------------------------------------------
# Audit log: append-only JSONL with a SHA-256 hash chain
# ---------------------------------------------------------------------------

GENESIS = "0" * 64
AUDIT_ROTATE_BYTES = 10 * 1024 * 1024


def _entry_hash(entry: Dict[str, Any]) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _last_hash(f) -> str:
    f.seek(0, os.SEEK_END)
    size = f.tell()
    if size == 0:
        return GENESIS
    f.seek(max(0, size - 65536))
    lines = f.read().splitlines()
    for line in reversed(lines):
        try:
            return json.loads(line)["hash"]
        except (ValueError, KeyError, TypeError):
            continue
    return GENESIS


class AuditLog:
    """Each entry stores the previous entry's hash. `verify()` detects edited or deleted lines.

    This is tamper-evident, not tamper-proof: a user who can write the file can rewrite the
    whole chain. Ship the log elsewhere if you need stronger guarantees.
    """

    def __init__(self, path: str, prompt_preview_chars: int = 80):
        self.path = path
        self.prompt_preview_chars = prompt_preview_chars

    def describe_prompt(self, prompt: str) -> Dict[str, Any]:
        d: Dict[str, Any] = {"chars": len(prompt), "sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        if self.prompt_preview_chars:
            d["preview"] = prompt[: self.prompt_preview_chars]
        return d

    def record(self, source: str, action: str, status: str, agent: Optional[str] = None, **details: Any) -> None:
        entry: Dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": source,
            "action": action,
            "agent": agent,
            "status": status,
            "details": details,
        }
        try:
            self._rotate_if_needed()
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "r+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    entry["prev"] = _last_hash(f)
                    entry["hash"] = _entry_hash(entry)
                    f.seek(0, os.SEEK_END)
                    f.write(json.dumps(entry, sort_keys=True) + "\n")
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError as e:
            print(f"agenthub: audit log write failed: {e}", file=sys.stderr)

    def _rotate_if_needed(self) -> None:
        try:
            if os.path.getsize(self.path) > AUDIT_ROTATE_BYTES:
                os.replace(self.path, self.path + ".1")
        except OSError:
            pass

    def entries(self) -> Iterator[Dict[str, Any]]:
        if not os.path.exists(self.path):
            return
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except ValueError:
                        yield {"_invalid": line}

    def tail(self, n: int) -> List[Dict[str, Any]]:
        return list(self.entries())[-n:] if n > 0 else []

    def verify(self) -> Tuple[bool, int, Optional[str]]:
        """Return (ok, entries_checked, first_problem)."""
        prev: Optional[str] = None
        count = 0
        for count, entry in enumerate(self.entries(), start=1):
            if "_invalid" in entry:
                return False, count, f"line {count}: not valid JSON"
            if entry.get("hash") != _entry_hash(entry):
                return False, count, f"line {count}: hash mismatch (entry was edited)"
            if prev is not None and entry.get("prev") != prev:
                return False, count, f"line {count}: chain broken (an entry before it was removed or edited)"
            prev = entry.get("hash")
        return True, count, None
