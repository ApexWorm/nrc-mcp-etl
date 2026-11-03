"""Bot waypoint graphs: seeded from the stance navigation grids, written in the bot's own format.

A server running a bot add-on needs a waypoint graph per map, or its bots stand still. The
analysis grids already know where a body fits standing, crouched and prone, and which moves
between cells are possible; this module turns that into a sparse graph of waypoints with
directed connections, flags each waypoint with the stance needed to reach it, names the ones
at team spawns and objective approach areas, and serializes it.

Everything about the bot and the game comes from the profile's `bots:` section: the file
format and version, the header layout, the flag bits, the waypoint height above the floor,
the default radius, where the add-on looks for the file. The codec here implements one
format family (`packed_waypoints`: a fixed header, then per waypoint a field mask, the
position, varint-packed optional fields and relative-index connections); the profile states
which version it is and cites the add-on's source for every field.

What a seeded graph is not: a hand-tuned waypoint file. It carries no jump, ladder, door,
sniper or camp spots, and no map script; the add-on's goals come from the game itself
(objective entities the game reports to it), and they need only reachable waypoints near
them.
"""

from __future__ import annotations

import math
import struct
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import analysis, profiles


class BotNavError(RuntimeError):
    pass


# --------------------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------------------


def spec(profile_id: str) -> dict[str, Any]:
    data = profiles.load(profile_id).get("bots")
    if not isinstance(data, dict):
        raise BotNavError(f"profile {profile_id} describes no bot waypoint format")
    fmt = data.get("format")
    if not isinstance(fmt, dict) or fmt.get("codec") != "packed_waypoints":
        raise BotNavError(f"profile {profile_id} bots.format names no codec this server implements")
    return data


# --------------------------------------------------------------------------------------
# Codec
# --------------------------------------------------------------------------------------


@dataclass
class Waypoint:
    position: tuple[float, float, float]
    uid: int = 0
    facing: tuple[float, float, float] = (0.0, 0.0, 0.0)
    flags: int = 0
    radius: float | None = None  # None: the format's default radius
    name: str = ""
    properties: list[tuple[str, str]] = field(default_factory=list)
    connections: list[int] = field(default_factory=list)  # indices into the file's list


@dataclass
class WaypointFile:
    version: int
    waypoints: list[Waypoint]
    timestamp: bytes = b""
    comments: bytes = b""
    world_mins: tuple[float, float, float] = (0.0, 0.0, 0.0)
    world_maxs: tuple[float, float, float] = (0.0, 0.0, 0.0)


def _header(fmt: dict) -> struct.Struct:
    h = fmt["header"]
    return struct.Struct(f"<BI{int(h['timestamp_bytes'])}s{int(h['comment_bytes'])}s6f")


class _Reader:
    def __init__(self, data: bytes, offset: int):
        self.data, self.pos = data, offset

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise BotNavError(f"truncated at byte {self.pos}: wanted {n} more")
        out = self.data[self.pos : self.pos + n]
        self.pos += n
        return out

    def byte(self) -> int:
        return self.take(1)[0]

    def varint(self) -> int:
        # Seven bits per byte, low first; the high bit marks the LAST byte.
        value, shift = 0, 0
        while True:
            c = self.byte()
            value |= (c & 0x7F) << shift
            if c & 0x80:
                return value
            shift += 7
            if shift > 70:
                raise BotNavError(f"varint too long at byte {self.pos}")

    def signed(self) -> int:
        u = self.varint() & 0xFFFFFFFF
        v = (u >> 1) if not u & 1 else ~(u >> 1)
        return v

    def string(self) -> str:
        return self.take(self.varint()).decode("latin-1")

    def floats(self, n: int) -> tuple[float, ...]:
        return struct.unpack(f"<{n}f", self.take(4 * n))


def _varint(value: int) -> bytes:
    if value < 0:
        raise BotNavError("packed integers are unsigned")
    out = bytearray()
    while True:
        out.append(value & 0x7F)
        value >>= 7
        if not value:
            break
    out[-1] |= 0x80
    return bytes(out)


