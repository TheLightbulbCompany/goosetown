"""Quota exception used by UserService and the register_agent router.

Maps to HTTP 429 Too Many Requests via the FastAPI exception handler in
main.py. Every quota check raises this with a structured payload so the
error response includes the actual limit and current count.
"""


class QuotaExceeded(Exception):
    """Raised when a user exceeds a hard quota.

    Attributes:
        kind: short identifier — 'agents', 'sprite_generation', etc.
        limit: the configured cap
        current: the user's current count at the point of the raise
    """

    def __init__(self, kind: str, limit: int, current: int) -> None:
        super().__init__(f"{kind} quota exceeded: {current} >= {limit}")
        self.kind = kind
        self.limit = limit
        self.current = current
