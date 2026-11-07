"""Native A* over the walk grids, with each cell's edges learned once.

The two Python searches — `analysis.path_distance` and `routes.find_route` — spent almost all
their time asking `NavGrid.neighbours` for the same cells again on every query: a neighbour
test sweeps the player's body box against the brushes, and a short link check on a 16-unit
grid expands a few thousand cells. Here the search runs in the kernel (`nrc_py.RouteGraph`)
and calls back into the grid only the first time it expands a cell; the answer is kept on
the grid, so a thousand link checks pay for each cell's clearance tests once.

The grid stays the only authority on movement: every edge is one `neighbours()` returned,
with the cost the Python search would have charged. The kernel reproduces the Python
searches' priority order, budget accounting and heuristic bit for bit, so the same queries
give the same reachability, the same cost and the same path
(`python/tests/test_route_search.py` holds it to that against the Python reference).

A search may also take a `bound`: nodes whose straight-line lower bound already exceeds it
are pruned. A link from A to B then only explores the ellipse around the segment that a
route within the bound could use, which is what makes a failed short link cheap.
"""

from __future__ import annotations

import math
import multiprocessing
import os
import struct
import sys
import threading
import warnings
from array import array
from typing import Any

from .kernel import kernel

#: Graphs kept on a grid, keyed by search kind and the grids it spans.
_ATTR = "_route_graphs"
#: Fewest cells worth handing to worker processes; below it, learning stays lazy.
PARALLEL_MIN_CELLS = 4000
#: Worker processes for parallel learning; `NRC_ROUTE_WORKERS=1` turns it off.
MAX_WORKERS = 16
_MOVE = struct.Struct("<iiiidB")
#: Held around every use of a graph: a search calls back into Python, and another thread
#: must not start a second search on the same graph meanwhile (the kernel would refuse).
LOCK = threading.RLock()


def available() -> bool:
    """Whether the loaded kernel has the native search (an old `nrc_py.so` may not)."""
    return hasattr(kernel(), "RouteGraph")


def _cache(grid: Any) -> dict:
    graphs = getattr(grid, _ATTR, None)
    if graphs is None:
        graphs = {}
        setattr(grid, _ATTR, graphs)
    return graphs


def _heights(graph, layer: int, grid: Any) -> None:
    """Give the graph a grid's resting heights, so its heuristic measures from the same
    points `NavGrid.centre` does (an older kernel without them measures from the cell
    boundary: same routes, possibly a different expansion count)."""
    stand_z = getattr(grid, "stand_z", None)
    if stand_z and hasattr(graph, "set_heights"):
        # The kernel takes a dict; a built grid's heights are a flat view (`RestHeights`).
        graph.set_heights(layer, stand_z if isinstance(stand_z, dict) else dict(stand_z.items()))


def step_graph(grid: Any):
    """The graph `path_distance` searches: one layer, edges and costs from `neighbours()`."""
    graphs = _cache(grid)
    entry = graphs.get("step")
    if entry is None:

        def expand(_layer: int, ix: int, iy: int, iz: int) -> list:
            return [
                (0, dest[0], dest[1], dest[2], step, False)
                for dest, step in grid.neighbours((ix, iy, iz))
            ]

        graph = kernel().RouteGraph(tuple(grid.origin), grid.cell)
        _heights(graph, 0, grid)
        entry = (graph, expand)
        graphs["step"] = entry
    return entry


def stance_graph(grids: dict, stances: tuple[str, ...], allowed: tuple[str, ...], length):
    """The graph `find_route` searches: a layer per stance, walking moves measured by
    `length(grid, cell, dest)` (unweighted), and a stance change wherever both grids have
    the cell. Kept on the first grid, keyed by the stances it allows."""
    reference = grids[allowed[0]]
    graphs = _cache(reference)
    key = ("stance", allowed, tuple(id(grids[s]) for s in allowed))
    entry = graphs.get(key)
    if entry is None:
        layer_of = {s: stances.index(s) for s in allowed}

        def expand(layer: int, ix: int, iy: int, iz: int) -> list:
            stance = stances[layer]
            cell = (ix, iy, iz)
            grid = grids[stance]
            rows = [
                (layer, dest[0], dest[1], dest[2], length(grid, cell, dest), False)
                for dest, _ in grid.neighbours(cell)
            ]
            rows += [
                (layer_of[other], ix, iy, iz, 0.0, True)
                for other in allowed
                if other != stance and grids[other].is_walkable(cell)
            ]
            return rows

        # Holding the grids keeps the ids in the key from being reused.
        graph = kernel().RouteGraph(tuple(reference.origin), reference.cell)
        for stance in allowed:
            _heights(graph, layer_of[stance], grids[stance])
        entry = (graph, expand, grids)
        graphs[key] = entry
    return entry[0], entry[1]


#: Most endpoint snaps kept per grid; past it the cache starts over.
MAX_SNAPS = 200_000


