"""Error types shared by the MCP server, the HTTP server and the CLI."""

from __future__ import annotations


class HubError(Exception):
    """Base error. `code` is a stable machine-readable string, `http_status` the REST mapping."""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class InvalidArgument(HubError):
    code = "invalid_argument"
    http_status = 400


class NotFound(HubError):
    code = "not_found"
    http_status = 404


class PolicyError(HubError):
    """The request is well-formed but the configured security policy forbids it."""

    code = "policy_denied"
    http_status = 403


class Unavailable(HubError):
    """The agent binary is missing or the agent does not support the operation."""

    code = "unavailable"
    http_status = 409


class LimitExceeded(HubError):
    code = "limit_exceeded"
    http_status = 429


class ConfigError(HubError):
    code = "config_error"
    http_status = 500


class QuotaExhausted(HubError):
    """The agent's provider reported a quota or rate limit. `retry_after` is in seconds, if known."""

    code = "quota_exhausted"
    http_status = 429

    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        return {**super().to_dict(), "retry_after_seconds": self.retry_after}
