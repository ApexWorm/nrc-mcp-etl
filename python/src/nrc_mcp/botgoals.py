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
# GameMonkey: reading the add-on's scripts, mirroring them, writing them back
# --------------------------------------------------------------------------------------
#
# Both files are GameMonkey, the add-on's script language, read here with the language's own
# grammar: the scanner and parser of gmscriptex, the GameMonkey build Omni-bot 0.93 embeds
# (github.com/jswigart/gmscriptex at 2d24f6e, gmsrc_ex/src/gm/gmScanner.l and gmParser.y;
# the Omni-bot tree vendored with ET: Legacy carries only the game interface headers). The
# reader is lossless: every byte of a file is in exactly one token, comments and whitespace
# included, so the tokens joined give the file back byte for byte, and a rewrite (a mirror)
# touches only the tokens it changes.

# gmScanner.l:48-72; "&&" and "||" scan as `and` and `or` (gmScanner.l:86-87).
_GM_KEYWORDS = frozenset(
    [
        "local",
        "global",
        "member",
        "and",
        "or",
        "if",
        "else",
        "while",
        "for",
        "foreach",
        "in",
        "dowhile",
        "break",
        "continue",
        "null",
        "return",
        "function",
        "table",
        "this",
        "true",
        "false",
        "fork",
        "switch",
        "case",
        "default",
    ]
)

# One token per match, in gmScanner.l's terms: whitespace (:131), comments (:31-46; an
# unclosed block comment runs to the end of the file), strings in double quotes with
# backslash escapes or in backquotes with `` for a backquote (:83-84; flex's `.` stops at a
# newline), character constants (:79), numbers (:76-82, longest match first), identifiers
# and keywords (:48-74) and operators (:86-129, longest first). Anything else is the
# scanner's TOKEN_ERROR (:132).
_GM_LEX = re.compile(
    r"(?P<ws>[ \t\v\r\n\f]+)"
    r"|(?P<comment>/\*.*?(?:\*/|\Z)|//[^\n]*)"
    r'|(?P<str>"(?:\\[^\n]|[^\\"])*"|`(?:[^`]|``)*`)'
    r"|(?P<char>'(?:\\[^\n]|[^\\'])+')"
    r"|(?P<num>0[xX][0-9a-fA-F]+|0[bB][01]+"
    r"|\d+[eE][-+]?\d+[fF]?|\d*\.\d+(?:[eE][-+]?\d+)?[fF]?|\d+\.\d*(?:[eE][-+]?\d+)?[fF]?|\d+)"
    r"|(?P<id>[A-Za-z_][A-Za-z0-9_]*)"
    r"|(?P<op>>>=|<<=|&&|\|\||[-+*/%&|^]=|>>|<<|\+\+|--|<=|>=|==|!=|[;{},=()\[\].!\-+*/%<>&|^~:])",
    re.S,
)

_GM_TRIVIA = ("ws", "comment")

# Binary operators by precedence, loosest first, all left-associative
# (gmParser.y:467-617, logical_or_expression down to multiplicative_expression).
_GM_BINARY = {
    "or": 1,
    "||": 1,
    "and": 2,
    "&&": 2,
    "|": 3,
    "^": 4,
    "&": 5,
    "==": 6,
    "!=": 6,
    "<": 7,
    ">": 7,
    "<=": 7,
    ">=": 7,
    "<<": 8,
    ">>": 8,
    "+": 9,
    "-": 9,
    "*": 10,
    "/": 10,
    "%": 10,
}
_GM_UNARY = frozenset(("++", "--", "+", "-", "~", "!"))  # gmParser.y:619-657
_GM_ASSIGN = frozenset(("=", ">>=", "<<=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^="))
# Nodes a postfix_expression can be: the left side of an assignment (gmParser.y:393-447).
_GM_POSTFIX = frozenset(
    ("id", "dotid", "this", "num", "char", "str", "bool", "null", "table", "func", "paren")
    + ("index", "call", "method", "dot")
)


