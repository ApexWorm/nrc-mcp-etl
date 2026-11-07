"""Opening, reading, validating and writing the `.map` file.

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import contextlib
import itertools
import math
import re
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import analysis as anamod
from .. import gamepath, mapscript, profiles, rules, soundscript
from .. import playspace as playspacemod
from ..kernel import parse_map, repo_root
from ..project_state import write_batch
from ..runtime import (
    SESSION,
    _add_entity,
    _kernel,
    _profile_error_text,
    active_profile,
    tool,
)


@tool("map", "Open map")
def map_open(path: str, grid: int = 8) -> dict:
    """Open a `.map` file for analysis.

    Args:
        path: path to the `.map`. Relative paths resolve against the repo root.
        grid: authoring grid that off-grid geometry is measured against.

    Returns a summary plus a round-trip check. If `round_trip.identical` is false, treat
    every later answer about this map with suspicion and report it — that is the §3.2 gate
    failing on a real file, which is a kernel bug worth fixing before anything else.
    """
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    p = p.resolve()
    loaded = p.read_bytes()
    m = parse_map(loaded.decode("utf-8"))

    SESSION.map = m
    SESSION.path = p
    SESSION.grid = grid
    SESSION.warnings = []
    SESSION.baseline_files = {p: loaded}
    SESSION.pending_files = {}
    SESSION.checkpoints = {}
    SESSION.grids = {}
    SESSION.last_compile = None
    SESSION.read_file(p.with_suffix(".solids.json"))
    pid = active_profile()
    spec = profiles.load(pid).get("mapscript") if pid else None
    if spec:
        SESSION.read_file(mapscript.script_path(spec, p))

    rt = m.round_trip()
    SESSION.opened_round_trip = rt
    SESSION.opened_brushes = frozenset(h for _e, _p, h in m.brush_fingerprints())
    if not rt["identical"]:
        SESSION.warnings.append(
            "this map does not round-trip byte-identically; map_save will refuse to write"
        )

    stats = m.stats(grid=grid)
    return {
        "path": str(p),
        "revision": SESSION.receipt(),
        "round_trip": rt,
        "entities": stats["entities"],
        "brushes": stats["brushes"],
        "patches": stats["patches"],
        "texdef_kinds": stats["texdef_kinds"],
        "bounds": stats["bounds"],
        "grid": grid,
        "warnings": SESSION.warnings,
    }


#: An empty map: the worldspawn every map starts with, and nothing else.
EMPTY_MAP = '{\n"classname" "worldspawn"\n}\n'


@tool("map", "Create a new map", writes=True)
def map_new(path: str, grid: int = 8) -> dict:
    """Create an empty `.map` holding only a worldspawn, and open it.

    The start of a map built from nothing: then `solid_commit` for geometry, `entity_place`
    for point entities, `map_save` to write. An existing file is never overwritten — open it
    with `map_open` instead. Missing parent folders are created.

    Args:
        path: where to create it, ending in `.map`. Relative paths resolve against the repo root.
        grid: authoring grid, as for `map_open`.
    """
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    if p.suffix.lower() != ".map":
        raise ToolError(f"{p} does not end in .map")
    if p.exists():
        raise ToolError(f"{p} already exists; map_new never overwrites — open it with map_open")
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Exclusive creation, so a file that appears between the check and the write is not
        # overwritten either.
        with p.open("x", encoding="utf-8", newline="\n") as f:
            f.write(EMPTY_MAP)
    except FileExistsError as e:
        raise ToolError(f"{p} already exists; map_new never overwrites") from e
    result = map_open(str(p), grid=grid)
    result["created"] = True
    result["next"] = "solid_commit for geometry, entity_place for point entities, then map_save"
    return result


@tool("map", "Map statistics")
def map_stats(grid: int | None = None) -> dict:
    """Statistics for the open map: counts, bounds, shader histogram, grid alignment.

    `structural_brushes` vs `detail_brushes` is the split that matters most for vis
    performance (§6.1): structural brushes block visibility and are expensive, detail
    brushes do not.
    """
    m = SESSION.require()
    return m.stats(grid=grid if grid is not None else SESSION.grid)


@tool("map", "Save map", writes=True, destructive=True)
def map_save(
    path: str | None = None,
    allow_non_identical: bool = False,
    check_playable_space: bool = True,
    acknowledge_regression: bool = False,
) -> dict:
    """Write the open map back to disk, if it is safe to.

    Staged script and generated-solid sidecars are saved with the map. Three gates apply.

    **The file.** If the kernel could not reproduce the bytes it *loaded*, it misunderstands
    something in this file and writing would silently discard it. That check is taken at
    `map_open` and it is the one enforced here. A difference introduced since then is your
    edit, not a kernel failure, and does not block the write.

    **The map.** The write is compared against the file it is about to replace, and refused
    if it destroys playable space — an interior floor sealed off, or a surface you can now
    stand on top of a clip volume. Both are silent in every other check: the map still
    compiles, still seals, still validates, and is unplayable. See `playable_space_diff` for
    the same report on its own.

    **Other edits.** Refuse if a destination changed on disk since this session read it.
    All writes are prepared before replacing files; a write failure rolls replacements back.
    A successful save clears checkpoints. Individual replacements are atomic, but this is
    not a filesystem-wide transaction across multiple files.

    The comparison costs a second or two on a city-sized map. If it cannot be made at all —
    a map too large to voxelize, or one with no evaluable brush — the write proceeds and
    says so, because a check that cannot run has not found anything.

    Args:
        path: destination. Defaults to the open map. Save As requires a new destination,
            copies known companions and makes the new path current.
        allow_non_identical: write even though the map did not round-trip when it was opened.
        check_playable_space: run the before/after comparison. Off skips it entirely.
        acknowledge_regression: write anyway despite a playable-space error. Use it when you
            meant to remove that space.
    """
    m = SESSION.require()
    opened = SESSION.opened_round_trip or m.round_trip()
    current = m.round_trip()
    edited = SESSION.receipt()["dirty"]

    if not opened["identical"] and not allow_non_identical:
        # A refused write is an anticipated failure, so it is an error rather than a
        # `{"written": False}` result a caller can skim past. The first difference is what
        # makes it actionable, so it travels in the message — nothing else survives.
        where = opened.get("first_difference")
        detail = f" First difference: {where}." if where else ""
        raise ToolError(
            "refusing to write: the kernel could not reproduce this file byte-for-byte "
            f"when it was opened, so writing it could lose data.{detail} Pass "
            "allow_non_identical=true if you accept the risk."
        )

    playspace_report: dict | None = None
    playspace_note: str | None = None
    baseline = SESSION.path if SESSION.path and SESSION.path.is_file() else None
    if check_playable_space and edited and baseline is not None:
        try:
            playspace_report = playspacemod.diff(baseline, (baseline, m))
        except (anamod.AnalysisError, playspacemod.PlayspaceError) as e:
            playspace_note = f"playable space could not be compared, so it was not checked: {e}"

    if playspace_report is not None and playspacemod.has_errors(playspace_report):
        if not acknowledge_regression:
            brief = playspacemod.brief(playspace_report)
            findings = "; ".join(
                f"{f.get('code')}: {f.get('message')}"
                for f in brief["findings"]
                if f.get("severity") == "error"
            )
            raise ToolError(
                "refusing to write: this would destroy playable space. "
                f"{findings or 'See playable_space_diff for the findings.'} "
                "Call playable_space_diff for the full report, or pass "
                "acknowledge_regression=true if you meant to remove that space."
            )
        SESSION.warnings.append(
            "wrote a map with a playable-space regression, acknowledged by the caller"
        )

    target = Path(path) if path else SESSION.path
    if target is None:
        raise ToolError("no destination for the open map")
    if not target.is_absolute():
        target = repo_root() / target
    target = target.resolve()
    source_path = SESSION.path.resolve()
    changes = {target: m.source().encode("utf-8")}
    expected = {target: SESSION.baseline_files.get(source_path)}
    if target != source_path:
        if target.exists():
            raise ToolError(f"save-copy destination already exists: {target}")
        expected[target] = None
    companions = dict(SESSION.pending_files)
    if target != source_path:
        companions = {
            p: data
            for p, data in {**SESSION.baseline_files, **companions}.items()
            if p != source_path and data is not None
        }
    for companion, data in companions.items():
        dest = companion
        if target != source_path:
            # Companions beside the source follow Save As, including compound suffixes.
            if companion.parent != source_path.parent or not companion.name.startswith(
                source_path.stem + "."
            ):
                raise ToolError(
                    f"cannot relocate staged companion {companion}; save to the original path"
                )
            dest = target.with_name(target.stem + companion.name[len(source_path.stem) :])
            if dest.exists():
                raise ToolError(f"save-copy companion already exists: {dest}")
        changes[dest] = data
        expected[dest] = SESSION.baseline_files.get(companion) if dest == companion else None
    try:
        write_batch(changes, expected)
    except OSError as e:
        raise ToolError(f"save failed; staged changes remain available: {e}") from e
    SESSION.path = target
    if target != source_path:
        relocated = {}
        for companion, data in SESSION.baseline_files.items():
            if companion.parent == source_path.parent and companion.name.startswith(
                source_path.stem + "."
            ):
                dest = target.with_name(target.stem + companion.name[len(source_path.stem) :])
                relocated[dest] = data
        SESSION.baseline_files = {**relocated, **changes}
    else:
        SESSION.baseline_files.update(changes)
    SESSION.pending_files.clear()
    SESSION.checkpoints.clear()
    written = str(target)
    out: dict[str, Any] = {
        "written": True,
        "path": written,
        "round_trip": current,
        "modified_since_open": edited,
        "files_written": [str(p) for p in changes],
        "revision": SESSION.receipt(),
    }
    if playspace_report is not None:
        out["playable_space"] = playspacemod.brief(playspace_report)
    if playspace_note:
        out["playable_space_note"] = playspace_note
    return out


@tool("map", "Checkpoint staged map and companions", writes=True, destructive=True, idempotent=True)
def map_checkpoint(name: str = "checkpoint") -> dict:
    """Remember the current geometry and staged companions under a name, without saving.

    Use map_restore to undo back to it. Checkpoints last until a save or another map is opened.
    """
    SESSION.checkpoints[name] = (SESSION.require().source(), dict(SESSION.pending_files))
    return {"checkpoint": name, "revision": SESSION.receipt()}


@tool("map", "Restore a checkpoint", writes=True, destructive=True, idempotent=True)
def map_restore(name: str = "checkpoint") -> dict:
    """Restore geometry and staged companions from map_checkpoint; disk is untouched.

    Save to persist the restored state. Saving clears checkpoints; this undoes staged edits.
    """
    if name not in SESSION.checkpoints:
        raise ToolError(f"no checkpoint {name!r}")
    source, companions = SESSION.checkpoints[name]
    SESSION.map = _kernel().Map.parse(source)
    SESSION.pending_files = dict(companions)
    return {"checkpoint": name, "revision": SESSION.receipt()}


@tool("map", "Mirror the whole map", writes=True, destructive=True)
def map_mirror(axis: str = "x", at: float = 0, vehicles: list[str] | None = None) -> dict:
    """Mirror the open map across the plane `axis = at`: geometry, entities, facings.

    Every brush and patch is reflected with its planes kept outward (the kernel swaps the
    plane points' order, which a reflection otherwise turns inside out), its texture mapping
    rewritten so the surface shows the mirror image of the original, and its patch grid
    reordered so it keeps facing out. Then the entity keys the profile names (`mirror:` in
    `nrc://profile/{id}`; ET: Legacy: `origin`, `angle`, `angles`, the command map corners)
    are reflected: a facing `a` along x becomes `180 - a` (along y, `-a`), `angles`
    `p y r` becomes `p 180-y -r` (`p -y -r`), and doors that swing are made to swing the
    mirrored way. Special yaw values (ET: -1 up, -2 down) stay.

    `vehicles` names (targetname or scriptname) brush entities the game turns to face their
    direction of travel, such as an escort truck on a spline and the triggers riding on it.
    Mirrored in place, their local "forward" would point backwards; each is given a half
    turn about its own origin instead, so it drives forward along the mirrored path and is
    the mirror image of itself across its own length.

    What it cannot mirror, it reports: raw primitive blocks the kernel keeps as text, and
    models (`misc_model` and the like), which turn to the mirrored facing but are drawn
    unmirrored. A map script is not touched; coordinates or angles written in it (rotation
    targets, `faceangles`) need mirroring by hand. Nothing reaches disk until `map_save`.
    """
    m = SESSION.require()
    axes = {"x": 0, "y": 1}
    if axis not in axes:
        raise ToolError("axis is 'x' or 'y' (a mirror in z would turn floors into ceilings)")
    if not math.isfinite(at):
        raise ToolError("at is a coordinate")
    a = axes[axis]
    pid = active_profile()
    spec = (profiles.load(pid).get("mirror") if pid else None) or {}
    point_keys = [str(k) for k in spec.get("point_keys") or ["origin"]]
    yaw_key = str(spec.get("yaw_key") or "angle")
    angles_key = str(spec.get("angles_key") or "angles")
    yaw_special = {float(v) for v in spec.get("yaw_special") or []}
    corner_pairs = [tuple(map(str, p)) for p in spec.get("corner_pairs") or []]
    senses = spec.get("rotation_sense") or {}
    origin_texture = str(spec.get("origin_texture") or "")

    ents = m.entities(with_keys=True)
    named: dict[str, int] = {}
    for e in ents:
        for k, v in e["keys"]:
            if k.lower() in ("targetname", "scriptname"):
                named.setdefault(v, e["index"])
    missing = [n for n in vehicles or [] if n not in named]
    if missing:
        raise ToolError(f"no entity named {', '.join(missing)}")
    turned = sorted({named[n] for n in vehicles or []})

    candidate = m.copy()
    try:
        stats = dict(candidate.mirror_geometry(a, float(at)))
    except ValueError as e:
        raise ToolError(str(e)) from e

    def flip(text: str, centre: float) -> str:
        try:
            value = float(text)
        except ValueError:
            return text
        if centre == 0:
            if value == 0:
                return text
            return text[1:] if text.startswith("-") else "-" + text.lstrip("+")
        return _value_text("mirror", round(2 * centre - value, 6))

    def angle_text(value: float) -> str:
        value = round(value % 360, 6)
        return _value_text("mirror", 0 if value == 360 else value)

    changed_keys = 0
    for e in ents:
        keys = dict(e["keys"])  # the last copy is the one the engine reads
        new: list[tuple[str, str]] = []
        origin = None
        for key in point_keys:
            if key in keys:
                parts = keys[key].split()
                if len(parts) == 3:
                    if key == "origin":
                        with contextlib.suppress(ValueError):
                            origin = [float(p) for p in parts]
                    parts[a] = flip(parts[a], float(at))
                    new.append((key, " ".join(parts)))
        if yaw_key in keys:
            with contextlib.suppress(ValueError):
                yaw = float(keys[yaw_key])
                if yaw not in yaw_special:
                    new.append((yaw_key, angle_text(180 - yaw if a == 0 else -yaw)))
        if angles_key in keys:
            parts = keys[angles_key].split()
            with contextlib.suppress(ValueError):
                p, y, r = (float(v) for v in parts)
                y = 180 - y if a == 0 else -y
                new.append((angles_key, " ".join(angle_text(v) for v in (p, y, -r))))
        for lo, hi in corner_pairs:
            if lo in keys and hi in keys:
                lo_v, hi_v = keys[lo].split(), keys[hi].split()
                if len(lo_v) == 2 and len(hi_v) == 2:
                    lo_v[a], hi_v[a] = flip(hi_v[a], float(at)), flip(lo_v[a], float(at))
                    new += [(lo, " ".join(lo_v)), (hi, " ".join(hi_v))]
        sense = senses.get(e["classname"])
        if sense:
            flags = int(float(keys.get("spawnflags", "0") or 0))
            about_axis = int((sense.get("axis_flags") or {}).get(axis, 0))
            if not (about_axis and flags & about_axis):
                new.append(("spawnflags", str(flags ^ int(sense["flag"]))))
        new = [(k, v) for k, v in new if keys.get(k) != v]
        if new:
            try:
                candidate.set_keys(e["index"], new)
            except ValueError as err:
                raise ToolError(f"entity {e['index']}: {err}") from err
            changed_keys += len(new)
        if e["index"] in turned:
            if origin is not None:
                o = list(origin)
                o[a] = 2 * float(at) - o[a]
            else:
                o = _origin_brush_centre(candidate, e["index"], origin_texture)
            if o is None:
                raise ToolError(f"entity {e['index']} has no origin key or brush to turn about")
            # A half turn is two mirrors, about the (mirrored) origin's x and y.
            for ax in (0, 1):
                candidate.mirror_geometry(ax, o[ax], e["index"])

    models = [
        e["index"]
        for e in ents
        if any(k == "model" and not v.startswith("*") for k, v in e["keys"])
        or any(k == "model2" for k, _ in e["keys"])
    ]
    SESSION.map = candidate
    notes = []
    if stats.get("raw_blocks"):
        notes.append(f"{stats['raw_blocks']} raw primitive blocks were left unmirrored")
    if stats.get("degenerate_faces"):
        notes.append(f"{stats['degenerate_faces']} faces with collinear points kept their texdef")
    if models:
        notes.append(
            f"{len(models)} entities draw a model: they face the mirrored way but the model "
            "itself is not mirrored"
        )
    return {
        "axis": axis,
        "at": at,
        "geometry": stats,
        "entities_changed_keys": changed_keys,
        "vehicles_turned": turned,
        "models": models[:50],
        "notes": notes,
        "revision": SESSION.receipt(),
        "next": "map_save (to a new path for a mirrored copy); mirror map-script coordinates by hand",
    }


def _origin_brush_centre(m: Any, entity: int, texture: str) -> list[float] | None:
    """The centre of an entity's origin brush (every face `texture`), or None."""
    if not texture:
        return None
    for ent, prim, _ in m.brush_sources():
        if ent != entity:
            continue
        g = m.brush_geometry(ent, prim)
        if g.get("usable") and all(s.lower() == texture.lower() for s in g["shaders"]):
            vs = g["vertices"]
            return [(min(v[i] for v in vs) + max(v[i] for v in vs)) / 2 for i in range(3)]
    return None


@tool("map", "Query entities")
def query_entities(classname: str | None = None, with_keys: bool = True) -> dict:
    """Entities in the open map, optionally filtered by classname.

    Keys come back as ordered pairs rather than a mapping, because key order is meaningful
    in a `.map` and duplicate keys occur in real files.
    """
    m = SESSION.require()
    ents = m.entities(classname=classname, with_keys=with_keys)
    return {"count": len(ents), "filter": classname, "entities": ents}


@tool("map", "Place a point entity", writes=True)
def entity_place(
    classname: str,
    origin: list[float],
    keys: dict[str, str | float | list[float]] | None = None,
    grid: int | None = None,
) -> dict:
    """Add a point entity to the open map: a spawn, a script or objective marker, a light.

    `keys` are written after `classname` and `origin`, in the order given. A number becomes
    its shortest spelling and a list of numbers a space-separated vector, so
    `{"angles": [0, 90, 0]}` writes `"angles" "0 90 0"`.

    The classname and keys are checked against the active profile and anything it does not
    declare comes back in `notes` — written anyway, because profiles are incomplete, but a
    misspelt key is silent in game, so read them. `profile_summary` and `nrc://profile/{id}`
    list what the profile knows. The origin is written as given, never snapped; one off the
    grid (default: the session's) is noted, as the brush tools count off-grid vertices.

    A classname the profile declares as a brush entity — a door, a trigger — is refused: build
    its brushes with `solid_commit` and `target_classname`, then give it keys with
    `entity_set_keys`. Nothing reaches disk until `map_save`. To add several entities, use
    `entities_place`: one call, all or none.
    """
    pairs = _point_pairs(classname, origin, keys)
    index = _add_entity(pairs)
    return {
        "entity": index,
        "keys": pairs,
        "notes": _placement_notes(classname, pairs, origin, grid),
        "entities_now": SESSION.map.entity_count,
        "next": "call map_save to write it, and validate_profile to check spawns and counts",
    }


@tool("map", "Place several point entities", writes=True)
def entities_place(entities: list[dict[str, Any]], grid: int | None = None) -> dict:
    """Add many point entities in one call, all or none: lights, spawns, markers.

    Each item is `{"classname": ..., "origin": [x, y, z], "keys": {...}}`, checked exactly as
    `entity_place` checks one. Every item is checked before any is added, so a refusal names
    the item (`entities[3]: ...`) and changes nothing.

    The reply is compact: the new indices, in the order given, and each distinct note once
    with the items it applies to. Ten lights with the same key produce one note, not ten.
    Read the notes: a key the profile does not declare is written anyway and does nothing
    in game if it is misspelt. For team spawns with their region marker, `spawn_cluster`
    also finds the standing positions. Nothing reaches disk until `map_save`.
    """
    m = SESSION.require()
    if not entities:
        raise ToolError("no entities given; pass a list of {classname, origin, keys}")
    prepared = []
    for i, item in enumerate(entities):
        where = f"entities[{i}]"
        if not isinstance(item, dict):
            raise ToolError(f"{where}: each item is an object {{classname, origin, keys}}")
        extra = sorted(set(item) - {"classname", "origin", "keys"})
        if extra:
            raise ToolError(
                f"{where}: unknown field(s) {extra}; an item has classname, origin, keys"
            )
        classname, origin, keys = item.get("classname"), item.get("origin"), item.get("keys")
        if not isinstance(classname, str) or not classname:
            raise ToolError(f"{where}: classname must be a non-empty string")
        if not isinstance(origin, list) or not all(
            isinstance(c, (int, float)) and not isinstance(c, bool) for c in origin
        ):
            raise ToolError(f"{where}: origin must be a list of three numbers")
        if keys is not None and not isinstance(keys, dict):
            raise ToolError(f"{where}: keys must be a mapping of key to value")
        try:
            pairs = _point_pairs(classname, origin, keys)
        except ToolError as e:
            raise ToolError(f"{where}: {e}") from e
        prepared.append((classname, origin, pairs))

    # Built on a copy, so an entity the kernel refuses part-way leaves nothing behind.
    candidate = m.copy()
    indices = []
    for i, (_classname, _origin, pairs) in enumerate(prepared):
        try:
            indices.append(candidate.add_entity(pairs))
        except ValueError as e:
            raise ToolError(f"entities[{i}]: {e}; nothing was added") from e
    SESSION.map = candidate

    g = grid if grid is not None else SESSION.grid
    grouped: dict[str, list[int]] = {}
    off_grid: list[int] = []
    for i, (classname, origin, pairs) in enumerate(prepared):
        for note in _profile_notes(classname, pairs):
            grouped.setdefault(note, []).append(i)
        if g > 0 and any(float(c) % g for c in origin):
            off_grid.append(i)
    if off_grid:
        note = f"origin off the {g}-unit grid; written as given, not snapped"
        grouped[note] = off_grid
    counts: dict[str, int] = {}
    for classname, _, _ in prepared:
        counts[classname] = counts.get(classname, 0) + 1
    return {
        "placed": len(indices),
        "entities": _index_span(indices),
        "classnames": counts,
        "notes": [{"note": note, "items": _index_span(items)} for note, items in grouped.items()],
        "entities_now": candidate.entity_count,
        "next": "call map_save to write them, and validate_profile to check spawns and counts",
    }


def _index_span(indices: list[int]) -> list[int] | dict[str, int]:
    """Consecutive indices as `{"first", "last"}`, anything else as the list itself."""
    if len(indices) > 2 and indices == list(range(indices[0], indices[0] + len(indices))):
        return {"first": indices[0], "last": indices[-1]}
    return indices


def _point_pairs(
    classname: str, origin: list[float], keys: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """The key/value pairs for a point entity, or a refusal. Changes nothing."""
    if "classname" in (keys or {}) or "origin" in (keys or {}):
        raise ToolError("pass classname and origin as their own arguments, not inside keys")
    if len(origin) != 3:
        raise ToolError(f"origin must be three numbers, got {len(origin)}")
    pid = active_profile()
    ent = None
    if pid:
        with contextlib.suppress(profiles.ProfileError):
            ent = profiles.entity_def(pid, classname)
    if ent is not None and ent.get("kind") == "brush":
        raise ToolError(
            f"profile {pid} declares {classname} as a brush entity, and a point entity of it "
            "has no volume. Build it with solid_commit(target_classname=...), then give it "
            "keys with entity_set_keys."
        )
    pairs = [("classname", classname), ("origin", _value_text("origin", origin))]
    return pairs + [(k, _value_text(k, v)) for k, v in (keys or {}).items()]


def _placement_notes(
    classname: str, pairs: list[tuple[str, str]], origin: list[float], grid: int | None
) -> list[str]:
    g = grid if grid is not None else SESSION.grid
    notes = _profile_notes(classname, pairs)
    if g > 0 and any(float(c) % g for c in origin):
        notes.append(
            f"origin {pairs[1][1]} is off the {g}-unit grid; it was written as given, not snapped"
        )
    return notes


@tool("map", "Set entity keys", writes=True, destructive=True, idempotent=True)
def entity_set_keys(entity: int, keys: dict[str, str | float | list[float]]) -> dict:
    """Set keys on any entity — a point entity, a brush entity or the worldspawn.

    `entity` is the index `query_entities` reports; worldspawn is normally 0. An existing key
    keeps its position and gets the new value; a new key is appended. Values are spelt as in
    `entity_place`. Changing `classname` is allowed, except on the worldspawn or into a
    second one.

    The previous value of every key comes back in `changed`, because this overwrites. If the
    entity carried a key twice, the later copy is what the engine reads, so it is removed and
    reported in `duplicates_removed`. Keys and values the profile does not declare for the
    classname come back in `notes`, as in `entity_place`.

    Every key is checked before any is written, so a refusal changes nothing. Nothing
    reaches disk until `map_save`.
    """
    m = SESSION.require()
    if not keys:
        raise ToolError("no keys given; pass a mapping of key to value")
    pairs = [(k, _value_text(k, v)) for k, v in keys.items()]
    try:
        results = m.set_keys(_entity_index(entity), pairs)
    except ValueError as e:
        raise ToolError(str(e)) from e

    classname = m.entities(with_keys=False)[entity]["classname"]
    values = dict(pairs)
    out: dict[str, Any] = {
        "entity": entity,
        "classname": classname,
        "changed": [
            {"key": r["key"], "value": values[r["key"]], "previous": r["previous"]} for r in results
        ],
        "notes": _profile_notes(classname, pairs),
        "next": "call map_save to write it",
    }
    duplicates = {r["key"]: r["duplicates_removed"] for r in results if r["duplicates_removed"]}
    if duplicates:
        out["duplicates_removed"] = duplicates
    return out


@tool("map", "Remove entity keys", writes=True, destructive=True, idempotent=True)
def entity_remove_keys(entity: int, keys: list[str]) -> dict:
    """Remove keys from any entity, every copy of each.

    `entity` is the index `query_entities` reports. A key the entity does not have is listed
    in `absent` rather than refused, so the call is safe to repeat. `classname` cannot be
    removed: change it with `entity_set_keys` instead. Nothing reaches disk until `map_save`.
    """
    m = SESSION.require()
    if not keys:
        raise ToolError("no keys given; pass a list of key names")
    try:
        results = m.remove_keys(_entity_index(entity), list(keys))
    except ValueError as e:
        raise ToolError(str(e)) from e
    return {
        "entity": entity,
        "classname": m.entities(with_keys=False)[entity]["classname"],
        "removed": [k for k, removed in results if removed],
        "absent": [k for k, removed in results if not removed],
        "next": "call map_save to write it",
    }


def _entity_index(entity: int) -> int:
    # The kernel takes an unsigned index; a negative one would fail as an overflow, with a
    # message about integers rather than about entities.
    if entity < 0:
        raise ToolError(f"no entity {entity}: indices start at 0; query_entities lists them")
    return entity


def _value_text(key: str, value: Any) -> str:
    """A key's value as the `.map` spells it."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ToolError(f"{key}: {value} is not a number a .map can hold")
        # repr is the shortest spelling that reads back as the same float; `:g` is not, and
        # rounds a coordinate like 1234.5678 to six significant figures.
        return str(int(value)) if float(value).is_integer() else repr(float(value))
    if isinstance(value, (list, tuple)):
        return " ".join(_value_text(key, v) for v in value)
    raise ToolError(
        f"{key}: a value is a string, a number or a list of numbers, not {type(value).__name__}"
    )


def _profile_notes(classname: str, pairs: list[tuple[str, str]]) -> list[str]:
    """What the active profile does not know about this classname and these keys.

    Advice, never a refusal: a profile is extracted from a gamepack and can lag the game, so a
    key it does not declare may still be real. But a misspelt key does nothing in game and
    says nothing, so it is worth a sentence.
    """
    pid = active_profile()
    if not pid:
        return ["no profile is selected (NRC_PROFILE), so the classname and keys were not checked"]
    try:
        ent = profiles.entity_def(pid, classname)
        declared = profiles.declared_keys(pid, ent) if ent is not None else {}
    except profiles.ProfileError as e:
        return [f"the classname and keys were not checked: {_profile_error_text(e)}"]
    if ent is None:
        return [
            f"profile {pid} does not define classname {classname!r}. It was written anyway, "
            "since the profile may be incomplete, but a misspelt classname does nothing in game"
        ]

    notes = []
    # Keys every entity format has, which entries do not repeat.
    generic = {"classname"}
    if ent.get("kind") == "point":
        generic.add("origin")
    if ent.get("spawnflags"):
        generic.add("spawnflags")
    unknown = [k for k, _ in pairs if k not in declared and k not in generic]
    if unknown:
        notes.append(
            f"profile {pid} declares no {', '.join(repr(k) for k in unknown)} on {classname}. "
            "Written anyway, since the profile may be incomplete; check the spelling. It "
            f"declares: {', '.join(sorted(declared)) or 'no keys'}"
        )
    for k, v in pairs:
        allowed = (declared.get(k) or {}).get("values")
        if isinstance(allowed, list) and allowed and v not in [str(a) for a in allowed]:
            notes.append(
                f"{k}={v!r} is not one of the values profile {pid} lists for it: "
                f"{', '.join(str(a) for a in allowed)}"
            )
    return notes


@tool("map", "Brush geometry")
def brush_geometry(entity: int, primitive: int) -> dict:
    """Exact vertices and derived properties of one brush.

    Vertices are computed with exact integer arithmetic. When a brush cannot be evaluated
    exactly — off-grid plane points, too few faces — this reports `usable: false` with a
    reason rather than returning approximate numbers that look authoritative.
    """
    m = SESSION.require()
    return m.brush_geometry(entity, primitive)


@tool("map", "Validate geometry")
def validate(grid: int | None = None, severity_min: str = "warning") -> dict:
    """Validate the open map's geometry and file format.

    Every finding carries a `rule_source` and a `confidence`. Only `verified` findings are
    safe to treat as hard failures.

    Scope note: these are the game-agnostic checks — degenerate brushes, duplicate and
    mirrored planes, off-grid vertices, thin brushes, patch problems. For the game's own rules
    (spawn arrangement, objective counts, gametype keys) call `validate_profile`.

    Off-grid and too-thin are errors for geometry added or reshaped since `map_open` - new
    work is held to the grid (an angled solid from `solid_commit` cannot be on it; `solid_help`
    says which shapes are) - and warnings for brushes the map already had, summarised in one
    finding per code: neither stops the compiler or the engine, and an existing map's angled
    geometry is not this session's to fix.
    """
    m = SESSION.require()
    g = grid if grid is not None else SESSION.grid
    report = m.validate(grid=g, severity_min="info")
    report = _grade_by_provenance(m, report)
    floor = rules.SEVERITIES.index(severity_min) if severity_min in rules.SEVERITIES else 0
    report["findings"] = [
        f for f in report["findings"] if rules.SEVERITIES.index(f["severity"]) >= floor
    ]
    return report


#: Findings about how a brush sits on the grid, not whether the map works.
GRID_CODES = ("BRUSH_OFF_GRID", "BRUSH_TOO_THIN")


def _grade_by_provenance(m: Any, report: dict) -> dict:
    """Grid findings on brushes the map was opened with become one warning per code."""
    opened = SESSION.opened_brushes
    if not opened:
        return report
    fingerprints = {(e, p): h for e, p, h in m.brush_fingerprints()}
    kept, inherited = [], {}
    for f in report.get("findings", []):
        old = fingerprints.get((f.get("entity"), f.get("primitive"))) in opened
        if f.get("code") in GRID_CODES and f.get("severity") == "error" and old:
            inherited.setdefault(f["code"], []).append(f)
        else:
            kept.append(f)
    for code, found in inherited.items():
        kept.append(
            {
                **found[0],
                "severity": "warning",
                "primitive": None,
                "face": None,
                "location": f"{len(found)} brushes",
                "message": f"{len(found)} brush(es) the map already had when it was opened: "
                f"{code}, for example entity {found[0].get('entity')} brush "
                f"{found[0].get('primitive')} ({found[0].get('message')}). Neither the compiler "
                "nor the engine stops on it; new geometry is held to the grid as an error.",
                "count": len(found),
                "examples": [[x.get("entity"), x.get("primitive")] for x in found[:10]],
            }
        )
    order = {"error": 0, "warning": 1, "info": 2}
    kept.sort(key=lambda f: order.get(f.get("severity"), 3))
    summary = {
        s: sum(1 for f in kept if f.get("severity") == s) for s in ("error", "warning", "info")
    }
    return {**report, "findings": kept, "summary": summary}


@tool("map", "Validate against game profile")
def validate_profile(profile_id: str | None = None, severity_min: str = "info") -> dict:
    """Validate the open map's entities against the active game profile.

    Complements `validate`, which covers game-agnostic geometry. This checks the game's own
    rules: spawn arrangement, objective counts, gametype keys — and, for a game whose maps
    carry a script, that script against the map's entities. For a game with speaker and sound
    scripts (the profile's `sound:` section), their syntax, the sound files they name, and
    whether the sound scripts and speakers the map and its script name exist. Files are looked
    for beside the map (under the folder holding `maps/`) and, with the game path set, in the
    base game.

    Every rule is data in the profile, and every finding carries a `confidence`. **An
    unverified rule can never produce an error** — it is downgraded to `info`;
    `nrc://corrections` says why.
    """
    m = SESSION.require()
    pid = profile_id or active_profile()
    if not pid:
        return {
            "error": "no profile selected; set NRC_PROFILE or pass profile_id",
            "available": profiles.available(),
        }
    try:
        ents = m.entities()
        found = rules.evaluate(pid, ents) + rules.unknown_classnames(pid, ents)
        # A profile with a map script also checks the script beside the .map against it.
        spec = profiles.load(pid).get("mapscript")
        text = None
        if spec and SESSION.path:
            data = SESSION.read_file(mapscript.script_path(spec, SESSION.path))
            text = data.decode("utf-8", errors="replace") if data is not None else ""
        found += mapscript.check(pid, ents, SESSION.path, text=text)
        # And its speaker and sound scripts, and the names that point into them.
        found += soundscript.check(pid, ents, SESSION.path, script_text=text)

    except profiles.ProfileError as e:
        raise ToolError(_profile_error_text(e)) from e
    found += _missing_textures(m, pid)

    floor = rules.SEVERITIES.index(severity_min) if severity_min in rules.SEVERITIES else 0
    kept = [f for f in found if rules.SEVERITIES.index(f.severity) >= floor]
    order = {"error": 0, "warning": 1, "info": 2}
    kept.sort(key=lambda f: (order[f.severity], f.code))
    return {
        "profile": pid,
        "entities_checked": len(ents),
        "summary": rules.summarize(found),
        "findings": [f.as_dict() for f in kept],
    }


def _missing_textures(m, pid: str) -> list[rules.Finding]:
    """shader_audit's SHADER_MISSING, so the check a builder runs also catches a texture name
    nothing supplies - which packs into a _FAILEDpack archive. Only with the base game known
    and the map on disk (what it ships is looked for beside it); otherwise nothing is claimed.
    """
    from .. import optimize as optmod  # noqa: PLC0415
    from .compiling import _basegame_dir  # noqa: PLC0415

    if SESSION.path is None:
        return []
    basegame = _basegame_dir(gamepath.basepath(), pid)
    if not basegame or not Path(basegame).is_dir():
        return []
    try:
        audit = optmod.shader_audit(SESSION.path, [], pid, [basegame], game_map=m)
    except (OSError, ValueError):
        return []
    return [
        rules.Finding(
            code=f["code"],
            severity=f["severity"],
            message=f["message"] + " Find a real one with texture_search.",
            confidence=f.get("confidence", "verified"),
            rule_source=f.get("rule_source", "shader_audit"),
            fix_hint=f.get("fix_hint", ""),
        )
        for f in audit.get("findings", [])
        if f["code"] == "SHADER_MISSING"
    ]


def _script_spec() -> tuple[str, dict, Path]:
    """The active profile's map-script description and the open map's script path."""
    SESSION.require()
    if SESSION.path is None:
        raise ToolError("the open map has no path on disk, so its script has no place to live")
    pid = active_profile()
    if not pid:
        raise ToolError("no profile selected; set NRC_PROFILE")
    try:
        spec = profiles.load(pid).get("mapscript")
    except profiles.ProfileError as e:
        raise ToolError(_profile_error_text(e)) from e
    if not isinstance(spec, dict):
        raise ToolError(f"profile {pid} describes no map script; this game's maps carry none")
    return pid, spec, mapscript.script_path(spec, SESSION.path)


def _action_text(action: mapscript.Action) -> str:
    """An action as a script line, quoting arguments with spaces, so it can be written back."""
    args = [f'"{x}"' if (not x or any(c.isspace() for c in x)) else x for x in action.args]
    return " ".join([action.name, *args])


def _script_report(pid: str, spec: dict, path: Path, text: str) -> dict:
    """The script as blocks and events, which blocks run, and what the checker says."""
    ents = SESSION.map.entities()
    named = mapscript.names(spec, ents)
    blocks = mapscript.parse(text, mapscript.brace_actions(spec)) if text.strip() else []
    found = mapscript.check(pid, ents, SESSION.path, text=text)
    return {
        "path": str(path),
        "exists": SESSION.read_file(path) is not None,
        "staged": path.resolve() in SESSION.pending_files,
        "blocks": [
            {
                "block": b.name,
                "line": b.line,
                "runs_for_entities": named.get(b.name.lower(), []),
                "events": [
                    {
                        "event": " ".join([e.name, *e.params]),
                        "line": e.line,
                        "actions": [_action_text(a) for a in e.actions],
                    }
                    for e in b.events
                ],
            }
            for b in blocks
        ],
        "summary": rules.summarize(found),
        "findings": [f.as_dict() for f in found],
    }


def _write_script(path: Path, text: str) -> None:
    SESSION.stage_file(path, text.encode("utf-8"))


@tool("map", "Show the map script")
def script_show() -> dict:
    """The open map's script: its blocks and events, which entities run each block, and findings.

    For a game whose maps carry a script beside the `.map` (the profile's `mapscript:`). A block
    with an empty `runs_for_entities` never runs: no entity names it. The findings are the same
    ones `validate_profile` reports for the script.
    """
    pid, spec, path = _script_spec()
    text = (SESSION.read_file(path) or b"").decode("utf-8", errors="replace")
    try:
        return _script_report(pid, spec, path, text)
    except mapscript.ScriptSyntaxError as e:
        raise ToolError(f"{path.name}: {e}; fix it by hand, the engine stops on it too") from e


@tool("map", "Set a script event", writes=True, destructive=True, idempotent=True)
def script_set_event(
    block: str, event: str, actions: list[str], params: list[str] | None = None
) -> dict:
    """Set one event's actions in a block of the open map's script, creating what is missing.

    `actions` are the event's lines in order, each one action with its arguments, e.g.
    `["wait 50", "setautospawn \"Axis Bunker\" 0"]`. They replace the event's current actions;
    the event, its block and the script file are created if they do not exist. `params` follow
    the event name, as in `trigger open`. Everything else in the file - comments, layout,
    other blocks - is left exactly as it was.

    Refused before anything is written: an event or action the profile does not list, and a
    line the engine would misparse. Written, then reported in `findings`: references that
    resolve to nothing yet, since the entity may be placed next. The script is staged
    with the geometry; `map_save` saves both and checks for external changes.
    """
    pid, spec, path = _script_spec()
    text = (SESSION.read_file(path) or b"").decode("utf-8", errors="replace")
    try:
        new, did = mapscript.set_event(text, spec, block, event, list(params or []), actions)
    except mapscript.ScriptEditError as e:
        raise ToolError(f"refused, nothing written: {e}") from e
    except mapscript.ScriptSyntaxError as e:
        raise ToolError(f"{path.name} does not parse ({e}); fix it by hand first") from e
    _write_script(path, new)
    return {"did": did, **_script_report(pid, spec, path, new)}


@tool("map", "Remove from the map script", writes=True, destructive=True, idempotent=False)
def script_remove(block: str, event: str | None = None, params: list[str] | None = None) -> dict:
    """Remove one event from a block of the open map's script, or the whole block.

    Without `event`, the whole block goes. Everything else in the file is left as it was. The
    script is staged until `map_save`.
    """
    pid, spec, path = _script_spec()
    if SESSION.read_file(path) is None:
        raise ToolError(f"there is no script at {path} to remove anything from")
    text = SESSION.read_file(path).decode("utf-8", errors="replace")
    try:
        new = mapscript.remove(text, spec, block, event, list(params or []))
    except mapscript.ScriptEditError as e:
        raise ToolError(f"nothing removed: {e}") from e
    except mapscript.ScriptSyntaxError as e:
        raise ToolError(f"{path.name} does not parse ({e}); fix it by hand first") from e
    _write_script(path, new)
    return {
        "removed": " ".join([block, *([event] if event else []), *(params or [])]),
        **_script_report(pid, spec, path, new),
    }


def _fill(value: Any, subs: dict[str, str], seqs: dict[str, list[dict]] | None = None) -> Any:
    """A recipe with `{placeholders}` filled in.

    A list item `{"each": <sequence>, "do": [...]}` stands for the `do` items once per row of
    that sequence (an escort's path segments), each row adding its own placeholders.
    """
    if isinstance(value, str):
        return value.format(**subs)
    if isinstance(value, list):
        out: list = []
        for v in value:
            if isinstance(v, dict) and "each" in v:
                rows = (seqs or {}).get(str(v["each"]))
                if rows is None:
                    raise ToolError(
                        f"the recipe repeats over {v['each']!r}, which it does not have"
                    )
                for row in rows:
                    out.extend(_fill(list(v.get("do") or []), {**subs, **row}, seqs))
            else:
                out.append(_fill(v, subs, seqs))
        return out
    if isinstance(value, dict):
        return {k: _fill(v, subs, seqs) for k, v in value.items()}
    return value


def _as_ir(shape: dict, what: str) -> dict:
    """A box given as {min, max}, or any Solid IR tree."""
    if not isinstance(shape, dict):
        raise ToolError(f"{what} must be a box {{min, max}} or a Solid IR tree")
    if "op" in shape:
        return shape
    if "min" in shape and "max" in shape:
        return {"op": "box", "min": shape["min"], "max": shape["max"]}
    raise ToolError(f"{what} must be a box {{min, max}} or a Solid IR tree")


def _xyz(value: Any) -> list[float] | None:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 3
        and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in value)
    ):
        return [float(c) for c in value]
    return None


def _as_points(value: Any, role: str) -> list[tuple[list[float], Any]]:
    """A role's positions: [x, y, z], {"origin": [x, y, z], "angle": yaw}, or a list of those."""
    shape = f"points.{role}: a point is [x, y, z] or {{origin, angle}}"
    one = _xyz(value)
    if one is not None:
        return [(one, None)]
    if isinstance(value, dict):
        origin = _xyz(value.get("origin"))
        angle = value.get("angle")
        if origin is None or set(value) - {"origin", "angle"}:
            raise ToolError(shape)
        if angle is not None and (isinstance(angle, bool) or not isinstance(angle, (int, float))):
            raise ToolError(f"points.{role}: angle is a yaw in degrees")
        return [(origin, angle)]
    if isinstance(value, list) and value and all(isinstance(v, (list, dict)) for v in value):
        return [p for v in value for p in _as_points(v, role)]
    raise ToolError(shape + ", or a list of them")


def _for_team(part: dict, team: str) -> bool:
    """A recipe part that names `teams` is built only for those teams."""
    only = part.get("teams")
    return not only or team in [str(t) for t in only]


def _point(value: Any, what: str) -> list[float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or not all(
            isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c)
            for c in value
        )
    ):
        raise ToolError(f"{what} must be three finite numbers, got {value!r}")
    return [float(c) for c in value]


def _as_shape(shape: Any, what: str) -> dict:
    """`{"point": [x, y, z]}` for `{"origin": [x, y, z]}`, else `{"ir": <Solid IR>}`."""
    if isinstance(shape, dict) and set(shape) == {"origin"}:
        return {"point": _point(shape["origin"], f"{what} origin")}
    return {"ir": _as_ir(shape, what)}


def _centre(k: Any, shape: dict) -> list[float]:
    """Where a point entity goes for a shape: the point itself, or the middle of its bounds."""
    if "point" in shape:
        return shape["point"]
    b = k.solid_compile(shape["ir"], None, 1)["bounds"]
    if not b:
        raise ToolError("the shape is empty, so nothing can be placed inside it")
    return [(lo + hi) / 2 for lo, hi in zip(b["min"], b["max"], strict=True)]


def _paths_spec(pid: str) -> dict:
    spec = profiles.load(pid).get("paths")
    if not isinstance(spec, dict) or not spec.get("point_class"):
        raise ToolError(f"profile {pid} describes no paths for movers to follow")
    return spec


def _path_chain(m: Any, spec: dict, start: str) -> list[tuple[str, list[float]]]:
    """The points of the path that starts at `start`, in order, by following each point's
    next key as the engine links them."""
    fold = spec.get("case_sensitive") is False
    norm = (lambda s: s.lower()) if fold else (lambda s: s)
    points: dict[str, tuple[str, list[float] | None, dict]] = {}
    for e in m.entities(classname=str(spec["point_class"])):
        # A later duplicate key is the one the engine reads.
        keys = {str(a).lower(): str(b) for a, b in e["keys"]}
        nm = keys.get(str(spec["name_key"]).lower(), "").strip()
        if nm:
            points.setdefault(norm(nm), (nm, e["origin"], keys))
    here = points.get(norm(start))
    if here is None:
        raise ToolError(
            f"no {spec['point_class']} is named {start!r}; lay the path with path_place and pass "
            "the `start` it returns"
        )
    chain: list[tuple[str, list[float]]] = []
    seen: set[str] = set()
    while here is not None:
        nm, origin, keys = here
        if norm(nm) in seen:
            raise ToolError(f"the path from {start!r} loops back to {nm!r}; a path must end")
        if origin is None:
            raise ToolError(f"path point {nm!r} has no origin")
        seen.add(norm(nm))
        chain.append((nm, [float(c) for c in origin]))
        nxt = keys.get(str(spec["next_key"]).lower(), "").strip()
        if not nxt:
            break
        here = points.get(norm(nxt))
        if here is None:
            raise ToolError(f"path point {nm!r} leads to {nxt!r}, which is not a path point")
    return chain


def _curve_length(pts: list[list[float]], steps: int = 64) -> float:
    """Length of the Bezier curve through these control points, by de Casteljau."""

    def at(t: float) -> list[float]:
        ps = [list(p) for p in pts]
        while len(ps) > 1:
            ps = [
                [a + (b - a) * t for a, b in zip(p, q, strict=True)]
                for p, q in itertools.pairwise(ps)
            ]
        return ps[0]

    samples = [at(i / steps) for i in range(steps + 1)]
    return sum(math.dist(a, b) for a, b in itertools.pairwise(samples))


@tool("map", "Lay a path", writes=True)
def path_place(name: str, points: list, max_segment: float = 0) -> dict:
    """Lay a path for a mover to follow: a chain of named path points, straight or curved.

    `points` are where the mover's origin travels, in order, at least two: each `[x, y, z]`,
    or `{"at": [x, y, z], "controls": [[x, y, z], ...]}` to bend the segment that leaves that
    point into a curve pulled towards its control points (the profile says how many a segment
    may have; ET: Legacy takes four). The last point has no segment, so no controls.

    The points are named `<name>_0`, `<name>_1`, ... and each names the next, as the engine
    links them; control points are `<name>_<i>_c<j>`. The first point's name comes back as
    `start`: pass it to `objective_place(path=...)`. A mover stops only at points, so for a
    vehicle that should halt soon after its escort leaves, keep segments short:
    `max_segment` splits every straight segment longer than that into equal ones.

    For an escort vehicle, put the points at the height of the vehicle's middle and the first
    one inside the vehicle where it starts: the vehicle keeps its offset from the point it
    travels with, and turns to face along each segment. Nothing reaches disk until `map_save`.
    """
    m = SESSION.require()
    pid = active_profile()
    if not pid:
        raise ToolError("no profile selected; set NRC_PROFILE")
    spec = _paths_spec(pid)
    try:
        mapscript._word(name, "path name")
    except mapscript.ScriptEditError as e:
        raise ToolError(str(e)) from e
    if not isinstance(points, list) or len(points) < 2:
        raise ToolError("a path needs at least two points")
    if max_segment < 0 or not math.isfinite(max_segment):
        raise ToolError("max_segment is a length in units, or 0 to keep segments as given")
    control_keys = [str(c) for c in spec.get("control_keys") or []]

    laid: list[tuple[list[float], list[list[float]]]] = []
    for i, p in enumerate(points):
        if isinstance(p, dict):
            unknown = set(p) - {"at", "controls"}
            if unknown or "at" not in p:
                raise ToolError(f"point {i} is [x, y, z] or {{at, controls}}, got keys {sorted(p)}")
            at = _point(p["at"], f"point {i}")
            controls = [_point(c, f"point {i} control") for c in p.get("controls") or []]
        else:
            at, controls = _point(p, f"point {i}"), []
        if len(controls) > len(control_keys):
            raise ToolError(
                f"point {i} has {len(controls)} controls; a segment takes at most "
                f"{len(control_keys)}"
            )
        if controls and i == len(points) - 1:
            raise ToolError("the last point has no segment to curve, so it takes no controls")
        laid.append((at, controls))

    # Split long straight segments into equal ones.
    route: list[tuple[list[float], list[list[float]]]] = []
    for (a, ctl), nxt in zip(laid, [*laid[1:], None], strict=True):
        route.append((a, ctl))
        if nxt is None or ctl or not max_segment:
            continue
        pieces = math.ceil(math.dist(a, nxt[0]) / max_segment)
        for s in range(1, pieces):
            t = s / pieces
            route.append(([u + (v - u) * t for u, v in zip(a, nxt[0], strict=True)], []))

    names = [f"{name}_{i}" for i in range(len(route))]
    wanted = {n.lower() for n in names}
    wanted |= {f"{name}_{i}_c{j}".lower() for i, (_, c) in enumerate(route) for j in range(len(c))}
    name_key = str(spec["name_key"]).lower()
    clash = [
        (e["index"], v)
        for e in m.entities()
        for k, v in e["keys"]
        if k.lower() == name_key and v.lower() in wanted
    ]
    if clash:
        raise ToolError(f"entity {clash[0][0]} is already named {clash[0][1]!r}; pick another name")
    existing = len(m.entities(classname=str(spec["point_class"]), with_keys=False))
    limit = int(spec.get("max_points") or 0)
    if limit and existing + len(route) > limit:
        raise ToolError(
            f"the map would have {existing + len(route)} path points; the game takes {limit}"
        )

    k = _kernel()
    m = k.Map.parse(m.source())
    out_points = []
    total = 0.0
    for i, (at, controls) in enumerate(route):
        pairs = [
            ("classname", str(spec["point_class"])),
            ("origin", _value_text("origin", at)),
            (str(spec["name_key"]), names[i]),
        ]
        if i + 1 < len(route):
            pairs.append((str(spec["next_key"]), names[i + 1]))
        ctl_names = []
        for j, c in enumerate(controls):
            ctl_names.append(f"{name}_{i}_c{j}")
            pairs.append((control_keys[j], ctl_names[-1]))
            m.add_entity(
                [
                    ("classname", str(spec["control_class"])),
                    ("origin", _value_text("origin", c)),
                    (str(spec["name_key"]), ctl_names[-1]),
                ]
            )
        entity = m.add_entity(pairs)
        length = _curve_length([at, *controls, route[i + 1][0]]) if i + 1 < len(route) else 0.0
        total += length
        out_points.append(
            {"name": names[i], "entity": entity, "origin": at, "controls": ctl_names}
            | ({"segment_length": round(length, 1)} if i + 1 < len(route) else {})
        )
    SESSION.map = m
    report = validate_profile()
    notes = []
    if SESSION.grid > 0 and any(c % SESSION.grid for c in route[0][0]):
        notes.append(
            f"the first point is off the {SESSION.grid}-unit grid; a vehicle put on this path "
            "turns about a brush centred there, which will be off the grid too"
        )
    return {
        "start": names[0],
        "points": out_points,
        "segments": len(route) - 1,
        "length": round(total, 1),
        "notes": notes,
        "summary": report["summary"],
        "findings": report["findings"],
        "next": "objective_place(kind=..., path=start) to put a vehicle on it; map_save to write it",
    }


@tool("map", "Place an objective", writes=True)
def objective_place(
    kind: str,
    name: str,
    track: str,
    team: str,
    objective: dict | None = None,
    trigger: dict | None = None,
    texture: str = "",
    points: dict | None = None,
    path: str = "",
    options: dict[str, str | float] | None = None,
) -> dict:
    """Build a whole objective in one call: its trigger, the objective entities, keys and script.

    The kinds are the profile's (`objectives:` in `nrc://profile/{id}`, which describes each);
    for ET: Legacy:

    - `dynamite`: the other team blows it up. `team` owns and defends it.
    - `constructible`: `team`'s engineers build it; the other team can blow it up once built.
    - `command_post`: the stock command post. Built, it shortens its team's charge times and
      reports landmines to it; destroyed, that stops. `team` is `neutral` (either team builds
      it, one at a time), `axis` or `allies` (only that team builds it).
    - `checkpoint`: a forward spawn flag both teams capture. `team` holds it, and its spawns,
      at the start.
    - `checkpoint_one_way`: the same, but only the other team can raise its flag. `team` can
      still knock that flag down, and that hands the spawns back (the engine's rule).
    - `escort`: a vehicle `team` escorts along `path` (the `start` from `path_place`). It moves
      while `team` players stand in `trigger`, stops at the next path point a couple of seconds
      after they leave, and runs `trigger arrived` on `name` at the end. Not damageable.
    - `steal`: an item the other team steals from `team` and carries into `trigger`, which
      runs `death` on `<name>_deliver`; `objective` is where it lies.

    `objective` and `trigger` are boxes `{"min": [...], "max": [...]}` or Solid IR trees, for
    the kinds that have them: the objective's own brushes (a vehicle as it stands at the path's
    first point), and the volume players stand in - to see "You are near <track>" and plant or
    build, to escort, or to deliver. Where the objective is a point entity (a stolen item),
    `objective` may also be `{"origin": [x, y, z]}`. Make the trigger enclose the objective
    with room to stand. `texture` goes on the objective's brushes.

    `points` places point entities by role, for the kinds that have them:
    `{"flag": [x, y, z], "spawns": [[x, y, z], {"origin": [x, y, z], "angle": 90}, ...]}`.
    For a checkpoint, `flag` is the foot of the flagpole, and the spawn region marker named
    `track` goes there too. Each of `spawns` gets one spawn for each team, linked to the flag:
    the holder's start active, and a capture hands them to the capturing team. Spawns are
    optional; place them later with the targetname given under `links`.

    `options` sets a kind's tunables, such as an escort's `speed` or a stolen item's `model`;
    the profile lists them with their defaults.

    The links the engine needs are made for you - triggers target what they work, a dynamite
    objective carries its weapon class, a vehicle carries the origin it turns about - and the
    script gets the objective's events, which you can extend with `script_set_event`. Nothing
    in the map reaches disk until `map_save`; the script is staged with it.
    """
    m = SESSION.require()
    pid, spec, script_path = _script_spec()
    recipes = profiles.load(pid).get("objectives")
    if not isinstance(recipes, dict) or not recipes.get("kinds"):
        raise ToolError(f"profile {pid} describes no objectives to build")
    kinds = recipes["kinds"]
    if kind not in kinds:
        raise ToolError(f"unknown objective kind {kind!r}; the profile has: {', '.join(kinds)}")
    raw = kinds[kind]
    teams = [str(t) for t in raw.get("teams") or recipes.get("teams") or []]
    if team not in teams:
        raise ToolError(f"team must be one of {', '.join(teams)} for {kind}, got {team!r}")
    try:
        mapscript._word(name, "objective name")
    except mapscript.ScriptEditError as e:
        raise ToolError(str(e)) from e
    if not track.strip() or '"' in track or "\n" in track:
        raise ToolError("track is the text shown in game: non-empty, one line, no double quotes")

    subs = {"name": name, "track": track, "team": team}
    for key, by_team in (recipes.get("team_values") or {}).items():
        if team in by_team:
            subs[key] = str(by_team[team])
    declared = raw.get("options") or {}
    unknown = sorted(set(options or {}) - set(declared))
    if unknown:
        known = ", ".join(declared) or "none"
        raise ToolError(f"{kind} has no option {unknown[0]!r}; its options: {known}")
    for opt, about in declared.items():
        value = _value_text(opt, (options or {}).get(opt, about.get("default", "")))
        pattern = about.get("pattern")
        if pattern and not re.fullmatch(str(pattern), value):
            raise ToolError(f"option {opt} {value!r} is not {about.get('description', pattern)}")
        subs[opt] = value

    seqs: dict[str, list[dict]] = {}
    anchors: dict[str, list[float]] = {}
    if raw.get("path"):
        if not path:
            raise ToolError(f"{kind} follows a path: lay one with path_place and pass its start")
        chain = _path_chain(m, _paths_spec(pid), path)
        segments = len(chain) - 1
        cap = int(raw["path"].get("max_segments") or 0) if isinstance(raw["path"], dict) else 0
        if segments < 1:
            raise ToolError(f"the path from {path!r} has one point and no segment to follow")
        if cap and segments > cap:
            raise ToolError(f"the path has {segments} segments; {kind} takes at most {cap}")
        seqs["segment"] = [{"i": str(i), "segment": chain[i][0]} for i in range(segments)]
        subs["segments"] = str(segments)
        anchors["path_start"] = chain[0][1]
    elif path:
        raise ToolError(f"{kind} does not follow a path; leave path out")

    # Only the parts that are filled in: a kind's option patterns are regexes, not templates.
    try:
        parts = [ent for ent in _fill(raw["entities"], subs, seqs) if _for_team(ent, team)]
        events = [ev for ev in _fill(raw.get("script") or [], subs, seqs) if _for_team(ev, team)]
        links = _fill(raw.get("links"), subs, seqs) if raw.get("links") else None
    except KeyError as e:
        raise ToolError(f"profile {pid}: {kind} needs a team value {e} for {team}") from e

    # Every name the recipe hands out must be free, or two objectives would answer to it.
    name_keys = ("targetname", "scriptname")
    ours = (
        {name.lower()}
        | {
            str(v).lower()
            for ent in parts
            for k, v in (ent.get("keys") or {}).items()
            if k.lower() in name_keys
        }
        | {str(ev["block"]).lower() for ev in events}
    )
    for e in m.entities():
        clash = [v for k, v in e["keys"] if k.lower() in name_keys and v.lower() in ours]
        if clash:
            raise ToolError(f"entity {e['index']} already uses the name {clash[0]!r}; pick another")

    # Where each part goes: a point role at the positions given in `points`, or at the centre
    # of the shape it names (`at`, else its own role); a brush part on its shape's brushes.
    given = dict(points or {})
    supplied = {"trigger": trigger, "objective": objective}
    point_roles = {str(ent["role"]) for ent in parts if ent.get("point")}
    if set(given) - point_roles:
        raise ToolError(
            f"{kind} places no {', '.join(sorted(set(given) - point_roles))}; its points are: "
            f"{', '.join(sorted(point_roles)) or 'none'}"
        )
    shapes: dict[str, dict] = {}
    places: dict[str, list] = {}

    def shape_for(role: str) -> dict:
        if role not in shapes:
            if supplied.get(role) is None:
                raise ToolError(f"{kind} needs `{role}`: a box {{min, max}} or a Solid IR tree")
            shapes[role] = _as_shape(supplied[role], role)
        return shapes[role]

    for ent in parts:
        role = str(ent["role"])
        where = str(ent.get("at", role))
        if ent.get("point"):
            if role in given:
                places[role] = _as_points(given[role], role)
            elif "at" in ent or supplied.get(where) is not None:
                shape_for(where)
            elif not ent.get("optional"):
                raise ToolError(f"{kind} needs points.{role}")
        else:
            shape_for(where)
    extra = [r for r, v in supplied.items() if v is not None and r not in shapes]
    if extra:
        raise ToolError(f"{kind} has no `{'`, `'.join(extra)}` brushes; leave it out")

    # The script edit is worked out first, so a refusal there changes nothing in the map.
    text = (SESSION.read_file(script_path) or b"").decode("utf-8", errors="replace")
    try:
        for ev in events:
            text, _ = mapscript.set_event(
                text,
                spec,
                ev["block"],
                ev["event"],
                list(ev.get("params") or []),
                list(ev.get("actions") or []),
            )
    except (mapscript.ScriptEditError, mapscript.ScriptSyntaxError) as e:
        raise ToolError(f"nothing changed: the script edit was refused ({e})") from e

    k = _kernel()
    # Build on a copy: a later invalid shape must not leave half an objective behind.
    m = k.Map.parse(m.source())
    made = []
    origin_brush = recipes.get("origin_brush") or {}
    for ent in parts:
        role = str(ent["role"])
        where = str(ent.get("at", role))
        keys = [(str(a), str(b)) for a, b in (ent.get("keys") or {}).items()]
        if ent.get("point"):
            if role in places:
                spots = places[role]
            elif where in shapes:
                spots = [(_centre(k, shapes[where]), None)]
            else:
                spots = []  # optional, and not given
            for origin, angle in spots:
                pairs = [("classname", ent["classname"]), ("origin", _value_text("origin", origin))]
                if angle is not None:
                    pairs.append(("angle", _value_text("angle", angle)))
                index = m.add_entity(pairs + keys)
                made.append(
                    {"role": role, "classname": ent["classname"], "entity": index, "origin": origin}
                )
            continue
        shape = shapes[where]
        if "ir" not in shape:
            raise ToolError(f"{role} is a brush entity here: give a box or a Solid IR tree")
        tex = ent.get("texture") or texture or "common/caulk"
        r = k.solid_commit(
            m,
            shape["ir"],
            {"default": tex},
            SESSION.grid,
            ent["classname"],
            False,
            f"{name}_{role}",
            True,
            None,
        )
        if ent.get("origin_at"):
            # The brushes turn and travel about this point (an origin brush).
            at, h = anchors[ent["origin_at"]], float(origin_brush.get("half_size", 8))
            k.solid_commit(
                m,
                {"op": "box", "min": [c - h for c in at], "max": [c + h for c in at]},
                {"default": str(origin_brush["texture"])},
                1,
                ent["classname"],
                False,
                f"{name}_{role}_origin",
                False,
                r["entity"],
            )
        m.set_keys(r["entity"], keys)
        made.append(
            {
                "role": role,
                "classname": ent["classname"],
                "entity": r["entity"],
                "brushes": r["brushes_created"],
            }
        )
    _write_script(script_path, text)
    SESSION.map = m

    report = validate_profile()
    out = {
        "kind": kind,
        "name": name,
        "entities": made,
        "script": str(script_path),
        "summary": report["summary"],
        "findings": report["findings"],
        "next": "map_save to write the map; script_set_event to add to the objective's events",
    }
    if links:
        out["links"] = links
    return out


@tool("map", "Game profile summary")
def profile_summary(profile_id: str | None = None) -> dict:
    """What the active game profile knows, and how much of it is verified.

    The profile is the only game-specific layer (§7.4). Anything marked `unverified` came
    from documentation rather than a shipped gamepack and must not be treated as a hard
    rule.
    """
    pid = profile_id or active_profile()
    if not pid:
        return {
            "error": "no profile selected; set NRC_PROFILE or pass profile_id",
            "available": profiles.available(),
        }
    try:
        return profiles.summary(pid)
    except profiles.ProfileError as e:
        raise ToolError(_profile_error_text(e)) from e