def _signed(value: int) -> bytes:
    return _varint((value << 1) & 0xFFFFFFFF if value >= 0 else ((~value << 1) | 1) & 0xFFFFFFFF)


def _string(text: str) -> bytes:
    raw = text.encode("latin-1")
    return _varint(len(raw)) + raw


def read(data: bytes, fmt: dict) -> WaypointFile:
    """Parse a waypoint file exactly as the add-on's loader does (see the profile's citations)."""
    head = _header(fmt)
    if len(data) < head.size:
        raise BotNavError(f"file is {len(data)} bytes; the header alone is {head.size}")
    version, count, stamp, comments, *box = head.unpack_from(data)
    if version != int(fmt["version"]):
        raise BotNavError(f"file is version {version}; this codec reads version {fmt['version']}")
    mask = {k: int(v) for k, v in fmt["mask"].items()}
    r = _Reader(data, head.size)
    waypoints: list[Waypoint] = []
    prev_uid = max_uid = 0
    for index in range(count):
        m = r.byte()
        wp = Waypoint(position=(0.0, 0.0, 0.0))
        wp.uid = r.varint() if m & mask["uid"] else prev_uid + 1
        wp.position = r.floats(3)
        if m & mask["facing"]:
            wp.facing = r.floats(3)
        if m & mask["flags"]:
            wp.flags = r.varint()
        if m & mask["radius_int"]:
            wp.radius = float(r.byte())
        elif m & mask["radius_float"]:
            wp.radius = r.floats(1)[0]
        else:
            wp.radius = None
        if m & mask["name"]:
            wp.name = r.string()
        if m & mask["properties"]:
            wp.properties = [(r.string(), r.string()) for _ in range(r.varint())]
        for _ in range(r.varint()):
            target = r.signed() + index
            if not 0 <= target < count:
                raise BotNavError(f"waypoint {index} connects to {target}, outside 0..{count - 1}")
            wp.connections.append(target)
        if wp.uid == 0:
            wp.uid = max_uid + 1
        prev_uid = wp.uid
        max_uid = max(max_uid, prev_uid)
        waypoints.append(wp)
    if r.pos != len(data):
        raise BotNavError(f"{len(data) - r.pos} trailing bytes after {count} waypoints")
    return WaypointFile(
        version=version,
        waypoints=waypoints,
        timestamp=stamp,
        comments=comments,
        world_mins=tuple(box[:3]),
        world_maxs=tuple(box[3:]),
    )


def write(wf: WaypointFile, fmt: dict) -> bytes:
    """Serialize exactly as the add-on's own saver does, so a read/write round trip is identity."""
    head = _header(fmt)
    h = fmt["header"]
    mask = {k: int(v) for k, v in fmt["mask"].items()}
    default_radius = float(fmt["default_radius"])
    out = bytearray(
        head.pack(
            int(fmt["version"]),
            len(wf.waypoints),
            wf.timestamp[: int(h["timestamp_bytes"])],
            wf.comments[: int(h["comment_bytes"])],
            *wf.world_mins,
            *wf.world_maxs,
        )
    )
    uid = 0
    for index, wp in enumerate(wf.waypoints):
        m = 0
        if wp.uid != uid + 1:
            m |= mask["uid"]
        uid = wp.uid
        if any(wp.facing):
            m |= mask["facing"]
        if wp.flags:
            m |= mask["flags"]
        radius = default_radius if wp.radius is None else float(wp.radius)
        if radius != round(radius) or not 0 <= radius <= 255:
            m |= mask["radius_float"]
        elif radius != default_radius:
            m |= mask["radius_int"]
        if wp.name:
            m |= mask["name"]
        if wp.properties:
            m |= mask["properties"]
        out.append(m)
        if m & mask["uid"]:
            out += _varint(wp.uid)
        out += struct.pack("<3f", *wp.position)
        if m & mask["facing"]:
            out += struct.pack("<3f", *wp.facing)
        if m & mask["flags"]:
            out += _varint(wp.flags)
        if m & mask["radius_int"]:
            out.append(int(radius))
        if m & mask["radius_float"]:
            out += struct.pack("<f", radius)
        if m & mask["name"]:
            out += _string(wp.name)
        if m & mask["properties"]:
            out += _varint(len(wp.properties))
            for k, v in wp.properties:
                out += _string(k) + _string(v)
        out += _varint(len(wp.connections))
        for target in wp.connections:
            if not 0 <= target < len(wf.waypoints):
                raise BotNavError(f"waypoint {index} connects to missing waypoint {target}")
            out += _signed(target - index)
    return bytes(out)


