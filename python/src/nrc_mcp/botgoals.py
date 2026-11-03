"""Bot map goals and the per-map bot script: camping spots chosen from the geometry, and a
script that switches them as the round's objectives change hands.

A waypoint graph lets bots move; what they do comes from map goals. The game itself reports
the objective goals (plant here, build there), but where a team should wait for its enemy,
and when to stop, are the map's to say. The bot add-on reads that from two scripts beside
the waypoint file: a table of map goals (spots with a position, a team, facings, a stance
and how long to stay) and a map script whose functions run on the game's objective
triggers and switch goals on and off per team.

Spots are chosen conservatively and each one carries the reason it was chosen:

- defend spots guard a part of an objective (the thing to blow up, the flag, the item, the
  place an item is delivered) for the team that holds it. They are seeded waypoints the team
  reaches from its spawns, near the part, that see where the attackers use it, ranked by how
  much of the attackers' shortest routes in they see; they face what they see.
- snipe spots, per team, are waypoints well back from the enemy's routes that see the most
  of those routes at long range.

Callers can pass their own spots too. Which objective kinds exist, which parts get spots for
which teams, which events switch them and where each trigger name comes from are all the
profile's (`bots.goals`, `bots.script`, `bots.spots`); the trigger names are read from the
map's own script where the game sends a script's text, so an edited announcement still
matches. Everything written is read back by the parsers here before a file is staged.
"""

from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import analysis, profiles
from . import objective_tests as objtests


class BotGoalsError(ValueError):
    pass


# --------------------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------------------


def spec(bots: dict[str, Any]) -> tuple[dict, dict, dict]:
    goals, script, spots = bots.get("goals"), bots.get("script"), bots.get("spots")
    if not all(isinstance(s, dict) for s in (goals, script, spots)):
        raise BotGoalsError("the profile's bots section describes no map goals or map script")
    return goals, script, spots


def sanitize(text: str) -> str:
    """Letters, digits and underscores only, the way the add-on names its own goals."""
    out = re.sub(r"[^A-Za-z0-9_]", "", re.sub(r"[\s\-]+", "_", str(text)))
    return re.sub(r"_+", "_", out).strip("_")


# --------------------------------------------------------------------------------------
# Spots
# --------------------------------------------------------------------------------------


@dataclass
class Spot:
    type: str
    name: str
    teams: list[str]  # teams that may ever use it
    position: tuple[float, float, float]  # player origin
    aims: list[tuple[float, float, float]] = field(default_factory=list)
    stance: str = "stand"
    camp_time: tuple[float, float] | None = None
    objective: str | None = None
    group: str | None = None
    initially: list[str] = field(default_factory=list)  # teams it starts on for
    source: str = "auto"
    why: dict[str, Any] = field(default_factory=dict)

    def row(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "teams": list(self.teams),
            "initially": list(self.initially),
            "position": [round(v, 1) for v in self.position],
            "aims": [[round(v, 3) for v in a] for a in self.aims],
            "stance": self.stance,
            **({"camp_time": list(self.camp_time)} if self.camp_time else {}),
            **({"objective": self.objective, "group": self.group} if self.objective else {}),
            "source": self.source,
            **({"why": self.why} if self.why else {}),
        }


@dataclass
class Nav:
    """The seeded graph plus what line-of-sight needs."""

    waypoints: list  # botnav.Waypoint
    stance_of: list[str]
    spawn_waypoints: dict[str, set[int]]  # by team (axis/allies)
    solids: list
    index: Any
    floor_offset: float
    eye_height: float
    grid: analysis.NavGrid
    reach: dict[str, set[int]] = field(default_factory=dict)
    adjacency: list[list[tuple[int, float]]] = field(default_factory=list)

    def __post_init__(self):
        from . import botnav

        self.adjacency = [
            [(t, math.dist(w.position, self.waypoints[t].position)) for t in w.connections]
            for w in self.waypoints
        ]
        for team, starts in self.spawn_waypoints.items():
            got: set[int] = set()
            for s in starts:
                got |= botnav.reachable(self.waypoints, s)
            self.reach[team] = got

    def eye(self, i: int) -> tuple[float, float, float]:
        x, y, z = self.waypoints[i].position
        return (x, y, z - self.floor_offset + self.eye_height)

    def sees(self, a: tuple, b: tuple) -> bool:
        return analysis.segment_clear(self.solids, self.index, a, b)

    def routes(self, starts: set[int], target: int) -> list[int]:
        """The waypoints of the shortest directed route from any of `starts` to `target`."""
        dist = {s: 0.0 for s in starts}
        prev: dict[int, int] = {}
        heap = [(0.0, s) for s in sorted(starts)]
        while heap:
            d, n = heapq.heappop(heap)
            if n == target:
                break
            if d > dist.get(n, math.inf):
                continue
            for m, w in self.adjacency[n]:
                nd = d + w
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    prev[m] = n
                    heapq.heappush(heap, (nd, m))
        if target not in dist:
            return []
        path = [target]
        while path[-1] in prev:
            path.append(prev[path[-1]])
        return path[::-1]

    def nearest(self, point: tuple, among: set[int], limit: float) -> int | None:
        best = None
        for i in among:
            d = math.dist(self.waypoints[i].position, point)
            if d <= limit and (best is None or (d, i) < best):
                best = (d, i)
        return best[1] if best else None


