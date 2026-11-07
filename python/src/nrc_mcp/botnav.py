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

Jumps and ladders, which the walk grids cannot express, are added after seeding
(`add_traversals`): a ledge within the jump's reach or a gap the player box's arc clears gets
take-off, landing and one-way jump links, and a ladder brush gets climb waypoints at its foot
and top.

Brushes that change state during a round (doors, blown walls, constructibles) are seeded in
every state at once and gated with the add-on's own runtime tests, as the profile describes:
a doorway waypoint that makes the bot press use, wall links closed while a trace hits solid,
bridge links closed while nothing is under their midpoint.

What a seeded graph is not: a hand-tuned waypoint file. The add-on's objective goals come
from the game itself (objective entities the game reports to it), and they need only
reachable waypoints near them. Sniping and camping spots and the per-map script are
`botgoals`'s, written beside this file.
"""

from __future__ import annotations

import math
import struct
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import analysis, profiles
from .rules import parse_spawnflags


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

    def centre(self, cell) -> tuple[float, float, float]:
        return self.grids[self.order[0]].centre(cell)

    def column_at(self, x: float, y: float) -> tuple[int, int]:
        grid = self.grids[self.order[0]]
        return (
            math.floor((x - grid.origin[0]) / grid.cell),
            math.floor((y - grid.origin[1]) / grid.cell),
        )

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
    doors: dict | None = None,
    decks: frozenset = frozenset(),
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

    `doors` maps the cells inside a door a player opens to the flag bits its waypoint takes:
    a piece holding such cells gets its waypoint in the doorway, carrying them, so a bot
    heading for it from either side opens the door. `decks` are cells walkable only once a
    constructible is built: a connection crossing them gets the profile's bridge flags on
    both ends, and is split at the deck's edges where its midpoint would not be over the deck
    (the add-on decides from the midpoint, see `_over_deck`).
    """
    doors = doors or {}
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
        if c in decks or c in openings:
            continue  # no waypoint stands on a deck or in a wall: see the spans below
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
    door_rep = _door_reps(pieces, doors) if doors else {}
    reps: list[tuple[int, int, int]] = []
    names: list[str] = []
    for label, piece in enumerate(pieces):
        if label in chosen:
            reps.append(chosen[label][0])
            names.append(str(chosen[label][1].get("name") or ""))
            continue
        if label in door_rep:
            reps.append(door_rep[label])
            names.append("")
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
        a = piece_of.get(c)
        if a is None:
            continue
        for rr in range(r, len(g.order)):
            for d in g.nb(rr, c):
                b = piece_of.get(d)
                if b is not None and b != a:
                    borders.setdefault((a, b), []).append((c, d))
    edges: dict[int, set[int]] = {}
    rejected = 0
    blockable: set[int] = set()
    node_rank = list(piece_rank)

    bridged: set[int] = set()
    door_crossings: list[tuple[int, int, list]] = []
    gate = {"bridge_splits": 0, "bridge_rejected": 0}
    deck_floors: dict[tuple[int, int], list[float]] = {}
    for c in decks:
        deck_floors.setdefault((c[0], c[1]), []).append(g.centre(c)[2])
    depth = float(bots.get("bridge_trace_depth") or 0)

    def node_at(turn, r: int) -> int:
        nonlocal portals
        node = portal_at.get(turn)
        if node is None:
            node = portal_at[turn] = len(reps)
            reps.append(turn)
            names.append("")
            # The leg into a turn may cross the stricter piece's cells: flag it so.
            node_rank.append(max(r, g.rank[turn]))
            portals += 1
        return node

    def over_deck(p, q) -> bool:
        # Both ends stand floor_offset above their floor; the add-on traces `depth` down.
        (ax, ay, az), (bx, by, bz) = g.centre(p), g.centre(q)
        mz = (az + bz) / 2 + floor_offset
        column = g.column_at((ax + bx) / 2, (ay + by) / 2)
        return any(mz - depth <= z <= mz for z in deck_floors.get(column, ()))

    def plain(x: int, y: int, cells: list) -> None:
        edges.setdefault(x, set()).add(y)
        if openings and any(c in openings for c in cells):
            blockable.update((x, y))
        if doors and any(c in doors for c in cells):
            door_crossings.append((x, y, cells))

    def plan(p, q, r: int) -> list | None:
        """The legs a connection p -> q is written as: itself, or split at the deck's
        edges so the leg across the deck is the one the add-on tests. None when a leg that
        touches the deck cannot be tested (stepping off its side, say)."""
        cells = g.walk(r, p, q) or []
        if not decks or not any(c in decks for c in cells) or over_deck(p, q):
            return [(p, q, cells, bool(decks) and any(c in decks for c in cells))]
        stops = [p]
        for before, after in zip(cells, cells[1:], strict=False):
            if (before in decks) != (after in decks):
                edge = after if before in decks else before
                if edge != stops[-1]:
                    stops.append(edge)
        if stops[-1] != q:
            stops.append(q)
        legs = []
        for u, v in zip(stops, stops[1:], strict=False):
            walked = g.walk(r, u, v)
            on_deck = walked is not None and any(c in decks for c in walked)
            if walked is None or (on_deck and not over_deck(u, v)):
                return None
            legs.append((u, v, walked, on_deck))
        return legs

    def apply(x: int, y: int, r: int, legs: list) -> None:
        if len(legs) > 1:
            gate["bridge_splits"] += 1
        nodes = [x, *(node_at(u, r) for u, *_ in legs[1:]), y]
        for (_u, _v, walked, on_deck), a, b in zip(legs, nodes, nodes[1:], strict=False):
            plain(a, b, walked)
            if on_deck:
                bridged.update((a, b))

    portal_at: dict[tuple[int, int, int], int] = {}
    portals = 0
    for a, b in sorted(borders):
        r = max(piece_rank[a], piece_rank[b])
        if g.straight(r, reps[a], reps[b]):
            legs = plan(reps[a], reps[b], r)
            if legs is None:
                gate["bridge_rejected"] += 1
            else:
                apply(a, b, r, legs)
            continue
        # The straight line cuts a corner: walk the grid inside the two pieces instead, and
        # pull that path taut into straight legs, with a waypoint at each turn.
        turns = _taut_path(g, r, reps[a], reps[b], set(pieces[a]) | set(pieces[b]))
        if turns is None:
            rejected += 1
            continue
        plans = [plan(u, v, r) for u, v in zip(turns, turns[1:], strict=False)]
        if any(legs is None for legs in plans):
            gate["bridge_rejected"] += 1
            continue
        nodes = [a, *(node_at(turn, r) for turn in turns[1:-1]), b]
        for legs, x, y in zip(plans, nodes, nodes[1:], strict=False):
            apply(x, y, r, legs)

    # Bridges and walls. The add-on sends a bot to the waypoint nearest a goal, closed links
    # or not (PathPlannerWaypoint.cpp:1500-1515 filters only closed waypoints), so a waypoint
    # on a deck or inside a wall would be the goal of the objective that opens it, and
    # unreachable until it is open. No piece stands on either; instead each pair of pieces on
    # its edges is joined by one link straight across, from lip to lip, as stock files span a
    # bridge (battery 18-30) or a wall (battery 371-373).
    spans = {"deck": 0, "wall": 0}
    for kind, cells in (("deck", decks), ("wall", openings)):
        if cells:
            spans[kind] = _span(
                g, cells, pieces, piece_of, piece_rank, reps, plan, apply, node_at, gate
            )

    # A connection through a door that does not end in the doorway: the bot presses use on
    # it too. A team door's connections must end in the doorway, where the team flags are.
    door_bits = {label: doors[rep] for label, rep in enumerate(reps) if rep in doors}
    door_plain = 0
    for flag in bots.get("doors", {}).get("flags") or []:
        door_plain |= flags[flag]
    team_bits = 0
    for names_ in (bots.get("doors", {}).get("team_flags") or {}).values():
        for flag in names_:
            team_bits |= flags[flag]
    dropped_door = 0
    for x, y, cells in door_crossings:
        if x in door_bits or y in door_bits:
            continue
        if any(doors[c] & team_bits for c in cells if c in doors):
            edges[x].discard(y)
            dropped_door += 1
            continue
        door_bits[x] = door_bits.get(x, 0) | door_plain
        door_bits[y] = door_bits.get(y, 0) | door_plain

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
        if label in bridged:
            for name in bots.get("bridge_flags") or []:
                wp_flags |= flags[name]
        wp_flags |= door_bits.get(label, 0)
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
        "bridge_waypoints": len(bridged),
        "bridge_connections_split": gate["bridge_splits"],
        "bridge_connections_rejected": gate["bridge_rejected"],
        "deck_spans": spans["deck"],
        "wall_spans": spans["wall"],
        "door_waypoints": len(door_bits),
        "team_door_bypasses_dropped": dropped_door,
        "spacing": k * cell,
        "cell": cell,
        "cells_per_waypoint": {"min": min(sizes), "max": max(sizes)},
        "unplaced_pins": unplaced,
        "components": components(waypoints),
    }
    return Seed(
        waypoints=waypoints, stance_of=stance_of, cells=reps, report=report, piece_of=piece_of
    )


