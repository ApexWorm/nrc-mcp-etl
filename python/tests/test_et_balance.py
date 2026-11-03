"""Reinforcement estimates retain their inputs and never claim script execution."""

from __future__ import annotations

import pytest
from nrc_mcp import analysis, balance
from test_analysis import box, point_entity, room, write_map


@pytest.fixture
def grid(tmp_path):
    path = write_map(
        tmp_path,
        room(0, 0, 768, 384),
        [
            point_entity("team_CTF_redspawn", (96, 96, 24), spawnflags=2),
            point_entity("team_CTF_redspawn", (112, 96, 24), spawnflags=2),
            point_entity("team_CTF_redspawn", (576, 96, 24), spawnflags=0),
            point_entity("team_CTF_bluespawn", (672, 288, 24), spawnflags=2),
            point_entity("team_CTF_blueflag", (384, 192, 24)),
            point_entity("script_multiplayer", (128, 128, 24), scriptname="controller"),
        ],
    )
    return analysis.build_navgrid(path, profile_id="etl", cell=16)


def script(red=20, blue=30, extra=""):
    return f"""controller
{{
    spawn
    {{
        wm_axis_respawntime {red}
        wm_allied_respawntime {blue}
    }}
    {extra}
}}
"""


def enriched(grid, **kwargs):
    report = analysis.balance_report(grid.map_path, grid=grid)
    return balance.enrich(report, grid, **kwargs)["reinforcement_balance"]


def test_initial_spawns_timing_and_static_overlap(grid):
    result = enriched(grid, script_text=script(), team_sizes={"red": 4, "blue": 2})
    initial = result["stages"][0]
    red = next(g for g in initial["spawn_groups"] if g["team"] == "red")
    assert red["spawn_entities"] == [1, 2]
    assert red["available_slots"] == 2
    assert red["slot_shortfall"] == 2
    assert red["overlapping_pairs"] == [[1, 2]]
    assert red["non_overlapping_slots_lower_bound"] == 1
    assert initial["respawn"]["red"]["seconds"] == 20
    route = next(r for r in initial["routes"] if r["team"] == "red")
    assert route["representative_spawn_entity"] == 1
    assert route["reachable"]
    assert route["travel_seconds_estimate"] == round(route["distance"] / 320, 3)
    assert route["arrival_seconds_estimate"] == [
        round(route["distance"] / 320, 3),
        round(route["distance"] / 320 + 20, 3),
    ]
    assert route["confidence"] == "unverified"
    assert result["team_labels"] == {"red": "Axis", "blue": "Allies"}


def test_changed_script_interval_is_ambiguous_until_explicitly_overridden(grid):
    text = script(extra="trigger advance\n{\nwm_axis_respawntime 10\n}")
    result = enriched(grid, script_text=text)
    interval = result["stages"][0]["respawn"]["red"]
    assert interval["seconds"] is None
    assert {c["seconds"] for c in interval["candidates"]} == {10, 20}
    red = next(r for r in result["stages"][0]["routes"] if r["team"] == "red")
    assert red["travel_seconds_estimate"] is not None
    assert red["arrival_seconds_estimate"] is None
    overridden = enriched(grid, script_text=text, respawn_seconds={"red": 12})
    assert overridden["stages"][0]["respawn"]["red"]["seconds"] == 12


def test_stages_activate_specific_spawns_and_keep_collision_assumption_explicit(grid):
    result = enriched(
        grid,
        script_text=script(),
        stages=[
            {
                "name": "forward spawn",
                "spawn_entities": [3, 4],
                "objective_entities": [5],
                "respawn_seconds": {"red": 15},
            }
        ],
    )
    stage = result["stages"][0]
    assert {e for g in stage["spawn_groups"] for e in g["spawn_entities"]} == {3, 4}
    assert {r["objective_entity"] for r in stage["routes"]} == {5}
    assert stage["respawn"]["red"]["seconds"] == 15
    assert any("same geometry snapshot" in n for n in result["notes"])
    assert stage["paths_measured"] == 2