def gm_lex(text: str) -> list[tuple[str, str, int]]:
    """Every token of a GameMonkey text as (kind, text, offset), whitespace and comments
    included: the texts joined are the input."""
    out: list[tuple[str, str, int]] = []
    pos = 0
    keywords = _GM_KEYWORDS
    for m in _GM_LEX.finditer(text):
        at = m.start()
        if at != pos:
            break
        kind, v = m.lastgroup, m.group()
        if kind == "id" and v in keywords:
            kind = "kw"
        out.append((kind, v, at))
        pos = m.end()
    if pos < len(text):
        line = text.count("\n", 0, pos) + 1
        raise BotGoalsError(f"line {line}: unexpected character {text[pos]!r} at byte {pos}")
    return out


def gm_string(token: str) -> str:
    """The value of a string token: backslash escapes (decimal \\ddd included) in double
    quotes, `` in backquotes (gmCodeTree.cpp:473-547, gmProcess*QuoteString)."""
    if token.startswith("`"):
        return token[1:-1].replace("``", "`")
    body, out, i = token[1:-1], [], 0
    if "\\" not in body:
        return body
    esc = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            d = body[i + 1]
            if d.isdigit():
                j = i + 1
                while j < len(body) and j < i + 4 and body[j].isdigit():
                    j += 1
                out.append(chr(int(body[i + 1 : j]) & 0xFF))
                i = j
                continue
            out.append(esc.get(d, d))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


class GmNode:
    """A parse tree node over the significant tokens [lo, hi)."""

    __slots__ = ("kind", "lo", "hi", "kids", "value")

    def __init__(self, kind: str, lo: int, hi: int, kids: list | None = None, value: Any = None):
        self.kind, self.lo, self.hi, self.kids, self.value = kind, lo, hi, kids or [], value

    def __repr__(self) -> str:
        return f"GmNode({self.kind}, {self.value!r}, {len(self.kids)} kids)"