def _unit(v: tuple) -> tuple[float, float, float] | None:
    n = math.sqrt(sum(c * c for c in v))
    if n < 1e-6:
        return None
    return (v[0] / n, v[1] / n, v[2] / n)


def _toward(src: tuple, points: list[tuple]) -> tuple[float, float, float] | None:
    if not points:
        return None
    mean = tuple(sum(p[k] for p in points) / len(points) for k in range(3))
    return _unit((mean[0] - src[0], mean[1] - src[1], mean[2] - src[2]))


def _thin(items: list, limit: int) -> list:
    if len(items) <= limit:
        return items
    stride = len(items) / limit
    return [items[int(i * stride)] for i in range(limit)]


def _pick(ranked: list[tuple], count: int, separation: float, nav: Nav, taken: list) -> list:
    """The best `count` of `ranked` (score tuples ending with the waypoint), kept apart."""
    out = []
    for row in ranked:
        i = row[-1]
        p = nav.waypoints[i].position
        if any(math.dist(p, q) < separation for q in taken):
            continue
        out.append(row)
        taken.append(p)
        if len(out) == count:
            break
    return out


@dataclass
class Guard:
    """One part of one objective that a set of teams may defend."""

    objective: str
    kind: str
    group: str  # the part's role
    lo: tuple[float, float, float]  # the part's bounds (a point entity: lo == hi)
    hi: tuple[float, float, float]
    teams: list[str]
    initially: list[str]

    @property
    def point(self) -> tuple[float, float, float]:
        """The middle of its floor footprint."""
        return ((self.lo[0] + self.hi[0]) / 2, (self.lo[1] + self.hi[1]) / 2, self.lo[2])

    def distance(self, p: tuple[float, float, float]) -> float:
        """From a point to the part's box (0 inside it)."""
        return math.sqrt(sum(max(self.lo[k] - p[k], 0.0, p[k] - self.hi[k]) ** 2 for k in range(3)))


def _part_bounds(game_map: Any, entity: dict) -> tuple[tuple, tuple] | None:
    bounds = analysis._brush_bounds(game_map, entity)
    if bounds is not None:
        return tuple(bounds[0]), tuple(bounds[1])
    origin = dict((str(k).lower(), str(v)) for k, v in entity.get("keys") or []).get("origin")
    if origin:
        try:
            x, y, z = (float(v) for v in origin.split()[:3])
        except ValueError:
            return None
        return (x, y, z), (x, y, z)
    return None


def _teams_of(words: list, owner: str, real: list[str]) -> list[str]:
    out: list[str] = []
    for w in words or []:
        w = str(w)
        if w == "owner":
            got = [owner] if owner in real else list(real)
        elif w == "other":
            got = [t for t in real if t != owner] if owner in real else list(real)
        elif w in real:
            got = [w]
        else:
            raise BotGoalsError(f"unknown team word {w!r} in bots.script")
        out += [t for t in got if t not in out]
    return out


def guards(found: list, game_map: Any, script_spec: dict, real: list[str]) -> list[Guard]:
    kinds = script_spec.get("kinds") or {}
    out = []
    for f in found:
        rule = kinds.get(f.kind) or {}
        for g in rule.get("groups") or []:
            part = str(g["at"])
            ents = f.parts.get(part) or []
            bounds = _part_bounds(game_map, ents[0]) if ents else None
            if bounds is None:
                continue
            teams = _teams_of(g.get("teams") or [], f.team, real)
            initially = [t for t in _teams_of(g.get("initially") or [], f.team, real) if t in teams]
            out.append(Guard(f.name, f.kind, part, bounds[0], bounds[1], teams, initially))
    return out


