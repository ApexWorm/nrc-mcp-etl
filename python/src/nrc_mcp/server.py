"""The MCP surface (§8): resources, prompts, and how the tool surface is assembled.

The tools themselves live in `nrc_mcp.surface`, one module per group; importing that
package registers them. This module binds the rest together — the resources, the §8.4
prompts, the derived inventory, and the scoping that decides which tools a given run
actually exposes.

Three conventions worth knowing, which the server also sends to the client at initialize
(`nrc_mcp.runtime.INSTRUCTIONS`, so they arrive before the first call rather than waiting
to be discovered):

**One open map.** Tools operate on a session map opened with `map_open`, rather than taking
a path every time. That keeps a sequence of queries consistent with each other, and makes
"the map I am editing" a single explicit thing.

**Nothing writes without being asked.** `map_save` is the only tool that touches a `.map`,
and it verifies the round-trip first: if the kernel cannot reproduce the file it loaded, it
refuses to write, because a tool that cannot reproduce your file has no business replacing
it. Every tool declares whether it writes, and a client reads that before deciding whether
to ask permission.

**Confidence is enforced, not advertised.** Findings from game rules carry a `confidence`,
and an unverified rule is downgraded so it can never fail a build. That is not ceremony:
three of the four spawn rules the design document called verified were wrong, and one would
have failed correct maps (`nrc://corrections`).
"""

from __future__ import annotations

import os
import sys

from . import profiles, solids, surface, tasks
from .kernel import KernelUnavailable, repo_root
from .runtime import (  # noqa: F401
    INSTRUCTIONS,
    SESSION,
    Session,
    _kernel,
    _profile_error_text,
    _require_path,
    _require_profile,
    active_profile,
    mcp,
    tool,
)
from .surface import get as get_tool  # noqa: F401

# Every tool is re-exported here, so `from nrc_mcp import server; server.map_open(...)`
# keeps working for the scenario tapes and the tests that call the functions directly.
#
# Driven by the *registered* names rather than by whatever each module happens to expose,
# so the re-export cannot drag an imported `Path` or `ToolError` into this namespace and
# shadow something. If a name is not a registered tool, it does not appear here.
for _tool in mcp._tool_manager.list_tools():
    _fn = surface.get(_tool.name)
    if _fn is not None:
        globals()[_tool.name] = _fn


@mcp.resource("nrc://tasks")
def resource_tasks() -> str:
    """The live mise task list — what this project can do."""
    return tasks.describe_tasks()


@mcp.resource("nrc://profile/{profile_id}")
def resource_profile(profile_id: str) -> str:
    """A game profile as YAML.

    Note the scheme is `nrc://`, not a per-game one: a game-specific URI scheme in code
    would itself be a seam violation (§7.4). The game is identified by the profile id.
    """
    path = profiles.profiles_dir() / f"{profile_id}.yaml"
    if not path.is_file():
        return f"no profile {profile_id!r}; available: {', '.join(profiles.available()) or 'none'}"
    return path.read_text()


@mcp.resource("nrc://conventions")
def resource_conventions() -> str:
    """The design tier rules and authoring guidance.

    Read this before authoring geometry. It says which representation to use and why, what to caulk,
    the authoring order, the composition patterns that work — and what is not built yet, which saves
    more time than it costs.
    """
    p = repo_root() / "docs" / "conventions.md"
    return p.read_text() if p.is_file() else "docs/conventions.md is missing"


@mcp.resource("nrc://corrections")
def resource_corrections() -> str:
    """Design claims that turned out to be wrong when verified against real sources.

    Read this before trusting a rule from the specification. It records which claims were
    checked, which failed, and what the evidence was.
    """
    p = repo_root() / "docs" / "spec-corrections.md"
    return p.read_text() if p.is_file() else "docs/spec-corrections.md is missing"


