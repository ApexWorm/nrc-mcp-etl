"""Packaging a compiled map and checking it is fit to release (§6.4, §7.1).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from .. import pack as packmod
from ..runtime import (
    SESSION,
    _require_profile,
    tool,
)


@tool("shipping", "Build a pk3 archive", writes=True)
def pack_pk3(bsp: str, complevel: int | None = None, png: bool = False) -> dict:
    """Build a release archive for a compiled BSP with `q3map2 -pk3` (§6.4).

    The packer's own naming is the oracle: a `_FAILEDpack.pk3` means the BSP references a
    resource that is not on disk, so the map would show missing content on a server even
    though it compiled. That is reported as a failure whatever the exit code.
    """
    try:
        return packmod.pack_pk3(bsp, complevel=complevel, png=png)
    except packmod.PackError as e:
        raise ToolError(str(e)) from e


@tool("shipping", "List BSP resources")
def repack_analyze(bsp: str) -> dict:
    """List every resource a compiled BSP references, via `q3map2 -repack -analyze`.

    Parsed from the compiler's own dump rather than traced independently — the compiler's view
    of what a BSP needs is the one that decides whether the map works.
    """
    try:
        return packmod.repack_analyze(bsp)
    except packmod.PackError as e:
        raise ToolError(str(e)) from e


@tool("shipping", "Release checklist")
def ship_check(
    target: str | None = None, profile_id: str | None = None, pk3: str | None = None
) -> dict:
    """Run the release checklist: naming, levelshot, arena file, package contents.

    Conventions come from the profile's `packaging` section, and unverified ones produce
    `info` rather than failures — the levelshot size and arena key list are community practice
    rather than anything the gamepack documents.
    """
    t = target or (str(SESSION.path) if SESSION.path else None)
    if t is None:
        raise ToolError("no target given and no map is open — pass target, or call map_open.")
    pid = _require_profile(profile_id)
    try:
        return packmod.ship_check(t, pid, pk3=pk3)
    except packmod.PackError as e:
        raise ToolError(str(e)) from e
