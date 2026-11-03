"""Packaging a compiled map and checking it is fit to release (§6.4, §7.1).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import analysis, botgoals, botnav, mapscript, profiles, routes
from .. import detail as detailmod
from .. import pack as packmod
from ..detail import Detail
from ..kernel import parse_map, repo_root
from ..runtime import (
    SESSION,
    _require_profile,
    active_profile,
    tool,
)


def _absolute(path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    if not p.is_file():
        raise ToolError(f"{p} does not exist")
    return p


@tool("shipping", "Build a pk3 archive", writes=True)
def pack_pk3(
    bsp: str,
    complevel: int | None = None,
    png: bool = False,
    ignore: list[str] | None = None,
    depends: list[str] | None = None,
    detail: Detail = "summary",
) -> dict:
    """Build a release archive for a compiled BSP with `q3map2 -pk3` (§6.4).

    The packer's own naming is the first oracle: a `_FAILEDpack.pk3` means it could not find
    a resource the BSP references. Each name it missed is then checked again, because the
    packer cannot read shaders defined with `implicitMap` and reports them missing although the
    base game ships them. A name counts as found when the base game supplies it, the map's own
    shader script defines it (script and images are packed), a file of that name sits beside
    the map, the profile lists it under `packaging.allowed_missing` (stock assets the base game
    never shipped), or it matches `ignore`: exact names or shell patterns such as
    `sound/world/*`, for resources you know the game does without. If all are found the
    package is `_autopacked.pk3`; whatever is left is in `not_found` and the pack still fails.
    `packer_not_found` is the packer's own list, `accounted_for` and `allowed_missing` say what
    became of each.

    The archive is then fixed up from the active profile: companion files and the arena that
    sit beside the map are added, with the sound files its speaker and sound scripts name,
    files the base game already ships are dropped, and it is written next to the folder that
    holds `maps/`, never into the game install.

    `depends` lists other archives the map is played alongside, whose textures, models and
    sounds it uses (a map built on another map's assets): what they hold counts as found and
    is not copied in. Load them with the package, e.g. `engine_playtest(depends=...)`. For the
    compiler to see them, keep them beside the folder that holds `maps/`.

    `detail="summary"` (default) keeps `not_found` and `allowed_missing` whole and caps the
    other lists; `"full"` adds the packer's console tail and every row.
    """
    full = detailmod.wants_full(detail)
    try:
        report = packmod.pack_pk3(
            bsp,
            complevel=complevel,
            png=png,
            profile_id=active_profile() or None,
            ignore=list(ignore or []),
            depends=[_absolute(d) for d in depends or []],
        )
    except packmod.PackError as e:
        raise ToolError(str(e)) from e
    return report if full else detailmod.pack_pk3(report)


@tool("shipping", "List BSP resources")
def repack_analyze(bsp: str) -> dict:
    """List every resource a compiled BSP references, via `q3map2 -repack -analyze`.

    Parsed from the compiler's own dump rather than traced independently — the compiler's view
    of what a BSP needs is the one that decides whether the map works.
    """
    try:
        return packmod.repack_analyze(bsp, active_profile() or None)
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


# ---------------------------------------------------------------------------
# Release files: everything a game looks a map up by besides the BSP and its script
# ---------------------------------------------------------------------------


def _release() -> tuple[str, dict, Path, str]:
    """The active profile's packaging section, the folder holding maps/, and the map's name."""
    SESSION.require()
    if SESSION.path is None:
        raise ToolError("the open map has no path on disk; save it first")
    pid = active_profile()
    if not pid:
        raise ToolError("no profile selected; set NRC_PROFILE")
    pkg = profiles.load(pid).get("packaging")
    if not isinstance(pkg, dict):
        raise ToolError(f"profile {pid} states no packaging conventions")
    return pid, pkg, packmod._map_root(SESSION.path), SESSION.path.stem


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, str):
        tmp.write_text(data, encoding="utf-8", newline="\n")
    else:
        tmp.write_bytes(data)
    tmp.replace(path)


def _quoted(text: str, what: str) -> str:
    text = str(text)
    if '"' in text or "\n" in text or "\r" in text:
        raise ToolError(
            f"{what} is written in quotes on one line: no double quotes, no line breaks"
        )
    return text


@tool("shipping", "Write the arena file", writes=True, destructive=True, idempotent=True)
def arena_write(longname: str, keys: dict | None = None) -> dict:
    """Write the map's arena file, which puts it in the game's map list with a name and briefing.

    `longname` is the name players see. `keys` adds any other key the profile lists (for
    ET: Legacy: `briefing`, `timelimit`, `axisRespawnTime`, `alliedRespawnTime`,
    `mapposition_x`/`_y`, win texts, `type`); the map name and the profile's defaults are filled
    in. Written beside the map's folder, where `pack_pk3` picks it up. Replaces an existing one.
    """
    _pid, pkg, root, stem = _release()
    arena = pkg.get("arena")
    if not isinstance(arena, dict):
        raise ToolError("the profile describes no arena file")
    allowed = [str(k) for k in arena.get("keys") or []]
    values: dict[str, Any] = {
        **(arena.get("defaults") or {}),
        **(keys or {}),
        "longname": longname,
        "map": stem,
    }
    unknown = [k for k in values if allowed and k not in allowed]
    if unknown:
        raise ToolError(
            f"unknown arena key(s) {', '.join(unknown)}; the profile lists: {', '.join(allowed)}"
        )
    order = [k for k in allowed if k in values] or list(values)
    width = max(len(k) for k in order)
    body = "".join(f'\t{k.ljust(width)}\t"{_quoted(values[k], k)}"\n' for k in order)
    path = (
        root / str(arena.get("directory", "scripts")) / f"{stem}.{arena.get('extension', 'arena')}"
    )
    _write(path, "{\n" + body + "}\n")
    missing = [k for k in arena.get("required_keys") or [] if k not in values]
    return {"path": str(path), "keys": {k: values[k] for k in order}, "missing_required": missing}


@tool("shipping", "Write objective descriptions", writes=True, destructive=True, idempotent=True)
def objdata_write(descriptions: dict, objectives: dict | None = None) -> dict:
    """Write the descriptions players read in the limbo menu: the map's, and each objective's.

    `descriptions` maps a team (for ET: Legacy `axis`, `allied`, `neutral`) to its text;
    `objectives` maps a team to a list of texts, numbered in order from 1, matching the
    objective numbers the map script uses. Replaces an existing file.
    """
    _pid, pkg, root, stem = _release()
    spec = pkg.get("objdata")
    if not isinstance(spec, dict):
        raise ToolError("the profile describes no objective description file")
    teams = [str(t) for t in spec.get("teams") or []]
    oteams = [str(t) for t in spec.get("objective_teams") or []]
    lines = []
    for team, text in descriptions.items():
        if team not in teams:
            raise ToolError(f"descriptions: team must be one of {', '.join(teams)}, got {team!r}")
        lines.append(
            spec["description"].format(team=team, text=_quoted(text, f"{team} description"))
        )
    for team, texts in (objectives or {}).items():
        if team not in oteams:
            raise ToolError(f"objectives: team must be one of {', '.join(oteams)}, got {team!r}")
        if len(texts) > int(spec.get("max_objectives", 99)):
            raise ToolError(f"at most {spec['max_objectives']} objectives per team")
        for n, text in enumerate(texts, 1):
            lines.append(
                spec["objective"].format(
                    team=team, number=n, text=_quoted(text, f"{team} objective {n}")
                )
            )
    path = root / spec["path"].format(map=stem)
    _write(path, "\n".join(lines) + "\n")
    return {"path": str(path), "lines": len(lines)}


def _cut_height(pid: str, m, lo_z: float, hi_z: float) -> float:
    """Where to cut the roof off: above the highest spawn by a standing player's height."""
    spawns = analysis.roles(pid).spawns
    zs = [e["origin"][2] for e in m.entities() if e["classname"] in spawns and e.get("origin")]
    head = profiles.standing_height(pid) or 64.0
    if zs:
        return max(zs) + head + 32.0
    return (lo_z + hi_z) / 2


@tool("shipping", "Make the command map", writes=True, destructive=True, idempotent=True)
def commandmap_make(margin: int = 128, cut_height: float | None = None) -> dict:
    """Draw the command map and tell the game where it lies.

    Sets the worldspawn's map coordinates to a square around the map (plus `margin`), draws a
    top-down section of the map into exactly that square - roofs cut away above `cut_height`,
    walls in a dark section colour - and writes the image and the shaders the game draws it
    with, beside the map's folder. Default `cut_height`: a standing player's height above the
    highest spawn. Save the map afterwards: the coordinates are worldspawn keys.
    """
    pid, pkg, root, stem = _release()
    spec = pkg.get("commandmap")
    if not isinstance(spec, dict):
        raise ToolError("the profile describes no command map")
    m = SESSION.map
    bounds = m.bounds()
    if bounds is None:
        raise ToolError("the map has no geometry to draw")
    (x0, y0, z0), (x1, y1, z1) = bounds
    side = max(x1 - x0, y1 - y0) + 2 * margin
    side = math.ceil(side / 64) * 64
    cx, cy = round((x0 + x1) / 2), round((y0 + y1) / 2)
    half = side // 2
    mins = (cx - half, cy + half)  # top-left
    maxs = (cx + half, cy - half)  # bottom-right
    m.set_keys(
        0, [(spec["mins_key"], f"{mins[0]} {mins[1]}"), (spec["maxs_key"], f"{maxs[0]} {maxs[1]}")]
    )
    cut = cut_height if cut_height is not None else _cut_height(pid, m, z0, z1)
    size = int(spec.get("size", 512))
    image, notes = m.render(
        view="top",
        width=size,
        height=size,
        grid_spacing=0,
        wireframe=False,
        hide_invisible=True,
        annotate=False,
        frame=[cx - half, cy - half, cx + half, cy + half],
        padding=0,
        tga=True,
        cut_above=float(cut),
    )
    img_path = root / spec["image"].format(map=stem)
    shader_path = root / spec["shader_file"].format(map=stem)
    _write(img_path, image)
    _write(shader_path, spec["shader_text"].format(map=stem))
    return {
        "image": str(img_path),
        "shaders": str(shader_path),
        spec["mins_key"]: list(mins),
        spec["maxs_key"]: list(maxs),
        "cut_height": cut,
        "units_per_pixel": notes.get("units_per_pixel"),
        "next": "map_save: the map coordinates are worldspawn keys; pack_pk3 picks up the files",
    }


@tool("shipping", "Make the levelshot", writes=True, destructive=True, idempotent=True)
def levelshot_make(position: list[float], yaw_deg: float = 0.0) -> dict:
    """Render the levelshot - the picture shown while the map loads and in the server browser.

    Taken from a standing player's eyes at the floor `position`, looking along `yaw_deg`
    (0 = +X, 90 = +Y), at the profile's preferred size. Written beside the map's folder.
    """
    pid, pkg, root, stem = _release()
    shot = pkg.get("levelshot")
    if not isinstance(shot, dict):
        raise ToolError("the profile describes no levelshot")
    eye = profiles.standing_height(pid)
    if eye is None:
        raise ToolError("the profile states no standing height to take the shot from")
    w, h = (int(v) for v in shot.get("preferred_size") or (256, 256))
    image, _notes = SESSION.map.render(
        view="player_eye",
        width=w,
        height=h,
        grid_spacing=0,
        hide_invisible=True,
        annotate=False,
        eye=list(position),
        yaw_deg=yaw_deg,
        eye_height=eye,
        tga=True,
    )
    path = root / str(shot.get("directory", "levelshots")) / f"{stem}.tga"
    _write(path, image)
    return {"path": str(path), "size": [w, h]}


@tool("shipping", "Seed bot waypoints", writes=True, destructive=True, idempotent=True)
def bot_waypoints_write(
    cell: float = 16,
    spacing: float | None = None,
    omnibot_path: str | None = None,
    spots: list[dict] | None = None,
    auto_spots: bool = True,
    detail: Detail = "summary",
) -> dict:
    """Seed the bot add-on's waypoint file for the open map from its stance navigation grids,
    with the map goals (camping spots) and the map script that switches them.

    For ET: Legacy this is an Omni-bot `.way` file (format version 9, read and written as
    the add-on's own serializer does). Waypoints sit about `spacing` units apart (default
    from the profile) on the walkable cells of the stand, crouch and prone grids (`cell`,
    as in `route_report`); each is connected to its neighbours only where the straight line
    between them is walkable, since bots steer straight between waypoints. Waypoints that
    need crouch or prone carry that flag (bots press it while heading there). Brushes that
    change state are seeded in every state and flagged for the add-on to test at runtime
    (the profile's `bots:` section says which classes and flags):

    - a door a player opens with the use key is passable, with a waypoint in the doorway
      carrying the door flag (bots press use heading for it) and, for a door `allowteams`
      limits to one team, that team's flags, so the other team never routes through it;
    - a blown wall, a locked or scripted door, and a constructible that blocks a gap once
      built are walls: connections through them carry the wall flag, closed while solid;
    - floor only a built constructible gives (a bridge deck) is no floor at round start:
      connections onto or across it carry the bridge flag, closed while nothing is under
      their midpoint, and are split at the deck's edges so that midpoint is over the deck.

    Team spawns and every objective approach area get a named waypoint; parts of the map no
    spawn connects to are dropped; walking off a ledge is a one-way connection. `doors`,
    `constructibles` and the `bridge_*` / `door_*`
    counts say what was gated.

    Jumps and ladders (the profile's `bots.jumps` / `bots.ladders`): where walking between
    two areas is impossible or a long detour, a ledge whose top is within the jump's rise
    (swept with the standing box up its face) or a gap the box's ballistic arc clears from
    every take-off point and at every speed the profile lists, gets a run-up waypoint, a
    take-off waypoint flagged to jump (for ET: Legacy `JUMP`, small radius) and a ONE-WAY
    connection to a landing waypoint (`JUMPLOW` on a ledge's). A brush whose shader has the
    ladder surfaceparm gets a foot waypoint in front of its face and a top waypoint on the
    floor at its top, both `CLIMB`, connected both ways. `jumps` and `ladders` list them
    (a ladder without a reachable foot or top says why).

    The add-on reads waypoints from its own folder, never from the map's pk3, so the file
    is staged beside the map's folder under the profile's add-on layout (for ET: Legacy
    `omni-bot/et/nav/<map>.way`), to be copied into the server's `omnibot_path`. Pass
    `omnibot_path` to also write it there. Uses the current unsaved map.

    `objectives` says, per objective, whether the graph reaches it from each team's spawn
    waypoints. Objective goals (plant, build, capture) come from the game itself.

    Beside the waypoints go two scripts the add-on runs at map load (ET: Legacy:
    `<map>_goals.gm`, its map goals, and `<map>.gm`, the map script). With `auto_spots`
    (default) spots are chosen from the geometry, each on a standing waypoint its team
    reaches: per objective part a team defends (the thing to blow up, the flag it holds,
    the item and, once stolen, its delivery place), up to two defend spots that see where
    the other team uses it, ranked by how much of that team's shortest routes in they see
    and facing it; per team, up to two snipe spots off the enemy's routes that see the most
    of them at long range. `goals.spots[].why` says what each one sees. `spots` adds your
    own: [{"type": "DEFEND", "team": "axis", "position": [x, y, z], "aim_at": [x, y, z]}],
    with `yaw` (degrees) instead of `aim_at`, and optional `name`, `stance`,
    `camp_time` [min, max] seconds and `objective` (its spots' on/off follows that
    objective's); positions snap to the nearest walkable floor. Types are the profile's
    (DEFEND, ATTACK, CAMP, SNIPE, MOBILEMG42, MOBILEMORTAR; SNIPE is for sniper rifles).

    The map script hooks each objective's events by the trigger names the game sends:
    the game's own "Planted at <track>." / "Defused at <track>." and the announcements in
    the map's script (read from the staged script, so edited texts still match). Each
    hook switches its objective's spots per team: a destroyed objective's defenders stand
    down, a built one is defended, a captured flag changes defenders, a stolen item sends
    them to its delivery place. `goals.triggers` lists them; `goals.not_hooked` names
    events the map's script gives no text for (add a wm_announce there). Both scripts are
    read back before anything is written.
    `bot_playtest` loads all three in the engine with bots and reports what they did.

    `detail="summary"` (default) leaves out the per-pin rows and why each spot was chosen;
    `"full"` returns them.
    """
    full = detailmod.wants_full(detail)
    pid, _pkg, root, stem = _release()
    try:
        bots = botnav.spec(pid)
        cell = routes.number(cell, "cell", maximum=4096)
        if spacing is not None:
            spacing = routes.number(spacing, "spacing", maximum=4096)
        if spots is not None and not isinstance(spots, list):
            raise ToolError("spots must be a list of spot objects")
        game_map = parse_map(SESSION.require().source())
        extras: dict[str, Any] = {}
        waypoint_file, report = botnav.build(
            SESSION.path, game_map, pid, cell=cell, spacing=spacing, extras=extras
        )
        data = botnav.write(waypoint_file, bots["format"])
        if botnav.write(botnav.read(data, bots["format"]), bots["format"]) != data:
            raise ToolError("the written file does not read back identically; nothing written")
        script_spec = profiles.load(pid).get("mapscript")
        script_data = (
            SESSION.read_file(mapscript.script_path(script_spec, SESSION.path))
            if script_spec
            else None
        )
        goals_spec, map_script_spec, _ = botgoals.spec(bots)
        made = botgoals.make(
            game_map,
            pid,
            bots,
            waypoint_file.waypoints,
            extras,
            script_text=script_data.decode("utf-8", errors="replace") if script_data else None,
            given=spots,
            auto=bool(auto_spots),
            header=f"{goals_spec.get('comment', '')} for {stem}".strip(),
        )
    except (
        botnav.BotNavError,
        botgoals.BotGoalsError,
        analysis.AnalysisError,
        ValueError,
        MemoryError,
    ) as e:
        raise ToolError(str(e)) from e
    relative = botnav.install_path(bots, stem)
    files = {
        relative: data,
        Path(str(goals_spec["install_path"]).format(map=stem)): made.goals,
        Path(str(map_script_spec["install_path"]).format(map=stem)): made.script,
    }
    folder = Path(omnibot_path) if omnibot_path else None
    if folder is not None and not folder.is_dir():
        raise ToolError(f"omnibot_path {folder} is not a folder; nothing was written")
    staged = root / str(bots["stage_dir"]) / relative
    for rel, body in files.items():
        _write(root / str(bots["stage_dir"]) / rel, body)
        if folder is not None:
            _write(folder / rel, body)
    installed = folder / relative if folder is not None else None
    warnings = []
    for row in report["objectives"]:
        missing = [k for k, v in row.items() if k.startswith("reached_from_") and not v]
        if missing or not row["waypoints"]:
            warnings.append(
                f"objective {row['name'] or row['entity']} (entity {row['entity']}): "
                + ("no walkable cell near it" if not row["waypoints"] else ", ".join(missing))
                + " is false"
            )
    if report["components"]["count"] > 1:
        warnings.append(
            f"the graph has {report['components']['count']} unconnected parts "
            f"(sizes {report['components']['sizes']}); bots cannot cross between them"
        )
    if report["unplaced_pins"]:
        warnings.append(f"{len(report['unplaced_pins'])} spawn/objective markers found no cell")
    warnings += made.report["warnings"]
    goals = {
        "files": {str(rel): str(root / str(bots["stage_dir"]) / rel) for rel in list(files)[1:]},
        **{k: v for k, v in made.report.items() if k != "warnings"},
    }
    out = {
        "path": str(staged),
        "installed": str(installed) if installed else None,
        "install_as": str(relative),
        "bytes": len(data),
        **report,
        "goals": goals,
        "warnings": warnings,
    }
    if not full:
        pins = out.pop("pins")
        out["pins"] = {
            kind: sum(1 for p in pins if p["kind"] == kind) for kind in ("spawn", "objective")
        }
        for row in goals["spots"]:
            row.pop("why", None)
        out["omitted"] = {
            "pins": 'per-marker cells and waypoints; detail="full" lists them',
            "goals.spots.why": 'what each spot sees and where it stands; detail="full"',
        }
    return out
