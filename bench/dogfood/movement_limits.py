"""Measure the movement figures the profile cannot read off a constant, in the real engine.

Builds a hall of lanes over stdio MCP, one test per lane, and drives one player through them
with `engine_playtest` (`scenarios/movement_limits.json`, generated from the lane table below):

- run and sprint speed on a flat lane (held 1.5 s, the top speed sampled);
- a jump in the open (its apex, from the observer's 100 ms samples);
- risers of 18 and 19 units walked into (the step height);
- ledges of 40 to 68 units, jumped from standing against their face with forward held (the
  highest ledge a jump reaches: the rise plus the step-up a falling body gets at a riser);
- platforms of 256 to 514 units walked off, each after a fresh spawn (a team switch) and the
  spawn shield, reading health in the samples taken in that lane (the drop that starts to
  hurt, the damage per band, the drop that kills).

The script checks every lane against the expectations in the table, which are the figures
recorded under `movement:` in the profile, and fails on any mismatch; the engine receipt is
kept either way. `--write-scenario` rewrites the checked-in scenario from the lane table;
otherwise the script asserts the file matches it. `--pk3` reuses a package.

Crouch-jumping is not a lane: crouching lowers only the top of the box (bg_pmove.c:2036,
:2069), and the driver releases forward between holds, so a crouch pressed after the jump
also stops the push into the riser that the step-up needs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dogfood_session import DogfoodSession

SCENARIO = Path(__file__).parent / "scenarios" / "movement_limits.json"
LANE = 96  # lane pitch along y; every lane box is 64 wide
RISER_X = (256, 512)  # step and ledge boxes; the player starts against the face at x 256
FACE_START = 236  # origin x: the 36-wide box 2 units short of the face
PLATFORM_X = (512, 640)  # fall platforms; the player starts on top and walks off at 640
PLATFORM_START = 620
HALL = (1280, 672)  # interior x and height

# (kind, height, expectation). Expectations are what ET: Legacy 2.86.2 showed (2026-11-06,
# seven runs; see bench/dogfood/README.md). Heights are integer brush heights; the lanes sit
# either side of each threshold, leaving out heights a frame's timing decides (258, 320, 513).
SPEED = [("run", None, {"speed": 256}), ("sprint", None, {"speed": 352})]
FREE_JUMP = ("jump", None, {"apex_min": 47.5, "apex_max": 48.7})
STEPS = [("step", 18, {"climbs": True}), ("step", 19, {"climbs": False})]
LEDGES = [("ledge", h, {"climbs": h <= 66}) for h in (40, 48, 56, 60, 64, 65, 66, 67, 68)]
# Damage per landing (bg_pmove.c:1666-1716, g_active.c:981-1014); None = killed outright.
FALLS = [
    ("fall", h, {"damage": d})
    for h, d in (
        (256, 0),
        (257, 0),
        (259, 10),
        (319, 10),
        (321, 15),
        (386, 15),
        (387, 25),
        (446, 25),
        (447, 50),
        (512, 50),
        (514, None),
    )
]


def lanes() -> list[dict]:
    """Every test with its lane centre y; the free jump reuses the run lane."""
    out = []
    for i, (kind, height, expect) in enumerate(SPEED + STEPS + LEDGES):
        y = 48 + LANE * i
        out.append({"kind": kind, "height": height, "y": y, "expect": expect, "build": True})
    kind, height, expect = FREE_JUMP
    out.append({"kind": kind, "height": height, "y": 48, "expect": expect, "build": False})
    first = len(SPEED + STEPS + LEDGES)
    for i, (kind, height, expect) in enumerate(FALLS):
        y = 48 + LANE * (first + i)
        out.append({"kind": kind, "height": height, "y": y, "expect": expect, "build": True})
    return out


def width() -> int:
    return LANE * len([lane for lane in lanes() if lane["build"]])


def label(lane: dict) -> str:
    return f"{lane['kind']} {lane['height']}" if lane["height"] is not None else lane["kind"]


def scenario() -> dict:
    """The steps, labelled `<kind> <height>`; each lane's last step is the one measured."""
    steps: list[dict] = [{"action": "join", "team": "red", "role": "engineer", "timeout": 40}]
    team = "red"
    for lane in lanes():
        name, kind, h, y = label(lane), lane["kind"], lane["height"], lane["y"]
        if kind in ("run", "sprint"):
            steps.append({"action": "position", "origin": [40, y, 24], "angles": [0, 0, 0]})
            controls = ["forward"] if kind == "run" else ["forward", "sprint"]
            steps.append(
                {
                    "action": "hold",
                    "controls": controls,
                    "seconds": 1.5,
                    "timeout": 6,
                    "label": name,
                }
            )
        elif kind == "jump":
            steps.append({"action": "position", "origin": [40, y, 24], "angles": [0, 0, 0]})
            steps.append(
                {
                    "action": "hold",
                    "controls": ["jump"],
                    "seconds": 1.0,
                    "timeout": 5,
                    "label": name,
                }
            )
        elif kind in ("step", "ledge"):
            steps.append({"action": "position", "origin": [FACE_START, y, 24], "angles": [0, 0, 0]})
            controls = ["forward"] if kind == "step" else ["forward", "jump"]
            steps.append(
                {
                    "action": "hold",
                    "controls": controls,
                    "seconds": 1.0,
                    "timeout": 5,
                    "label": name,
                }
            )
        elif kind == "fall":
            # A fresh spawn for every drop: switching team respawns at full health. The spawn
            # shield (3 s, g_client.c:3322) blocks all damage (g_combat.c:1601): wait it out.
            team = "blue" if team == "red" else "red"
            steps.append(
                {
                    "action": "join",
                    "team": team,
                    "role": "engineer",
                    "timeout": 40,
                    "label": f"{name} spawn",
                }
            )
            steps.append({"action": "hold", "controls": ["crouch"], "seconds": 3.5, "timeout": 8})
            steps.append(
                {"action": "position", "origin": [PLATFORM_START, y, h + 24], "angles": [0, 0, 0]}
            )
            # Long enough to land, and for a killed player to respawn (1 s waves) so the
            # hold's closing wait for a living player passes; the outcome is read from the
            # samples taken in this lane, whose y no other lane shares.
            steps.append(
                {
                    "action": "hold",
                    "controls": ["forward"],
                    "seconds": 2.5,
                    "timeout": 10,
                    "label": name,
                }
            )
    return {"steps": steps}