@mcp.resource("nrc://solid-ir", mime_type="text/markdown")
def resource_solid_ir() -> str:
    """Every Solid IR operator, its fields, and the on-grid caveat.

    Served as a resource as well as through `solid_help`, because a client that lists
    resources should not have to spend a tool call to read a static reference.
    """
    return solids.IR_HELP


@mcp.prompt(title="Mesh asset pipeline")
def asset_pipeline(feature: str) -> str:
    """The brush/patch/mesh decision and the Blender round trip, in order (§5.5)."""
    return f"""\
Add this to the open map: {feature}

Work in this order, and do not skip the decision step.

1. `asset_plan` — decide brush, patch or mesh. The ordering in `nrc://conventions` is the
   substance: a decorative-looking pillar that seals a room is still structural.
2. Brush parts: author Solid IR, `solid_compile`, `solid_preview`, then `solid_commit`.
3. Mesh parts: `blender_brief` → build it in Blender → export → `model_import` to validate
   against the brief that asked for it. Iterate on failures; do not place a failing mesh.
4. `model_place_plan` to check the keys, then `model_place`, then `model_make_clip` if it
   needs collision, then `render_camera` to look at it.
5. `validate`, then `compile_map` with the draft preset, then `map_save`.

Patches cannot be authored here. If the feature needs one, say so and stop.
"""


@mcp.prompt(title="Optimization pass")
def optimize_pass(target: str = "the open map") -> str:
    """Find vis cost, confirm it with a measured A/B rather than assuming it (§6.1)."""
    return f"""\
Reduce the compile and runtime cost of {target}, without changing how it plays.

1. `map_stats` — read the structural/detail split first; it is the dominant cost.
2. `structural_audit` — candidates, not a to-do list. Its benefit estimate is a heuristic.
3. Apply a change to a **copy**, then `compile_ab` against the original. This is the step
   that separates a real win from a plausible one; `ab_history` keeps the record.
4. `leak_trace` if it will not seal, `hint_suggest` on the `.prt` once it does.
5. `shader_audit` for the "works for me, broken on the server" class of problem.
6. Re-run `playable_space_diff` before saving. Optimization that removes playable space is
   not optimization.
"""


@mcp.prompt(title="Start a new map")
def new_map_bootstrap(concept: str, gametype: str = "") -> str:
    """Open a map from nothing, in an order that keeps it sealed and measurable."""
    gt = f" for {gametype}" if gametype else ""
    return f"""\
Start a new map{gt}: {concept}

1. `profile_summary` first. Size everything from the profile's verified constants — the
   design document's own standing height was wrong for this game by 13 units, so a corridor
   sized from memory will pass every check and be unusable.
2. `solid_help`, then block the space out as Solid IR: `hollow` for rooms, `carve_opening`
   for doorways. Preview before committing.
3. Keep it sealed as you go; `compile_map` draft and `leak_trace` catch a leak while it is
   still one room, not twenty.
4. `navgrid_stats` once there is floor — check `reachable_share` is 1.0.
5. Add spawns and objectives, then `validate_profile`.
"""


@mcp.prompt(title="Retrofit a gametype")
def gametype_retrofit(gametype: str) -> str:
    """Add a gametype's spawns and objectives to a map that does not have them (§8.4)."""
    return f"""\
Add {gametype} support to the open map.

1. `profile_summary` — the classnames, keys and counts are all profile data, never guessed.
2. `query_entities` — see what is already there before adding anything.
3. Place spawns and objectives, then `validate_profile` to check arrangement and counts.
4. `spawn_safety` and `balance_report` — a spawn with one exit can be held, and an
   asymmetric walk to the objective is a design problem the geometry will not reveal.
5. `playable_space_diff` before `map_save`.

Treat any finding marked `unverified` as advice. An unverified rule is never a failure —
see `nrc://corrections` for why that clamp exists.
"""


