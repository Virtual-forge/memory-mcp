"""Exercise the MCP server through its real stdio client boundary."""

import argparse
import asyncio
import json
import os
import shutil
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

DEFAULT_REMOTE_URL = "https://footbath-handshake-devouring.ngrok-free.dev/mcp"


def unwrap_result(result: Any) -> Any:
    if getattr(result, "isError", False):
        raise RuntimeError(str(result.content))
    value = getattr(result, "structuredContent", None)
    if value is None:
        text = next((getattr(item, "text", None) for item in result.content), None)
        value = json.loads(text) if text else None
    if isinstance(value, dict) and set(value) == {"result"}:
        return value["result"]
    return value


@asynccontextmanager
async def open_session(
    command: str | None = None,
    url: str | None = None,
) -> AsyncIterator[ClientSession]:
    if url:
        async with streamable_http_client(url) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                yield session
        return

    environment = os.environ.copy()
    environment["MCP_TRANSPORT"] = "stdio"
    server = (
        StdioServerParameters(command=command, env=environment)
        if command
        else StdioServerParameters(
            command=sys.executable,
            args=["-m", "memory_manager.mcp.server"],
            env=environment,
        )
    )
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            yield session


async def exercise_session(session: ClientSession) -> dict[str, Any]:
    scope = f"mcp-smoke-{uuid4()}"
    await session.initialize()
    tools = await session.list_tools()
    tool_names = {tool.name for tool in tools.tools}
    expected_tools = {
        "remember",
        "recall",
        "list_projects",
        "resolve_project",
        "search_candidates",
        "fetch",
        "drill_down",
        "update",
        "forget",
    }
    if tool_names != expected_tools:
        raise RuntimeError(f"unexpected MCP tools: {sorted(tool_names)}")
    undocumented = [
        tool.name for tool in tools.tools if not (tool.description or "").strip()
    ]
    if undocumented:
        raise RuntimeError(f"MCP tools missing descriptions: {sorted(undocumented)}")

    remembered = unwrap_result(
        await session.call_tool(
            "remember",
            {
                "statement": "MCP smoke test memory",
                "scope": scope,
                "category": "fact",
            },
        )
    )
    atom_id = remembered["id"]
    recalled = unwrap_result(
        await session.call_tool(
            "recall",
            {"query": "MCP smoke test", "scope": scope, "top_k": 5},
        )
    )
    candidates = unwrap_result(
        await session.call_tool(
            "search_candidates",
            {"query": "MCP smoke test", "scope": scope, "top_k": 5},
        )
    )
    fetched = unwrap_result(
        await session.call_tool("fetch", {"ids": [atom_id], "scope": scope})
    )
    drilled_down = unwrap_result(
        await session.call_tool(
            "drill_down",
            {"id": atom_id, "source_table": "atoms", "scope": scope},
        )
    )
    updated = unwrap_result(
        await session.call_tool(
            "update",
            {
                "atom_id": atom_id,
                "statement": "MCP smoke test memory updated",
                "scope": scope,
            },
        )
    )
    forgotten = unwrap_result(
        await session.call_tool("forget", {"atom_id": atom_id, "scope": scope})
    )
    after_forget = unwrap_result(
        await session.call_tool(
            "recall",
            {"query": "MCP smoke test", "scope": scope, "top_k": 5},
        )
    )

    return {
        "tool_names": tool_names,
        "scope": scope,
        "remembered": remembered,
        "recalled": recalled,
        "candidates": candidates,
        "fetched": fetched,
        "drilled_down": drilled_down,
        "updated": updated,
        "forgotten": forgotten,
        "after_forget": after_forget,
    }


async def run_smoke_test(command: str | None = None, url: str | None = None) -> None:
    endpoint = normalize_endpoint(url) if url else None
    async with open_session(command=command, url=endpoint) as session:
        result = await exercise_session(session)

    if (
        not result["remembered"]["id"]
        or not result["recalled"]
        or not result["candidates"]
        or not result["fetched"]
    ):
        raise RuntimeError("MCP read operations returned no data")
    if result["updated"]["statement"] != "MCP smoke test memory updated":
        raise RuntimeError("MCP update did not return the updated statement")
    if result["forgotten"] is not True or result["after_forget"]:
        raise RuntimeError("MCP forget did not hide the memory from recall")

    transport = f"streamable HTTP ({endpoint})" if endpoint else "stdio"
    print(f"MCP {transport} handshake: ok")
    print("tools:", ", ".join(sorted(result["tool_names"])))
    print("tool descriptions: ok")
    print("scope:", result["scope"])
    print("remember id:", result["remembered"]["id"])
    print("recall results:", len(result["recalled"]))
    print("candidate results:", len(result["candidates"]))
    print("fetch results:", len(result["fetched"]))
    print("drill-down turns:", len(result["drilled_down"]))
    print("update result: ok")
    print("forget result: ok")
    print("post-forget recall results:", len(result["after_forget"]))


def normalize_endpoint(url: str) -> str:
    endpoint = url.rstrip("/")
    return endpoint if endpoint.endswith("/mcp") else f"{endpoint}/mcp"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    transport_group = parser.add_mutually_exclusive_group()
    transport_group.add_argument(
        "--installed-entrypoint",
        action="store_true",
        help="launch the installed memory-mcp console script in stdio mode",
    )
    transport_group.add_argument(
        "--url",
        default=None,
        help=f"connect to a Streamable HTTP server (default path: {DEFAULT_REMOTE_URL})",
    )
    args = parser.parse_args()
    command = shutil.which("memory-mcp") if args.installed_entrypoint else None
    if args.installed_entrypoint and command is None:
        raise RuntimeError("memory-mcp is not available on PATH")
    asyncio.run(run_smoke_test(command=command, url=args.url))


if __name__ == "__main__":
    main()