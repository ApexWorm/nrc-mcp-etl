"""Engine playtests generated from the objectives a map already has.

Every objective kind the profile can build (`objectives.kinds`) may carry a `playtest:`
template: how to recognise one in any map (`find`), which team and class play it (`actor`),
and the steps that prove it works, written in `engine_playtest`'s own scenario language plus
a few macros this module expands from the map's geometry:

- `stand`: a position on reachable floor inside one part's box, clear of another's, facing it.
- `walk_to`: a position a short walk from a point or box, facing it, then a held control.
- `spawned_at`: switch team and back to respawn, then assert the player is among a set of
  spawns: the objective's own (`spawns`) or the team's other active ones (`base`).
- `follow_path`: escort a vehicle along its path, one path point at a time.
- `round_end`: follow the objective's script events to a round end, and wait for it.

Nothing here knows a classname, key or script action: they are all the profile's. Positions
come from the navigation grid: a floor the player box rests on with headroom
(`spawns.resting_floor`), on the walkable grid, and connected to one of the team's spawns by a
walkable route, searched from all of them at once in the kernel (`routesearch`).

An objective no spawn reaches at the start of the round may sit behind another one that opens
the way (the profile's `objectives.playtest.state.opens`: a destroyed explosive frees its
brushes). Its test is then looked for again on the map with those destroyed, and `requires`
them: `pack` plays them first, in the same run. Entities the script hides as the round starts
(`state.start_hidden`) are neither spawns nor collision.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import analysis as anamod
from . import mapscript, profiles, routesearch
from . import playtest as playtestmod
from . import spawns as spawnsmod

#: Room left between the player box and what it must stay clear of.
CLEAR_MARGIN = 8.0
#: How far outside a spawn set a respawned player may stand and still count as there.
SPAWN_MARGIN = 32.0
#: How far the player box reaches into a trigger from a place beside it. A player touches a
#: trigger its box (as a capsule) overlaps (G_TouchTriggers, game/g_active.c:436-499), so its
#: origin may stand up to half its width outside the trigger's side.
TOUCH_DEPTH = 6.0
#: Reachability is checked for at most this many candidate spots per position.
MAX_ROUTE_TRIES = 12
#: Seconds the engine needs to start, before the first step (engine_playtest's connect wait).
STARTUP_SECONDS = 60


class Untestable(Exception):
    """No test can be made for this objective; the message says why."""


class Unreachable(Untestable):
    """No place to play it is reachable from the team's spawns, on the map as it stands."""


@dataclass
class Found:
    kind: str
    name: str
    team: str
    subs: dict[str, str]
    parts: dict[str, list[dict]]
    claimed: set[int] = field(default_factory=set)

    @property
    def anchor(self) -> dict:
        return self.parts["anchor"][0]

    def describe(self) -> dict:
        return {
            "objective": self.name,
            "kind": self.kind,
            "team": self.team,
            "entities": {role: [e["index"] for e in ents] for role, ents in self.parts.items()},
        }


def _keys(entity: dict) -> dict[str, str]:
    """Keys by lower-cased name; the last of a duplicate wins, as the game reads them
    (`rules._keys`)."""
    return {str(k).lower(): str(v) for k, v in entity.get("keys") or []}


def _flags(entity: dict, key: str) -> int:
    try:
        return int(float(_keys(entity).get(key.lower(), "0") or 0))
    except ValueError:
        return 0


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?\}")


def fill(value: Any, subs: dict[str, str], parts: dict[str, list[dict]] | None = None) -> Any:
    """A template with `{name}` and `{part.key}` placeholders filled in.

    `{part.key}` is that key of the part's first entity; a missing one raises `Untestable`.
    """
    if isinstance(value, str):

        def one(m: re.Match) -> str:
            head, key = m.group(1), m.group(2)
            if key is None:
                if head not in subs:
                    raise Untestable(f"the template names {{{head}}}, which this objective lacks")
                return subs[head]
            ents = (parts or {}).get(head) or []
            got = _keys(ents[0]).get(key.lower()) if ents else None
            if not got:
                raise Untestable(f"its {head} has no {key} key")
            return got

        return _PLACEHOLDER.sub(one, value)
    if isinstance(value, list):
        return [fill(v, subs, parts) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, subs, parts) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Recognising objectives
# ---------------------------------------------------------------------------


def _subs(recipes: dict, team: str) -> dict[str, str]:
    subs = {"team": team}
    for key, by_team in (recipes.get("team_values") or {}).items():
        if isinstance(by_team, dict) and team in by_team:
            subs[key] = str(by_team[team])
    return subs


def _classes(value: Any, subs: dict[str, str]) -> set[str]:
    values = value if isinstance(value, list) else [value]
    out = set()
    for v in values:
        try:
            out.add(str(fill(v, subs)))
        except Untestable:
            continue
    return out


def _match(kind: str, spec: dict, anchor: dict, entities: list[dict], recipes: dict, teams):
    """The best reading of `anchor` as this kind, or the reason it is not one."""
    find = spec["find"]
    name = _keys(anchor).get(str(find["anchor"]["name_key"]).lower(), "").strip()
    if not name:
        return None, f"it has no {find['anchor']['name_key']} to name it by"
    best = None
    reason = None
    for team in teams:
        subs = {**_subs(recipes, team), "name": name}
        if anchor["classname"] not in _classes(find["anchor"]["classname"], subs):
            continue
        parts: dict[str, list[dict]] = {"anchor": [anchor]}
        claimed = {anchor["index"]}
        for role, part in (find.get("parts") or {}).items():
            classes = _classes(part["classname"], subs)
            value = str(fill(part["value"], subs)).casefold()
            matches = [
                e
                for e in entities
                if e["classname"] in classes
                and _keys(e).get(str(part["key"]).lower(), "").casefold() == value
            ]
            claimed |= {e["index"] for e in matches}
            chosen = matches
            if part.get("flags"):
                want = int(subs.get(str(part["flags"]["value"]), "0"))
                chosen = [e for e in matches if _flags(e, part["flags"]["key"]) & want == want]
            if not chosen and not part.get("optional"):
                reason = f"no {' or '.join(sorted(classes))} has {part['key']} {value!r}" + (
                    " with the team's flag" if matches else ""
                )
                break
            if len(chosen) > 1 and not part.get("many"):
                reason = (
                    f"{len(chosen)} {' or '.join(sorted(classes))} have {part['key']} {value!r}; "
                    "which one is meant is ambiguous"
                )
                break
            parts[role] = chosen
        else:
            score = 0
            rule = find.get("team")
            if rule:
                holder = parts.get(str(rule["part"])) or []
                want = int(subs.get(str(rule["value"]), "0") or 0)
                have = _flags(holder[0], str(rule["key"])) if holder else 0
                if not want or have & want != want:
                    reason = f"its {rule['key']} {have} does not carry a team's {rule['value']}"
                    continue
                score = bin(want).count("1")
            if best is None or score > best[0]:
                best = (score, Found(kind, name, team, subs, parts, claimed))
    if best is None:
        return None, reason or "its classname does not fit the kind for any team"
    return best[1], None


