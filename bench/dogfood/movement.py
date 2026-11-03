"""Build a three-route movement bunker using the real MCP stdio API.

Standing players take the stair/arch route; crouching unlocks a shorter tunnel;
prone unlocks the direct vent. This is an authored experiment, not an engine replay.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from dogfood_session import DogfoodSession


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def build():
    async with DogfoodSession("movement") as run:
        call = run.call
        await call("profile_summary")
        await call("solid_help")
        wall_rows = await call("texture_search", query="bunker_sd int_wall01")
        metal_rows = await call("texture_search", query="battery_wall wall02_mid")
        wall = next(r["texture"] for r in wall_rows["textures"] if not r.get("not_wall_material"))
        metal = next(r["texture"] for r in metal_rows["textures"] if not r.get("not_wall_material"))
        path = run.root / "maps/nrc_movement.map"
        await call("map_new", path=str(path), grid=8)
        await call(
            "entity_set_keys", entity=0, keys={"message": "Movement Bunker", "_minlight": "12"}
        )

        async def solid(label, ir, texture=wall, detail=False):
            return await call(
                "solid_commit", label=label, ir=ir, textures={"default": texture, "detail": detail}
            )

        await solid(
            "shell",
            {"op": "hollow", "solid": box([-32, -32, -32], [1568, 800, 288]), "thickness": 32},
        )
        for label, lo, hi in (
            ("north_partition", [384, 240, 0], [1152, 272, 256]),
            ("south_partition", [384, 496, 0], [1152, 528, 256]),
            ("crouch_roof", [384, 272, 56], [1152, 496, 256]),
            ("prone_roof", [384, 528, 48], [1152, 768, 256]),
        ):
            await solid(label, box(lo, hi))
        await solid(
            "arch_wall",
            {
                "op": "subtract",
                "from": box([736, 0, 0], [800, 240, 256]),
                "cut": [box([720, 48, 0], [816, 208, 176])],
            },
        )
        # Broad, shallow steps form a low bridge along the standing lane.
        for number, (x, height) in enumerate(
            ((384, 16), (448, 32), (512, 48), (576, 32), (640, 16))
        ):
            await solid(f"step_{number}", box([x, 0, 0], [x + 64, 240, height]), metal, True)
        await call(
            "patch_commit",
            kind="arch",
            centre=[736, 128, 96],
            radius=80,
            length=64,
            shader=metal,
            axis="x",
            label="door_arch",
        )
        # Decorative columns stay outside the traversal lanes.
        for x in (64, 1472):
            await call(
                "patch_commit",
                kind="cylinder",
                centre=[x, 384, 0],
                radius=24,
                length=256,
                shader=metal,
                label=f"column_{x}",
            )
            await solid(
                f"column_clip_{x}", box([x - 24, 360, 0], [x + 24, 408, 256]), "common/clip", True
            )

        spawn_ids = {}
        for team, classname, xs, ys, marker, flag, angle in (
            ("red", "team_CTF_redspawn", [96, 160], [544, 608, 672], [160, 640, 24], 1, 0),
            ("blue", "team_CTF_bluespawn", [1376, 1440], [64, 128, 192], [1376, 128, 24], 2, 180),
        ):
            await call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=marker,
                keys={"description": f"{team} training bay", "spawnflags": flag},
            )
            spawn_ids[team] = []
            for x in xs:
                for y in ys:
                    placed = await call(
                        "entity_place",
                        classname=classname,
                        origin=[x, y, 24],
                        keys={"spawnflags": 3, "angle": angle},
                    )
                    spawn_ids[team].append(placed["entity"])
        await call("entity_place", classname="script_multiplayer", origin=[256, 384, 64])
        await call(
            "entity_place",
            classname="info_player_intermission",
            origin=[240, 600, 96],
            keys={"angles": [0, 315, 0], "spawnflags": 3},
        )
        for x in (192, 640, 960, 1344):
            await call("entity_place", classname="light", origin=[x, 128, 200], keys={"light": 700})
        for x in (192, 1344):
            await call("entity_place", classname="light", origin=[x, 608, 200], keys={"light": 700})
        for x in (512, 1024):
            await call("entity_place", classname="light", origin=[x, 384, 24], keys={"light": 150})
            await call("entity_place", classname="light", origin=[x, 640, 16], keys={"light": 120})
        target = await call(
            "objective_place",
            kind="dynamite",
            name="training_radio",
            track="Training Radio",
            team="allies",
            objective={"min": [1392, 608, 0], "max": [1456, 672, 64]},
            trigger={"min": [1328, 560, 0], "max": [1504, 720, 128]},
            texture=metal,
        )
        objective_id = next(e["entity"] for e in target["entities"] if e["role"] == "trigger")
        await call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_axis_respawntime 15",
                "wm_allied_respawntime 20",
                "wm_set_round_timelimit 8",
                "wm_number_of_objectives 1",
                "wm_setwinner 1",
                "wait 50",
                'setautospawn "red training bay" 0',
                'setautospawn "blue training bay" 1',
            ],
        )
        await call(
            "script_set_event",
            block="training_radio",
            event="death",
            actions=[
                'wm_announce "Training radio destroyed"',
                "wm_setwinner 0",
                "wm_endround",
            ],
        )

        geometry = await call("validate")
        profile = await call("validate_profile")
        run.artifact("validation", {"geometry": geometry, "profile": profile})
        assert geometry["summary"]["error"] == 0, geometry
        assert profile["summary"]["error"] == 0, profile
        await call("render_topdown", overlay="structural", width=1400, height=900)
        await call(
            "render_camera", eye=[240, 128, 96], target=[900, 128, 120], width=1200, height=750
        )
        await call(
            "render_camera", eye=[240, 608, 64], target=[780, 608, 24], width=1200, height=750
        )
        await call("render_contact_sheet", overlay="structural", width=1600, height=1000)

        stages = [
            {
                "name": "west bay to radio",
                "spawn_entities": spawn_ids["red"],
                "objective_entities": [objective_id],
            }
        ]
        reports = {}
        for cell in (16, 8):
            for stance in ("stand", "crouch", "prone"):
                reports[f"{stance}_{cell}"] = await call(
                    "balance_report",
                    cell=cell,
                    stance=stance,
                    stages=stages,
                    team_sizes={"red": 6, "blue": 6},
                )
        run.artifact("routes", reports)
        routes = {
            key: value["reinforcement_balance"]["stages"][0]["routes"][0]
            for key, value in reports.items()
        }
        assert all(route["reachable"] for route in routes.values()), routes
        for cell in (8, 16):
            assert (
                routes[f"stand_{cell}"]["distance"]
                > routes[f"crouch_{cell}"]["distance"]
                > routes[f"prone_{cell}"]["distance"]
            ), routes
        # Exercise reversible parametric authoring on a real traversal constraint.
        await call("map_checkpoint", name="before_low_crouch_roof")
        await call("solid_param_preview", name="crouch_roof", path="min[2]", value=48)
        await call("solid_edit_param", name="crouch_roof", path="min[2]", value=48)
        lower = await call("balance_report", cell=8, stance="crouch", stages=stages)
        await call("map_restore", name="before_low_crouch_roof")
        restored = await call("balance_report", cell=8, stance="crouch", stages=stages)
        run.artifact("parametric_route_experiment", {"lowered": lower, "restored": restored})
        lowered_route = lower["reinforcement_balance"]["stages"][0]["routes"][0]
        restored_route = restored["reinforcement_balance"]["stages"][0]["routes"][0]
        assert lowered_route["distance"] > restored_route["distance"], (
            lowered_route,
            restored_route,
        )
        assert restored_route["distance"] == routes["crouch_8"]["distance"], restored_route

        await call(
            "arena_write",
            longname="Movement Bunker",
            keys={"briefing": "Three routes: stairs, crouch tunnel, prone vent."},
        )
        await call(
            "objdata_write",
            descriptions={
                "axis": "Reach and destroy the training radio.",
                "allied": "Defend the training radio.",
                "neutral": "Movement training through three routes.",
            },
            objectives={
                "axis": ["Destroy the Training Radio"],
                "allied": ["Defend the Training Radio"],
            },
        )
        await call("commandmap_make", cut_height=32)
        await call("levelshot_make", position=[240, 128, 0], yaw_deg=0)
        saved = await call("map_save")
        compile_result = await call("compile_map", preset="iterate")
        assert compile_result["ok"] and not compile_result["leaked"], compile_result
        bsp = next(p for p in compile_result["artifacts"] if p.endswith(".bsp"))
        package = await call("pack_pk3", bsp=bsp)
        assert package["ok"], package
        shipping = await call("ship_check", pk3=package["packages"][0])
        assert shipping["summary"]["error"] == 0, shipping
        summary = {
            "source": str(path),
            "root": str(run.root),
            "saved": saved,
            "routes": routes,
            "package": package,
            "compile": compile_result,
            "shipping": shipping,
            "profile_errors": profile["summary"]["error"],
            "geometry_errors": geometry["summary"]["error"],
            "route_design": "standing route via north stair/arch; shorter crouch route; direct prone vent",
            "clearance": "The 48-unit vent clears the 40-unit prone body plus its floor contact offset; crouch requires more than 48 units",
            "quantization": "Both 8- and 16-unit cells distinguish these three routes; measured distances vary with sampling resolution",
            "engine_tested": False,
        }
        run.artifact("summary", summary)
        print(
            json.dumps(
                {"root": str(run.root), "routes": routes, "package": package["packages"]}, indent=2
            )
        )


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, allow_abbrev=False).parse_args()
    asyncio.run(build())