def _span(g, decks, pieces, piece_of, piece_rank, reps, plan, apply, node_at, gate) -> int:
    """Join the pieces around each patch of `decks` (deck or wall cells) by links straight
    across it, lip to lip (see `seed`); of the candidates for a pair of pieces, the one
    that keeps furthest from the patch's sides, then the shortest. Returns the spans made."""
    made = 0
    seen: set = set()
    banks = {(c[0], c[1]) for c in piece_of}
    for start in sorted(decks):
        if start in seen:
            continue
        patch, todo = set(), [start]
        seen.add(start)
        while todo:
            c = todo.pop()
            patch.add(c)
            for r in range(len(g.order)):
                for d in g.nb(r, c):
                    if d in decks and d not in seen:
                        seen.add(d)
                        todo.append(d)
        columns = {(c[0], c[1]) for c in patch}
        # How far each column is from the patch's sides (0 on the side): where it ends in
        # neither more of it nor ground a piece stands on (the drop off a deck's edge).
        held = columns | banks
        side = {
            (x, y): 0
            for x, y in columns
            if any((x + dx, y + dy) not in held for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)))
        }
        ring = deque(side)
        while ring:
            x, y = ring.popleft()
            for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if n in columns and n not in side:
                    side[n] = side[(x, y)] + 1
                    ring.append(n)
        # Bank cells a move leads to or from, per piece.
        edge: dict[int, set] = {}
        for c in patch:
            for r in range(len(g.order)):
                for d in g.nb(r, c):
                    if d in piece_of:
                        edge.setdefault(piece_of[d], set()).add(d)
        for a in sorted(edge):
            for b in sorted(edge):
                if a == b:
                    continue
                r = max(piece_rank[a], piece_rank[b])
                best = None
                for ea in sorted(edge[a]):
                    for eb in sorted(edge[b]):
                        legs = plan(ea, eb, r)
                        if not legs or len(legs) != 1:
                            continue
                        walked = [c for c in legs[0][2] if c in patch]
                        if not walked:
                            continue
                        score = (
                            -min(side[(c[0], c[1])] for c in walked),
                            math.dist(g.centre(ea), g.centre(eb)),
                        )
                        if best is None or score < best[0]:
                            best = (score, ea, eb, legs)
                if best is None:
                    continue
                _length, ea, eb, legs = best
                ends = []
                for piece, lip in ((a, ea), (b, eb)):
                    turns = (
                        [reps[piece], lip]
                        if g.straight(r, reps[piece], lip) or g.straight(r, lip, reps[piece])
                        else _taut_path(g, r, reps[piece], lip, set(pieces[piece]))
                    )
                    ends.append(turns)
                if ends[0] is None or ends[1] is None:
                    gate["bridge_rejected"] += 1
                    continue
                # rep_a -> ... -> lip_a, across, lip_b -> ... -> rep_b
                path = [*ends[0], *reversed(ends[1])]
                nodes = [a, *(node_at(c, r) for c in path[1:-1]), b]
                for (u, v), x, y in zip(
                    zip(path, path[1:], strict=False), nodes, nodes[1:], strict=False
                ):
                    hop = plan(u, v, r)
                    if hop is not None:
                        apply(x, y, r, hop)
                made += 1
    return made


