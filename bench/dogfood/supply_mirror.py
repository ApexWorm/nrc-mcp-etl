"""Mirror a real map's source left-right over the MCP wire, then build and play it.

    mise run bench:dogfood -- supply_mirror [--map <etl_supply.map>] [--pk3 <etl_supply pk3>]

The input is the ET: Legacy overhaul of Supply Depot (`etl_supply.map`, its release pk3 for
the script, objective texts and art). Nothing is copied out of the pk3 into ours: the map is
compiled with the pk3 beside it (so q3map2 resolves its textures and models), packed with
`depends` (so its art stays out of our archive), and loaded with it in the engine.

Steps, all over MCP: map_open, map_mirror (x -> -x, the truck and the triggers riding on it
turned to face forward), Save As `etl_supply_mirror`, the script's one coordinate-bound
animation (the crane gauge's `faceangles` pitch) mirrored with script_set_event, arena,
objective texts, command map and levelshot, compile, pack, ship_check, an engine smoke
playtest, the generated objective playtests, and Omni-bot: seeded waypoints and the add-on's
own stock `etl_supply.way` mirrored the same way, each run with bot_playtest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

from dogfood_session import REPO, DogfoodSession, ToolCallError

NAME = "etl_supply_mirror"
SOURCE = "etl_supply"
# Brush entities the game turns to face the truck's path (followspline / attachtotag).
VEHICLES = ["truck", "truck_crate", "truck_trigger", "truck_toi"]
BOT_PORT = 27964

#: q3map2's alphaMod volumes (docs/shaderManual/terrain-and-alphamod-blends.html) and the
#: hint brushes' skip faces, as the stock Quake 3 common shaders define them.
COMPILE_SHADERS = """
textures/editor/alpha_000
{
	q3map_alphaMod volume
	q3map_alphaMod set 0
	surfaceparm nodraw
	surfaceparm nonsolid
	surfaceparm trans
	qer_trans 0.5
}

textures/editor/alpha_100
{
	q3map_alphaMod volume
	q3map_alphaMod set 1
	surfaceparm nodraw
	surfaceparm nonsolid
	surfaceparm trans
	qer_trans 0.5
}

textures/common/hintskip
{
	qer_nocarve
	surfaceparm nodraw
	surfaceparm nonsolid
	surfaceparm structural
	surfaceparm trans
	surfaceparm noimpact
	surfaceparm skip
}
"""


def mirror_loc(text: str) -> str:
    """A location file's "x y z name" lines with x negated."""
    out = []
    for line in text.splitlines():
        m = re.match(r"^(\s*)(-?[0-9.]+)(\s+.*)$", line)
        if m and not line.lstrip().startswith("//"):
            x = m.group(2)
            x = x[1:] if x.startswith("-") else ("-" + x if float(x) else x)
            line = m.group(1) + x + m.group(3)
        out.append(line)
    return "\n".join(out) + "\n"


def gauge_events(script: str) -> list[tuple[str, list[str], list[str]]]:
    """The crane gauge's events, its `faceangles` pitches negated (a turn about y reverses)."""
    block = re.search(r"^crane_control_gauge\s*\{(.*?)^\}", script, re.S | re.M)
    if not block:
        return []
    events = []
    for head, body in re.findall(
        r"^\t(\w[^\n{]*?)\s*\n\t\{(.*?)\n\t\}", block.group(1), re.S | re.M
    ):
        words = head.split()
        actions = []
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            m = re.match(r"faceangles\s+(-?[0-9.]+)\s+(.*)$", line)
            if m:
                line = f"faceangles {(-float(m.group(1))) % 360:g} {m.group(2)}"
            actions.append(line)
        events.append((words[0], words[1:], actions))
    return events


def objective_texts(objdata: str) -> tuple[dict, dict]:
    desc = dict(re.findall(r'wm_mapdescription\s+(\w+)\s+"([^"]*)"', objdata))
    objs: dict[str, list[str]] = {"axis": [], "allied": []}
    for team, _, text in re.findall(
        r'wm_objective_(axis|allied)_desc\s+(\d+)\s+"([^"]*)"', objdata
    ):
        objs[team].append(text)
    return desc, objs


_NUM = r"\s*(-?[0-9.]+)\s*"