class GmScript:
    """A parsed GameMonkey text. `tokens` hold every byte; the tree indexes the significant
    tokens (not whitespace or comments)."""

    def __init__(self, text: str):
        self.text = text
        self.tokens = gm_lex(text)
        self.sig = [i for i, t in enumerate(self.tokens) if t[0] not in _GM_TRIVIA]
        self.k = [self.tokens[i][0] for i in self.sig] + ["eof"]
        self.v = [self.tokens[i][1] for i in self.sig] + [""]
        self.p = 0
        self.program = self._program()

    # -- positions -----------------------------------------------------------------------

    def offset(self, at: int) -> int:
        if at >= len(self.sig):
            return len(self.text)
        return self.tokens[self.sig[at]][2]

    def line(self, at: int) -> int:
        return self.text.count("\n", 0, self.offset(at)) + 1

    def span(self, node: GmNode) -> tuple[int, int]:
        """The node's byte range, without the whitespace and comments around it."""
        last = self.tokens[self.sig[node.hi - 1]]
        return self.offset(node.lo), last[2] + len(last[1])

    def source(self, node: GmNode) -> str:
        a, b = self.span(node)
        return self.text[a:b]

    def compact(self, node: GmNode) -> str:
        """The node's tokens without whitespace or comments, e.g. `Util.MapDebugPrint`."""
        return "".join(self.v[node.lo : node.hi])

    def emit(self) -> str:
        return "".join(t[1] for t in self.tokens)

    def walk(self):
        """Every node with the name of the innermost named function it is in (None outside
        any): a function is named by the table field, variable or assignment that holds it."""
        stack: list[tuple[GmNode, str | None]] = [(self.program, None)]
        while stack:
            node, fn = stack.pop()
            if node.kind == "func" and node.value:
                fn = node.value
            yield node, fn
            stack.extend((k, fn) for k in reversed(node.kids) if k is not None)

    # -- the parser (gmParser.y) ---------------------------------------------------------

    def _fail(self, want: str):
        got = self.v[self.p] or "the end of the file"
        raise BotGoalsError(f"line {self.line(self.p)}: expected {want}, got {got!r}")

    def _take(self, value: str) -> int:
        if self.v[self.p] != value or self.k[self.p] not in ("op", "kw"):
            self._fail(repr(value))
        self.p += 1
        return self.p - 1

    def _is(self, value: str) -> bool:
        return self.v[self.p] == value and self.k[self.p] in ("op", "kw")

    def _ident(self) -> str:
        if self.k[self.p] != "id":
            self._fail("a name")
        self.p += 1
        return self.v[self.p - 1]

    def _program(self) -> GmNode:
        # program: statement_list (gmParser.y:127-143). A file of only comments is taken as
        # empty rather than refused.
        kids = []
        while self.k[self.p] != "eof":
            kids.append(self._statement())
        return GmNode("program", 0, self.p, [k for k in kids if k is not None])

    def _compound(self) -> GmNode:
        lo = self._take("{")  # gmParser.y:168-178
        kids = []
        while not self._is("}"):
            if self.k[self.p] == "eof":
                self._fail("'}'")
            kids.append(self._statement())
        self.p += 1
        return GmNode("block", lo, self.p, [k for k in kids if k is not None])

    def _paren_expr(self) -> GmNode:
        self._take("(")
        e = self._expr()
        self._take(")")
        return e

    @staticmethod
    def _name_function(value: GmNode | None, name: str | None):
        if value is not None and value.kind == "func" and name and not value.value:
            value.value = name

    def _statement(self) -> GmNode | None:  # noqa: C901 - one branch per grammar rule
        lo, v, k = self.p, self.v[self.p], self.k[self.p]
        if k == "op" and v == ";":  # expression_statement ';' (gmParser.y:208-217)
            self.p += 1
            return None
        if k == "kw":
            if v in ("local", "global", "member"):  # var_statement (gmParser.y:180-206)
                self.p += 1
                name = self._ident()
                kids = []
                if self._is("="):
                    self.p += 1
                    kids.append(self._expr())
                    self._name_function(kids[0], name)
                self._take(";")
                return GmNode("var", lo, self.p, kids, (v, name))
            if v == "if":  # selection_statement (gmParser.y:219-257)
                self.p += 1
                kids = [self._paren_expr(), self._compound()]
                if self._is("else"):
                    self.p += 1
                    kids.append(self._statement() if self._is("if") else self._compound())
                return GmNode("if", lo, self.p, kids)
            if v == "fork":
                self.p += 1
                name = self._ident() if self.k[self.p] == "id" else None
                return GmNode("fork", lo, self.p, [self._compound()], name)
            if v == "switch":  # with case_selection_statement (gmParser.y:259-326)
                self.p += 1
                kids = [self._paren_expr()]
                self._take("{")
                while not self._is("}"):
                    kids.append(self._case())
                if len(kids) == 1:
                    self._fail("'case' or 'default'")
                self.p += 1
                return GmNode("switch", lo, self.p, kids)
            if v in ("while", "dowhile"):  # iteration_statement (gmParser.y:328-371)
                self.p += 1
                return GmNode(v, lo, self.p, [self._paren_expr(), self._compound()])
            if v == "for":
                self.p += 1
                self._take("(")
                kids: list = [None if self._is(";") else self._assignment()]
                self._take(";")
                kids.append(None if self._is(";") else self._expr())
                self._take(";")
                kids.append(None if self._is(")") else self._assignment())
                self._take(")")
                kids.append(self._compound())
                return GmNode("for", lo, self.p, kids)
            if v == "foreach":
                self.p += 1
                self._take("(")
                names = [self._ident()]
                if self._is("and") or self._is("&&"):
                    self.p += 1
                    names.append(self._ident())
                self._take("in")
                kids = [self._expr()]
                self._take(")")
                kids.append(self._compound())
                return GmNode("foreach", lo, self.p, kids, tuple(names))
            if v in ("continue", "break"):  # jump_statement (gmParser.y:373-391)
                self.p += 1
                self._take(";")
                return GmNode(v, lo, self.p)
            if v == "return":
                self.p += 1
                kids = [] if self._is(";") else [self._expr()]
                self._take(";")
                return GmNode("return", lo, self.p, kids)
        node = self._assignment()
        self._take(";")
        return GmNode("expr", lo, self.p, [node])

    def _case(self) -> GmNode:
        lo = self.p
        if self._is("default"):
            self.p += 1
            self._take(":")
            return GmNode("default", lo, self.p, [self._compound()])
        self._take("case")
        # postfix_case_expression: a name, .name, this or a constant, then [i] and .name.
        start = self.p
        if self._is("."):
            self.p += 1
            node = GmNode("dotid", start, self.p + 1, value=self._ident())
        elif self.k[self.p] == "id":
            node = GmNode("id", start, self.p + 1, value=self._ident())
        elif self._is("this"):
            self.p += 1
            node = GmNode("this", start, self.p)
        else:
            node = self._constant()
            if node is None:
                self._fail("a case value")
        while True:
            if self._is("["):
                self.p += 1
                i = self._expr()
                self._take("]")
                node = GmNode("index", start, self.p, [node, i])
            elif self._is("."):
                self.p += 1
                node = GmNode("dot", start, self.p + 1, [node], self._ident())
            else:
                break
        self._take(":")
        kids = [node]
        if self._is("{"):
            kids.append(self._compound())
        return GmNode("case", lo, self.p, kids)

    def _assignment(self) -> GmNode:
        lhs = self._expr()
        if self.v[self.p] in _GM_ASSIGN and self.k[self.p] == "op":
            if lhs.kind not in _GM_POSTFIX:
                self._fail("';' (only a name, field or call is assigned to)")
            op = self.v[self.p]
            self.p += 1
            rhs = self._expr()
            if op == "=":
                name = lhs.value if lhs.kind in ("id", "dotid", "dot") else None
                self._name_function(rhs, name)
            return GmNode("assign", lhs.lo, self.p, [lhs, rhs], op)
        return lhs

    def _expr(self, least: int = 1) -> GmNode:
        left = self._unary()
        while True:
            k = self.k[self.p]
            prec = _GM_BINARY.get(self.v[self.p]) if k in ("op", "kw") else None
            if prec is None or prec < least:
                return left
            op = self.v[self.p]
            self.p += 1
            right = self._expr(prec + 1)
            left = GmNode("bin", left.lo, right.hi, [left, right], op)

    def _unary(self) -> GmNode:
        if self.k[self.p] == "op" and self.v[self.p] in _GM_UNARY:
            lo, op = self.p, self.v[self.p]
            self.p += 1
            operand = self._unary()
            return GmNode("un", lo, operand.hi, [operand], op)
        return self._postfix()

    def _args(self, close: str) -> list[GmNode]:
        args: list[GmNode] = []
        if not self._is(close):
            args.append(self._expr())
            while self._is(","):
                self.p += 1
                args.append(self._expr())
        self._take(close)
        return args

    def _postfix(self) -> GmNode:
        node = self._primary()
        lo = node.lo
        while self.k[self.p] == "op":  # gmParser.y:659-696
            v = self.v[self.p]
            if v == "[":
                self.p += 1
                i = self._expr()
                self._take("]")
                node = GmNode("index", lo, self.p, [node, i])
            elif v == "(":
                self.p += 1
                node = GmNode("call", lo, -1, [node, *self._args(")")])
                node.hi = self.p
            elif v == ":":
                self.p += 1
                name = self._ident()
                self._take("(")
                node = GmNode("method", lo, -1, [node, *self._args(")")], name)
                node.hi = self.p
            elif v == ".":
                self.p += 1
                node = GmNode("dot", lo, self.p + 1, [node], self._ident())
            else:
                break
        return node

    def _constant(self) -> GmNode | None:
        lo, k, v = self.p, self.k[self.p], self.v[self.p]
        if k == "num":
            self.p += 1
            return GmNode("num", lo, self.p, value=v)
        if k == "char":
            self.p += 1
            return GmNode("char", lo, self.p, value=v)
        if k == "str":  # constant_string_list: adjacent strings join (gmParser.y:938-975)
            parts = []
            while self.k[self.p] == "str":
                parts.append(gm_string(self.v[self.p]))
                self.p += 1
            return GmNode("str", lo, self.p, value="".join(parts))
        if k == "kw" and v in ("true", "false"):
            self.p += 1
            return GmNode("bool", lo, self.p, value=v == "true")
        if k == "kw" and v == "null":
            self.p += 1
            return GmNode("null", lo, self.p)
        return None

    def _primary(self) -> GmNode:  # gmParser.y:809-839
        lo, k, v = self.p, self.k[self.p], self.v[self.p]
        if k == "id":
            self.p += 1
            return GmNode("id", lo, self.p, value=v)
        if k == "op" and v == ".":
            self.p += 1
            return GmNode("dotid", lo, self.p + 1, value=self._ident())
        if k == "kw" and v == "this":
            self.p += 1
            return GmNode("this", lo, self.p)
        const = self._constant()
        if const is not None:
            return const
        if (k == "kw" and v == "table") or (k == "op" and v == "{"):
            return self._table()
        if k == "kw" and v == "function":  # function_constructor (gmParser.y:735-747)
            self.p += 1
            self._take("(")
            params = []
            if not self._is(")"):
                while True:
                    plo = self.p
                    name = self._ident()
                    default = []
                    if self._is("="):
                        self.p += 1
                        default = [self._expr()]
                    params.append(GmNode("param", plo, self.p, default, name))
                    if not self._is(","):
                        break
                    self.p += 1
            self._take(")")
            body = self._compound()
            return GmNode("func", lo, self.p, [*params, body])
        if k == "op" and v == "(":
            self.p += 1
            e = self._expr()
            self._take(")")
            return GmNode("paren", lo, self.p, [e])
        self._fail("a value")

    def _table(self) -> GmNode:
        # table_constructor: table( fields ) or { fields [,] } (gmParser.y:709-733); a field is
        # a value, name = value or [integer] = value (:760-782).
        lo = self.p
        close = ")" if self._is("table") else "}"
        if close == ")":
            self.p += 1
            self._take("(")
        else:
            self._take("{")
        fields: list[GmNode] = []
        while not self._is(close):
            flo = self.p
            if self.k[self.p] == "id" and self.v[self.p + 1] == "=" and self.k[self.p + 1] == "op":
                name = self.v[self.p]
                self.p += 2
                value = self._expr()
                self._name_function(value, name)
                fields.append(GmNode("field", flo, self.p, [value], name))
            elif self._is("["):
                self.p += 1
                if self.k[self.p] != "num" or not self.v[self.p].isdigit():
                    self._fail("an integer index")
                key = int(self.v[self.p])
                self.p += 1
                self._take("]")
                self._take("=")
                fields.append(GmNode("field", flo, self.p, [self._expr()], key))
            else:
                value = self._expr()
                fields.append(GmNode("field", flo, self.p, [value], None))
            if self._is(","):
                self.p += 1
                if close == ")" and self._is(")"):
                    self._fail("a field")
            elif not self._is(close):
                self._fail(f"',' or {close!r}")
        self.p += 1
        return GmNode("table", lo, self.p, fields)


