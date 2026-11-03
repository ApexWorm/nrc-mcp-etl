"""Auditable routes across stance-specific navigation grids.

Costs are explicit planning preferences in weighted world units, never travel time.
Only graph edges tested by NavGrid are used. A stance change stays on the same floor
cell, and is possible only if both body boxes fit there.
"""

from __future__ import annotations

import heapq
import math
from typing import Any

from . import analysis

STANCES = ("stand", "crouch", "prone")
DEFAULT_COSTS = {"stand": 1.0, "crouch": 1.5, "prone": 2.0}
MAX_NODES = 250_000


def number(value: Any, name: str, *, zero: bool = False, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite {'non-negative' if zero else 'positive'} number")
    value = float(value)
    if not math.isfinite(value) or (value < 0 if zero else value <= 0):
        raise ValueError(f"{name} must be a finite {'non-negative' if zero else 'positive'} number")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum:g}")
    return value


def stances(profile_id: str, stance: str) -> tuple[str, ...]:
    if stance not in (*STANCES, "mixed"):
        raise ValueError("stance must be stand, crouch, prone or mixed")
    if stance != "mixed":
        return (stance,)
    movement = analysis.movement_constants(profile_id)
    return tuple(s for s in STANCES if getattr(movement, f"headroom_{s}") is not None)


def endpoint(game_map, profile_id: str, position, entity_index, name: str) -> dict:
    """Resolve point entities and objective approach areas without parsing map text."""
    if (position is None) == (entity_index is None):
        raise ValueError(f"specify exactly one of {name} and {name}_entity")
    if position is not None:
        if not isinstance(position, (list, tuple)) or len(position) != 3:
            raise ValueError(f"{name} must contain three finite coordinates")
        if any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in position
        ):
            raise ValueError(f"{name} must contain three finite coordinates")
        return {"kind": "position", "requested": list(position), "points": [tuple(position)]}
    if isinstance(entity_index, bool) or not isinstance(entity_index, int):
        raise ValueError(f"{name}_entity must be an entity index")
    entities = {int(e["index"]): e for e in game_map.entities()}
    entity = entities.get(entity_index)
    if entity is None or entity_index == 0:
        raise ValueError(f"{name}_entity must identify a placed entity, got {entity_index}")
    spawns, objectives, _ = analysis._markers(game_map, analysis.roles(profile_id))
    marker = next((m for m in [*spawns, *objectives] if entity_index in m.entity_indices), None)
    origin = marker.origin if marker else entity.get("origin")
    bounds = marker.bounds if marker else analysis._brush_bounds(game_map, entity)
    if origin is None and bounds is None:
        raise ValueError(f"entity {entity_index} has no origin or usable brush bounds")
    return {
        "kind": "entity",
        "entity": entity_index,
        "classname": entity.get("classname"),
        "represented_entities": list(marker.entity_indices) if marker else [entity_index],
        "requested": list(origin) if origin is not None else None,
        "bounds": bounds,
        "points": [tuple(origin)] if bounds is None else [],
    }


