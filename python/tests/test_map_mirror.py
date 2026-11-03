"""map_mirror: the whole map reflected, geometry valid, entity keys mirrored as the profile says."""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel, profiles
from nrc_mcp import server as srv

BOX = """{{
brushDef
{{
( {x0} 0 0 ) ( {x0} 64 0 ) ( {x0} 0 64 ) ( ( 0.03125 0 0 ) ( 0 0.03125 0 ) ) {tex} 0 0 0
( {x1} 0 0 ) ( {x1} 0 64 ) ( {x1} 64 0 ) ( ( 0.03125 0 0 ) ( 0 0.03125 0 ) ) {tex} 0 0 0
( 0 0 0 ) ( 0 0 64 ) ( 64 0 0 ) ( ( 0.03125 0 0.25 ) ( 0 0.03125 0 ) ) {tex} 0 0 0
( 0 64 0 ) ( 64 64 0 ) ( 0 64 64 ) ( ( 0.03125 0 0 ) ( 0 0.03125 0 ) ) {tex} 0 0 0
( 0 0 0 ) ( 64 0 0 ) ( 0 64 0 ) ( ( 0.03125 0 0 ) ( 0 0.03125 0.5 ) ) {tex} 0 0 0
( 0 0 64 ) ( 0 64 64 ) ( 64 0 64 ) ( ( 0.03125 0 0 ) ( 0 0.03125 0 ) ) {tex} 0 0 0
}}
}}
"""


def box(x0: int, x1: int, tex: str = "common/caulk") -> str:
    return BOX.format(x0=x0, x1=x1, tex=tex)


MAP = (
    '{\n"classname" "worldspawn"\n"mapcoordsmins" "-1000 900"\n"mapcoordsmaxs" "3000 -900"\n'
    + box(100, 200)
    + "}\n"
    + '{\n"classname" "info_player_deathmatch"\n"origin" "300 40 24"\n"angle" "30"\n}\n'
    + '{\n"classname" "misc_model"\n"origin" "-50 8 0"\n"angles" "10 30 5"\n}\n'
    + '{\n"classname" "func_door"\n"angle" "-1"\n'
    + box(400, 432)
    + "}\n"
    + '{\n"classname" "func_door_rotating"\n"spawnflags" "2"\n'
    + box(500, 504)
    + "}\n"
    + '{\n"classname" "func_door_rotating"\n"spawnflags" "4"\n'
    + box(600, 604)
    + "}\n"
    # A vehicle whose body runs +x from its origin brush (centre x 1000).
    + '{\n"classname" "script_mover"\n"targetname" "car"\n'
    + box(992, 1008, "common/origin")
    + box(1008, 1200, "common/caulk")
    + "}\n"
)


@pytest.fixture(autouse=True)
def _session(tmp_path, monkeypatch):
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    if "etl" not in profiles.available():
        pytest.skip("no etl profile")
    monkeypatch.setenv("NRC_PROFILE", "etl")
    path = tmp_path / "maps" / "m.map"
    path.parent.mkdir()
    path.write_text(MAP)
    srv.map_open(str(path))
    yield
    srv.SESSION.map = None
    srv.SESSION.path = None


def keys(index: int) -> dict:
    return dict(srv.SESSION.map.entities(with_keys=True)[index]["keys"])


def xs(entity: int, prim: int) -> tuple[float, float]:
    v = srv.SESSION.map.brush_geometry(entity, prim)["vertices"]
    return min(p[0] for p in v), max(p[0] for p in v)


def test_geometry_is_reflected_and_stays_valid():
    before = srv.SESSION.map.validate(1, "error")["summary"]
    r = srv.map_mirror("x", 0, ["car"])
    assert r["geometry"]["brushes"] == 6 and r["geometry"]["degenerate_faces"] == 0
    assert xs(0, 0) == (-200, -100)
    assert srv.SESSION.map.validate(1, "error")["summary"] == before
    for entity, prim, _ in srv.SESSION.map.brush_sources():
        assert srv.SESSION.map.brush_geometry(entity, prim)["usable"]


def test_entity_keys_follow_the_mirror():
    srv.map_mirror("x", 0)
    assert keys(0)["mapcoordsmins"] == "-3000 900" and keys(0)["mapcoordsmaxs"] == "1000 -900"
    assert keys(1)["origin"] == "-300 40 24"
    assert keys(1)["angle"] == "150"  # 180 - 30
    assert keys(2)["origin"] == "50 8 0"
    assert keys(2)["angles"] == "10 150 355"  # p, 180 - y, -r
    assert keys(3)["angle"] == "-1"  # "up" is not a facing
    # A door turning about z swings the other way; one turning about x does not.
    assert keys(4)["spawnflags"] == str(2 | 16)
    assert keys(5)["spawnflags"] == "4"


def test_mirror_in_y_and_about_a_plane():
    srv.map_mirror("y", 100)
    assert keys(1)["origin"] == "300 160 24"
    assert keys(1)["angle"] == "330"  # -30


def test_a_vehicle_keeps_its_body_ahead_of_its_origin():
    srv.map_mirror("x", 0, ["car"])
    # The origin brush is mirrored to x 1000 -> -1000; the body still runs +x from it.
    assert xs(6, 0) == (-1008, -992)
    assert xs(6, 1) == (-992, -800)


def test_without_vehicles_the_body_is_reflected_behind_the_origin():
    srv.map_mirror("x", 0)
    assert xs(6, 1) == (-1200, -1008)


def test_refusals():
    with pytest.raises(ToolError):
        srv.map_mirror("z")
    with pytest.raises(ToolError):
        srv.map_mirror("x", 0, ["nobody"])
