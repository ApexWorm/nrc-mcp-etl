"""Lifetime findings use explicit owner rules and retain uncertainty about script flow."""

from __future__ import annotations

from copy import deepcopy

import pytest
from nrc_mcp import mapscript, profiles


def entity(index=1, classname="func_explosive", name="relay"):
    return {
        "index": index,
        "classname": classname,
        "keys": [("classname", classname), ("scriptname", name)],
    }


def script(name="relay", event="death", actions=()):
    return f"{name}\n{{\n {event}\n {{\n" + "".join(f"  {a}\n" for a in actions) + " }\n}\n"


def findings(tmp_path, text, entities=None):
    if entities is None:
        entities = [entity()]
    return [
        f
        for f in mapscript.check("etl", entities, tmp_path / "m.map", text=text)
        if f.code.startswith("MAPSCRIPT_LIFETIME")
    ]


def test_long_wait_on_dying_entity_has_owner_line_event_and_source(tmp_path):
    rows = findings(tmp_path, script(actions=["wait 1500", "wm_endround"]))
    assert len(rows) == 1
    row = rows[0]
    assert row.code == "MAPSCRIPT_LIFETIME_WAIT"
    assert row.severity == "warning" and row.confidence == "verified"
    assert row.entities == [1]
    assert "m.script:5" in row.message and "relay.death" in row.message
    assert "wm_endround at line 6" in row.message and "100 ms" in row.message
    assert "g_mover.c" in row.rule_source and "q_math.h" in row.rule_source
    assert "persistent game_manager" in row.fix_hint


@pytest.mark.parametrize(
    "duration", ["0", "1", "50", "99", "random 50 1500", "random 1500 50", "-1", "unknown"]
)
def test_short_or_uncertain_wait_is_information_not_a_proven_failure(tmp_path, duration):
    (row,) = findings(tmp_path, script(actions=[f"wait {duration}", "wm_endround"]))
    assert row.code == "MAPSCRIPT_LIFETIME_RISK"
    assert row.severity == "info" and row.confidence == "unverified"


@pytest.mark.parametrize("duration", ["100", "1500", "random 100 1500"])
def test_literal_wait_range_with_a_long_minimum_warns(tmp_path, duration):
    (row,) = findings(tmp_path, script(actions=[f"wait {duration}", "wm_endround"]))
    assert row.code == "MAPSCRIPT_LIFETIME_WAIT"


def test_cumulative_waits_and_last_wait_without_continuation(tmp_path):
    rows = findings(tmp_path, script(actions=["wait 50", "wait 50", "wm_endround"]))
    assert [row.code for row in rows] == ["MAPSCRIPT_LIFETIME_RISK", "MAPSCRIPT_LIFETIME_WAIT"]
    assert not findings(tmp_path, script(actions=["wm_announce done", "wait 1500"]))


@pytest.mark.parametrize(
    "guard",
    [
        "accum 0 abort_if_equal 1",
        "abortifwarmup",
        "setstate relay invisible",
        "trigger game_manager touched",
        "unknown_action",
    ],
)
def test_conditional_or_unknown_prefix_does_not_claim_certain_reachability(tmp_path, guard):
    (row,) = findings(tmp_path, script(actions=[guard, "wait 1500", "wm_endround"]))
    assert row.severity == "info" and row.confidence == "unverified"


def test_conditional_wait_warns_only_as_a_potential_risk(tmp_path):
    (row,) = findings(tmp_path, script(actions=["accum 0 wait_while_equal 1", "wm_endround"]))
    assert row.severity == "info" and row.code == "MAPSCRIPT_LIFETIME_RISK"


@pytest.mark.parametrize("target", ["self", "RELAY"])
def test_immediate_same_owner_handoff_does_not_extend_owner_lifetime(tmp_path, target):
    text = """relay
{
 death
 {
  trigger TARGET later
 }
 trigger later
 {
  wait 1500
  wm_endround
 }
}
""".replace("TARGET", target)
    (row,) = findings(tmp_path, text)
    assert row.code == "MAPSCRIPT_LIFETIME_WAIT"
    assert "relay.death:5 -> relay.trigger later" in row.message