def _door_reps(pieces: list[list], doors: dict) -> dict[int, tuple[int, int, int]]:
    """For each piece holding door cells, the door cell nearest the middle of them: the
    piece's waypoint goes in the doorway, where the bot heading for it meets the door."""
    out: dict[int, tuple[int, int, int]] = {}
    for label, piece in enumerate(pieces):
        inside = [c for c in piece if c in doors]
        if not inside:
            continue
        cx = sum(c[0] for c in inside) / len(inside)
        cy = sum(c[1] for c in inside) / len(inside)
        cz = sum(c[2] for c in inside) / len(inside)
        out[label] = min(
            inside, key=lambda c: ((c[0] - cx) ** 2 + (c[1] - cy) ** 2 + (c[2] - cz) ** 2, c)
        )
    return out


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
# Jumps and ladders: the links the walk grids cannot make
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Physics:
    """The movement figures a jump needs, all from the profile's movement section."""

    jump_velocity: float
    gravity: float  # what an airborne body falls at (the profile's effective gravity)
    speeds: tuple[float, ...]  # horizontal take-off speeds every gap arc must work at
    separation: float
    safe_drop: float
    step: float
    width: float
    # The highest ledge a jump lands on (the profile's jump_up_max: the rise plus the step-up
    # a falling body gets at the riser), or the bare rise when the profile states none.
    ledge_max: float | None = None

    @property
    def apex(self) -> float:
        return self.jump_velocity**2 / (2 * self.gravity)

    @property
    def ledge(self) -> float:
        return self.apex if self.ledge_max is None else self.ledge_max

    def rise(self, t: float) -> float:
        return self.jump_velocity * t - 0.5 * self.gravity * t * t


def _physics(profile_id: str, jumps: dict) -> _Physics | None:
    """None when the profile lacks a figure: no jump is then guessed at."""
    section = profiles.movement(profile_id).get("movement") or {}

    def number(key: str, attr: str) -> float | None:
        row = section.get(key)
        v = row.get(attr) if isinstance(row, dict) else None
        ok = isinstance(v, int | float) and math.isfinite(v) and v > 0
        return float(v) if ok else None

    jump = number("jump_velocity", "value")
    # `effective` where the game's movement code changes what bodies fall at (ET's fixed
    # physics: 750 for g_gravity 800); the arcs follow what the engine does.
    gravity = number("gravity", "effective") or number("gravity", "default")
    run = number("run_speed", "value")
    mv = analysis.movement_constants(profile_id)
    if None in (jump, gravity, run) or mv.player_width is None:
        return None
    scales = [float(s) for s in jumps.get("speed_scales") or [] if float(s) > 0]
    if not scales:
        return None
    return _Physics(
        jump_velocity=jump,
        gravity=gravity,
        speeds=tuple(sorted(run * s for s in scales)),
        separation=mv.floor_separation.value if mv.floor_separation else 0.0,
        safe_drop=mv.fall_no_damage.value if mv.fall_no_damage else mv.step_height.value,
        step=mv.step_height.value,
        width=mv.player_width.value,
        ledge_max=mv.jump_up_max.value if mv.jump_up_max else None,
    )


def _floor(grid: analysis.NavGrid, cell, x: float | None = None, y: float | None = None) -> float:
    """The exact top of the floor under a walkable cell (at its centre, or at x, y)."""
    cx, cy, cz = grid.centre(cell)
    x = cx if x is None else x
    y = cy if y is None else y
    intervals = analysis.column_intervals(grid.solids, grid.index, x, y)
    found, _ = analysis.floor_and_ceiling(intervals, cz + grid.cell / 2)
    return cz if found is None else found


def _swept(grid: analysis.NavGrid, points: list[tuple[float, float, float]]) -> bool:
    """The standing box, feet at each point in turn, meets nothing along the polyline."""
    return all(
        analysis.segment_clear(grid.clearance_solids, grid.clearance_index, a, b)
        for a, b in zip(points, points[1:], strict=False)
        if a != b
    )


def _arc(
    grid: analysis.NavGrid,
    phys: _Physics,
    start: tuple[float, float, float],
    direction: tuple[float, float],
    speed: float,
    lowest: float,
    dt: float = 0.02,
):
    """Sweep the standing box along a jump's ballistic arc from `start` (feet).

    Returns (landing cell, feet point) where the arc first comes down onto a walkable floor
    with the box's centre over it, or None when it strikes anything on the way up, hits a
    wall or edge coming down, or falls below `lowest`.
    """
    sx, sy, sz = start
    p = start
    t = 0.0
    while True:
        t += dt
        q = (
            sx + direction[0] * speed * t,
            sy + direction[1] * speed * t,
            sz + phys.rise(t),
        )
        if q[2] < lowest:
            return None
        if analysis.segment_clear(grid.clearance_solids, grid.clearance_index, p, q):
            p = q
            continue
        if phys.jump_velocity - phys.gravity * t >= 0:
            return None  # struck something while still rising
        intervals = analysis.column_intervals(grid.solids, grid.index, p[0], p[1])
        floor, _ = analysis.floor_and_ceiling(intervals, p[2] + 0.5)
        if floor is None or floor < q[2] - 0.5:
            return None  # the box met an edge or a wall, not a floor under its centre
        ix, iy, _ = grid.cell_at((p[0], p[1], floor))
        for iz in grid.columns.get((ix, iy), ()):
            if abs(grid.centre((ix, iy, iz))[2] - floor) <= grid.cell / 2 + 1e-6:
                return (ix, iy, iz), (p[0], p[1], floor + phys.separation)
        return None