def gm_parse(text: str) -> GmScript:
    """A GameMonkey text parsed with the add-on's grammar; BotGoalsError (with its line) when
    the add-on's compiler would refuse it."""
    return GmScript(text)


def _gm_number(text: str) -> int | float:
    if text[:2] in ("0x", "0X"):
        return int(text, 16)
    if text[:2] in ("0b", "0B"):
        return int(text[2:], 2)
    if any(c in text for c in ".eEfF"):
        return float(text.rstrip("fF"))
    return int(text)


def _gm_value(script: GmScript, node: GmNode, vectors: tuple[str, ...]):
    """A literal value: numbers (signed), strings, booleans, null, vectors of three numbers
    and tables keyed by name, [index] or position."""
    kind = node.kind
    if kind == "num":
        return _gm_number(node.value)
    if kind == "un" and node.value in ("-", "+") and node.kids[0].kind == "num":
        v = _gm_number(node.kids[0].value)
        return -v if node.value == "-" else v
    if kind == "str":
        return node.value
    if kind == "bool":
        return node.value
    if kind == "null":
        return None
    if kind == "table":
        out: dict = {}
        n = 0
        for f in node.kids:
            if f.value is None:
                out[n] = _gm_value(script, f.kids[0], vectors)
                n += 1
            else:
                out[f.value] = _gm_value(script, f.kids[0], vectors)
        return out
    if kind == "call" and node.kids[0].kind == "id" and node.kids[0].value in vectors:
        args = [_gm_value(script, a, vectors) for a in node.kids[1:]]
        if len(args) != 3 or not all(
            isinstance(a, (int, float)) and not isinstance(a, bool) for a in args
        ):
            raise BotGoalsError(
                f"line {script.line(node.lo)}: {node.kids[0].value} takes three numbers"
            )
        return tuple(float(a) for a in args)
    raise BotGoalsError(
        f"line {script.line(node.lo)}: unsupported value {script.source(node)[:40]!r}"
    )