def test_path_budget_reports_omitted_stages(grid):
    result = enriched(
        grid,
        stages=[
            {"name": "first", "spawn_entities": [1, 4]},
            {"name": "later", "spawn_entities": [3, 4]},
        ],
        max_paths=1,
    )
    assert sum(s["paths_measured"] for s in result["stages"]) == 1
    assert all(s["truncated"] for s in result["stages"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"team_sizes": {"Axis": 8}},
        {"team_sizes": {"red": 2.5}},
        {"run_speed": 0},
        {"run_speed": float("nan")},
        {"run_speed": True},
        {"respawn_seconds": {"red": -2}},
        {"stages": []},
        {"stages": [{"name": "bad", "spawn_entities": [5]}]},
        {"stages": [{"name": "bad", "spawn_entities": [1], "objective_entities": [1]}]},
        {"stages": [{"name": "bad", "spawn_entities": [True]}]},
        {"stages": [{"name": "bad", "spawn_entities": [1], "open_doors": True}]},
    ],
)
def test_invalid_analysis_inputs_refuse_instead_of_being_ignored(grid, kwargs):
    with pytest.raises(ValueError):
        enriched(grid, **kwargs)


def test_invalid_script_does_not_fall_back_to_plausible_default_times(grid):
    result = enriched(grid, script_text="controller { spawn {")
    assert result["stages"][0]["respawn"]["red"]["seconds"] is None
    assert any("could not be parsed" in n for n in result["notes"])


def test_unreferenced_script_block_does_not_change_wave_time(grid):
    result = enriched(grid, script_text=script().replace("controller", "unused"))
    wave = result["stages"][0]["respawn"]["red"]
    assert wave["seconds"] == 30
    assert wave["candidates"] == []
    assert "profile default" in wave["source"]


def test_slow_stance_needs_speed_override(grid):
    grid.stance = "crouch"
    result = enriched(grid)
    assert result["run_speed"]["units_per_second"] is None
    assert all(r["travel_seconds_estimate"] is None for r in result["stages"][0]["routes"])
    supplied = enriched(grid, run_speed=160)
    assert supplied["run_speed"]["units_per_second"] == 160


def test_non_reinforcement_profile_unchanged_without_options(tmp_path):
    path = write_map(tmp_path, room(0, 0, 256, 256))
    other = analysis.build_navgrid(path, profile_id="urt43")
    original = {"profile": "urt43"}
    assert balance.enrich(original, other) is original
    with pytest.raises(ValueError, match="does not describe"):
        balance.enrich(original, other, team_sizes={"red": 8})


def test_symmetry_is_not_a_fairness_rule_for_objective_progression(grid):
    original = {
        "findings": [
            {"code": "BALANCE_NOT_MIRROR_SYMMETRIC", "severity": "info"},
            {"code": "BALANCE_OBJECTIVE_ASYMMETRIC", "severity": "info"},
            {"code": "KEEP_THIS", "severity": "warning"},
        ],
        "notes": ["no traversal time is estimated: old claim"],
    }
    result = balance.enrich(original, grid)
    assert not any("ASYMMETRIC" in f["code"] or "MIRROR" in f["code"] for f in result["findings"])
    assert any(f["code"] == "KEEP_THIS" for f in result["findings"])
    assert not any(n.startswith("no traversal time") for n in result["notes"])
    assert len(original["findings"]) == 3


def brush_entity(classname, mins, maxs, **keys):
    fields = {"classname": classname, **keys}
    return "{\n" + "".join(f'"{k}" "{v}"\n' for k, v in fields.items()) + box(mins, maxs) + "}\n"