def plan(
    nav: Nav,
    guard_list: list[Guard],
    goals_spec: dict,
    spots_spec: dict,
    real: list[str],
) -> tuple[list[Spot], list[dict]]:
    """Defend spots per guarded part and team, snipe spots per team. Returns the spots and
    one note per guard or team that got none, saying why."""
    types = goals_spec.get("types") or {}
    sep = float(spots_spec.get("separation", 192))
    max_cand = int(spots_spec.get("max_candidates", 400))
    max_route = int(spots_spec.get("max_route_points", 160))
    d = spots_spec.get("defend") or {}
    s = spots_spec.get("snipe") or {}
    spots: list[Spot] = []
    notes: list[dict] = []
    standing = {i for i, st in enumerate(nav.stance_of) if st == "stand"}
    lo, hi = float(d.get("min_distance", 96)), float(d.get("max_distance", 768))
    radius = float(d.get("approach_radius", 1536))
    names: set[str] = set()

    def unique(base: str) -> str:
        n = 1
        while f"{base}_{n}" in names:
            n += 1
        names.add(f"{base}_{n}")
        return f"{base}_{n}"

    def camp(gtype: str):
        return tuple((types.get(gtype) or {}).get("camp_time") or ()) or None

    # Every team's shortest routes from its spawns to every guarded part: the attackers'
    # routes in for a defend spot, the enemy's movement for a snipe spot.
    use: dict[tuple[int, str], int] = {}
    paths: dict[tuple[int, str], list[int]] = {}
    for n, g in enumerate(guard_list):
        origin = (g.point[0], g.point[1], g.point[2] + nav.floor_offset)
        for team in real:
            starts = nav.spawn_waypoints.get(team) or set()
            target = nav.nearest(origin, nav.reach.get(team, set()), hi)
            if target is None or not starts:
                continue
            path = nav.routes(starts, target)
            if path:
                use[(n, team)], paths[(n, team)] = target, path

    gtype = str(d.get("type", "DEFEND"))
    for n, g in enumerate(guard_list):
        origin = (g.point[0], g.point[1], g.point[2] + nav.floor_offset)
        for team in g.teams:
            label = f"{g.objective} {g.group} for {team}"
            attackers = [t for t in real if t != team and (n, t) in use]
            if not attackers:
                notes.append({"guard": label, "reason": "no other team reaches a waypoint near it"})
                continue
            # Where the attackers use it: their waypoint nearest the part.
            target = use[(n, attackers[0])]
            target_eye = nav.eye(target)
            route = [i for a in attackers for i in paths[(n, a)]]
            approach = _thin(
                [
                    i
                    for i in dict.fromkeys(route)
                    if math.dist(nav.waypoints[i].position, origin) <= radius
                ],
                max_route,
            )
            cands = _thin(
                [
                    i
                    for i in sorted(nav.reach.get(team, set()) & standing)
                    if lo <= g.distance(nav.waypoints[i].position) <= hi and i != target
                ],
                max_cand,
            )
            ranked = []
            for i in cands:
                eye = nav.eye(i)
                if not nav.sees(eye, target_eye):
                    continue
                seen = [p for p in approach if p != i and nav.sees(eye, nav.eye(p))]
                ranked.append(
                    (-len(seen), round(g.distance(nav.waypoints[i].position), 1), i, seen)
                )
            ranked.sort(key=lambda r: r[:3])
            chosen = _pick(
                [(r[0], r[1], r[3], r[2]) for r in ranked], int(d.get("per_group", 2)), sep, nav, []
            )
            if not chosen:
                notes.append(
                    {
                        "guard": label,
                        "reason": f"no standing waypoint {lo:g}-{hi:g} units from it that its "
                        "team reaches sees where the other team uses it",
                    }
                )
            for neg, dist, seen, i in chosen:
                eye = nav.eye(i)
                aim = _toward(eye, [nav.eye(p) for p in seen]) or _toward(eye, [target_eye])
                suffix = "" if g.group == "anchor" else f"_{g.group}"
                spots.append(
                    Spot(
                        type=gtype,
                        name=unique(sanitize(f"{gtype}_{g.objective}{suffix}_{team}")),
                        teams=[team],
                        position=nav.waypoints[i].position,
                        aims=[aim] if aim else [],
                        camp_time=camp(gtype),
                        objective=g.objective,
                        group=g.group,
                        initially=[team] if team in g.initially else [],
                        why={
                            "waypoint": i,
                            "distance": dist,
                            "watches": target,
                            "route_points_seen": -neg,
                            "route_points_near": len(approach),
                        },
                    )
                )

    # Snipe: per team, over the enemy's routes to every guarded part, from its own side.
    stype = str(s.get("type", "SNIPE"))
    standoff = float(s.get("standoff", 384))
    near, far = float(s.get("min_sight", 768)), float(s.get("max_sight", 4096))
    need = int(s.get("min_points", 3))
    for team in real:
        enemy = [t for t in real if t != team]
        points = _thin(
            sorted({i for (n, t), path in paths.items() if t in enemy for i in path}), max_route
        )
        if not points:
            notes.append(
                {
                    "guard": f"snipe for {team}",
                    "reason": "the enemy has no route to any guarded part",
                }
            )
            continue
        own = nav.spawn_waypoints.get(team) or set()
        theirs = set().union(*(nav.spawn_waypoints.get(t) or set() for t in enemy))

        def side(i: int, own=own, theirs=theirs) -> bool:
            # Nearer its own team's spawns than the enemy's, in a straight line.
            p = nav.waypoints[i].position
            mine = min((math.dist(p, nav.waypoints[w].position) for w in own), default=math.inf)
            other = min((math.dist(p, nav.waypoints[w].position) for w in theirs), default=math.inf)
            return mine < other

        cands = _thin(
            [
                i
                for i in sorted(nav.reach.get(team, set()) & standing)
                if side(i)
                and all(
                    math.dist(nav.waypoints[i].position, nav.waypoints[p].position) >= standoff
                    for p in points
                )
            ],
            max_cand,
        )
        ranked = []
        for i in cands:
            eye = nav.eye(i)
            seen = [
                p
                for p in points
                if near <= math.dist(nav.waypoints[i].position, nav.waypoints[p].position) <= far
                and nav.sees(eye, nav.eye(p))
            ]
            if len(seen) >= need:
                longest = max(
                    math.dist(nav.waypoints[i].position, nav.waypoints[p].position) for p in seen
                )
                ranked.append((-len(seen), -round(longest, 1), i, seen))
        ranked.sort(key=lambda r: r[:3])
        chosen = _pick(
            [(r[0], r[1], r[3], r[2]) for r in ranked], int(s.get("per_team", 2)), sep, nav, []
        )
        if not chosen:
            notes.append(
                {
                    "guard": f"snipe for {team}",
                    "reason": f"no standing waypoint on its side, {standoff:g}+ units off the enemy "
                    f"routes, sees {need} of their waypoints {near:g}-{far:g} units away",
                }
            )
        for neg, longest, seen, i in chosen:
            eye = nav.eye(i)
            aim = _toward(eye, [nav.eye(p) for p in seen])
            spots.append(
                Spot(
                    type=stype,
                    name=unique(sanitize(f"{stype}_{team}")),
                    teams=[team],
                    position=nav.waypoints[i].position,
                    aims=[aim] if aim else [],
                    camp_time=camp(stype),
                    initially=[team],
                    why={
                        "waypoint": i,
                        "route_points_seen": -neg,
                        "route_points": len(points),
                        "longest_sight": -longest,
                    },
                )
            )
    return spots, notes


