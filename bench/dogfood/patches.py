"""Patch collision: the analysis's verdict next to the real engine's, lane by lane.

`build` authors a probe map over the MCP stdio API: eight walled lanes, each with one patch
made by `patch_commit` (pipes on the floor, an arch over a doorway, pipes under a prone vent
ceiling, a nonsolid pipe and an inward-facing one). It asks `route_report` whether each lane
can be crossed, compiles and packs the map, then walks every lane in ET: Legacy with
`engine_playtest` and records both answers side by side.

`stock` drops a player onto barrel caps in Oasis — patch cylinders capped by flat patches — and
records the height the engine settles at, which `python/tests/test_patch_collision.py`
compares against the analysis.

    mise run bench:dogfood -- patches build
    mise run bench:dogfood -- patches stock
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import zipfile
from pathlib import Path

from dogfood_session import DogfoodSession

LANE = 256  # lane pitch in y: 224 open, 32 wall
# Stops shots, not players (common.shader: clipshot + nonsolid). A drawn nonsolid shader from
# the base game would do too, but the packer cannot resolve those (no shaderlist in pak0).
NONSOLID = "common/clipshot"


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


def lane_y(k: int) -> float:
    return LANE * k + 112


# (name, stance to cross in, what the engine does, analysis expectation)
LANES = [
    ("pipe_r48_floor", "stand", "blocked", False),
    ("pipe_r8_floor", "stand", "crosses", True),
    ("pipe_r24_floor", "stand", "crosses", False),  # 45-degree diamond: grid slope limitation
    ("arch_doorway", "stand", "crosses", True),
    ("vent_pipe_r16", "prone", "blocked", False),
    ("vent_pipe_r4", "prone", "crosses", True),
    ("pipe_r48_nonsolid", "stand", "crosses", True),
    ("pipe_r48_inward", "stand", "enters, stopped by far side", False),
]


async def build():
    async with DogfoodSession("patches") as run:
        call = run.call
        await call("profile_summary")
        wall_rows = await call("texture_search", query="bunker_sd int_wall01")
        metal_rows = await call("texture_search", query="battery_wall wall02_mid")
        wall = next(r["texture"] for r in wall_rows["textures"] if not r.get("not_wall_material"))
        metal = next(r["texture"] for r in metal_rows["textures"] if not r.get("not_wall_material"))
        path = run.root / "maps/nrc_patchprobe.map"
        await call("map_new", path=str(path), grid=8)
        await call("entity_set_keys", entity=0, keys={"message": "Patch Probe", "_minlight": "40"})

        async def solid(label, ir, texture=wall):
            return await call("solid_commit", label=label, ir=ir, textures={"default": texture})

        async def pipe(label, k, z, radius, shader=metal, facing=None):
            args = {
                "kind": "cylinder",
                "centre": [512, LANE * k, z],
                "radius": radius,
                "length": 224,
                "shader": shader,
                "axis": "y",
                "label": label,
            }
            if facing:
                args["facing"] = facing
            return await call("patch_commit", **args)

        lanes = len(LANES)
        await solid(
            "shell",
            {
                "op": "hollow",
                "solid": box([-32, -32, -32], [1056, LANE * lanes + 32 - 32, 288]),
                "thickness": 32,
            },
        )
        for k in range(lanes - 1):
            await solid(f"partition_{k}", box([0, LANE * k + 224, 0], [1024, LANE * (k + 1), 256]))
        await pipe("pipe_r48_floor", 0, 48, 48)
        await pipe("pipe_r8_floor", 1, 8, 8)
        await pipe("pipe_r24_floor", 2, 24, 24)
        y0 = LANE * 3
        await solid(
            "arch_wall",
            {
                "op": "subtract",
                "from": box([496, y0, 0], [528, y0 + 224, 256]),
                "cut": [box([480, y0 + 48, 0], [544, y0 + 176, 176])],
            },
        )
        await call(
            "patch_commit",
            kind="arch",
            centre=[496, y0 + 112, 112],
            radius=64,
            length=32,
            shader=metal,
            axis="x",
            label="arch_doorway",
        )
        for k in (4, 5):
            await solid(f"vent_roof_{k}", box([384, LANE * k, 48], [640, LANE * k + 224, 256]))
        await pipe("vent_pipe_r16", 4, 48, 16)
        await pipe("vent_pipe_r4", 5, 48, 4)
        await pipe("pipe_r48_nonsolid", 6, 48, 48, shader=NONSOLID)
        await pipe("pipe_r48_inward", 7, 48, 48, facing="in")

        for team, classname, x, angle in (
            ("red", "team_CTF_redspawn", 96, 0),
            ("blue", "team_CTF_bluespawn", 928, 180),
        ):
            await call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=[x, 112, 24],
                keys={"description": f"{team} bay", "spawnflags": 1 if team == "red" else 2},
            )
            for y in (64, 160):
                await call(
                    "entity_place",
                    classname=classname,
                    origin=[x, y, 24],
                    keys={"spawnflags": 3, "angle": angle},
                )
        await call("entity_place", classname="script_multiplayer", origin=[256, 112, 64])
        await call(
            "entity_place",
            classname="info_player_intermission",
            origin=[200, 112, 96],
            keys={"angles": [0, 0, 0], "spawnflags": 3},
        )
        for k in range(lanes):
            for x in (192, 832):
                await call(
                    "entity_place",
                    classname="light",
                    origin=[x, lane_y(k), 200],
                    keys={"light": 500},
                )
        await call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_set_round_timelimit 30",
                "wm_number_of_objectives 1",
                "wm_setwinner 1",
                "wait 50",
                'setautospawn "red bay" 0',
                'setautospawn "blue bay" 1',
            ],
        )

        analysis = {}
        for k, (name, stance, _, _) in enumerate(LANES):
            report = await call(
                "route_report",
                start=[320, lane_y(k), 0],
                end=[760, lane_y(k), 0],
                stance=stance,
                cell=8,
            )
            analysis[name] = {
                "reachable": report.get("reachable"),
                "status": report.get("status"),
                "distance": report.get("distance"),
            }
        run.artifact("analysis", analysis)

        await call("map_save")
        compiled = await call("compile_map", preset="iterate")
        assert compiled["ok"] and not compiled["leaked"], compiled
        bsp = next(p for p in compiled["artifacts"] if p.endswith(".bsp"))
        package = await call("pack_pk3", bsp=bsp)
        assert package["ok"], package
        pk3 = package["packages"][0]

        engine = {}
        for k, (name, stance, _, _) in enumerate(LANES):
            engine[name] = await walk_lane(run, pk3, k, name, stance)
        summary = {
            "package": pk3,
            "lanes": [
                {
                    "lane": name,
                    "stance": stance,
                    "analysis_reachable": analysis[name]["reachable"],
                    "analysis_expected": expected,
                    "engine_expected": engine_expected,
                    "engine": engine[name],
                }
                for name, stance, engine_expected, expected in LANES
            ],
        }
        run.artifact("summary", summary)
        print(json.dumps(summary, indent=2))


async def walk_lane(run, pk3: str, k: int, name: str, stance: str) -> dict:
    """One engine run per lane, so one lane's surprise cannot hide the others."""
    y = lane_y(k)
    steps = [
        {"action": "join", "team": "red", "role": "engineer", "timeout": 60},
        {"action": "position", "origin": [320, y, 24], "angles": [0, 0, 0]},
    ]
    if stance == "prone":
        steps.append(
            {
                "action": "hold",
                "controls": ["prone"],
                "until": [{"scope": "player", "field": "stance", "value": "prone"}],
                "timeout": 5,
            }
        )
    steps += [
        {"action": "hold", "controls": ["forward"], "seconds": 6, "timeout": 10},
        {"action": "screenshot", "name": name},
        {
            "action": "wait",
            "until": [{"scope": "player", "field": "alive", "value": True}],
            "timeout": 5,
        },
    ]
    response = await run.call(
        "engine_playtest",
        pk3=pk3,
        scenario={"steps": steps},
        profile_id="etl",
        timeout_seconds=120,
        include_images=False,
    )
    result = response
    if isinstance(response, dict) and "result" in response and isinstance(response["result"], list):
        result = json.loads(response["result"][0])
    final = (result.get("steps") or [{}])[-1].get("state", {}).get("player", {})
    position = final.get("position")
    return {
        "ok": result.get("ok"),
        "error": result.get("error"),
        "final_position": position,
        "final_stance": final.get("stance"),
        "crossed": bool(position and position[0] > 700),
        "result": result.get("result_path"),
    }


