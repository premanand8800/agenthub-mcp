"""Remembers which agents recently hit a quota or rate limit, so callers fail fast or fall back.

State lives in ~/.agenthub/health.json and is shared by every AgentHub process.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

from agenthub.security import atomic_write_json

_QUOTA_RE = re.compile(
    r"quota|rate[ _-]?limit|resource_exhausted|usage limit|too many requests|"
    r"\b(?:http|status|code|error)[ :=]*429\b|\(429\)",
    re.IGNORECASE,
)
_WHEN_RE = re.compile(r"(?:resets?|retry|try again)\D{0,20}?((?:\d+\s*(?:d|h|m|s)[a-z]*\s*)+)", re.IGNORECASE)
_PART_RE = re.compile(r"(\d+)\s*(d|h|m|s)", re.IGNORECASE)
_UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def detect_quota(text: str) -> Optional[Tuple[str, Optional[int]]]:
    """Return (message, retry_after_seconds) if `text` reports a quota or rate limit."""
    if not text:
        return None
    for line in text.splitlines():
        if _QUOTA_RE.search(line):
            retry = None
            m = _WHEN_RE.search(line)
            if m:
                retry = sum(int(n) * _UNIT[u.lower()] for n, u in _PART_RE.findall(m.group(1))) or None
            return line.strip()[:500], retry
    return None


class HealthStore:
    def __init__(self, path: str, default_backoff: int):
        self.path = path
        self.default_backoff = default_backoff

    def _locked(self):
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        f = os.fdopen(fd, "r+")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        return f

    def _read(self) -> Dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, agent: str) -> Dict[str, Any]:
        """{"status": "ok"} or {"status": "quota_exhausted", "until": ts, "retry_after_seconds", "message"}."""
        entry = self._read().get(agent)
        now = time.time()
        if not entry or entry.get("until", 0) <= now:
            return {"status": "ok"}
        return {**entry, "retry_after_seconds": int(entry["until"] - now)}

    def mark_quota(self, agent: str, message: str, retry_after: Optional[int]) -> int:
        wait = retry_after or self.default_backoff
        with self._locked():
            data = self._read()
            data[agent] = {"status": "quota_exhausted", "message": message, "until": time.time() + wait}
            atomic_write_json(self.path, data)
        return wait

    def clear(self, agent: str) -> None:
        if agent not in self._read():
            return
        with self._locked():
            data = self._read()
            data.pop(agent, None)
            atomic_write_json(self.path, data)