def caller_spots(
    given: list[dict],
    nav: Nav,
    grids: dict[str, analysis.NavGrid],
    goals_spec: dict,
    guard_list: list[Guard],
    real: list[str],
    taken: set[str],
) -> tuple[list[Spot], list[str]]:
    """The caller's own spots, snapped to the nearest walkable floor. Returns them and warnings."""
    types = goals_spec.get("types") or {}
    stances = [str(s) for s in goals_spec.get("stances") or ["stand"]]
    out: list[Spot] = []
    warnings: list[str] = []
    for n, raw in enumerate(given or []):
        where = f"spots[{n}]"
        if not isinstance(raw, dict):
            raise BotGoalsError(f"{where} must be an object")
        unknown = set(raw) - {
            "type",
            "team",
            "position",
            "aim_at",
            "yaw",
            "name",
            "objective",
            "stance",
            "camp_time",
        }
        if unknown:
            raise BotGoalsError(f"{where} has unknown keys {sorted(unknown)}")
        gtype = str(raw.get("type", "")).upper()
        if gtype not in types:
            raise BotGoalsError(
                f"{where}.type must be one of {sorted(types)}, got {raw.get('type')!r}"
            )
        team = str(raw.get("team", "")).lower()
        teams = list(real) if team in ("both", "all") else [team]
        if not set(teams) <= set(real):
            raise BotGoalsError(
                f"{where}.team must be one of {[*real, 'both']}, got {raw.get('team')!r}"
            )
        pos = raw.get("position")
        if not (
            isinstance(pos, (list, tuple))
            and len(pos) == 3
            and all(isinstance(v, (int, float)) for v in pos)
        ):
            raise BotGoalsError(f"{where}.position must be [x, y, z]")
        stance = str(raw.get("stance", "stand")).lower()
        if stance not in stances or (stance != "stand" and not (types[gtype] or {}).get("stance")):
            raise BotGoalsError(f"{where}.stance must be one of {stances} (only stand for {gtype})")
        hit = None
        for st in (stance, "stand", "crouch", "prone"):
            g = grids.get(st)
            if g is not None:
                hit = g.nearest_walkable(tuple(float(v) for v in pos), radius=g.cell * 8)
                if hit is not None:
                    grid = g
                    break
        if hit is None:
            raise BotGoalsError(f"{where}.position {list(pos)} has no walkable floor within reach")
        x, y, z = grid.centre(hit[0])
        origin = (x, y, z + nav.floor_offset)
        if "aim_at" in raw and "yaw" in raw:
            raise BotGoalsError(f"{where}: give aim_at or yaw, not both")
        eye = (x, y, z + nav.eye_height)
        if "aim_at" in raw:
            at = raw["aim_at"]
            if not (isinstance(at, (list, tuple)) and len(at) == 3):
                raise BotGoalsError(f"{where}.aim_at must be [x, y, z]")
            aim = _unit((at[0] - eye[0], at[1] - eye[1], at[2] - eye[2]))
        elif "yaw" in raw:
            yaw = math.radians(float(raw["yaw"]))
            aim = (math.cos(yaw), math.sin(yaw), 0.0)
        else:
            raise BotGoalsError(f"{where} needs aim_at [x, y, z] or yaw (degrees) to face")
        if aim is None:
            raise BotGoalsError(f"{where}.aim_at is the spot itself")
        camp = raw.get("camp_time")
        if camp is not None:
            if not (
                isinstance(camp, (list, tuple))
                and len(camp) == 2
                and 0 <= camp[0] <= camp[1] <= 9999
            ):
                raise BotGoalsError(
                    f"{where}.camp_time must be [min, max] seconds, 0 <= min <= max <= 9999"
                )
            camp = (float(camp[0]), float(camp[1]))
        else:
            camp = tuple((types[gtype] or {}).get("camp_time") or ()) or None
        objective = raw.get("objective")
        group = None
        initially = list(teams)
        if objective is not None:
            match = [g for g in guard_list if g.objective.casefold() == str(objective).casefold()]
            if not match:
                raise BotGoalsError(
                    f"{where}.objective {objective!r} is none of {sorted({g.objective for g in guard_list})}"
                )
            objective, group = match[0].objective, match[0].group
            initially = [t for t in teams if t in match[0].initially]
        tag = sanitize(str(raw.get("name") or f"{objective or 'spot'}_{team}"))
        base = f"{gtype}_{tag}"
        name, k = f"{base}_1", 1
        while name in taken:
            k += 1
            name = f"{base}_{k}"
        taken.add(name)
        near = nav.nearest(origin, set(range(len(nav.waypoints))), 256)
        if hit[1] > 64:
            warnings.append(f"{name}: moved {hit[1]:.0f} units to the nearest walkable floor")
        if near is None:
            warnings.append(f"{name}: no waypoint within 256 units; bots may not find a way to it")
        elif not any(near in nav.reach.get(t, set()) for t in teams):
            warnings.append(f"{name}: its nearest waypoint is not reached from its team's spawns")
        out.append(
            Spot(
                type=gtype,
                name=name,
                teams=teams,
                position=origin,
                aims=[aim],
                stance=stance,
                camp_time=camp,
                objective=objective,
                group=group,
                initially=initially,
                source="caller",
                why={
                    "snapped": round(hit[1], 1),
                    **({"waypoint": near} if near is not None else {}),
                },
            )
        )
    return out, warnings