def flag_names(flags: int, spec_flags: dict[str, int]) -> list[str]:
    return [name for name, bit in spec_flags.items() if flags & int(bit)]


# --------------------------------------------------------------------------------------
# Seeding a graph from the stance grids
# --------------------------------------------------------------------------------------

STANCES = ("stand", "crouch", "prone")


@dataclass
class Seed:
    """A seeded graph plus everything needed to judge it."""

    waypoints: list[Waypoint]
    stance_of: list[str]
    cells: list[tuple[int, int, int]]
    report: dict[str, Any]
    #: Every walkable cell's waypoint (the waypoint of the piece it fell in).
    piece_of: dict[tuple[int, int, int], int] = field(default_factory=dict)


class _Graph:
    """The union of the stance grids, as one directed cell graph with a stance per cell."""

    def __init__(self, grids: dict[str, analysis.NavGrid], exclude: frozenset = frozenset()):
        self.grids = grids
        self.exclude = exclude
        self.order = [s for s in STANCES if s in grids]
        self.rank: dict[tuple[int, int, int], int] = {}
        for r, stance in enumerate(self.order):
            for c in grids[stance].walkable_cells():
                if c not in exclude:
                    self.rank.setdefault(c, r)
        self._nb: dict[tuple[int, tuple[int, int, int]], frozenset] = {}

    def nb(self, r: int, cell) -> frozenset:
        """Directed neighbours of `cell` in the grid of stance rank `r`."""
        key = (r, cell)
        hit = self._nb.get(key)
        if hit is None:
            grid = self.grids[self.order[r]]
            hit = (
                frozenset(d for d, _ in grid.neighbours(cell) if d not in self.exclude)
                if grid.is_walkable(cell) and cell not in self.exclude
                else frozenset()
            )
            self._nb[key] = hit
        return hit

    def moves(self, r: int, cell) -> frozenset:
        """Moves in any stance up to rank `r`: a body changes stance where both boxes fit."""
        if r == 0:
            return self.nb(0, cell)
        return frozenset().union(*(self.nb(rr, cell) for rr in range(r + 1)))

    def straight(self, r: int, a, b) -> bool:
        return self.walk(r, a, b) is not None

    def walk(self, r: int, a, b) -> list | None:
        """The cells a body allowed stances up to rank `r` crosses walking the straight XY
        line a -> b, or None where it cannot.

        Steps column by column along the line through directed grid moves only, changing
        stance only where both bodies fit, as route_report does. A riser can clear the cells
        right against it (body clearance), so when the next column has no move, columns a
        little further along the line are tried, as the grid's own longer step moves do.
        """
        if a == b:
            return [a]
        span = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
        line: list[tuple[int, int]] = []
        for i in range(1, 2 * span + 1):
            col = (
                round(a[0] + (b[0] - a[0]) * i / (2 * span)),
                round(a[1] + (b[1] - a[1]) * i / (2 * span)),
            )
            if col != (a[0], a[1]) and (not line or line[-1] != col):
                line.append(col)
        cur, i, cells = a, 0, [a]
        while i < len(line):
            moves = self.moves(r, cur)
            found = None
            for ahead in range(i, min(len(line), i + 4)):
                options = [d for d in moves if (d[0], d[1]) == line[ahead]]
                if options:
                    want = a[2] + (b[2] - a[2]) * (ahead + 1) / len(line)
                    found = (ahead, min(options, key=lambda d: (abs(d[2] - want), d)))
                    break
            if found is None:
                return None
            i, cur = found[0] + 1, found[1]
            cells.append(cur)
        return cells if cur == b else None


