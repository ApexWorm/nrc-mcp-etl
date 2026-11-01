"""The release-file tools against the shipped ET: Legacy profile and the nrcroom fixture.

Loaded in ET: Legacy once: the command map shows on the HUD compass with its icons, and the
game loads the shader file and the TGA.
"""

from __future__ import annotations

import shutil
import struct
import zipfile
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel, pack, profiles
from nrc_mcp import server as srv

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


@pytest.fixture
def root(tmp_path) -> Path:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    (tmp_path / "scripts" / "nrcroom.arena").unlink()
    srv.map_open(str(tmp_path / "maps" / "nrcroom.map"))
    return tmp_path


def test_arena_write_fills_the_name_and_defaults_and_refuses_unknown_keys(root):
    r = srv.arena_write("NRC Room", {"briefing": "Two bunkers."})
    text = (root / "scripts" / "nrcroom.arena").read_text()
    assert pack._arena_keys(text) == {
        "map": "nrcroom",
        "longname": "NRC Room",
        "type": "wolfmp wolfsw",
        "briefing": "Two bunkers.",
    }
    assert r["missing_required"] == []
    with pytest.raises(ToolError, match="unknown arena key"):
        srv.arena_write("x", {"colour": "red"})
    with pytest.raises(ToolError, match="no double quotes"):
        srv.arena_write('say "hi"')


def test_objdata_write_numbers_objectives_per_team(root):
    srv.objdata_write({"neutral": "Blow it up."}, {"axis": ["First", "Second"]})
    assert (root / "maps" / "nrcroom.objdata").read_text().splitlines() == [
        'wm_mapdescription neutral "Blow it up."',
        'wm_objective_axis_desc 1 "First"',
        'wm_objective_axis_desc 2 "Second"',
    ]
    with pytest.raises(ToolError, match="team must be one of"):
        srv.objdata_write({"red": "x"})


def test_commandmap_make_frames_a_square_and_writes_image_and_shaders(root):
    r = srv.commandmap_make()
    (x0, y_top), (x1, y_bottom) = r["mapcoordsmins"], r["mapcoordsmaxs"]
    assert x1 - x0 == y_top - y_bottom > 0, "the game needs a square, top-left then bottom-right"
    world = dict(srv.query_entities()["entities"][0]["keys"])
    assert world["mapcoordsmins"] == f"{x0} {y_top}"
    tga = (root / "levelshots" / "nrcroom_cc.tga").read_bytes()
    assert tga[2] == 2 and struct.unpack("<HH", tga[12:16]) == (512, 512) and tga[16] == 24
    shaders = (root / "scripts" / "levelshots_nrcroom.shader").read_text()
    assert "levelshots/nrcroom_cc_automap" in shaders and "levelshots/nrcroom_cc_trans" in shaders
    assert "{map}" not in shaders and "{{" not in shaders


def test_levelshot_make_takes_the_profiles_size(root):
    r = srv.levelshot_make([96, 96, 0], 0)
    tga = Path(r["path"]).read_bytes()
    assert struct.unpack("<HH", tga[12:16]) == (256, 256)


def _pk3(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n, t in files.items():
            z.writestr(n, t)
    return path


def test_ship_check_reads_the_arena_and_does_not_take_a_command_map_for_a_levelshot(tmp_path):
    base = {"maps/m.bsp": "x", "maps/m.script": "x", "levelshots/m_cc.tga": "x"}
    bad = _pk3(tmp_path / "a.pk3", {**base, "scripts/m.arena": '{ map "other" }'})
    codes = {f["code"] for f in pack.ship_check(tmp_path / "m.bsp", "etl", pk3=bad)["findings"]}
    assert {"SHIP_NO_LEVELSHOT", "SHIP_ARENA_KEYS", "SHIP_ARENA_MAP_NAME"} <= codes
    good = _pk3(
        tmp_path / "b.pk3",
        {**base, "levelshots/m.jpg": "x", "scripts/m.arena": '{ map "m" longname "M" type "a b" }'},
    )
    codes = {f["code"] for f in pack.ship_check(tmp_path / "m.bsp", "etl", pk3=good)["findings"]}
    assert not codes & {"SHIP_NO_LEVELSHOT", "SHIP_ARENA_KEYS", "SHIP_ARENA_MAP_NAME"}
