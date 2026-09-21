"""Turning geometry into a picture the agent can actually look at (§4.2).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

from mcp.server.mcpserver import Image as _Image

from .. import profiles
from ..runtime import (
    SESSION,
    active_profile,
    tool,
)


def _image_and_notes(png: bytes, ann: dict, extra: str = "") -> list:
    """Return an image plus its numbers.

    §4.2 wants renders annotated with what the agent cannot see. The spatial parts are drawn;
    the counts and dimensions come back as text so they can be read exactly rather than
    inferred from pixels.
    """
    lines = [f"{ann['view']} view, {ann['width']}x{ann['height']}, overlay {ann['overlay']}"]
    b = ann.get("bounds")
    if b:
        lines.append(f"bounds {b['min']} .. {b['max']}  (size {b['size']})")
    else:
        lines.append("no geometry was drawn")
    c = ann["counts"]
    lines.append(
        f"structural {c['structural_brushes']}, detail {c['detail_brushes']}, "
        f"brush entities {c['brush_entities']}, patches {c['patches']}, "
        f"surfaces drawn {c['facets']} ({c['invisible_facets']} not rendered in game)"
    )
    if ann.get("units_per_pixel"):
        lines.append(f"scale: {ann['units_per_pixel']:.3g} units per pixel")
    if ann.get("camera_eye"):
        lines.append(f"camera at {ann['camera_eye']}")
    if ann["off_grid_vertices"]:
        lines.append(f"{ann['off_grid_vertices']} vertices off a grid of {ann['grid']}")
    if ann["skipped_brushes"]:
        lines.append(
            f"WARNING: {ann['skipped_brushes']} brush(es) not drawn: "
            + "; ".join(ann["skipped_examples"])
        )
    lines += ann.get("notes", [])
    if extra:
        lines.append(extra)
    return [_Image(data=png, format="png"), "\n".join(lines)]


@tool("render", "Render top-down view")
def render_topdown(
    overlay: str = "shaded",
    width: int = 1000,
    height: int = 800,
    grid: int | None = None,
    grid_spacing: float = 64.0,
    solid: bool = False,
) -> list:
    """Render a top-down (XY) view of the open map.

    Orthographic views render as backface-culled wireframe, which gives a readable
    architectural floor plan. A *solid* top-down of a sealed map shows only the underside of
    its sky brush — one flat rectangle — so pass `solid=True` only when you know the geometry
    is open.

    Args:
        overlay: `shaded`, `structural` (colour by structural/detail/entity/patch),
            `caulk` (highlight surfaces not drawn in game), or `off_grid` (mark bad vertices).
        grid: grid that off-grid vertices are measured against. Defaults to the session grid.
        grid_spacing: world-unit spacing of the drawn grid; 0 disables it.
        solid: fill faces instead of drawing wireframe.
    """
    m = SESSION.require()
    png, ann = m.render(
        view="top",
        overlay=overlay,
        width=width,
        height=height,
        grid=grid if grid is not None else SESSION.grid,
        grid_spacing=grid_spacing,
        wireframe=False if solid else None,
    )
    return _image_and_notes(png, ann)


@tool("render", "Render camera view")
def render_camera(
    eye: list[float] | None = None,
    target: list[float] | None = None,
    fov_deg: float = 55.0,
    overlay: str = "shaded",
    width: int = 1000,
    height: int = 800,
    hide_invisible: bool = True,
    view: str = "perspective",
) -> list:
    """Render a perspective view, or one of the front/side orthographic views.

    Args:
        eye: camera position. Omit for an automatic three-quarter framing of the whole map.
        target: what to look at. Defaults to the centre of the geometry.
        view: `perspective`, `front` or `side`.
        hide_invisible: skip caulk, nodraw, clip and trigger surfaces. On by default because a
            sealed map is wrapped in such a shell, and a perspective view of the shell shows
            nothing useful.
    """
    m = SESSION.require()
    if view not in ("perspective", "front", "side", "top"):
        raise ValueError(f"view must be perspective, front, side or top — got {view!r}")
    png, ann = m.render(
        view=view,
        overlay=overlay,
        width=width,
        height=height,
        grid=SESSION.grid,
        grid_spacing=64.0,
        hide_invisible=hide_invisible,
        eye=list(eye) if eye else None,
        target=list(target) if target else None,
        fov_deg=fov_deg,
    )
    return _image_and_notes(png, ann)


@tool("render", "Render contact sheet")
def render_contact_sheet(
    overlay: str = "shaded",
    width: int = 1200,
    height: int = 900,
    hide_invisible: bool = True,
    grid_spacing: float = 64.0,
) -> list:
    """Render three orthographic views plus a perspective view in one image.

    This is the default way to look at geometry: one call, one image, and a shape that three
    orthographic views plus a perspective view make unambiguous. Panels clockwise from
    top-left are top (XY), front (XZ), side (YZ), perspective.
    """
    m = SESSION.require()
    png, ann = m.render(
        view="sheet",
        overlay=overlay,
        width=width,
        height=height,
        grid=SESSION.grid,
        grid_spacing=grid_spacing,
        hide_invisible=hide_invisible,
    )
    return _image_and_notes(png, ann)


@tool("render", "Render player eye view")
def render_player_eye(
    position: list[float],
    yaw_deg: float = 0.0,
    fov_deg: float = 90.0,
    width: int = 900,
    height: int = 700,
    eye_height: float | None = None,
) -> list:
    """Render the view a standing player would have from a floor position.

    Args:
        position: floor point to stand on, `[x, y, z]`.
        yaw_deg: facing, degrees counter-clockwise from +X.
        eye_height: height above `position`. Omit to read the verified standing height from
            the active game profile, which is where it belongs — the design document's assumed
            figure was wrong for this game by 13 units.

    Position `z` should be the floor, not the eye. If the result looks empty, the point is
    probably inside solid geometry or outside the map.
    """
    m = SESSION.require()
    if eye_height is None:
        pid = active_profile()
        eye_height = profiles.standing_height(pid) if pid else None
        if eye_height is None:
            raise ValueError(
                "no eye_height given and the active profile states no verified standing "
                "height. Pass eye_height explicitly, or check profile_summary(). Guessing a "
                "player height here would put the camera where a player cannot stand."
            )
        source = f"eye height {eye_height} from profile {pid} (verified)"
    else:
        source = f"eye height {eye_height} supplied by caller"

    png, ann = m.render(
        view="player_eye",
        overlay="shaded",
        width=width,
        height=height,
        grid=SESSION.grid,
        grid_spacing=0.0,
        eye=list(position),
        yaw_deg=yaw_deg,
        eye_height=eye_height,
        fov_deg=fov_deg,
        hide_invisible=True,
    )
    return _image_and_notes(png, ann, source)
