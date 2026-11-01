"""Curved surfaces and the lighting report, through the tool surface.

The patches were loaded in ET: Legacy once: a cylinder drawn from outside, an arch's underside
from below, a bevel as a rounded corner - which is how `facing` was settled.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel
from nrc_mcp import server as srv

ROOM = {
    "op": "hollow",
    "solid": {"op": "box", "min": [0, 0, 0], "max": [512, 512, 256]},
    "thickness": 16,
}


@pytest.fixture(autouse=True)
def _session():
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    yield
    srv.SESSION.map = None
    srv.SESSION.path = None


def test_patches_are_written_as_patchdef2_and_round_trip(tmp_path):
    path = tmp_path / "c.map"
    srv.map_new(str(path))
    srv.solid_commit(ROOM, label="room")
    col = srv.patch_commit("cylinder", [256, 256, 16], 24, 224, "t/column")
    arch = srv.patch_commit("arch", [128, 256, 128], 64, 32, "t/arch", axis="x")
    bevel = srv.patch_commit("bevel", [32, 32, 16], 16, 224, "t/trim", start_deg=0)
    assert (col["width"], arch["width"], bevel["width"]) == (9, 5, 3)
    srv.map_save(allow_non_identical=True)
    reopened = srv.map_open(str(path))
    assert reopened["round_trip"]["identical"] and reopened["patches"] == 3
    patch_codes = {
        f["code"]
        for f in srv.validate(severity_min="info")["findings"]
        if f["code"].startswith("PATCH")
    }
    assert patch_codes == set(), "dimensions consistent, patchDef2, inside the world"
    assert path.read_text().count("patchDef2") == 3


def test_facing_in_reverses_the_grid_and_bad_kinds_are_refused(tmp_path):
    srv.map_new(str(tmp_path / "f.map"))
    srv.patch_commit("cylinder", [0, 0, 0], 32, 64, "t/a", facing="out")
    srv.patch_commit("cylinder", [0, 0, 0], 32, 64, "t/a", facing="in")
    out, inward = (p for p in srv.SESSION.map.patches())
    assert out["bounds"] == inward["bounds"]
    with pytest.raises(ToolError, match="unknown patch kind"):
        srv.patch_commit("sphere", [0, 0, 0], 32, 64, "t/a")
    with pytest.raises(ToolError, match="facing must be"):
        srv.patch_commit("arch", [0, 0, 0], 32, 64, "t/a", facing="up")


def test_the_lighting_report_reads_lights_as_q3map2_does(tmp_path):
    srv.map_new(str(tmp_path / "l.map"))
    srv.solid_commit(ROOM, label="room")
    assert [f["code"] for f in srv.lighting_report()["findings"]] == ["LIGHT_NONE"]
    srv.entity_set_keys(0, {"_ambient": "10"})
    assert srv.lighting_report()["findings"] == [], "ambient light is light"
    srv.entity_remove_keys(0, ["_ambient"])
    srv.entity_place("light", [64, 64, 128])
    srv.map_save(allow_non_identical=True)
    r = srv.lighting_report(reach=128, cell=32)
    assert r["lights"] == 1 and r["intensity"]["max"] == 300, "q3map2's default intensity"
    assert r["dim_cells"] > 0 and r["findings"][0]["code"] == "LIGHT_DIM_AREA"
    assert r["findings"][0]["severity"] == "info", "a heuristic never warns"