def read(result: dict) -> list[dict]:
    """Each lane's measured outcome from the receipt's step states (and, for the free jump,
    the observer's samples: the highest feet seen standing on the spot it jumped from)."""
    rows = {row["action"].get("label"): row for row in result["steps"]}
    samples = [
        json.loads(line)
        for line in (Path(result["output"]) / "telemetry.jsonl").read_text().splitlines()
    ]
    out = []
    for lane in lanes():
        name = label(lane)
        row = rows.get(name)
        if row is None or not row.get("ok"):
            out.append({"lane": name, "ok": False, "error": "step did not complete"})
            continue
        player = row["state"]["player"]
        z = player["position"][2]
        got: dict = {"lane": name, "position": player["position"]}
        if lane["kind"] == "jump":
            heights = [
                p["position"][2] - 24
                for p in (s["player"] for s in samples)
                if p.get("position")
                and abs(p["position"][0] - 40) < 0.5
                and abs(p["position"][1] - lane["y"]) < 0.5
            ]
            got["apex_sampled"] = round(max(heights), 3)
            ok = lane["expect"]["apex_min"] <= got["apex_sampled"] <= lane["expect"]["apex_max"]
        elif lane["kind"] in ("run", "sprint"):
            # The top speed sampled on the floor of this lane (the free jump shares the run
            # lane, but leaves the floor).
            got["speed"] = round(
                max(
                    p["speed"]
                    for p in (s["player"] for s in samples)
                    if p.get("position")
                    and p.get("speed") is not None
                    and abs(p["position"][1] - lane["y"]) < 0.5
                    and abs(p["position"][2] - 24.125) < 0.5
                ),
                1,
            )
            ok = abs(got["speed"] - lane["expect"]["speed"]) <= 2
        elif lane["kind"] in ("step", "ledge"):
            # Feet on the riser top (origin 24 above it) or back on the floor (24).
            got["climbs"] = z >= lane["height"] + 23
            ok = got["climbs"] == lane["expect"]["climbs"]
        else:
            seen = [
                p
                for p in (s["player"] for s in samples)
                if p.get("position") and abs(p["position"][1] - lane["y"]) < 0.5
            ]
            before = seen[0]["health"]
            lowest = min(p["health"] for p in seen)
            killed = not all(p["alive"] for p in seen)
            got.update(health_before=before, health_lowest=lowest, killed=killed)
            got["damage"] = None if killed else before - lowest
            ok = got["damage"] == lane["expect"]["damage"]
        got["expect"], got["ok"] = lane["expect"], ok
        out.append(got)
    return out


def box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