class _Extend:
    """Appends waypoints and connections to a seeded graph, keeping its lists in step."""

    def __init__(self, seeded: Seed, grid: analysis.NavGrid, graph: _Graph, floor_offset: float):
        self.s = seeded
        self.grid = grid
        self.g = graph
        self.floor_offset = floor_offset
        self.turns: dict[tuple[int, int, int], int] = {}
        self.members: dict[int, set] = {}
        for c, label in seeded.piece_of.items():
            self.members.setdefault(label, set()).add(c)

    def node(self, cell, *, floor: float | None = None, flags: int = 0, radius=None) -> int:
        x, y, z = self.grid.centre(cell)
        index = len(self.s.waypoints)
        self.s.waypoints.append(
            Waypoint(
                position=(x, y, (z if floor is None else floor) + self.floor_offset),
                uid=index + 1,
                flags=flags,
                radius=radius,
            )
        )
        self.s.stance_of.append("stand")
        self.s.cells.append(cell)
        return index

    def link(self, a: int, b: int) -> None:
        conns = self.s.waypoints[a].connections
        if a != b and b not in conns:
            conns.append(b)
            conns.sort()

    def legs(self, a, b, allowed) -> list | None:
        if a == b:
            return [a]
        if self.g.straight(0, a, b):
            return [a, b]
        return _taut_path(self.g, 0, a, b, allowed)

    def walk(self, a: int, b: int) -> bool:
        """Join waypoint a to b by straight standing walks inside a's piece (or b's), adding
        a plain waypoint at each turn."""
        ca, cb = self.s.cells[a], self.s.cells[b]
        piece = self.s.piece_of.get(ca, self.s.piece_of.get(cb))
        allowed = self.members.get(piece, set()) | {ca, cb}
        legs = self.legs(ca, cb, allowed)
        if legs is None:
            return False
        nodes = [a]
        for turn in legs[1:-1]:
            if turn not in self.turns:
                self.turns[turn] = self.node(turn)
            nodes.append(self.turns[turn])
        nodes.append(b)
        for x, y in zip(nodes, nodes[1:], strict=False):
            self.link(x, y)
        return True

    def attach(self, index: int) -> bool:
        """Walk both ways between a new waypoint and its own piece's waypoint."""
        piece = self.s.piece_of.get(self.s.cells[index])
        if piece is None:
            return False
        return self.walk(piece, index) and self.walk(index, piece)


def _distances(waypoints: list[Waypoint], start: int) -> dict[int, float]:
    """Shortest directed travel from `start` over the connections, in world units."""
    import heapq  # noqa: PLC0415

    best = {start: 0.0}
    todo = [(0.0, start)]
    while todo:
        d, n = heapq.heappop(todo)
        if d > best.get(n, math.inf):
            continue
        for m in waypoints[n].connections:
            nd = d + math.dist(waypoints[n].position, waypoints[m].position)
            if nd < best.get(m, math.inf):
                best[m] = nd
                heapq.heappush(todo, (nd, m))
    return best


def _jump_candidates(grid: analysis.NavGrid, piece_of: dict, phys: _Physics, margin: float):
    """(take-off cell, landing cell, direction, columns apart) for every brink of the
    standing grid: a cell whose next column offers no walk at its level, scanned along the
    direction for the first walkable floor of each other piece within jump reach."""
    cell = grid.cell
    rise_cells = math.ceil((phys.ledge - margin) / cell)
    drop_cells = max(0, math.floor((phys.safe_drop - phys.apex) / cell))
    reach = max(phys.speeds) * 2 * phys.jump_velocity / phys.gravity + phys.width
    k_max = max(2, math.ceil(reach / cell))
    headroom = grid.headroom_cells
    for a in grid.walkable_cells():
        pa = piece_of.get(a)
        if pa is None:
            continue
        ix, iy, iz = a
        for ux, uy in analysis._NEIGHBOURS:
            nxt = grid.columns.get((ix + ux, iy + uy), ())
            if any(abs(z - iz) <= grid.step_up_cells for z in nxt):
                continue
            seen = {pa}
            for k in range(2, k_max + 1):
                cx, cy = ix + ux * k, iy + uy * k
                for z in grid.columns.get((cx, cy), ()):
                    c = (cx, cy, z)
                    pc = piece_of.get(c)
                    if pc is None or pc in seen or not -drop_cells <= z - iz <= rise_cells:
                        continue
                    seen.add(pc)
                    yield a, c, (ux, uy), k
                # A wall above jump height, or a ceiling in the way: nothing beyond counts.
                if any(
                    grid.is_solid(cx, cy, z)
                    for z in range(iz + rise_cells + 1, iz + rise_cells + headroom)
                ):
                    break