def test_same_owner_call_carries_already_elapsed_delay(tmp_path):
    text = """relay
{
 death
 {
  wait 50
  trigger self later
 }
 trigger later
 {
  wait 50
  wm_endround
 }
}
"""
    rows = findings(tmp_path, text)
    assert [row.code for row in rows] == ["MAPSCRIPT_LIFETIME_RISK", "MAPSCRIPT_LIFETIME_WAIT"]


def test_immediate_handoff_to_persistent_owner_is_safe(tmp_path):
    text = script(actions=["trigger game_manager end_round"]) + script(
        "game_manager", "trigger end_round", ["wait 1500", "wm_endround"]
    )
    assert not findings(tmp_path, text, [entity(), entity(2, "script_multiplayer", "")])


def test_only_the_expiring_owner_is_listed_when_entities_share_block(tmp_path):
    (row,) = findings(
        tmp_path,
        script(actions=["wait 1500", "wm_endround"]),
        [entity(), entity(2, "func_constructible")],
    )
    assert row.entities == [1]


def test_persistent_classes_other_events_and_dead_blocks_do_not_inherit_death_rule(tmp_path):
    assert not findings(
        tmp_path, script(actions=["wait 1500", "wm_endround"]), [entity(1, "func_constructible")]
    )
    assert not findings(tmp_path, script(event="spawn", actions=["wait 1500", "wm_endround"]))
    assert not findings(tmp_path, script(actions=["wait 1500", "wm_endround"]), [])


def test_remove_action_creates_lifetime_on_otherwise_persistent_owner(tmp_path):
    (row,) = findings(
        tmp_path,
        script(
            name="game_manager",
            event="trigger done",
            actions=["remove", "wait 1500", "wm_endround"],
        ),
        [entity(1, "script_multiplayer", "")],
    )
    assert row.entities == [1] and row.code == "MAPSCRIPT_LIFETIME_WAIT"


def test_remove_action_on_live_owner(tmp_path):
    (row,) = findings(
        tmp_path,
        script(event="trigger done", actions=["remove", "wait 1500", "wm_endround"]),
        [entity(1, "func_constructible")],
    )
    assert row.code == "MAPSCRIPT_LIFETIME_WAIT" and "3952" in row.rule_source


def test_first_duplicate_event_wins_and_recursive_calls_terminate_analysis(tmp_path):
    text = """relay
{
 death
 {
  wm_announce done
 }
 death
 {
  wait 1500
  wm_endround
 }
 trigger loop
 {
  trigger self loop
 }
}
"""
    assert not findings(tmp_path, text)
    text = text.replace("wm_announce done", "trigger self loop")
    assert not findings(tmp_path, text)


def test_rule_is_profile_driven_and_unverified_evidence_stays_information(tmp_path, monkeypatch):
    data = deepcopy(profiles.load("etl"))
    lifetime = data["mapscript"]["lifetime"]
    rule = lifetime["event_lifetimes"][0]
    rule["classname"], rule["events"], rule["confidence"] = (
        "custom_actor",
        ["activate"],
        "unverified",
    )
    monkeypatch.setattr(profiles, "load", lambda _pid: data)
    (row,) = findings(
        tmp_path,
        script(event="activate", actions=["wait 1500", "wm_endround"]),
        [entity(1, "custom_actor")],
    )
    assert row.confidence == "unverified" and row.severity == "info"


def test_profile_without_lifetime_model_is_unchanged(tmp_path, monkeypatch):
    data = deepcopy(profiles.load("etl"))
    data["mapscript"].pop("lifetime")
    monkeypatch.setattr(profiles, "load", lambda _pid: data)
    assert not findings(tmp_path, script(actions=["wait 1500", "wm_endround"]))


def test_branching_same_owner_call_graph_has_a_reported_work_limit(tmp_path):
    events = [" death\n {\n  trigger self e0\n }\n"]
    for index in range(20):
        actions = [f"trigger self e{index + 1}"] * 2 if index < 19 else ["wm_announce done"]
        events.append(f" trigger e{index}\n {{\n" + "".join(f"  {a}\n" for a in actions) + " }\n")
    rows = findings(tmp_path, "relay\n{\n" + "".join(events) + "}\n")
    (row,) = rows
    assert row.code == "MAPSCRIPT_LIFETIME_INCOMPLETE" and row.severity == "info"