def recognise(profile_id: str, game_map: Any) -> tuple[list[Found], list[dict]]:
    """Every objective the profile's `find` rules recognise, and objective entities left over."""
    recipes = profiles.load(profile_id).get("objectives") or {}
    kinds = recipes.get("kinds") or {}
    meta = recipes.get("playtest") or {}
    order = [k for k in meta.get("order") or kinds if k in kinds]
    order += [k for k in kinds if k not in order]
    entities = game_map.entities()
    claimed: set[int] = set()
    found: list[Found] = []
    misses: dict[int, list[str]] = {}
    for kind in order:
        spec = kinds[kind].get("playtest")
        if not isinstance(spec, dict) or "find" not in spec:
            continue
        teams = [str(t) for t in kinds[kind].get("teams") or recipes.get("teams") or []]
        all_classes = set()
        for team in teams:
            all_classes |= _classes(spec["find"]["anchor"]["classname"], _subs(recipes, team))
        for e in entities:
            if e["classname"] not in all_classes or e["index"] in claimed:
                continue
            got, why = _match(kind, spec, e, entities, recipes, teams)
            if got is None:
                misses.setdefault(e["index"], []).append(f"not a {kind}: {why}")
                continue
            if got.claimed & claimed:
                misses.setdefault(e["index"], []).append(
                    f"not a {kind}: it shares entities with an objective already recognised"
                )
                continue
            claimed |= got.claimed
            found.append(got)
    listed = set(meta.get("classnames") or [])
    leftover = []
    for e in entities:
        if e["index"] in claimed or e["classname"] not in listed:
            continue
        keys = _keys(e)
        leftover.append(
            {
                "entity": e["index"],
                "classname": e["classname"],
                "name": keys.get("targetname") or keys.get("scriptname") or keys.get("target"),
                "reason": "; ".join(misses.get(e["index"], []))
                or "no objective kind recognises it on its own; it belongs to none found",
            }
        )
    return found, leftover


# ---------------------------------------------------------------------------
# Geometry from the navigation grid
# ---------------------------------------------------------------------------


@dataclass
class Spot:
    x: float
    y: float
    floor: float
    route: dict | None = None
    #: The route's grid cells, when the search gives them (to tell what it passes through).
    cells: list | None = None

    def origin(self, height: float) -> list[float]:
        return [_num(self.x), _num(self.y), _num(self.floor + height)]


def _num(v: float) -> float | int:
    v = round(v, 1)
    return int(v) if float(v).is_integer() else v


def _yaw(a: tuple[float, float], b: tuple[float, float]) -> float:
    return round(math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])), 1)