def linked_objective_grid(tmp_path, extra=()):
    path = write_map(
        tmp_path,
        room(0, 0, 768, 384),
        [
            point_entity("team_CTF_redspawn", (96, 96, 24), spawnflags=2),
            point_entity("team_CTF_bluespawn", (672, 288, 24), spawnflags=2),
            brush_entity(
                "trigger_objective_info",
                (320, 128, 0),
                (448, 256, 128),
                target="Bridge",
                spawnflags=2,
                track="Bridge",
            ),
            brush_entity(
                "func_constructible",
                (384, 160, 0),
                (416, 224, 64),
                targetname="bridge",
                spawnflags=8,
            ),
            *extra,
        ],
    )
    return analysis.build_navgrid(path, profile_id="etl", cell=16)


def test_linked_constructible_is_one_owned_objective_at_the_trigger_approach_area(tmp_path):
    grid = linked_objective_grid(tmp_path)
    report = analysis.balance_report(grid.map_path, grid=grid)
    assert len(report["objectives"]) == 1
    objective = report["objectives"][0]
    assert objective["entity"] == 3
    assert objective["linked_entities"] == [4]
    assert objective["team"] == "blue"
    assert objective["origin"] == [384, 192, 64]
    _, markers, _ = analysis._markers(grid.game_map, analysis.roles("etl"))
    assert markers[0].bounds == ([320, 128, 0], [448, 256, 128])
    assert len(report["distances"]) == 2
    initial = enriched(grid)["stages"][0]
    assert initial["paths_requested"] == initial["paths_measured"] == 2
    for route in report["distances"] + initial["routes"]:
        assert route["objective_entity"] == 3
        assert route["objective_entities"] == [3, 4]
        assert route["objective_team"] == "blue"
        assert route["reachable"]


@pytest.mark.parametrize("selected", [[3], [4], [3, 4]])
def test_stages_accept_both_linked_objective_indices_without_duplicate_routes(tmp_path, selected):
    result = enriched(
        linked_objective_grid(tmp_path),
        stages=[{"name": "bridge", "spawn_entities": [1, 2], "objective_entities": selected}],
    )
    stage = result["stages"][0]
    assert stage["paths_requested"] == stage["paths_measured"] == 2
    assert {r["objective_entity"] for r in stage["routes"]} == {3}
    assert all(r["objective_entities"] == [3, 4] for r in stage["routes"])


def test_independent_objectives_and_links_to_other_classes_are_not_grouped(tmp_path):
    grid = linked_objective_grid(
        tmp_path,
        [
            brush_entity(
                "func_constructible",
                (512, 160, 0),
                (544, 224, 64),
                targetname="another_bridge",
                track="Bridge",
            ),
            point_entity("trigger_objective_info", (256, 256, 24), target="flag", track="Bridge"),
            point_entity("team_CTF_blueflag", (256, 320, 24), targetname="flag", track="Bridge"),
        ],
    )
    report = analysis.balance_report(grid.map_path, grid=grid)
    assert [o["entity"] for o in report["objectives"]] == [3, 5, 6, 7]
    assert enriched(grid)["stages"][0]["paths_requested"] == 8


@pytest.mark.parametrize(
    "extra",
    [
        point_entity("func_constructible", (512, 192, 24), targetname="bridge"),
        point_entity("info_null", (512, 192, 24), targetname="bridge"),
        point_entity("trigger_objective_info", (512, 192, 24), target="bridge"),
        '{\n"classname" "trigger_objective_info"\n"target" "bridge"\n}\n',
    ],
    ids=["multiple_targets", "non_objective_name_collision", "shared_target", "unplaced_source"],
)
def test_ambiguous_objective_links_leave_every_positioned_marker_visible(tmp_path, extra):
    grid = linked_objective_grid(tmp_path, [extra])
    report = analysis.balance_report(grid.map_path, grid=grid)
    indices = {o["entity"] for o in report["objectives"]}
    assert {3, 4} <= indices
    assert not any(o.get("linked_entities") for o in report["objectives"])
    assert enriched(grid)["stages"][0]["paths_requested"] == 2 * len(indices)