def mirror_gm(text: str) -> str:
    """An Omni-bot map script with its `Vec3(x, y, z)` and `AABB(x1, ..., z2)` mirrored in x."""

    def neg(v: str) -> str:
        return f"{-float(v):.3f}"

    text = re.sub(
        rf"Vec3\({_NUM},{_NUM},{_NUM}\)",
        lambda m: f"Vec3({neg(m[1])}, {m[2]}, {m[3]})",
        text,
    )
    return re.sub(
        rf"AABB\({_NUM},{_NUM},{_NUM},{_NUM},{_NUM},{_NUM}\)",
        lambda m: f"AABB({neg(m[4])},{m[2]},{m[3]},{neg(m[1])},{m[5]},{m[6]})",
        text,
    )


def mirror_way(src: Path, dest: Path) -> dict:
    """Mirror an Omni-bot waypoint file in x with the repo's codec (positions, facings, box)."""
    sys.path.insert(0, str(REPO / "python/src"))
    from nrc_mcp import botnav  # noqa: PLC0415

    fmt = botnav.spec("etl")["format"]
    wf = botnav.read(src.read_bytes(), fmt)
    for wp in wf.waypoints:
        x, y, z = wp.position
        wp.position = (-x, y, z)
        fx, fy, fz = wp.facing
        wp.facing = (-fx, fy, fz)
    lo, hi = wf.world_mins, wf.world_maxs
    wf.world_mins, wf.world_maxs = (-hi[0], lo[1], lo[2]), (-lo[0], hi[1], hi[2])
    data = botnav.write(wf, fmt)
    dest.write_bytes(data)
    return {"waypoints": len(wf.waypoints), "file": str(dest)}


def per_objective(generated_run: dict, receipt: dict) -> list[dict]:
    """Each objective's outcome and engine seconds in one run: its steps end at the
    `<name>_done` screenshot objective_playtests puts after each test (the templates take
    none of their own). A receipt names each step's action; the scenario has the rest."""
    rows, current = [], {"seconds": 0.0, "ok": True, "steps": 0}
    names = iter(generated_run["objectives"])
    planned = generated_run["scenario"]["steps"]
    for s in receipt.get("steps", []):
        index = s.get("index")
        step = planned[index] if isinstance(index, int) and index < len(planned) else {}
        current["seconds"] += float(s.get("seconds") or 0)
        current["steps"] += 1
        current["ok"] = current["ok"] and bool(s.get("ok"))
        if not s.get("ok"):
            current["failed"] = step.get("label") or step.get("action")
            current["error"] = s.get("error")
        if step.get("action") == "screenshot" and str(step.get("name", "")).endswith("_done"):
            rows.append({"objective": next(names, "?"), **current})
            current = {"seconds": 0.0, "ok": True, "steps": 0}
    if current["steps"]:
        rows.append({"objective": next(names, "?"), **current, "ok": False})
    for row in rows:
        row["seconds"] = round(row["seconds"], 1)
    return rows


