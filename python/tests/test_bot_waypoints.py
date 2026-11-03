"""Bot waypoint files: the codec against the add-on's own encoding and shipped files, the
seeded graph's flags and connections, and the two tools' contracts.

Loaded in ET: Legacy 2.86.2 with its bundled Omni-bot 0.93 (and the 0.92 release): the
add-on logs "N Waypoints Loaded from file nav/<map>.way" for seeded files, and bots walk
them, plant and defuse dynamite (bench/dogfood/README.md has the runs).
"""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import botnav, botplay, kernel, profiles
from nrc_mcp import server as srv
from nrc_mcp.kernel import parse_map
from test_analysis import box, point_entity, room, write_map

FIXTURE = Path(__file__).resolve().parents[2] / "bench" / "tapes" / "fixtures" / "nrcroom"


@pytest.fixture(autouse=True)
def _etl(monkeypatch):
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    monkeypatch.setenv("NRC_PROFILE", "etl")
    yield
    srv.SESSION.map = None
    srv.SESSION.path = None


def spec():
    return botnav.spec("etl")


# --------------------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------------------


def test_packed_integers_match_the_addon_encoding():
    # File::WriteIntPk: 7 bits per byte, low first, HIGH bit on the last byte.
    assert botnav._varint(0) == b"\x80"
    assert botnav._varint(127) == b"\xff"
    assert botnav._varint(128) == b"\x00\x81"
    assert botnav._varint(300) == b"\x2c\x82"
    # WriteSignIntPk: zigzag, i >= 0 -> i << 1, i < 0 -> (~i << 1) | 1.
    assert botnav._signed(0) == b"\x80"
    assert botnav._signed(1) == b"\x82"
    assert botnav._signed(-1) == b"\x81"
    assert botnav._signed(-3) == b"\x85"
    for value in (-70000, -64, -1, 0, 1, 63, 64, 70000):
        assert botnav._Reader(botnav._signed(value), 0).signed() == value
    big = 1 << 37  # a 64-bit nav flag
    assert botnav._Reader(botnav._varint(big), 0).varint() == big


def test_round_trip_covers_every_optional_field():
    fmt = spec()["format"]
    wf = botnav.WaypointFile(
        version=fmt["version"],
        timestamp=b"Tue Nov  3 10:00:00 2026\n",
        comments=b"test",
        world_mins=(-1024.0, -512.0, -65535.0),
        world_maxs=(1024.0, 512.0, 65535.0),
        waypoints=[
            botnav.Waypoint((0.0, 0.0, 24.0), uid=1, connections=[1, 2]),
            botnav.Waypoint(
                (128.0, 0.0, 24.0),
                uid=7,
                facing=(1.0, 0.0, 0.0),
                flags=64 | (1 << 37),
                radius=50,
                name="Axis spawn",
                properties=[("stance", "crouch"), ("paththrough", "x:y")],
                connections=[0],
            ),
            botnav.Waypoint((256.0, 0.5, 24.0), uid=8, radius=12.5, connections=[1, 0]),
        ],
    )
    data = botnav.write(wf, fmt)
    assert data[0] == 9
    assert struct.unpack_from("<I", data, 1)[0] == 3
    header = 1 + 4 + fmt["header"]["timestamp_bytes"] + fmt["header"]["comment_bytes"] + 24
    assert header == 293
    # Waypoint 0: mask 0 (uid 1 = 0 + 1, default radius), position, 2 connections (+1, +2).
    assert data[header] == 0
    assert data[header + 13 :][:3] == b"\x82\x82\x84"
    back = botnav.read(data, fmt)
    assert back.waypoints[0].radius is None
    assert back.waypoints[1].radius == 50 and back.waypoints[2].radius == 12.5
    assert [w.uid for w in back.waypoints] == [1, 7, 8]
    assert back.waypoints[1].properties == wf.waypoints[1].properties
    assert [w.connections for w in back.waypoints] == [[1, 2], [0], [1, 0]]
    assert botnav.write(back, fmt) == data