def _jump(ext: _Extend, phys: _Physics, cfg: dict, a, c, d, k) -> dict | None:
    """Test one brink: as a ledge (climb its face) or as a gap (the arc). None if neither."""
    grid = ext.grid
    norm = math.hypot(*d)
    unit = (d[0] / norm, d[1] / norm)
    margin = float(cfg.get("margin", 0))
    radius = float(cfg.get("radius", 16))
    fa = _floor(grid, a)
    fc = _floor(grid, c)
    pa = grid.centre(a)
    pc = grid.centre(c)
    feet_a = (pa[0], pa[1], fa + phys.separation)
    feet_c = (pc[0], pc[1], fc + phys.separation)
    rise = fc - fa
    # Ledge: the floor runs up to a riser (or the riser starts right away) and its top is
    # within the jump's reach: the body slides up the face, steps up at the apex and across.
    floor_runs = all(
        grid.is_solid(a[0] + d[0] * i, a[1] + d[1] * i, a[2] - 1)
        or grid.is_solid(a[0] + d[0] * i, a[1] + d[1] * i, a[2])
        for i in range(1, k)
    )
    kind = landing = None
    near = math.dist(pa[:2], pc[:2]) <= phys.width + 2 * grid.cell
    if floor_runs and near and phys.step < rise <= phys.ledge - margin:
        top = fc + phys.separation + 1.0
        if _swept(grid, [feet_a, (pa[0], pa[1], top), (pc[0], pc[1], top), feet_c]):
            kind, landing = "ledge", (c, fc)
    if kind is None:
        # Gap: every take-off point within the radius, at every speed, lands in one piece.
        lowest = fa - (phys.safe_drop - phys.apex)
        landings = []
        for offset in (radius, radius / 2, 0.0):
            sx, sy = pa[0] - unit[0] * offset, pa[1] - unit[1] * offset
            if abs(_floor(grid, a, sx, sy) - fa) > 0.5:
                return None
            for speed in phys.speeds:
                hit = _arc(grid, phys, (sx, sy, fa + phys.separation), unit, speed, lowest)
                if hit is None:
                    return None
                landings.append(hit)
        far = max(landings, key=lambda h: (h[1][0] - pa[0]) * unit[0] + (h[1][1] - pa[1]) * unit[1])
        # The landing waypoint is the farthest touch-down; the shorter ones walk on to it.
        pieces = {ext.s.piece_of.get(h[0]) for h in landings}
        if None in pieces or ext.s.piece_of[a] in pieces:
            return None
        if not all(ext.g.straight(0, h[0], far[0]) for h in landings):
            return None
        kind, landing = "gap", (far[0], far[1][2] - phys.separation)
        rise = landing[1] - fa
    # A run-up in line with the jump, so the bot takes off moving the right way.
    runup = None
    for m in range(max(1, math.ceil(float(cfg.get("runup", 0)) / (grid.cell * norm))), 0, -1):
        r = (a[0] - d[0] * m, a[1] - d[1] * m, a[2])
        if grid.is_walkable(r) and r in ext.s.piece_of and ext.g.straight(0, r, a):
            runup = r
            break
    return {
        "kind": kind,
        "takeoff": a,
        "takeoff_floor": fa,
        "landing": landing[0],
        "landing_floor": landing[1],
        "runup": runup,
        "rise": round(rise, 2),
        "distance": round(math.dist(pa[:2], grid.centre(landing[0])[:2]), 2),
    }


def _ladder_boxes(grid: analysis.NavGrid, parms: dict, names: set[str]) -> list[tuple]:
    """Bounds of each ladder: brushes with a ladder surfaceparm face, touching ones merged."""
    from .optimize import SHADER_NAME_PREFIX  # noqa: PLC0415

    boxes = []
    for s in grid.solids:
        if s.patch:
            continue
        if any(set(parms.get((SHADER_NAME_PREFIX + sh).lower(), ())) & names for sh in s.shaders):
            boxes.append([list(s.mins), list(s.maxs)])
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                if all(a[0][n] <= b[1][n] + 1 and b[0][n] <= a[1][n] + 1 for n in range(3)):
                    boxes[i] = [
                        [min(a[0][n], b[0][n]) for n in range(3)],
                        [max(a[1][n], b[1][n]) for n in range(3)],
                    ]
                    del boxes[j]
                    merged = True
                    break
            if merged:
                break
    return [(tuple(lo), tuple(hi)) for lo, hi in sorted(boxes)]


def _cells_in(grid: analysis.NavGrid, lo: tuple[float, float], hi: tuple[float, float]):
    x0, y0, _ = grid.cell_at((lo[0], lo[1], 0))
    x1, y1, _ = grid.cell_at((hi[0], hi[1], 0))
    for ix in range(x0, x1 + 1):
        for iy in range(y0, y1 + 1):
            for iz in grid.columns.get((ix, iy), ()):
                yield (ix, iy, iz)


def _ladder(ext: _Extend, phys: _Physics, cfg: dict, box) -> dict:
    """The foot and top waypoints of one ladder, or why there are none."""
    grid = ext.grid
    mins, maxs = box
    thin = 0 if maxs[0] - mins[0] <= maxs[1] - mins[1] else 1
    wide = 1 - thin
    half = phys.width / 2
    reach = float(cfg.get("reach", 64))
    window = float(cfg.get("top_window", 48))
    headroom = grid.movement.headroom_stand.value
    row = {"mins": list(mins), "maxs": list(maxs)}
    for sign in (1, -1):
        face = maxs[thin] if sign > 0 else mins[thin]
        span = (mins[wide] - grid.cell / 2, maxs[wide] + grid.cell / 2)
        mid = (mins[wide] + maxs[wide]) / 2

        def region(near: float, far: float, face=face, sign=sign, span=span):
            lo, hi = [0.0, 0.0], [0.0, 0.0]
            lo[thin], hi[thin] = sorted((face + sign * near, face + sign * far))
            lo[wide], hi[wide] = span
            return _cells_in(grid, tuple(lo), tuple(hi))

        feet = []
        for c in region(0, half + reach):
            centre = grid.centre(c)
            f = _floor(grid, c)
            if mins[2] <= f + headroom / 2 and maxs[2] >= f + phys.step + 1:
                feet.append(((centre[thin] - face) * sign, abs(centre[wide] - mid), c, f))
        for _dist, _off, foot, ffoot in sorted(feet):
            tops = []
            for c in region(-reach, grid.cell / 2):
                f = _floor(grid, c)
                if not maxs[2] - window <= f <= maxs[2] + phys.step or f <= ffoot + phys.step:
                    continue
                if ext.s.piece_of.get(c) in (None, ext.s.piece_of.get(foot)):
                    continue
                back = (face - grid.centre(c)[thin]) * sign
                tops.append((back < half, abs(back - half), abs(grid.centre(c)[wide] - mid), c, f))
            for *_rank, top, ftop in sorted(tops):
                pf, pt = grid.centre(foot), grid.centre(top)
                at_face = [0.0, 0.0]
                at_face[thin] = face + sign * (half + 0.5)
                at_face[wide] = min(max(pf[wide], mins[wide]), maxs[wide])
                over = max(ftop, maxs[2]) + phys.separation + 0.5
                if over - ftop > phys.safe_drop:
                    continue
                path = [
                    (pf[0], pf[1], ffoot + phys.separation),
                    (at_face[0], at_face[1], ffoot + phys.separation),
                    (at_face[0], at_face[1], over),
                    (pt[0], pt[1], over),
                    (pt[0], pt[1], ftop + phys.separation),
                ]
                if _swept(grid, path):
                    return {**row, "foot": foot, "foot_floor": ffoot, "top": top, "top_floor": ftop}
        if feet:
            row.setdefault("reason", "no top floor the standing box reaches up the face")
    row.setdefault("reason", "no walkable floor in front of a face")
    return row


