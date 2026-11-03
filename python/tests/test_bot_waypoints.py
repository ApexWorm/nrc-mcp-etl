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
    assert report["components"]["count"] == 1
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