def test_reader_refuses_other_versions_and_bad_connections():
    fmt = spec()["format"]
    data = bytearray(botnav.write(botnav.WaypointFile(version=9, waypoints=[]), fmt))
    data[0] = 8
    with pytest.raises(botnav.BotNavError, match="version 8"):
        botnav.read(bytes(data), fmt)
    wf = botnav.WaypointFile(version=9, waypoints=[botnav.Waypoint((0, 0, 0), uid=1)])
    good = botnav.write(wf, fmt)
    with pytest.raises(botnav.BotNavError, match="trailing"):
        botnav.read(good + b"\x00", fmt)
    bad = good[:-1] + b"\x81" + botnav._signed(5)  # one connection, to index 5
    with pytest.raises(botnav.BotNavError, match="outside"):
        botnav.read(bad, fmt)


STOCK = ("oasis", "battery", "goldrush", "radar", "railgun", "fueldump")


def _stock_nav() -> Path | None:
    base = os.environ.get("NRC_FS_BASEPATH")
    if not base:
        return None
    nav = (
        Path(str(spec()["install_dir"]).format(basepath=base)) / Path(spec()["install_path"]).parent
    )
    return nav if nav.is_dir() else None


@pytest.mark.parametrize("name", STOCK)
def test_shipped_stock_waypoints_read_and_rewrite_byte_identically(name):
    nav = _stock_nav()
    if nav is None or not (nav / f"{name}.way").is_file():
        pytest.skip("no installed bot add-on with stock waypoints (NRC_FS_BASEPATH)")
    data = (nav / f"{name}.way").read_bytes()
    wf = botnav.read(data, spec()["format"])
    assert len(wf.waypoints) > 300
    assert all(w.connections for w in wf.waypoints[:50])
    assert botnav.write(wf, spec()["format"]) == data


# --------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------


def _brush_entity(classname: str, *brushes: str, **keys) -> str:
    lines = ["{", f'"classname" "{classname}"', *(f'"{k}" "{v}"' for k, v in keys.items())]
    return "\n".join(lines) + "\n" + "".join(brushes) + "}\n"


def _seed(path: Path, **kwargs):
    return botnav.build(path, parse_map(path.read_text()), "etl", cell=16, **kwargs)


def _assert_straight(wf, report, path):
    """Every written connection is a directed straight walk in the seeded grids."""
    grids = {
        s: botnav.analysis.build_navgrid(
            path, cell=16, profile_id="etl", stance=s, open_state_changing=True
        )
        for s in report["stances"]
    }
    graph = botnav._Graph(grids)
    floor = spec()["floor_offset"]
    stand = grids["stand"]
    cells = [
        stand.cell_at((w.position[0], w.position[1], w.position[2] - floor + 1))
        for w in wf.waypoints
    ]
    flags = spec()["flags"]
    for i, w in enumerate(wf.waypoints):
        rank = 2 if w.flags & flags["PRONE"] else 1 if w.flags & flags["CROUCH"] else 0
        for t in w.connections:
            other = wf.waypoints[t]
            r2 = 2 if other.flags & flags["PRONE"] else 1 if other.flags & flags["CROUCH"] else 0
            assert graph.straight(max(rank, r2), cells[i], cells[t]), (i, t)


@pytest.mark.parametrize("height,flag", [(56, "CROUCH"), (48, "PRONE")])
def test_low_passage_waypoints_carry_the_stance_flag(tmp_path, height, flag):
    # The only way between the two halves is a low passage through a partition.
    path = write_map(
        tmp_path,
        [
            *room(0, 0, 1024, 256),
            box((480, 16, 0), (544, 96, 256)),
            box((480, 160, 0), (544, 240, 256)),
            box((480, 96, height), (544, 160, 256)),
        ],
        [
            point_entity("team_CTF_redspawn", (96, 128, 24)),
            point_entity("team_CTF_bluespawn", (928, 128, 24)),
            _brush_entity(
                "trigger_objective_info", box((864, 32, 0), (960, 96, 96)), track="Depot"
            ),
        ],
    )
    wf, report = _seed(path, spacing=64)
    flags = spec()["flags"]
    low = [w for w in wf.waypoints if w.flags & flags[flag]]
    # Under the roof, the cells beside it where a standing box still hits it, and turns on
    # the legs into it (flagged because the leg needs the stance).
    assert low and all(448 <= w.position[0] <= 576 for w in low)
    assert not [w for w in wf.waypoints if 480 <= w.position[0] <= 544 and w not in low]
    assert report["components"]["count"] == 1
    assert report["spawn_waypoints"].keys() == {"red", "blue"}
    (objective,) = report["objectives"]
    assert objective["reached_from_red_spawns"] and objective["reached_from_blue_spawns"]
    names = {w.name for w in wf.waypoints if w.name}
    assert {"Axis spawn", "Allies spawn", "Depot"} <= names
    # Positions are the player origin: floor + 24.
    assert {w.position[2] for w in wf.waypoints} == {24.0}
    _assert_straight(wf, report, path)