def add_traversals(
    seeded: Seed,
    grids: dict[str, analysis.NavGrid],
    bots: dict[str, Any],
    profile_id: str,
    map_path: str | Path | None = None,
) -> dict[str, Any]:
    """Add the jump and ladder links the walk grids cannot make, in place.

    Jumps (profile `bots.jumps`): every brink of the standing grid is scanned for a floor of
    another area within jump reach; a pair of areas whose walking route is missing or a long
    detour gets its first brink that passes as a ledge (rise within the jump's reach, `movement.jump_up_max`
    less the margin, the box sweeps up the face and across) or as a gap (the box's ballistic arc lands in the far area from
    every take-off point and at every speed). It becomes a run-up waypoint, a take-off
    waypoint flagged to jump, and a one-way link to a landing waypoint.

    Ladders (profile `bots.ladders`): brushes with a ladder surfaceparm get a foot waypoint
    in front of the climbing face and a top waypoint on the floor at its top, flagged and
    linked both ways, when the box sweeps from the foot up the face onto that floor.
    """
    out: dict[str, Any] = {"jumps": [], "ladders": []}
    grid = grids.get("stand")
    jumps_cfg = bots.get("jumps")
    ladders_cfg = bots.get("ladders")
    if grid is None or grid.clearance_index is None or grid.index is None:
        return out
    phys = _physics(profile_id, jumps_cfg or {"speed_scales": [1]})
    if phys is None:
        out["skipped"] = (
            "the profile's movement section lacks a jump, gravity, speed or width figure"
        )
        return out
    flags = {str(n): int(v) for n, v in bots["flags"].items()}

    def bits(names) -> int:
        value = 0
        for name in names or []:
            value |= flags[str(name)]
        return value

    graph = _Graph({"stand": grid})
    ext = _Extend(seeded, grid, graph, float(bots["floor_offset"]))

    if isinstance(jumps_cfg, dict):
        margin = float(jumps_cfg.get("margin", 0))
        detour = float(jumps_cfg.get("detour", 4))
        tries = int(jumps_cfg.get("tries", 8))
        radius = float(jumps_cfg.get("radius", 16))
        takeoff_flags = bits(jumps_cfg.get("takeoff_flags"))
        landing_flags = jumps_cfg.get("landing_flags") or {}
        pairs: dict[tuple[int, int], list] = {}
        for a, c, d, k in _jump_candidates(grid, seeded.piece_of, phys, margin):
            key = (seeded.piece_of[a], seeded.piece_of[c])
            home = seeded.cells[key[0]]
            pairs.setdefault(key, []).append(
                (k * math.hypot(*d), abs(d[0]) + abs(d[1]), math.dist(a, home), a, c, d, k)
            )
        # Detours are judged on the graph as it grows: once one jump serves a stretch of
        # ledge, its neighbours walk to it rather than getting their own.
        for (pa, _pc), cands in sorted(pairs.items()):
            for *_rank, a, c, d, k in sorted(cands)[:tries]:
                found = _jump(ext, phys, jumps_cfg, a, c, d, k)
                if found is None:
                    continue
                pb = seeded.piece_of[found["landing"]]
                w = seeded.waypoints
                straight = math.dist(w[pa].position, w[pb].position)
                if _distances(w, pa).get(pb, math.inf) <= detour * straight:
                    break
                takeoff = ext.node(
                    a, floor=found["takeoff_floor"], flags=takeoff_flags, radius=radius
                )
                landing = ext.node(
                    found["landing"],
                    floor=found["landing_floor"],
                    flags=bits(landing_flags.get(found["kind"])),
                    radius=radius,
                )
                ok = ext.attach(landing)
                if found["runup"] is not None:
                    runup = ext.node(found["runup"])
                    ok = ok and ext.attach(runup)
                    ext.link(runup, takeoff)
                    ext.link(takeoff, runup)
                else:
                    ok = ok and ext.attach(takeoff)
                ext.link(takeoff, landing)
                out["jumps"].append(
                    {
                        "kind": found["kind"],
                        "takeoff": [round(v, 1) for v in seeded.waypoints[takeoff].position],
                        "landing": [round(v, 1) for v in seeded.waypoints[landing].position],
                        "rise": found["rise"],
                        "distance": found["distance"],
                        "waypoints": {"takeoff": takeoff, "landing": landing},
                        **({} if ok else {"note": "a walk to or from it was not found"}),
                    }
                )
                break

    if isinstance(ladders_cfg, dict):
        names = {str(n).lower() for n in ladders_cfg.get("surfaceparms") or []}
        parms = analysis.shader_contents(profile_id, map_path).parms if names else {}
        climb = bits(ladders_cfg.get("flags"))
        for box in _ladder_boxes(grid, parms, names) if names else []:
            row = _ladder(ext, phys, ladders_cfg, box)
            if "foot" in row:
                foot = ext.node(row.pop("foot"), floor=row.pop("foot_floor"), flags=climb)
                top = ext.node(row.pop("top"), floor=row.pop("top_floor"), flags=climb)
                ext.link(foot, top)
                ext.link(top, foot)
                ok = ext.attach(foot) and ext.attach(top)
                row.update(
                    foot=[round(v, 1) for v in seeded.waypoints[foot].position],
                    top=[round(v, 1) for v in seeded.waypoints[top].position],
                    waypoints={"foot": foot, "top": top},
                )
                if not ok:
                    row["note"] = "a walk to or from it was not found"
            out["ladders"].append(row)

    waypoints = seeded.waypoints
    seeded.report.update(
        waypoints=len(waypoints),
        connections=sum(len(w.connections) for w in waypoints),
        one_way_connections=sum(
            1
            for i, w in enumerate(waypoints)
            for t in w.connections
            if i not in waypoints[t].connections
        ),
        by_stance={s: seeded.stance_of.count(s) for s in dict.fromkeys(seeded.stance_of)},
        components=components(waypoints),
    )
    return out


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
    extras: dict[str, Any] | None = None,
) -> tuple[WaypointFile, dict[str, Any]]:
    """Seed the waypoint file for a map, and report how it was made and what it connects.

    `extras`, when given, receives what later steps reuse: the grids (`grids`, the map as the
    round starts with doors open, and `closed`, the same when anything changes state, else
    None), the final `stance_of` per waypoint and the
    `spawn_waypoints` per team."""
    from . import routes  # routes imports analysis; keep this module importable on its own

    bots = spec(profile_id)
    if spacing is None:
        spacing = float(bots["default_spacing"])
    if not math.isfinite(spacing) or spacing < cell:
        raise BotNavError(f"spacing must be at least one cell ({cell:g}), got {spacing:g}")
    role_map = analysis.roles(profile_id)
    stances = routes.stances(profile_id, "mixed")
    entities = game_map.entities()
    flags = {str(k): int(v) for k, v in bots["flags"].items()}
    states = _state_entities(entities, bots, role_map, flags)
    changing = sorted(states.walls + states.unbuilt + states.built_at_start)

    def grids_without(nonsolid: set[int]) -> dict:
        return {
            s: analysis.build_navgrid(
                map_path,
                cell=cell,
                profile_id=profile_id,
                game_map=game_map,
                stance=s,
                nonsolid_entities=frozenset(nonsolid),
            )
            for s in stances
        }

    # Three states of the map, every door open in each: everything that changes gone (open),
    # as the round starts (closed), and every constructible built with the walls gone (built).
    doors = set(states.doors)
    constructible = set(states.unbuilt + states.built_at_start + states.parts)
    opened = grids_without(doors | set(states.walls) | constructible)
    start_set = doors | set(states.unbuilt) | set(states.parts)
    closed = (
        grids_without(start_set) if start_set != doors | set(states.walls) | constructible else None
    )
    built = grids_without(doors | set(states.walls)) if constructible else None
    _aligned(opened, closed, built)

    def walkable(state: dict) -> set:
        return {c for g in state.values() for c in g.walkable_cells()}

    exclude: set = set()
    for other in (closed, built):
        if other:
            exclude |= _enclosed_openings(opened, other)
    open_cells = walkable(opened)
    blocked: set = set()
    for other in (closed, built):
        if other:
            blocked |= open_cells - walkable(other)
    openings = frozenset(blocked - exclude)
    decks = frozenset(walkable(built) - open_cells) if built else frozenset()
    grids = {s: _Union(opened[s], built[s]) for s in stances} if built else opened
    door_cells = _door_cells(game_map, entities, states.doors, grids)
    # Spawns and objectives are pinned where players stand when the round starts.
    start = closed or opened
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

    seeded = seed(
        grids,
        bots,
        spacing=spacing,
        pins=pins,
        openings=openings,
        exclude=frozenset(exclude),
        doors=door_cells,
        decks=decks,
    )
    traversals = add_traversals(seeded, start, bots, profile_id, map_path)
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
            for key in ("jumps", "ladders"):
                rows = []
                for row in traversals[key]:
                    if "waypoints" in row:
                        row["waypoints"] = {k: remap.get(v) for k, v in row["waypoints"].items()}
                        if None in row["waypoints"].values():
                            continue  # in a part of the map no spawn connects to
                    rows.append(row)
                traversals[key] = rows
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
    if extras is not None:
        extras.update(
            grids=start, closed=closed, stance_of=list(seeded.stance_of), spawn_waypoints=spawn_wps
        )
    report = {
        **seeded.report,
        "pruned_unconnected_to_spawns": pruned,
        "stances": list(stances),
        "state_changing_entities": changing,
        "state_changing_cells_skipped": len(exclude),
        "doors": [
            {"entity": e, "flags": flag_names(bits, flags)}
            for e, bits in sorted(states.doors.items())
        ],
        "locked_doors": states.locked_doors,
        "constructibles": {"unbuilt": states.unbuilt, "built_at_start": states.built_at_start},
        "door_cells": len(door_cells),
        "deck_cells": len(decks),
        "pins": [{k: (list(v) if isinstance(v, tuple) else v) for k, v in p.items()} for p in pins],
        "objectives": list(coverage.values()),
        "spawn_waypoints": {t: sorted(v) for t, v in sorted(spawn_wps.items())},
        "flag_counts": {
            name: sum(1 for w in waypoints if w.flags & bit) for name, bit in flags.items()
        },
        **traversals,
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


@dataclass
class _StateEntities:
    """The brush entities whose state the seeding follows, by what they do to the walk."""

    doors: dict[int, int]  # a door a player opens -> the flag bits of its doorway waypoint
    walls: list[int]  # breakables and locked doors: solid until they go
    unbuilt: list[int]  # constructibles absent at round start
    built_at_start: list[int]
    parts: list[int]  # stage brushes of staged constructibles
    locked_doors: list[int]


def _keys(entity: dict) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in entity.get("keys") or []}


