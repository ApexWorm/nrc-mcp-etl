"""Build a convoy yard with an escorted vehicle and stealable documents over the MCP wire."""

from __future__ import annotations

import argparse
import asyncio
import json

from dogfood_session import DogfoodSession


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def main():
    async with DogfoodSession("convoy") as run:
        await run.call("profile_summary")
        wall = (await run.call("texture_search", query="bunker_sd int_wall01"))["textures"][0][
            "texture"
        ]
        floor = (await run.call("texture_search", query="battery_wall wall02_mid"))["textures"][0][
            "texture"
        ]
        metal_search = await run.call("texture_search", query="metal", limit=40)
        metal = next(
            r["texture"] for r in metal_search["textures"] if not r.get("not_wall_material")
        )
        run.artifact("materials", {"wall": wall, "floor": floor, "metal": metal})

        map_path = run.root / "maps/nrc_convoy.map"
        await run.call("map_new", path=str(map_path))
        await run.call(
            "entity_set_keys",
            entity=0,
            keys={"message": "Convoy Yard", "_ambient": 14, "_minlight": 6},
        )
        # One yard: the floor top is z=0, the interior 2048 x 1024 x 368.
        shell = {"op": "hollow", "solid": box([-16, -16, -16], [2064, 1040, 384]), "thickness": 16}
        await run.call("solid_commit", ir=shell, label="shell", textures={"default": wall})
        await run.call(
            "solid_commit",
            label="yard_floor",
            ir=box([0, 0, -16], [2048, 1024, 0]),
            textures={"default": floor},
        )

        # The Allies escort a truck west from their end, round a bend, to the Axis gate.
        path = await run.call(
            "path_place",
            name="convoy_path",
            points=[
                [1696, 512, 32],
                {"at": [1296, 512, 32], "controls": [[1000, 512, 32]]},
                [1000, 808, 32],
                [600, 808, 32],
            ],
            max_segment=128,
        )
        assert not [f for f in path["findings"] if f["code"].startswith("ETL_SPLINE")], path
        escort = await run.call(
            "objective_place",
            kind="escort",
            name="truck",
            track="the Supply Truck",
            team="allies",
            texture=metal,
            path=path["start"],
            options={"speed": 96},
            objective=box([1648, 472, 0], [1744, 552, 64]),
            trigger=box([1536, 352, 0], [1856, 672, 128]),
        )
        # The Axis keep their orders in the west; the Allies carry them to the east radio.
        docs = await run.call(
            "objective_place",
            kind="steal",
            name="orders",
            track="the Axis Orders",
            team="axis",
            objective={"origin": [300, 200, 24]},
            trigger=box([1856, 96, 0], [2000, 256, 128]),
        )
        # A lamp the stolen event hides, so a playtest can see that event run.
        await run.call(
            "solid_commit",
            label="orders_lamp",
            ir=box([256, 96, 96], [288, 128, 128]),
            textures={"default": metal},
            target_classname="func_static",
            new_entity=True,
        )
        lamp = (await run.call("query_entities", classname="func_static"))["entities"][0]["index"]
        await run.call(
            "entity_set_keys",
            entity=lamp,
            keys={"targetname": "orders_lamp", "scriptname": "orders_lamp"},
        )

        for classname, marker, x, flag, angle in [
            ("team_CTF_redspawn", "Axis Gate", 160, 1, 0),
            ("team_CTF_bluespawn", "Allied Depot", 1936, 2, 180),
        ]:
            await run.call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=[x, 512, 24],
                keys={"description": marker, "spawnflags": flag},
            )
            for y in (400, 480, 560, 640):
                await run.call(
                    "entity_place",
                    classname=classname,
                    origin=[x, y, 24],
                    keys={"angle": angle, "spawnflags": 3},
                )
        await run.call("entity_place", classname="script_multiplayer", origin=[1024, 64, 64])
        await run.call(
            "entity_place",
            classname="info_player_intermission",
            origin=[1024, 160, 256],
            keys={"angles": [30, 90, 0], "spawnflags": 3},
        )
        for x in (256, 768, 1280, 1792):
            for y in (256, 768):
                await run.call(
                    "entity_place",
                    classname="light",
                    origin=[x, y, 300],
                    keys={"light": 600, "_color": [1, 0.94, 0.82]},
                )

        await run.call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_axis_respawntime 20",
                "wm_allied_respawntime 15",
                "wm_set_round_timelimit 10",
                "wm_number_of_objectives 2",
                "wm_setwinner 0",
                "wm_set_defending_team 0",
                "wait 50",
                'setautospawn "Axis Gate" 0',
                'setautospawn "Allied Depot" 1',
            ],
        )
        await run.call(
            "script_set_event",
            block="game_manager",
            event="trigger",
            params=["allies_win"],
            actions=["wm_setwinner 1", "wait 500", "wm_endround"],
        )
        await run.call(
            "script_set_event",
            block="truck",
            event="trigger",
            params=["arrived"],
            actions=[
                'wm_announce "The Supply Truck has reached the Axis gate!"',
                "wm_objective_status 1 1 1",
                "wm_objective_status 1 0 2",
                "trigger game_manager allies_win",
            ],
        )
        await run.call(
            "script_set_event",
            block="orders",
            event="trigger",
            params=["stolen"],
            actions=[
                'wm_announce "The Allies have stolen the Axis Orders!"',
                "setstate orders_lamp invisible",
            ],
        )
        await run.call(
            "script_set_event",
            block="orders",
            event="trigger",
            params=["returned"],
            actions=[
                'wm_announce "The Axis have returned their Orders!"',
                "setstate orders_lamp default",
            ],
        )
        await run.call(
            "script_set_event",
            block="orders_deliver",
            event="death",
            actions=[
                'wm_announce "The Allies have transmitted the Axis Orders!"',
                "wm_objective_status 2 1 1",
                "wm_objective_status 2 0 2",
                "trigger game_manager allies_win",
            ],
        )
        await run.call(
            "arena_write",
            longname="Convoy Yard",
            keys={
                "briefing": "Allies escort the truck to the Axis gate, or steal the Axis orders.",
                "timelimit": "10",
                "axisRespawnTime": "20",
                "alliedRespawnTime": "15",
            },
        )
        await run.call(
            "objdata_write",
            descriptions={
                "axis": "Stop the truck and keep your orders.",
                "allied": "Escort the truck to the gate, or steal the Axis orders.",
                "neutral": "A yard with an escorted truck and stealable orders.",
            },
            objectives={
                "axis": ["Stop the Supply Truck", "Defend the Axis Orders"],
                "allied": ["Escort the Supply Truck", "Steal the Axis Orders"],
            },
        )
        script = await run.call("script_show")
        geometry = await run.call("validate")
        validation = await run.call("validate_profile")
        assert geometry["summary"]["error"] == 0, geometry
        assert validation["summary"]["error"] == 0, validation
        saved = await run.call("map_save")
        compiled = await run.call("compile_map", preset="iterate")
        assert compiled["ok"] and not compiled.get("leaked"), compiled
        bsp = next(p for p in compiled["artifacts"] if p.endswith(".bsp"))
        package = await run.call("pack_pk3", bsp=bsp)
        assert package["ok"], package
        ship = await run.call("ship_check", target=bsp, pk3=package["packages"][0])
        assert ship["summary"]["error"] == 0, ship
        summary = {
            "map": str(map_path),
            "project": str(run.root),
            "package": package["packages"][0],
            "path": path,
            "escort": escort,
            "steal": docs,
            "script": script,
            "profile": validation,
            "save": saved,
            "compile": compiled,
            "ship": ship,
        }
        out = run.artifact("summary", summary)
        print(json.dumps({"summary": out, "package": package["packages"][0]}, indent=2))


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, allow_abbrev=False).parse_args()
    asyncio.run(main())
