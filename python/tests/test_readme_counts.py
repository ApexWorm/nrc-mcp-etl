"""The README's headline counts must match what the server actually registers."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

from nrc_mcp import server

README = Path(__file__).resolve().parents[2] / "README.md"


def _registered() -> dict[str, int]:
    mcp = server.mcp
    return {
        "tools": len(mcp._tool_manager.list_tools()),
        # Templates (`nrc://profile/{profile_id}`) are resources to a reader of the README.
        "resources": len(asyncio.run(mcp.list_resources()))
        + len(asyncio.run(mcp.list_resource_templates())),
        "prompts": len(asyncio.run(mcp.list_prompts())),
    }


def test_headline_counts_match_the_server():
    text = README.read_text()
    m = re.search(r"\*\*(\d+) tools, (\d+) resources and (\d+) prompts\*\*", text)
    assert m, "the README headline no longer states 'N tools, M resources and P prompts'"
    stated = {"tools": int(m[1]), "resources": int(m[2]), "prompts": int(m[3])}
    assert stated == _registered(), f"README says {stated}; the server registers {_registered()}"


def test_tool_reference_count_matches_the_server():
    m = re.search(r"^(\d+) tools in (\d+) groups\.", README.read_text(), re.M)
    assert m, "the Tool reference no longer opens with 'N tools in G groups.'"
    assert int(m[1]) == _registered()["tools"]
    assert int(m[2]) == len(server.all_groups())
