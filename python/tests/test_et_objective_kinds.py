"""Spawn flags, command posts and team doors: the objective_place recipes that build them, and
the rules that catch what the engine stops on or silently ignores."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel, profiles, rules
from nrc_mcp import server as srv

FIXTURE = Path(__file__).resolve().parents[2] / "bench" / "tapes" / "fixtures" / "nrcroom"


def ent(index: int, classname: str, **keys) -> dict:
    return {
        "index": index,
        "classname": classname,
        "origin": None,
        "keys": [("classname", classname), *keys.items()],
        "brushes": 0,
        "patches": 0,
    }


def _rule(check: str, **params) -> dict:
    return {
        "id": "T",
        "check": check,
        "severity": "error",
        "confidence": "verified",
        "message": "{found} {detail}",
        "params": params,
    }


def run(rule: dict, entities: list[dict], monkeypatch) -> list[rules.Finding]:
    monkeypatch.setattr(profiles, "load", lambda _pid: {"rules": [rule]})
    return rules.evaluate("test", entities)


# ---------------------------------------------------------------------------
# Rule engine: the checks these rules added
# ---------------------------------------------------------------------------


def test_forbid_selects_by_flags_and_keys(monkeypatch):
    rule = _rule(
        "forbid", classname="c", when_flags_any=[4, 8], when_flags_none=[1], when_keys=["k"]
    )
    assert run(rule, [ent(0, "c", spawnflags="4")], monkeypatch) == [], "needs the key too"
    assert run(rule, [ent(0, "c", spawnflags="5", k="x")], monkeypatch) == [], "1 excuses it"
    assert run(rule, [ent(0, "c", spawnflags="2", k="x")], monkeypatch) == [], "neither 4 nor 8"
    (f,) = run(rule, [ent(0, "c", spawnflags="8", k="x"), ent(1, "d", spawnflags="8")], monkeypatch)
    assert f.entities == [0]
    absent = _rule("forbid", classname="c", when_flags_all=[1, 8], when_keys_absent=["t"])
    assert run(absent, [ent(0, "c", spawnflags="9", t="n")], monkeypatch) == []
    assert run(absent, [ent(0, "c", spawnflags="9")], monkeypatch)
    assert run(absent, [ent(0, "c", spawnflags="8")], monkeypatch) == []


def test_second_target_checks_what_shares_the_first_targets_name(monkeypatch):
    classes = _rule("second_target", classname="t", first_classes=["a"], second_classes=["a"])
    sides = _rule(
        "second_target",
        classname="t",
        first_classes=["a"],
        second_classes=["a"],
        distinct_flags=[4, 8],
    )
    trig = ent(0, "t", target="Obj")
    pair = [
        trig,
        ent(1, "a", targetname="obj", spawnflags="4"),
        ent(2, "a", targetname="OBJ", spawnflags="8"),
    ]
    assert run(classes, pair, monkeypatch) == [] and run(sides, pair, monkeypatch) == []
    same = [
        trig,
        ent(1, "a", targetname="obj", spawnflags="12"),
        ent(2, "a", targetname="obj", spawnflags="4"),
    ]
    assert run(sides, same, monkeypatch), "12 counts as the first flag, like the engine"
    other = [trig, ent(1, "a", targetname="obj", spawnflags="4"), ent(2, "b", targetname="obj")]
    assert run(classes, other, monkeypatch) and run(sides, other, monkeypatch) == []
    first_b = [trig, ent(1, "b", targetname="obj"), ent(2, "c", targetname="obj")]
    assert run(classes, first_b, monkeypatch) == [], "only a first of first_classes counts"
    assert run(classes, [trig, ent(1, "a", targetname="obj")], monkeypatch) == []


def test_key_pattern_without_a_classname_reads_every_entity(monkeypatch):
    rule = _rule("key_pattern", key="k", pattern="(?is).*(axis|allies).*")
    good = [ent(0, "c", k="Axis,cvops"), ent(1, "d", k="allies"), ent(2, "e")]
    assert run(rule, good, monkeypatch) == []
    (f,) = run(rule, [ent(0, "c", k="allied"), ent(1, "d", k="cvops")], monkeypatch)
    assert f.entities == [0, 1]


# ---------------------------------------------------------------------------
# The ET: Legacy rules
# ---------------------------------------------------------------------------


def _etl_codes(*entities, severity=None) -> set[str]:
    world = ent(0, "worldspawn")
    return {
        f.code
        for f in rules.evaluate("etl", [world, *entities])
        if severity is None or f.severity == severity
    }


@pytest.fixture
def etl_profile():
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")


def test_a_command_post_pair_must_be_two_teams_constructibles(etl_profile):
    toi = ent(1, "trigger_objective_info", track="cp", target="cp")
    ok = (
        ent(2, "func_constructible", targetname="cp", spawnflags="4"),
        ent(3, "func_constructible", targetname="cp", spawnflags="8"),
    )
    assert not {
        "ETL_OBJECTIVE_INFO_SECOND_TARGET_CLASS",
        "ETL_OBJECTIVE_INFO_CONSTRUCTIBLE_PAIR_TEAMS",
    } & _etl_codes(toi, *ok)
    same = (ok[0], ent(3, "func_constructible", targetname="cp", spawnflags="4"))
    assert "ETL_OBJECTIVE_INFO_CONSTRUCTIBLE_PAIR_TEAMS" in _etl_codes(toi, *same, severity="error")
    wrong = (ok[0], ent(3, "func_brushmodel", targetname="cp"))
    codes = _etl_codes(toi, *wrong, severity="error")
    assert "ETL_OBJECTIVE_INFO_SECOND_TARGET_CLASS" in codes
    assert "ETL_OBJECTIVE_INFO_CONSTRUCTIBLE_PAIR_TEAMS" not in codes


def test_checkpoint_flag_combinations_the_engine_ignores(etl_profile):
    def cp(flags):
        return ent(1, "team_WOLF_checkpoint", scriptname="f", spawnflags=flags)

    assert "ETL_CHECKPOINT_TEAM_ONLY_IGNORED" in _etl_codes(cp("8"))
    assert "ETL_CHECKPOINT_TEAM_ONLY_IGNORED" not in _etl_codes(cp("9"))
    assert "ETL_CHECKPOINT_HOLD_NOT_TOUCHED" in _etl_codes(cp("2"))
    assert "ETL_CHECKPOINT_HOLD_NOT_TOUCHED" not in _etl_codes(cp("3"))
    assert "ETL_CHECKPOINT_NO_TEAM_RAISES" in _etl_codes(cp("13"))
    clean = _etl_codes(cp("1"))
    assert not {c for c in clean if c.startswith("ETL_CHECKPOINT")}


def test_a_spawn_region_marker_needs_an_owner(etl_profile):
    assert "ETL_SPAWN_REGION_OWNER" in _etl_codes(ent(1, "team_WOLF_objective", description="x"))
    owned = ent(1, "team_WOLF_objective", description="x", spawnflags="2")
    assert "ETL_SPAWN_REGION_OWNER" not in _etl_codes(owned)


def test_team_doors_that_open_for_everyone(etl_profile):
    door = {"allowteams": "axis,cvops"}
    assert not {"ETL_ALLOWTEAMS_NAMES_A_TEAM", "ETL_TEAM_DOOR_OPENS_ON_TOUCH"} & _etl_codes(
        ent(1, "func_door_rotating", **door), ent(2, "func_door", **door)
    )
    assert "ETL_ALLOWTEAMS_NAMES_A_TEAM" in _etl_codes(ent(1, "func_door", allowteams="allied"))
    assert "ETL_ALLOWTEAMS_NAMES_A_TEAM" in _etl_codes(ent(1, "func_door", allowteams="cvops"))
    touch = ent(1, "func_door", spawnflags="8", **door)
    assert "ETL_TEAM_DOOR_OPENS_ON_TOUCH" in _etl_codes(touch)
    named = ent(1, "func_door", spawnflags="8", targetname="d", **door)
    assert "ETL_TEAM_DOOR_OPENS_ON_TOUCH" not in _etl_codes(named), "a targeted door has no trigger"
    rotating = ent(1, "func_door_rotating", spawnflags="8", **door)
    assert "ETL_TEAM_DOOR_OPENS_ON_TOUCH" not in _etl_codes(rotating)


def test_allowteams_is_a_key_every_entity_takes(etl_profile):
    door = profiles.entity_def("etl", "func_door")
    assert "allowteams" in profiles.declared_keys("etl", door)


# ---------------------------------------------------------------------------
# objective_place: the new kinds, end to end on the nrcroom fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def nrcroom(tmp_path, monkeypatch, etl_profile) -> Path:
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    monkeypatch.setenv("NRC_PROFILE", "etl")
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    srv.map_open(str(tmp_path / "maps" / "nrcroom.map"))
    yield tmp_path / "maps" / "nrcroom.script"
    srv.SESSION.map = None
    srv.SESSION.path = None


def _keys(index: int) -> dict:
    return dict(srv.query_entities()["entities"][index]["keys"])


def test_checkpoint_builds_flag_marker_spawns_and_capture_events(nrcroom):
    r = srv.objective_place(
        "checkpoint",
        "fwd",
        "Forward Bunker",
        "axis",
        points={
            "flag": [512, 256, 0],
            "spawns": [[448, 192, 24], {"origin": [576, 192, 24], "angle": 90}],
        },
    )
    assert r["summary"] == {"error": 0, "warning": 0, "info": 0}, r["findings"]
    by_class: dict[str, list[int]] = {}
    for e in r["entities"]:
        by_class.setdefault(e["classname"], []).append(e["entity"])
    (flag,) = by_class["team_WOLF_checkpoint"]
    (marker,) = by_class["team_WOLF_objective"]
    assert _keys(flag) == {
        "classname": "team_WOLF_checkpoint",
        "origin": "512 256 0",
        "targetname": "fwd",
        "scriptname": "fwd",
        "target": "fwd_spawns",
        "spawnflags": "1",
    }
    assert _keys(marker)["spawnflags"] == "1" and _keys(marker)["description"] == "Forward Bunker"
    reds, blues = by_class["team_CTF_redspawn"], by_class["team_CTF_bluespawn"]
    assert len(reds) == len(blues) == 2
    assert {_keys(i)["spawnflags"] for i in reds} == {"3"}, "the holder's spawns start active"
    assert {_keys(i)["spawnflags"] for i in blues} == {"1"}
    assert {_keys(i)["targetname"] for i in reds + blues} == {"fwd_spawns"}
    assert _keys(reds[1])["angle"] == "90" and "angle" not in _keys(reds[0])
    assert r["links"]["spawns_targetname"] == "fwd_spawns"

    blocks = {b["block"]: b for b in srv.script_show()["blocks"]}
    events = {e["event"]: e["actions"] for e in blocks["fwd"]["events"]}
    assert events["spawn"] == ["accum 0 set 0"], "accum 0 starts as the holder"
    assert events["trigger allied_capture"] == [
        "accum 0 abort_if_equal 1",
        "accum 0 set 1",
        'wm_announce "Allies capture Forward Bunker!"',
        "alertentity fwd_wobj",
        'setautospawn "Forward Bunker" 1',
    ]
    assert blocks["fwd"]["runs_for_entities"]


def test_one_way_checkpoint_lets_only_the_other_team_raise_it(nrcroom):
    r = srv.objective_place(
        "checkpoint_one_way", "fwd", "Forward Bunker", "allies", points={"flag": [512, 256, 0]}
    )
    (flag,) = [e["entity"] for e in r["entities"] if e["classname"] == "team_WOLF_checkpoint"]
    assert _keys(flag)["spawnflags"] == "5", "SPAWNPOINT and AXIS_ONLY when Allies hold it"
    (marker,) = [e["entity"] for e in r["entities"] if e["classname"] == "team_WOLF_objective"]
    assert _keys(marker)["spawnflags"] == "2"
    assert "accum 0 set 1" in srv.SESSION.read_file(nrcroom).decode()
    # Without spawns the flag's target names nothing yet, and validation says so.
    assert any(f["code"] == "ETL_TARGETS_RESOLVE" for f in r["findings"])
    assert r["summary"]["error"] == 0


def test_neutral_command_post_builds_both_teams_constructibles(nrcroom):
    r = srv.objective_place(
        "command_post",
        "cp",
        "the Command Post",
        "neutral",
        objective={"min": [800, 400, 0], "max": [864, 464, 64]},
        trigger={"min": [768, 368, 0], "max": [896, 496, 128]},
    )
    assert r["summary"] == {"error": 0, "warning": 0, "info": 0}, r["findings"]
    toi, axis, allies = (_keys(e["entity"]) for e in r["entities"])
    assert toi["spawnflags"] == "64" and toi["target"] == "cp"
    assert (axis["spawnflags"], axis["scriptname"]) == ("4", "cp_axis")
    assert (allies["spawnflags"], allies["scriptname"]) == ("8", "cp_allies")
    assert axis["targetname"] == allies["targetname"] == "cp"
    assert axis["constructible_class"] == "2"
    blocks = {b["block"]: b for b in srv.script_show()["blocks"]}
    built = {e["event"]: e["actions"] for e in blocks["cp_allies"]["events"]}["built final"]
    assert "setchargetimefactor 1 engineer 0.75" in built and "sethqstatus 1 1" in built
    death = {e["event"]: e["actions"] for e in blocks["cp_axis"]["events"]}["death"]
    assert "setchargetimefactor 0 medic 1" in death and "sethqstatus 0 0" in death


def test_a_teams_command_post_builds_one_constructible(nrcroom):
    r = srv.objective_place(
        "command_post",
        "acp",
        "the Axis Command Post",
        "axis",
        objective={"min": [800, 400, 0], "max": [864, 464, 64]},
        trigger={"min": [768, 368, 0], "max": [896, 496, 128]},
    )
    assert [e["classname"] for e in r["entities"]] == [
        "trigger_objective_info",
        "func_constructible",
    ]
    assert _keys(r["entities"][0]["entity"])["spawnflags"] == "65"
    assert [b["block"] for b in srv.script_show()["blocks"]][-1] == "acp_axis"
    assert r["summary"]["error"] == 0


def test_new_kinds_refuse_before_changing_anything(nrcroom):
    before = (len(srv.query_entities()["entities"]), nrcroom.read_text())
    flag = {"flag": [0, 0, 0]}
    box = {"min": [0, 0, 0], "max": [8, 8, 8]}
    for kwargs, msg in [
        (
            {"kind": "checkpoint", "team": "neutral", "points": flag},
            "team must be one of axis, allies",
        ),
        ({"kind": "checkpoint"}, "checkpoint needs points.flag"),
        ({"kind": "checkpoint", "points": {**flag, "tank": [0, 0, 0]}}, "places no tank"),
        ({"kind": "checkpoint", "points": {"flag": [0, 0]}}, "a point is"),
        ({"kind": "checkpoint", "points": flag, "trigger": box}, "has no `trigger` brushes"),
        ({"kind": "command_post"}, "needs `trigger`"),
        ({"kind": "checkpoint", "name": "gate", "points": flag}, "already uses the name 'gate'"),
    ]:
        args = {"kind": "checkpoint", "name": "fwd", "track": "x", "team": "axis", **kwargs}
        with pytest.raises(ToolError, match=msg):
            srv.objective_place(**args)
    assert (len(srv.query_entities()["entities"]), nrcroom.read_text()) == before


def test_derived_names_must_be_free_too(nrcroom):
    srv.entity_place("info_notnull", [0, 0, 0], {"targetname": "fwd_wobj"})
    with pytest.raises(ToolError, match="already uses the name 'fwd_wobj'"):
        srv.objective_place("checkpoint", "fwd", "x", "axis", points={"flag": [0, 0, 0]})
