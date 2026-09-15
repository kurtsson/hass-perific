"""Exceptions raised by the Enegic client.

Nothing outside this package is raised, so the client stays independent of Home
Assistant. The integration maps these onto HA's own config-entry exceptions.
"""

from __future__ import annotations


class PerificError(Exception):
    """Base for every error this package raises."""


class PerificAuthError(PerificError):
    """Credentials or token were rejected."""


class PerificConnectionError(PerificError):
    """The API could not be reached, or the connection failed mid-request."""


class PerificRateLimitError(PerificError):
    """The API answered 429."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        """Record how long the API asked us to wait, when it said."""
        super().__init__(message)
        self.retry_after = retry_after


class PerificResponseError(PerificError):
    """The API answered, but not with something we can use."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Record the HTTP status, when the failure had one."""
        super().__init__(message)
        self.status = status