def test_too_low_passage_splits_the_graph_instead_of_inventing_a_link(tmp_path):
    path = write_map(
        tmp_path,
        [
            *room(0, 0, 1024, 256),
            box((480, 16, 0), (544, 96, 256)),
            box((480, 160, 0), (544, 240, 256)),
            box((480, 96, 32), (544, 160, 256)),
        ],
        [
            point_entity("team_CTF_redspawn", (96, 128, 24)),
            point_entity("team_CTF_bluespawn", (928, 128, 24)),
        ],
    )
    wf, report = _seed(path, spacing=64)
    assert report["components"]["count"] == 2
    assert not any(w.flags for w in wf.waypoints)


def test_blown_wall_connections_are_blockable(tmp_path):
    path = write_map(
        tmp_path,
        [
            *room(0, 0, 1024, 256),
            box((480, 16, 0), (544, 96, 256)),
            box((480, 160, 0), (544, 240, 256)),
        ],
        [
            point_entity("team_CTF_redspawn", (96, 128, 24)),
            point_entity("team_CTF_bluespawn", (928, 128, 24)),
            _brush_entity("func_explosive", box((480, 96, 0), (544, 160, 256))),
        ],
    )
    wf, report = _seed(path, spacing=64)
    blockable = spec()["flags"]["BLOCKWALL"]
    walled = [w for w in wf.waypoints if w.flags & blockable]
    assert report["state_changing_entities"] and walled
    assert all(400 <= w.position[0] <= 640 for w in walled)
    # None inside the wall: the add-on would make it the goal for planting there, and it
    # is unreachable while the wall stands. The wall is spanned from one side to the other.
    assert not [w for w in wf.waypoints if 480 < w.position[0] < 544]
    assert report["wall_spans"] > 0
    assert report["components"]["count"] == 1
    # None inside the wall: the add-on would make it the dynamite goal's waypoint,
    # unreachable while the wall stands. The crossing spans it lip to lip.
    assert not [w for w in wf.waypoints if 480 < w.position[0] < 544]
    # The only crossings are the blockable ones.
    for w in wf.waypoints:
        for t in w.connections:
            if (w.position[0] < 512) != (wf.waypoints[t].position[0] < 512):
                assert w.flags & blockable and wf.waypoints[t].flags & blockable


def test_a_breakable_objective_gets_no_waypoint_inside_it(tmp_path):
    # A crate to blow up in the middle of a room opens no way anywhere: waypoints inside it
    # would be the add-on's nearest waypoint to the objective, unreachable until it is gone.
    path = write_map(
        tmp_path,
        room(0, 0, 768, 512),
        [
            point_entity("team_CTF_redspawn", (96, 256, 24)),
            point_entity("team_CTF_bluespawn", (672, 256, 24)),
            _brush_entity("func_explosive", box((352, 224, 0), (416, 288, 96))),
        ],
    )
    wf, report = _seed(path, spacing=64)
    assert report["state_changing_cells_skipped"] > 0
    assert not any(w.flags for w in wf.waypoints)
    assert not [w for w in wf.waypoints if 352 < w.position[0] < 416 and 224 < w.position[1] < 288]
    assert report["components"]["count"] == 1