@mcp.resource("map://current/summary")
def resource_current_map() -> str:
    """A summary of the open map, or an explanation of why there isn't one."""
    if SESSION.map is None:
        return "No map is open. Call map_open(path)."
    s = SESSION.map.stats(grid=SESSION.grid)
    b = s["bounds"]
    lines = [
        f"path: {SESSION.path}",
        f"entities: {s['entities']}   brushes: {s['brushes']}   patches: {s['patches']}",
        f"faces: {s['faces']}   structural: {s['structural_brushes']}   detail: {s['detail_brushes']}",
        f"texdef formats: {', '.join(s['texdef_kinds']) or 'none'}",
        f"grid {s['grid']}: {s['vertices_on_grid']}/{s['vertices_total']} vertices aligned",
    ]
    if b:
        lines.append(f"bounds: {b['min']} .. {b['max']}  (size {b['size']})")
    if s["unevaluated_brushes"]:
        lines.append(f"NOTE: {s['unevaluated_brushes']} brush(es) could not be evaluated exactly")
    for w in SESSION.warnings:
        lines.append(f"WARNING: {w}")
    top = ", ".join(f"{t['shader']} ({t['faces']})" for t in s["top_shaders"][:8])
    lines.append(f"most used shaders: {top}")
    return "\n".join(lines)


TOOL_NAMES = (
    "map_open",
    "map_stats",
    "map_save",
    "query_entities",
    "brush_geometry",
    "validate",
    "render_topdown",
    "render_camera",
    "render_contact_sheet",
    "render_player_eye",
    "validate_profile",
    "bsp_report",
    "bsp_entity_diff",
    "pack_pk3",
    "repack_analyze",
    "ship_check",
    "solid_help",
    "solid_compile",
    "solid_preview",
    "solid_commit",
    "solid_inspect",
    "solid_list",
    "solid_param_preview",
    "solid_edit_param",
    "asset_plan",
    "blender_brief",
    "model_import",
    "model_place_plan",
    "model_place",
    "model_make_clip",
    "structural_audit",
    "hint_suggest",
    "leak_trace",
    "shader_audit",
    "compile_ab",
    "ab_history",
    "navgrid_stats",
    "balance_report",
    "sightline_report",
    "movement_check",
    "spawn_safety",
    "playable_space_diff",
    "bench_run",
    "upstream_diff",
    "upstream_fetch",
    "pr_plan_status",
    "selfdev_protected",
    "task_list",
    "task_run",
    "compile_map",
    "profile_summary",
)


def describe_surface() -> str:
    """Human-readable inventory, for `mise run mcp:tools`.

    Read from the SDK's own tool list rather than a table kept beside it, so it cannot
    describe a surface the server does not actually serve. Grouped, and marked `write` where
    a tool changes something — the two facts a reader most wants and the listing never had.
    """
    registered = mcp._tool_manager.list_tools()
    by_group: dict[str, list] = {}
    for t in registered:
        by_group.setdefault((t.meta or {}).get("group") or "(ungrouped)", []).append(t)

    writers = sum(1 for t in registered if t.annotations and not t.annotations.read_only_hint)
    lines = [
        "nrc-mcp surface",
        "",
        f"{len(registered)} tools in {len(by_group)} groups; {writers} of them write.",
        "Narrow it with NRC_MCP_GROUPS=<group,...> or NRC_MCP_READ_ONLY=1.",
        "",
        "TOOLS",
    ]
    for group in by_group:
        lines.append(f"  [{group}]")
        for t in by_group[group]:
            mark = " " if t.annotations and t.annotations.read_only_hint else "*"
            first = (t.description or "").strip().splitlines()[0] if t.description else ""
            lines.append(f"  {mark} {t.name:<22} {first}")
    lines += [
        "",
        "  * = changes something (readOnlyHint false)",
        "",
        "RESOURCES",
        "  nrc://tasks                the live mise task list",
        "  nrc://profile/{id}         a game profile as YAML",
        "  nrc://conventions          design tiers, caulk, authoring order, what is not built",
        "  nrc://corrections          verified corrections to the design document",
        "  nrc://solid-ir             the Solid IR operator reference",
        "  map://current/summary      the open map",
        "",
        f"active profile: {active_profile() or '(none)'}",
        f"profiles available: {', '.join(profiles.available()) or 'none'}",
        "",
        "LIMITS — ask for these and you will get an honest refusal, not a guess:",
        "  patch authoring    patches are read, validated and rendered, never created.",
        "                     Curved geometry has to be built in the editor.",
        "  reference sizes    there is no measured dimension corpus, so sizing comes from",
        "                     the profile's verified constants.",
        "  cover analysis     cover density and peek angles are not implemented.",
        "  traversal time     no player speed is verified, so distances are in world units",
        "                     and never in seconds.",
        "  rotated geometry   brushes whose plane points are off-grid are reported as",
        "                     BRUSH_NOT_EXACT and excluded from analysis rather than",
        "                     approximated. Every report carries the count.",
        "",
        "The editor bridge at contrib/mcpbridge has never been compiled and is not reachable",
        "from here. Kernel self-modification is gated behind human review; the prompt-layer",
        "loop is opt-in via NRC_SELFDEV=1.",
    ]
    try:
        lines.append(f"mise tasks discovered: {len(tasks.list_tasks())}")
    except RuntimeError as e:
        lines.append(f"mise task discovery failed: {e}")
    return "\n".join(lines)


