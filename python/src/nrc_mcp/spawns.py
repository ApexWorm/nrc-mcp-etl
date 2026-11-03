"""Standing positions for a cluster of team spawns, and the profile recipe that names them.

`spawn_cluster` is the tool; this is the part that can be tested without a session. Two
questions are answered here, and neither is guessed:

**Where can a player stand?** Not "where is a walkable voxel": a spawn is a single point
the whole player box is put at, so the test is exact. The navgrid already carries the map's
collision brushes expanded by the player box (`NavGrid.clearance_solids`, see
`analysis._body_obstacles`): a feet position is clear exactly when it lies outside every
expanded brush. The free intervals on one vertical line through those brushes are the
heights a body fits at, and the bottom of each free interval is a floor the box rests on.
The expansion is by the brush's face planes, which encloses the true Minkowski sum, so an
angled corner can be refused that a player would fit, never the reverse.

A box can rest on a single corner of a ledge, which the engine allows and a spawn should
not: the floor under the origin itself must lie within a step of the resting height. And the
position must snap onto the walkable grid, so `spawn_safety` and `balance_report` measure
from it rather than reporting it off the grid.

**What does the game need written?** Classnames, spawnflags, the facing key, the region
marker and the names a script uses are all in the profile's `spawn_cluster:` section. This
module reads that section and has no game's strings of its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from . import profiles
from .analysis import NavGrid, column_intervals

#: Clearance above a floor that counts as room for the body. The expanded brushes already
#: include the profile's floor separation; this only keeps a zero-width gap from passing.
GAP_EPS = 1.0e-3


class SpawnClusterError(ValueError):
    pass


def recipe(profile_id: str) -> dict[str, Any]:
    """The profile's `spawn_cluster:` section, or an error saying it has none."""
    data = profiles.load(profile_id).get("spawn_cluster")
    if not isinstance(data, dict) or not isinstance(data.get("spawn"), dict):
        raise SpawnClusterError(
            f"profile {profile_id} has no spawn_cluster section, so it does not say which "
            "classnames, flags and marker a team spawn needs; use entities_place instead"
        )
    return data


def team_label(profile_id: str, team: str) -> str:
    """A side's label (`teams.sides[].label`) from its label or its name, any case."""
    sides = (profiles.load(profile_id).get("teams") or {}).get("sides") or []
    wanted = team.strip().lower()
    for side in sides:
        if not isinstance(side, dict):
            continue
        names = {str(side.get("label", "")).lower(), str(side.get("name", "")).lower()}
        if wanted in names:
            return str(side["label"])
    known = ", ".join(f"{s.get('label')} ({s.get('name')})" for s in sides if isinstance(s, dict))
    raise SpawnClusterError(f"unknown team {team!r}; profile {profile_id} has {known or 'none'}")


def by_team(section: dict, key: str, team: str, what: str) -> Any:
    value = section.get(key)
    if isinstance(value, dict):
        if team not in value:
            raise SpawnClusterError(f"the profile's spawn_cluster gives no {what} for {team}")
        return value[team]
    if value is None:
        raise SpawnClusterError(f"the profile's spawn_cluster gives no {what}")
    return value


def lattice(lo: float, hi: float, spacing: float, width: float, grid: int) -> list[float]:
    """Positions along one axis, `spacing` apart, centred in `[lo, hi]`.

    As many as fit with the player box inside the span; one at the centre when the span is
    narrower than a box. The first is put on the authoring grid, so with a spacing that is a
    multiple of it every position is on the grid.
    """
    n = max(1, math.floor((hi - lo - width) / spacing) + 1) if hi - lo >= width else 1
    start = (lo + hi) / 2 - (n - 1) * spacing / 2
    if grid > 0:
        start = round(start / grid) * grid
    return [start + k * spacing for k in range(n)]


@dataclass
class Spot:
    x: float
    y: float
    floor: float
    cell: tuple[int, int, int]


@dataclass
class Search:
    spots: list[Spot] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, reason: str) -> None:
        self.rejected[reason] = self.rejected.get(reason, 0) + 1


def resting_floor(grid: NavGrid, x: float, y: float, bottom: float, top: float) -> float | None:
    """The highest height in `[bottom, top]` the player box rests at with room above it.

    Read off the expanded brushes on the vertical line through `(x, y)`: each blocked
    interval's top is where a box stands on something, and it is a floor when the next
    blocked interval starts above it.
    """
    blocked = column_intervals(grid.clearance_solids, grid.clearance_index, x, y)
    best = None
    for i, (_lo, hi) in enumerate(blocked):
        if hi < bottom or hi > top:
            continue
        above = blocked[i + 1][0] if i + 1 < len(blocked) else math.inf
        if above - hi > GAP_EPS:
            best = hi
    return best


def supported(grid: NavGrid, x: float, y: float, floor: float) -> bool:
    """Whether the floor under the origin itself is within a step of the resting height."""
    step = grid.movement.step_height.value
    tops = [hi for _lo, hi in column_intervals(grid.solids, grid.index, x, y) if hi <= floor + 1e-6]
    return bool(tops) and floor - max(tops) <= step


def find_spots(
    grid: NavGrid,
    xs: list[float],
    ys: list[float],
    bottom: float,
    top: float,
    avoid: list[tuple[float, float, float]],
    keep_apart: float,
) -> Search:
    """Every lattice position a player can stand at, with a count of why the rest cannot."""
    if grid.clearance_index is None:
        raise SpawnClusterError(
            f"profile {grid.profile} states no player width (units.player.bounding_box), so "
            "no position can be checked for room to stand"
        )
    out = Search()
    for y in ys:
        for x in xs:
            floor = resting_floor(grid, x, y, bottom, top)
            if floor is None:
                out.reject("no_floor_with_headroom")
                continue
            if not supported(grid, x, y, floor):
                out.reject("over_an_edge")
                continue
            snapped = grid.nearest_walkable((x, y, floor), radius=grid.cell * 2)
            if snapped is None:
                out.reject("off_walkable_grid")
                continue
            if any(
                math.hypot(ax - x, ay - y) < keep_apart and abs(az - floor) < grid.cell * 4
                for ax, ay, az in avoid
            ):
                out.reject("near_existing_spawn")
                continue
            out.spots.append(Spot(x, y, floor, snapped[0]))
    return out


def choose(spots: list[Spot], count: int, centre: tuple[float, float]) -> list[Spot]:
    """The `count` spots nearest the area's centre, returned in lattice order."""
    order = {id(s): i for i, s in enumerate(spots)}
    nearest = sorted(
        spots, key=lambda s: (math.hypot(s.x - centre[0], s.y - centre[1]), order[id(s)])
    )
    return sorted(nearest[:count], key=lambda s: order[id(s)])


def names(section: dict, group: str) -> dict[str, str]:
    """The keys `section.group_keys` gives an entity of group `group`."""
    keys = section.get("group_keys") or {}
    return {str(k): str(v).format(group=group) for k, v in keys.items()}
