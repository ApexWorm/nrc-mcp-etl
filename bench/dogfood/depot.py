"""Build a two-route dynamite depot using only MCP authoring calls."""

from __future__ import annotations

import argparse
import asyncio
import json

from dogfood_session import DogfoodSession


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def main():
    async with DogfoodSession("depot") as run:
        await run.call("profile_summary")
        await run.call("solid_help")
        textures = {}
        for label, query in (
            ("wall", "bunker wall"),
            ("floor", "floor"),
            ("crate", "wood"),
            ("metal", "metal"),
        ):
            choices = await run.call("texture_search", query=query, limit=12)
            assert choices["textures"], choices
            textures[label] = choices["textures"][0]["texture"]
        textures["wall"] = "bunker_sd/int_wall01"
        run.artifact("materials", textures)
        path = run.root / "maps/nrc_depot.map"
        await run.call("map_new", path=str(path))
        await run.call(
            "entity_set_keys",
            entity=0,
            keys={"message": "Relay Depot", "_ambient": "12", "_minlight": "6"},
        )
        shell = {"op": "hollow", "solid": box([-32, -32, -32], [2080, 1312, 416]), "thickness": 32}
        await run.call("solid_compile", ir=shell)
        await run.call(
            "solid_commit",
            ir=shell,
            label="warehouse_shell",
            textures={"default": textures["wall"], "top": textures["floor"]},
        )
        divider = {
            "op": "subtract",
            "from": box([1008, 0, 0], [1040, 1280, 384]),
            "cut": [
                box([992, 192, -16], [1056, 448, 256]),
                box([992, 960, -16], [1056, 1152, 160]),
            ],
        }
        await run.call(
            "solid_preview", ir=divider, textures={"default": textures["wall"]}, view="sheet"
        )
        await run.call(
            "solid_commit",
            ir=divider,
            label="route_divider",
            textures={"default": textures["wall"]},
        )
        await run.call(
            "patch_commit",
            kind="arch",
            centre=[1008, 320, 128],
            radius=128,
            length=32,
            shader=textures["metal"],
            axis="x",
        )

        # Cargo creates offset approaches; the northern loading aisle is the longer flank.
        for i, (x, y, sx, sy, height) in enumerate(
            [
                (448, 448, 160, 128, 96),
                (736, 704, 128, 160, 128),
                (384, 960, 128, 128, 96),
                (1216, 192, 128, 192, 96),
                (1408, 640, 160, 128, 112),
                (1664, 960, 128, 128, 96),
            ]
        ):
            await run.call(
                "solid_commit",
                ir=box([x, y, 0], [x + sx, y + sy, height]),
                label=f"cargo_{i}",
                textures={"default": textures["crate"], "detail": True},
            )
        for i, x in enumerate((384, 896, 1408, 1920)):
            await run.call(
                "solid_commit",
                ir=box([x, 16, 0], [x + 32, 48, 384]),
                label=f"support_{i}",
                textures={"default": textures["metal"], "detail": True},
            )

        # Both teams get six separated slots rather than one placeholder spawn.
        for _team, cls, x, angle, marker_flag, description in [
            ("blue", "team_CTF_bluespawn", 160, 0, 2, "Allied Loading Bay"),
            ("red", "team_CTF_redspawn", 1920, 180, 1, "Axis Relay Room"),
        ]:
            await run.call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=[x, 640, 24],
                keys={"description": description, "spawnflags": marker_flag},
            )
            for y in (480, 576, 672, 768, 864, 960):
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
            origin=[384, 256, 160],
            keys={"angles": [12, 0, 0], "spawnflags": 3},
        )
        for x in (320, 768, 1280, 1792):
            for y in (320, 960):
                await run.call(
                    "entity_place",
                    classname="light",
                    origin=[x, y, 320],
                    keys={"light": 600, "_color": [1, 0.85, 0.65]},
                )

        await run.call(
            "objective_place",
            kind="dynamite",
            name="relay",
            track="Axis relay transmitter",
            team="axis",
            objective={"min": [1696, 608, 0], "max": [1760, 672, 96]},
            trigger={"min": [1600, 512, 0], "max": [1856, 768, 160]},
            texture=textures["metal"],
        )
        await run.call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_axis_respawntime 20",
                "wm_allied_respawntime 15",
                "wm_set_round_timelimit 8",
                "wm_number_of_objectives 1",
                "wm_setwinner 0",
                "wait 50",
                'setautospawn "Axis Relay Room" 0',
                'setautospawn "Allied Loading Bay" 1',
                "wm_objective_status 1 0 0",
                "wm_objective_status 1 1 0",
            ],
        )
        await run.call(
            "script_set_event",
            block="game_manager",
            event="trigger",
            params=["relay_destroyed"],
            actions=[
                'wm_announce "DOGFOOD: relay destroyed by dynamite"',
                "wm_objective_status 1 0 2",
                "wm_objective_status 1 1 1",
                "wm_setwinner 1",
                "wait 1500",
                "wm_endround",
            ],
        )
        # The explosive is freed after death. Its delayed actions must run on a persistent
        # script owner, or the wait never resumes and the round cannot reach intermission.
        await run.call(
            "script_set_event",
            block="relay",
            event="death",
            actions=["trigger game_manager relay_destroyed"],
        )
        await run.call("render_topdown", width=1200, height=800)
        await run.call(
            "render_camera", eye=[384, 320, 112], target=[1728, 640, 48], width=1200, height=800
        )
        await run.call(
            "render_player_eye", position=[256, 320, 0], yaw_deg=0, width=1200, height=800
        )
        validation = await run.call("validate_profile")
        assert validation["summary"]["error"] == 0, validation
        geometric = await run.call("validate")
        balance = await run.call("balance_report", cell=32, team_sizes={"red": 6, "blue": 6})
        safety = await run.call("spawn_safety", cell=32)
        light = await run.call("lighting_report", cell=32)

        await run.call(
            "arena_write",
            longname="Relay Depot",
            keys={
                "briefing": "Allied engineers must destroy the Axis relay. A high main arch and a northern flank connect the loading bays."
            },
        )
        await run.call(
            "objdata_write",
            descriptions={
                "axis": "Defend the relay transmitter.",
                "allied": "Destroy the relay transmitter with dynamite.",
                "neutral": "Two routes through an industrial relay depot.",
            },
            objectives={"axis": ["Defend the transmitter"], "allied": ["Destroy the transmitter"]},
        )
        await run.call("commandmap_make")
        await run.call("levelshot_make", position=[256, 320, 0], yaw_deg=0)
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
            "geometry": geometric,
            "profile": validation,
            "balance": balance,
            "spawn_safety": safety,
            "lighting": light,
            "ship_check": shipped,
            "compile": compiled,
            "engine_probe": {
                "team": "allies",
                "class": 2,
                "position": [1640, 640, 24],
                "pitch": 65,
                "yaw": 0,
                "objective": "relay",
                "event_marker": "DOGFOOD: relay destroyed by dynamite",
            },
        }
        print(
            json.dumps({"result": run.artifact("result", result), "package": result["package"]}),
            flush=True,
        )


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, allow_abbrev=False).parse_args()
    asyncio.run(main())
