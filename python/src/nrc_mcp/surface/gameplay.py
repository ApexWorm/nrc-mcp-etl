"""How the map plays, measured over a voxel approximation of the walkable space (§7.3).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import json
import math
from typing import Any

from mcp.server.mcpserver import Image as _Image
from mcp.server.mcpserver.exceptions import ToolError

from .. import analysis as anamod
from .. import balance as balancemod
from .. import detail as detailmod
from .. import mapscript, profiles
from .. import playspace as playspacemod
from .. import routes as routesmod
from ..detail import Detail
from ..kernel import parse_map
from ..project_state import digest
from ..runtime import (
    SESSION,
    _require_path,
    _require_profile,
    tool,
)


def _session_grid(cell: float, pid: str, stance: str = "stand"):
    """Reuse only a grid built from these exact session bytes and profile inputs."""
    receipt = SESSION.receipt()
    key = (str(SESSION.path), receipt["sha256"], pid, cell, stance, repr(profiles.load(pid)))
    if key not in SESSION.grids:
        SESSION.grids[key] = anamod.cached_navgrid(
            _require_path(),
            cell=cell,
            profile_id=pid,
            game_map=parse_map(SESSION.require().source()),
            stance=stance,
        )
        while len(SESSION.grids) > 4:
            SESSION.grids.pop(next(iter(SESSION.grids)))
    return SESSION.grids[key]


def _receipt(report: dict) -> dict:
    return {**report, "revision": SESSION.receipt()}


def _route_query(
    start,
    end,
    start_entity,
    end_entity,
    stance,
    cell,
    profile_id,
    start_stance,
    end_stance,
    stance_costs,
    transition_cost,
    max_nodes,
    snap_radius,
):
    path = _require_path()
    pid = _require_profile(profile_id)
    source = SESSION.require().source()
    editor_grid = SESSION.grid
    snapshot = parse_map(source)
    revision = SESSION.receipt()
    revision["sha256"] = digest(source.encode("utf-8"))
    revision["dirty"] = source.encode("utf-8") != SESSION.baseline_files.get(path.resolve())
    cell = routesmod.number(cell, "cell", maximum=4096)
    first = routesmod.endpoint(snapshot, pid, start, start_entity, "start")
    last = routesmod.endpoint(snapshot, pid, end, end_entity, "end")
    grids = {}
    for selected in routesmod.stances(pid, stance):
        key = (str(path), revision["sha256"], pid, cell, selected, repr(profiles.load(pid)))
        if key not in SESSION.grids:
            SESSION.grids[key] = anamod.cached_navgrid(
                path,
                cell=cell,
                profile_id=pid,
                game_map=snapshot,
                stance=selected,
            )
            while len(SESSION.grids) > 4:
                SESSION.grids.pop(next(iter(SESSION.grids)))
        grids[selected] = SESSION.grids[key]
    report = routesmod.find_route(
        grids,
        first,
        last,
        start_stance=start_stance,
        end_stance=end_stance,
        stance_costs=stance_costs,
        transition_cost=transition_cost,
        max_nodes=max_nodes,
        snap_radius=snap_radius,
    )
    return {**report, "revision": revision, "editor_grid": editor_grid}, snapshot


@tool("gameplay", "Route with stance changes")
def route_report(
    start: list[float] | None = None,
    end: list[float] | None = None,
    start_entity: int | None = None,
    end_entity: int | None = None,
    stance: str = "mixed",
    cell: float = 16,
    profile_id: str | None = None,
    start_stance: str | None = None,
    end_stance: str | None = None,
    stance_costs: dict[str, float] | None = None,
    transition_cost: float = 32,
    max_nodes: int = 100000,
    snap_radius: float | None = None,
    detail: Detail = "summary",
) -> dict:
    """Find a directed route with a geometric witness and explicit stance changes.

    Give exactly one of `start` (floor XYZ) / `start_entity`, and one of `end` /
    `end_entity`. Spawn origins snap to floor; brush objectives use their approach area.
    Linked objective entities resolve to the same interaction area. Snap displacement is
    reported separately and excluded from route distance.

    `stance` is stand/crouch/prone or `mixed` (all stances described by the profile).
    Mixed routes start and end standing by default. `start_stance`/`end_stance` may select
    a stance or `any`. Stance changes occur only where both body boxes fit on the same cell.

    Minimized cost is distance times a stance weight plus `transition_cost` per change.
    Defaults are stand=1, crouch=1.5, prone=2 and transition=32 weighted world units.
    These are explicit planning preferences, NOT speeds or seconds. Override them with
    `stance_costs`. Path vertices retain every corner and transition; only collinear
    interior vertices are omitted. Step/drop witnesses follow their checked lift/horizontal/
    lower sweep legs, and distance measures those legs. Width and floor separation are checked.

    Uses the current unsaved map. This is voxel planning: no jumping, animation, prone
    legs/turning, patch collision or script-state simulation. `max_nodes` (1..250000)
    bounds search; exhaustion returns reachable=null rather than claiming no route.
    `route_render` draws the same report over the map geometry.

    `detail="summary"` (default) omits the model caveats, per-grid constants and the
    per-vertex `path` (`segments` holds the same vertices); `"full"` returns everything.
    """
    full = detailmod.wants_full(detail)
    try:
        report, _ = _route_query(
            start,
            end,
            start_entity,
            end_entity,
            stance,
            cell,
            profile_id,
            start_stance,
            end_stance,
            stance_costs,
            transition_cost,
            max_nodes,
            snap_radius,
        )
        return report if full else detailmod.route_report(report)
    except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
        raise ToolError(str(e)) from e


@tool("render", "Render route and stance changes")
def route_render(
    start: list[float] | None = None,
    end: list[float] | None = None,
    start_entity: int | None = None,
    end_entity: int | None = None,
    stance: str = "mixed",
    cell: float = 16,
    profile_id: str | None = None,
    start_stance: str | None = None,
    end_stance: str | None = None,
    stance_costs: dict[str, float] | None = None,
    transition_cost: float = 32,
    max_nodes: int = 100000,
    snap_radius: float | None = None,
    width: int = 1000,
    height: int = 800,
    detail: Detail = "summary",
) -> list:
    """Draw a route over a top-down geometry view, coloured by stance with change markers.

    Parameters and planning limitations match `route_report`. The PNG and JSON witness
    use the same immutable current-map snapshot and revision. All floors project onto XY:
    use the witness's Z coordinates to distinguish overlapping levels. Width/height must
    be integers from 256 to 2048. An unsuccessful search still returns map geometry and
    the reason; it never draws an invented connection. `detail` as in `route_report`.
    """
    full = detailmod.wants_full(detail)
    try:
        for name, value in (("width", width), ("height", height)):
            if isinstance(value, bool) or not isinstance(value, int) or not 256 <= value <= 2048:
                raise ValueError(f"{name} must be an integer from 256 to 2048")
        report, snapshot = _route_query(
            start,
            end,
            start_entity,
            end_entity,
            stance,
            cell,
            profile_id,
            start_stance,
            end_stance,
            stance_costs,
            transition_cost,
            max_nodes,
            snap_radius,
        )
        png, annotations = snapshot.render(
            view="top",
            overlay="shaded",
            width=width,
            height=height,
            grid=report["editor_grid"],
            grid_spacing=64.0,
            wireframe=True,
            padding=64,
            route_segments=report["segments"],
            route_transitions=report["transitions"],
        )
        witness = report if full else detailmod.route_report(report)
        return [_Image(data=png, format="png"), json.dumps({**witness, "render": annotations})]
    except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Walkable grid statistics")
def navgrid_stats(cell: float = 16, profile_id: str | None = None, stance: str = "stand") -> dict:
    """Build the walkable grid and report its size and coverage.

    A cell is walkable when it is empty, the cell below is solid, and there is at least the
    profile's standing height of clear space above it — so the grid is the space a player could
    actually occupy, not merely the empty space.

    This is a **voxel approximation, not compiled AAS**. It is good enough for distances and
    reachability and will disagree with the engine at the margins. Everything else here is built on
    it, so run this first to see whether the map is tractable at the cell size you want.

    `stance` selects stand, crouch or prone; player-width clearance is included. Prone
    checks use the profile body box only, not a full turning/legs simulation.

    Read `reachable_share` before the rest: below 1.0, some standable floor cannot be reached
    from the largest region, and every distance the other reports derive is measured across a
    space the player cannot actually cross.
    """
    _require_path()
    pid = _require_profile(profile_id)
    try:
        g = _session_grid(cell, pid, stance)
        return _receipt(anamod.navgrid_summary(g))
    except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Team balance report")
def balance_report(
    cell: float = 16,
    profile_id: str | None = None,
    stance: str = "stand",
    stages: list[dict] | None = None,
    team_sizes: dict[str, int] | None = None,
    run_speed: float | None = None,
    respawn_seconds: dict[str, float] | None = None,
    detail: Detail = "summary",
) -> dict:
    """Per-team traversal distance from each spawn group to each objective.

    Reports path lengths over the walkable grid, the asymmetry between teams, and whether the map
    is mirror-symmetric about its centre. Which classnames count as spawns and objectives comes
    from the profile, so this works for any game with a profile.

    Distances are walked, not straight-line — a 500-unit gap with a wall in it is not 500 units.
    Uses the current unsaved map and staged script. `stance` selects stand/crouch/prone.

    Profiles with reinforcement rules also report wave waits, estimated route times, active
    spawn capacity and overlapping spawn boxes. `team_sizes` and `respawn_seconds` use the
    profile's team IDs (red/blue in the shipped objective profile). `run_speed` overrides
    units/second explicitly; crouch/prone times require it. No estimate models combat.

    Optional `stages`: [{"name": "after gate", "spawn_entities": [3, 4],
    "objective_entities": [8], "respawn_seconds": {"red": 20, "blue": 30}}]. These are
    explicit spawn/objective selections on the SAME geometry snapshot, not script simulation.

    `detail="summary"` (default): verdicts, counts, every error finding, and the rows that
    need attention first; `omitted` names what was left out. `"full"` returns everything.
    """
    full = detailmod.wants_full(detail)
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        grid = _session_grid(cell, pid, stance)
        report = anamod.balance_report(map_path, pid, cell=cell, grid=grid)
        spec = profiles.load(pid).get("mapscript")
        data = SESSION.read_file(mapscript.script_path(spec, SESSION.path)) if spec else None
        report = balancemod.enrich(
            report,
            grid,
            script_text=data.decode("utf-8", errors="replace") if data is not None else None,
            stages=stages,
            team_sizes=team_sizes,
            run_speed=run_speed,
            respawn_seconds=respawn_seconds,
        )
        if not full:
            report = detailmod.balance_report(report, frozenset(balancemod.MODEL_NOTES))
        return _receipt(report)
    except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Sightline report")
def sightline_report(
    samples: int = 200,
    cell: float = 16,
    profile_id: str | None = None,
    detail: Detail = "summary",
) -> dict:
    """Sightline length distribution and power positions.

    Samples walkable positions, casts rays between them at the profile's eye height, and reports how
    far a player can see. Long uncontested lanes are the finding that matters in a sniper-sensitive
    game; "power positions" are the points that see the most of the map.

    Sampled, so the numbers move a little between runs. Read the distribution, not any single value.
    `detail` as in `balance_report`.
    """
    full = detailmod.wants_full(detail)
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        report = anamod.sightline_report(
            map_path, samples=samples, profile_id=pid, cell=cell, grid=_session_grid(cell, pid)
        )
        return _receipt(report if full else detailmod.sightline_report(report))
    except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Movement clearance check")
def movement_check(
    cell: float = 16, profile_id: str | None = None, detail: Detail = "summary"
) -> dict:
    """Check clearances against the profile's movement constants.

    Standing headroom, crouch headroom, step height, doorway widths. Every finding names the
    constant it used and that constant's confidence, and **anything derived from an unverified
    constant is reported as info, never an error**.

    `detail` as in `balance_report`.
    """
    full = detailmod.wants_full(detail)
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        report = anamod.movement_check(map_path, pid, cell=cell, grid=_session_grid(cell, pid))
        return _receipt(report if full else detailmod.movement_check(report))
    except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
        raise ToolError(str(e)) from e


@tool("gameplay", "Spawn safety report")
def spawn_safety(
    cell: float = 16, profile_id: str | None = None, detail: Detail = "summary"
) -> dict:
    """Exits per spawn and distance to the nearest enemy spawn.

    A spawn with one exit is a spawn that can be held; a spawn close to an enemy spawn is a spawn
    that gets contested immediately. Both are design smells worth seeing before playtesting finds
    them. `detail="summary"` (default) lists the riskiest spawns first; `"full"` lists all.
    """
    full = detailmod.wants_full(detail)
    map_path = _require_path()
    pid = _require_profile(profile_id)
    try:
        report = anamod.spawn_safety(map_path, pid, cell=cell, grid=_session_grid(cell, pid))
        return _receipt(report if full else detailmod.spawn_safety(report))
    except (OSError, ValueError, MemoryError, anamod.AnalysisError) as e:
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
    _require_path()
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
            grid = _session_grid(cell, pid)
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