def test_waypoints_no_spawn_connects_to_are_dropped(tmp_path):
    # A sealed box beside the room: walkable, but nobody can get there.
    path = write_map(
        tmp_path,
        [*room(0, 0, 512, 256), *room(640, 0, 896, 256)],
        [point_entity("team_CTF_redspawn", (96, 128, 24))],
    )
    wf, report = _seed(path, spacing=64)
    assert report["pruned_unconnected_to_spawns"] > 0
    assert all(w.position[0] < 512 for w in wf.waypoints)
    assert [w.uid for w in wf.waypoints] == list(range(1, len(wf.waypoints) + 1))


def test_spacing_must_cover_a_cell(tmp_path):
    path = write_map(tmp_path, room(0, 0, 256, 256))
    with pytest.raises(botnav.BotNavError, match="spacing"):
        _seed(path, spacing=8)


# --------------------------------------------------------------------------------------
# Jumps and ladders
# --------------------------------------------------------------------------------------


def _links(wf, test):
    """Connections (i, j) whose endpoints pass `test(from_position, to_position)`."""
    w = wf.waypoints
    return [
        (i, t) for i, a in enumerate(w) for t in a.connections if test(a.position, w[t].position)
    ]


def _ledge_map(tmp_path, height, extra=()):
    return write_map(
        tmp_path,
        [*room(0, 0, 1024, 256), box((512, 16, 0), (1008, 240, height)), *extra],
        [
            point_entity("team_CTF_redspawn", (96, 128, 24)),
            point_entity("team_CTF_bluespawn", (900, 128, height + 24)),
        ],
    )


def test_a_ledge_within_the_jump_gets_a_one_way_jump_and_drops_stay_one_way(tmp_path):
    flags = spec()["flags"]
    jumps = spec()["jumps"]
    wf, report = _seed(_ledge_map(tmp_path, 40), spacing=128)
    (jump,) = report["jumps"]
    assert jump["kind"] == "ledge" and jump["rise"] == 40
    takeoff = wf.waypoints[jump["waypoints"]["takeoff"]]
    landing = wf.waypoints[jump["waypoints"]["landing"]]
    assert takeoff.flags == flags["JUMP"] and landing.flags == flags["JUMPLOW"]
    assert takeoff.radius == landing.radius == jumps["radius"]
    # Feet on the exact floors: 0 below, the ledge's top at 40 above.
    assert takeoff.position[2] == 24 and landing.position[2] == 64
    assert takeoff.position[0] < 512 < landing.position[0]
    # Up: only the jump. Down: walking off, one way, never flagged.
    up = _links(wf, lambda a, b: b[2] - a[2] > 18)
    assert up == [(jump["waypoints"]["takeoff"], jump["waypoints"]["landing"])]
    down = _links(wf, lambda a, b: a[2] - b[2] > 18)
    assert down and all(i not in wf.waypoints[t].connections for i, t in down)
    assert all(not wf.waypoints[i].flags & flags["JUMP"] for i, _ in down)
    # Nothing leads through the take-off but the jump and the walk back to its run-up.
    into = [i for i, w in enumerate(wf.waypoints) if jump["waypoints"]["takeoff"] in w.connections]
    assert len(into) == 1
    (blue,) = report["spawn_waypoints"]["blue"]
    (red,) = report["spawn_waypoints"]["red"]
    assert blue in botnav.reachable(wf.waypoints, red)
    assert report["one_way_connections"] >= len(down) + 1


def test_a_ledge_above_the_jump_gets_no_link(tmp_path):
    # 44 > 45.56 (270^2 / 1600) less the 4-unit margin.
    wf, report = _seed(_ledge_map(tmp_path, 44), spacing=128)
    assert report["jumps"] == []
    assert not any(w.flags for w in wf.waypoints)
    (blue,) = report["spawn_waypoints"]["blue"]
    (red,) = report["spawn_waypoints"]["red"]
    assert blue not in botnav.reachable(wf.waypoints, red)
    assert red in botnav.reachable(wf.waypoints, blue)


