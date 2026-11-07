"""Driving q3map2, reading what it produced, and cutting what it costs (§6).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from mcp.server.mcpserver import Image as _Image
from mcp.server.mcpserver.exceptions import ToolError

from .. import botnav, botplay, gamepath, profiles
from .. import bsp as bspmod
from .. import detail as detailmod
from .. import optimize as optmod
from .. import playtest as playtestmod
from ..detail import Detail
from ..engine import EngineError
from ..kernel import repo_root, run_mise_task
from ..pack import _map_root
from ..runtime import (
    SESSION,
    _require_path,
    active_profile,
    tool,
)


@tool("compiling", "Play a bounded engine scenario", writes=True)
def engine_playtest(
    pk3: str,
    scenario: dict,
    profile_id: str | None = None,
    timeout_seconds: float = 180,
    include_images: bool = True,
    depends: list[str] | None = None,
    detail: Detail = "summary",
) -> list:
    """Exercise a packaged map with real player controls and fresh state assertions.

    Read `nrc://playtest/{profile_id}` for the scenario schema, supported inputs and fields.
    A scenario has `watch` names and ordered `steps`: join, position, aim, weapon, hold,
    wait, assert or screenshot. Each `until` is a list of predicates that must all match
    one fresh observation, e.g. {"scope":"player","field":"alive","value":true}.
    Joins wait for a living player; holds release on success, timeout and failure.

    `position` and `aim` are explicit test setup; `aim` takes angles, or `at` a point or a
    vector field of the latest observation (e.g. where a dynamite charge landed). Other
    actions use ordinary controls; the profile observer reads state without changing
    entities or executing scripts. `objective_playtests` writes scenarios for a map's
    objectives.
    Steps have bounded timeouts, with an overall limit of 600 seconds. An isolated game
    home, commands, observations, screenshots and result.json are retained even on failure.
    This tests a single client, not multiplayer balance. Requires a supported profile,
    POSIX console transport, game data and a display (or Xvfb).

    Returns the JSON receipt and, by default, the last three screenshot image blocks.
    `include_images=false` returns just the JSON receipt; all image paths remain recorded.
    `depends` lists other archives the map needs loaded with it (a map built on another
    map's textures and models, packed with `pack_pk3(depends=...)`).

    `detail="summary"` (default) gives passing steps as verdicts and failed steps in full;
    `"full"` adds every observation, the scenario echo and the command line.
    """
    full = detailmod.wants_full(detail)
    path = Path(pk3)
    if not path.is_absolute():
        path = repo_root() / path
    pid = profile_id or active_profile()
    if not pid:
        raise ToolError("select a profile or pass profile_id")
    try:
        report = playtestmod.run(
            path,
            pid,
            scenario,
            timeout_seconds=timeout_seconds,
            depends=[_resolve(d) for d in depends or []],
        )
    except (EngineError, profiles.ProfileError, OSError, ValueError) as e:
        raise ToolError(str(e)) from e
    content = [json.dumps(report if full else detailmod.engine_playtest(report))]
    if include_images:
        content.extend(
            _Image(data=Path(p).read_bytes(), format="jpeg") for p in report["screenshots"][-3:]
        )
    return content


@tool("compiling", "Run bots on the map in the engine", writes=True)
def bot_playtest(
    pk3: str,
    waypoints: str | None = None,
    seconds: float = 90,
    bots: int = 6,
    addon_dir: str | None = None,
    port: int = 0,
    profile_id: str | None = None,
    keep: bool = False,
    scripts: bool = True,
    depends: list[str] | None = None,
    nav_files: list[str] | None = None,
    detail: Detail = "summary",
) -> dict:
    """Load a packaged map with its bot waypoints on a dedicated server, add bots, watch them.

    `waypoints` is the file `bot_waypoints_write` staged (default: the open map's staged
    file). With `scripts` (default) the map goals and map script staged beside it go too
    (ET: Legacy `<map>_goals.gm`, `<map>.gm`), when present. The server runs for `seconds`
    (10..600) in a throwaway game home, with a throwaway copy of the bot add-on's folder
    (`addon_dir`, default the game install's own, for ET: Legacy `legacy/omni-bot`; it is
    only read) whose nav folder holds just these files, so no stock or stale ones can stand
    in for them. `bots` (1..32) bots join via the add-on's own command. `port` is the
    server's UDP port (0 picks a free one).

    With the scripts, `scripts` reports how many goals the add-on loaded, any goal or
    script errors it logged, the map script's own messages (map-script debugging is
    switched on in the throwaway folder only) with the goals each one switched, and per
    goal spot the seconds a bot of its team stood on it, so a spot switched off by an
    objective event can be seen emptied.

    Reports what the add-on logged (`waypoints_loaded`, one count per map load; any
    `load_errors`), per bot how far it travelled, how many distinct waypoints it passed
    and the stances it was seen in (a read-only observer samples every 2 seconds), and the
    game's own objective `events` (dynamite planted/defused, flags stolen, announcements,
    the round ending). `ok` means the file loaded without errors and at least one bot
    moved along the waypoints. Whether bots attempt an objective in a given run depends on
    the add-on's goal choices and the fight; read `events`, and run longer for more.
    `keep=true` retains the folders and names the logs. Needs the engine and game data.
    `depends` lists other archives the map needs loaded with it, as for `engine_playtest`.
    `nav_files` are more files for the add-on's nav folder beside the waypoints, such as the
    map's bot script (for ET: Legacy Omni-bot, `<map>.gm`), named as the add-on wants them.

    `ok` also needs the goals loaded and no goal or script error when scripts went in.
    `detail="summary"` (default) leaves out the per-bot rows when more than 12 bots ran;
    `"full"` always returns them.
    """
    full = detailmod.wants_full(detail)

    path = Path(pk3)
    if not path.is_absolute():
        path = repo_root() / path
    pid = profile_id or active_profile()
    if not pid:
        raise ToolError("select a profile or pass profile_id")
    for name, value, lo, hi in (("seconds", seconds, 10, 600), ("bots", bots, 1, 32)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ToolError(f"{name} must be between {lo} and {hi}")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ToolError("port must be an integer from 0 to 65535")
    try:
        spec = botnav.spec(pid)
        if waypoints is None:
            if SESSION.path is None:
                raise ToolError("pass waypoints, or open the map whose staged file to use")

            waypoints = str(
                _map_root(SESSION.path)
                / str(spec["stage_dir"])
                / botnav.install_path(spec, SESSION.path.stem)
            )
        files = [Path(waypoints)]
        if scripts:
            stem = Path(waypoints).stem
            for section in ("goals", "script"):
                name = Path(str((spec.get(section) or {}).get("install_path", ""))).name
                sibling = Path(waypoints).with_name(name.format(map=stem)) if name else None
                if sibling is not None and sibling.is_file():
                    files.append(sibling)
        report = botplay.run(
            path,
            pid,
            [*files, *[_resolve(f) for f in nav_files or []]],
            seconds=float(seconds),
            bots=int(bots),
            addon_dir=addon_dir,
            port=port,
            keep=keep,
            depends=[_resolve(d) for d in depends or []],
        )
    except (EngineError, botnav.BotNavError, profiles.ProfileError, OSError, ValueError) as e:
        raise ToolError(str(e)) from e
    if not full and len(report["bots"]) > 12:
        report["bots"] = report["bots"][:12]
        report["omitted"] = {"bots": 'rows past the first 12; detail="full" returns all'}
    return report


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else repo_root() / p


#: How long `compile_map` waits for a whole preset, in seconds. A release compile of a full-size
#: map is mostly -light: ET: Legacy's Supply Depot with its author's own light line took 15
#: minutes on 32 busy cores (README "Compile times"); a smaller machine needs a multiple of that.
COMPILE_TIMEOUTS = {"draft": 900, "iterate": 1800, "quality": 3600, "final": 7200}


@tool("compiling", "Compile with q3map2", writes=True)
def compile_map(
    preset: str = "draft", path: str | None = None, timeout_seconds: int | None = None
) -> dict:
    """Compile a map with q3map2.

    Presets (§6.1): `draft` geometry-only seconds-long check, `iterate` playable test build,
    `quality` review build, `final` release build. Light dominates the slow presets: on a
    full-size map `final` can take tens of minutes. `timeout_seconds` overrides the preset's
    limit (draft 900, iterate 1800, quality 3600, final 7200).

    On a WSL host where q3map2 is a Windows binary, the wrapper stages the map onto the
    Windows filesystem and copies artefacts back to `out/` — paths are handled for you.

    With no path (or the open map's path), compile a snapshot of the current unsaved map
    and staged companions. The result identifies that revision and its retained build folder.

    The profile's compile-only shaders (`compile_shaders`: editor shaders such as terrain
    alpha-blend volumes and hint-skip that real maps use but no game archive defines) are given
    to q3map2 on every compile where nothing else defines them, so a definition the game, the
    map or a pk3 beside it carries wins. They are never packed; shader_audit lists them under
    `compile_only`.
    """
    if preset not in COMPILE_TIMEOUTS:
        raise ToolError(f"unknown preset {preset!r} — choose draft, iterate, quality or final.")
    limit = COMPILE_TIMEOUTS[preset] if timeout_seconds is None else int(timeout_seconds)
    if limit < 1:
        raise ToolError("timeout_seconds must be at least 1")
    explicit = Path(path) if path else None
    if explicit is not None and not explicit.is_absolute():
        explicit = repo_root() / explicit
    use_session = explicit is None or (
        SESSION.path is not None and explicit.resolve() == SESSION.path.resolve()
    )
    receipt = None
    if use_session:
        SESSION.require()
        if SESSION.path is None:
            raise ToolError("no map is open and no path was given.")
        receipt = SESSION.receipt()
        source = SESSION.path.resolve()
        builds = repo_root() / "out" / "builds"
        builds.mkdir(parents=True, exist_ok=True)
        build = Path(
            tempfile.mkdtemp(prefix=f"{source.stem}-{receipt['sha256'][:12]}-", dir=builds)
        )
        target = build / "maps" / source.name
        target.parent.mkdir()
        root = source.parent.parent if source.parent.name.lower() == "maps" else source.parent
        # Keep asset resolution intact without copying a whole texture/model library.
        for name in ("textures", "models", "sound", "sounds", "video", "levelshots"):
            if (root / name).exists():
                (build / name).symlink_to(root / name, target_is_directory=True)
        if (root / "scripts").is_dir():
            shutil.copytree(root / "scripts", build / "scripts")
        # Archives kept beside `maps/` are on the compiler's search path there (-fs_pakpath);
        # a map built on another package's textures and models resolves them through these.
        for archive in root.iterdir():
            if archive.suffix.lower() == ".pk3" and archive.is_file():
                (build / archive.name).symlink_to(archive.resolve())
        for companion in source.parent.iterdir():
            if companion.resolve() in SESSION.baseline_files:
                continue
            if companion.is_file() and companion.suffix.lower() not in (
                ".map",
                ".bsp",
                ".prt",
                ".srf",
                ".lin",
                ".pk3",
            ):
                shutil.copy2(companion, target.parent / companion.name)
        for companion, data in {**SESSION.baseline_files, **SESSION.pending_files}.items():
            if data is not None and companion != source and companion.parent == source.parent:
                (target.parent / companion.name).write_bytes(data)
        target.write_text(SESSION.require().source(), encoding="utf-8", newline="")
        (build / "revision.json").write_text(
            json.dumps(
                {"revision": receipt, "profile": active_profile(), "preset": preset}, indent=2
            )
            + "\n"
        )
    else:
        target = explicit
    try:
        run = run_mise_task(
            f"compile:{preset}",
            [str(target)],
            timeout=limit,
            extra_env={"NRC_BUILD_OUTPUT": str(target.parent)} if receipt is not None else None,
        )
    except subprocess.TimeoutExpired as e:
        raise ToolError(
            f"the {preset} compile did not finish in {limit} seconds; a full-size map's light "
            "stage can take longer on this machine. Pass a larger timeout_seconds, or use "
            "quality/iterate while iterating."
        ) from e
    if receipt is not None:
        run["revision"] = receipt
        run["build_directory"] = str(build)
    # The wrapper's own report, lifted out of the task output: whether the map leaked (and
    # why, when there is no pointfile to trace) is the first thing to know after a compile.
    report = optmod._last_json_object(run.get("stdout") or "")
    if isinstance(report, dict):
        run["leaked"] = bool(report.get("leaked"))
        run["hint"] = report.get("hint") or ""
        run["artifacts"] = report.get("artifacts") or []
    if receipt is not None:
        SESSION.last_compile = {
            "source": str(target),
            "revision": receipt,
            "artifacts": run.get("artifacts", []),
        }
    return run


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
        report = optmod.structural_audit(
            SESSION.path,
            grid if grid is not None else SESSION.grid,
            limit=limit,
            game_map=SESSION.require(),
        )
        return {**report, "revision": SESSION.receipt()}
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Suggest hint brushes")
def hint_suggest(prt_path: str, limit: int = 10) -> dict:
    """Propose hint brush planes from a compiled portal file.

    Needs a `.prt`, which `-vis -saveprt` writes: compile with the `final` preset, or run
    `task_run("compile:final", [map])`.

    Each suggestion names the leaf, its portal count and a proposed splitting plane, with a
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
    void inwards: the first point is outside the map, the last is the origin of the entity that
    leaked (leakfile.cpp:80-107), and the hull is open where the path crosses it.

    With a map open and the active profile describing what seals (`sealing:`), the findings also
    name likely causes on the path: a brush that looks solid but has a side whose shader is
    translucent to the compiler (`alphashadow`, `trans`, clip and the like make the whole brush
    detail and non-opaque), detail brushes, and model entities, which never seal. Shaders are
    looked up in the base game (with the game path set) and the map's own scripts.

    Defaults to the most recent session build's `.lin`, with that build's revision.
    Without a session build, checks beside the open map. Explicit paths remain supported.
    """
    build = SESSION.last_compile if lin_path is None else None
    source = SESSION.path
    if build:
        source = Path(build["source"])
        candidates = [Path(p) for p in build["artifacts"] if str(p).endswith(".lin")]
        p = candidates[0] if candidates else source.with_suffix(".lin")
    elif lin_path:
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
    pid = active_profile() or None
    base = _basegame_dir(gamepath.basepath(), pid)
    try:
        report = optmod.leak_trace(
            p,
            source,
            profile_id=pid,
            basegame_dirs=[base] if base and Path(base).is_dir() else None,
        )
        if build:
            report["revision"] = build["revision"]
            report["stale"] = build["revision"] != SESSION.receipt()
        return report
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Audit shader references")
def shader_audit(
    shader_dirs: list[str] | None = None,
    profile_id: str | None = None,
    basegame_dir: str | None = None,
) -> dict:
    """Audit shader references against the shader scripts on disk.

    Reports shaders the map references but nothing defines, shaders defined but never used,
    shaders shadowing a base-game path — a classic cause of "works for me, broken on the server" —
    and the watercaulk trap, where a visible surface's shader draws nothing.

    `shader_dirs` defaults to the game's script directory from the environment. `basegame_dir`
    is where the base game's `.pk3` archives live, so the shaders and textures they ship count as
    defined; it defaults to the game path joined with the profile's `packaging.basegame`. With a
    base game known, `shader_dirs` may be empty: a map that ships no shaders of its own.
    Shaders the profile supplies to the compiler (`compile_shaders`) count as defined and are
    listed under `compile_only`: nothing needs to ship for them.
    """
    _require_path()
    pid = profile_id or active_profile() or None
    base = gamepath.basepath()
    dirs: list[str] = list(shader_dirs or [])
    if not dirs:
        game = os.environ.get("NRC_FS_GAME")
        if base and game:
            dirs = [str(Path(base) / game / "scripts")]
    basegame = basegame_dir or _basegame_dir(base, pid)
    if not dirs and not basegame:
        return {
            "error": "no shader directories given and none could be inferred",
            "hint": "pass shader_dirs or basegame_dir, or set the game path in mise.local.toml",
        }
    try:
        report = optmod.shader_audit(
            SESSION.path, dirs, pid, [basegame] if basegame else None, game_map=SESSION.require()
        )
        return {**report, "revision": SESSION.receipt()}
    except (OSError, ValueError) as e:
        raise ToolError(str(e)) from e


@tool("compiling", "Find base-game textures")
def texture_search(query: str = "", limit: int = 40, profile_id: str | None = None) -> dict:
    """Texture names the base game ships, for brush faces: every server has them, so a map
    that uses them packs and draws everywhere.

    `query` is matched as space-separated words that must all appear in the name (`floor sand`,
    `wall brick`, `trim`); empty lists the texture folders with a count each. Names come back
    the way a brush face spells them, without the `textures/` prefix. A name that is not a real
    texture packs into a `_FAILEDpack` archive, so pick from here rather than guessing.

    Each match says whether a shader script defines it (and its surfaceparms, e.g. `nonsolid`,
    `trans`, `nodraw`) or it is a plain image. Shaders whose surfaceparms make them invisible or
    non-solid are marked, since they are not wall material.
    """
    pid = profile_id or active_profile() or None
    basegame = _basegame_dir(gamepath.basepath(), pid)
    if not basegame:
        return {
            "error": "no base game known",
            "hint": "set the game path in mise.local.toml and select a profile with packaging.basegame",
        }
    index = optmod.basegame_index([basegame])
    names: dict[str, dict] = {}
    for name, shader in index["shaders"].items():
        if name.startswith("textures/"):
            names[name] = {"shader": True, "surfaceparms": shader.get("surfaceparms") or []}
    for name in index["images"]:
        if name.startswith("textures/") and name not in names:
            names[name] = {"shader": False, "surfaceparms": []}

    words = [w.lower() for w in query.split()]
    if not words:
        folders: dict[str, int] = {}
        for name in names:
            folder = name.split("/")[1] if name.count("/") >= 2 else ""
            folders[folder] = folders.get(folder, 0) + 1
        return {
            "basegame": basegame,
            "textures": len(names),
            "folders": dict(sorted(folders.items())),
            "hint": "search with words, e.g. texture_search('floor stone')",
        }

    special = {"nodraw", "nonsolid", "trans", "sky", "fog", "lava", "slime", "water", "playerclip"}
    hits = sorted(n for n in names if all(w in n for w in words))
    rows = []
    for name in hits[: max(1, limit)]:
        info = names[name]
        parms = [p.lower() for p in info["surfaceparms"]]
        rows.append(
            {
                "texture": name.removeprefix("textures/"),
                "shader": info["shader"],
                **({"surfaceparms": parms} if parms else {}),
                **({"not_wall_material": True} if special & set(parms) else {}),
            }
        )
    return {"query": query, "matches": len(hits), "shown": len(rows), "textures": rows}


def _basegame_dir(base: str | None, profile_id: str | None) -> str | None:
    """The base game's directory: the game path joined with the profile's `packaging.basegame`."""
    if not base or not profile_id:
        return None
    try:
        pkg = profiles.load(profile_id).get("packaging")
    except profiles.ProfileError:
        return None
    name = pkg.get("basegame") if isinstance(pkg, dict) else None
    return str(Path(base) / str(name)) if name else None