def read_goals(text: str, goals_spec: dict) -> dict:
    """The goal table a goals script sets with `global <table> = { ... };`, read as literals
    (numbers, strings, booleans, vectors and nested tables keyed by name, [index] or
    position). The whole file must parse."""
    table = str(goals_spec["table"])
    script = gm_parse(text)
    vectors = tuple(str(v) for v in goals_spec["vector_names"])
    for st in script.program.kids:
        if st.kind == "var" and st.value == ("global", table) and st.kids:
            if st.kids[0].kind != "table":
                raise BotGoalsError(f"{table} is not set to a table")
            return _gm_value(script, st.kids[0], vectors)
    raise BotGoalsError(f"the script does not set {table}")


def _gm_strings(node: GmNode) -> list[str] | None:
    if node.kind == "str":
        return [node.value]
    if node.kind == "table" and all(f.kids[0].kind == "str" for f in node.kids):
        return [f.kids[0].value for f in node.kids]
    return None


def read_script(text: str, script_spec: dict, *, strict: bool = True) -> dict:
    """What a map script defines and registers. Its globals; its named functions (table
    fields, variables and assignments holding a function) with the goal switches each makes
    with literal arguments (team expression, on, names) and the messages it prints with a
    literal; the trigger registrations `<on_trigger>("name", <map_table>.function)`; the
    functions those name that the script does not define (`undefined`, an error when
    `strict`; a shipped script or two registers a misspelt one, which the add-on passes as
    null); and how many switches and registrations take computed arguments, which are not
    followed. The whole file must parse with the add-on's
    grammar. The statement names are the profile's (`bots.script`)."""
    table, on_trigger = str(script_spec["map_table"]), str(script_spec["on_trigger"])
    available = str(script_spec["set_available"])
    debug = str(script_spec["debug_print"])
    stack: list[str] = []
    pairs = {"}": "{", ")": "(", "]": "["}
    for kind, v, _at in gm_lex(text):
        if kind != "op":
            continue
        if v in "{([":
            stack.append(v)
        elif v in pairs and (not stack or stack.pop() != pairs[v]):
            raise BotGoalsError(f"unbalanced {v!r}")
    if stack:
        raise BotGoalsError(f"unclosed {stack[-1]!r}")
    script = gm_parse(text)
    out: dict[str, Any] = {
        "globals": [],
        "functions": {},
        "triggers": [],
        "computed": {"switches": 0, "triggers": 0},
    }
    for node, _fn in script.walk():
        if node.kind == "var" and node.value[0] == "global":
            out["globals"].append(node.value[1])
        elif node.kind == "func" and node.value:
            out["functions"].setdefault(node.value, {"switches": [], "messages": []})
    for node, fn in script.walk():
        if node.kind != "call":
            continue
        callee, args = script.compact(node.kids[0]), node.kids[1:]
        if callee == on_trigger:
            name = _gm_strings(args[0]) if args else None
            target = args[1] if len(args) > 1 else None
            if (
                name is not None
                and len(name) == 1
                and target is not None
                and target.kind == "dot"
                and script.compact(target.kids[0]) == table
            ):
                out["triggers"].append((name[0], target.value))
            else:
                out["computed"]["triggers"] += 1
        elif fn is None:
            continue
        elif callee == available:
            names = _gm_strings(args[2]) if len(args) > 2 else None
            if len(args) > 2 and args[1].kind == "bool" and names is not None:
                out["functions"][fn]["switches"].append(
                    (script.compact(args[0]), args[1].value, names)
                )
            else:
                out["computed"]["switches"] += 1
        elif callee == debug and args and args[0].kind == "str":
            out["functions"][fn]["messages"].append(args[0].value)
    out["undefined"] = [f for _t, f in out["triggers"] if f not in out["functions"]]
    if out["undefined"] and strict:
        raise BotGoalsError(
            f"{on_trigger} names functions the script does not define: {out['undefined']}"
        )
    return out


