"""A golden snapshot of the agent-facing tool surface.

The tool surface is an API. Its names, its signatures and its descriptions are what an
agent reads to decide what to call, so a change to any of them is a change to behaviour
even when no logic moved — and the rest of the suite cannot see it. The scenario tapes
exercise ten of the tools and the unit tests call four; the remaining thirty-eight are
covered by this file and nothing else.

So the snapshot exists to make a surface change **deliberate** rather than incidental.
When one is intended, regenerate it and read the diff:

    NRC_UPDATE_SURFACE=1 uv run pytest python/tests/test_tool_surface.py

The regenerated fixture is part of the change under review. A diff nobody can explain is
the bug this file exists to catch.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "tool_surface.json"


def _surface() -> list[tuple[str, object]]:
    """Every registered tool as `(name, function)`, in listing order.

    One indirection on purpose: the surface is being split out of `server.py`, and this
    resolver is the only line in the test that has to know where it currently lives.
    """
    from nrc_mcp import server

    return [(name, getattr(server, name)) for name in server.TOOL_NAMES]


def _describe() -> dict:
    return {
        name: {
            "signature": str(inspect.signature(fn)),
            "doc": inspect.getdoc(fn) or "",
        }
        for name, fn in _surface()
    }


def test_tool_surface_matches_the_snapshot():
    live = _describe()

    if os.environ.get("NRC_UPDATE_SURFACE") == "1":
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        return

    assert FIXTURE.is_file(), (
        f"{FIXTURE} is missing. Generate it with NRC_UPDATE_SURFACE=1 and commit it."
    )
    recorded = json.loads(FIXTURE.read_text())

    # Report the three kinds of drift separately: which tools appeared or vanished is a
    # different failure from one whose description changed, and a combined diff of 48
    # entries is unreadable.
    added = sorted(set(live) - set(recorded))
    removed = sorted(set(recorded) - set(live))
    assert not added, f"tools not in the snapshot: {added}"
    assert not removed, f"tools missing from the surface: {removed}"

    for name in live:
        assert live[name]["signature"] == recorded[name]["signature"], (
            f"{name}: signature changed\n  was {recorded[name]['signature']}\n"
            f"  now {live[name]['signature']}"
        )
        assert live[name]["doc"] == recorded[name]["doc"], (
            f"{name}: description changed. An agent reads this to choose the tool, so the "
            "change has to be intended — regenerate with NRC_UPDATE_SURFACE=1 if it is."
        )


def test_every_tool_is_documented():
    """A tool with no description is unusable from the far side of the protocol."""
    missing = [name for name, fn in _surface() if not (inspect.getdoc(fn) or "").strip()]
    assert not missing, f"tools with no docstring: {missing}"
