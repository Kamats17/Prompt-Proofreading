"""Minimal retry decorator for transient API errors.

Targets: anthropic rate-limits, transient 5xx, network timeouts. We avoid the
tenacity dependency since stdlib + a small wrapper is enough for our needs.

Usage:
    @retry_api(attempts=3, base_delay=2.0)
    def call_anthropic(...): ...
"""
from __future__ import annotations
import functools
import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def _is_retryable(exc: BaseException) -> bool:
    """Conservative: retry on Anthropic rate-limit / overloaded / API status; transient network errors."""
    # Errors that classify themselves (e.g. ClaudeCallError) win: an expired
    # login must never be retried.
    explicit = getattr(exc, "retryable", None)
    if isinstance(explicit, bool):
        return explicit
    name = type(exc).__name__
    # Match anthropic SDK exception class names without importing (keeps this module dep-free).
    retryable_names = {
        "RateLimitError",
        "APITimeoutError",
        "APIConnectionError",
        "InternalServerError",
        "OverloadedError",
        "ServiceUnavailableError",
        # claude_agent_sdk (CLI transport) — the actual client used here.
        "CLIConnectionError",
        "ProcessError",
        "CLIJSONDecodeError",
        # stdlib transient transport/timeout errors.
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
    }
    if name in retryable_names:
        return True
    # APIStatusError instances expose .status_code; retry on 5xx and 429.
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    return False


def retry_api(*, attempts: int = 3, base_delay: float = 2.0,
              max_delay: float = 30.0, jitter: float = 0.5):
    """Exponential-backoff decorator with jitter."""
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except BaseException as e:
                    last_exc = e
                    if attempt >= attempts or not _is_retryable(e):
                        raise
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    delay += random.uniform(0, jitter)
                    time.sleep(delay)
            # Unreachable in practice — last_exc is always set if we exit the loop.
            assert last_exc is not None
            raise last_exc
        return wrapper
    return deco