def test_a_ledge_with_stairs_beside_it_is_walked_not_jumped(tmp_path):
    # Two 16-unit steps up the ledge's side: the walk is short, so no jump is added.
    stairs = [box((448, 16, 0), (512, 80, 16)), box((480, 16, 16), (512, 80, 32))]
    _wf, report = _seed(_ledge_map(tmp_path, 40, stairs), spacing=128)
    assert report["jumps"] == []


def _gap_map(tmp_path, width, ceiling=None):
    brushes = [
        *room(0, 0, 1024, 256, floor_top=-512, height=768),
        box((16, 16, -496), (480, 240, 0)),
        box((480 + width, 16, -496), (1008, 240, 0)),
    ]
    if ceiling is not None:
        brushes.append(box((432, 16, ceiling), (480 + width + 48, 240, ceiling + 16)))
    return write_map(
        tmp_path,
        brushes,
        [
            point_entity("team_CTF_redspawn", (96, 128, 24)),
            point_entity("team_CTF_bluespawn", (900, 128, 24)),
        ],
    )


def test_a_gap_within_every_arc_gets_jumps_both_ways(tmp_path):
    flags = spec()["flags"]
    wf, report = _seed(_gap_map(tmp_path, 48), spacing=128)
    assert {j["kind"] for j in report["jumps"]} == {"gap"}
    across = {(j["takeoff"][0] < 480, j["landing"][0] > 528) for j in report["jumps"]}
    assert (True, True) in across and (False, False) in across
    for jump in report["jumps"]:
        takeoff = wf.waypoints[jump["waypoints"]["takeoff"]]
        landing = wf.waypoints[jump["waypoints"]["landing"]]
        assert takeoff.flags == flags["JUMP"] and landing.flags == 0
        assert jump["waypoints"]["landing"] in takeoff.connections
        assert jump["waypoints"]["takeoff"] not in landing.connections
    assert report["components"]["count"] == 1
    (blue,) = report["spawn_waypoints"]["blue"]
    (red,) = report["spawn_waypoints"]["red"]
    assert blue in botnav.reachable(wf.waypoints, red)
    assert red in botnav.reachable(wf.waypoints, blue)


def test_a_gap_too_wide_for_the_slowest_runner_gets_no_jump(tmp_path):
    # 64 units: a heavy-weapon carrier at 0.4 x 320 = 128 units/s lands short.
    _wf, report = _seed(_gap_map(tmp_path, 64), spacing=128)
    assert report["jumps"] == [] and report["components"]["count"] == 2


def test_a_low_ceiling_over_a_gap_stops_the_arc(tmp_path):
    # 96 above the floor: the box (72 tall) hits it on the way up.
    _wf, report = _seed(_gap_map(tmp_path, 48, ceiling=96), spacing=128)
    assert report["jumps"] == []


def _ladder_map(tmp_path, shader="t/ladder", parms="surfaceparm ladder\n\tsurfaceparm playerclip"):
    (tmp_path / "scripts").mkdir(exist_ok=True)
    (tmp_path / "scripts" / "t.shader").write_text(
        f"textures/{shader}\n{{\n\t{parms}\n\tsurfaceparm nodraw\n}}\n"
    )
    return write_map(
        tmp_path,
        [
            *room(0, 0, 1024, 256, height=384),
            box((512, 16, 0), (1008, 240, 192)),
            box((496, 96, 0), (512, 160, 192), shader),
        ],
        [
            point_entity("team_CTF_redspawn", (96, 128, 24)),
            point_entity("team_CTF_bluespawn", (900, 128, 216)),
        ],
    )


def test_a_ladder_gets_a_climb_waypoint_at_its_foot_and_top_linked_both_ways(tmp_path):
    climb = spec()["flags"]["CLIMB"]
    wf, report = _seed(_ladder_map(tmp_path), spacing=128)
    (ladder,) = report["ladders"]
    foot = wf.waypoints[ladder["waypoints"]["foot"]]
    top = wf.waypoints[ladder["waypoints"]["top"]]
    assert foot.flags == top.flags == climb
    assert ladder["waypoints"]["top"] in foot.connections
    assert ladder["waypoints"]["foot"] in top.connections
    # The foot stands in front of the face (x 496), the body's half width clear of it; the
    # top stands on the upper floor behind it.
    assert 496 - 18 - 32 <= foot.position[0] <= 496 - 18 and 96 <= foot.position[1] <= 160
    assert foot.position[2] == 24 and top.position[2] == 216 and top.position[0] > 512
    assert [w for w in wf.waypoints if w.flags & climb] == [foot, top]
    (blue,) = report["spawn_waypoints"]["blue"]
    (red,) = report["spawn_waypoints"]["red"]
    assert blue in botnav.reachable(wf.waypoints, red)