class SurfaceError(RuntimeError):
    """The requested surface does not exist, so the server must not start pretending it does."""


def all_groups() -> list[str]:
    """Every group currently registered, in listing order, without duplicates."""
    seen: dict[str, None] = {}
    for t in mcp._tool_manager.list_tools():
        seen.setdefault((t.meta or {}).get("group") or "", None)
    return [g for g in seen if g]


def select_surface(groups: str | None = None, read_only: bool = False) -> dict:
    """Narrow the registered tools to the requested groups, and report what happened.

    Unknown group names and an empty selection are both errors. Silently serving zero tools
    would look exactly like a broken server to the client, and silently ignoring a typo
    would serve a surface nobody asked for — both are worse than refusing to start.
    """
    available = all_groups()
    wanted = [g.strip() for g in (groups or "").split(",") if g.strip()]

    if groups is not None and groups.strip() and not wanted:
        raise SurfaceError(f"NRC_MCP_GROUPS={groups!r} names no groups")
    if wanted:
        unknown = [g for g in wanted if g not in available]
        if unknown:
            raise SurfaceError(
                f"unknown tool group(s): {', '.join(unknown)}. Available: {', '.join(available)}"
            )

    dropped: list[str] = []
    for t in list(mcp._tool_manager.list_tools()):
        group = (t.meta or {}).get("group")
        writes = t.annotations is not None and t.annotations.read_only_hint is False
        if (wanted and group not in wanted) or (read_only and writes):
            mcp.remove_tool(t.name)
            dropped.append(t.name)

    kept = [t.name for t in mcp._tool_manager.list_tools()]
    if not kept:
        raise SurfaceError(
            "that selection leaves no tools at all. "
            f"Groups requested: {', '.join(wanted) or '(all)'}; read_only={read_only}"
        )
    return {"kept": kept, "dropped": sorted(dropped), "groups": wanted or available}


def _selection_from_env() -> dict:
    return select_surface(
        groups=os.environ.get("NRC_MCP_GROUPS"),
        read_only=os.environ.get("NRC_MCP_READ_ONLY") == "1",
    )


def main() -> int:
    try:
        from .kernel import kernel

        kernel()
    except KernelUnavailable as e:
        print(f"nrc-mcp: {e}")
        return 2
    try:
        _selection_from_env()
    except SurfaceError as e:
        # stderr, not stdout: stdout carries protocol and nothing else.
        print(f"nrc-mcp: {e}", file=sys.stderr)
        return 2
    mcp.run()
    return 0
