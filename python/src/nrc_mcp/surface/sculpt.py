"""Authoring geometry through the Solid IR (§4).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from .. import solids
from ..runtime import (
    SESSION,
    _kernel,
    _require_path,
    tool,
)

# `solid_preview` renders what it compiled, so the sculpt surface borrows the render
# surface's annotated-image helper. One direction only: render never imports sculpt.
from .render import _image_and_notes


@tool("sculpt", "Solid IR reference")
def solid_help() -> str:
    """The Solid IR reference: every operator, its fields, and the on-grid caveat.

    Read this before authoring geometry. It is short.
    """
    return solids.IR_HELP


@tool("sculpt", "Compile Solid IR")
def solid_compile(ir: dict, textures: dict | None = None, grid: int | None = None) -> dict:
    """Compile a Solid IR tree and report what it would produce, touching nothing.

    Use this first. It returns brush and face counts, bounds, minimum thickness, off-grid
    vertex count and any warnings, so a shape can be checked before it is drawn or committed.

    Errors name the failing node by path (e.g. `subtract/cut[0]:box`), because a nested tree is
    otherwise very hard to debug from the outside. Call `solid_help` for the operator list.
    """
    k = _kernel()
    return k.solid_compile(ir, textures, grid if grid is not None else SESSION.grid)


@tool("sculpt", "Preview Solid IR")
def solid_preview(
    ir: dict,
    textures: dict | None = None,
    grid: int | None = None,
    view: str = "sheet",
    width: int = 1100,
    height: int = 850,
) -> list:
    """Compile a Solid IR tree and render it **without committing**.

    The sculpting loop: author IR, preview, adjust, commit. Nothing touches the open map, so
    this is free to call repeatedly.

    If a map is open the geometry is previewed in place against it, so a new piece can be seen
    in context rather than floating in isolation.
    """
    k = _kernel()
    g = grid if grid is not None else SESSION.grid
    # Preview against a *copy*, so an unwanted shape never has to be undone.
    base = SESSION.map.source() if SESSION.map is not None else '{\n"classname" "worldspawn"\n}\n'
    scratch = k.Map.parse(base)
    info = k.solid_commit(scratch, ir, textures, g, "worldspawn", False, "preview")
    png, ann = scratch.render(
        view=view,
        overlay="structural",
        width=width,
        height=height,
        grid=g,
        grid_spacing=64.0,
        hide_invisible=False,
    )
    extra = (
        f"preview only — nothing was committed. {info['brushes_created']} brush(es), "
        f"{info['faces']} faces"
    )
    if info["warnings"]:
        extra += "\nwarnings: " + "; ".join(info["warnings"][:4])
    return _image_and_notes(png, ann, extra)


@tool("sculpt", "Commit Solid IR to map", writes=True)
def solid_commit(
    ir: dict,
    label: str,
    textures: dict | None = None,
    grid: int | None = None,
    target_classname: str = "worldspawn",
    remember: bool = True,
) -> dict:
    """Compile a Solid IR tree and add the brushes to the open map.

    `textures` picks shaders per face direction and can make the brushes detail; `solid_help`
    lists its keys (`default`, `top`, `bottom`, `north`/`south`/`east`/`west`, `scale`,
    `offset`, `rotate`, `detail`).

    Nothing is written to disk: call `map_save` for that. The brushes are tagged with a comment
    naming `label`, so a human reading the `.map` can see where they came from and delete them
    as a unit.

    With `remember`, the IR is recorded in a `<map>.solids.json` sidecar under `label`, so the
    shape can be re-parameterized later with `solid_edit_param` — "make that corridor 32 units
    wider" instead of moving faces by hand. The `.map` stays canonical; the sidecar is
    advisory and re-derivable.
    """
    m = SESSION.require()
    k = _kernel()
    g = grid if grid is not None else SESSION.grid
    result = k.solid_commit(m, ir, textures, g, target_classname, False, label)

    if remember and SESSION.path is not None:
        try:
            solids.put(SESSION.path, label, ir, brushes=result["brushes_created"])
            result["sidecar"] = str(solids.sidecar_path(SESSION.path))
        except solids.SolidStoreError as e:
            # The brushes are already in the map; failing to record the IR is a lesser problem
            # and must not be reported as though the commit failed.
            result["sidecar_error"] = str(e)
    result["next"] = "call map_save to write the map, or render_contact_sheet to look at it"
    return result


@tool("sculpt", "Mark brushes detail", writes=True, idempotent=True)
def brush_set_detail(brushes: list, detail: bool = True) -> dict:
    """Mark existing brushes detail (or structural again with `detail=false`).

    A detail brush still blocks players and shots but no longer splits the vis tree, which is
    the biggest lever on vis cost: trim, pillars, props and anything that does not seal the map
    should be detail. `brushes` is a list of `[entity, primitive]` pairs, or the findings
    `structural_audit` returns (each carries its entity and primitive), passed straight in.

    Every brush is checked before any is changed; a patch or a missing primitive refuses the
    whole call. Nothing reaches disk until `map_save`. New geometry can be made detail as it is
    built: `solid_commit(textures={"detail": true, ...})`.
    """
    m = SESSION.require()
    pairs: list[tuple[int, int]] = []
    for item in brushes:
        if isinstance(item, dict):
            src = item.get("detail") if isinstance(item.get("detail"), dict) else item
            ent, prim = src.get("entity"), src.get("primitive")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            ent, prim = item
        else:
            raise ToolError(
                f"each brush is [entity, primitive] or a structural_audit finding: {item!r}"
            )
        if not isinstance(ent, int) or not isinstance(prim, int):
            raise ToolError(f"entity and primitive must be whole numbers: {item!r}")
        pairs.append((ent, prim))
    if not pairs:
        raise ToolError("no brushes given")
    try:
        changed = m.set_brush_detail(pairs, detail)
    except ValueError as e:
        raise ToolError(f"nothing changed: {e}") from e
    return {
        "detail": detail,
        "brushes": changed,
        "already": sum(1 for c in changed if c["was_detail"] == detail),
        "next": "map_save to write it; compile_ab to measure the vis difference",
    }


@tool("sculpt", "Inspect a solid")
def solid_inspect(name: str | None = None, ir: dict | None = None) -> dict:
    """Show a Solid IR tree's structure — either a recorded one by `name`, or one passed in.

    The outline names every operator and its parameters with the paths `solid_edit_param`
    accepts, which is how you find out what there is to change.
    """
    if ir is None:
        if name is None:
            raise ToolError(
                "pass either a recorded name or an ir tree. solid_list() shows what is recorded."
            )
        if SESSION.path is None:
            raise ToolError("no map is open, so there is no sidecar to read.")
        try:
            entry = solids.get(SESSION.path, name)
        except solids.SolidStoreError as e:
            raise ToolError(str(e)) from e
        ir = entry["ir"]
        meta = {k: v for k, v in entry.items() if k != "ir"}
    else:
        meta = {}
    return {"name": name, "outline": solids.describe(ir), "ir": ir, "recorded": meta}


@tool("sculpt", "List recorded solids")
def solid_list() -> dict:
    """Solid IR trees recorded for the open map, with their brush counts."""
    _require_path()
    try:
        store = solids.load(SESSION.path)
    except solids.SolidStoreError as e:
        raise ToolError(str(e)) from e
    return {
        "sidecar": str(solids.sidecar_path(SESSION.path)),
        "solids": {
            n: {k: v for k, v in e.items() if k not in ("ir", "superseded")}
            for n, e in sorted(store["solids"].items())
        },
    }


def _recompile_param(name: str, path: str, value: Any) -> tuple[Path, dict, dict, dict]:
    """The shared half of the two param tools: edit the tree and compile both versions."""
    sidecar = _require_path()
    try:
        entry = solids.get(sidecar, name)
        edited = solids.edit_param(entry["ir"], path, value)
    except solids.SolidStoreError as e:
        raise ToolError(str(e)) from e

    k = _kernel()
    try:
        before = k.solid_compile(entry["ir"], None, SESSION.grid)
        after = k.solid_compile(edited, None, SESSION.grid)
    except ValueError as e:
        # The parameter was structurally valid and still produced impossible geometry, so
        # the tree comes back in the message — it is what the caller needs to adjust.
        raise ToolError(
            f"the edited tree does not compile: {e}. The parameter was accepted but "
            f"produces invalid geometry; try another value. Edited tree: {edited}"
        ) from e

    keep = ("brushes", "faces", "bounds", "volume")
    return (
        sidecar,
        edited,
        {kk: before[kk] for kk in keep},
        {kk: after[kk] for kk in keep},
    )


@tool("sculpt", "Preview a parameter change")
def solid_param_preview(name: str, path: str, value: Any) -> dict:
    """Show what changing one parameter of a recorded solid *would* produce.

    This is the point of keeping the IR: `solid_param_preview("corridor", "max[1]", 192)`
    widens a corridor, where editing brushes by hand would mean moving several faces
    consistently.

    `path` is dotted with bracket indices — `from.solid.max[0]`, `cut[0].min`. Run
    `solid_inspect` to see what paths exist.

    Nothing is recorded and nothing is drawn. `solid_edit_param` is the one that writes.
    """
    _, edited, before, after = _recompile_param(name, path, value)
    return {
        "name": name,
        "path": path,
        "value": value,
        "before": before,
        "after": after,
        "ir": edited,
        "recorded": False,
    }


@tool("sculpt", "Record a parameter change", writes=True)
def solid_edit_param(name: str, path: str, value: Any) -> dict:
    """Change one parameter of a recorded solid and **record** the edited tree.

    `solid_param_preview` takes the same arguments and reports the same numbers without
    writing; call that first.

    This writes the edited tree to the `<map>.solids.json` sidecar. The old brushes are
    **not** removed automatically — that would mean guessing which brushes came from this
    solid, and guessing wrong would delete a mapper's work. Delete them yourself, then call
    `solid_commit` to draw the new ones.
    """
    sidecar, edited, before, after = _recompile_param(name, path, value)
    solids.put(sidecar, name, edited, brushes=after["brushes"], notes=f"edited {path}")
    return {
        "name": name,
        "path": path,
        "value": value,
        "before": before,
        "after": after,
        "ir": edited,
        "recorded": True,
        "next": (
            "the sidecar now holds the edited tree; delete the old brushes, then call "
            "solid_commit to draw the new ones"
        ),
    }