def test_a_brush_without_the_ladder_surfaceparm_is_no_ladder(tmp_path):
    wf, report = _seed(_ladder_map(tmp_path, parms="surfaceparm playerclip"), spacing=128)
    assert report["ladders"] == []
    assert not any(w.flags for w in wf.waypoints)


# --------------------------------------------------------------------------------------
# Doors, walls and constructibles
# --------------------------------------------------------------------------------------

_SPAWNS = [
    point_entity("team_CTF_redspawn", (96, 128, 24)),
    point_entity("team_CTF_bluespawn", (928, 128, 24)),
]
# A partition at x 480..496 whose only gap is y 96..160 under a lintel at 128.
_PARTITION = [
    box((480, 16, 0), (496, 96, 256)),
    box((480, 160, 0), (496, 240, 256)),
    box((480, 96, 128), (496, 160, 256)),
]


def _door_map(tmp_path, classname="func_door", **keys):
    door = _brush_entity(classname, box((484, 96, 0), (492, 160, 128)), **keys)
    return write_map(tmp_path, [*room(0, 0, 1024, 256), *_PARTITION], [*_SPAWNS, door])


def _crossings(wf, x):
    return [
        (w, wf.waypoints[t])
        for w in wf.waypoints
        for t in w.connections
        if (w.position[0] < x) != (wf.waypoints[t].position[0] < x)
    ]


def test_a_door_gets_a_doorway_waypoint_that_opens_it(tmp_path):
    # A door is solid to a grid, so without this no waypoint ever passed a doorway.
    wf, report = _seed(_door_map(tmp_path), spacing=64)
    flags = spec()["flags"]
    doorway = [w for w in wf.waypoints if w.flags & flags["DOOR"]]
    assert doorway and all(476 <= w.position[0] <= 500 for w in doorway)
    assert all(not w.flags & (flags["TEAMONLY"] | flags["BLOCKWALL"]) for w in doorway)
    assert report["components"]["count"] == 1
    assert report["doors"] == [{"entity": 3, "flags": ["DOOR"]}]
    # Every way through ends at a waypoint that presses the use key.
    crossings = _crossings(wf, 488)
    assert crossings and all((a.flags | b.flags) & flags["DOOR"] for a, b in crossings)


@pytest.mark.parametrize(
    "allowteams,team", [("axis,cvops", "AXIS"), ("allies", "ALLIES"), ("Axis Allies", None)]
)
def test_a_team_door_keeps_its_doorway_to_that_team(tmp_path, allowteams, team):
    wf, _report = _seed(_door_map(tmp_path, allowteams=allowteams), spacing=64)
    flags = spec()["flags"]
    want = flags["DOOR"] | (flags[team] | flags["TEAMONLY"] if team else 0)
    doorway = [w for w in wf.waypoints if w.flags & flags["DOOR"]]
    assert doorway and {w.flags for w in doorway} == {want}
    # The other team cannot route around the doorway waypoint.
    for a, b in _crossings(wf, 488):
        assert want in (a.flags, b.flags)


def test_a_touch_door_opens_for_everyone(tmp_path):
    path = _door_map(tmp_path, allowteams="axis", spawnflags=8)
    _wf, report = _seed(path, spacing=64)
    assert report["doors"] == [{"entity": 3, "flags": ["DOOR"]}]


