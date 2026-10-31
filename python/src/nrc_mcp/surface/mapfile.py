"""Opening, reading, validating and writing the `.map` file.

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import analysis as anamod
from .. import mapscript, profiles, rules
from .. import playspace as playspacemod
from ..kernel import load_map, repo_root
from ..runtime import (
    SESSION,
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