# --------------------------------------------------------------------------------------
# The map script's events
# --------------------------------------------------------------------------------------


@dataclass
class Hook:
    objective: str
    kind: str
    event: str
    trigger: str | None
    source: str
    on: list[tuple[str, str]] = field(default_factory=list)  # (group, team)
    off: list[tuple[str, str]] = field(default_factory=list)
    reason: str | None = None

    def row(self, spots: list[Spot]) -> dict[str, Any]:
        def names(pairs):
            return sorted(
                {
                    s.name
                    for s in spots
                    for g, t in pairs
                    if s.objective == self.objective and s.group == g and t in s.teams
                }
            )

        return {
            "objective": self.objective,
            "kind": self.kind,
            "event": self.event,
            "trigger": self.trigger,
            "source": self.source,
            **({"on": names(self.on)} if self.on else {}),
            **({"off": names(self.off)} if self.off else {}),
            **({"not_hooked": self.reason} if self.reason else {}),
        }


def _announce(
    script: objtests.Script, block: str, event: str, params: list, rule: dict, call: dict
):
    """The first announcement an event makes, following script calls."""
    selfs = {s.casefold() for s in call.get("self_names") or []}
    todo = [(block, event, list(params))]
    seen = set()
    while todo:
        b, e, p = todo.pop(0)
        key = (b.casefold(), e.casefold(), tuple(x.casefold() for x in p))
        if key in seen:
            continue
        seen.add(key)
        ev = script.event(b, e, p)
        if ev is None:
            continue
        for action in ev.actions:
            if action.name == str(rule["action"]).casefold():
                arg = int(rule.get("arg", 0))
                if arg < len(action.args):
                    return action.args[arg]
            elif call and action.name == str(call.get("action")).casefold():
                bi, li = int(call.get("block_arg", 0)), int(call.get("label_arg", 1))
                if len(action.args) > max(bi, li):
                    target = b if action.args[bi].casefold() in selfs else action.args[bi]
                    todo.append((target, str(call.get("event")), [action.args[li]]))
    return None


def hooks(
    found: list, script_text: str, profile_id: str, script_spec: dict, real: list[str]
) -> list[Hook]:
    prof = profiles.load(profile_id)
    meta = (prof.get("objectives") or {}).get("playtest") or {}
    script = objtests.Script(script_text, prof.get("mapscript") or {}, meta)
    call = (meta.get("script") or {}).get("call") or {}
    rule = script_spec.get("announce") or {}
    limit = int(script_spec.get("trigger_bytes", 71))
    out = []
    for f in found:
        kind = (script_spec.get("kinds") or {}).get(f.kind) or {}
        group_teams = {
            str(g["at"]): _teams_of(g.get("teams") or [], f.team, real)
            for g in kind.get("groups") or []
        }

        def pairs(entries, group_teams=group_teams, f=f):
            got = []
            for e in entries or []:
                teams = group_teams.get(str(e["group"]), [])
                want = (
                    teams
                    if e.get("teams") == "all"
                    else _teams_of(e.get("teams") or [], f.team, real)
                )
                got += [(str(e["group"]), t) for t in want if t in teams]
            return got

        for ev in kind.get("events") or []:
            hook = Hook(
                f.name,
                f.kind,
                str(ev["name"]),
                None,
                "",
                pairs(ev.get("enable")),
                pairs(ev.get("disable")),
            )
            try:
                if "trigger" in ev:
                    hook.source = "game"
                    hook.trigger = str(
                        objtests.fill(ev["trigger"], f.subs | {"name": f.name}, f.parts)
                    )
                else:
                    a = objtests.fill(ev["announce"], f.subs | {"name": f.name}, f.parts)
                    hook.source = f"map script: {a['block']} {a['event']} {' '.join(a.get('params') or [])}".strip()
                    if not script_text:
                        hook.reason = "the map has no script"
                    else:
                        text = _announce(
                            script,
                            str(a["block"]),
                            str(a["event"]),
                            list(a.get("params") or []),
                            rule,
                            call,
                        )
                        if text is None:
                            hook.reason = f"that event makes no {rule.get('action')}"
                        hook.trigger = text
            except objtests.Untestable as e:
                hook.reason = str(e)
            if hook.trigger is not None:
                raw = hook.trigger.encode("latin-1", "replace")
                if len(raw) > limit:
                    hook.trigger = raw[:limit].decode("latin-1")
            out.append(hook)
    return out


