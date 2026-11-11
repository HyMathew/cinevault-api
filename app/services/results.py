"""Calculated service results; transport rendering happens after commit."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServiceResult:
    status: int
    body: dict[str, Any] | None = field(default=None, repr=False)
    error: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


def failure(status, code, message, *, headers=None):
    return ServiceResult(
        status=status,
        error={"code": code, "message": message},
        headers={} if headers is None else dict(headers),
    )
