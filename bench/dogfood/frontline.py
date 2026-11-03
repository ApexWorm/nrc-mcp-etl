"""Build a hall with a forward spawn flag, a neutral command post and an Axis-only door,
using objective_place for the objectives and only MCP calls for everything else."""

from __future__ import annotations

import argparse
import asyncio
import json

from dogfood_session import DogfoodSession


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def main(one_way: bool):
    async with DogfoodSession("frontline-one-way" if one_way else "frontline") as run:
        textures = {}
        for label, query in (("floor", "floor"), ("metal", "metal")):
            choices = await run.call("texture_search", query=query, limit=12)
            assert choices["textures"], choices
            textures[label] = choices["textures"][0]["texture"]
        textures["wall"] = "bunker_sd/int_wall01"
        run.artifact("materials", textures)
        path = run.root / "maps/nrc_frontline.map"
        await run.call("map_new", path=str(path))
        await run.call(
            "entity_set_keys",
            entity=0,
            keys={"message": "Frontline Hall", "_ambient": "12", "_minlight": "6"},
        )
        # One hall, 3072 x 768: Allied camp west, Axis base east behind a partition.
        shell = {"op": "hollow", "solid": box([-32, -32, -32], [3104, 800, 288]), "thickness": 32}
        await run.call(
            "solid_commit",
            ir=shell,
            label="hall_shell",
            textures={"default": textures["wall"], "top": textures["floor"]},
        )
        partition = {
            "op": "subtract",
            "from": box([2400, 0, 0], [2432, 768, 256]),
            "cut": [
                box([2384, 64, -16], [2448, 256, 160]),
                box([2384, 512, -16], [2448, 640, 128]),
            ],
        }
        await run.call(
            "solid_commit", ir=partition, label="partition", textures={"default": textures["wall"]}
        )
        # The Axis-only door fills the northern gap; Axis open it with the use key.
        door = await run.call(
            "solid_commit",
            ir=box([2408, 512, 0], [2424, 640, 128]),
            label="axis_door",
            textures={"default": textures["metal"]},
            target_classname="func_door",
            new_entity=True,
        )
        await run.call(
            "entity_set_keys",
            entity=door["entity"],
            keys={
                "targetname": "axisdoor",
                "allowteams": "axis,cvops",
                "angle": -1,
                "speed": 200,
                "wait": 2,
                "type": 5,
            },
        )

        for cls, x, angle, flag, description in [
            ("team_CTF_bluespawn", 192, 0, 2, "Allied Camp"),
            ("team_CTF_redspawn", 2880, 180, 1, "Axis Base"),
        ]:
            await run.call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=[x, 384, 24],
                keys={"description": description, "spawnflags": flag},
            )
            for y in (192, 288, 384, 480, 576):
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
            origin=[1536, 96, 200],
            keys={"angles": [20, 90, 0], "spawnflags": 3},
        )

        flag = await run.call(
            "objective_place",
            kind="checkpoint_one_way" if one_way else "checkpoint",
            name="fwdflag",
            track="Forward Bunker",
            team="axis",
            points={
                "flag": [1536, 384, 0],
                "spawns": [
                    {"origin": [1408, 192, 24], "angle": 0},
                    {"origin": [1664, 192, 24], "angle": 180},
                    {"origin": [1408, 576, 24], "angle": 0},
                    {"origin": [1664, 576, 24], "angle": 180},
                ],
            },
        )
        assert flag["summary"]["error"] == 0, flag
        cp = await run.call(
            "objective_place",
            kind="command_post",
            name="cp",
            track="the Command Post",
            team="neutral",
            objective={"min": [992, 640, 0], "max": [1056, 704, 48]},
            trigger={"min": [944, 592, 0], "max": [1104, 752, 112]},
            texture=textures["metal"],
        )
        assert cp["summary"]["error"] == 0, cp

        for x in range(256, 3072, 512):
            await run.call(
                "entity_place",
                classname="light",
                origin=[x, 384, 224],
                keys={"light": 500, "_color": [1, 0.9, 0.75]},
            )
        await run.call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_axis_respawntime 2",
                "wm_allied_respawntime 2",
                "wm_set_round_timelimit 15",
                "wm_setwinner 0",
                "wait 50",
                'setautospawn "Allied Camp" 1',
                'setautospawn "Axis Base" 0',
            ],
        )
        validation = await run.call("validate_profile")
        assert validation["summary"]["error"] == 0, validation
        await run.call("render_topdown", width=1200, height=400)
        await run.call("arena_write", longname="Frontline Hall")
        await run.call("map_save")
        compiled = await run.call("compile_map", preset="iterate")
        assert compiled["ok"] and not compiled.get("leaked"), compiled
        bsp = next(p for p in compiled["artifacts"] if p.endswith(".bsp"))
        packed = await run.call("pack_pk3", bsp=bsp)
        assert packed["ok"], packed
        shipped = await run.call("ship_check", pk3=packed["packages"][0])
        assert shipped["summary"]["error"] == 0, shipped
        result = {
            "map": str(path),
            "package": packed["packages"][0],
            "profile": validation,
            "objectives": {"flag": flag, "command_post": cp},
            "ship_check": shipped,
        }
        print(
            json.dumps({"result": run.artifact("result", result), "package": result["package"]}),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "--one-way", action="store_true", help="only Allies can raise the flag; Axis lower it"
    )
    asyncio.run(main(parser.parse_args().one_way))