def seed(
    grids: dict[str, analysis.NavGrid],
    bots: dict[str, Any],
    *,
    spacing: float,
    pins: list[dict] | None = None,
    openings: frozenset = frozenset(),
    exclude: frozenset = frozenset(),
) -> Seed:
    """Sample the walkable cells into waypoints about `spacing` apart and connect them.

    Cells are bucketed into `spacing`-sized columns per stance; each bucket splits into the
    pieces connected by two-way moves inside it (so a ledge and the floor below it, or two
    storeys, stay apart). Each piece gets one waypoint, on the pinned cell if a spawn or
    objective pinned one, else on the cell nearest the piece's centre. A connection A->B is
    written only when a grid move leads from A's piece into B's AND the straight line between
    the two waypoints is walkable by directed moves in the stricter of their two stances:
    the add-on steers in a straight line between waypoints.

    `openings` are cells walkable only once a state-changing brush is gone (a blown wall):
    a connection walking through one gets the profile's blockable flags on both ends, which
    the add-on re-tests at runtime by tracing between them. `exclude` cells get no waypoint
    and are never walked through (a breakable objective's own volume).
    """
    g = _Graph(grids, exclude)
    if not g.rank:
        raise BotNavError("the grids hold no walkable cell")
    cell = next(iter(grids.values())).cell
    k = max(1, round(spacing / cell))
    flags = {str(n): int(v) for n, v in bots["flags"].items()}
    floor_offset = float(bots["floor_offset"])

    # Pieces: bucket by (column block, stance rank), split by two-way connectivity in that rank.
    buckets: dict[tuple[int, int, int], list] = {}
    for c, r in g.rank.items():
        buckets.setdefault((c[0] // k, c[1] // k, r), []).append(c)
    piece_of: dict[tuple[int, int, int], int] = {}
    pieces: list[list[tuple[int, int, int]]] = []
    piece_rank: list[int] = []
    for key in sorted(buckets):
        members = set(buckets[key])
        r = key[2]
        for start in sorted(members):
            if start in piece_of:
                continue
            label = len(pieces)
            piece_of[start] = label
            todo, piece = deque([start]), []
            while todo:
                c = todo.popleft()
                piece.append(c)
                for d in g.nb(r, c):
                    if d in members and d not in piece_of and c in g.nb(r, d):
                        piece_of[d] = label
                        todo.append(d)
            pieces.append(sorted(piece))
            piece_rank.append(r)

    # Representatives: pinned cells win, then the cell nearest the centroid.
    chosen: dict[int, tuple[tuple[int, int, int], dict]] = {}
    unplaced: list[dict] = []
    for pin in pins or []:
        c = pin.get("cell")
        if c is None or c not in piece_of:
            unplaced.append({k2: v for k2, v in pin.items() if k2 != "cell"})
            continue
        chosen.setdefault(piece_of[c], (c, pin))
    reps: list[tuple[int, int, int]] = []
    names: list[str] = []
    for label, piece in enumerate(pieces):
        if label in chosen:
            reps.append(chosen[label][0])
            names.append(str(chosen[label][1].get("name") or ""))
            continue
        cx = sum(c[0] for c in piece) / len(piece)
        cy = sum(c[1] for c in piece) / len(piece)
        cz = sum(c[2] for c in piece) / len(piece)
        reps.append(
            min(
                piece,
                key=lambda c: ((c[0] - cx) ** 2 + (c[1] - cy) ** 2 + (c[2] - cz) ** 2, c),
            )
        )
        names.append("")

    # Candidate connections from grid moves between pieces, each checked as a straight walk.
    # The moves themselves are kept: where the straight line between the two waypoints cuts
    # a corner, a waypoint on the border cell they cross can still join them.
    borders: dict[tuple[int, int], list[tuple]] = {}
    for c, r in g.rank.items():
        a = piece_of[c]
        for rr in range(r, len(g.order)):
            for d in g.nb(rr, c):
                b = piece_of.get(d)
                if b is not None and b != a:
                    borders.setdefault((a, b), []).append((c, d))
    edges: dict[int, set[int]] = {}
    rejected = 0
    blockable: set[int] = set()
    node_rank = list(piece_rank)

    def link(x: int, y: int, r: int) -> None:
        edges.setdefault(x, set()).add(y)
        if openings and any(c in openings for c in g.walk(r, reps[x], reps[y]) or ()):
            blockable.update((x, y))

    portal_at: dict[tuple[int, int, int], int] = {}
    portals = 0
    for a, b in sorted(borders):
        r = max(piece_rank[a], piece_rank[b])
        if g.straight(r, reps[a], reps[b]):
            link(a, b, r)
            continue
        # The straight line cuts a corner: walk the grid inside the two pieces instead, and
        # pull that path taut into straight legs, with a waypoint at each turn.
        legs = _taut_path(g, r, reps[a], reps[b], set(pieces[a]) | set(pieces[b]))
        if legs is None:
            rejected += 1
            continue
        nodes = [a]
        for turn in legs[1:-1]:
            node = portal_at.get(turn)
            if node is None:
                node = portal_at[turn] = len(reps)
                reps.append(turn)
                names.append("")
                # The leg into a turn may cross the stricter piece's cells: flag it so.
                node_rank.append(max(r, g.rank[turn]))
                portals += 1
            nodes.append(node)
        nodes.append(b)
        for x, y in zip(nodes, nodes[1:], strict=False):
            link(x, y, r)

    waypoints: list[Waypoint] = []
    stance_of: list[str] = []
    for label, rep in enumerate(reps):
        stance = g.order[node_rank[label]]
        x, y, z = grids[stance].centre(rep)
        wp_flags = 0
        for name in (bots.get("stance_flags") or {}).get(stance, []):
            wp_flags |= flags[name]
        if label in blockable:
            for name in bots.get("blockable_flags") or []:
                wp_flags |= flags[name]
        waypoints.append(
            Waypoint(
                position=(x, y, z + floor_offset),
                uid=label + 1,
                flags=wp_flags,
                name=names[label],
                connections=sorted(edges.get(label, [])),
            )
        )
        stance_of.append(stance)

    sizes = [len(p) for p in pieces]
    report = {
        "waypoints": len(waypoints),
        "connections": sum(len(w.connections) for w in waypoints),
        "one_way_connections": sum(
            1
            for i, w in enumerate(waypoints)
            for t in w.connections
            if i not in waypoints[t].connections
        ),
        "candidate_connections_rejected": rejected,
        "border_waypoints": portals,
        "by_stance": {s: stance_of.count(s) for s in g.order},
        "blockable_waypoints": len(blockable),
        "spacing": k * cell,
        "cell": cell,
        "cells_per_waypoint": {"min": min(sizes), "max": max(sizes)},
        "unplaced_pins": unplaced,
        "components": components(waypoints),
    }
    return Seed(
        waypoints=waypoints, stance_of=stance_of, cells=reps, report=report, piece_of=piece_of
    )


def _taut_path(g: _Graph, r: int, start, goal, allowed: set) -> list | None:
    """A directed grid path from start to goal inside `allowed`, pulled taut: the fewest
    cells such that each consecutive pair is a straight walk. None if there is no path."""
    came = {start: None}
    todo = deque([start])
    while todo and goal not in came:
        cur = todo.popleft()
        for nxt in sorted(g.moves(r, cur)):
            if nxt in allowed and nxt not in came:
                came[nxt] = cur
                todo.append(nxt)
    if goal not in came:
        return None
    path = [goal]
    while came[path[-1]] is not None:
        path.append(came[path[-1]])
    path.reverse()
    taut = [path[0]]
    i = 0
    while i < len(path) - 1:
        j = len(path) - 1
        while j > i + 1 and not g.straight(r, path[i], path[j]):
            j -= 1
        if j == i + 1 and not g.straight(r, path[i], path[j]):
            return None
        taut.append(path[j])
        i = j
    return taut


def components(waypoints: list[Waypoint]) -> dict[str, Any]:
    """Weakly connected pieces of the waypoint graph, largest first."""
    adjacency: dict[int, set[int]] = {i: set() for i in range(len(waypoints))}
    for i, w in enumerate(waypoints):
        for t in w.connections:
            adjacency[i].add(t)
            adjacency[t].add(i)
    seen: set[int] = set()
    sizes = []
    for start in adjacency:
        if start in seen:
            continue
        seen.add(start)
        stack, size = [start], 0
        while stack:
            n = stack.pop()
            size += 1
            for m in adjacency[n]:
                if m not in seen:
                    seen.add(m)
                    stack.append(m)
        sizes.append(size)
    sizes.sort(reverse=True)
    return {"count": len(sizes), "sizes": sizes[:10], "isolated": sizes.count(1)}


def _weakly_connected(waypoints: list[Waypoint], starts: set[int]) -> set[int]:
    adjacency: dict[int, set[int]] = {i: set() for i in range(len(waypoints))}
    for i, w in enumerate(waypoints):
        for t in w.connections:
            adjacency[i].add(t)
            adjacency[t].add(i)
    seen = set(starts)
    stack = list(starts)
    while stack:
        for m in adjacency[stack.pop()]:
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen


def reachable(waypoints: list[Waypoint], start: int) -> set[int]:
    """Waypoints a bot can reach from `start` following directed connections."""
    seen = {start}
    stack = [start]
    while stack:
        n = stack.pop()
        for m in waypoints[n].connections:
            if m not in seen:
                seen.add(m)
                stack.append(m)
    return seen


# --------------------------------------------------------------------------------------
# A whole map: grids, pins at spawns and objectives, the graph, and its own check
# --------------------------------------------------------------------------------------


def _team_names(profile_id: str) -> dict[str, str]:
    teams = profiles.load(profile_id).get("teams") or {}
    return {
        str(side.get("label")): str(side.get("name") or side.get("label"))
        for side in teams.get("sides") or []
        if isinstance(side, dict)
    }


def _label(entity: dict, keys: list[str]) -> str:
    values = dict((str(k), str(v)) for k, v in entity.get("keys") or [])
    for key in keys:
        if values.get(key):
            return values[key]
    return ""


def _pin_cell(grids: dict[str, analysis.NavGrid], points, radius: float):
    """The walkable cell nearest any of `points`, standing preferred, and its distance."""
    for stance in STANCES:
        grid = grids.get(stance)
        if grid is None:
            continue
        hits = [grid.nearest_walkable(p, radius=radius) for p in points]
        hits = [h for h in hits if h is not None]
        if hits:
            return min(hits, key=lambda h: (h[1], h[0]))
    return None


def build(
    map_path: str | Path,
    game_map: Any,
    profile_id: str,
    *,
    cell: float,
    spacing: float | None = None,
) -> tuple[WaypointFile, dict[str, Any]]:
    """Seed the waypoint file for a map, and report how it was made and what it connects."""
    from . import routes  # routes imports analysis; keep this module importable on its own

    bots = spec(profile_id)
    if spacing is None:
        spacing = float(bots["default_spacing"])
    if not math.isfinite(spacing) or spacing < cell:
        raise BotNavError(f"spacing must be at least one cell ({cell:g}), got {spacing:g}")
    role_map = analysis.roles(profile_id)
    stances = routes.stances(profile_id, "mixed")
    entities = game_map.entities()
    changing = [
        int(e["index"])
        for e in entities
        if role_map.category_of.get(str(e.get("classname") or "")) in role_map.state_changing
    ]
    grids = {
        s: analysis.build_navgrid(
            map_path,
            cell=cell,
            profile_id=profile_id,
            game_map=game_map,
            stance=s,
            open_state_changing=True,
        )
        for s in stances
    }
    closed = (
        {
            s: analysis.build_navgrid(
                map_path, cell=cell, profile_id=profile_id, game_map=game_map, stance=s
            )
            for s in stances
        }
        if changing
        else None
    )

    exclude = _enclosed_openings(grids, closed) if closed else frozenset()
    openings = (
        frozenset(
            {c for g in grids.values() for c in g.walkable_cells()}
            - {c for g in closed.values() for c in g.walkable_cells()}
            - exclude
        )
        if closed
        else frozenset()
    )
    # Spawns and objectives are pinned where players stand when the round starts.
    start = closed or grids
    names = bots.get("names") or {}
    teams = _team_names(profile_id)
    by_index = {int(e["index"]): e for e in entities}
    spawns, objectives, _ = analysis._markers(game_map, role_map)
    radius = cell * 4
    pins: list[dict] = []
    for marker in objectives:
        points = (
            analysis._approach_points(marker.bounds, cell)
            if marker.bounds is not None
            else (marker.origin,)
        )
        label = _label(by_index[marker.entity], list(names.get("objective_keys") or []))
        # One pin per approach point (a brush objective's footprint centre and each side):
        # which of them a team can reach is the graph's to say, as it is balance_report's.
        hits = [h for h in (_pin_cell(start, (p,), radius) for p in points) if h is not None]
        kept: dict[tuple, float] = {}
        for c, distance in sorted(hits, key=lambda h: (h[1], h[0])):
            kept.setdefault(c, distance)
        for c, distance in list(kept.items()) or [(None, None)]:
            pins.append(
                {
                    "kind": "objective",
                    "entity": marker.entity,
                    "classname": marker.classname,
                    "team": marker.team,
                    "name": str(names.get("objective", "{classname} {label}"))
                    .format(classname=marker.classname, label=label or marker.entity)
                    .strip(),
                    "cell": c,
                    "snap": round(distance, 2) if distance is not None else None,
                }
            )
    for marker in spawns:
        hit = _pin_cell(start, (marker.origin,), radius)
        pins.append(
            {
                "kind": "spawn",
                "entity": marker.entity,
                "classname": marker.classname,
                "team": marker.team,
                "group": marker.group,
                "name": str(names.get("spawn", "{team} spawn")).format(
                    team=teams.get(marker.team, marker.team), group=marker.group
                ),
                "cell": hit[0] if hit else None,
                "snap": round(hit[1], 2) if hit else None,
            }
        )

    seeded = seed(grids, bots, spacing=spacing, pins=pins, openings=openings, exclude=exclude)
    for pin in pins:
        pin["waypoint"] = seeded.piece_of.get(pin["cell"]) if pin["cell"] is not None else None
    # Waypoints no spawn connects to in either direction (a ledge on a decorative pipe, a
    # sealed box) are only noise to the bots: drop them, keeping indices consistent.
    starts = {p["waypoint"] for p in pins if p["kind"] == "spawn" and p["waypoint"] is not None}
    pruned = 0
    if starts:
        keep = sorted(_weakly_connected(seeded.waypoints, starts))
        pruned = len(seeded.waypoints) - len(keep)
        if pruned:
            remap = {old: new for new, old in enumerate(keep)}
            kept = []
            for old in keep:
                w = seeded.waypoints[old]
                w.uid = remap[old] + 1
                w.connections = [remap[t] for t in w.connections if t in remap]
                kept.append(w)
            seeded.waypoints = kept
            seeded.stance_of = [seeded.stance_of[old] for old in keep]
            seeded.cells = [seeded.cells[old] for old in keep]
            for pin in pins:
                if pin["waypoint"] is not None:
                    pin["waypoint"] = remap.get(pin["waypoint"])
            seeded.report.update(
                waypoints=len(kept),
                connections=sum(len(w.connections) for w in kept),
                by_stance={s: seeded.stance_of.count(s) for s in stances},
                components=components(kept),
            )
    waypoints = seeded.waypoints

    spawn_wps: dict[str, set[int]] = {}
    for pin in pins:
        if pin["kind"] == "spawn" and pin["waypoint"] is not None:
            spawn_wps.setdefault(pin["team"], set()).add(pin["waypoint"])
    reach = {s: reachable(waypoints, s) for starts in spawn_wps.values() for s in starts}
    coverage: dict[int, dict] = {}
    for pin in pins:
        if pin["kind"] != "objective":
            continue
        row = coverage.setdefault(
            pin["entity"],
            {
                "entity": pin["entity"],
                "classname": pin["classname"],
                "name": pin["name"],
                "waypoints": [],
            },
        )
        if pin["waypoint"] is not None and pin["waypoint"] not in row["waypoints"]:
            row["waypoints"].append(pin["waypoint"])
    for row in coverage.values():
        for team, starts in sorted(spawn_wps.items()):
            row[f"reached_from_{team}_spawns"] = any(
                w in reach[s] for s in starts for w in row["waypoints"]
            )

    lo = next(iter(grids.values())).origin
    dims = next(iter(grids.values())).dims
    hi = tuple(lo[i] + dims[i] * cell for i in range(3))
    header = bots["format"].get("header") or {}
    wf = WaypointFile(
        version=int(bots["format"]["version"]),
        waypoints=waypoints,
        comments=str(header.get("comment", ""))
        .format(cell=cell, spacing=spacing)
        .encode("latin-1"),
        world_mins=tuple(float(v) for v in lo),
        world_maxs=tuple(float(v) for v in hi),
    )
    flags = {str(k): int(v) for k, v in bots["flags"].items()}
    report = {
        **seeded.report,
        "pruned_unconnected_to_spawns": pruned,
        "stances": list(stances),
        "state_changing_entities": changing,
        "state_changing_cells_skipped": len(exclude),
        "pins": [{k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()} for p in pins],
        "objectives": list(coverage.values()),
        "spawn_waypoints": {t: sorted(v) for t, v in sorted(spawn_wps.items())},
        "flag_counts": {
            name: sum(1 for w in waypoints if w.flags & bit) for name, bit in flags.items()
        },
    }
    return wf, report


def _enclosed_openings(grids: dict, closed: dict) -> frozenset:
    """Cells walkable only once state-changing brushes are gone, that join nothing.

    A blown wall or door opens a way between places that were apart: its cells stay, and
    connections through it are flagged blockable. A breakable objective (a radio, a crate)
    opens nothing; its volume would only add waypoints inside a solid box that the add-on
    then picks as the nearest waypoint to the objective. Each connected patch of open-only
    cells is kept when its rim touches two or more pieces of the start-of-round space.
    """
    labels = next(iter(closed.values())).components()
    first = next(iter(closed.values()))
    union = {c for g in closed.values() for c in g.walkable_cells()}
    opened = {c for g in grids.values() for c in g.walkable_cells()} - union
    graph = _Graph(grids)
    out: set = set()
    seen: set = set()
    for start in sorted(opened):
        if start in seen:
            continue
        patch, rim, todo = [], set(), [start]
        seen.add(start)
        while todo:
            c = todo.pop()
            patch.append(c)
            for r in range(len(graph.order)):
                for d in graph.nb(r, c):
                    if d in opened and d not in seen:
                        seen.add(d)
                        todo.append(d)
                    elif d in union:
                        rim.add(labels.get(d, -1) if first.is_walkable(d) else -1)
        if len(rim - {-1}) < 2:
            out.update(patch)
    return frozenset(out)


def install_path(bots: dict[str, Any], mapname: str) -> Path:
    """Where the add-on looks for a map's waypoints, relative to its own folder."""
    return Path(str(bots["install_path"]).format(map=mapname))
