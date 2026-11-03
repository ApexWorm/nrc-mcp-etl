"""The map-script tools, end to end: the real kernel, the shipped ET: Legacy profile, and the
nrcroom fixture, which has been loaded and played in etlded."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel, profiles
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
def nrcroom(tmp_path) -> Path:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    srv.map_open(str(tmp_path / "maps" / "nrcroom.map"))
    return tmp_path / "maps" / "nrcroom.script"


def test_script_show_says_which_entities_run_each_block(nrcroom):
    r = srv.script_show()
    blocks = {b["block"]: b for b in r["blocks"]}
    assert set(blocks) == {"game_manager", "gate"}
    assert blocks["game_manager"]["runs_for_entities"], "script_multiplayer runs it implicitly"
    assert blocks["gate"]["runs_for_entities"], "the func_explosive's scriptname names it"
    spawn = blocks["game_manager"]["events"][0]
    assert spawn["event"] == "spawn" and 'setautospawn "Axis Bunker" 0' in spawn["actions"]
    assert r["summary"]["error"] == 0


def test_set_event_stages_until_save_and_reports_what_does_not_resolve_yet(nrcroom):
    before = nrcroom.read_text()
    r = srv.script_set_event(
        "gate", "trigger", ['wm_announce "Gate!"', "alertentity lever"], params=["open"]
    )
    assert r["did"] == "added event"
    assert nrcroom.read_text() == before
    assert r["staged"]
    text = srv.SESSION.read_file(nrcroom).decode()
    assert text.startswith(before[: before.index("gate\n{")]), "everything before is untouched"
    assert "\ttrigger open\n" in text
    codes = {f["code"]: f["severity"] for f in r["findings"]}
    assert codes.get("MAPSCRIPT_UNRESOLVED_REFERENCE") == "error", "no entity is named lever"
    assert srv.validate_profile()["summary"]["error"] == 1, "validate_profile sees the same"


def test_an_unknown_action_is_refused_and_nothing_is_written(nrcroom):
    before = nrcroom.read_text()
    with pytest.raises(ToolError, match="refused, nothing written: unknown action 'teleport'"):
        srv.script_set_event("gate", "spawn", ["teleport 0 0 0"])
    assert nrcroom.read_text() == before


def test_remove_takes_out_an_event_or_a_block(nrcroom):
    srv.script_remove("gate", "death")
    assert "death" not in srv.SESSION.read_file(nrcroom).decode()
    r = srv.script_remove("gate")
    assert [b["block"] for b in r["blocks"]] == ["game_manager"]
    with pytest.raises(ToolError, match="no block named 'gate'"):
        srv.script_remove("gate")


def test_a_map_built_from_nothing_gets_its_script_through_the_tools(tmp_path):
    srv.map_new(str(tmp_path / "maps" / "fresh.map"))
    srv.entity_place("script_multiplayer", [0, 0, 64])
    assert srv.script_show()["exists"] is False
    r = srv.script_set_event(
        "game_manager", "spawn", ["wm_axis_respawntime 10", "wm_setwinner 0", "wait 50"]
    )
    assert r["did"] == "created block"
    assert not (tmp_path / "maps" / "fresh.script").exists()
    srv.map_save(check_playable_space=False)
    assert (tmp_path / "maps" / "fresh.script").read_text().startswith("game_manager\n{\n\tspawn")
    assert r["summary"] == {"error": 0, "warning": 0, "info": 0}


def test_a_profile_without_map_scripts_says_so(nrcroom, monkeypatch):
    if "urt43" not in profiles.available():
        pytest.skip("no urt43 profile on disk")
    monkeypatch.setenv("NRC_PROFILE", "urt43")
    with pytest.raises(ToolError, match="describes no map script"):
        srv.script_show()


def test_script_show_quotes_arguments_so_actions_can_be_written_back(nrcroom):
    spawn = srv.script_show()["blocks"][0]["events"][0]["actions"]
    assert 'setautospawn "Axis Bunker" 0' in spawn
    srv.script_set_event("game_manager", "spawn", spawn)
    assert srv.validate_profile()["summary"]["error"] == 0, "round-trips without breaking names"


def test_staged_lifetime_findings_reach_script_and_profile_reports(nrcroom):
    original = nrcroom.read_text()
    edited = srv.script_set_event("gate", "death", ["wait 1500", "wm_endround"])
    assert nrcroom.read_text() == original, "analysis must inspect the staged edit"
    for report in (edited, srv.script_show(), srv.validate_profile()):
        (row,) = [f for f in report["findings"] if f["code"] == "MAPSCRIPT_LIFETIME_WAIT"]
        assert row["severity"] == "warning" and "gate.death" in row["message"]
    srv.script_set_event("game_manager", "trigger", ["wait 1500", "wm_endround"], params=["finish"])
    srv.script_set_event("gate", "death", ["trigger game_manager finish"])
    assert not [
        f for f in srv.script_show()["findings"] if f["code"].startswith("MAPSCRIPT_LIFETIME")
    ]


def test_objective_place_builds_each_kind_that_validates_clean(nrcroom):
    r = srv.objective_place(
        "dynamite",
        "radio",
        "the Allied Radio",
        "allies",
        objective={"min": [800, 400, 0], "max": [864, 464, 64]},
        trigger={"min": [768, 368, 0], "max": [896, 496, 128]},
    )
    assert [e["classname"] for e in r["entities"]] == ["trigger_objective_info", "func_explosive"]
    assert r["summary"]["error"] == 0 and r["summary"]["warning"] == 0
    toi, boom = (srv.query_entities()["entities"][e["entity"]] for e in r["entities"])
    assert dict(toi["keys"])["target"] == "radio" and dict(toi["keys"])["spawnflags"] == "2"
    assert dict(boom["keys"])["constructible_class"] == "3"

    c = srv.objective_place(
        "constructible",
        "barrier",
        "the Axis Barrier",
        "axis",
        objective={"min": [300, 16, 0], "max": [316, 160, 96]},
        trigger={"min": [256, 16, 0], "max": [384, 208, 128]},
    )
    assert c["summary"]["error"] == 0
    text = srv.SESSION.read_file(nrcroom).decode()
    assert "built final" in text and "decayed final" in text


def test_objective_place_refuses_before_changing_anything(nrcroom):
    before = (len(srv.query_entities()["entities"]), nrcroom.read_text())
    for kwargs, msg in [
        ({"kind": "flag"}, "unknown objective kind"),
        ({"team": "red"}, "team must be one of"),
        ({"name": "gate"}, "already uses the name 'gate'"),
        ({"track": 'say "hi"'}, "no double quotes"),
    ]:
        args = {
            "kind": "dynamite",
            "name": "radio",
            "track": "x",
            "team": "allies",
            "objective": {"min": [0, 0, 0], "max": [8, 8, 8]},
            "trigger": {"min": [0, 0, 0], "max": [8, 8, 8]},
        }
        with pytest.raises(ToolError, match=msg):
            srv.objective_place(**{**args, **kwargs})
    assert (len(srv.query_entities()["entities"]), nrcroom.read_text()) == before


def _keys_of(index: int) -> dict:
    return dict(srv.query_entities()["entities"][index]["keys"])


def test_path_place_links_points_and_controls_as_the_engine_reads_them(nrcroom):
    r = srv.path_place(
        "rail",
        [
            [64, 64, 32],
            {"at": [320, 64, 32], "controls": [[448, 64, 32], [448, 96, 32]]},
            [448, 256, 32],
        ],
        max_segment=128,
    )
    assert r["start"] == "rail_0" and r["segments"] == 3, "the first straight leg splits in two"
    names = [p["name"] for p in r["points"]]
    assert names == ["rail_0", "rail_1", "rail_2", "rail_3"]
    assert [p["origin"] for p in r["points"]][1] == [192.0, 64.0, 32.0]
    first, bend, last = (_keys_of(r["points"][i]["entity"]) for i in (0, 2, 3))
    assert first["classname"] == "info_train_spline_main" and first["target"] == "rail_1"
    assert bend["control"] == "rail_2_c0" and bend["control2"] == "rail_2_c1"
    assert "target" not in last, "the path ends"
    controls = srv.query_entities(classname="info_train_spline_control")["entities"]
    assert sorted(dict(e["keys"])["targetname"] for e in controls) == ["rail_2_c0", "rail_2_c1"]
    assert r["points"][2]["segment_length"] > math.dist([320, 64], [448, 256])
    assert not [f for f in r["findings"] if f["code"].startswith("ETL_SPLINE")]


def test_path_place_refuses_before_changing_anything(nrcroom):
    before = srv.SESSION.map.source()
    srv.path_place("taken", [[0, 0, 0], [64, 0, 0]])
    taken_source = srv.SESSION.map.source()
    for points, kwargs, msg in [
        ([[0, 0, 0]], {}, "at least two points"),
        ([[0, 0, 0], {"at": [8, 0, 0], "controls": [[1, 1, 1]]}], {}, "last point"),
        ([{"at": [0, 0, 0], "controls": [[1, 1, 1]] * 5}, [8, 0, 0]], {}, "at most 4"),
        ([[0, 0], [8, 0, 0]], {}, "three finite numbers"),
        ([[0, 0, 0], [8, 0, 0]], {"name": "taken"}, "already named 'taken_0'"),
    ]:
        with pytest.raises(ToolError, match=msg):
            srv.path_place(kwargs.get("name", "fresh"), points)
    assert srv.SESSION.map.source() == taken_source != before


def test_escort_moves_a_vehicle_and_its_escort_volume_along_the_path(nrcroom):
    path = srv.path_place("route", [[96, 96, 32], [352, 96, 32], [352, 352, 32]])
    r = srv.objective_place(
        "escort",
        "truck",
        "the Truck",
        "allies",
        objective={"min": [64, 72, 0], "max": [128, 120, 64]},
        trigger={"min": [0, 0, 0], "max": [192, 192, 128]},
        path=path["start"],
        options={"speed": 80},
    )
    assert r["summary"]["error"] == 0 and r["summary"]["warning"] == 0, r["findings"]
    made = {e["role"]: e for e in r["entities"]}
    assert made["objective"]["classname"] == "script_mover"
    assert made["objective"]["brushes"] == 1, "the origin brush comes on top"
    for role in ("objective", "trigger"):
        sources = [s for e, _p, s in srv.SESSION.map.brush_sources() if e == made[role]["entity"]]
        assert any("common/origin" in s for s in sources), f"{role} turns about the path start"
        origin_brush = next(s for s in sources if "common/origin" in s)
        assert "( 88 104 40 )" in origin_brush and "( 104 88 24 )" in origin_brush
    escort = _keys_of(made["trigger"]["entity"])
    assert escort["classname"] == "trigger_multiple" and escort["spawnflags"] == "2"
    assert escort["target"] == "truck_enabler"
    assert _keys_of(made["ticker"]["entity"])["target"] == "truck_disabler"
    text = srv.SESSION.read_file(nrcroom).decode()
    assert "followspline 0 route_0 80 wait" in text and "followspline 0 route_1 80 wait" in text
    assert "accum 0 trigger_if_equal 2 truck arrive" in text
    assert "trigger truck_escort run_1" in text
    blocks = {b["block"]: b for b in srv.script_show()["blocks"]}
    assert all(blocks[b]["runs_for_entities"] for b in ("truck", "truck_escort", "truck_enabler"))


def test_escort_refuses_without_a_path_or_with_a_bad_option(nrcroom):
    path = srv.path_place("route", [[96, 96, 32], [352, 96, 32]])
    args = {
        "kind": "escort",
        "name": "truck",
        "track": "x",
        "team": "axis",
        "objective": {"min": [64, 64, 0], "max": [128, 128, 64]},
        "trigger": {"min": [0, 0, 0], "max": [192, 192, 128]},
        "path": path["start"],
    }
    before = (srv.SESSION.map.source(), nrcroom.read_text())
    for kwargs, msg in [
        ({"path": ""}, "follows a path"),
        ({"path": "nowhere"}, "no info_train_spline_main is named 'nowhere'"),
        ({"path": "route_1"}, "one point and no segment"),
        ({"options": {"speed": "fast"}}, "option speed 'fast'"),
        ({"options": {"colour": 1}}, "no option 'colour'"),
        ({"kind": "dynamite"}, "does not follow a path"),
        ({"name": "route_0"}, "already uses the name"),
    ]:
        with pytest.raises(ToolError, match=msg):
            srv.objective_place(**{**args, **kwargs})
    assert (srv.SESSION.map.source(), nrcroom.read_text()) == before


@pytest.mark.parametrize(
    "team,item,flag", [("axis", "team_CTF_redflag", "1"), ("allies", "team_CTF_blueflag", "2")]
)
def test_steal_lays_the_item_and_a_delivery_volume_for_the_other_team(nrcroom, team, item, flag):
    r = srv.objective_place(
        "steal",
        "docs",
        "the Documents",
        team,
        objective={"origin": [200, 200, 24]},
        trigger={"min": [400, 400, 0], "max": [480, 480, 96]},
    )
    assert r["summary"]["error"] == 0 and r["summary"]["warning"] == 0, r["findings"]
    made = {e["role"]: e for e in r["entities"]}
    docs = _keys_of(made["objective"]["entity"])
    assert docs["classname"] == item and docs["origin"] == "200 200 24"
    assert "targetname" not in docs, "a targeted item never appears"
    assert docs["model"].endswith(".md3") and docs["message"] == "the Documents"
    deliver = _keys_of(made["trigger"]["entity"])
    assert deliver["classname"] == "trigger_flagonly_multiple" and deliver["spawnflags"] == flag
    text = srv.SESSION.read_file(nrcroom).decode()
    assert "trigger stolen" in text and "trigger returned" in text
    assert "docs_deliver\n{\n\tdeath" in text
    with pytest.raises(ToolError, match="is a brush entity here"):
        srv.objective_place(
            "dynamite", "radio", "x", "axis", objective={"origin": [0, 0, 0]}, trigger=_box()
        )


def _box():
    return {"min": [0, 0, 0], "max": [64, 64, 64]}


def test_followspline_to_a_point_that_does_not_exist_is_an_error(nrcroom):
    srv.path_place("route", [[96, 96, 32], [352, 96, 32]])
    r = srv.script_set_event("gate", "trigger", ["followspline 0 ROUTE_0 50 wait"], params=["go"])
    assert not [f for f in r["findings"] if f["code"] == "MAPSCRIPT_UNRESOLVED_REFERENCE"]
    r = srv.script_set_event("gate", "trigger", ["followspline 0 route_9 50 wait"], params=["go"])
    (f,) = [f for f in r["findings"] if f["code"] == "MAPSCRIPT_UNRESOLVED_REFERENCE"]
    assert "route_9" in f["message"]