def _snaps(grid: analysis.NavGrid, target: dict, radius: float):
    points = target["points"]
    if target.get("bounds") is not None:
        points = analysis._approach_points(target["bounds"], grid.cell)
    found = {}
    rings = math.ceil(radius / grid.cell)
    for point in points:
        anchor = point
        if grid.index is not None:
            floor, _ = analysis.floor_and_ceiling(
                analysis.column_intervals(grid.solids, grid.index, point[0], point[1]), point[2]
            )
            if floor is None:
                continue
            anchor = (point[0], point[1], floor)
        if grid.clearance_index is not None and not analysis.segment_clear(
            grid.clearance_solids, grid.clearance_index, anchor, anchor
        ):
            continue
        ix, iy, _ = grid.cell_at(point)
        best = None
        for x in range(ix - rings, ix + rings + 1):
            for y in range(iy - rings, iy + rings + 1):
                for z in grid.columns.get((x, y), ()):
                    cell = (x, y, z)
                    snapped = grid.centre(cell)
                    distance = math.dist(point, snapped)
                    if (
                        distance > radius
                        or abs(point[2] - snapped[2]) > grid.movement.headroom_stand.value
                    ):
                        continue
                    if best is not None and distance >= best[0]:
                        continue
                    # A coordinate may be at the floor or at a point entity's origin above
                    # it. Resolve that reference first, then keep the connector on the same
                    # floor (allowing one cell of conservative vertical quantization).
                    if abs(anchor[2] - snapped[2]) > min(
                        grid.cell, grid.movement.step_height.value
                    ):
                        continue
                    if grid.index is not None and not analysis.segment_clear(
                        grid.solids, grid.index, point, snapped
                    ):
                        continue
                    if grid.index is not None:
                        samples = max(
                            1,
                            math.ceil(max(abs(anchor[i] - snapped[i]) for i in (0, 1)) / grid.cell),
                        )
                        supported = True
                        for part in range(1, samples):
                            x, y = (
                                anchor[i] + (snapped[i] - anchor[i]) * part / samples
                                for i in (0, 1)
                            )
                            floor, _ = analysis.floor_and_ceiling(
                                analysis.column_intervals(grid.solids, grid.index, x, y),
                                max(anchor[2], snapped[2]),
                            )
                            if floor is None or min(
                                abs(floor - anchor[2]), abs(floor - snapped[2])
                            ) > min(grid.cell, grid.movement.step_height.value):
                                supported = False
                                break
                        if not supported:
                            continue
                    if grid.clearance_index is not None:
                        legs = _legs(anchor, snapped)
                        if not all(
                            analysis.segment_clear(
                                grid.clearance_solids, grid.clearance_index, a, b
                            )
                            for a, b in zip(legs, legs[1:], strict=False)
                        ):
                            continue
                    best = (distance, cell, point)
        if best is not None:
            distance, cell, point = best
            if cell not in found or distance < found[cell]["distance"]:
                found[cell] = {
                    "distance": distance,
                    "approach": list(point),
                    "position": list(grid.centre(cell)),
                }
    return found


def _simplify(states):
    """Remove only collinear interior vertices of the same stance, never corners."""
    if len(states) < 3:
        return states
    kept = [states[0]]
    for before, current, after in zip(states, states[1:], states[2:], strict=False):
        if before[0] == current[0] == after[0]:
            a = tuple(current[1][i] - before[1][i] for i in range(3))
            b = tuple(after[1][i] - current[1][i] for i in range(3))
            cross = (
                a[1] * b[2] - a[2] * b[1],
                a[2] * b[0] - a[0] * b[2],
                a[0] * b[1] - a[1] * b[0],
            )
            if cross == (0, 0, 0) and sum(a[i] * b[i] for i in range(3)) > 0:
                continue
        kept.append(current)
    kept.append(states[-1])
    return kept


def _legs(p0, p1):
    top = max(p0[2], p1[2])
    points = [p0]
    for point in ((p0[0], p0[1], top), (p1[0], p1[1], top), p1):
        if point != points[-1]:
            points.append(point)
    return points


def _sweep(grid, source, target):
    """The same lift/horizontal/lower legs used by NavGrid's body clearance test."""
    return _legs(grid.centre(source), grid.centre(target))


def _length(points):
    return sum(math.dist(a, b) for a, b in zip(points, points[1:], strict=False))