def snap_cache(grid: Any) -> dict:
    """Endpoint snaps kept on a grid by the batch link checks (see `routes.find_routes`)."""
    cache = getattr(grid, "_route_snaps", None)
    if cache is None or len(cache) > MAX_SNAPS:
        cache = {}
        grid._route_snaps = cache
    return cache


def graph_sizes(grid: Any) -> dict[str, Any]:
    """What the cached graphs on a grid hold, for diagnostics and tests."""
    out = {}
    for key, entry in _cache(grid).items():
        nodes, expanded, edges = entry[0].sizes()
        name = key if isinstance(key, str) else f"stance:{'+'.join(key[1])}"
        out[name] = {"nodes": nodes, "expanded": expanded, "edges": edges}
    return out


# ---------------------------------------------------------------------------
# Learning many cells at once
# ---------------------------------------------------------------------------
#
# A batch of thousands of links touches most of a map, and every cell's first expansion is
# a Python clearance test. Those tests are independent, so a large batch learns the cells
# it will need up front, in forked worker processes that inherit the grid as it is: the
# moves come from the same `expand` the search would have called, so the graph is the one
# lazy learning would have built.

_FORK_EXPAND = None


def workers() -> int:
    """Worker processes for parallel learning: 1 where fork is unavailable or turned off."""
    if not sys.platform.startswith("linux"):
        return 1
    configured = os.environ.get("NRC_ROUTE_WORKERS")
    if configured is not None:
        try:
            return max(1, int(configured))
        except ValueError:
            return 1
    return max(1, min(MAX_WORKERS, (os.cpu_count() or 1) - 1))


def _pack(keys: list) -> tuple[bytes, bytes, bytes]:
    nodes, counts, moves = array("i"), array("I"), []
    for key in keys:
        rows = _FORK_EXPAND(*key)  # type: ignore[misc]
        nodes.extend(key)
        counts.append(len(rows))
        moves.extend(_MOVE.pack(*row) for row in rows)
    if sys.byteorder != "little":
        nodes.byteswap()
        counts.byteswap()
    return nodes.tobytes(), counts.tobytes(), b"".join(moves)


def learn(graph, expand, keys: list, *, processes: int | None = None) -> int:
    """Learn, in worker processes, the moves of `keys` (`(layer, ix, iy, iz)`) the graph
    does not know yet. Returns how many nodes were learned: none when there are too few to
    be worth a pool or fork is unavailable, since searches learn lazily anyway.
    """
    keys = [k for k in keys if not graph.knows(k)]
    processes = workers() if processes is None else processes
    if not keys:
        return 0
    if processes <= 1 or len(keys) < PARALLEL_MIN_CELLS:
        # Not worth a pool: the searches learn these lazily, with the same result.
        return 0
    global _FORK_EXPAND
    chunk = math.ceil(len(keys) / (processes * 4))
    chunks = [keys[i : i + chunk] for i in range(0, len(keys), chunk)]
    _FORK_EXPAND = expand
    learned = 0
    LOCK.acquire()
    try:
        with warnings.catch_warnings():
            # Python warns that forking a threaded process may deadlock the child. The
            # children only run `expand` (pure geometry, no locks, no I/O) and return.
            warnings.simplefilter("ignore", DeprecationWarning)
            context = multiprocessing.get_context("fork")
            with context.Pool(processes) as pool:
                for nodes, counts, moves in pool.imap(_pack, chunks):
                    learned += graph.learn_packed(nodes, counts, moves)
    finally:
        _FORK_EXPAND = None
        LOCK.release()
    return learned


def cells_near(grid: Any, pairs: list, pad) -> list[tuple[int, int, int]]:
    """Walkable cells in columns within each pair's bounding box grown by `pad(a, b)`.

    Coarse on purpose (8-cell buckets): it decides what to learn ahead, not what a search
    may use, so a generous answer costs a little time and a stingy one nothing but laziness.
    """
    bucket = grid.cell * 8
    ox, oy = grid.origin[0], grid.origin[1]
    marked: set[tuple[int, int]] = set()
    for a, b in pairs:
        grow = pad(a, b)
        x0 = math.floor((min(a[0], b[0]) - grow - ox) / bucket)
        x1 = math.floor((max(a[0], b[0]) + grow - ox) / bucket)
        y0 = math.floor((min(a[1], b[1]) - grow - oy) / bucket)
        y1 = math.floor((max(a[1], b[1]) + grow - oy) / bucket)
        for bx in range(x0, x1 + 1):
            for by in range(y0, y1 + 1):
                marked.add((bx, by))
    return [
        (x, y, z)
        for (x, y), column in sorted(grid.columns.items())
        if (x // 8, y // 8) in marked
        for z in column
    ]


def link_pad(grid: Any, max_detour: float | None):
    """How far around a link its search may wander: the ellipse a route within
    `max_detour` could use, or the link's own length when unbounded (a guess: an
    unbounded search goes wherever the route does, and learns the rest lazily)."""
    floor = grid.cell * 6

    def pad(a, b) -> float:
        length = math.dist(a, b)
        if max_detour is None:
            return length + floor
        return length / 2 * math.sqrt(max(max_detour * max_detour - 1, 0.0)) + floor

    return pad