# --------------------------------------------------------------------------------------
# Writing the two scripts
# --------------------------------------------------------------------------------------


def _gm_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _num(v: float) -> str:
    return f"{v:.3f}"


def goals_text(spots: list[Spot], goals_spec: dict, header: str) -> str:
    types = goals_spec.get("types") or {}
    teams = goals_spec.get("teams") or {}
    vec = str(goals_spec["vector"])
    lines = [f"// {header}", f"global {goals_spec['table']} =", "{"]
    for s in sorted(spots, key=lambda s: s.name):
        t = types[s.type] or {}
        lines.append(f"\t{s.name} =")
        lines.append("\t{")
        props: list[tuple[str, list[str]]] = []
        if s.aims:
            props.append(
                (
                    str(t.get("aim", "AimVectors")),
                    ["\t\t{"]
                    + [f"\t\t\t{vec}({', '.join(_num(c) for c in a)})," for a in s.aims]
                    + ["\t\t},"],
                )
            )
        if s.camp_time and "camp_time" in t:
            props.append(("MaxCampTime", [f" {s.camp_time[1]:g},"]))
            props.append(("MinCampTime", [f" {s.camp_time[0]:g},"]))
        props.append(("Position", [f" {vec}({', '.join(_num(c) for c in s.position)}),"]))
        if t.get("stance") and s.stance != "stand":
            props.append(("Stance", [f" {_gm_string(s.stance)},"]))
        bits = sum(int(teams[x]["bit"]) for x in s.teams)
        props.append(("TeamAvailability", [f" {bits},"]))
        for key, body in sorted(props):
            if body[0].startswith("\t"):
                lines.append(f"\t\t{key} =")
                lines += body
            else:
                lines.append(f"\t\t{key} ={body[0]}")
        lines.append("\t},")
    lines.append(f"\tVersion = {int(goals_spec['version'])},")
    lines.append("};")
    return "\n".join(lines) + "\n"


def _ident(text: str) -> str:
    out = sanitize(text)
    return out if out and not out[0].isdigit() else f"o_{out}"


