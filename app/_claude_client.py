"""Thin wrapper around `claude_agent_sdk.query()` so audit + draft modules don't
each duplicate the message-pump logic.

Auth: uses the local Claude Code CLI session (your Max subscription). No API key
required. The `claude` binary must be on PATH and logged in (`claude auth login`).

Returns the concatenated assistant text. Calls are sync from the caller's POV
(we run the async generator inside `asyncio.run`).

Isolation: the CLI is started with NO tools, NO user/project settings and NO MCP
servers. Scraped page text is untrusted input; with tools disabled a prompt
injection inside a prospect's homepage can't make the model act on your machine.
The one exception is lead discovery, which may enable WebSearch only: a
server-side search that cannot touch this machine.
"""
from __future__ import annotations
import asyncio
import re
from typing import Optional

from claude_agent_sdk import (
    query, ClaudeAgentOptions, AssistantMessage, ResultMessage, TextBlock,
)

from app._retry import retry_api

CALL_TIMEOUT_S = 180

_AUTH_PATTERNS = re.compile(
    r"(401|authentication_error|failed to authenticate|oauth access token has expired|"
    r"not logged in|please run /login|invalid api key|re-authenticate)",
    re.IGNORECASE,
)
_RATE_PATTERNS = re.compile(r"(429|rate.?limit|overloaded|529|usage limit)", re.IGNORECASE)


class ClaudeCallError(Exception):
    """A Claude call failed. `kind` is 'auth' | 'rate_limit' | 'timeout' | 'other'.

    Auth failures are never retried and should stop a bulk run immediately —
    retrying cannot fix an expired login, it only burns a Playwright render per lead.
    """

    def __init__(self, message: str, kind: str = "other"):
        super().__init__(message)
        self.kind = kind
        self.retryable = kind in ("rate_limit", "timeout")


def _classify(text: str) -> str:
    if _AUTH_PATTERNS.search(text or ""):
        return "auth"
    if _RATE_PATTERNS.search(text or ""):
        return "rate_limit"
    return "other"


_ALLOWED_TOOLS = {"WebSearch"}


async def _query_text(*, system: str, user: str, model: Optional[str] = None,
                      max_turns: int = 1, tools: tuple[str, ...] = ()) -> str:
    """Run one query, collect all assistant text, return concatenated string."""
    text_parts: list[str] = []
    stderr_lines: list[str] = []
    if set(tools) - _ALLOWED_TOOLS:
        raise ValueError(f"only {sorted(_ALLOWED_TOOLS)} may be enabled, got {tools}")
    options = ClaudeAgentOptions(
        system_prompt=system,
        tools=list(tools),        # none by default (not merely "none allowed")
        allowed_tools=list(tools),
        setting_sources=[],       # ignore ~/.claude settings, CLAUDE.md, hooks
        mcp_servers={},           # no MCP servers
        max_turns=max_turns,
        permission_mode="default",
        stderr=lambda line: stderr_lines.append(line),
        **({"model": model} if model else {}),
    )
    result_error: str | None = None
    try:
        async for message in query(prompt=user, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage) and getattr(message, "is_error", False):
                result_error = str(getattr(message, "result", "") or "error result")
    except Exception as e:
        detail = " | ".join(s.strip() for s in stderr_lines[-8:] if s.strip())
        joined = "".join(text_parts)
        msg = f"{type(e).__name__}: {e}"
        if detail:
            msg += f" | stderr: {detail}"
        if joined:
            msg += f" | output: {joined[:300]}"
        raise ClaudeCallError(msg[:800], _classify(msg)) from e

    text = "".join(text_parts)
    if result_error:
        raise ClaudeCallError(f"{result_error} | output: {text[:300]}",
                              _classify(result_error + " " + text))
    # The CLI sometimes reports auth failures as plain assistant text.
    if text.startswith(("Failed to authenticate", "Invalid API key", "API Error")):
        raise ClaudeCallError(text[:500], _classify(text))
    return text


@retry_api(attempts=3, base_delay=2.0)
def call_claude(*, system: str, user: str, model: Optional[str] = None,
                tools: tuple[str, ...] = (), max_turns: int = 1,
                timeout_s: int = CALL_TIMEOUT_S) -> str:
    """Sync entry point. Runs the async query in a fresh event loop.

    Safe to call from FastAPI handlers via `asyncio.to_thread(call_claude, ...)`.
    Raises ClaudeCallError on any failure (check `.kind`).
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        # Scheduling onto the same running loop and blocking on the result would
        # deadlock. Fail loudly instead — callers inside a loop must offload:
        #   await asyncio.to_thread(call_claude, ...)
        raise RuntimeError(
            "call_claude was invoked from inside a running event loop. "
            "Use `await asyncio.to_thread(call_claude, ...)` instead."
        )
    try:
        return asyncio.run(
            asyncio.wait_for(
                _query_text(system=system, user=user, model=model,
                            max_turns=max_turns, tools=tools),
                timeout=timeout_s,
            )
        )
    except asyncio.TimeoutError as e:
        raise ClaudeCallError(f"timed out after {timeout_s}s", "timeout") from e


def probe(model: Optional[str] = None) -> tuple[bool, str]:
    """Cheap live check used by `pp-agent doctor`: (ok, message)."""
    try:
        out = call_claude(system="Reply with exactly: OK", user="ping", model=model)
        return ("OK" in out.upper(), out.strip()[:80] or "(empty reply)")
    except ClaudeCallError as e:
        hint = " -> run `claude auth login` in a terminal" if e.kind == "auth" else ""
        return False, f"[{e.kind}] {' '.join(str(e).split())[:200]}{hint}"
    except Exception as e:  # pragma: no cover - defensive
        return False, f"{type(e).__name__}: {str(e)[:200]}"