def find_route(
    grids: dict[str, analysis.NavGrid],
    start: dict,
    end: dict,
    *,
    start_stance: str | None = None,
    end_stance: str | None = None,
    stance_costs: dict | None = None,
    transition_cost: float = 32,
    max_nodes: int = 100_000,
    snap_radius: float | None = None,
) -> dict:
    """A* over (stance, cell), with directed walking edges and safe stance changes."""
    if not grids:
        raise ValueError("at least one stance grid is required")
    reference = next(iter(grids.values()))
    source = reference.game_map.source() if reference.game_map is not None else None
    for stance, grid in grids.items():
        if stance not in STANCES or grid.stance != stance:
            raise ValueError("grid keys must match their stance")
        if (grid.origin, grid.dims, grid.cell, grid.profile) != (
            reference.origin,
            reference.dims,
            reference.cell,
            reference.profile,
        ):
            raise ValueError("all stance grids must describe the same aligned snapshot")
        if (
            grid.map_path != reference.map_path
            or grid.movement != reference.movement
            or (grid.game_map is not None and grid.game_map.source() != source)
        ):
            raise ValueError("all stance grids must describe the same aligned snapshot")
    allowed = tuple(s for s in STANCES if s in grids)
    start_stance = allowed[0] if start_stance is None else start_stance
    end_stance = allowed[0] if end_stance is None else end_stance
    for name, value in (("start_stance", start_stance), ("end_stance", end_stance)):
        if value not in (*allowed, "any"):
            raise ValueError(f"{name} must be one of {', '.join(allowed)} or any")
    if (
        isinstance(max_nodes, bool)
        or not isinstance(max_nodes, int)
        or not 1 <= max_nodes <= MAX_NODES
    ):
        raise ValueError(f"max_nodes must be an integer between 1 and {MAX_NODES}")
    transition_cost = number(transition_cost, "transition_cost", zero=True, maximum=1_000_000)
    radius = (
        min(reference.cell * 4, 4096)
        if snap_radius is None
        else number(snap_radius, "snap_radius", maximum=4096)
    )
    if radius / reference.cell > 64:
        raise ValueError("snap_radius must span at most 64 cells")
    if stance_costs is not None and not isinstance(stance_costs, dict):
        raise ValueError("stance_costs must map allowed stances to positive weights")
    if set(stance_costs or {}) - set(allowed):
        raise ValueError("stance_costs may only name stances included in this route")
    costs = {
        s: number(
            (stance_costs or {}).get(s, DEFAULT_COSTS[s]), f"stance_costs.{s}", maximum=1_000_000
        )
        for s in allowed
    }
    sources = {
        (s, cell): snap
        for s in allowed
        if start_stance in (s, "any")
        for cell, snap in _snaps(grids[s], start, radius).items()
    }
    goals = {
        (s, cell): snap
        for s in allowed
        if end_stance in (s, "any")
        for cell, snap in _snaps(grids[s], end, radius).items()
    }
    notes = list(dict.fromkeys(note for grid in grids.values() for note in grid.notes))
    notes += [
        "Costs are weighted world distance plus a fixed cost per stance change; they are planning preferences, not seconds or measured speed.",
        "Endpoint snapping is reported and excluded from route cost/distance. Reference heights resolve to floor; connectors stay within one cell/step of that floor, sample floor support and check body clearance without crossing brushes. They do not simulate engine movement.",
        "Stance changes require both body boxes at the same floor cell. Animation, prone legs/turning, combat and dynamic script states are not simulated.",
        "The witness retains every corner and stance transition; only collinear interior vertices are removed. Top-down projection can overlap routes on different floors.",
        "Steps and drops follow the tested lift/horizontal/lower sweep. Intermediate lifted points need not be floor nodes; distance measures these sweep legs, not a straight line through a riser.",
    ]
    report = {
        "map": reference.map_path,
        "profile": reference.profile,
        "cell": reference.cell,
        "stances": list(allowed),
        "cost_model": {
            "kind": "weighted_distance",
            "units": "weighted_world_units",
            "stance_costs": costs,
            "transition_cost": transition_cost,
            "source": "caller weights with explicit planning defaults for omitted stances",
            "distance_model": "checked lift/horizontal/lower sweep legs",
        },
        "start": {k: v for k, v in start.items() if k != "points"},
        "end": {k: v for k, v in end.items() if k != "points"},
        "snap_radius": radius,
        "notes": notes,
        "grids": {s: g.as_dict() for s, g in grids.items()},
        "reachable": False,
        "distance": None,
        "cost": None,
        "path": [],
        "segments": [],
        "transitions": [],
        "nodes_expanded": 0,
    }
    if not sources or not goals:
        report.update(
            status="no_endpoint",
            reason=f"no visible walkable {'start' if not sources else 'end'} cell within the snap radius in its requested stance",
        )
        return report
    goal_points = [grids[s].centre(c) for s, c in goals]
    min_weight = min(costs.values())

    def heuristic(state):
        point = grids[state[0]].centre(state[1])
        return min(math.dist(point, target) for target in goal_points) * min_weight

    best = {state: 0.0 for state in sources}
    came = {}
    queue = [
        (heuristic(state), 0, STANCES.index(state[0]), n, state) for n, state in enumerate(sources)
    ]
    heapq.heapify(queue)
    tie = len(queue)
    closed = set()
    changes = dict.fromkeys(sources, 0)
    reached = None
    while queue:
        _, _, _, _, current = heapq.heappop(queue)
        if current in closed:
            continue
        if len(closed) >= max_nodes:
            report.update(
                status="budget_exhausted",
                reachable=None,
                nodes_expanded=len(closed),
                reason=f"search reached max_nodes={max_nodes}; reachability remains unknown",
            )
            return report
        closed.add(current)
        if current in goals:
            reached = current
            break
        stance, cell = current
        edges = [
            ((stance, dest), _length(_sweep(grids[stance], cell, dest)) * costs[stance], False)
            for dest, _ in grids[stance].neighbours(cell)
        ]
        edges += [
            ((other, cell), transition_cost, True)
            for other in allowed
            if other != stance and grids[other].is_walkable(cell)
        ]
        for nxt, edge_cost, change in edges:
            if nxt in closed:
                continue
            candidate = best[current] + edge_cost
            change_count = changes[current] + int(change)
            if candidate < best.get(nxt, math.inf) or (
                candidate == best.get(nxt) and change_count < changes[nxt]
            ):
                best[nxt] = candidate
                changes[nxt] = change_count
                came[nxt] = current
                tie += 1
                heapq.heappush(
                    queue,
                    (candidate + heuristic(nxt), change_count, STANCES.index(nxt[0]), tie, nxt),
                )
    report["nodes_expanded"] = len(closed)
    if reached is None:
        report.update(
            status="unreachable",
            reason="no directed route connects the snapped endpoints in the allowed stances",
        )
        return report
    states = [reached]
    while states[-1] in came:
        states.append(came[states[-1]])
    states.reverse()
    first = states[0]
    report["start"].update(sources[first], stance=first[0], cell=list(first[1]))
    report["end"].update(goals[reached], stance=reached[0], cell=list(reached[1]))
    witness = [(first[0], grids[first[0]].centre(first[1]))]
    distance = 0.0
    stance_distances = dict.fromkeys(allowed, 0.0)
    for a, b in zip(states, states[1:], strict=False):
        if a[0] == b[0]:
            points = _sweep(grids[a[0]], a[1], b[1])
            length = _length(points)
            distance += length
            stance_distances[a[0]] += length
            witness.extend((a[0], point) for point in points[1:])
        else:
            witness.append((b[0], grids[b[0]].centre(b[1])))
    simplified = _simplify(witness)
    segments = []
    transitions = []
    for state in simplified:
        stance, point = state
        point = list(point)
        if not segments or segments[-1]["stance"] != stance:
            if segments:
                transitions.append(
                    {
                        "position": point,
                        "from_stance": segments[-1]["stance"],
                        "to_stance": stance,
                        "cost": transition_cost,
                    }
                )
            segments.append({"stance": stance, "points": []})
        segments[-1]["points"].append(point)
    for segment in segments:
        segment["distance"] = round(
            sum(
                math.dist(a, b)
                for a, b in zip(segment["points"], segment["points"][1:], strict=False)
            ),
            6,
        )
    report.update(
        reachable=True,
        status="found",
        reason=None,
        distance=round(distance, 6),
        cost=round(best[reached], 6),
        distance_by_stance={s: round(v, 6) for s, v in stance_distances.items()},
        path_cells=len(states),
        path=[{"position": list(point), "stance": s} for s, point in simplified],
        collinear_vertices_removed=len(witness) - len(simplified),
        segments=segments,
        transitions=transitions,
    )
    return report
