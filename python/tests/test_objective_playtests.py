"""objective_playtests: objectives recognised from a map's entities and script, and the engine
scenario generated for each from its kind's profile template."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from nrc_mcp import kernel, playtest, profiles
from nrc_mcp import objective_tests as ot
from nrc_mcp import server as srv

FIXTURE = Path(__file__).resolve().parents[2] / "bench" / "tapes" / "fixtures" / "nrcroom"


@pytest.fixture
def etl_profile():
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")


def ent(index: int, classname: str, origin=None, **keys) -> dict:
    return {
        "index": index,
        "classname": classname,
        "origin": origin,
        "keys": [("classname", classname), *keys.items()],
        "brushes": 0,
        "patches": 0,
    }


class FakeMap:
    def __init__(self, entities):
        self._entities = entities

    def entities(self):
        return self._entities


# ---------------------------------------------------------------------------
# Recognition: from entities alone, whoever made them
# ---------------------------------------------------------------------------


def test_kinds_are_recognised_from_entities_by_their_links_and_flags(etl_profile):
    entities = [
        ent(0, "worldspawn"),
        # A dynamite objective owned by the Allies, made by hand: no scriptname on it.
        ent(1, "trigger_objective_info", target="Safe", spawnflags="2"),
        ent(2, "func_explosive", targetname="safe"),
        # A neutral command post: one trigger, both teams' constructibles.
        ent(3, "trigger_objective_info", target="cp", spawnflags="64"),
        ent(4, "func_constructible", targetname="cp", scriptname="cp_axis", spawnflags="4"),
        ent(5, "func_constructible", targetname="cp", scriptname="cp_allies", spawnflags="8"),
        # A one-way flag held by the Axis (SPAWNPOINT | ALLIED_ONLY) and a two-way one.
        ent(6, "team_WOLF_checkpoint", [0, 0, 0], targetname="f1", spawnflags="9"),
        ent(7, "team_WOLF_objective", [0, 0, 0], targetname="f1_wobj", spawnflags="1"),
        ent(8, "team_WOLF_checkpoint", [0, 0, 0], targetname="f2", spawnflags="1"),
        ent(9, "team_WOLF_objective", [0, 0, 0], targetname="f2_wobj", spawnflags="2"),
        ent(10, "team_CTF_bluespawn", [0, 0, 24], targetname="f2_spawns", spawnflags="3"),
        # Loose ends: an explosive nothing targets, a constructible with no team flag.
        ent(11, "func_explosive", targetname="gate"),
        ent(12, "trigger_objective_info", target="hut", spawnflags="1"),
        ent(13, "func_constructible", targetname="hut"),
        # Stolen Axis documents and where they are delivered.
        ent(14, "team_CTF_redflag", [0, 0, 24], scriptname="docs"),
        ent(15, "trigger_flagonly_multiple", scriptname="docs_deliver", spawnflags="1"),
    ]
    found, left = ot.recognise("etl", FakeMap(entities))
    got = {f.name: (f.kind, f.team) for f in found}
    assert got == {
        "safe": ("dynamite", "allies"),
        "cp": ("command_post", "neutral"),
        "f1": ("checkpoint_one_way", "axis"),
        "f2": ("checkpoint", "allies"),
        "docs": ("steal", "axis"),
    }
    by_name = {f.name: f for f in found}
    assert [e["index"] for e in by_name["cp"].parts["objective"]] == [5], "the builder's own"
    assert by_name["cp"].claimed >= {3, 4, 5}, "both constructibles belong to the post"
    assert [e["index"] for e in by_name["f2"].parts["spawns"]] == [10]
    reasons = {row["entity"]: row["reason"] for row in left}
    assert set(reasons) == {11, 12, 13}
    assert "no trigger_objective_info has target 'gate'" in reasons[11]
    assert "does not carry a team's builder_flag" in reasons[13]


def test_placeholders_read_parts_and_say_what_is_missing():
    parts = {"anchor": [ent(1, "x", scriptname="Boom")], "trigger": [ent(2, "y")]}
    assert ot.fill(["{name}:{anchor.scriptname}"], {"name": "n"}, parts) == ["n:Boom"]
    with pytest.raises(ot.Untestable, match="its trigger has no scriptname key"):
        ot.fill("{trigger.scriptname}", {}, parts)


# ---------------------------------------------------------------------------
# Script: does the objective end the round, and for whom
# ---------------------------------------------------------------------------


SCRIPT = """
game_manager
{
    trigger won
    {
        wm_setwinner 1
        wait 500
        wm_endround
    }
    trigger loop
    {
        trigger self loop
    }
}
relay
{
    death
    {
        trigger game_manager won
        trigger game_manager loop
    }
}
bridge
{
    built final
    {
        wm_announce "built"
    }
}
"""


def test_round_end_is_followed_through_calls(etl_profile):
    data = profiles.load("etl")
    script = ot.Script(SCRIPT, data["mapscript"], data["objectives"]["playtest"])
    out = script.outcome([{"block": "relay", "event": "death"}])
    assert out["ends_round"] and out["winners"] == ["blue"]
    assert out["events"][:2] == ["relay death", "game_manager trigger won"]
    quiet = script.outcome([{"block": "Bridge", "event": "built", "params": ["FINAL"]}])
    assert not quiet["ends_round"] and quiet["winners"] == []


# ---------------------------------------------------------------------------
# The driver's aim at an observed point
# ---------------------------------------------------------------------------


def test_aim_at_an_observed_vector_field(etl_profile):
    adapter = copy.deepcopy(playtest.spec("etl"))
    at = {"scope": "game", "field": "charge_position", "offset": [0, 0, 4]}
    playtest.validate({"steps": [{"action": "aim", "at": at}]}, adapter)
    for bad in (
        {"action": "aim"},
        {"action": "aim", "at": at, "angles": [0, 0, 0]},
        {"action": "aim", "at": {"scope": "game", "field": "phase"}},
        {"action": "aim", "at": {"scope": "entity", "name": "x", "field": "position"}},
    ):
        with pytest.raises(playtest.PlaytestError):
            playtest.validate({"steps": [bad]}, adapter)
    snapshot = {
        "player": {"position": [0, 0, 24], "view_height": 40},
        "game": {"charge_position": [20, 20, 0]},
    }
    pitch, yaw, roll = playtest._look(snapshot, at, adapter)
    assert yaw == 45 and roll == 0
    assert pitch == pytest.approx(64.76, abs=0.01), "looks down at the charge"


# ---------------------------------------------------------------------------
# Packing tests into engine runs
# ---------------------------------------------------------------------------


def test_a_round_ending_test_closes_its_run(etl_profile):
    adapter = playtest.spec("etl")

    def test(name, ends):
        steps = [{"action": "join", "team": "blue", "role": "engineer", "timeout": 40}]
        return {"objective": name, "ends_round": ends, "watch": [name], "steps": steps}

    runs = ot.pack([test("a", True), test("b", False), test("c", True), test("d", False)], adapter)
    assert [r["objectives"] for r in runs] == [["b", "d", "a"], ["c"]]
    assert runs[0]["scenario"]["watch"] == ["b", "d", "a"]
    assert runs[0]["scenario"]["steps"][-1] == {"action": "screenshot", "name": "a_done"}
    assert all(r["timeout_seconds"] <= 600 for r in runs)


# ---------------------------------------------------------------------------
# End to end on a real map: positions on reachable floor, scenario valid for engine_playtest
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
    yield tmp_path / "maps" / "nrcroom.map"
    srv.SESSION.map = None
    srv.SESSION.path = None


def test_dynamite_test_stands_in_the_trigger_and_waits_for_the_win(nrcroom):
    obj = {"min": [800, 300, 0], "max": [848, 348, 64]}
    trig = {"min": [752, 256, 0], "max": [896, 400, 128]}
    srv.objective_place("dynamite", "relay", "the relay", "axis", objective=obj, trigger=trig)
    srv.script_set_event("relay", "death", actions=["trigger game_manager won"])
    srv.script_set_event(
        "game_manager", "trigger", params=["won"], actions=["wm_setwinner 1", "wm_endround"]
    )
    srv.map_save()
    # A fresh session: what it knows comes from the file, not from objective_place.
    srv.SESSION.map = None
    srv.map_open(str(nrcroom))
    r = srv.objective_playtests()
    (test,) = r["tests"]
    assert (test["objective"], test["kind"], test["team"]) == ("relay", "dynamite", "axis")
    steps = test["steps"]
    assert steps[0] == {"action": "join", "team": "blue", "role": "engineer", "timeout": 40}
    stand = steps[1]
    x, y, z = stand["origin"]
    half = 18 + ot.CLEAR_MARGIN
    assert 752 <= x <= 896 and 256 <= y <= 400, "inside the trigger"
    assert not (800 - half < x < 848 + half and 300 - half < y < 348 + half), "clear of it"
    assert z == 24, "standing on the floor at z 0"
    assert test["standing"][0]["route"]["distance"] > 0, "reached from an Allied spawn"
    assert {"action": "weapon", "name": "dynamite"} in steps
    assert any(s["action"] == "aim" and "at" in s for s in steps)
    assert steps[-1]["until"] == [
        {"scope": "game", "field": "phase", "value": "intermission"},
        {"scope": "game", "field": "winner", "value": "blue"},
    ]
    assert test["ends_round"]
    (run,) = r["runs"]
    playtest.validate(run["scenario"], playtest.spec("etl"))
    # The fixture's own explosive has no objective trigger, and is reported, not tested.
    (gate,) = [s for s in r["skipped"] if s.get("name") == "gate"]
    assert "trigger_objective_info" in gate["reason"]


def test_one_objective_by_name_and_unknown_names(nrcroom):
    srv.objective_place(
        "constructible",
        "hut",
        "the hut",
        "allies",
        objective={"min": [800, 300, 0], "max": [848, 348, 64]},
        trigger={"min": [752, 256, 0], "max": [896, 400, 128]},
    )
    r = srv.objective_playtests(objective="HUT")
    assert [t["objective"] for t in r["tests"]] == ["hut"] and not r["skipped"]
    hold = [s for s in r["tests"][0]["steps"] if s["action"] == "hold"][-1]
    assert hold["until"] == [{"scope": "entity", "name": "hut", "field": "built", "value": True}]
    with pytest.raises(Exception, match="no objective named 'shed'"):
        srv.objective_playtests(objective="shed")


def test_an_objective_out_of_reach_is_skipped_with_the_reason(nrcroom):
    # The trigger's floor is the top of a pillar no one can climb.
    srv.solid_commit({"op": "box", "min": [800, 300, 0], "max": [900, 400, 160]}, label="pillar")
    srv.objective_place(
        "constructible",
        "perch",
        "the perch",
        "allies",
        objective={"min": [830, 330, 160], "max": [870, 370, 176]},
        trigger={"min": [800, 300, 160], "max": [900, 400, 240]},
    )
    r = srv.objective_playtests()
    (skip,) = [s for s in r["skipped"] if s.get("objective") == "perch"]
    assert "reachable" in skip["reason"] or "no floor" in skip["reason"]
    assert not r["tests"]