# Oasis barrel caps: flat patch facets 44 units above the ground around them.
OASIS_CAPS = [(9102, 5258, -372), (9129, 5248, -372), (9147, 5224, -372), (9113, 5223, -372)]


async def stock():
    base = Path(os.environ["NRC_FS_BASEPATH"]) / "etmain" / "pak0.pk3"
    async with DogfoodSession("patches-stock") as run:
        pk3 = run.root / "zz_oasis_probe.pk3"
        with zipfile.ZipFile(base) as src, zipfile.ZipFile(pk3, "w") as dst:
            dst.writestr("maps/oasis.bsp", src.read("maps/oasis.bsp"))
        steps = [{"action": "join", "team": "red", "role": "engineer", "timeout": 60}]
        for i, (x, y, z) in enumerate(OASIS_CAPS):
            steps += [
                {"action": "position", "origin": [x, y, z + 25], "angles": [0, 0, 0]},
                # Settled: below where it was dropped, and no longer moving.
                {
                    "action": "wait",
                    "until": [
                        {"scope": "player", "field": "speed", "value": 1, "op": "lt"},
                        {"scope": "player", "field": "position.2", "value": z + 24.5, "op": "lt"},
                    ],
                    "timeout": 10,
                },
                {"action": "screenshot", "name": f"cap_{i}"},
            ]
        response = await run.call(
            "engine_playtest",
            pk3=str(pk3),
            scenario={"steps": steps},
            profile_id="etl",
            timeout_seconds=180,
            include_images=False,
        )
        result = response
        if isinstance(response, dict) and isinstance(response.get("result"), list):
            result = json.loads(response["result"][0])
        settled = [
            row["state"]["player"]["position"]
            for row in result["steps"]
            if row["action"]["action"] == "wait"
        ]
        run.artifact("settled", {"caps": OASIS_CAPS, "settled": settled, "ok": result["ok"]})
        print(json.dumps({"ok": result["ok"], "settled": settled}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("mode", choices=["build", "stock"])
    args = parser.parse_args()
    asyncio.run(build() if args.mode == "build" else stock())
