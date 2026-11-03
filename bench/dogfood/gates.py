"""Build a corridor of gates bots must open in turn, seed its waypoints, and (with --bots) run
bots on it and say where they were before and after each gate changed.

West to east: the Axis rooms (spawn and radio) | a wall Allies dynamite, the only way through |
a hall | a trench the Allies bridge, the only way across (falling in kills) | a hall | an
Allies-only door | the Allied spawn room. Allies must open the door, build the bridge, blow
the wall and then the radio; Axis are shut in until the wall goes.

    mise run bench:dogfood -- gates                    # build, pack, seed waypoints
    mise run bench:dogfood -- gates --bots [--seconds 600] [--port 27962] [--strip BLOCKWALL]

`--bots` uses the newest build under out/dogfood/gates/. `--strip` removes a flag from every
waypoint before the run, as a control: what the bots do without it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
from pathlib import Path

from dogfood_session import REPO, DogfoodSession

# The gates, as the map builds them (x is west-east, every room spans y 0..512).
WALL = {"min": [512, 192, 0], "max": [544, 320, 128]}
TRENCH = (1056, 1312)
DECK = {"min": [1056, 192, -16], "max": [1312, 320, 0]}
DOOR = {"min": [1832, 192, 0], "max": [1848, 320, 128]}


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def build():
    async with DogfoodSession("gates") as run:
        wall = (await run.call("texture_search", query="bunker_sd int_wall01"))["textures"][0]
        floor = (await run.call("texture_search", query="battery_wall wall02_mid"))["textures"][0]
        metals = (await run.call("texture_search", query="metal", limit=40))["textures"]
        metal = next(r["texture"] for r in metals if not r.get("not_wall_material"))
        textures = {"wall": wall["texture"], "floor": floor["texture"], "metal": metal}
        run.artifact("materials", textures)
        path = run.root / "maps/nrc_gates.map"
        await run.call("map_new", path=str(path))
        await run.call(
            "entity_set_keys",
            entity=0,
            keys={"message": "Gates", "_ambient": "12", "_minlight": "6"},
        )
        shell = {"op": "hollow", "solid": box([-32, -32, -288], [2592, 544, 288]), "thickness": 32}
        await run.call(
            "solid_commit", ir=shell, label="shell", textures={"default": textures["wall"]}
        )
        for label, lo, hi in [
            ("floor_west", [0, 0, -256], [TRENCH[0], 512, 0]),
            ("floor_east", [TRENCH[1], 0, -256], [2560, 512, 0]),
        ]:
            await run.call(
                "solid_commit", ir=box(lo, hi), label=label, textures={"default": textures["floor"]}
            )
        for label, x0, x1 in [("wall_partition", 512, 544), ("door_partition", 1824, 1856)]:
            await run.call(
                "solid_commit",
                label=label,
                ir={
                    "op": "subtract",
                    "from": box([x0, 0, 0], [x1, 512, 256]),
                    "cut": [box([x0 - 16, 192, -16], [x1 + 16, 320, 128])],
                },
                textures={"default": textures["wall"]},
            )
        # Falling into the trench kills, so a bot that walks off the edge shows up as a death.
        hurt = await run.call(
            "solid_commit",
            ir=box([TRENCH[0], 0, -256], [TRENCH[1], 512, -192]),
            label="trench_hurt",
            textures={"default": "common/trigger"},
            target_classname="trigger_hurt",
            new_entity=True,
        )
        await run.call("entity_set_keys", entity=hurt["entity"], keys={"dmg": 500})
        door = await run.call(
            "solid_commit",
            ir=box(DOOR["min"], DOOR["max"]),
            label="allied_door",
            textures={"default": textures["metal"]},
            target_classname="func_door",
            new_entity=True,
        )
        await run.call(
            "entity_set_keys",
            entity=door["entity"],
            keys={"allowteams": "allies", "angle": -1, "speed": 200, "wait": 2, "type": 5},
        )

        for cls, x, angle, flag, description in [
            ("team_CTF_redspawn", 288, 0, 1, "Axis Rooms"),
            ("team_CTF_bluespawn", 2400, 180, 2, "Allied Rooms"),
        ]:
            await run.call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=[x, 256, 24],
                keys={"description": description, "spawnflags": flag},
            )
            for y in (96, 176, 336, 416):
                await run.call(
                    "entity_place",
                    classname=cls,
                    origin=[x, y, 24],
                    keys={"angle": angle, "spawnflags": 3},
                )
        await run.call("entity_place", classname="script_multiplayer", origin=[64, 64, 64])
        await run.call(
            "entity_place",
            classname="info_player_intermission",
            origin=[1184, 448, 200],
            keys={"angles": [30, 270, 0], "spawnflags": 3},
        )
        objectives = {}
        objectives["wall"] = await run.call(
            "objective_place",
            kind="dynamite",
            name="gate_wall",
            track="the Gate Wall",
            team="axis",
            objective=WALL,
            trigger={"min": [480, 128, 0], "max": [704, 384, 160]},
            texture=textures["metal"],
        )
        objectives["bridge"] = await run.call(
            "objective_place",
            kind="constructible",
            name="trench_bridge",
            track="the Trench Bridge",
            team="allies",
            objective=DECK,
            trigger={"min": [1024, 128, -32], "max": [1408, 384, 128]},
            texture=textures["metal"],
        )
        objectives["radio"] = await run.call(
            "objective_place",
            kind="dynamite",
            name="radio",
            track="the Axis Radio",
            team="axis",
            objective={"min": [64, 224, 0], "max": [128, 288, 64]},
            trigger={"min": [16, 160, 0], "max": [192, 352, 128]},
            texture=textures["metal"],
        )
        for o in objectives.values():
            assert o["summary"]["error"] == 0, o
        for x in range(256, 2560, 512):
            await run.call(
                "entity_place",
                classname="light",
                origin=[x, 256, 224],
                keys={"light": 500, "_color": [1, 0.9, 0.75]},
            )
        await run.call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_axis_respawntime 5",
                "wm_allied_respawntime 5",
                "wm_set_round_timelimit 30",
                "wm_number_of_objectives 3",
                "wm_setwinner 0",
                "wm_set_defending_team 0",
                "wait 50",
                'setautospawn "Axis Rooms" 0',
                'setautospawn "Allied Rooms" 1',
            ],
        )
        await run.call("arena_write", longname="Gates")
        geometry = await run.call("validate")
        validation = await run.call("validate_profile")
        assert geometry["summary"]["error"] == 0, geometry
        assert validation["summary"]["error"] == 0, validation
        await run.call("render_topdown", width=1600, height=400)
        await run.call("map_save")
        compiled = await run.call("compile_map", preset="iterate")
        assert compiled["ok"] and not compiled.get("leaked"), compiled
        bsp = next(p for p in compiled["artifacts"] if p.endswith(".bsp"))
        packed = await run.call("pack_pk3", bsp=bsp)
        assert packed["ok"], packed
        waypoints = await run.call("bot_waypoints_write", cell=16, spacing=128)
        result = {
            "map": str(path),
            "package": packed["packages"][0],
            "waypoints": waypoints,
            "objectives": objectives,
            "profile": validation,
        }
        print(json.dumps({"result": run.artifact("result", result)}), flush=True)


def _newest() -> dict:
    runs = sorted((REPO / "out/dogfood/gates").glob("*/result.json"))
    if not runs:
        raise SystemExit("no gates build under out/dogfood/gates; run the scenario first")
    return json.loads(runs[-1].read_text())


def region(p) -> str:
    """Which room a position is in, by the gates' x (and the trench's depth)."""
    x, z = p[0], p[2]
    if x < WALL["min"][0]:
        return "axis_rooms"
    if x < TRENCH[0]:
        return "wall_hall"
    if x < TRENCH[1]:
        return "deck" if z > -64 else "trench"
    if x < DOOR["min"][0]:
        return "bridge_hall"
    return "allied_rooms"


def timeline(game_log: str, token: str) -> list[dict]:
    """The game log in order: bot samples and objective events, as one list."""
    out = []
    marker = f"NRC_BOTS {token} "
    for line in game_log.splitlines():
        if marker in line:
            out.append({"sample": json.loads(line.split(marker, 1)[1])})
        elif any(k in line for k in (" announce: ", " popup: ", "Objective_Destroyed: ", "Kill: ")):
            out.append({"event": line.strip()})
    return out


def analyse(entries: list[dict]) -> dict:
    """Per phase (between the gates opening), where bots of each team were seen."""
    phase = "before_bridge"
    # The announcements objective_place scripts.
    marks = {
        "the Trench Bridge has been constructed": "bridge_built",
        "the Gate Wall has been destroyed": "wall_destroyed",
    }
    phases: dict[str, dict] = {}
    deaths_in_trench: dict[str, int] = {}
    trench_deaths: dict[str, int] = {}
    near_wall: dict[str, int] = {}
    door_passes = {"red": 0, "blue": 0}
    last: dict[int, tuple] = {}
    for e in entries:
        if "event" in e:
            text = e["event"]
            for needle, name in marks.items():
                if needle in text:
                    if name == "bridge_built" and phase == "before_bridge":
                        phase = "bridge_built"
                    elif name == "wall_destroyed" and phase != "wall_destroyed":
                        phase = "wall_destroyed"
            phases.setdefault(phase, {}).setdefault("events", []).append(text)
            if "MOD_TRIGGER_HURT" in text:
                trench_deaths[phase] = trench_deaths.get(phase, 0) + 1
            continue
        packet = e["sample"]
        row = phases.setdefault(phase, {})
        row["samples"] = row.get("samples", 0) + 1
        for b in packet.get("bots") or []:
            if b.get("dead") or b.get("team") not in ("red", "blue"):
                continue
            pos = b["position"]
            where = region(pos)
            seen = row.setdefault(b["team"], {})
            seen[where] = seen.get(where, 0) + 1
            if where == "trench":
                deaths_in_trench[phase] = deaths_in_trench.get(phase, 0) + 1
            # Pressed against the wall: within 48 units of either face, inside its width.
            if (
                phase != "wall_destroyed"
                and WALL["min"][0] - 48 <= pos[0] <= WALL["max"][0] + 48
                and WALL["min"][1] <= pos[1] <= WALL["max"][1]
            ):
                key = f"{b['team']}_{'west' if pos[0] < WALL['min'][0] else 'east'}"
                near_wall[key] = near_wall.get(key, 0) + 1
            prev = last.get(b["client"])
            if prev and prev[1] == b["team"]:
                a, c = prev[0][0], pos[0]
                if min(a, c) < DOOR["min"][0] <= max(a, c) and math.dist(prev[0], pos) < 800:
                    door_passes[b["team"]] += 1
            last[b["client"]] = (pos, b["team"])
    return {
        "phases": phases,
        "samples_in_trench": deaths_in_trench,
        "trench_deaths": trench_deaths,
        "samples_at_wall_before_it_went": near_wall,
        "door_crossings_between_samples": door_passes,
    }


def bots(seconds: float, port: int, strip: list[str], count: int) -> None:
    os.environ.setdefault("NRC_PROFILE", "etl")
    sys.path.insert(0, str(REPO / "python/src"))
    from nrc_mcp import botnav, botplay

    built = _newest()
    staged = Path(built["waypoints"]["path"])
    spec = botnav.spec("etl")
    wf = botnav.read(staged.read_bytes(), spec["format"])
    removed = 0
    for name in strip:
        bit = int(spec["flags"][name])
        for w in wf.waypoints:
            if w.flags & bit:
                w.flags &= ~bit
                removed += 1
    out = REPO / "out/dogfood/gates-bots"
    out.mkdir(parents=True, exist_ok=True)
    tag = "-".join(["run", *strip]) if strip else "run"
    nav = out / tag / staged.name
    nav.parent.mkdir(parents=True, exist_ok=True)
    nav.write_bytes(botnav.write(wf, spec["format"]))
    report = botplay.run(
        built["package"], "etl", [nav], seconds=seconds, bots=count, port=port, keep=True
    )
    log = Path(report["game_log"]).read_text(errors="replace")
    token = next(
        (
            line.split("NRC_BOTS ", 1)[1].split(" ", 1)[0]
            for line in log.splitlines()
            if "NRC_BOTS " in line
        ),
        "",
    )
    entries = timeline(log, token)
    summary = {
        "stripped": strip,
        "flags_removed": removed,
        "report": {k: v for k, v in report.items() if k != "bots"},
        "bots": report["bots"],
        "analysis": analyse(entries),
    }
    (out / tag / "timeline.json").write_text(json.dumps(entries, indent=1))
    (out / tag / "summary.json").write_text(json.dumps(summary, indent=2))
    # The engine home and add-on folder stay for their logs (report["home"]).
    print(json.dumps(summary["analysis"], indent=2))
    print(json.dumps({"events": report["events"], "summary": str(out / tag / "summary.json")}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bots", action="store_true", help="run bots on the newest build")
    parser.add_argument("--seconds", type=float, default=600)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--count", type=int, default=8)
    parser.add_argument("--strip", action="append", default=[], help="flag to remove (control)")
    args = parser.parse_args()
    if args.bots:
        bots(args.seconds, args.port, args.strip, args.count)
    else:
        asyncio.run(build())
