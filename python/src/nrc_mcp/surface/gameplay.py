"""How the map plays, measured over a voxel approximation of the walkable space (§7.3).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import math
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import analysis as anamod
from .. import playspace as playspacemod
from ..runtime import (
    SESSION,
    _require_path,
    _require_profile,
    tool,
)


@tool("gameplay", "Walkable grid statistics")
def navgrid_stats(cell: float = 16, profile_id: str | None = None) -> dict:
    """Build the walkable grid and report its size and coverage.

    A cell is walkable when it is empty, the cell below is solid, and there is at least the
    profile's standing height of clear space above it — so the grid is the space a player could
    actually occupy, not merely the empty space.

    This is a **voxel approximation, not compiled AAS**. It is good enough for distances and
    reachability and will disagree with the engine at the margins. Everything else here is built on
    it, so run this first to see whether the map is tractable at the cell size you want.

    Read `reachable_share` before the rest: below 1.0, some standable floor cannot be reached
    from the largest region, and every distance the other reports derive is measured across a
    space the player cannot actually cross.
    """
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        g = anamod.cached_navgrid(map_path, cell=cell, profile_id=pid)
        return anamod.navgrid_summary(g)
    except (OSError, ValueError, MemoryError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Team balance report")
def balance_report(cell: float = 16, profile_id: str | None = None) -> dict:
    """Per-team traversal distance from each spawn group to each objective.

    Reports path lengths over the walkable grid, the asymmetry between teams, and whether the map
    is mirror-symmetric about its centre. Which classnames count as spawns and objectives comes
    from the profile, so this works for any game with a profile.

    Distances are walked, not straight-line — a 500-unit gap with a wall in it is not 500 units.
    """
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        return anamod.balance_report(map_path, pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Sightline report")
def sightline_report(samples: int = 200, cell: float = 16, profile_id: str | None = None) -> dict:
    """Sightline length distribution and power positions.

    Samples walkable positions, casts rays between them at the profile's eye height, and reports how
    far a player can see. Long uncontested lanes are the finding that matters in a sniper-sensitive
    game; "power positions" are the points that see the most of the map.

    Sampled, so the numbers move a little between runs. Read the distribution, not any single value.
    """
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        return anamod.sightline_report(map_path, samples=samples, profile_id=pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Movement clearance check")
def movement_check(cell: float = 16, profile_id: str | None = None) -> dict:
    """Check clearances against the profile's movement constants.

    Standing headroom, crouch headroom, step height, doorway widths. Every finding names the
    constant it used and that constant's confidence, and **anything derived from an unverified
    constant is reported as info, never an error**.

    That clamp is not ceremony. The design document's own standing height was wrong by 13 units, and
    a corridor sized from it would pass every geometric check while being unusable.
    """
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        return anamod.movement_check(map_path, pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Spawn safety report")
def spawn_safety(cell: float = 16, profile_id: str | None = None) -> dict:
    """Exits per spawn and distance to the nearest enemy spawn.

    A spawn with one exit is a spawn that can be held; a spawn close to an enemy spawn is a spawn
    that gets contested immediately. Both are design smells worth seeing before playtesting finds
    them.
    """
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        return anamod.spawn_safety(map_path, pid, cell=cell)
    except (OSError, ValueError, MemoryError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Lighting report")
def lighting_report(reach: float = 512, cell: float = 32, profile_id: str | None = None) -> dict:
    """What lights the map, and where players stand far from any light.

    Read the way q3map2 reads it: an entity whose classname starts with `light` is a light, at
    intensity 300 unless its `light`/`_light` key says otherwise (light.cpp:330, :439-442);
    the worldspawn's `_ambient` (or `ambient`) lights everything evenly, and `_minlight` sets a
    floor (light.cpp:1946, :1959). A map with none of these compiles black. Sky shaders that
    emit light are not read here.

    `reach` is a heuristic, not q3map2's falloff: walkable areas farther than that from every
    light are listed as possibly dim. With ambient or minimum light set they are lit anyway.
    """
    map_path = _require_path()
    pid = _require_profile(profile_id)
    m = SESSION.require()
    ents = m.entities()
    world = {k.lower(): v for k, v in ents[0]["keys"]} if ents else {}
    lights = []
    for e in ents:
        if not str(e["classname"]).lower().startswith("light") or not e.get("origin"):
            continue
        k = {a.lower(): b for a, b in e["keys"]}
        try:
            value = float(k.get("_light") or k.get("light") or 300)
        except ValueError:
            value = 300.0
        lights.append({"entity": e["index"], "origin": list(e["origin"]), "intensity": value})

    def number(*keys: str) -> float:
        for key in keys:
            try:
                return float(world[key])
            except (KeyError, ValueError):
                continue
        return 0.0

    ambient, minlight = number("_ambient", "ambient"), number("_minlight")
    findings = []
    if not lights and ambient <= 0 and minlight <= 0:
        findings.append(
            {
                "code": "LIGHT_NONE",
                "severity": "warning",
                "confidence": "verified",
                "message": "no light entity, no _ambient and no _minlight: q3map2 -light will leave "
                "the map black unless a sky shader emits light",
                "rule_source": "q3map2 light.cpp:330, :439-442, :1946, :1959",
                "fix_hint": "place light entities (entity_place classname light), or set _ambient on the worldspawn",
            }
        )
    dim: list[list[float]] = []
    if lights and ambient <= 0 and minlight <= 0:
        try:
            grid = anamod.cached_navgrid(map_path, cell=cell, profile_id=pid)
        except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
            raise ToolError(str(e)) from e
        points = [tuple(lt["origin"]) for lt in lights]
        for c in grid.walkable_cells():
            p = grid.centre(c)
            if all(math.dist(p, q) > reach for q in points):
                dim.append([round(v) for v in p])
        if dim:
            findings.append(
                {
                    "code": "LIGHT_DIM_AREA",
                    "severity": "info",
                    "confidence": "unverified",
                    "message": f"{len(dim)} walkable cell(s) of {cell:g} units are farther than "
                    f"{reach:g} from every light (for example {dim[0]}); they may compile dark",
                    "rule_source": "heuristic distance, not q3map2's falloff",
                    "fix_hint": "add a light near them, or set _minlight on the worldspawn",
                }
            )
    return {
        "lights": len(lights),
        "intensity": {
            "min": min((lt["intensity"] for lt in lights), default=None),
            "max": max((lt["intensity"] for lt in lights), default=None),
        },
        "ambient": ambient,
        "minlight": minlight,
        "dim_cells": len(dim),
        "dim_examples": dim[:10],
        "findings": findings,
        "summary": {
            s_: sum(1 for f in findings if f["severity"] == s_)
            for s_ in ("error", "warning", "info")
        },
    }


@tool("gameplay", "Compare playable space")
def playable_space_diff(
    before: str,
    after: str | None = None,
    cell: float = playspacemod.DEFAULT_DIFF_CELL,
    profile_id: str | None = None,
) -> dict:
    """Compare the space a player can stand in, between two versions of a map.

    This is the check that no amount of validating, compiling or leak-testing performs: all
    of those pass on a map whose streets have been filled with clip. Only a before/after
    comparison sees it.

    Two findings are errors rather than observations, because neither has a legitimate form:

    - `PLAYSPACE_INTERIOR_SEALED` — floor that had a ceiling over it is no longer standable.
      A roof is sky-exposed by definition, so this is the inside of the map being closed off.
    - `PLAYSPACE_CLIP_SURFACE_WALKABLE` — you can now stand on top of a clip volume. Clip
      stops the player, so capping something with clip does not remove the surface, it
      raises it.

    Args:
        before: the earlier map.
        after: the later map. Defaults to the map open in this session, including edits that
            have not been written yet — which is how you check a change before committing it.
        cell: voxel size. Coarser is faster and still comparable; both sides use the same one.
        profile_id: game profile supplying the movement constants and clip shader names.
    """
    later: Any
    if after is None:
        m = SESSION.require()
        if SESSION.path is None:
            raise ToolError(
                "the open map has no path on disk, so there is no earlier version to compare "
                "against. Pass `after` explicitly."
            )
        later = (str(SESSION.path), m)
    else:
        later = after
    try:
        return playspacemod.diff(before, later, cell=cell, profile_id=profile_id)
    except (anamod.AnalysisError, playspacemod.PlayspaceError) as e:
        raise ToolError(str(e)) from e
