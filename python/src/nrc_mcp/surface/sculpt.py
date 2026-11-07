"""Authoring geometry through the Solid IR (§4).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import hashlib
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


def _store() -> dict:
    path = solids.sidecar_path(_require_path())
    try:
        return solids.decode(SESSION.read_file(path), path)
    except solids.SolidStoreError as e:
        raise ToolError(str(e)) from e


def _entry(name: str) -> tuple[dict, dict]:
    store = _store()
    entry = store["solids"].get(name)
    if not isinstance(entry, dict):
        raise ToolError(f"no solid named {name!r}; recorded: {sorted(store['solids'])}")
    return store, entry


def _generated(map_, name: str) -> list[tuple[int, int, str]]:
    marker = f"// nrc-mcp solid:{name}"
    return [row for row in map_.brush_sources() if marker in row[2].splitlines()]


def _fingerprints(brushes: list[tuple[int, int, str]]) -> list[str]:
    return sorted(hashlib.sha256(source.encode()).hexdigest() for _, _, source in brushes)


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
    new_entity: bool = False,
    target_entity: int | None = None,
) -> dict:
    """Compile a Solid IR tree and add the brushes to the open map.

    The brushes go to the worldspawn by default. Any other `target_classname` - a door, a
    trigger, an objective - makes a new entity of that class on every call, so a second door
    is never merged into the first; to add brushes to an entity that already exists, pass its
    index as `target_entity`. The result's `entity` says which (`created_entity` when it is
    new); give it keys with `entity_set_keys`. `new_entity` is accepted and changes nothing.

    `textures` picks shaders per face direction and can make the brushes detail; `solid_help`
    lists its keys (`default`, `top`, `bottom`, `north`/`south`/`east`/`west`, `scale`,
    `offset`, `rotate`, `detail`).

    Nothing is written to disk: call `map_save` for that. The brushes are tagged with a comment
    naming `label`, so a human reading the `.map` can see where they came from and delete them
    as a unit.

    With `remember`, the IR is staged in a `<map>.solids.json` sidecar under `label`, so the
    shape can be re-parameterized later with `solid_edit_param` — "make that corridor 32 units
    wider" instead of moving faces by hand. The `.map` stays canonical; the sidecar is
    advisory and re-derivable.

    To commit several solids, use `solids_commit`: one call, all or none.
    """
    m = SESSION.require()
    k = _kernel()
    g = grid if grid is not None else SESSION.grid
    store = _store() if remember else None
    if store is not None:
        if label in store["solids"] or _generated(m, label):
            raise ToolError(f"solid {label!r} already exists; use solid_edit_param or a new label")
        try:
            solids.record({"solids": {}}, label, ir)  # Validate before any mutation.
        except solids.SolidStoreError as e:
            raise ToolError(str(e)) from e
    candidate = m.copy()
    try:
        result = k.solid_commit(
            candidate, ir, textures, g, target_classname, False, label, new_entity, target_entity
        )
    except ValueError as e:
        raise ToolError(str(e)) from e

    if store is not None:
        solids.record(
            store,
            label,
            ir,
            brushes=result["brushes_created"],
            textures=textures,
            grid=g,
            provenance=_fingerprints(_generated(candidate, label)),
        )
        sidecar = solids.sidecar_path(SESSION.path)
        SESSION.stage_file(sidecar, solids.encode(store))
        result["sidecar"] = str(sidecar)
        result["staged"] = True
    SESSION.map = candidate
    result["next"] = "call map_save to write the map, or render_contact_sheet to look at it"
    return result


#: The fields one `solids_commit` item may carry: `solid_commit`'s own, minus the shared ones.
_ITEM_FIELDS = {"ir", "label", "textures", "target_classname", "new_entity", "target_entity"}

#: The sidecar module, under a name `solids_commit`'s `solids` argument does not shadow.
_sidecar = solids


@tool("sculpt", "Commit several solids to map", writes=True)
def solids_commit(
    solids: list[dict[str, Any]],
    textures: dict | None = None,
    grid: int | None = None,
    remember: bool = True,
) -> dict:
    """Commit many Solid IR trees in one call, all or none: crates, pillars, a row of rooms.

    Each item is what `solid_commit` takes for one solid: `{"ir": ..., "label": ...}` plus,
    optionally, `textures`, `target_classname`, `new_entity` and `target_entity`. The
    top-level `textures` applies to every item that gives none of its own.

    Items are compiled in order onto a copy of the map, and the copy replaces the map only
    if every one succeeds: a refusal names the item (`solids[2] (label 'crate_2'): ...`) and
    changes nothing, sidecar included. Labels must be new and distinct. With `remember`, each
    tree is staged in the sidecar under its label, so `solid_edit_param` can re-shape any of
    them later.

    The reply is compact: per item its label, brush count and entity, plus totals, and any
    compiler warnings tagged with their label. Nothing reaches disk until `map_save`.
    """
    m = SESSION.require()
    k = _kernel()
    g = grid if grid is not None else SESSION.grid
    if not solids:
        raise ToolError("no solids given; pass a list of {ir, label, ...}")
    store = _store() if remember else None
    seen: set[str] = set()
    for i, item in enumerate(solids):
        where = f"solids[{i}]"
        if not isinstance(item, dict):
            raise ToolError(f"{where}: each item is an object {{ir, label, ...}}")
        extra = sorted(set(item) - _ITEM_FIELDS)
        if extra:
            raise ToolError(
                f"{where}: unknown field(s) {extra}; an item takes {sorted(_ITEM_FIELDS)}"
            )
        label = item.get("label")
        if not isinstance(label, str) or not label:
            raise ToolError(f"{where}: label must be a non-empty string")
        if not isinstance(item.get("ir"), dict):
            raise ToolError(f"{where} (label {label!r}): ir must be a Solid IR tree")
        if label in seen:
            raise ToolError(f"{where}: label {label!r} is used twice in this call")
        seen.add(label)
        if store is not None:
            if label in store["solids"] or _generated(m, label):
                raise ToolError(
                    f"{where}: solid {label!r} already exists; use solid_edit_param or a new label"
                )
            try:
                _sidecar.record({"solids": {}}, label, item["ir"])
            except _sidecar.SolidStoreError as e:
                raise ToolError(f"{where}: {e}") from e

    candidate = m.copy()
    made: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    totals = {"brushes_created": 0, "faces": 0, "off_grid_vertices": 0}
    result: dict[str, Any] = {}
    for i, item in enumerate(solids):
        label = item["label"]
        tex = item.get("textures", textures)
        try:
            result = k.solid_commit(
                candidate,
                item["ir"],
                tex,
                g,
                item.get("target_classname", "worldspawn"),
                False,
                label,
                bool(item.get("new_entity", False)),
                item.get("target_entity"),
            )
        except ValueError as e:
            raise ToolError(f"solids[{i}] (label {label!r}): {e}; nothing was committed") from e
        for key in totals:
            totals[key] += int(result.get(key) or 0)
        warnings += [{"label": label, "warning": w} for w in result.get("warnings") or []]
        made.append(
            {"label": label, "brushes": result["brushes_created"], "entity": result["entity"]}
        )
        if store is not None:
            _sidecar.record(
                store,
                label,
                item["ir"],
                brushes=result["brushes_created"],
                textures=tex,
                grid=g,
                provenance=_fingerprints(_generated(candidate, label)),
            )

    out: dict[str, Any] = {"committed": len(made), "solids": made, **totals}
    if warnings:
        out["warnings"] = warnings
    out["map_brushes_now"] = result.get("map_brushes_now")
    if store is not None:
        sidecar = _sidecar.sidecar_path(SESSION.path)
        SESSION.stage_file(sidecar, _sidecar.encode(store))
        out["sidecar"] = str(sidecar)
    SESSION.map = candidate
    out["next"] = "call map_save to write the map, or render_contact_sheet to look at it"
    return out


@tool("sculpt", "Add a curved surface", writes=True)
def patch_commit(
    kind: str,
    centre: list[float],
    radius: float,
    length: float,
    shader: str,
    axis: str = "z",
    facing: str | None = None,
    start_deg: float = 0.0,
    target_classname: str = "worldspawn",
    new_entity: bool = False,
    target_entity: int | None = None,
    label: str | None = None,
) -> dict:
    """Add a curved surface - brushes cannot curve, so the engine draws curves from patches.

    Kinds:
      - `cylinder`: a column or pipe along `axis` (`x`, `y`, or default `z`). `centre` is
        the circle's centre at the starting end; `length` extends along the positive axis.
      - `arch`: a half-round vault spanning a doorway or corridor, running along `axis`
        (`x` or `y`). `centre` is the middle of the circle at one end, on the springing line;
        `length` is the depth along the axis.
      - `bevel`: a quarter-round for softening a corner, vertical by default. `start_deg`
        (0, 90, 180, 270) picks the quarter; `centre`, `length`, and `axis` as for a cylinder.

    `facing` is the side that is drawn: `out` (default for cylinder and bevel) or `in` (default
    for an arch, seen from below); the engine collides with that same side. Patches do not
    seal the map. Player collision depends on the shader's contents. The engine collides with
    a coarse polygon, not the drawn curve: each quarter-circle is one straight chord until its
    midpoint would sit 16 units or more off the curve (radius 45.3 and up), so a 24-radius pipe
    collides as a 45-degree diamond. The movement analysis uses the same polygon. The patch
    joins the worldspawn, a new entity of any other `target_classname`, or the entity
    `target_entity` names, as for `solid_commit`. Nothing reaches disk until `map_save`.
    """
    m = SESSION.require()
    k = _kernel()
    spec = {
        "kind": kind,
        "centre": centre,
        "radius": radius,
        "length": length,
        "axis": axis,
        "start_deg": start_deg,
    }
    if facing is not None:
        spec["facing"] = facing
    try:
        result = k.patch_commit(m, spec, shader, target_classname, new_entity, target_entity, label)
    except ValueError as e:
        raise ToolError(str(e)) from e
    result["next"] = (
        "map_save to write it; check shader collision in game or add simple clip geometry"
    )
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
            _, entry = _entry(name)
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
        store = _store()
    except solids.SolidStoreError as e:
        raise ToolError(str(e)) from e
    return {
        "sidecar": str(solids.sidecar_path(SESSION.path)),
        "solids": {
            n: {k: v for k, v in e.items() if k not in ("ir", "superseded")}
            for n, e in sorted(store["solids"].items())
        },
    }


def _recompile_param(name: str, path: str, value: Any) -> dict:
    """Prepare an exact replacement on a working copy, including its staged sidecar."""
    m = SESSION.require()
    store, entry = _entry(name)
    original = _generated(m, name)
    if not entry.get("provenance"):
        raise ToolError(
            "this older solid has no brush provenance; automatic replacement is refused. "
            "Recreate it with a new label to enable guarded parameter editing"
        )
    if not original or _fingerprints(original) != entry["provenance"]:
        raise ToolError(
            f"generated brushes for {name!r} changed or are missing; nothing changed. "
            "Manual geometry, textures and flags are preserved"
        )
    entities = {ei for ei, _, _ in original}
    if len(entities) != 1:
        raise ToolError(
            f"generated brushes for {name!r} were split across entities; nothing changed"
        )
    try:
        edited = solids.edit_param(entry["ir"], path, value)
    except solids.SolidStoreError as e:
        raise ToolError(str(e)) from e

    k = _kernel()
    try:
        textures = entry.get("textures")
        grid = entry.get("grid", SESSION.grid)
        before = k.solid_compile(entry["ir"], textures, grid)
        after = k.solid_compile(edited, textures, grid)
        replacement = k.Map.parse('{\n"classname" "worldspawn"\n}\n')
        k.solid_commit(replacement, edited, textures, grid, "worldspawn", False, name)
        candidate = m.copy()
        candidate.replace_brushes(
            next(iter(entities)), [(pi, source) for _, pi, source in original], replacement.source()
        )
    except ValueError as e:
        # The parameter was structurally valid and still produced impossible geometry, so
        # the tree comes back in the message — it is what the caller needs to adjust.
        raise ToolError(
            f"the edited tree does not compile: {e}. The parameter was accepted but "
            f"produces invalid geometry; try another value. Edited tree: {edited}"
        ) from e

    keep = ("brushes", "faces", "bounds", "volume")
    solids.record(
        store,
        name,
        edited,
        brushes=after["brushes"],
        notes=f"edited {path}",
        textures=textures,
        grid=grid,
        provenance=_fingerprints(_generated(candidate, name)),
    )
    return {
        "map": candidate,
        "store": store,
        "report": {
            "name": name,
            "path": path,
            "value": value,
            "ir": edited,
            "before": {kk: before[kk] for kk in keep},
            "after": {kk: after[kk] for kk in keep},
            "brushes_replaced": len(original),
        },
    }


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
    prepared = _recompile_param(name, path, value)
    return {**prepared["report"], "recorded": False}


@tool("sculpt", "Replace a generated solid", writes=True, destructive=True)
def solid_edit_param(name: str, path: str, value: Any) -> dict:
    """Change one parameter and replace exactly the solid's unchanged generated brushes.

    `solid_param_preview` takes the same arguments and reports the same numbers without
    writing; call that first.

    Geometry, textures, flags and comments are fingerprinted at creation. If any generated
    brush changed manually, replacement is refused. Other brushes and entity keys are kept.
    The recorded textures and grid are reused. Map and sidecar stay staged until map_save;
    a map checkpoint can undo both together.
    """
    prepared = _recompile_param(name, path, value)
    SESSION.stage_file(solids.sidecar_path(_require_path()), solids.encode(prepared["store"]))
    SESSION.map = prepared["map"]
    return {
        **prepared["report"],
        "recorded": True,
        "staged": True,
        "next": "render_contact_sheet to inspect the replacement; map_save writes map and sidecar",
    }
