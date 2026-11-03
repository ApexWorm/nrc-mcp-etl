"""The tool surface, one module per group.

**Import order is listing order.** Tools register as these modules are imported, and
`tools/tier2.py` caches the tool-schema prefix between turns — that prefix has to be
byte-identical or the cache never reads. Order is therefore behaviour, not layout.

Which is why the modules are named in a tuple and imported through `importlib`, rather
than written as a plain `from . import a, b, c`. An import block is exactly the thing an
import sorter rewrites: the first version of this file said the order mattered and was
then quietly alphabetised by `ruff format`, which reordered the whole surface. A tuple is
data, and no formatter touches it.

The groups match the README's own headings, so the generated tool table and the prose
around it describe the same thing.
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: Registration order. Changing it re-orders the tool list every client sees.
ORDER = (
    "mapfile",
    "render",
    "sculpt",
    "assets",
    "compiling",
    "gameplay",
    "shipping",
    "project",
    "maintenance",
)

#: What every preset starts from: open, inspect, check and save a map, and read the profile.
_CORE = (
    "map_open",
    "map_new",
    "map_stats",
    "map_save",
    "map_checkpoint",
    "map_restore",
    "query_entities",
    "validate",
    "validate_profile",
    "profile_summary",
)
_LOOK = ("render_topdown", "render_camera", "render_contact_sheet", "render_player_eye")

#: Named tool subsets for typical sessions, cutting across the groups. A group is where a
#: tool lives; a preset is what one kind of session calls. Drawn from the dogfood sessions
#: (`bench/dogfood`, `out/dogfood/*/calls`), which build, analyse and ship maps over MCP:
#: their calls fall into these three phases. `NRC_MCP_GROUPS` accepts these names beside
#: the group names, and a selection is the union of everything it names.
PRESETS: dict[str, tuple[str, ...]] = {
    # Blocking out and wiring a map: geometry, entities, objectives and the script.
    "build": _CORE
    + _LOOK
    + (
        "entity_place",
        "entities_place",
        "spawn_cluster",
        "entity_set_keys",
        "entity_remove_keys",
        "objective_place",
        "path_place",
        "script_show",
        "script_set_event",
        "script_remove",
        "solid_help",
        "solid_compile",
        "solid_preview",
        "solid_commit",
        "solids_commit",
        "patch_commit",
        "brush_set_detail",
        "solid_inspect",
        "solid_list",
        "solid_param_preview",
        "solid_edit_param",
        "texture_search",
    ),
    # Measuring how it plays, without changing geometry.
    "analyse": _CORE
    + _LOOK
    + (
        "script_show",
        "brush_geometry",
        "navgrid_stats",
        "route_report",
        "route_render",
        "balance_report",
        "spawn_safety",
        "movement_check",
        "sightline_report",
        "lighting_report",
        "playable_space_diff",
    ),
    # Compiling, packaging and proving it in the engine.
    "ship": _CORE
    + (
        "render_player_eye",
        "compile_map",
        "leak_trace",
        "bsp_report",
        "bsp_entity_diff",
        "shader_audit",
        "texture_search",
        "pack_pk3",
        "repack_analyze",
        "ship_check",
        "arena_write",
        "objdata_write",
        "commandmap_make",
        "levelshot_make",
        "engine_playtest",
    ),
}
#: A whole map-making session, start to finish: the three phases together.
PRESETS["level"] = tuple(dict.fromkeys(PRESETS["build"] + PRESETS["analyse"] + PRESETS["ship"]))

#: The imported modules, in `ORDER`. Importing them is what registers their tools.
MODULES: tuple[ModuleType, ...] = tuple(
    importlib.import_module(f".{name}", __name__) for name in ORDER
)


def get(name: str):
    """The plain Python function behind a tool name, or None if there is no such tool.

    `tools/tapes.py` replays recorded sequences by calling these directly, with no protocol
    in the way — so a tape exercises the same code an agent reaches, minus the transport.
    """
    for mod in MODULES:
        fn = vars(mod).get(name)
        if callable(fn):
            return fn
    return None