@pytest.mark.parametrize("keys", [{"key": -1}, {"targetname": "gate"}])
def test_a_door_no_player_opens_is_seeded_as_a_wall(tmp_path, keys):
    door = _brush_entity("func_door", box((484, 96, 0), (492, 160, 128)), **keys)
    opener = point_entity("target_relay", (64, 64, 64), target="gate")
    path = write_map(tmp_path, [*room(0, 0, 1024, 256), *_PARTITION], [*_SPAWNS, door, opener])
    wf, report = _seed(path, spacing=64)
    flags = spec()["flags"]
    assert report["locked_doors"] == [3] and report["doors"] == []
    assert not any(w.flags & flags["DOOR"] for w in wf.waypoints)
    crossings = _crossings(wf, 488)
    assert crossings
    assert all(a.flags & flags["BLOCKWALL"] and b.flags & flags["BLOCKWALL"] for a, b in crossings)


def _trench_map(tmp_path, **keys):
    # Banks at floor 0 either side of a trench x 384..640, 208 deep; a constructible deck
    # (allies, unbuilt unless keys say so) spans it at y 96..160.
    brushes = [
        box((0, 0, -224), (1024, 256, -208)),
        box((0, 0, -208), (384, 256, 0)),
        box((640, 0, -208), (1024, 256, 0)),
        box((0, 0, 256), (1024, 256, 272)),
        box((0, 0, -208), (16, 256, 256)),
        box((1008, 0, -208), (1024, 256, 256)),
        box((0, 0, -208), (1024, 16, 256)),
        box((0, 240, -208), (1024, 256, 256)),
    ]
    deck = _brush_entity(
        "func_constructible", box((384, 96, -16), (640, 160, 0)), **{"spawnflags": 8, **keys}
    )
    return write_map(tmp_path, brushes, [*_SPAWNS, deck])


@pytest.mark.parametrize("spacing", [64, 128])
def test_an_unbuilt_bridge_is_crossed_only_through_blockbridge_links(tmp_path, spacing):
    wf, report = _seed(_trench_map(tmp_path), spacing=spacing)
    flags = spec()["flags"]
    bridge = flags["BLOCKBRIDGE"]
    depth = spec()["bridge_trace_depth"]
    assert report["constructibles"] == {"unbuilt": [3], "built_at_start": []}
    assert report["deck_cells"] > 0 and report["bridge_waypoints"] > 0
    # Spanned lip to lip, as stock files do.
    spans = [
        (w, wf.waypoints[t])
        for w in wf.waypoints
        for t in w.connections
        if w.position[0] < 384 and wf.waypoints[t].position[0] > 640
    ]
    assert spans and all(a.flags & bridge and b.flags & bridge for a, b in spans)
    assert report["components"]["count"] == 1

    def on_deck(x, y):
        return 384 <= x <= 640 and 96 <= y <= 160

    def side(w):
        return "trench" if w.position[2] < 0 else "west" if w.position[0] < 512 else "east"

    for w in wf.waypoints:
        # Nothing stands on the deck or over the trench: the add-on would make a deck
        # waypoint the constructible's goal, unreachable until it is built.
        assert not (384 < w.position[0] < 640 and side(w) != "trench"), w.position
        for t in w.connections:
            o = wf.waypoints[t]
            if "trench" in (side(w), side(o)):
                continue  # falling in (the deck's sides) is the trench floor's business
            touches = on_deck(*w.position[:2]) or on_deck(*o.position[:2])
            if not touches and side(w) == side(o):
                continue
            # A link onto, off or across the deck is one the add-on tests: both ends flagged,
            # and its middle is over the deck within the trace's reach, so it is closed while
            # the deck is missing and open once it is built.
            assert w.flags & bridge and o.flags & bridge, (w.position, o.position)
            mx, my = (w.position[0] + o.position[0]) / 2, (w.position[1] + o.position[1]) / 2
            mz = (w.position[2] + o.position[2]) / 2
            assert on_deck(mx, my) and mz - depth <= 0 <= mz, (w.position, o.position)


def test_a_bridge_built_at_start_is_still_gated(tmp_path):
    # It can be blown up, and then the deck is gone.
    wf, report = _seed(_trench_map(tmp_path, spawnflags=9), spacing=128)
    assert report["constructibles"] == {"unbuilt": [], "built_at_start": [3]}
    assert any(w.flags & spec()["flags"]["BLOCKBRIDGE"] for w in wf.waypoints)


