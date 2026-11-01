"""The server object, the session, and the preconditions every tool shares.

Separated from `server.py` so the tool modules in `surface/` can import what they need
without importing the module that imports *them*. `server.py` binds the surface together;
this is what the surface binds to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer as _Server
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import profiles

INSTRUCTIONS = """\
Authoring, analysing and shipping levels for a Quake-3-engine game.

**One open map.** Almost everything here operates on a session map opened with `map_open`,
rather than taking a path each time. Open a map first; tools that need one say so.

**Nothing writes without being asked.** `map_save` is the only tool that touches a `.map`,
and it refuses if the kernel could not reproduce the file it loaded, or if the write would
destroy playable space. Tools annotated read-only change nothing at all.

**Confidence is enforced, not advertised.** Findings carry a `confidence`, and a rule the
profile marks unverified is downgraded so it can never fail a build. Three of the four
spawn rules the design document called verified turned out to be wrong, and one would have
failed correct maps — read `nrc://corrections` before trusting a rule.

**Ask for what is not built and you get a refusal, not a guess.** Patches are read and
rendered but never authored; there is no measured dimension corpus; cover analysis and
traversal times do not exist. `nrc://conventions` lists the rest.
"""


mcp = _Server("nrc-mcp", instructions=INSTRUCTIONS)


def tool(
    group: str,
    title: str,
    *,
    writes: bool = False,
    destructive: bool = False,
    idempotent: bool | None = None,
    open_world: bool = False,
):
    """Register a tool, with the group and safety hints a client needs to decide anything.

    A thin adapter over `mcp.tool()`, not a registry: the SDK's `ToolManager` already stores
    every tool, and duplicating that here would mean keeping a parallel copy in step with it
    by hand. All this does is make the arguments impossible to forget.

    And forgetting is the failure that matters. `readOnlyHint` is what a host reads when it
    decides whether to run something without asking, so a tool that writes and does not say
    so gets auto-approved. There is no default for it here: every tool states `writes`
    explicitly, or the annotation would be a guess with a safety consequence.

    Args:
        group: which part of the surface this belongs to. Rides in `_meta` and is what
            `NRC_MCP_GROUPS` filters on.
        title: a short human-readable name, shown by clients in place of the function name.
        writes: the tool changes something — the open map, the sidecar, a file, a build
            artefact. `readOnlyHint` is its inverse.
        destructive: the change can *lose* something that was there. Narrower than `writes`:
            adding a brush is a write, overwriting a `.map` is destructive.
        idempotent: calling twice with the same arguments is the same as calling once.
            Defaults to true for readers and false for writers, which is right far more
            often than not; pass it explicitly where that is wrong.
        open_world: the tool reaches outside this machine.
    """
    annotations = ToolAnnotations(
        title=title,
        readOnlyHint=not writes,
        destructiveHint=destructive,
        idempotentHint=(not writes) if idempotent is None else idempotent,
        openWorldHint=open_world,
    )
    return mcp.tool(title=title, annotations=annotations, meta={"group": group})


@dataclass
class Session:
    """The one open map, plus what we know about it."""

    map: Any = None
    path: Path | None = None
    grid: int = 8
    warnings: list[str] = field(default_factory=list)
    #: The round-trip result taken at `map_open`, before anything could edit the map.
    #:
    #: This, not the live one, is the gate. Once a tool has added a brush the live check
    #: differs by construction — that is the edit — and reading it as a kernel failure made
    #: `map_save` refuse every real change and taught callers to pass the override every
    #: time, which switched the guard off for the case it exists to catch.
    opened_round_trip: dict | None = None

    def require(self):
        if self.map is None:
            raise ToolError(
                "no map is open — call map_open(path) first. "
                "Use task_list() to see what else the project can do."
            )
        return self.map


SESSION = Session()


def active_profile() -> str:
    return os.environ.get("NRC_PROFILE", "")


def _require_path() -> Path:
    """The open map's path on disk, for tools that read the file rather than the parse."""
    if SESSION.path is None:
        raise ToolError("no map is open — call map_open(path) first.")
    return SESSION.path


def _profile_error_text(exc: Exception) -> str:
    """A profile failure, with the list of profiles that would have worked.

    `ToolError` carries a string and nothing else, so anything the caller needs in order to
    recover has to be *in* that string. These sites used to return
    `{"error": ..., "available": [...]}`; dropping the second key while converting them
    would have quietly turned an actionable error into a dead end.
    """
    available = ", ".join(profiles.available()) or "none on disk"
    return f"{exc}. Profiles available: {available}"


def _require_profile(profile_id: str | None) -> str:
    """The profile id to use, or an error naming what there is to choose from.

    Guessing here is not an option: the movement constants live in the profile, and the
    design document's own standing height was wrong for this game by 13 units (§7.4,
    `nrc://corrections`). A tool with no profile has no numbers, not default numbers.
    """
    pid = profile_id or active_profile()
    if not pid:
        available = ", ".join(profiles.available()) or "none on disk"
        raise ToolError(
            "no profile selected, and the movement constants live there. Set NRC_PROFILE "
            f"or pass profile_id. Available: {available}"
        )
    return pid


def _add_entity(keys: list[tuple[str, str]]) -> int:
    """Append an entity to the open map and return its index — the one path that does.

    The map is edited in place, so it keeps the path `map_save` writes to and the bytes it
    was opened from, which is what lets `map_save` see the edit and check playable space.
    """
    m = SESSION.require()
    try:
        return m.add_entity(keys)
    except ValueError as e:
        raise ToolError(str(e)) from e


def _kernel():
    from .kernel import kernel

    return kernel()
