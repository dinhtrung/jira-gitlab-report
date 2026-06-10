"""
sse_client.py — MCP Streamable HTTP client for Dockerized Jira & GitLab servers.

Communicates over Server-Sent Events (SSE) using the MCP Streamable HTTP
transport. Each MCPSession manages one server connection lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class MCPTool:
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCallResult:
    content: list[dict[str, Any]]
    is_error: bool = False


# ---------------------------------------------------------------------------
# Low‑level JSON-RPC helpers
# ---------------------------------------------------------------------------


class MCPError(Exception):
    """Raised when an MCP JSON-RPC response carries an error."""


def _build_request(method: str, params: dict | None = None) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": _next_id(),
        "method": method,
        "params": params or {},
    }


_id_counter: int = 0


def _next_id() -> int:
    global _id_counter
    _id_counter += 1
    return _id_counter


# ---------------------------------------------------------------------------
# MCP Session
# ---------------------------------------------------------------------------


class MCPSession:
    """Manages a single MCP server connection via SSE/Streamable HTTP.

    Typical lifecycle::

        async with MCPSession("http://localhost:8080") as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("search_issues", {"jql": "..."})
    """

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._message_endpoint: str | None = None
        self._response_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._sse_task: asyncio.Task | None = None
        self._pending: dict[int, asyncio.Future[dict]] = {}
        self._closed = False

    # -- context manager ---------------------------------------------------

    async def __aenter__(self) -> MCPSession:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # -- connection --------------------------------------------------------

    async def connect(self) -> None:
        """Open SSE stream and discover the message endpoint."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"Accept": "application/json, text/event-stream"},
        )
        sse_url = self.base_url
        logger.info("Connecting to MCP SSE endpoint: %s", sse_url)

        self._sse_task = asyncio.create_task(self._read_sse(sse_url))
        for _ in range(50):  # 5 s max
            if self._message_endpoint is not None:
                break
            await asyncio.sleep(0.1)
        if self._message_endpoint is None:
            raise MCPError("Timed out waiting for SSE endpoint event")

    async def _read_sse(self, url: str) -> None:
        """Consume SSE events from *url*, dispatching to response queue."""
        assert self._client is not None
        try:
            async with self._client.stream("GET", url) as resp:
                resp.raise_for_status()
                event_name = ""
                data_lines: list[str] = []
                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:") :].strip())
                    elif line == "" and data_lines:
                        await self._dispatch_sse_event(event_name, "".join(data_lines))
                        event_name = ""
                        data_lines = []
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("SSE stream error")

    async def _dispatch_sse_event(self, event: str, data: str) -> None:
        """Handle one SSE event."""
        if event == "endpoint":
            self._message_endpoint = urljoin(self.base_url, data)
            logger.info("Message endpoint: %s", self._message_endpoint)
            return

        if event == "message":
            try:
                msg: dict = json.loads(data)
            except json.JSONDecodeError:
                logger.warning("Unparseable SSE message: %s", data)
                return
            rid = msg.get("id")
            if rid is not None and rid in self._pending:
                fut = self._pending.pop(rid)
                if "error" in msg:
                    fut.set_exception(MCPError(msg["error"].get("message", str(msg["error"]))))
                else:
                    fut.set_result(msg)
            else:
                self._response_queue.put_nowait(msg)

    # -- initialize --------------------------------------------------------

    async def initialize(self, client_name: str = "jira-gitlab-reporter") -> dict:
        """Send initialize request (required before any tool calls)."""
        params = {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "0.1.0"},
        }
        result = await self._send_request("initialize", params)
        await self._send_notification("notifications/initialized", {})
        return result

    # -- tools -------------------------------------------------------------

    async def list_tools(self) -> list[MCPTool]:
        """Return every tool advertised by the server."""
        response = await self._send_request("tools/list")
        result = response.get("result", {})
        tools: list[MCPTool] = []
        for raw in result.get("tools", []):
            tools.append(
                MCPTool(
                    name=raw["name"],
                    description=raw.get("description", ""),
                    input_schema=raw.get("inputSchema", {}),
                )
            )
        return tools

    async def call_tool(self, name: str, arguments: dict | None = None) -> ToolCallResult:
        """Invoke a named tool with keyword arguments."""
        response = await self._send_request(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        result = response.get("result", {})
        content = result.get("content", [])
        is_error = result.get("isError", False)
        return ToolCallResult(content=content, is_error=is_error)

    # -- JSON-RPC plumbing -------------------------------------------------

    async def _send_request(self, method: str, params: dict | None = None) -> dict:
        if self._message_endpoint is None:
            raise MCPError("Not connected — no message endpoint")
        req = _build_request(method, params)
        rid = req["id"]
        assert isinstance(rid, int)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[rid] = fut
        try:
            await self._post_json(req)
            return await fut
        except Exception:
            self._pending.pop(rid, None)
            raise

    async def _send_notification(self, method: str, params: dict) -> None:
        if self._message_endpoint is None:
            raise MCPError("Not connected — no message endpoint")
        notif = {"jsonrpc": "2.0", "method": method, "params": params}
        await self._post_json(notif)

    async def _post_json(self, payload: dict) -> None:
        assert self._client is not None
        assert self._message_endpoint is not None
        resp = await self._client.post(
            self._message_endpoint,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()

    # -- cleanup -----------------------------------------------------------

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._sse_task:
            self._sse_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sse_task
        if self._client:
            await self._client.aclose()
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(MCPError("Session closed"))
        self._pending.clear()

    async def drain_notifications(self, timeout: float = 1.0) -> list[dict]:
        """Collect any queued notifications (non‑blocking drain)."""
        notifications: list[dict] = []
        while True:
            try:
                msg = await asyncio.wait_for(self._response_queue.get(), timeout=timeout)
                notifications.append(msg)
            except asyncio.TimeoutError:
                break
        return notifications