async def build(run: DogfoodSession) -> str:
    call = run.call
    await call("profile_summary")
    wall_rows = await call("texture_search", query="bunker_sd int_wall01")
    metal_rows = await call("texture_search", query="battery_wall wall02_mid")
    wall = next(r["texture"] for r in wall_rows["textures"] if not r.get("not_wall_material"))
    metal = next(r["texture"] for r in metal_rows["textures"] if not r.get("not_wall_material"))
    path = run.root / "maps/nrc_movelimits.map"
    await call("map_new", path=str(path), grid=1)
    await call("entity_set_keys", entity=0, keys={"message": "Movement Limits", "_minlight": "48"})

    async def solid(label, ir, texture=wall):
        return await call("solid_commit", label=label, ir=ir, textures={"default": texture})

    x_in, z_in = HALL
    await solid(
        "shell",
        {
            "op": "hollow",
            "solid": box([-32, -32, -32], [x_in + 32, width() + 32, z_in + 32]),
            "thickness": 32,
        },
    )
    for lane in lanes():
        if not lane["build"] or lane["height"] is None:
            continue
        lo, hi = RISER_X if lane["kind"] in ("step", "ledge") else PLATFORM_X
        y = lane["y"]
        await solid(
            f"{lane['kind']}_{lane['height']}",
            box([lo, y - 32, 0], [hi, y + 32, lane["height"]]),
            metal,
        )
    # Spawns and the objective at the far end, past where any lane's test runs to.
    for team, classname, y, flag in (
        ("red", "team_CTF_redspawn", 144, 1),
        ("blue", "team_CTF_bluespawn", 336, 2),
    ):
        await call(
            "entity_place",
            classname="team_WOLF_objective",
            origin=[1200, y, 24],
            keys={"description": f"{team} start", "spawnflags": flag},
        )
        for x in (1168, 1232):
            for dy in (-32, 32):
                await call(
                    "entity_place",
                    classname=classname,
                    origin=[x, y + dy, 24],
                    keys={"spawnflags": 3, "angle": 180},
                )
    await call("entity_place", classname="script_multiplayer", origin=[1200, 48, 64])
    await call(
        "entity_place",
        classname="info_player_intermission",
        origin=[1200, 48, 200],
        keys={"angles": [20, 180, 0], "spawnflags": 3},
    )
    for y in range(256, width(), 512):
        await call("entity_place", classname="light", origin=[640, y, 600], keys={"light": 1500})
    await call(
        "objective_place",
        kind="dynamite",
        name="limits_radio",
        track="Limits Radio",
        team="allies",
        objective={"min": [1216, width() - 96, 0], "max": [1264, width() - 48, 48]},
        trigger={"min": [1184, width() - 128, 0], "max": [1280, width() - 16, 96]},
        texture=metal,
    )
    await call(
        "script_set_event",
        block="game_manager",
        event="spawn",
        actions=[
            "wm_axis_respawntime 1",
            "wm_allied_respawntime 1",
            "wm_set_round_timelimit 30",
            "wm_number_of_objectives 1",
            "wm_setwinner 1",
            "wait 50",
            'setautospawn "red start" 0',
            'setautospawn "blue start" 1',
        ],
    )
    await call(
        "script_set_event",
        block="limits_radio",
        event="death",
        actions=["wm_setwinner 0", "wm_endround"],
    )
    geometry = await call("validate")
    profile = await call("validate_profile")
    run.artifact("validation", {"geometry": geometry, "profile": profile})
    assert geometry["summary"]["error"] == 0, geometry
    assert profile["summary"]["error"] == 0, profile
    await call("arena_write", longname="Movement Limits", keys={"briefing": "Measurement lanes."})
    await call(
        "objdata_write",
        descriptions={"axis": "Measure.", "allied": "Measure.", "neutral": "Measurement lanes."},
        objectives={"axis": ["Destroy the Limits Radio"], "allied": ["Defend the Limits Radio"]},
    )
    await call("commandmap_make", cut_height=64)
    await call("levelshot_make", position=[40, 48, 0], yaw_deg=0)
    await call("map_save")
    compiled = await call("compile_map", preset="iterate")
    assert compiled["ok"] and not compiled["leaked"], compiled
    bsp = next(p for p in compiled["artifacts"] if p.endswith(".bsp"))
    package = await call("pack_pk3", bsp=bsp)
    assert package["ok"], package
    return package["packages"][0]


async def main(pk3: str | None, write: bool):
    wanted = scenario()
    if write:
        SCENARIO.write_text(json.dumps(wanted, indent=2) + "\n")
    assert json.loads(SCENARIO.read_text()) == wanted, (
        f"{SCENARIO} is stale; rerun with --write-scenario"
    )
    async with DogfoodSession("movement_limits") as run:
        package = pk3 or await build(run)
        response = await run.call(
            "engine_playtest",
            pk3=package,
            scenario=wanted,
            profile_id="etl",
            timeout_seconds=600,
            include_images=False,
            detail="full",
        )
        run.artifact("result", response)
        measured = read(response)
        run.artifact("measured", measured)
        print(
            json.dumps(
                {
                    "package": package,
                    "output": response.get("output"),
                    "engine_ok": response.get("ok"),
                    "error": response.get("error"),
                    "lanes": measured,
                },
                indent=2,
            )
        )
        assert response.get("ok"), response.get("error")
        failed = [row for row in measured if not row["ok"]]
        assert not failed, failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--pk3", help="reuse a package built by an earlier run")
    parser.add_argument("--write-scenario", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.pk3, args.write_scenario))
