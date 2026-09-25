"""Errors shared by the engine modules: the user-facing one every operation raises, a broken fixed contract, a failed workspace
and a job's stop at a safe point."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class Stop(Exception):
    """A job limit reached or a cancellation requested: the run stops before the next model call or case, without scoring the candidate.

    ``reason`` is ``time_limit``, ``budget`` or ``cancelled``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class JobError(Exception):
    """User-facing reason an operation cannot proceed; never contains credentials.

    ``details`` are the structured fields the error payload adds next to its message and code, such as the command a person runs to go on.
    """

    def __init__(self, message: str, code: str = "invalid-request", details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class ContractError(ValueError):
    """An artifact, a case or a proposal that breaks the adapter's fixed contract."""


class WorkspaceError(JobError):
    """The isolated workspace could not be set up or read: infrastructure, never the candidate's result."""

    def __init__(self, message: str, code: str = "infrastructure-error") -> None:
        super().__init__(message, code)