def test_a_constructible_barrier_is_a_wall_once_built(tmp_path):
    # Unbuilt it is no obstacle; built, it shuts the gap, so links through it are blockable.
    barrier = _brush_entity("func_constructible", box((480, 96, 0), (496, 160, 128)), spawnflags=4)
    path = write_map(tmp_path, [*room(0, 0, 1024, 256), *_PARTITION], [*_SPAWNS, barrier])
    wf, report = _seed(path, spacing=64)
    flags = spec()["flags"]
    crossings = _crossings(wf, 488)
    assert report["components"]["count"] == 1 and crossings
    assert all(a.flags & flags["BLOCKWALL"] and b.flags & flags["BLOCKWALL"] for a, b in crossings)
    assert not any(w.flags & flags["BLOCKBRIDGE"] for w in wf.waypoints)


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


@pytest.fixture
def nrcroom(tmp_path) -> Path:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    srv.map_open(str(tmp_path / "maps" / "nrcroom.map"))
    return tmp_path


def test_tool_stages_the_file_where_the_addon_layout_says(nrcroom, tmp_path_factory):
    report = srv.bot_waypoints_write(spacing=96)
    staged = nrcroom / "omni-bot" / "et" / "nav" / "nrcroom.way"
    assert report["path"] == str(staged) and staged.is_file()
    assert report["install_as"] == "et/nav/nrcroom.way"
    assert (report["cell"], report["spacing"]) == (16, 96)
    wf = botnav.read(staged.read_bytes(), spec()["format"])
    assert len(wf.waypoints) == report["waypoints"] > 0
    assert report["pins"]["spawn"] == 4 and "omitted" in report
    assert report["installed"] is None
    full = srv.bot_waypoints_write(spacing=96, detail="full")
    assert {p["kind"] for p in full["pins"]} >= {"spawn"}
    addon = tmp_path_factory.mktemp("addon")
    installed = srv.bot_waypoints_write(spacing=96, omnibot_path=str(addon))
    assert (addon / "et" / "nav" / "nrcroom.way").read_bytes() == staged.read_bytes()
    assert installed["installed"] == str(addon / "et" / "nav" / "nrcroom.way")
    with pytest.raises(ToolError, match="not a folder"):
        srv.bot_waypoints_write(omnibot_path=str(addon / "missing"))
    with pytest.raises(ToolError, match="spacing"):
        srv.bot_waypoints_write(spacing=4)


def test_bot_playtest_validates_before_starting_anything(nrcroom, tmp_path):
    with pytest.raises(ToolError, match="seconds"):
        srv.bot_playtest(str(tmp_path / "x.pk3"), seconds=5)
    with pytest.raises(ToolError, match="bots"):
        srv.bot_playtest(str(tmp_path / "x.pk3"), bots=0)
    with pytest.raises(ToolError, match="does not exist"):
        srv.bot_playtest(str(tmp_path / "x.pk3"))


def test_movement_counts_travel_waypoints_and_stances():
    waypoints = [botnav.Waypoint((0.0, 0.0, 24.0)), botnav.Waypoint((200.0, 0.0, 24.0))]
    samples = [
        {
            "time": 0,
            "bots": [{"client": 1, "team": "red", "position": [0, 0, 24], "stance": "stand"}],
        },
        {
            "time": 2000,
            "bots": [{"client": 1, "team": "red", "position": [100, 0, 24], "stance": "crouch"}],
        },
        {
            "time": 4000,
            "bots": [{"client": 1, "team": "red", "position": [190, 0, 24], "stance": "crouch"}],
        },
        {
            "time": 6000,
            "bots": [{"client": 1, "team": "red", "position": [190, 0, 24], "dead": True}],
        },
        {
            "time": 8000,
            "bots": [{"client": 1, "team": "red", "position": [5000, 0, 24], "stance": "stand"}],
        },
    ]
    (row,) = botplay.movement(samples, waypoints, reach=70)
    assert row["travelled"] == 190
    assert row["waypoints_passed"] == 2
    assert row["stances_seen"] == ["crouch", "stand"]
