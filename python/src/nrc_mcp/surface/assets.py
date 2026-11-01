"""The brush/patch/mesh decision and the Blender handoff (§5).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import blender as blendermod
from .. import profiles
from ..kernel import repo_root
from ..runtime import (
    SESSION,
    _add_entity,
    _require_profile,
    active_profile,
    tool,
)


@tool("assets", "Choose a representation")
def asset_plan(
    blocks_movement: bool,
    blocks_visibility: bool,
    is_axis_aligned: bool,
    is_curved: bool,
    needs_clean_collision: bool = False,
    is_organic: bool = False,
) -> dict:
    """Decide whether a feature should be a brush, a patch or a mesh.

    The decision rule, in order: *does it block movement, block vis, or need cheap clean
    collision?* → brush. *Is it axis-aligned architecture?* → brush. *Is it curved but simple?*
    → patch. *Everything else* → Blender.

    That ordering is not arbitrary. Brushes are the only representation that seals a map and
    blocks visibility, patches are never structural, and meshes are always non-structural — so
    anything load-bearing has to be a brush regardless of how it looks.
    """
    if blocks_visibility:
        return _tier(
            1,
            "brush, structural, caulked",
            "it blocks visibility, and only a structural brush can do that",
            grid=16,
        )
    if blocks_movement or needs_clean_collision:
        return _tier(
            2,
            "brush, detail",
            "it blocks movement or needs cheap predictable collision, which a brush "
            "gives and a mesh does not",
            grid=8,
        )
    if is_axis_aligned and not is_curved:
        return _tier(
            2, "brush, detail", "axis-aligned architecture is cheaper and tidier as a brush", grid=8
        )
    if is_curved and not is_organic:
        return _tier(3, "patch", "curved but simple, so a patch — and a patch is never structural")
    # The model entity's classname is game-specific, so it comes from the profile. The seam lint
    # caught this line naming it directly, which is exactly the drift §7.4 predicts.
    entity = "a model entity"
    pid = active_profile()
    if pid:
        with contextlib.suppress(profiles.ProfileError):
            entity = str((profiles.load(pid).get("assets") or {}).get("model_entity") or entity)
    return _tier(
        4,
        f"mesh, placed with {entity}",
        "ornament, clutter or organic geometry, which is what Blender is for; always "
        "non-structural, so pair it with an explicit collision decision",
    )


def _tier(tier: int, representation: str, why: str, grid: int | None = None) -> dict:
    out = {
        "tier": tier,
        "representation": representation,
        "why": why,
        "next": {
            1: "author it with solid_commit; texture hidden faces with the caulk shader",
            2: "author it with solid_commit and set detail=true in textures",
            3: "patches are not yet authorable by this tool — build it in the editor for now",
            4: "call blender_brief, send the prompt to Blender, then model_import",
        }[tier],
    }
    if grid:
        out["minimum_grid"] = grid
    return out


@tool("assets", "Write a Blender brief")
def blender_brief(
    asset_id: str,
    purpose: str,
    bounds: dict,
    collision: str = "brush_hull",
    materials: list[dict] | None = None,
    triangles: int | None = None,
    silhouette_notes: str = "",
    profile_id: str | None = None,
) -> dict:
    """Emit a numerically complete asset brief plus a ready-to-send Blender prompt.

    `bounds` is `{"x": [lo, hi], "y": [...], "z": [...]}` in world units — normally the brush
    volume the asset replaces.

    The point is that the returned `prompt` leaves nothing to infer: dimensions, origin, budget,
    material names, UV density, export axes and the collision decision are all decided here. A
    brief that says "make it crate-sized" gets a crate of the wrong size.

    The unit scale comes from the profile, not from this code — §7.4 names that constant
    specifically as a place game specifics leak in.
    """
    pid = _require_profile(profile_id)
    try:
        return blendermod.blender_brief(
            pid,
            asset_id,
            purpose,
            bounds,
            collision=collision,
            materials=materials,
            triangles=triangles,
            silhouette_notes=silhouette_notes,
        )
    except (blendermod.AssetError, profiles.ProfileError) as e:
        raise ToolError(str(e)) from e


@tool("assets", "Validate an exported mesh")
def model_import(path: str, brief: dict, profile_id: str | None = None) -> dict:
    """Validate an exported mesh against the brief that asked for it.

    Checks scale, fit, origin, triangle budget, material names, UVs and structure. The scale
    check comes first and names the likely cause: a mesh 1/39.37 of the requested size is the
    metres-exported-as-units mistake, and saying so is far more useful than reporting the raw
    numbers.
    """
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return blendermod.model_import(p, brief, profile_id or active_profile() or None)
    except blendermod.AssetError as e:
        raise ToolError(str(e)) from e


def _model_place_keys(
    model_path: str,
    origin: list[float],
    angles: list[float] | None,
    scale: float | None,
    lightmap_scale: float | None,
    remap: dict | None,
    profile_id: str | None,
) -> list:
    pid = _require_profile(profile_id)
    try:
        return blendermod.model_place_keys(
            pid,
            model_path,
            origin,
            angles=angles,
            scale=scale,
            lightmap_scale=lightmap_scale,
            remap=remap,
        )
    except (blendermod.AssetError, profiles.ProfileError) as e:
        raise ToolError(str(e)) from e


@tool("assets", "Plan a model placement")
def model_place_plan(
    model_path: str,
    origin: list[float],
    angles: list[float] | None = None,
    scale: float | None = None,
    lightmap_scale: float | None = None,
    remap: dict | None = None,
    profile_id: str | None = None,
) -> dict:
    """Build the entity that *would* place a model, without touching the map.

    Every key name comes from the profile: the model entity's classname, the scale keys, the
    remap prefix. Negative scale is supported by this compiler and is the cheapest way to mirror
    a prop.

    Check the keys here, then call `model_place` to add them. This tool changes nothing.
    """
    keys = _model_place_keys(model_path, origin, angles, scale, lightmap_scale, remap, profile_id)
    return {"keys": keys, "committed": False, "next": "model_place adds this to the open map"}


@tool("assets", "Place a model in the map", writes=True)
def model_place(
    model_path: str,
    origin: list[float],
    angles: list[float] | None = None,
    scale: float | None = None,
    lightmap_scale: float | None = None,
    remap: dict | None = None,
    profile_id: str | None = None,
) -> dict:
    """Add a model-placing entity to the open map.

    This **modifies the open map**. `model_place_plan` takes the same arguments and returns
    the keys without touching anything, which is the one to call while you are still deciding.

    Nothing reaches disk until `map_save`.
    """
    keys = _model_place_keys(model_path, origin, angles, scale, lightmap_scale, remap, profile_id)
    index = _add_entity(keys)
    return {
        "keys": keys,
        "committed": True,
        "entity": index,
        "entities_now": SESSION.map.entity_count,
        "next": "call map_save to write it, and model_make_clip if it needs collision",
    }


@tool("assets", "Fit a collision hull")
def model_make_clip(
    path: str,
    origin: list[float] | None = None,
    scale: float = 1.0,
    k: int = 14,
    grid: int = 1,
    kind: str = "player",
    profile_id: str | None = None,
) -> dict:
    """Fit a convex collision hull to a mesh, returned as Solid IR.

    The hull is a k-DOP: the tightest intersection of half-spaces with `k` fixed normals that
    contains every vertex, pushed outward to the grid so it never cuts into the visual. That is a
    deliberate choice, not a shortcut — §5.4 argues that for a competitive shooter snappy,
    predictable collision beats accurate collision, and a 14-plane hull is something a player can
    read while sliding along it.

    `kind` selects the clip shader from the profile: `player` for movement only, `weapon` to stop
    bullets where the visual has gaps, `both` for solid.

    Returns IR rather than brushes, so preview it with `solid_preview` and look at it against the
    model before committing. A hull noticeably larger than the visual is what players report as
    an invisible wall.
    """
    pid = _require_profile(profile_id)
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return blendermod.model_make_clip(
            p, pid, origin=origin, scale=scale, k=k, grid=grid, kind=kind
        )
    except (blendermod.AssetError, profiles.ProfileError) as e:
        raise ToolError(str(e)) from e
