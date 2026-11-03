"""Mixed-stance witnesses must remain valid directed player-box paths."""

from __future__ import annotations

import json
import math

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import analysis, routes, server
from nrc_mcp.surface import gameplay
from test_analysis import box, point_entity, room, write_map


def grids(path, stances=("stand", "crouch", "prone"), cell=8):
    return {s: analysis.build_navgrid(path, cell=cell, profile_id="etl", stance=s) for s in stances}


def query(built, start=(64, 128, 0), end=(448, 128, 0), **kwargs):
    game_map = next(iter(built.values())).game_map
    return routes.find_route(
        built,
        routes.endpoint(game_map, "etl", start, None, "start"),
        routes.endpoint(game_map, "etl", end, None, "end"),
        **kwargs,
    )


@pytest.mark.parametrize("height,needed", [(56, "crouch"), (48, "prone")])
def test_mixed_route_changes_stance_only_where_both_boxes_fit(tmp_path, height, needed):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((192, 16, height), (320, 240, 256))])
    built = grids(path)
    result = query(built)
    assert result["reachable"] is True
    assert [s["stance"] for s in result["segments"]] == ["stand", needed, "stand"]
    assert result["distance_by_stance"][needed] > 100
    assert result["path"][0]["stance"] == result["path"][-1]["stance"] == "stand"
    assert len(result["transitions"]) == 2
    for transition in result["transitions"]:
        for stance in (transition["from_stance"], transition["to_stance"]):
            grid = built[stance]
            assert grid.is_walkable(grid.cell_at(transition["position"]))
    weights = result["cost_model"]["stance_costs"]
    expected = sum(s["distance"] * weights[s["stance"]] for s in result["segments"]) + 64
    assert result["cost"] == pytest.approx(expected, abs=1e-5)
    assert result["collinear_vertices_removed"] > 0


def test_exact_fit_prone_tunnel_does_not_become_reachable_by_switching_stance(tmp_path):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((192, 16, 40), (320, 240, 256))])
    result = query(grids(path))
    assert result["reachable"] is False
    assert result["status"] == "unreachable"
    assert result["path"] == result["segments"] == []


def test_single_stance_and_explicit_endpoint_stances(tmp_path):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((192, 16, 56), (320, 240, 256))])
    assert query(grids(path, ("stand",)))["reachable"] is False
    fixed = query(grids(path, ("crouch",)))
    assert fixed["reachable"] is True and fixed["transitions"] == []
    built = grids(path)
    # The destination is in the low tunnel; requesting stand must fail instead of standing
    # through the ceiling. With a small snap radius it cannot silently snap out of the tunnel.
    blocked = query(built, end=(256, 128, 0), snap_radius=16)
    assert blocked["status"] == "no_endpoint"
    crouch = query(built, end=(256, 128, 0), end_stance="crouch", snap_radius=16)
    assert crouch["reachable"] and crouch["path"][-1]["stance"] == "crouch"


def test_standing_is_preferred_in_open_space_and_weights_are_explicit(tmp_path):
    built = grids(write_map(tmp_path, room(0, 0, 512, 256)))
    result = query(built)
    assert [s["stance"] for s in result["segments"]] == ["stand"]
    assert result["cost"] == result["distance"]
    selected = query(built, stance_costs={"prone": 0.1}, transition_cost=0)
    assert selected["distance_by_stance"]["prone"] > 300
    assert selected["cost_model"]["units"] == "weighted_world_units"
    assert "seconds" in " ".join(selected["notes"])


def test_directed_drop_does_not_allow_climbing_back_in_another_stance(tmp_path):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((16, 16, 0), (240, 240, 64))])
    built = grids(path)
    high, low = (64, 128, 64), (448, 128, 0)
    assert query(built, high, low)["reachable"] is True
    assert query(built, low, high)["reachable"] is False


def test_height_changing_witness_follows_clear_sweep_legs_not_through_a_riser(tmp_path):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((16, 16, 0), (240, 240, 16))])
    built = grids(path)
    result = query(built, (64, 128, 16), (448, 128, 0))
    assert result["reachable"]
    for segment in result["segments"]:
        grid = built[segment["stance"]]
        points = segment["points"]
        for start, end in zip(points, points[1:], strict=False):
            assert analysis.segment_clear(
                grid.clearance_solids, grid.clearance_index, tuple(start), tuple(end)
            )
    lengths = [
        math.dist(a["position"], b["position"])
        for a, b in zip(result["path"], result["path"][1:], strict=False)
    ]
    assert result["distance"] == pytest.approx(sum(lengths), abs=1e-5)


def test_node_budget_is_unknown_not_unreachable(tmp_path):
    built = grids(write_map(tmp_path, room(0, 0, 512, 256)))
    result = query(built, max_nodes=1)
    assert result["reachable"] is None
    assert result["status"] == "budget_exhausted" and result["nodes_expanded"] == 1
    assert not result["path"]


def test_zero_length_route_stays_in_one_stance(tmp_path):
    built = grids(write_map(tmp_path, room(0, 0, 128, 128)))
    result = query(built, start=(64, 64, 0), end=(64, 64, 0))
    assert result["reachable"] and result["distance"] == result["cost"] == 0
    assert len(result["path"]) == 1 and result["transitions"] == []


