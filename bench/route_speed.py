"""Link-check speed: the Python A* against the native route search, on real maps.

    uv run python bench/route_speed.py [MAP ...] [--cells 16 32] [--links 10000]

For each map and cell size it builds the standing grid, draws `--links` waypoint-like links
(endpoints on walkable cells, 100-400 units apart) and `--long` random pairs, and reports:

* before: `analysis.path_distance(engine="python")` on a sample of the links, plus the
  one-off component labelling every first query paid; the 10k total is extrapolated.
* after: `analysis.path_distances` over all links in one call on a grid with nothing
  learned yet, learning lazily (`workers=1`) and in parallel (the default); again once
  learned (warm); and cold with `max_detour=3`.
* that the two searches agree on the sample (the full result dict, path included).

Writes nothing; prints one JSON object per map/cell and a table.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))

from nrc_mcp import analysis, routesearch  # noqa: E402

DEFAULT_MAPS = sorted(str(p) for p in (ROOT / "corpus" / "real").glob("*_final.map"))


def links(grid, count: int, rng: random.Random) -> list:
    points = [grid.centre(c) for c in grid.walkable_cells()]
    out = []
    while len(out) < count:
        a, b = rng.choice(points), rng.choice(points)
        if 100 < math.dist(a, b) < 400:
            out.append((a, b))
    return out


def forget(grid) -> None:
    """Drop what the native search learned, so the next run starts cold."""
    if hasattr(grid, routesearch._ATTR):
        delattr(grid, routesearch._ATTR)


def timed(fn):
    t = time.perf_counter()
    value = fn()
    return value, time.perf_counter() - t


def run(map_path: str, cell: float, n_links: int, sample: int, n_long: int, long_sample: int):
    rng = random.Random(1)
    grid, build_s = timed(
        lambda: analysis.build_navgrid(
            map_path, cell=cell, profile_id="etl", stance="stand", max_cells=80_000_000
        )
    )
    short = links(grid, n_links, rng)
    walkable = [grid.centre(c) for c in grid.walkable_cells()]
    long_pairs = [(rng.choice(walkable), rng.choice(walkable)) for _ in range(n_long)]
    row = {
        "map": Path(map_path).name,
        "cell": cell,
        "build_s": round(build_s, 1),
        "walkable_cells": len(walkable),
        "links": n_links,
    }

    # --- after: native, cold (serial, then parallel learning), warm, bounded -----------
    def batch(**kw):
        return analysis.path_distances(grid, short, use_components=False, **kw)

    serial, serial_s = timed(lambda: batch(workers=1))
    row["after_cold_serial_total_s"] = round(serial_s, 2)
    row["after_cold_serial_ms_per_link"] = round(serial_s / n_links * 1000, 3)
    row["learned"] = routesearch.graph_sizes(grid)["step"]
    native, warm_s = timed(batch)
    row["after_warm_total_s"] = round(warm_s, 2)
    row["after_warm_ms_per_link"] = round(warm_s / n_links * 1000, 4)
    forget(grid)
    parallel, cold_s = timed(batch)
    row["workers"] = routesearch.workers()
    row["after_cold_total_s"] = round(cold_s, 2)
    row["after_cold_ms_per_link"] = round(cold_s / n_links * 1000, 3)
    assert parallel == native == serial, "parallel learning must not change an answer"
    forget(grid)
    bounded, bounded_s = timed(lambda: batch(max_detour=3))
    row["after_cold_max_detour3_total_s"] = round(bounded_s, 2)
    row["reachable"] = sum(r["reachable"] for r in native)
    row["reachable_within_detour3"] = sum(r["reachable"] for r in bounded)
    assert all(b == n for b, n in zip(bounded, native, strict=True) if b["reachable"]), (
        "a bounded result inside its bound must be the unbounded result"
    )

    # --- before: the Python reference --------------------------------------------------
    _, components_s = timed(grid.components)
    row["components_s"] = round(components_s, 1)
    sample_links = short[:sample]
    before, before_s = timed(
        lambda: [analysis.path_distance(grid, a, b, engine="python") for a, b in sample_links]
    )
    per_link = before_s / len(sample_links)
    row["before_ms_per_link"] = round(per_link * 1000, 2)
    row["before_10k_total_s_est"] = round(components_s + per_link * 10_000, 1)

    # --- agreement ------------------------------------------------------------------------
    native_full = [analysis.path_distance(grid, a, b, engine="native") for a, b in sample_links]
    row["sample_identical"] = sum(a == b for a, b in zip(before, native_full, strict=True))
    row["sample"] = len(sample_links)
    row["reachability_identical_without_components"] = all(
        a["reachable"] == b["reachable"] and a.get("distance") == b.get("distance")
        for a, b in zip(before, native[:sample], strict=True)
    )

    # --- long random pairs --------------------------------------------------------------
    some_long = long_pairs[:long_sample]
    py_long, py_long_s = timed(
        lambda: [analysis.path_distance(grid, a, b, engine="python") for a, b in some_long]
    )
    row["before_long_ms_per_pair"] = round(py_long_s / len(some_long) * 1000, 1)
    nat_long, nat_long_s = timed(lambda: analysis.path_distances(grid, long_pairs))
    row["after_long_ms_per_pair"] = round(nat_long_s / len(long_pairs) * 1000, 2)
    row["long_same_answer"] = sum(
        (a["reachable"], a.get("distance")) == (b["reachable"], b.get("distance"))
        for a, b in zip(py_long, nat_long, strict=False)
    )
    row["long_sample"] = len(some_long)
    row["learned_after_long"] = routesearch.graph_sizes(grid)["step"]
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("maps", nargs="*", default=DEFAULT_MAPS)
    parser.add_argument("--cells", type=float, nargs="+", default=[16, 32])
    parser.add_argument("--links", type=int, default=10_000)
    parser.add_argument("--sample", type=int, default=200, help="links timed in Python")
    parser.add_argument("--long", type=int, default=100)
    parser.add_argument("--long-sample", type=int, default=10, help="long pairs in Python")
    args = parser.parse_args(argv)
    if not routesearch.available():
        print("the loaded kernel has no RouteGraph: run `mise run kernel:build`", file=sys.stderr)
        return 2
    rows = []
    for map_path in args.maps:
        for cell in args.cells:
            row = run(map_path, cell, args.links, args.sample, args.long, args.long_sample)
            print(json.dumps(row), flush=True)
            rows.append(row)
    print()
    print(
        f"{'map':20} {'cell':>4} {'ms/link before':>14} {'cold 1 proc':>11} {'cold par.':>9}"
        f" {'warm':>7} | 10k s: {'before':>7} {'cold 1':>7} {'cold par.':>9} {'warm':>6}"
    )
    for r in rows:
        print(
            f"{r['map']:20} {r['cell']:>4g} {r['before_ms_per_link']:>14} "
            f"{r['after_cold_serial_ms_per_link']:>11} {r['after_cold_ms_per_link']:>9} "
            f"{r['after_warm_ms_per_link']:>7} | {'':6} {r['before_10k_total_s_est']:>7} "
            f"{r['after_cold_serial_total_s']:>7} {r['after_cold_total_s']:>9} "
            f"{r['after_warm_total_s']:>6}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