def _gm_negated(script: GmScript, node: GmNode) -> str | None:
    """The source text of minus a literal decimal number, exactly: the sign flipped, a zero
    left as it is; None for anything else."""
    sign, num = "", node
    if node.kind == "un" and node.value in ("-", "+") and node.kids[0].kind == "num":
        sign, num = node.value, node.kids[0]
    if num.kind != "num" or num.value[:2].lower() in ("0x", "0b"):
        return None
    if _gm_number(num.value) == 0:
        return script.source(node)
    return num.value if sign == "-" else "-" + num.value


def gm_mirror(
    text: str | GmScript, coordinates: dict, axis: int, *, check: bool = True
) -> tuple[str, dict]:
    """A GameMonkey script mirrored through the plane where world `axis` (0, 1, 2 for x, y, z)
    is 0: in every call the profile lists as holding a point (`coordinates.points`, callee ->
    the index of its x argument) that argument's component is negated, and in every box call
    (`coordinates.boxes`, callee -> the index of its first of six arguments, mins then maxes)
    the min and max of that component swap and are negated. Only literal numbers are
    rewritten (the sign flipped in place, the rest of the file byte for byte); calls whose
    component is computed are listed as not mirrored. The result is parsed again when
    `check`. `text` may be a script already parsed."""
    if axis not in (0, 1, 2):
        raise BotGoalsError("axis is 0, 1 or 2")
    script = text if isinstance(text, GmScript) else gm_parse(text)
    text = script.text
    points = {str(k): int(v) for k, v in (coordinates.get("points") or {}).items()}
    boxes = {str(k): int(v) for k, v in (coordinates.get("boxes") or {}).items()}
    edits: list[tuple[int, int, str]] = []
    report: dict[str, Any] = {"points": 0, "boxes": 0, "not_mirrored": []}

    def skip(node: GmNode):
        report["not_mirrored"].append(
            {"line": script.line(node.lo), "call": script.source(node)[:120]}
        )

    for node, _fn in script.walk():
        if node.kind != "call":
            continue
        callee, args = script.compact(node.kids[0]), node.kids[1:]
        if callee in points:
            i = points[callee] + axis
            neg = _gm_negated(script, args[i]) if i < len(args) else None
            if neg is None:
                skip(node)
                continue
            edits.append((*script.span(args[i]), neg))
            report["points"] += 1
        elif callee in boxes:
            lo, hi = boxes[callee] + axis, boxes[callee] + 3 + axis
            if hi >= len(args):
                skip(node)
                continue
            neg_lo, neg_hi = _gm_negated(script, args[lo]), _gm_negated(script, args[hi])
            if neg_lo is None or neg_hi is None:
                skip(node)
                continue
            edits.append((*script.span(args[lo]), neg_hi))
            edits.append((*script.span(args[hi]), neg_lo))
            report["boxes"] += 1
    parts, at = [], 0
    for a, b, new in sorted(edits):
        parts += [text[at:a], new]
        at = b
    out = "".join(parts) + text[at:]
    if check:
        gm_parse(out)
    return out, report


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