def test_mixed_uses_only_profile_described_stances():
    assert routes.stances("urt43", "mixed") == ("stand", "crouch")


def test_grids_from_different_sources_are_refused_even_with_equal_bounds(tmp_path):
    first = write_map(tmp_path, room(0, 0, 128, 128))
    built = grids(first)
    second = write_map(
        tmp_path, [*room(0, 0, 128, 128), box((56, 16, 0), (72, 112, 256))], name="second.map"
    )
    built["prone"] = analysis.build_navgrid(second, cell=8, profile_id="etl", stance="prone")
    with pytest.raises(ValueError, match="same aligned snapshot"):
        query(built)


def test_endpoint_snap_cannot_leave_a_point_embedded_in_a_wall(tmp_path):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((128, 16, 0), (144, 240, 256))])
    result = query(grids(path), start=(140, 128, 24))
    assert result["status"] == "no_endpoint"


def test_endpoint_snap_cannot_stand_up_by_leaving_a_low_ceiling(tmp_path):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((192, 16, 56), (320, 240, 256))])
    built = grids(path)
    result = query(built, end=(200, 128, 0))
    assert result["status"] == "no_endpoint"
    crouch = query(built, end=(200, 128, 0), end_stance="crouch")
    assert crouch["reachable"]


def test_endpoint_snap_cannot_jump_a_pit_from_an_unsampled_small_platform(tmp_path):
    brushes = [
        *room(0, 0, 512, 256, floor_top=-128, height=384),
        box((126, 112, -128), (130, 144, 0)),
        box((192, 16, -128), (496, 240, 0)),
    ]
    built = grids(write_map(tmp_path, brushes))
    result = query(built, start=(128, 128, 0), snap_radius=128)
    assert result["status"] == "no_endpoint"


def test_objective_entity_alias_uses_the_same_approach_area(tmp_path):
    entities = [
        point_entity("team_CTF_bluespawn", (64, 128, 24), spawnflags=3),
        '{\n"classname" "trigger_objective_info"\n"target" "bridge"\n"spawnflags" "2"\n'
        + box((384, 48, 0), (480, 208, 128), "common/trigger")
        + "}\n",
        '{\n"classname" "func_constructible"\n"targetname" "bridge"\n'
        + box((416, 96, 0), (448, 160, 64))
        + "}\n",
    ]
    built = grids(write_map(tmp_path, room(0, 0, 512, 256), entities))
    game_map = built["stand"].game_map
    start = routes.endpoint(game_map, "etl", None, 1, "start")
    a = routes.find_route(built, start, routes.endpoint(game_map, "etl", None, 2, "end"))
    b = routes.find_route(built, start, routes.endpoint(game_map, "etl", None, 3, "end"))
    assert a["reachable"] and b["reachable"]
    assert a["path"] == b["path"]
    assert a["end"]["represented_entities"] == b["end"]["represented_entities"] == [2, 3]
    assert a["end"]["bounds"] is not None
    assert a["start"]["distance"] >= 24


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_nodes": 0},
        {"max_nodes": True},
        {"max_nodes": 250001},
        {"transition_cost": -1},
        {"transition_cost": math.nan},
        {"stance_costs": {"prone": 0}},
        {"stance_costs": {"fly": 1}},
        {"snap_radius": math.inf},
        {"snap_radius": 1000},
        {"start_stance": "fly"},
        {"end_stance": "fly"},
    ],
)
def test_invalid_search_options_are_rejected(tmp_path, kwargs):
    built = grids(write_map(tmp_path, room(0, 0, 128, 128)))
    with pytest.raises(ValueError):
        query(built, **kwargs)


def test_snapshot_revision_and_render_remain_current_without_saving(tmp_path):
    path = write_map(tmp_path, room(0, 0, 512, 256))
    disk = path.read_bytes()
    server.map_open(str(path))
    kwargs = {"start": [64, 128, 0], "end": [448, 128, 0], "profile_id": "etl", "cell": 8}
    before = gameplay.route_report(**kwargs)
    image, text = gameplay.route_render(**kwargs, width=640, height=480)
    rendered = json.loads(text)
    assert rendered["path"] == before["path"]
    assert rendered["revision"]["sha256"] == before["revision"]["sha256"]
    assert image.data.startswith(b"\x89PNG")
    server.solid_commit(
        {"op": "box", "min": [240, 16, 0], "max": [272, 240, 256]}, label="block_route"
    )
    after = gameplay.route_report(**kwargs)
    assert before["reachable"] and after["reachable"] is False
    assert after["revision"]["dirty"]
    assert before["revision"]["sha256"] != after["revision"]["sha256"]
    assert path.read_bytes() == disk


def test_tool_rejects_ambiguous_endpoints_and_bad_render_size(tmp_path):
    server.map_open(str(write_map(tmp_path, room(0, 0, 128, 128))))
    with pytest.raises(ToolError, match="exactly one"):
        gameplay.route_report(start=[64, 64, 0], start_entity=1, end=[80, 80, 0], profile_id="etl")
    with pytest.raises(ToolError, match="width"):
        gameplay.route_render(start=[64, 64, 0], end=[80, 80, 0], width=10, profile_id="etl")