def script_text_for(
    hook_list: list[Hook],
    spots: list[Spot],
    goals_spec: dict,
    script_spec: dict,
    marker: str,
    header: str,
) -> str:
    """The map script: one function per hooked event in the map table, registered for its
    trigger at map load, where spots that start off are switched off."""
    teams = goals_spec.get("teams") or {}
    table, load = str(script_spec["map_table"]), str(script_spec["on_load"])
    on_trigger, available = str(script_spec["on_trigger"]), str(script_spec["set_available"])
    debug = str(script_spec["debug_print"])

    def switch(hook: Hook, pairs, on: bool) -> list[str]:
        by_team: dict[str, list[str]] = {}
        for group, team in pairs:
            for s in spots:
                if s.group == group and team in s.teams and s.objective == hook.objective:
                    by_team.setdefault(team, [])
                    if s.name not in by_team[team]:
                        by_team[team].append(s.name)
        return [
            f"\t\t{available}( {teams[team]['script']}, {'true' if on else 'false'}, "
            f"{{ {', '.join(_gm_string(n) for n in sorted(by_team[team]))} }} );"
            for team in sorted(by_team)
        ]

    live = [h for h in hook_list if h.trigger is not None]
    lines = [f"// {header}", f"global {table} =", "{", "\tDebug = 0,"]
    funcs: list[str] = []
    for hook in live:
        name = _ident(f"{hook.objective}_{hook.event}")
        while name in funcs or name == "Debug":
            name += "_"
        funcs.append(name)
        lines += ["", f"\t{name} = function( trigger )", "\t{"]
        lines += switch(hook, hook.on, True) + switch(hook, hook.off, False)
        lines.append(f"\t\t{debug}( {_gm_string(f'{marker}{hook.objective} {hook.event}')} );")
        lines.append("\t},")
    lines += ["};", "", f"global {load} = function()", "{"]
    for name, hook in zip(funcs, live, strict=True):
        lines.append(f"\t{on_trigger}( {_gm_string(hook.trigger)}, {table}.{name} );")
    off: dict[str, list[str]] = {}
    for s in spots:
        for team in s.teams:
            if team not in s.initially:
                off.setdefault(team, []).append(s.name)
    for team in sorted(off):
        names = ", ".join(_gm_string(x) for x in sorted(off[team]))
        lines.append(f"\t{available}( {teams[team]['script']}, false, {{ {names} }} );")
    summary = f"{marker}{load} {len(spots)} spots, {len(live)} triggers"
    lines += [f"\t{debug}( {_gm_string(summary)} );", "};"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------------------
# Reading them back (the subset of GameMonkey both files use)
# --------------------------------------------------------------------------------------

_TOKEN = re.compile(
    r'\s+|//[^\n]*|/\*.*?\*/|(?P<str>"(?:\\.|[^"\\])*")|(?P<num>-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)'
    r"|(?P<id>[A-Za-z_][A-Za-z0-9_]*)|(?P<op>[{}()=,;.\[\]<>+*/!&|-])",
    re.S,
)


def tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise BotGoalsError(f"unexpected character {text[pos]!r} at byte {pos}")
        pos = m.end()
        for kind in ("str", "num", "id", "op"):
            if m.group(kind) is not None:
                out.append((kind, m.group(kind)))
    return out


class _Parser:
    def __init__(self, toks, vectors: tuple[str, ...]):
        self.t, self.i, self.vectors = toks, 0, vectors

    def peek(self, k=0):
        return self.t[self.i + k] if self.i + k < len(self.t) else ("eof", "")

    def take(self, value=None):
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise BotGoalsError(f"expected {value!r}, got {tok[1]!r} (token {self.i})")
        self.i += 1
        return tok

    def value(self):
        kind, v = self.peek()
        if kind == "str":
            self.take()
            return bytes(v[1:-1], "latin-1").decode("unicode_escape")
        if kind == "num":
            self.take()
            return float(v) if any(c in v for c in ".eE") else int(v)
        if v == "{":
            return self.table()
        if kind == "id" and v in self.vectors:
            self.take()
            self.take("(")
            args = [self.value()]
            while self.peek()[1] == ",":
                self.take(",")
                args.append(self.value())
            self.take(")")
            if len(args) != 3 or not all(isinstance(a, (int, float)) for a in args):
                raise BotGoalsError(f"{v} takes three numbers")
            return tuple(float(a) for a in args)
        if kind == "id" and v in ("true", "false", "null"):
            self.take()
            return {"true": True, "false": False, "null": None}[v]
        raise BotGoalsError(f"unsupported value {v!r} (token {self.i})")

    def table(self):
        self.take("{")
        out: dict = {}
        n = 0
        while self.peek()[1] != "}":
            if self.peek()[0] in ("id", "str") and self.peek(1)[1] == "=":
                key = self.take()[1]
                key = key[1:-1] if key.startswith('"') else key
                self.take("=")
                out[key] = self.value()
            elif self.peek()[1] == "[":
                self.take("[")
                key = self.value()
                self.take("]")
                self.take("=")
                out[key] = self.value()
            else:
                out[n] = self.value()
                n += 1
            if self.peek()[1] in (",", ";"):
                self.take()
            elif self.peek()[1] != "}":
                raise BotGoalsError(
                    f"expected ',' or '}}', got {self.peek()[1]!r} (token {self.i})"
                )
        self.take("}")
        return out


def read_goals(text: str, goals_spec: dict) -> dict:
    """The goal table a goals script sets, for the literal subset goal files use (numbers,
    strings, booleans, vectors and nested tables, keyed by name or [value])."""
    table = str(goals_spec["table"])
    p = _Parser(tokens(text), tuple(str(v) for v in goals_spec["vector_names"]))
    p.take("global")
    if p.take()[1] != table:
        raise BotGoalsError(f"the script does not set {table}")
    p.take("=")
    out = p.table()
    if p.peek()[1] == ";":
        p.take()
    if p.peek()[0] != "eof":
        raise BotGoalsError(f"trailing tokens after the table: {p.peek()[1]!r}")
    return out


def _str(tok: tuple[str, str]) -> str:
    return bytes(tok[1][1:-1], "latin-1").decode("unicode_escape")


def read_script(text: str, script_spec: dict) -> dict:
    """What a map script defines and registers, for the statements the written scripts use:
    its globals, its functions (those of the map table and the map-load function) with the
    goals each one switches (team expression, on, names) and the messages it prints, and the
    trigger registrations with the function each calls. Brackets must pair up and every
    registration must name a defined function. The statement names are the profile's
    (`bots.script`)."""
    table, on_trigger = str(script_spec["map_table"]), str(script_spec["on_trigger"])
    available = str(script_spec["set_available"])
    debug = str(script_spec["debug_print"]).split(".")[-1]
    toks = tokens(text)
    stack: list[str] = []
    pairs = {"}": "{", ")": "(", "]": "["}
    for _kind, v in toks:
        if v in "{([":
            stack.append(v)
        elif v in pairs and (not stack or stack.pop() != pairs[v]):
            raise BotGoalsError(f"unbalanced {v!r}")
    if stack:
        raise BotGoalsError(f"unclosed {stack[-1]!r}")
    out: dict[str, Any] = {"globals": [], "functions": {}, "triggers": []}
    depth = 0
    current: list[tuple[str, int]] = []  # (function, brace depth of its body)
    i = 0
    while i < len(toks):
        kind, v = toks[i]
        if v == "global" and i + 1 < len(toks):
            out["globals"].append(toks[i + 1][1])
        if (
            kind == "id"
            and i + 3 < len(toks)
            and toks[i + 1][1] == "="
            and toks[i + 2][1] == "function"
        ):
            # name = function( args ) { body }
            j = i + 3
            while toks[j][1] != "{":
                j += 1
            out["functions"][v] = {"switches": [], "messages": []}
            current.append((v, depth + 1))
        elif v == "{":
            depth += 1
        elif v == "}":
            if current and current[-1][1] == depth:
                current.pop()
            depth -= 1
        elif v == on_trigger and toks[i + 1][1] == "(":
            if toks[i + 2][0] != "str" or [t[1] for t in toks[i + 3 : i + 6]] != [",", table, "."]:
                raise BotGoalsError(f"{on_trigger} takes a name and a {table} function")
            out["triggers"].append((_str(toks[i + 2]), toks[i + 6][1]))
        elif v == available and current:
            j = i + 2
            team = []
            while toks[j][1] != ",":
                team.append(toks[j][1])
                j += 1
            on = toks[j + 1][1]
            if toks[j + 2][1] != "," or toks[j + 3][1] != "{":
                raise BotGoalsError(
                    f"{available} takes a team, true/false and a name table"
                )
            names = []
            k = j + 4
            while toks[k][1] != "}":
                if toks[k][0] == "str":
                    names.append(_str(toks[k]))
                k += 1
            out["functions"][current[-1][0]]["switches"].append(
                ("".join(team), on == "true", names)
            )
        elif v == debug and current and toks[i + 2][0] == "str":
            out["functions"][current[-1][0]]["messages"].append(_str(toks[i + 2]))
        i += 1
    missing = [f for _t, f in out["triggers"] if f not in out["functions"]]
    if missing:
        raise BotGoalsError(f"{on_trigger} names functions the script does not define: {missing}")
    return out


# --------------------------------------------------------------------------------------
# A whole map
# --------------------------------------------------------------------------------------


@dataclass
class Made:
    goals: str
    script: str
    spots: list[Spot]
    hooks: list[Hook]
    report: dict[str, Any]


def make(
    game_map: Any,
    profile_id: str,
    bots: dict[str, Any],
    waypoints: list,
    extras: dict[str, Any],
    *,
    script_text: str | None,
    given: list[dict] | None = None,
    auto: bool = True,
    header: str = "",
) -> Made:
    """Spots (chosen and given), the hooks for the map's objectives, and both scripts, each
    read back before it is returned."""
    goals_spec, script_spec, spots_spec = spec(bots)
    teams = goals_spec.get("teams") or {}
    real = list(teams)
    side_to_team = {str(v.get("side")): t for t, v in teams.items()}
    grids = extras["closed"] or extras["grids"]
    grid = grids.get("stand") or next(iter(grids.values()))
    nav = Nav(
        waypoints=waypoints,
        stance_of=list(extras["stance_of"]),
        spawn_waypoints={
            side_to_team[s]: set(w)
            for s, w in (extras["spawn_waypoints"] or {}).items()
            if s in side_to_team
        },
        solids=grid.solids,
        index=grid.index,
        floor_offset=float(bots["floor_offset"]),
        eye_height=float(grid.movement.eye_height.value),
        grid=grid,
    )
    found, _left = objtests.recognise(profile_id, game_map)
    guard_list = guards(found, game_map, script_spec, real)
    spots: list[Spot] = []
    notes: list[dict] = []
    if auto:
        spots, notes = plan(nav, guard_list, goals_spec, spots_spec, real)
    mine, warnings = caller_spots(
        given or [], nav, extras["grids"], goals_spec, guard_list, real, {s.name for s in spots}
    )
    spots += mine
    hook_list = hooks(found, script_text or "", profile_id, script_spec, real)
    marker = str((bots.get("playtest") or {}).get("script_marker", ""))
    goals = goals_text(spots, goals_spec, header)
    script = script_text_for(hook_list, spots, goals_spec, script_spec, marker, header)
    # Read both back: the table must hold exactly these goals, and the script must switch
    # only goals the table has and register every hooked trigger.
    table = read_goals(goals, goals_spec)
    if table.pop("Version", None) != int(goals_spec["version"]):
        raise BotGoalsError("the written goal table does not read back with its Version")
    if sorted(table) != sorted(s.name for s in spots):
        raise BotGoalsError("the written goal table does not read back to the same goals")
    parsed = read_script(script, script_spec)
    switched = {
        n for f in parsed["functions"].values() for _t, _on, names in f["switches"] for n in names
    }
    if not switched <= set(table):
        raise BotGoalsError(
            f"the script switches goals the table lacks: {sorted(switched - set(table))}"
        )
    if len(parsed["triggers"]) != sum(1 for h in hook_list if h.trigger is not None):
        raise BotGoalsError("the written script does not register every hooked trigger")
    unhooked = [h.row(spots) for h in hook_list if h.trigger is None]
    for row in unhooked:
        warnings.append(f"{row['objective']} {row['event']} is not hooked: {row['not_hooked']}")
    report = {
        "spots": [s.row() for s in sorted(spots, key=lambda s: s.name)],
        "by_type": {
            t: sum(1 for s in spots if s.type == t) for t in sorted({s.type for s in spots})
        },
        "objectives": [{"objective": f.name, "kind": f.kind, "team": f.team} for f in found],
        "triggers": [h.row(spots) for h in hook_list if h.trigger is not None],
        "not_hooked": unhooked,
        "no_spots": notes,
        "warnings": warnings,
    }
    return Made(goals, script, spots, hook_list, report)
