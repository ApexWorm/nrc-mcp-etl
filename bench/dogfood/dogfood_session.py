"""Exercise the actual MCP wire API and retain tool receipts and returned images.

Run a scenario with ``mise run bench:dogfood -- <scenario>``.
Every run gets a fresh output folder; existing maps and previous evidence are preserved.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[2]


class ToolCallError(RuntimeError):
    pass


class DogfoodSession:
    def __init__(self, name: str):
        self.name = name
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        self.root = REPO / "out" / "dogfood" / name / stamp
        self.root.mkdir(parents=True)
        self.stack = AsyncExitStack()
        self.number = 0

    async def __aenter__(self):
        await self.stack.__aenter__()
        try:
            env = {
                **os.environ,
                "NRC_PROFILE": "etl",
                "NRC_ROOT": str(REPO),
                "PYTHONPATH": str(REPO / "python/src"),
            }
            env.pop("NRC_MCP_GROUPS", None)
            env.pop("NRC_MCP_READ_ONLY", None)
            self.stderr = self.stack.enter_context((self.root / "server.log").open("w"))
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "nrc_mcp"], env=env, cwd=str(REPO)
            )
            read, write = await self.stack.enter_async_context(
                stdio_client(params, errlog=self.stderr)
            )
            self.client = await self.stack.enter_async_context(ClientSession(read, write))
            init = await self.client.initialize()
            self.artifact("initialize", init.model_dump(mode="json", by_alias=True))
            self.artifact(
                "tools", (await self.client.list_tools()).model_dump(mode="json", by_alias=True)
            )
            print(f"[{self.name}] artifacts: {self.root}", flush=True)
        except BaseException:
            await self.stack.aclose()
            raise
        return self

    async def __aexit__(self, *exc):
        return await self.stack.__aexit__(*exc)

    def artifact(self, name: str, data) -> str:
        path = self.root / (name if name.endswith(".json") else name + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, default=str) + "\n")
        return str(path)

    async def call(self, tool_name: str, **kwargs):
        self.number += 1
        number = self.number
        start = time.monotonic()
        result = await self.client.call_tool(tool_name, kwargs)
        wire = result.model_dump(mode="json", by_alias=True)
        pictures = []
        texts = []
        for block in wire.get("content", []):
            if block.get("type") == "image":
                suffix = "png" if block.get("mimeType") == "image/png" else "img"
                path = self.root / f"{number:03d}-{tool_name}-{len(pictures)}.{suffix}"
                path.write_bytes(base64.b64decode(block.pop("data")))
                block["saved_to"] = str(path)
                pictures.append(str(path))
            elif block.get("type") == "text":
                texts.append(block["text"])
        entry = {
            "step": number,
            "tool": tool_name,
            "arguments": kwargs,
            "seconds": round(time.monotonic() - start, 3),
            "response": wire,
        }
        self.artifact(f"calls/{number:03d}-{tool_name}", entry)
        with (self.root / "transcript.jsonl").open("a") as f:
            f.write(json.dumps(entry) + "\n")
        print(
            f"[{self.name}] {number:03d} {tool_name} {entry['seconds']}s "
            f"{'ERROR' if wire.get('isError') else 'ok'}",
            flush=True,
        )
        if wire.get("isError"):
            raise ToolCallError(f"{tool_name}: {' '.join(texts)}")
        if pictures:
            return {"images": pictures, "notes": texts}
        structured = wire.get("structuredContent")
        if structured is not None:
            return structured
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except json.JSONDecodeError:
                return {"text": texts[0]}
        return {"text": "\n".join(texts)}
