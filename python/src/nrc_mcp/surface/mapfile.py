"""Opening, reading, validating and writing the `.map` file.

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import contextlib
import math
from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import analysis as anamod
from .. import mapscript, profiles, rules
from .. import playspace as playspacemod
from ..kernel import load_map, repo_root
from ..runtime import (
    SESSION,
    _add_entity,
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
    m = load_map(p)

    SESSION.map = m
    SESSION.path = p
    SESSION.grid = grid
    SESSION.warnings = []

    rt = m.round_trip()
    SESSION.opened_round_trip = rt
    if not rt["identical"]:
        SESSION.warnings.append(
            "this map does not round-trip byte-identically; map_save will refuse to write"
        )

    stats = m.stats(grid=grid)
    return {
        "path": str(p),
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

    Two gates, guarding two different things.

    **The file.** If the kernel could not reproduce the bytes it *loaded*, it misunderstands
    something in this file and writing would silently discard it. That check is taken at
    `map_open` and it is the one enforced here. A difference introduced since then is your
    edit, not a kernel failure, and does not block the write.

    **The map.** The write is compared against the file it is about to replace, and refused
    if it destroys playable space — an interior floor sealed off, or a surface you can now
    stand on top of a clip volume. Both are silent in every other check: the map still
    compiles, still seals, still validates, and is unplayable. See `playable_space_diff` for
    the same report on its own.

    The comparison costs a second or two on a city-sized map. If it cannot be made at all —
    a map too large to voxelize, or one with no evaluable brush — the write proceeds and
    says so, because a check that cannot run has not found anything.

    Args:
        path: destination. Defaults to the file the map was opened from.
        allow_non_identical: write even though the map did not round-trip when it was opened.
        check_playable_space: run the before/after comparison. Off skips it entirely.
        acknowledge_regression: write anyway despite a playable-space error. Use it when you
            meant to remove that space.
    """
    m = SESSION.require()
    opened = SESSION.opened_round_trip or m.round_trip()
    current = m.round_trip()
    edited = not current["identical"] and bool(opened["identical"])

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

    written = m.save(path)
    out: dict[str, Any] = {
        "written": True,
        "path": written,
        "round_trip": current,
        "modified_since_open": edited,
    }
    if playspace_report is not None:
        out["playable_space"] = playspacemod.brief(playspace_report)
    if playspace_note:
        out["playable_space_note"] = playspace_note
    return out


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
    `entity_set_keys`. Nothing reaches disk until `map_save`.
    """
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
    pairs += [(k, _value_text(k, v)) for k, v in (keys or {}).items()]

    index = _add_entity(pairs)

    g = grid if grid is not None else SESSION.grid
    notes = _profile_notes(classname, pairs)
    if g > 0 and any(float(c) % g for c in origin):
        notes.append(
            f"origin {pairs[1][1]} is off the {g}-unit grid; it was written as given, not snapped"
        )
    return {
        "entity": index,
        "keys": pairs,
        "notes": notes,
        "entities_now": SESSION.map.entity_count,
        "next": "call map_save to write it, and validate_profile to check spawns and counts",
    }


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
    """
    m = SESSION.require()
    return m.validate(
        grid=grid if grid is not None else SESSION.grid,
        severity_min=severity_min,
    )


@tool("map", "Validate against game profile")
def validate_profile(profile_id: str | None = None, severity_min: str = "info") -> dict:
    """Validate the open map's entities against the active game profile.

    Complements `validate`, which covers game-agnostic geometry. This checks the game's own
    rules: spawn arrangement, objective counts, gametype keys — and, for a game whose maps
    carry a script, that script against the map's entities.

    Every rule is data in the profile, and every finding carries a `confidence`. **An
    unverified rule can never produce an error** — it is downgraded to `info` — because three
    of the four spawn rules the design document called verified were wrong, and one would have
    failed correct maps. `nrc://corrections` has the details.
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
        found += mapscript.check(pid, ents, SESSION.path)
    except profiles.ProfileError as e:
        raise ToolError(_profile_error_text(e)) from e

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