def _state_entities(
    entities: list[dict], bots: dict[str, Any], role_map: Any, flags: dict[str, int]
) -> _StateEntities:
    """Sort the map's doors, walls and constructibles by the profile's `bots:` rules."""
    door_spec = bots.get("doors") or {}
    con_spec = bots.get("constructibles") or {}
    door_classes = {str(c).lower() for c in door_spec.get("classnames") or []}
    con_classes = {str(c).lower() for c in con_spec.get("classnames") or []}
    part_classes = {str(c).lower() for c in con_spec.get("parts") or []}
    door_bits = 0
    for name in door_spec.get("flags") or []:
        door_bits |= flags[name]
    team_flags = {
        str(team).lower(): sum(flags[n] for n in names)
        for team, names in (door_spec.get("team_flags") or {}).items()
    }
    touch = door_spec.get("touch") or {}
    leave_open = {str(c).lower() for c in door_spec.get("targeters_that_leave_it_open") or []}
    built_flag = con_spec.get("start_built") or {}
    targeted: set[str] = set()
    for e in entities:
        keys = _keys(e)
        if str(e.get("classname") or "").lower() not in leave_open and keys.get("target"):
            targeted.add(keys["target"].lower())

    out = _StateEntities({}, [], [], [], [], [])
    for e in entities:
        index = int(e["index"])
        classname = str(e.get("classname") or "")
        lowered = classname.lower()
        keys = _keys(e)
        if role_map.category_of.get(classname) in role_map.state_changing:
            out.walls.append(index)
        elif lowered in door_classes:
            key = keys.get(str(door_spec.get("key") or "key"))
            name = keys.get("targetname", "").lower()
            try:
                locked = int(float(key)) < 0 if key is not None else bool(name) and name in targeted
            except ValueError:
                locked = False
            allowed = keys.get(str(door_spec.get("team_key") or ""), "").lower()
            named = [t for t in team_flags if t in allowed]
            touches = (
                lowered in {str(c).lower() for c in touch.get("classnames") or []}
                and parse_spawnflags(keys.get("spawnflags")) & int(touch.get("spawnflag", 0))
                and not keys.get(str(touch.get("unless_key") or ""))
            )
            if touches:
                out.doors[index] = door_bits
            elif locked or (allowed and not named):
                out.walls.append(index)
                out.locked_doors.append(index)
            else:
                out.doors[index] = door_bits | (team_flags[named[0]] if len(named) == 1 else 0)
        elif lowered in con_classes:
            bits = parse_spawnflags(keys.get(str(built_flag.get("key") or "spawnflags")))
            if bits & int(built_flag.get("bit", 0)):
                out.built_at_start.append(index)
            else:
                out.unbuilt.append(index)
        elif lowered in part_classes:
            out.parts.append(index)
    return out