async def main(args):
    source_map = Path(args.map).resolve()
    source_pk3 = Path(args.pk3).resolve()
    async with DogfoodSession("supply_mirror") as run:
        maps = run.root / "maps"
        maps.mkdir()
        shutil.copy(source_map, maps / f"{SOURCE}.map")
        with zipfile.ZipFile(source_pk3) as z:
            script = z.read(f"maps/{SOURCE}.script").decode("latin-1")
            objdata = z.read(f"maps/{SOURCE}.objdata").decode("latin-1")
            loc = z.read(f"maps/{SOURCE}_loc_override.dat").decode("latin-1")
            arena = z.read(f"scripts/{SOURCE}.arena").decode("latin-1")
        # The script carries over (it names entities, not places): kept under the source's
        # name, it is a companion Save As takes along. The location names get x negated.
        (maps / f"{SOURCE}.script").write_text(script, encoding="latin-1")
        (maps / f"{NAME}_loc_override.dat").write_text(mirror_loc(loc), encoding="latin-1")
        # Beside maps/, so the compiler's -fs_pakpath finds the original textures and models.
        (run.root / source_pk3.name).symlink_to(source_pk3)
        # Compile-only shaders the source uses and no archive defines (they came with the
        # author's editor setup): without them q3map2 builds the terrain's alphaMod volumes
        # and hint-skip faces as solid, drawn brushes.
        (run.root / "scripts").mkdir()
        (run.root / "scripts" / f"{NAME}_compile.shader").write_text(COMPILE_SHADERS)

        opened = await run.call("map_open", path=str(maps / f"{SOURCE}.map"))
        assert opened["round_trip"]["identical"], opened["round_trip"]
        for event, params, actions in gauge_events(script):
            await run.call(
                "script_set_event",
                block="crane_control_gauge",
                event=event,
                params=params,
                actions=actions,
            )
        mirrored = await run.call("map_mirror", axis="x", at=0, vehicles=VEHICLES)
        # Save As, script along: a mirror moves all playable space, so the before/after
        # comparison would only report that; the geometry is validated instead (below).
        await run.call("map_save", path=str(maps / f"{NAME}.map"), check_playable_space=False)
        keys = {
            k: v.strip().strip('"')
            for k, v in re.findall(r"^\s*(\w+)[ \t]+([^\r\n]*)", arena, re.M)
        }
        await run.call(
            "arena_write",
            longname="Supply Depot (mirrored)",
            keys={
                k: keys[k]
                for k in ("briefing", "timelimit", "axisRespawnTime", "alliedRespawnTime")
                if k in keys
            },
        )
        desc, objs = objective_texts(objdata)
        await run.call("objdata_write", descriptions=desc, objectives=objs)
        await run.call("commandmap_make")
        # The Allied start, mirrored (x -2176 -> 2176), looking north up the truck road.
        await run.call("levelshot_make", position=[2176, -100, 0], yaw_deg=90)
        geometry = await run.call("validate", severity_min="error")
        profile = await run.call("validate_profile", severity_min="error")
        await run.call("map_save", check_playable_space=False)

        compiled = await run.call("compile_map", preset=args.preset)
        assert compiled["ok"] and not compiled.get("leaked"), compiled
        bsp = next(p for p in compiled["artifacts"] if p.endswith(".bsp"))
        # One decal the source names has no image anywhere; the release pk3 lacks it too.
        missing_upstream = ["textures/rubble/debri_m01b"]
        packed = await run.call(
            "pack_pk3", bsp=bsp, depends=[str(source_pk3)], ignore=missing_upstream
        )
        package = (packed["packages"] or packed["failed_packages"])[0]
        ship = await run.call("ship_check", pk3=package)
        result = {
            "map": str(maps / f"{NAME}.map"),
            "package": package,
            "depends": [str(source_pk3)],
            "mirror": mirrored,
            "validate": geometry["summary"],
            "validate_profile": profile["summary"],
            "compile": {
                "preset": args.preset,
                "seconds": compiled.get("total_seconds") or compiled.get("seconds"),
                "ok": compiled["ok"],
            },
            "pack": {
                k: packed.get(k)
                for k in ("ok", "not_found", "accounted_for", "allowed_missing", "dropped_basegame")
            },
            "ship_check": ship.get("summary"),
        }
        run.artifact("result", result)

        # In the engine: Allies start in the mirrored Allied Start (x > 0) beside the truck,
        # which the script has put on the mirrored path; Axis at the forward bunker flag.
        smoke = json.loads((Path(__file__).parent / "scenarios" / "supply_mirror.json").read_text())
        if args.engine:
            receipt = await run.call(
                "engine_playtest",
                pk3=package,
                scenario=smoke,
                profile_id="etl",
                timeout_seconds=240,
                include_images=False,
                depends=[str(source_pk3)],
            )
            result["engine_smoke"] = {k: receipt.get(k) for k in ("ok", "error", "result_path")}
            run.artifact("result", result)
            # The objective flow, hand-written (objective_playtests chains the objectives
            # behind the gates after them, but does not escort the truck or respawn in the
            # bunker): escort the truck a little,
            # dynamite the forward bunker gate, respawn in the bunker the script hands the
            # Allies, dynamite the depot gate. `position` is test setup, not a route.
            flow = json.loads(
                (Path(__file__).parent / "scenarios" / "supply_mirror_flow.json").read_text()
            )
            try:
                receipt = await run.call(
                    "engine_playtest",
                    pk3=package,
                    scenario=flow,
                    profile_id="etl",
                    timeout_seconds=420,
                    include_images=False,
                    depends=[str(source_pk3)],
                )
            except ToolCallError as e:
                receipt = {"ok": False, "error": str(e)}
            result["engine_flow"] = {
                "ok": receipt.get("ok"),
                "error": receipt.get("error"),
                "steps": [
                    {k: s.get(k) for k in ("index", "action", "ok", "seconds", "error")}
                    for s in receipt.get("steps", [])
                ],
                "result_path": receipt.get("result_path"),
            }
            run.artifact("result", result)

        if args.objectives:
            started = time.monotonic()
            generated = await run.call("objective_playtests", cell=args.cell)
            result["objective_tests"] = {
                "seconds": round(time.monotonic() - started, 1),
                "tests": [
                    {
                        "objective": t["objective"],
                        "kind": t["kind"],
                        "requires": t.get("requires", []),
                        "standing": t["standing"],
                    }
                    for t in generated["tests"]
                ],
                "skipped": generated["skipped"],
                "runs": [],
            }
            for r in generated["runs"]:
                try:
                    receipt = await run.call(
                        "engine_playtest",
                        pk3=package,
                        scenario=r["scenario"],
                        profile_id="etl",
                        timeout_seconds=r["timeout_seconds"],
                        include_images=False,
                        depends=[str(source_pk3)],
                    )
                except ToolCallError as e:
                    receipt = {"ok": False, "error": str(e)}
                failed = next((s for s in receipt.get("steps", []) if not s.get("ok")), None)
                result["objective_tests"]["runs"].append(
                    {
                        "objectives": r["objectives"],
                        "ok": receipt.get("ok"),
                        "error": receipt.get("error"),
                        "failed_step": failed,
                        "per_objective": per_objective(r, receipt),
                        "result_path": receipt.get("result_path"),
                    }
                )
                run.artifact("result", result)

        if args.bots:
            # The add-on's own files for the original map, mirrored: waypoints, and its map
            # script and goals (their Vec3 positions and AABB regions).
            stock_dir = Path(args.stock_way).parent
            mirrored = run.root / "omni-bot-stock"
            mirrored.mkdir()
            gm = []
            for suffix in (".gm", "_goals.gm"):
                src = stock_dir / f"{SOURCE}{suffix}"
                if src.is_file():
                    dest = mirrored / f"{NAME}{suffix}"
                    dest.write_text(mirror_gm(src.read_text(errors="replace")))
                    gm.append(str(dest))
            plays = {"seeded": (None, [])}
            if args.bots_with_gm and gm:
                plays["seeded+stock_gm"] = (None, gm)
            if Path(args.stock_way).is_file():
                way = mirrored / f"{NAME}.way"
                result["stock_way_mirrored"] = mirror_way(Path(args.stock_way), way)
                plays["stock_way+stock_gm"] = (str(way), gm)
            result["bots"] = {"write": await run.call("bot_waypoints_write", cell=args.cell)}
            for label, (waypoints, nav) in plays.items():
                try:
                    result["bots"][label] = await run.call(
                        "bot_playtest",
                        pk3=package,
                        **({"waypoints": waypoints} if waypoints else {}),
                        nav_files=nav,
                        seconds=args.bot_seconds,
                        bots=args.bot_count,
                        port=args.bot_port,
                        depends=[str(source_pk3)],
                    )
                except ToolCallError as e:
                    result["bots"][label] = {"ok": False, "error": str(e)}
                run.artifact("result", result)
        print(json.dumps({"result": str(run.root / "result.json"), "package": package}))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    p.add_argument("--map", default="/mnt/media_raid/etl_supply.map")
    p.add_argument("--pk3", default="/mnt/media_raid/etl_supply_v14.pk3")
    p.add_argument("--preset", default="iterate", choices=["draft", "iterate", "quality", "final"])
    p.add_argument("--no-engine", dest="engine", action="store_false")
    p.add_argument("--no-objectives", dest="objectives", action="store_false")
    p.add_argument("--no-bots", dest="bots", action="store_false")
    p.add_argument(
        "--bots-with-gm",
        action="store_true",
        help="also run the seeded waypoints with the mirrored stock bot script",
    )
    p.add_argument("--bot-seconds", type=float, default=300)
    # The grids cover the whole map, sky and clip ceiling included (6912 x 6912 x 3456):
    # 40M cells at 16, which the sparse grid holds in a few megabytes.
    p.add_argument("--cell", type=float, default=16)
    p.add_argument("--bot-count", type=int, default=8)
    p.add_argument("--bot-port", type=int, default=BOT_PORT, help="net_port for bot_playtest")
    p.add_argument(
        "--stock-way",
        default="/root/etlegacy/etlegacy-v2.86.2-x86_64/legacy/omni-bot/et/nav/etl_supply.way",
        help="the add-on's own waypoints for the unmirrored map, mirrored for comparison",
    )
    asyncio.run(main(p.parse_args()))