class Geometry:
    """Standing positions on the navigation grid, each reachable from a team's spawns."""

    def __init__(
        self,
        grid: anamod.NavGrid,
        profile_id: str,
        meta: dict,
        hidden: frozenset[int] = frozenset(),
    ):
        self.grid = grid
        #: Entities the script hides as the round starts: no spawn among them is used.
        self.hidden = hidden
        self._sources: dict[str, dict[tuple[int, int, int], list[float]]] = {}
        self._learned = False
        #: Objective entities left out of this grid because earlier tests destroy them.
        self.opened: frozenset[int] = frozenset()
        self.game_map = grid.game_map
        self.meta = meta
        width = grid.movement.player_width
        if width is None or grid.clearance_index is None:
            raise Untestable(
                f"profile {profile_id} states no player width, so no standing position can be checked"
            )
        self.half = width.value / 2
        self.step = grid.movement.step_height.value
        self.height = grid.movement.headroom_stand.value
        self.stand_height = float(meta.get("stand_height", 0))

    # --- the map -----------------------------------------------------------

    def bounds(self, entity: dict) -> tuple[list[float], list[float]] | None:
        if entity.get("origin"):
            return None
        return anamod._brush_bounds(self.game_map, entity)

    def where(self, entity: dict) -> list[float]:
        if entity.get("origin"):
            return [float(c) for c in entity["origin"]]
        b = self.bounds(entity)
        if b is None:
            raise Untestable(f"entity {entity['index']} has neither an origin nor brushes")
        return [(lo + hi) / 2 for lo, hi in zip(*b, strict=True)]

    def team_spawns(self, label: str) -> list[dict]:
        """The team's spawns, those active at the start first."""
        classname = (self.meta.get("spawns") or {}).get("classname", {}).get(label)
        ents = [e for e in self.game_map.entities() if e["classname"] == classname]
        return sorted(ents, key=lambda e: not self.active(e))

    def homes(self, label: str) -> list[list[float]]:
        """Where the team starts: its active spawns, or every spawn when none is active."""
        ents = self.team_spawns(label)
        active = [e for e in ents if self.active(e)]
        return [self.where(e) for e in (active or ents)]

    def active(self, entity: dict) -> bool:
        rule = (self.meta.get("spawns") or {}).get("active")
        if entity["index"] in self.hidden:
            return False
        if not rule:
            return True
        return bool(_flags(entity, str(rule["key"])) & int(rule["bit"]))

    # --- positions -----------------------------------------------------------

    def standable(self, x: float, y: float, bottom: float, top: float) -> float | None:
        floor = spawnsmod.resting_floor(self.grid, x, y, bottom, top)
        if floor is None or not spawnsmod.supported(self.grid, x, y, floor):
            return None
        if self.grid.nearest_walkable((x, y, floor), radius=self.grid.cell * 2) is None:
            return None
        return floor

    def sources(self, label: str) -> dict[tuple[int, int, int], list[float]]:
        """The walkable cells the team's starting spawns stand on, each with its spawn."""
        if label not in self._sources:
            cells: dict[tuple[int, int, int], list[float]] = {}
            for p in self.homes(label):
                snap = self.grid.nearest_walkable((p[0], p[1], p[2] - self.stand_height))
                if snap is not None:
                    cells.setdefault(snap[0], p)
            self._sources[label] = cells
        return self._sources[label]

    def reach(self, spot: Spot, label: str) -> dict | None:
        """A walkable route from the team's starting spawns to the spot: one search from all of
        them at once, in the kernel, which learns each cell's moves once per grid (all of them
        up front, in worker processes, before the first search)."""
        if not routesearch.available():
            return self._reach_python(spot, label)
        goal = self.grid.nearest_walkable((spot.x, spot.y, spot.floor))
        sources = self.sources(label)
        if goal is None or not sources:
            return None
        cells = sum(len(c) for c in self.grid.columns.values())
        with routesearch.LOCK:
            graph, expand = routesearch.step_graph(self.grid)
            if not self._learned:
                self._learned = True
                routesearch.learn(graph, expand, [(0, *c) for c in self.grid.walkable_cells()])
            status, _expanded, _pruned, cost, path = graph.search(
                [(0, *c) for c in sources],
                [(0, *goal[0])],
                [self.grid.centre(goal[0])],
                expand,
                max_nodes=max(anamod.MAX_A_STAR_NODES, cells + 1),
                budget="after_goal_test",
            )
        if status != "found" or cost is None or not path:
            return None
        start = sources[tuple(path[0][1:])]
        spot.cells = [tuple(k[1:]) for k in path]
        return {"from": [_num(c) for c in start], "distance": round(cost, 2)}

    def _reach_python(self, spot: Spot, label: str) -> dict | None:
        """`reach` for a kernel without the native search: nearest spawns first."""
        starts = sorted(self.homes(label), key=lambda p: math.dist(p[:2], (spot.x, spot.y)))
        for start in starts[:3]:
            feet = (start[0], start[1], start[2] - self.stand_height)
            r = anamod.path_distance(self.grid, feet, (spot.x, spot.y, spot.floor))
            if r.get("reachable"):
                return {"from": [_num(c) for c in start], "distance": r["distance"]}
        return None

    def first_reachable(self, spots: list[Spot], label: str) -> Spot | None:
        for spot in spots[:MAX_ROUTE_TRIES]:
            route = self.reach(spot, label)
            if route is not None:
                spot.route = route
                return spot
        return None

    def inside(self, box, clear, label: str) -> Spot:
        """Floor inside `box`, the player clear of `clear`, nearest to `clear`."""
        lo, hi = box
        cell = self.grid.cell

        def axis(a: float, b: float) -> list[float]:
            a2, b2 = a + self.half, b - self.half
            if a2 > b2:
                return [(a + b) / 2]
            n = int((b2 - a2) // cell)
            return [a2 + (b2 - a2 - n * cell) / 2 + i * cell for i in range(n + 1)]

        clo, chi = clear
        pad = self.half + CLEAR_MARGIN
        rejected = {"no_floor": 0, "in_the_way": 0}
        xs, ys = axis(lo[0], hi[0]), axis(lo[1], hi[1])
        # The lattice, and a ring just clear of the objective: a tight trigger leaves only that.
        ring_x = [v for v in (clo[0] - pad - 1, chi[0] + pad + 1) if lo[0] < v < hi[0]]
        ring_y = [v for v in (clo[1] - pad - 1, chi[1] + pad + 1) if lo[1] < v < hi[1]]
        candidates = [(x, y) for y in ys for x in xs]
        candidates += [(x, y) for x in ring_x for y in ys] + [(x, y) for y in ring_y for x in xs]
        # Then a band just outside each side, where the player box still reaches TOUCH_DEPTH
        # into the box: a trigger filled by what it builds (a pile of materials) is touched
        # from beside it, the way a player stands.
        out = self.half - TOUCH_DEPTH
        band = [(x, y) for x in (lo[0] - out, hi[0] + out) for y in ys]
        band += [(x, y) for y in (lo[1] - out, hi[1] + out) for x in xs]

        def standing(points) -> list[Spot]:
            spots = []
            for x, y in points:
                floor = self.standable(x, y, lo[2] - self.step, hi[2])
                if floor is None:
                    rejected["no_floor"] += 1
                    continue
                if (
                    clo[0] - pad < x < chi[0] + pad
                    and clo[1] - pad < y < chi[1] + pad
                    and floor <= chi[2] + self.step
                    and floor + self.height > clo[2]
                ):
                    rejected["in_the_way"] += 1
                    continue
                spots.append(Spot(x, y, floor))
            return spots

        tiers = [standing(candidates), standing(band)]
        if not any(tiers):
            raise Untestable(
                f"no floor a player can stand on in its box clear of the objective ({rejected})"
            )

        def gap(s: Spot) -> float:
            dx = max(clo[0] - s.x, 0, s.x - chi[0])
            dy = max(clo[1] - s.y, 0, s.y - chi[1])
            return math.hypot(dx, dy)

        # Nearest the objective; of those, the side the team comes from.
        homes = self.homes(label)

        def home(s: Spot) -> float:
            return min((math.dist(p[:2], (s.x, s.y)) for p in homes), default=0.0)

        for spots in tiers:
            spots.sort(key=lambda s: (round(gap(s) / cell), home(s)))
            chosen = self.first_reachable(spots, label)
            if chosen is not None:
                return chosen
        raise Unreachable(
            f"none of the {sum(map(len, tiers))} places to stand in its box is reachable from "
            f"the {label} spawns"
        )

    def approach(self, target: dict, distance: float, label: str) -> tuple[Spot, list[float]]:
        """A spot `distance` from a point (or from a box's side) with a clear walk to it."""
        box = self.bounds(target)
        centre = self.where(target)
        if box is None:
            floor = spawnsmod.resting_floor(
                self.grid, centre[0], centre[1], centre[2] - 256, centre[2] + self.step
            )
        else:
            floor = spawnsmod.resting_floor(
                self.grid, centre[0], centre[1], box[0][2] - self.step, box[1][2]
            )
        if floor is None:
            raise Untestable("there is no floor under it to walk to")
        goal = (centre[0], centre[1], floor)
        spawns = self.homes(label)
        spots = []
        for i in range(16):
            a = math.radians(i * 22.5)
            u = (math.cos(a), math.sin(a))
            reach = distance
            if box is not None:
                lo, hi = box
                exits = [
                    (hi[k] - centre[k]) / abs(u[k]) if abs(u[k]) > 1e-9 else math.inf
                    for k in range(2)
                ]
                reach += min(exits)
            x, y = centre[0] + u[0] * reach, centre[1] + u[1] * reach
            f = self.standable(x, y, floor - 2 * self.step, floor + 2 * self.step)
            if f is None:
                continue
            lift = self.step / 2
            if not anamod.segment_clear(
                self.grid.clearance_solids,
                self.grid.clearance_index,
                (x, y, f + lift),
                (goal[0], goal[1], floor + lift),
            ):
                continue
            near = min((math.dist(p[:2], (x, y)) for p in spawns), default=0.0)
            spots.append((near, Spot(x, y, f)))
        spots.sort(key=lambda t: t[0])
        chosen = self.first_reachable([s for _n, s in spots], label)
        if chosen is None:
            raise Unreachable(
                f"no reachable floor {distance:g} units from it with a clear walk in "
                f"({len(spots)} clear of {16} directions)"
            )
        return chosen, list(goal)


# ---------------------------------------------------------------------------
# Script reading
# ---------------------------------------------------------------------------


class Script:
    def __init__(self, text: str, spec: dict, meta: dict):
        self.meta = meta.get("script") or {}
        try:
            blocks = mapscript.parse_spec(text, spec) if text else []
        except mapscript.ScriptSyntaxError:
            blocks = []
        self.blocks = {b.name.casefold(): b for b in blocks}

    def event(self, block: str, event: str, params: list[str] | None):
        b = self.blocks.get(block.casefold())
        if b is None:
            return None
        want = [p.casefold() for p in params or []]
        for e in b.events:
            if e.name == event.casefold() and [p.casefold() for p in e.params] == want:
                return e
        return None

    def value(self, rule: dict, subs: dict, parts: dict) -> str | None:
        """One argument of the first matching action in an event."""
        rule = fill(rule, subs, parts)
        ev = self.event(str(rule["block"]), str(rule["event"]), list(rule.get("params") or []))
        if ev is None:
            return None
        for action in ev.actions:
            if action.name != str(rule["action"]).casefold():
                continue
            if any(
                int(i) >= len(action.args) or action.args[int(i)].casefold() != str(v).casefold()
                for i, v in (rule.get("args_equal") or {}).items()
            ):
                continue
            arg = int(rule["arg"])
            return action.args[arg] if arg < len(action.args) else None
        return None

    def start_hidden(self, rule: dict | None) -> set[str]:
        """Target names a spawn event hides as the round starts (folded; the engine looks
        them up without case)."""
        if not rule:
            return set()
        event, action = str(rule["event"]).casefold(), str(rule["action"]).casefold()
        name_arg, state_arg = int(rule.get("name_arg", 0)), int(rule.get("state_arg", 1))
        states = {str(v).casefold() for v in rule.get("states") or []}
        hidden: set[str] = set()
        for block in self.blocks.values():
            for ev in block.events:
                if ev.name != event or ev.params:
                    continue
                for a in ev.actions:
                    if a.name != action or len(a.args) <= max(name_arg, state_arg):
                        continue
                    name = a.args[name_arg].casefold()
                    if a.args[state_arg].casefold() in states:
                        hidden.add(name)
                    else:
                        hidden.discard(name)
        return hidden

    def outcome(self, events: list[dict]) -> dict:
        """Whether these events lead, through calls, to a round end, and the winners set."""
        call = self.meta.get("call") or {}
        winner = self.meta.get("winner") or {}
        end = (self.meta.get("end_round") or {}).get("action")
        selfs = {s.casefold() for s in call.get("self_names") or []}
        todo = [(str(e["block"]), str(e["event"]), list(e.get("params") or [])) for e in events]
        seen: set = set()
        winners: list[str] = []
        ended = False
        path: list[str] = []
        while todo:
            block, event, params = todo.pop(0)
            key = (block.casefold(), event.casefold(), tuple(p.casefold() for p in params))
            if key in seen:
                continue
            seen.add(key)
            ev = self.event(block, event, params)
            if ev is None:
                continue
            path.append(" ".join([block, event, *params]))
            for action in ev.actions:
                if call and action.name == str(call.get("action")):
                    args = action.args
                    bi, li = int(call.get("block_arg", 0)), int(call.get("label_arg", 1))
                    if len(args) > max(bi, li):
                        target = block if args[bi].casefold() in selfs else args[bi]
                        todo.append((target, str(call.get("event")), [args[li]]))
                elif winner and action.name == str(winner.get("action")):
                    arg = int(winner.get("arg", 0))
                    team = (winner.get("teams") or {}).get(
                        action.args[arg] if arg < len(action.args) else ""
                    )
                    if team and team not in winners:
                        winners.append(team)
                elif end and action.name == str(end):
                    ended = True
        return {"ends_round": ended, "winners": winners, "events": path}


# ---------------------------------------------------------------------------
# Building one objective's test
# ---------------------------------------------------------------------------


class Builder:
    def __init__(self, found: Found, spec: dict, geo: Geometry, script: Script, ctx: dict):
        self.found, self.spec, self.geo, self.script = found, spec, geo, script
        self.pid, self.adapter = ctx["profile"], ctx["adapter"]
        self.notes: list[str] = []
        self.standing: list[dict] = []
        #: Each standing position's route, as grid cells (None when the search gives none).
        self.paths: list[list | None] = []
        self.ends_round = False
        self.team: str | None = None
        self.role: str | None = None

    def fill(self, value: Any) -> Any:
        return fill(value, self.found.subs, self.found.parts)

    def side(self, team: str) -> str:
        """A playtest team label for a recipe team name or a label."""
        if team in self.adapter["teams"]:
            return team
        try:
            return spawnsmod.team_label(self.pid, team)
        except spawnsmod.SpawnClusterError as e:
            raise Untestable(str(e)) from e

    def other(self, label: str) -> str:
        sides = [t for t in self.adapter["teams"] if t != label]
        if len(sides) != 1:
            raise Untestable("switching team to respawn needs exactly two teams")
        return sides[0]

    def part(self, role: str) -> list[dict]:
        ents = self.found.parts.get(role) or []
        if not ents:
            raise Untestable(f"it has no {role}")
        return ents

    def box(self, role: str):
        b = self.geo.bounds(self.part(role)[0])
        if b is None:
            raise Untestable(f"its {role} is a point, not a box")
        return b

    def build(self) -> dict:
        rule = self.spec.get("untestable_if")
        if isinstance(rule, dict):
            ents = self.found.parts.get(str(rule["part"])) or []
            if ents and _flags(ents[0], str(rule["key"])) & int(rule["bit"]):
                raise Untestable(" ".join(str(rule["reason"]).split()))
        actor = self.fill(self.spec.get("actor") or {})
        steps: list[dict] = []
        if actor:
            steps += self.join(self.side(str(actor["team"])), str(actor["role"]))
        for raw in self.spec.get("steps") or []:
            if "macro" in raw:
                steps += getattr(self, "macro_" + str(raw["macro"]))(raw)
            else:
                # A step may state its own confidence in the profile; that is not a step field.
                step = self.fill({k: v for k, v in raw.items() if k != "confidence"})
                if step.get("action") == "join":
                    steps += self.join(self.side(step["team"]), step["role"], step.get("label"))
                else:
                    steps.append(step)
        watch = [str(w) for w in self.fill(self.spec.get("watch") or [])]
        return {"watch": watch, "steps": steps}

    # --- macros ------------------------------------------------------------

    def join(self, team: str, role: str, label: str | None = None) -> list[dict]:
        self.team, self.role = team, role
        step = {"action": "join", "team": team, "role": role, "timeout": 40}
        return [step | ({"label": label} if label else {})]

    def macro_stand(self, raw: dict) -> list[dict]:
        clear = self.box(str(raw["clear_of"]))
        spot = self.geo.inside(self.box(str(raw["in"])), clear, self.team or "")
        face = self.geo.where(self.part(str(raw.get("face", raw["clear_of"])))[0])
        angles = [float(raw.get("pitch", 0)), _yaw((spot.x, spot.y), face), 0]
        origin = spot.origin(self.geo.stand_height)
        self.standing.append({"origin": origin, "inside": raw["in"], "route": spot.route})
        self.paths.append(spot.cells)
        return [self._labelled({"action": "position", "origin": origin, "angles": angles}, raw)]

    def macro_walk_to(self, raw: dict) -> list[dict]:
        target = self.part(str(raw["target"]))[0]
        spot, goal = self.geo.approach(target, float(raw.get("distance", 64)), self.team or "")
        origin = spot.origin(self.geo.stand_height)
        self.standing.append(
            {"origin": origin, "walks_to": [_num(c) for c in goal], "route": spot.route}
        )
        self.paths.append(spot.cells)
        hold: dict[str, Any] = {"action": "hold", "controls": list(raw.get("controls") or [])}
        if "until" in raw:
            hold["until"] = self.fill(raw["until"])
        else:
            hold["seconds"] = float(raw.get("seconds", 1.5))
        hold["timeout"] = float(raw.get("timeout", 10))
        return [
            {
                "action": "position",
                "origin": origin,
                "angles": [0, _yaw((spot.x, spot.y), goal), 0],
            },
            self._labelled(hold, raw),
        ]

    def macro_spawned_at(self, raw: dict) -> list[dict]:
        label = self.side(str(self.fill(raw["team"]))) if "team" in raw else self.team
        if label is None:
            raise Untestable("spawned_at needs a team")
        role = str(raw.get("role") or self.role or "")
        mine = {e["index"] for ents in self.found.parts.values() for e in ents}
        classname = (self.geo.meta.get("spawns") or {}).get("classname", {}).get(label)
        own = [e for e in self.found.parts.get("spawns") or [] if e["classname"] == classname]
        base = [
            e for e in self.geo.team_spawns(label) if self.geo.active(e) and e["index"] not in mine
        ]
        chosen = own if raw["at"] != "base" else base
        if not chosen:
            raise Untestable(
                f"it has no {label} spawns of its own, so nothing shows a capture"
                if raw["at"] != "base"
                else f"the {label} team has no other active spawns to come back to"
            )
        box = self._spread(chosen)
        if own and base and self._overlap(self._spread(own), self._spread(base)):
            raise Untestable(
                f"the {label} spawns it hands over lie among that team's own, so where a player "
                "respawns cannot show who holds it"
            )
        steps: list[dict] = []
        if raw.get("rejoin", True):
            steps += self.join(self.other(label), role)
            steps += self.join(label, role)
        lo, hi = box
        until = []
        for axis in (0, 1):
            until.append(
                {"scope": "player", "field": f"position.{axis}", "op": "gte", "value": lo[axis]}
            )
            until.append(
                {"scope": "player", "field": f"position.{axis}", "op": "lte", "value": hi[axis]}
            )
        steps.append(self._labelled({"action": "assert", "until": until}, raw))
        return steps

    @staticmethod
    def _spread(ents: list[dict]) -> tuple[list[float], list[float]]:
        xs = [float(e["origin"][0]) for e in ents if e.get("origin")]
        ys = [float(e["origin"][1]) for e in ents if e.get("origin")]
        return (
            [_num(min(xs) - SPAWN_MARGIN), _num(min(ys) - SPAWN_MARGIN)],
            [_num(max(xs) + SPAWN_MARGIN), _num(max(ys) + SPAWN_MARGIN)],
        )

    @staticmethod
    def _overlap(a, b) -> bool:
        return all(a[0][k] <= b[1][k] and b[0][k] <= a[1][k] for k in (0, 1))

    def macro_round_end(self, raw: dict) -> list[dict]:
        try:
            events = self.fill(raw["events"])
        except Untestable as e:
            self.notes.append(f"the round end is not checked: {e}")
            return []
        out = self.script.outcome(events)
        if not out["ends_round"]:
            return []
        self.ends_round = True
        until = [{"scope": "game", "field": "phase", "value": "intermission"}]
        if len(out["winners"]) == 1:
            until.append({"scope": "game", "field": "winner", "value": out["winners"][0]})
        else:
            self.notes.append(
                "its script ends the round but sets "
                + ("no winner" if not out["winners"] else "more than one winner")
                + "; only the round end is checked"
            )
        return [
            {
                "action": "wait",
                "label": "its script ends the round: " + " -> ".join(out["events"]),
                "until": until,
                "timeout": float(raw.get("timeout", 30)),
            }
        ]

    def macro_follow_path(self, raw: dict) -> list[dict]:
        f = self.found
        start = self.script.value(raw["path"], f.subs, f.parts)
        speed_text = self.script.value(raw["speed"], f.subs, f.parts)
        if not start:
            raise Untestable("its script names no path for it to follow")
        try:
            speed = float(speed_text or "")
        except ValueError:
            raise Untestable("its script gives no speed it moves at") from None
        if speed <= 0:
            raise Untestable("its script gives no speed it moves at")
        chain = self._path(start)
        if len(chain) < 2:
            raise Untestable(f"its path from {start!r} has no segment")
        vehicle = self.box(str(raw["vehicle"]))
        volume = self.box(str(raw["volume"]))
        p0 = chain[0][0]
        radius = max(
            math.hypot(x - p0[0], y - p0[1])
            for x in (vehicle[0][0], vehicle[1][0])
            for y in (vehicle[0][1], vehicle[1][1])
        )
        room = min(
            p0[0] - volume[0][0], volume[1][0] - p0[0], p0[1] - volume[0][1], volume[1][1] - p0[1]
        )
        r_min = radius + self.geo.half + 2 * CLEAR_MARGIN
        r_max = room - self.geo.half - CLEAR_MARGIN
        if r_min > r_max:
            raise Untestable(
                f"its escort volume (reaching {room:g} from the path) leaves no room to stand "
                f"beside the vehicle (radius {radius:.0f})"
            )
        ring = (r_min + r_max) / 2
        below = p0[2] - volume[0][2]  # path height above the floor the escort stands on
        name = self.fill(self.spec.get("watch") or ["{name}"])[0]

        def beside(i: int) -> Spot:
            point = chain[i][0]
            ahead = (
                chain[i][1][0] if chain[i][1] else (chain[i + 1][0] if i + 1 < len(chain) else None)
            )
            if ahead is None:
                ahead = [2 * point[k] - chain[i - 1][0][k] for k in range(3)]
            tangent = (ahead[0] - point[0], ahead[1] - point[1])
            norm = math.hypot(*tangent) or 1.0
            tx, ty = tangent[0] / norm, tangent[1] / norm
            # Stand on the outside of a bend: away from where the next point lies off the tangent.
            nxt = chain[i + 1][0] if i + 1 < len(chain) else ahead
            off = (nxt[0] - point[0]) * -ty + (nxt[1] - point[1]) * tx
            sides = [(-ty, tx), (ty, -tx)]
            if off > 0:
                sides.reverse()
            spots = []
            for sx, sy in sides:
                for turn in (0, 30, -30, 60, -60):
                    c, s = math.cos(math.radians(turn)), math.sin(math.radians(turn))
                    ux, uy = sx * c - sy * s, sx * s + sy * c
                    x, y = point[0] + ux * ring, point[1] + uy * ring
                    floor = self.geo.standable(
                        x, y, point[2] - below - self.geo.step, point[2] - below + self.geo.step
                    )
                    if floor is not None:
                        spots.append(Spot(x, y, floor))
            chosen = self.geo.first_reachable(spots, self.team or "")
            if chosen is None:
                raise Unreachable(f"there is no reachable floor beside its path at point {i}")
            return chosen

        def passed(i: int, op_forward: bool = True) -> dict:
            a, b = chain[i - 1][0], chain[i][0]
            axis = max(range(3), key=lambda k: abs(b[k] - a[k]))
            rising = b[axis] > a[axis]
            if op_forward:
                op, value = ("gte", b[axis] - 2) if rising else ("lte", b[axis] + 2)
            else:
                op, value = ("lt", b[axis]) if rising else ("gt", b[axis])
            return {
                "scope": "entity",
                "name": name,
                "field": f"position.{axis}",
                "op": op,
                "value": _num(value),
            }

        def length(i: int) -> float:
            return math.dist(chain[i][0], chain[i + 1][0])

        steps: list[dict] = []
        spots = [beside(i) for i in range(len(chain) - 1)]
        if 2 * len(spots) + 6 > 120:
            raise Untestable(
                f"its path has {len(spots)} segments, more than one scenario can follow"
            )
        for i, spot in enumerate(spots):
            origin = spot.origin(self.geo.stand_height)
            self.standing.append({"origin": origin, "beside_path_point": i, "route": spot.route})
            self.paths.append(spot.cells)
            p = chain[i][0]
            steps.append(
                {
                    "action": "position",
                    "origin": origin,
                    "angles": [10, _yaw((spot.x, spot.y), (p[0], p[1])), 0],
                    "label": f"stand beside path point {i}, in the escort volume",
                }
            )
            steps.append(
                {
                    "action": "wait",
                    "label": f"it moves past path point {i + 1}",
                    "until": [passed(i + 1)],
                    "timeout": round(min(110, length(i) / speed * 2 + 10), 1),
                }
            )
            if i == 0:
                steps += self._stop_check(raw, chain, speed, ring, room, passed)
        last = chain[-1][0]
        steps.append(
            {
                "action": "wait",
                "label": "it reaches the end of its path",
                "until": [
                    {
                        "scope": "entity",
                        "name": name,
                        "field": f"position.{k}",
                        "op": "near",
                        "value": _num(last[k]),
                        "tolerance": 2,
                    }
                    for k in (0, 1)
                ],
                "timeout": 15,
            }
        )
        return steps

    def _stop_check(self, raw, chain, speed, ring, room, passed) -> list[dict]:
        """Stay put once it has moved off: it must stop within the delay the script gives."""
        f = self.found
        text = (
            self.script.value(raw["stop_after"], f.subs, f.parts) if raw.get("stop_after") else None
        )
        try:
            delay = float(text or "")
        except ValueError:
            self.notes.append("its stop delay is not in the script, so stopping is not checked")
            return []
        slack = float(raw.get("stop_slack", 2))
        longest = max(math.dist(chain[i][0], chain[i + 1][0]) for i in range(len(chain) - 1))
        # Out of the volume once it is `room + ring` past us; then the delay, then a segment.
        reach = math.hypot(room, ring) + self.geo.half + (delay + slack) * speed + longest
        travelled, limit = 0.0, None
        for i in range(1, len(chain)):
            travelled += math.dist(chain[i - 1][0], chain[i][0])
            if travelled > reach:
                limit = i
                break
        if limit is None or limit + 1 >= len(chain):
            self.notes.append(
                "its path is too short to see it stop before the end; stopping is not checked"
            )
            return []
        wait = round((delay + slack) + (math.hypot(room, ring) + longest) / speed + 1, 1)
        return [
            {
                "action": "hold",
                "controls": ["crouch"],
                "seconds": min(wait, 60),
                "timeout": min(wait, 60) + 10,
                "label": "stay behind: without its escort it must stop",
            }
            if "crouch" in self.adapter["controls"]
            else {
                "action": "wait",
                "until": [{"scope": "player", "field": "alive", "value": True}],
                "timeout": 1,
            },
            {
                "action": "assert",
                "label": f"it stopped before path point {limit}",
                "until": [passed(limit, op_forward=False)],
            },
        ]

    def _path(self, start: str) -> list[tuple[list[float], list[list[float]]]]:
        """Path points from `start`, each with its control points, following the next key."""
        spec = profiles.load(self.pid).get("paths") or {}
        fold = spec.get("case_sensitive") is False
        norm = (lambda s: s.casefold()) if fold else (lambda s: s)
        name_key = str(spec.get("name_key", "targetname")).lower()
        points: dict[str, dict] = {}
        controls: dict[str, list[float]] = {}
        for e in self.geo.game_map.entities():
            keys = _keys(e)
            nm = keys.get(name_key, "")
            if not nm or not e.get("origin"):
                continue
            if e["classname"] == spec.get("point_class"):
                points.setdefault(norm(nm), e)
            elif e["classname"] == spec.get("control_class"):
                controls.setdefault(norm(nm), [float(c) for c in e["origin"]])
        chain = []
        here = points.get(norm(start))
        seen = set()
        while here is not None and here["index"] not in seen:
            seen.add(here["index"])
            keys = _keys(here)
            ctl = []
            for k in spec.get("control_keys") or []:
                c = controls.get(norm(keys.get(str(k).lower(), "")))
                if c is None:
                    break
                ctl.append(c)
            chain.append(([float(c) for c in here["origin"]], ctl))
            here = points.get(norm(keys.get(str(spec.get("next_key", "target")).lower(), "")))
        return chain

    @staticmethod
    def _labelled(step: dict, raw: dict) -> dict:
        return step | ({"label": str(raw["label"])} if raw.get("label") else {})


# ---------------------------------------------------------------------------
# The whole map
# ---------------------------------------------------------------------------


def _budget(steps: list[dict]) -> float:
    """Seconds a run may need: the timeouts of what waits, a little for everything else."""
    total = 0.0
    for s in steps:
        if s["action"] == "join" or "until" in s and s["action"] != "assert":
            total += float(s.get("timeout", 30))
        elif s["action"] == "hold":
            total += float(s.get("seconds", 0)) + 2
        else:
            total += 3
    return total


def _hidden(script: Script, meta: dict, game_map: Any) -> frozenset[int]:
    """Entities the script hides as the round starts (`state.start_hidden`)."""
    names = script.start_hidden((meta.get("state") or {}).get("start_hidden"))
    if not names:
        return frozenset()
    return frozenset(
        e["index"]
        for e in game_map.entities()
        if (_keys(e).get("targetname") or "").casefold() in names
    )


def _regrid(grid: anamod.NavGrid, profile_id: str, nonsolid: frozenset[int]) -> anamod.NavGrid:
    """The same grid with these entities' brushes left out (hidden, or destroyed)."""
    if not nonsolid:
        return grid
    return anamod.build_navgrid(
        grid.map_path,
        cell=grid.cell,
        profile_id=profile_id,
        game_map=grid.game_map,
        stance=grid.stance,
        nonsolid_entities=nonsolid,
    )


def _crosses(geo: Geometry, cells: list, box) -> bool:
    """Whether a route's cells pass through a box (grown by the player's half width)."""
    pad = geo.half + geo.grid.cell
    lo, hi = box
    for c in cells:
        x, y, z = geo.grid.centre(tuple(c))
        if (
            lo[0] - pad <= x <= hi[0] + pad
            and lo[1] - pad <= y <= hi[1] + pad
            and lo[2] - geo.height <= z <= hi[2] + geo.step
        ):
            return True
    return False


def generate(
    profile_id: str,
    grid: anamod.NavGrid,
    script_text: str,
    *,
    only: str | None = None,
) -> dict:
    """Tests for every recognised objective, packed into as few engine runs as possible.

    Round 0 walks the map as the round starts. Each later round leaves out the brushes of the
    objectives earlier tests destroy (`state.opens`) and retries what was out of reach; a test
    found there `requires` the destroyed objectives its routes pass through.
    """
    data = profiles.load(profile_id)
    recipes = data.get("objectives") or {}
    meta = recipes.get("playtest") or {}
    adapter = playtestmod.spec(profile_id)
    spec = data.get("mapscript") or {}
    found, leftover = recognise(profile_id, grid.game_map)
    script = Script(script_text, spec, meta)
    opens = set((meta.get("state") or {}).get("opens") or [])
    hidden = _hidden(script, meta, grid.game_map)
    hidden_brushes = frozenset(
        e["index"]
        for e in grid.game_map.entities()
        if e["index"] in hidden and int(e.get("brushes") or 0)
    )
    wanted = None
    if only is not None:
        wanted = {f.name for f in found if f.name.casefold() == only.casefold()}
        leftover = []
    skipped = list(leftover)
    try:
        geo = Geometry(_regrid(grid, profile_id, hidden_brushes), profile_id, meta, hidden)
    except Untestable as e:
        return {
            "tests": [],
            "runs": [],
            "skipped": [
                f.describe() | {"reason": str(e)}
                for f in found
                if wanted is None or f.name in wanted
            ],
        }
    ctx = {"profile": profile_id, "adapter": adapter}
    built: dict[str, dict] = {}  # objective name -> test
    reasons: dict[str, str] = {}
    destroyed: dict[str, tuple[Found, Any]] = {}  # name -> (objective, brush box)
    pending = list(found)
    rounds = 0
    while pending:
        progress = False
        retry = []
        for f in pending:
            template = recipes["kinds"][f.kind]["playtest"]
            builder = Builder(f, template, geo, script, ctx)
            try:
                scenario = builder.build()
                playtestmod.validate(scenario, adapter)
            except Unreachable as e:
                reasons[f.name] = str(e)
                retry.append(f)
                continue
            except Untestable as e:
                reasons[f.name] = str(e)
                continue
            except playtestmod.PlaytestError as e:
                reasons[f.name] = f"the generated steps are invalid: {e}"
                continue
            requires: list[str] = []
            if rounds:
                through = [
                    name
                    for name, (_d, box) in destroyed.items()
                    if any(p is None or _crosses(geo, p, box) for p in builder.paths)
                ]
                for name in through:
                    for r in [*built[name]["requires"], name]:
                        if r not in requires:
                            requires.append(r)
                if requires:
                    builder.notes.append(
                        "out of reach as the round starts; played in the same run after "
                        + ", ".join(requires)
                        + " (destroyed, they open the way)"
                    )
            built[f.name] = {
                **f.describe(),
                "proves": template.get("proves", ""),
                "ends_round": builder.ends_round,
                "requires": requires,
                "round": rounds,
                "standing": builder.standing,
                "notes": builder.notes,
                "watch": scenario["watch"],
                "steps": scenario["steps"],
            }
            progress = True
            box = geo.bounds(f.anchor)
            if f.kind in opens and not builder.ends_round and box is not None:
                destroyed[f.name] = (f, box)
        pending = retry
        opened = frozenset(d[0].anchor["index"] for d in destroyed.values())
        if not pending or not progress or opened == geo.opened:
            break
        rounds += 1
        geo = Geometry(_regrid(grid, profile_id, hidden_brushes | opened), profile_id, meta, hidden)
        geo.opened = opened
    for f in pending:
        reason = reasons.get(f.name, "")
        if rounds and destroyed:
            reason += " (also with " + ", ".join(destroyed) + " destroyed first)"
        skipped.append(f.describe() | {"reason": reason})
    for f in found:
        if f.name in reasons and f.name not in built and f not in pending:
            skipped.append(f.describe() | {"reason": reasons[f.name]})
    tests = list(built.values())
    if wanted is not None:
        keep = set(wanted)
        for t in tests:
            if t["objective"] in wanted:
                keep |= set(t["requires"])
        tests = [t for t in tests if t["objective"] in keep]
        skipped = [s for s in skipped if s.get("objective") in wanted]
    return {"tests": tests, "runs": pack(tests, adapter), "skipped": skipped}


def pack(tests: list[dict], adapter: dict) -> list[dict]:
    """Runs for `engine_playtest`: tests that leave the round going first, one round end last.

    A test that ends the round must close its run: nothing after intermission can play. A
    test that `requires` others plays after them in the same run; one already played as
    another's requirement is not played again on its own.
    """
    by_name = {t["objective"]: t for t in tests}
    first = sorted(range(len(tests)), key=lambda i: (tests[i].get("round", 0), i))
    order = [tests[i] for i in first if not tests[i]["ends_round"]]
    order += [tests[i] for i in first if tests[i]["ends_round"]]
    runs: list[dict] = []
    current: dict | None = None
    played: set[str] = set()

    def steps_of(t: dict) -> list[dict]:
        name = re.sub(r"[^A-Za-z0-9_-]", "_", t["objective"])[:56]
        return [*t["steps"], {"action": "screenshot", "name": f"{name}_done"}]

    def fits(run: dict | None, chain: list[dict]) -> bool:
        if run is None or run["closed"]:
            return False
        steps = run["steps"] + [s for t in chain for s in steps_of(t)]
        watch = set(run["watch"]) | {w for t in chain for w in t["watch"]}
        return len(steps) <= 128 and len(watch) <= 32 and STARTUP_SECONDS + _budget(steps) <= 600

    for t in order:
        if t["objective"] in played:
            continue
        needs = [by_name[r] for r in t.get("requires") or [] if r in by_name]
        if any(n["ends_round"] for n in needs) or len(needs) != len(t.get("requires") or []):
            continue  # cannot be reached in a run that is still going
        chain = [n for n in needs if current is None or n["objective"] not in current["objectives"]]
        chain.append(t)
        if not fits(current, chain):
            current = {"objectives": [], "watch": [], "steps": [], "closed": False}
            runs.append(current)
            chain = [*needs, t]
        for c in chain:
            current["objectives"].append(c["objective"])
            current["watch"] = list(dict.fromkeys(current["watch"] + c["watch"]))
            current["steps"] += steps_of(c)
            played.add(c["objective"])
        current["closed"] = t["ends_round"]
    out = []
    for r in runs:
        scenario = (
            {"watch": r["watch"], "steps": r["steps"]} if r["watch"] else {"steps": r["steps"]}
        )
        playtestmod.validate(scenario, adapter)
        out.append(
            {
                "objectives": r["objectives"],
                "scenario": scenario,
                "timeout_seconds": min(600, round(STARTUP_SECONDS + _budget(r["steps"]))),
            }
        )
    return out
