"""Build a jump-and-climb course using the real MCP stdio API, and seed its bot waypoints.

Axis spawn on the floor of a long hall; the only way on is a 40-unit ledge to jump up, then
a 48-unit gap over a fatal shaft to jump across, then a 192-unit wall with a ladder, at the
top of which the Allies spawn beside the radio the Axis must destroy. Walking alone reaches
none of it, so a bot that gets to the radio has used every one of the three.

`--bots PORT` also runs `bot_playtest` on the packed map with the seeded waypoints.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from dogfood_session import DogfoodSession

LEDGE = 40  # zone 1 floor at 0; within the jump's rise less the profile's margin
GAP = (880, 928)  # the shaft, x; 48 wide
UPPER = 232  # the top floor, 192 above the gap's far side
SHAFT_FLOOR = -528  # 568 below the ledge: a fall there kills


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def build(bots_port: int | None, seconds: float, bots: int):
    async with DogfoodSession("traversal") as run:
        call = run.call
        await call("profile_summary")
        wall_rows = await call("texture_search", query="bunker_sd int_wall01")
        metal_rows = await call("texture_search", query="battery_wall wall02_mid")
        wall = next(r["texture"] for r in wall_rows["textures"] if not r.get("not_wall_material"))
        metal = next(r["texture"] for r in metal_rows["textures"] if not r.get("not_wall_material"))
        path = run.root / "maps/nrc_traversal.map"
        await call("map_new", path=str(path), grid=8)
        await call(
            "entity_set_keys", entity=0, keys={"message": "Traversal Course", "_minlight": "16"}
        )

        async def solid(label, ir, texture=wall, detail=False):
            return await call(
                "solid_commit", label=label, ir=ir, textures={"default": texture, "detail": detail}
            )

        await solid(
            "shell",
            {"op": "hollow", "solid": box([-32, -32, -560], [1632, 288, 448]), "thickness": 32},
        )
        await solid("start_floor", box([0, 0, -528], [512, 256, 0]))
        await solid("ledge", box([512, 0, -528], [GAP[0], 256, LEDGE]), metal)
        await solid("far_side", box([GAP[1], 0, -528], [1216, 256, LEDGE]), metal)
        await solid("upper_floor", box([1216, 0, -528], [1600, 256, UPPER]))
        # The ladder: a thin brush of the base game's ladder shader against the upper wall.
        await solid("ladder", box([1200, 96, LEDGE], [1216, 160, UPPER]), "common/ladder", True)
        await solid("ladder_rungs", box([1208, 96, LEDGE], [1216, 160, UPPER]), metal, True)

        spawn_ids = {}
        for team, classname, xs, z, marker, flag, angle in (
            ("red", "team_CTF_redspawn", [64, 128, 192], 24, [128, 128, 24], 1, 0),
            (
                "blue",
                "team_CTF_bluespawn",
                [1472, 1536],
                UPPER + 24,
                [1504, 128, UPPER + 24],
                2,
                180,
            ),
        ):
            await call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=marker,
                keys={"description": f"{team} start", "spawnflags": flag},
            )
            spawn_ids[team] = []
            for x in xs:
                for y in (64, 128, 192):
                    placed = await call(
                        "entity_place",
                        classname=classname,
                        origin=[x, y, z],
                        keys={"spawnflags": 3, "angle": angle},
                    )
                    spawn_ids[team].append(placed["entity"])
        await call("entity_place", classname="script_multiplayer", origin=[256, 128, 64])
        await call(
            "entity_place",
            classname="info_player_intermission",
            origin=[256, 128, 160],
            keys={"angles": [20, 0, 0], "spawnflags": 3},
        )
        for x, z in ((192, 200), (704, 240), (1072, 240), (1408, 400)):
            await call("entity_place", classname="light", origin=[x, 128, z], keys={"light": 700})
        await call("entity_place", classname="light", origin=[904, 128, -300], keys={"light": 300})
        target = await call(
            "objective_place",
            kind="dynamite",
            name="course_radio",
            track="Course Radio",
            team="allies",
            objective={"min": [1328, 96, UPPER], "max": [1392, 160, UPPER + 64]},
            trigger={"min": [1280, 48, UPPER], "max": [1440, 208, UPPER + 128]},
            texture=metal,
        )
        objective_id = next(e["entity"] for e in target["entities"] if e["role"] == "trigger")
        await call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_axis_respawntime 5",
                "wm_allied_respawntime 5",
                "wm_set_round_timelimit 10",
                "wm_number_of_objectives 1",
                "wm_setwinner 1",
                "wait 50",
                'setautospawn "red start" 0',
                'setautospawn "blue start" 1',
            ],
        )
        await call(
            "script_set_event",
            block="course_radio",
            event="death",
            actions=['wm_announce "Course radio destroyed"', "wm_setwinner 0", "wm_endround"],
        )

        geometry = await call("validate")
        profile = await call("validate_profile")
        run.artifact("validation", {"geometry": geometry, "profile": profile})
        assert geometry["summary"]["error"] == 0, geometry
        assert profile["summary"]["error"] == 0, profile
        await call("render_topdown", overlay="structural", width=1400, height=500)
        await call(
            "render_camera", eye=[64, 128, 64], target=[1200, 128, 120], width=1200, height=600
        )

        # The walk grid alone reaches nothing past the ledge: the jumps and the ladder are
        # what join the course, and the seeded file says so.
        stages = [
            {
                "name": "start to radio",
                "spawn_entities": spawn_ids["red"],
                "objective_entities": [objective_id],
            }
        ]
        walk = await call("balance_report", cell=16, stance="stand", stages=stages)
        route = walk["reinforcement_balance"]["stages"][0]["routes"][0]
        assert not route["reachable"], route

        await call(
            "arena_write", longname="Traversal Course", keys={"briefing": "Jump, jump, climb."}
        )
        await call(
            "objdata_write",
            descriptions={
                "axis": "Jump the ledge and the shaft, climb the ladder, destroy the radio.",
                "allied": "Defend the course radio.",
                "neutral": "A jump-and-climb course.",
            },
            objectives={
                "axis": ["Destroy the Course Radio"],
                "allied": ["Defend the Course Radio"],
            },
        )
        await call("commandmap_make", cut_height=64)
        await call("levelshot_make", position=[64, 128, 0], yaw_deg=0)
        saved = await call("map_save")
        waypoints = await call("bot_waypoints_write", cell=16, detail="full")
        kinds = sorted(j["kind"] for j in waypoints["jumps"])
        climbs = [row for row in waypoints["ladders"] if "foot" in row]
        assert {"ledge", "gap"} <= set(kinds), waypoints["jumps"]
        assert len(climbs) == 1, waypoints["ladders"]
        assert waypoints["components"]["count"] == 1, waypoints["components"]
        (radio,) = waypoints["objectives"]
        assert radio["reached_from_red_spawns"] and radio["reached_from_blue_spawns"], radio

        compile_result = await call("compile_map", preset="iterate")
        assert compile_result["ok"] and not compile_result["leaked"], compile_result
        bsp = next(p for p in compile_result["artifacts"] if p.endswith(".bsp"))
        package = await call("pack_pk3", bsp=bsp)
        assert package["ok"], package
        shipping = await call("ship_check", pk3=package["packages"][0])
        assert shipping["summary"]["error"] == 0, shipping
        playtest = None
        if bots_port is not None:
            playtest = await call(
                "bot_playtest",
                pk3=package["packages"][0],
                waypoints=waypoints["path"],
                seconds=seconds,
                bots=bots,
                port=bots_port,
                detail="full",
            )
            assert playtest["waypoints_loaded"] and not playtest["load_errors"], playtest
        summary = {
            "source": str(path),
            "root": str(run.root),
            "saved": saved,
            "walk_route": route,
            "jumps": waypoints["jumps"],
            "ladders": waypoints["ladders"],
            "flag_counts": waypoints["flag_counts"],
            "waypoint_file": waypoints["path"],
            "package": package,
            "shipping": shipping,
            "playtest": playtest,
        }
        run.artifact("summary", summary)
        print(
            json.dumps(
                {
                    "root": str(run.root),
                    "jumps": kinds,
                    "ladders": len(climbs),
                    "package": package["packages"],
                    "waypoints": waypoints["path"],
                    "playtest_home": playtest and playtest.get("home"),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--bots", type=int, metavar="PORT", help="also run bot_playtest on PORT")
    parser.add_argument("--seconds", type=float, default=300)
    parser.add_argument("--count", type=int, default=8, help="bots to add")
    args = parser.parse_args()
    asyncio.run(build(args.bots, args.seconds, args.count))
