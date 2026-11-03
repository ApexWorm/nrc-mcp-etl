"""Build and revise an engineer workshop through the real MCP wire interface."""

from __future__ import annotations

import argparse
import asyncio
import json

from dogfood_session import DogfoodSession


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def main():
    async with DogfoodSession("constructible") as run:
        await run.call("profile_summary")
        wall_search = await run.call("texture_search", query="bunker_sd int_wall01")
        floor_search = await run.call("texture_search", query="battery_wall wall02_mid")
        metal_search = await run.call("texture_search", query="metal", limit=40)
        wall = wall_search["textures"][0]["texture"]
        floor = floor_search["textures"][0]["texture"]
        metal = next(
            r["texture"] for r in metal_search["textures"] if not r.get("not_wall_material")
        )
        run.artifact("materials", {"wall": wall, "floor": floor, "metal": metal})

        map_path = run.root / "maps/nrc_workshop.map"
        await run.call("map_new", path=str(map_path))
        await run.call(
            "entity_set_keys",
            entity=0,
            keys={
                "message": "Engineer Workshop",
                "_ambient": 12,
                "_minlight": 4,
            },
        )
        shell = {"op": "hollow", "solid": box([-16, -16, -160], [1552, 1040, 336]), "thickness": 16}
        await run.call("solid_compile", ir=shell, textures={"default": wall})
        await run.call(
            "solid_preview", ir=shell, textures={"default": wall}, width=1000, height=750
        )
        await run.call("solid_commit", ir=shell, label="shell", textures={"default": wall})
        # Four platforms enclose a maintenance pit. Both long service routes remain usable.
        platforms = [
            ("west_floor", [0, 0, -144], [640, 1024, 0]),
            ("east_floor", [896, 0, -144], [1536, 1024, 0]),
            ("south_service", [640, 0, -144], [896, 256, 0]),
            ("north_service", [640, 768, -144], [896, 1024, 0]),
        ]
        for label, lo, hi in platforms:
            await run.call("solid_commit", label=label, ir=box(lo, hi), textures={"default": floor})
        await run.call(
            "solid_commit",
            label="pit_escape",
            ir={
                "op": "stair",
                "origin": [704, 480, -144],
                "width": 128,
                "steps": 9,
                "rise": 16,
                "run": 32,
                "along": "y",
                "up": "z",
            },
            textures={"default": metal, "detail": True},
        )
        for label, lo, hi in [
            ("west_cover", [400, 704, 0], [528, 832, 96]),
            ("east_cover", [1008, 192, 0], [1136, 320, 96]),
        ]:
            await run.call(
                "solid_commit",
                label=label,
                ir=box(lo, hi),
                textures={"default": metal, "detail": True},
            )
        await run.call(
            "solid_commit",
            label="workbench",
            ir=box([256, 160, 0], [448, 256, 64]),
            textures={"default": metal, "detail": True},
        )
        # Exercise a real resize, restore, and reapply without accumulating old brushes.
        before = await run.call("map_stats")
        await run.call("map_checkpoint", name="before_workbench_resize")
        preview = await run.call("solid_param_preview", name="workbench", path="max[0]", value=512)
        edited = await run.call("solid_edit_param", name="workbench", path="max[0]", value=512)
        after = await run.call("map_stats")
        assert after["brushes"] == before["brushes"], "resizing duplicated the workbench"
        await run.call("map_restore", name="before_workbench_resize")
        restored = await run.call("solid_inspect", name="workbench")
        assert restored["ir"]["max"][0] == 448
        restored_stats = await run.call("map_stats")
        assert restored_stats["brushes"] == before["brushes"], "restoring duplicated brushes"
        await run.call("solid_edit_param", name="workbench", path="max[0]", value=512)
        reapplied_stats = await run.call("map_stats")
        assert reapplied_stats["brushes"] == before["brushes"], "reapplying duplicated brushes"
        run.artifact(
            "parameter_exercise",
            {
                "before_brushes": before["brushes"],
                "after_brushes": after["brushes"],
                "preview": preview,
                "applied": edited,
                "restored_max_x": restored["ir"]["max"][0],
                "restored_brushes": restored_stats["brushes"],
                "reapplied_brushes": reapplied_stats["brushes"],
            },
        )
        # A decorative overhead pipe: it neither seals nor obstructs the player route.
        await run.call(
            "patch_commit",
            kind="cylinder",
            centre=[256, 944, 256],
            radius=16,
            length=1024,
            shader=metal,
            axis="x",
            label="overhead_pipe",
        )

        spawn_indices = {"red": [], "blue": []}
        for classname, marker, x, flag, angle, team in [
            ("team_CTF_redspawn", "Axis Workshop", 192, 1, 0, "red"),
            ("team_CTF_bluespawn", "Allied Workshop", 1344, 2, 180, "blue"),
        ]:
            await run.call(
                "entity_place",
                classname="team_WOLF_objective",
                origin=[x, 512, 24],
                keys={"description": marker, "spawnflags": flag},
            )
            for y in (352, 448, 544, 640):
                placed = await run.call(
                    "entity_place",
                    classname=classname,
                    origin=[x, y, 24],
                    keys={"angle": angle, "spawnflags": 3},
                )
                spawn_indices[team].append(placed["entity"])
        await run.call("entity_place", classname="script_multiplayer", origin=[768, 128, 64])
        await run.call(
            "entity_place",
            classname="info_player_intermission",
            origin=[1264, 800, 192],
            keys={"angles": [20, 210, 0], "spawnflags": 3},
        )
        for x in (256, 768, 1280):
            for y in (256, 768):
                await run.call(
                    "entity_place",
                    classname="light",
                    origin=[x, y, 256],
                    keys={"light": 550, "_color": [1, 0.92, 0.78]},
                )
        await run.call(
            "script_set_event",
            block="game_manager",
            event="spawn",
            actions=[
                "wm_axis_respawntime 20",
                "wm_allied_respawntime 15",
                "wm_set_round_timelimit 5",
                "wm_number_of_objectives 1",
                "wm_setwinner 0",
                "wm_set_defending_team 0",
                "wait 50",
                'setautospawn "Axis Workshop" 0',
                'setautospawn "Allied Workshop" 1',
            ],
        )
        objective = await run.call(
            "objective_place",
            kind="constructible",
            name="workshop_bridge",
            track="Workshop Bridge",
            team="allies",
            texture=metal,
            objective={"min": [640, 448, -16], "max": [896, 576, 0]},
            trigger={"min": [608, 384, -32], "max": [960, 640, 128]},
        )
        await run.call(
            "script_set_event",
            block="workshop_bridge",
            event="built",
            params=["final"],
            actions=[
                'wm_announce "The Allies built the workshop bridge!"',
                "wm_objective_status 1 1 1",
                "wm_objective_status 1 0 2",
                "wm_setwinner 1",
                "wait 1000",
                "wm_endround",
            ],
        )
        await run.call(
            "arena_write",
            longname="Engineer Workshop",
            keys={
                "briefing": "Allied engineers must build the workshop bridge. Axis defend it.",
                "timelimit": "5",
                "axisRespawnTime": "20",
                "alliedRespawnTime": "15",
            },
        )
        await run.call(
            "objdata_write",
            descriptions={
                "axis": "Prevent the Allied engineers from completing the bridge.",
                "allied": "Construct the bridge across the maintenance pit.",
                "neutral": "A compact workshop with a buildable bridge and two service routes.",
            },
            objectives={
                "axis": ["Defend the workshop bridge"],
                "allied": ["Build the workshop bridge"],
            },
        )
        await run.call("commandmap_make", margin=96, cut_height=160)
        await run.call("levelshot_make", position=[1184, 640, 0], yaw_deg=195)
        renders = await run.call("render_contact_sheet", width=1400, height=1000)
        eye = await run.call(
            "render_player_eye", position=[1184, 640, 0], yaw_deg=195, width=1200, height=800
        )
        balance = await run.call("balance_report", cell=16, team_sizes={"red": 4, "blue": 4})
        assert len(balance["objectives"]) == 1, "linked bridge entities count as one objective"
        routes = balance["reinforcement_balance"]["stages"][0]["routes"]
        assert len(routes) == 2 and {route["team"] for route in routes} == {"red", "blue"}, routes
        members = {entity["entity"] for entity in objective["entities"]}
        assert all(set(route["objective_entities"]) == members for route in routes), routes
        movement = await run.call("movement_check", cell=16)
        geometry = await run.call("validate")
        validation = await run.call("validate_profile")
        assert geometry["summary"]["error"] == 0, geometry
        assert validation["summary"]["error"] == 0, validation
        saved = await run.call("map_save")
        compiled = await run.call("compile_map", preset="iterate")
        assert compiled["ok"] and not compiled.get("leaked"), compiled
        bsp = next(path for path in compiled["artifacts"] if path.endswith(".bsp"))
        package = await run.call("pack_pk3", bsp=bsp)
        assert package["ok"], package
        ship = await run.call("ship_check", target=bsp, pk3=package["packages"][0])
        assert ship["summary"]["error"] == 0, ship
        summary = {
            "map": str(map_path),
            "project": str(run.root),
            "package": package["packages"][0],
            "compile": compiled,
            "pack": package,
            "ship": ship,
            "save": saved,
            "geometry": geometry,
            "profile": validation,
            "balance": balance,
            "movement": movement,
            "renders": renders,
            "eye": eye,
            "objective": objective,
            "spawn_entities": spawn_indices,
            "engineer_probe": {
                "team": "allies",
                "position": [928, 512, 24],
                "yaw": 180,
                "pitch": 55,
                "objective": "workshop_bridge",
                "track": "Workshop Bridge",
            },
            "engine_tested": False,
        }
        path = run.artifact("summary", summary)
        print(json.dumps({"summary": path, "package": package["packages"][0]}, indent=2))


if __name__ == "__main__":
    argparse.ArgumentParser(description=__doc__, allow_abbrev=False).parse_args()
    asyncio.run(main())