class _Union:
    """One map's grid in two states, walked as one: a cell or move either state allows.

    Seeding over the union puts waypoints on a blown wall's cells and on a built bridge's
    deck alike; which state a connection needs is then flagged for the add-on to test.
    """

    def __init__(self, a: Any, b: Any):
        self.a, self.b = a, b
        self.cell, self.origin, self.dims = a.cell, a.origin, a.dims

    def centre(self, cell) -> tuple[float, float, float]:
        return self.a.centre(cell)

    def is_walkable(self, cell) -> bool:
        return self.a.is_walkable(cell) or self.b.is_walkable(cell)

    def walkable_cells(self) -> list:
        return sorted(set(self.a.walkable_cells()) | set(self.b.walkable_cells()))

    def neighbours(self, cell, *, symmetric: bool = False):
        seen: set = set()
        for grid in (self.a, self.b):
            if grid.is_walkable(cell):
                for d, w in grid.neighbours(cell, symmetric=symmetric):
                    if d not in seen:
                        seen.add(d)
                        yield d, w


def _aligned(*states: dict | None) -> None:
    """Cells of different states compare only on one lattice."""
    frames = {(g.origin, g.dims, g.cell) for state in states if state for g in state.values()}
    if len(frames) > 1:
        raise BotNavError(
            "the map's grids in different door/wall/constructible states have different "
            "bounds (a state-changing brush reaches past the world's own brushes); enclose "
            "the map so the world brushes bound it"
        )


def _door_cells(game_map: Any, entities: list[dict], doors: dict[int, int], grids: dict) -> dict:
    """Walkable cells whose floor lies inside a door's bounds, each with its door's flags.

    The bounds grow by half a cell across, so a door thinner than a cell still holds a row.
    """
    out: dict[tuple[int, int, int], int] = {}
    if not doors:
        return out
    by_index = {int(e["index"]): e for e in entities}
    grid = next(iter(grids.values()))
    cell, origin = grid.cell, grid.origin
    cells = {c for g in grids.values() for c in g.walkable_cells()}
    for index, bits in doors.items():
        bounds = analysis._brush_bounds(game_map, by_index[index])
        if bounds is None:
            continue
        lo, hi = bounds
        x0 = math.floor((lo[0] - cell / 2 - origin[0]) / cell)
        x1 = math.ceil((hi[0] + cell / 2 - origin[0]) / cell)
        y0 = math.floor((lo[1] - cell / 2 - origin[1]) / cell)
        y1 = math.ceil((hi[1] + cell / 2 - origin[1]) / cell)
        for c in cells:
            if not (x0 <= c[0] < x1 and y0 <= c[1] < y1):
                continue
            x, y, z = grid.centre(c)
            if (
                lo[0] - cell / 2 <= x <= hi[0] + cell / 2
                and lo[1] - cell / 2 <= y <= hi[1] + cell / 2
                and lo[2] - cell <= z < hi[2]
            ):
                out[c] = out.get(c, 0) | bits
    return out


def install_path(bots: dict[str, Any], mapname: str) -> Path:
    """Where the add-on looks for a map's waypoints, relative to its own folder."""
    return Path(str(bots["install_path"]).format(map=mapname))
