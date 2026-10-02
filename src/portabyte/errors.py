"""Errors returned by the Portabyte API or upload gateway."""

from typing import Optional


class PortabyteError(Exception):
    """A failed Portabyte request.

    ``status`` is 0 for network or response parsing errors. ``request_id`` is
    available when the server returns one and can help support trace a request.
    """

    def __init__(self, message: str, status: int = 0, code: str = "request_failed",
                 request_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.request_id = request_id
