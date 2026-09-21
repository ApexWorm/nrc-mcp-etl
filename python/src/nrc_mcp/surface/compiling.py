"""Driving q3map2, reading what it produced, and cutting what it costs (§6).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from .. import bsp as bspmod
from .. import optimize as optmod
from ..kernel import repo_root, run_mise_task
from ..runtime import (
    SESSION,
    _require_path,
    active_profile,
    tool,
)


@tool("compiling", "Compile with q3map2", writes=True)
def compile_map(preset: str = "draft", path: str | None = None) -> dict:
    """Compile a map with q3map2.

    Presets (§6.1): `draft` geometry-only seconds-long check, `iterate` playable test build,
    `quality` review build, `final` release build.

    On a WSL host where q3map2 is a Windows binary, the wrapper stages the map onto the
    Windows filesystem and copies artefacts back to `out/` — paths are handled for you.
    """
    if preset not in ("draft", "iterate", "quality", "final"):
        raise ToolError(f"unknown preset {preset!r} — choose draft, iterate, quality or final.")
    target = path
    if target is None:
        if SESSION.path is None:
            raise ToolError("no map is open and no path was given.")
        target = str(SESSION.path)
    return run_mise_task(f"compile:{preset}", [target])


@tool("compiling", "Compile and compare two maps", writes=True)
def compile_ab(map_a: str, map_b: str, preset: str = "draft") -> dict:
    """Compile two variants and diff the numbers that matter.

    Reports per-stage wall time, draw surface count, leaf count, brush count and BSP size for each,
    and appends a row to `bench/ab-history.jsonl` so regressions stay visible over the life of a
    project rather than being rediscovered.

    This is how a structural_audit suggestion gets *confirmed* rather than assumed: apply it to a
    copy, compile both, and read the portal delta.
    """
    a, b = Path(map_a), Path(map_b)
    if not a.is_absolute():
        a = repo_root() / a
    if not b.is_absolute():
        b = repo_root() / b
    try:
        return optmod.compile_ab(a, b, preset=preset)
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "A/B comparison history")
def ab_history() -> dict:
    """Every recorded A/B comparison, oldest first."""
    try:
        return {"rows": optmod.ab_history()}
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Compiled BSP report")
def bsp_report(lumps_path: str, profile_id: str | None = None) -> dict:
    """Read a compiled BSP's structure from an unpacked `-json` lump directory.

    Produce the directory with `task_run("bsp:json-unpack", [path_to_bsp])`.

    Reports surfaces per shader (which shader is eating draw calls), surface types, lightmap
    coverage, unreferenced shaders, and headroom. Headroom comes in two parts and the
    distinction matters: **compiler limits** are read from this fork's source and are real,
    but most of the classic Quake 3 ceilings were removed upstream, so there is nothing to
    report for them. **Engine limits** come from the profile and are what decide whether the
    map loads at all — a map can compile cleanly and still be rejected in game.
    """
    p = Path(lumps_path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return bspmod.report(p, profile_id or active_profile() or None)
    except bspmod.BspError as e:
        raise ToolError(str(e)) from e


@tool("compiling", "BSP entity diff")
def bsp_entity_diff(lumps_path: str, source_map: str | None = None) -> dict:
    """Compare a compiled BSP's entity lump against the source `.map`.

    Entities can be dropped silently at compile time — the editor's writer discards empty
    group entities and q3map2 drops what it cannot place — so a count that quietly changed is
    a real bug this catches.
    """
    p = Path(lumps_path)
    if not p.is_absolute():
        p = repo_root() / p
    src = Path(source_map) if source_map else SESSION.path
    if src is None:
        raise ToolError(
            "no source map given and no map is open — pass source_map, or map_open the "
            "`.map` this BSP was compiled from."
        )
    try:
        return bspmod.entity_diff(p, src)
    except bspmod.BspError as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Audit structural brushes")
def structural_audit(grid: int | None = None, limit: int = 50) -> dict:
    """Find brushes marked structural that need not be — the biggest lever on vis cost.

    A structural brush blocks visibility and costs portals; a detail brush does not. Anything not
    sealing the map or acting as a major visual blocker should be detail, and converting it is
    usually the cheapest large win available on a Q3-engine map.

    The estimated benefit per brush is a **heuristic, not a measurement**. The real number needs a
    `-vis` compile before and after, which `compile_ab` can do. Treat the list as candidates to
    look at, not as a to-do list to apply blindly.
    """
    _require_path()
    try:
        return optmod.structural_audit(
            SESSION.path, grid if grid is not None else SESSION.grid, limit=limit
        )
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Suggest hint brushes")
def hint_suggest(prt_path: str, limit: int = 10) -> dict:
    """Propose hint brush planes from a compiled portal file.

    Needs a `.prt`, which `-vis -saveprt` writes: compile with the `final` preset, or run
    `task_run("compile:final", [map])`.

    §6.1 calls this "tedious, high-skill, high-payoff work that almost nobody does properly by
    hand". Each suggestion names the leaf, its portal count and a proposed splitting plane, with a
    predicted before/after — predicted, because confirming it needs another `-vis` run.
    """
    p = Path(prt_path)
    if not p.is_absolute():
        p = repo_root() / p
    try:
        return optmod.hint_suggest(p, limit=limit)
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Trace a leak")
def leak_trace(lin_path: str | None = None) -> dict:
    """Read a leak pointfile and report the path out of the map.

    q3map2 writes a `.lin` beside the map when the world is not sealed. The path runs from the
    entity that leaked to the void, so the first point is inside the map and the last is outside;
    the gap is somewhere along it.

    Defaults to the `.lin` beside the open map.
    """
    if lin_path:
        p = Path(lin_path)
        if not p.is_absolute():
            p = repo_root() / p
    elif SESSION.path is not None:
        p = SESSION.path.with_suffix(".lin")
    else:
        raise ToolError(
            "no pointfile given and no map is open — pass lin_path, or map_open the map whose "
            "last compile leaked."
        )
    if not p.is_file():
        return {
            "error": f"{p} does not exist",
            "hint": "a pointfile only exists when the last compile leaked; a sealed map has none",
        }
    try:
        return optmod.leak_trace(p, SESSION.path)
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Audit shader references")
def shader_audit(shader_dirs: list[str] | None = None, profile_id: str | None = None) -> dict:
    """Audit shader references against the shader scripts on disk.

    Reports shaders the map references but nothing defines, shaders defined but never used,
    shaders shadowing a base-game path — a classic cause of "works for me, broken on the server" —
    and the watercaulk trap, where a visible surface's shader draws nothing.

    `shader_dirs` defaults to the game's script directory from the environment.
    """
    _require_path()
    dirs: list[str] = list(shader_dirs or [])
    if not dirs:
        base = os.environ.get("NRC_FS_BASEPATH") or os.environ.get("URT_BASEPATH")
        game = os.environ.get("NRC_FS_GAME")
        if base and game:
            dirs = [str(Path(base) / game / "scripts")]
    if not dirs:
        return {
            "error": "no shader directories given and none could be inferred",
            "hint": "pass shader_dirs, or set the game path in mise.local.toml",
        }
    try:
        return optmod.shader_audit(SESSION.path, dirs, profile_id or active_profile() or None)
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e
