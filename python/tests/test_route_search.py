"""The native route search gives the Python search's answers, only faster.

`analysis.path_distance` and `routes.find_route` run in the kernel (`routesearch`); the
Python loops they replaced stay as `engine="python"`, the reference these tests hold the
native search to: the same reachability, cost, path and node count, not merely a route of
equal cost.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest
from nrc_mcp import analysis, routes, routesearch, server
from nrc_mcp.surface import gameplay
from test_analysis import box, room, write_map

pytestmark = pytest.mark.skipif(
    not routesearch.available(), reason="kernel without RouteGraph; run mise run kernel:build"
)

REAL = Path(__file__).resolve().parents[2] / "corpus" / "real"


def course(tmp_path) -> Path:
    """Walls with a gap, stairs, a ledge (walk off, not up), a low tunnel, a sealed cell."""
    return write_map(
        tmp_path,
        [
            *room(0, 0, 1024, 768),
            # A wall across the room with one gap at the bottom: routes bend.
            box((400, 208, 0), (432, 752, 256)),
            # A 64-unit ledge, too high to step up, low enough to drop from.
            box((560, 400, 0), (752, 560, 64)),
            # Stairs up to a landing, 16 units a step.
            *[box((800 + 32 * i, 300, 0), (1008, 460, 16 * (i + 1))) for i in range(4)],
            # A low tunnel: crouch, not stand.
            box((100, 450, 56), (380, 752, 256)),
            # A sealed box inside the room: its floor is a component of its own.
            *room(600, 600, 760, 740, floor_top=8, height=96),
        ],
    )


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    path = course(tmp_path_factory.mktemp("course"))
    return {
        s: analysis.build_navgrid(path, cell=16, profile_id="etl", stance=s)
        for s in ("stand", "crouch", "prone")
    }


def pairs(grid, count, seed=0, lo=0.0, hi=math.inf):
    rng = random.Random(seed)
    points = [grid.centre(c) for c in grid.walkable_cells()]
    out = []
    while len(out) < count:
        a, b = rng.choice(points), rng.choice(points)
        if lo <= math.dist(a, b) <= hi:
            out.append((a, b))
    return out


def test_path_distance_is_the_python_search_exactly(built):
    grid = built["stand"]
    sample = pairs(grid, 150, seed=1)
    # Off-grid endpoints too: they snap, and the snap is part of the answer.
    sample += [((a[0] + 5, a[1] - 3, a[2] + 20), (b[0] - 7, b[1] + 2, b[2])) for a, b in sample[:30]]
    high, low = (650.0, 480.0, 64.0), (650.0, 250.0, 0.0)
    sample += [(high, low), (low, high), ((900, 380, 64), (900, 600, 0)), ((40, 40, 0), (980, 700, 0))]
    native = [analysis.path_distance(grid, a, b, engine="native") for a, b in sample]
    python = [analysis.path_distance(grid, a, b, engine="python") for a, b in sample]
    assert native == python
    outcomes = {(r["reachable"], "different connected" in r.get("reason", "")) for r in python}
    # The course exercises found routes, one-way drops and separate components.
    assert outcomes >= {(True, False), (False, False), (False, True)}


def test_one_way_ledge_and_budget_match(built):
    grid = built["stand"]
    high, low = (650.0, 480.0, 64.0), (650.0, 250.0, 0.0)
    for engine in ("native", "python"):
        assert analysis.path_distance(grid, high, low, engine=engine)["reachable"] is True
        up = analysis.path_distance(grid, low, high, engine=engine)
        assert up["reachable"] is False and "one-way" in up["reason"]
    tight = [
        analysis.path_distance(grid, (40, 40, 0), (980, 700, 0), max_nodes=50, engine=e)
        for e in ("native", "python")
    ]
    assert tight[0] == tight[1] and "max_nodes" in tight[0]["reason"]


def test_without_components_the_answers_stay_the_same(built):
    grid = built["stand"]
    sample = pairs(grid, 120, seed=2)
    labelled = [analysis.path_distance(grid, a, b, engine="python") for a, b in sample]
    unlabelled = analysis.path_distances(grid, sample, use_components=False, with_paths=True)
    for a, b in zip(labelled, unlabelled, strict=True):
        assert (a["reachable"], a.get("distance")) == (b["reachable"], b.get("distance"))
        if a["reachable"]:
            assert a == b


def test_batch_rows_are_the_single_queries(built):
    grid = built["stand"]
    sample = pairs(grid, 60, seed=3, lo=50, hi=400)
    rows = analysis.path_distances(grid, sample, use_components=True)
    for (a, b), row in zip(sample, rows, strict=True):
        single = analysis.path_distance(grid, a, b)
        single.pop("path")
        single.pop("path_decimated", None)
        assert row == single


def test_edges_are_learned_once_and_kept_on_the_grid(built, monkeypatch):
    grid = built["stand"]
    sample = pairs(grid, 40, seed=4, lo=50, hi=300)
    analysis.path_distances(grid, sample, use_components=False)
    learned = routesearch.graph_sizes(grid)["step"]["expanded"]
    calls = []
    original = type(grid).neighbours

    def counting(self, cell, **kw):
        calls.append(cell)
        return original(self, cell, **kw)

    monkeypatch.setattr(type(grid), "neighbours", counting)
    again = analysis.path_distances(grid, sample, use_components=False)
    assert calls == [] and routesearch.graph_sizes(grid)["step"]["expanded"] == learned
    assert again == analysis.path_distances(grid, sample, use_components=False)


def test_parallel_learning_builds_the_lazy_graph(tmp_path, monkeypatch):
    path = course(tmp_path)
    lazy = analysis.build_navgrid(path, cell=16, profile_id="etl")
    eager = analysis.build_navgrid(path, cell=16, profile_id="etl")
    monkeypatch.setattr(routesearch, "PARALLEL_MIN_CELLS", 1)
    sample = pairs(lazy, 80, seed=5)
    serial = analysis.path_distances(lazy, sample, workers=1, with_paths=True)
    graph, expand = routesearch.step_graph(eager)
    keys = [(0, *c) for c in eager.walkable_cells()]
    assert routesearch.learn(graph, expand, keys, processes=3) == len(keys)
    assert graph.sizes()[1] == len(keys)
    parallel = analysis.path_distances(eager, sample, workers=3, with_paths=True)
    assert parallel == serial


def test_max_detour_is_exact_inside_the_bound_and_says_so_outside(built):
    grid = built["stand"]
    # Either side of the wall near its far end: the route goes round through the gap.
    a, b = (300.0, 400.0, 0.0), (520.0, 400.0, 0.0)
    free = analysis.path_distance(grid, a, b)
    assert free["reachable"] and free["detour_ratio"] > 2
    loose = analysis.path_distance(grid, a, b, max_detour=free["detour_ratio"] + 0.01)
    assert loose == free
    tight = analysis.path_distance(grid, a, b, max_detour=1.5)
    assert tight["reachable"] is False and tight["exceeds_max_detour"]
    assert tight["nodes_expanded"] < free["nodes_expanded"]
    sample = pairs(grid, 100, seed=6, lo=64, hi=500)
    bounded = analysis.path_distances(grid, sample, max_detour=1.3, use_components=False)
    unbounded = analysis.path_distances(grid, sample, use_components=False)
    for x, y in zip(bounded, unbounded, strict=True):
        if y["reachable"] and y["detour_ratio"] <= 1.3:
            assert x == y
        else:
            assert x["reachable"] is False


def _ends(game_map, a, b):
    return (
        routes.endpoint(game_map, "etl", a, None, "start"),
        routes.endpoint(game_map, "etl", b, None, "end"),
    )


@pytest.mark.parametrize("stances", [("stand", "crouch", "prone"), ("crouch",), ("stand",)])
def test_find_route_is_the_python_search_exactly(built, stances):
    grids = {s: built[s] for s in stances}
    game_map = built["stand"].game_map
    sample = pairs(built["prone"], 40, seed=7)
    for a, b in sample:
        start, end = _ends(game_map, a, b)
        native = routes.find_route(grids, start, end, engine="native")
        python = routes.find_route(grids, start, end, engine="python")
        assert native == python
    # Budget accounting is the same too (find_route stops before closing a new node).
    start, end = _ends(game_map, (40, 40, 0), (980, 700, 0))
    capped = [routes.find_route(grids, start, end, max_nodes=7, engine=e) for e in ("native", "python")]
    assert capped[0] == capped[1] and capped[0]["nodes_expanded"] == 7


def test_find_routes_batch_rows_match_find_route(built):
    grids = dict(built)
    game_map = built["stand"].game_map
    links = [_ends(game_map, a, b) for a, b in pairs(built["stand"], 50, seed=8, hi=500)]
    rows = routes.find_routes(grids, links, with_paths=True, workers=1)
    for (start, end), row in zip(links, rows, strict=True):
        single = routes.find_route(grids, start, end)
        single.pop("notes")
        single.pop("grids")
        assert row == single
    bounded = routes.find_routes(grids, links, max_detour=1.2)
    assert {r["status"] for r in bounded} <= {"found", "exceeds_max_detour", "unreachable", "no_endpoint"}
    for row, free in zip(bounded, rows, strict=True):
        if row["status"] == "found":
            assert row["cost"] == free["cost"]


def test_route_report_checks_a_list_of_links(tmp_path):
    server.map_open(str(course(tmp_path)))
    links = [
        [[60, 100, 0], [300, 100, 0]],
        {"start": [300, 400, 0], "end": [520, 400, 0], "stance": "stand"},
        {"start": [200, 300, 0], "end": [200, 600, 0], "stance": "stand"},
        {"start": [200, 300, 0], "end": [200, 600, 0], "stance": "crouch"},
        [[650, 250, 0], [650, 480, 64]],
    ]
    out = gameplay.route_report(links=links, profile_id="etl", max_detour=4)
    rows = out["links"]
    assert [r["index"] for r in rows] == list(range(5))
    assert rows[0]["status"] == "found" and rows[0]["detour_ratio"] < 1.1
    assert rows[1]["status"] == "found" and rows[1]["detour_ratio"] > 2
    assert rows[2]["status"] != "found"  # too low to stand in the tunnel
    assert rows[3]["status"] == "found" and rows[3]["stance"] == "crouch"
    assert rows[4]["status"] != "found"  # up a ledge
    assert sum(out["by_status"].values()) == 5 and out["links_checked"] == 5
    assert "segments" not in rows[0]
    full = gameplay.route_report(links=links[:1], profile_id="etl", detail="full")
    assert full["links"][0]["segments"] and "grids" in full
    single = gameplay.route_report(start=[60, 100, 0], end=[300, 100, 0], profile_id="etl")
    assert single["cost"] == rows[0]["cost"]
    with pytest.raises(Exception, match="not both"):
        gameplay.route_report(links=links, start=[0, 0, 0], profile_id="etl")
    with pytest.raises(Exception, match=r"links\[0\]"):
        gameplay.route_report(links=[[[0, 0], [1, 1, 1]]], profile_id="etl")


@pytest.mark.skipif(not (REAL / "oasis_final.map").is_file(), reason="real corpus not present")
def test_a_real_map_gives_the_same_answers():
    grid = analysis.build_navgrid(
        REAL / "oasis_final.map", cell=32, profile_id="etl", max_cells=80_000_000
    )
    sample = pairs(grid, 60, seed=9, lo=100, hi=400) + pairs(grid, 6, seed=10)
    native = [analysis.path_distance(grid, a, b, engine="native") for a, b in sample]
    python = [analysis.path_distance(grid, a, b, engine="python") for a, b in sample]
    assert native == python
    assert sum(r["reachable"] for r in native) > 20
