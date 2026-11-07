"""Gameplay analysis (§7.3): a voxel navigation grid, and the reports built on it.

§7.3 asks for balance, sightline, movement and spawn-safety reports, and says how to get a
navmesh: "derive from compiled AAS (`bspc`) where bots are wanted, otherwise voxelize the BSP
collision hull. A* over that for all distance/time metrics."

**This is the voxelize branch, and it is an approximation.** Saying so precisely, because a
number that looks exact invites decisions it cannot support:

- A voxel grid is not an AAS file. AAS knows about jumps, ladders, water, teleporters and the
  reachabilities between areas; this knows about "empty cell above a solid cell with enough
  room to stand", plus a step and fall limit read from the profile. Where bots matter, compile
  the real thing.
- Floor occupancy is sampled at cell centres. Routes additionally test a player box of the
  selected stance against expanded brush half-spaces. This is exact for axis-aligned brushes
  and conservative at angled brush edges; prone models only the recorded main body box.
- Cell size quantizes vertically as well: standing headroom is required in whole cells, so at
  `cell=16` the grid demands `ceil(69.375 / 16) * 16 = 80` units of clear space. It errs
  towards refusing to call space walkable, and reports the figure it actually used.
- Brushes the kernel cannot evaluate exactly are **left out**, not guessed at. Real maps
  contain them (a third of one corpus map's brushes have off-grid plane points), so every
  report carries the count and a finding when it is non-zero. A path that does not exist may
  simply be a wall we could not see.

What *is* exact: brush occupancy (half-space arithmetic on the kernel's exact vertices), the
raycasts used for sightlines and passage widths, and the clearance measurements
`movement_check` reports — the grid only chooses *where* to measure.

# Severity discipline

Findings use `nrc_mcp.rules`' shape and the same clamp the rule engine applies: a finding
whose reasoning rests on anything unverified is reported as `info`, never as an error. Here
that means almost everything, and deliberately — an approximate navmesh must not fail a
build. Two things earn a `warning`: facts about the input itself (geometry we could not
evaluate) and exact measurements compared against a `verified` profile constant.

Every physics constant comes from the profile (§7.4), including the one the design document
got wrong: it assumed 56 units of standing height, and the shipped gamepack says 69.375. See
`docs/spec-corrections.md` §1 W2. Nothing here has a fallback value, because a fallback is
how a wrong number survives.
"""

from __future__ import annotations

import bisect
import heapq
import math
import os
import random
import struct
from array import array
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import gamepath, profiles
from .kernel import KernelUnavailable, kernel, load_map, repo_root
from .rules import Finding, parse_spawnflags
from .rules import _keys as _rule_keys

# ---------------------------------------------------------------------------
# Caps and tunables
#
# All of these are arguments with defaults rather than constants used directly, so a caller
# who knows their map can raise them. The defaults are sized for a real map: §7's working
# example is 5000 x 4000 x 900 units, which at cell=16 is 313 x 250 x 57 = 4.5M cells.
# ---------------------------------------------------------------------------

DEFAULT_CELL = 16
"""Default voxel edge in world units. 16 is the usual authoring grid for interiors."""

MAX_CELLS = 1_000_000_000
"""Cell budget for one grid, counting every cell of its bounding box.

The grid is sparse: it stores each column's solid runs (4 bytes per column plus 8 per run)
and holds one x-slab of `ny * nz` bytes while voxelizing, so this caps time far more than
memory. A billion cells is ET: Legacy's Supply Depot (6912 x 6912 x 3456 units) at cell 8.
"""

DENSE_MAX_CELLS = 12_000_000
"""The cap when the kernel has no sparse voxel pass and the dense Python reference runs:
one byte per cell, so a 12 MB bytearray."""

MAX_HULL_VERTICES = 20
"""Above this, a brush is approximated by its bounding box instead of its half-spaces.

Deriving half-spaces from vertices alone costs O(V^4 / 6) (every triple, tested against every
vertex), which is fine for the 8-16 vertices a hand-built brush has and not fine for a
64-sided cylinder. Approximated brushes are counted and reported.
"""

MAX_BUCKETS_PER_SOLID = 2048
"""A brush spanning more buckets than this is tested on every ray instead of being indexed.

Map-sized slabs (floors, the sky shell) would otherwise dominate the index's memory while
appearing in nearly every query anyway.
"""

DEFAULT_BUCKET = 256
"""Edge of the 2D broad-phase bucket, in world units."""

MAX_A_STAR_NODES = 400_000
"""Nodes one A* run may expand before giving up and saying so."""

PLANE_EPS = 1.0e-6
"""Half-space tolerance, in world units.

Used one way only: a segment must penetrate a brush by more than this to count as blocked, so
a ray that grazes a surface — which is what happens when an eye sits exactly at ceiling height
in a minimum-height corridor — is not reported as an obstruction.
"""

# Role names in the profile's own entity taxonomy, not names from any game. `category` values
# containing SPAWN_ROLE mark spawn points; the one equal to OBJECTIVE_ROLE marks objectives.
SPAWN_ROLE = "spawn"
OBJECTIVE_ROLE = "objective"

# Categories whose brushes are not collision geometry.
NONSOLID_ROLES = ("trigger", "compiler")

NONSOLID_SHADER_FRAGMENTS = ("hint", "skip", "trigger", "origin", "botclip", "donotenter")
"""Shader fragments that mean "this brush is not collision geometry".

Engine-level rather than game-level, and the same reasoning `nrc-render` uses for its
invisible-surface list: every idTech game built with this compiler shares these. A brush is
dropped only when *every* face matches, so a wall with one trigger-textured face stays solid.
Clip variants are deliberately absent — they are what stops the player, so they are solid
here even though they are never drawn.
"""

SOLID_CLEARING_SURFACEPARMS = (
    "origin",
    "areaportal",
    "nonsolid",
    "trigger",
    "water",
    "slime",
    "lava",
    "playerclip",
    "monsterclip",
    "nodrop",
    "clusterportal",
    "donotenter",
    "botclip",
    "fog",
)
"""Surfaceparms that take solid contents away from a shader, in the compiler's default table.

q3map2 `games.cpp:142-173` (the `game_default` parms every game inherits): each of these
clears `CONTENTS_SOLID`. A game's own additions come from the profile's
`collision.solid_clearing_surfaceparms`. A shader with none of them, or with no script at all,
keeps the default solid contents (`games.cpp:132`).
"""

PATCH_SUBDIVIDE_DISTANCE = 16.0
"""`SUBDIVIDE_DISTANCE` (etlegacy `qcommon/cm_patch.h:129`), restated for reports: the kernel's
port (`nrc_solid::patch::collision`) is what applies it. A patch collides as chords that stop
short of its curve by up to this much."""

PLAYER_CLIP_SURFACEPARMS = ("playerclip",)
"""Surfaceparms that stop players even though they clear solid (`games.cpp:165`)."""

PATCH_THICKNESS = 1.0
"""How far behind its blocking side a patch facet is treated as solid, in world units.

The engine's facet has no thickness: a trace is stopped by its front plane, or by one of the
bevels it adds around it (`cm_patch.c:1017`, `1936`). The half-space model needs some volume,
so each facet is a prism this deep behind its front face, and it blocks from both sides —
as the engine did with a player box in `bench/dogfood/patches.py` (an inward-facing pipe
stopped a player outside it). Thin on purpose: a deep prism behind the lower half of
a small pipe would poke out of its top. The front face, the one players meet, stays exactly
where the engine puts it. The voxel grid does not sample these slivers at cell centres, which
would miss them; it marks every cell the facet passes through (`_rasterize_facet`).
"""

REST_NUDGE = 1.0e-4
"""How far a resting body rises past an obstacle whose top rounds to at or below the height
it already holds (two brushes sharing a top face compute it a few ulps apart)."""

PATCH_RASTER_SHIFT = 0.01
"""How far behind its front a facet is moved before it is rasterized, in world units.

A facet lying exactly on a cell boundary then lands in the cell behind it, as a brush face on
that boundary would: a floor at z=0 fills the cell below 0, not the one above.
"""

UNASSIGNED_TEAM = "unassigned"

_SOLID = b"\x01"
_SOLID_THEN_EMPTY = b"\x01\x00"

# 8-connected horizontal moves. Diagonals are allowed but corner-checked, so a path cannot
# squeeze between two brushes that touch along an edge.
_NEIGHBOURS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


class AnalysisError(RuntimeError):
    """Analysis could not be performed, with a message naming what to change."""


# ---------------------------------------------------------------------------
# Profile access — every physics constant, and which entities play which role
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Constant:
    """One physics constant, carried around with its provenance.

    The confidence travels with the value because it decides what a finding derived from it is
    allowed to say. A constant with no confidence recorded is treated as unverified.
    """

    key: str
    value: float
    confidence: str
    source: str
    description: str = ""
    approximate: bool = False

    @property
    def verified(self) -> bool:
        return self.confidence == "verified"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "confidence": self.confidence,
            "source": self.source,
            "description": self.description,
            "approximate": self.approximate,
        }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _constant(section: dict, key: str, *, path: str = "movement") -> Constant | None:
    entry = section.get(key)
    if not isinstance(entry, dict):
        return None
    value = _number(entry.get("value"))
    approximate = False
    if value is None:
        # A few entries state only an approximate figure (the gamepack's own measurement was
        # "117.xxx"). Usable, but the report should say which numbers are rounded.
        value = _number(entry.get("value_approx"))
        approximate = value is not None
    if value is None:
        return None
    return Constant(
        key=f"{path}.{key}",
        value=value,
        confidence=str(entry.get("confidence", "unverified")),
        source=str(entry.get("source", "")),
        description=str(entry.get("description", "")),
        approximate=approximate,
    )


def resolve_profile(profile_id: str | None) -> str:
    """The profile to analyse against.

    Explicit argument first, then `NRC_PROFILE`, then the only profile on disk if there is
    exactly one. Never a built-in default: no game's constants belong in this file.
    """
    if profile_id:
        return profile_id
    env = os.environ.get("NRC_PROFILE", "").strip()
    if env:
        return env
    available = profiles.available()
    if len(available) == 1:
        return available[0]
    raise AnalysisError(
        "no profile selected, and every movement constant has to come from one. "
        f"Pass profile_id or set NRC_PROFILE; available: {available or 'none'}"
    )


@dataclass(frozen=True)
class Movement:
    """The movement constants this module uses, each with its confidence."""

    profile: str
    step_height: Constant
    headroom_stand: Constant
    headroom_crouch: Constant | None
    jump_up_max: Constant | None
    ledge_grab_max: Constant | None
    fall_no_damage: Constant | None
    player_width: Constant | None
    eye_height: Constant
    eye_height_is_inferred: bool
    headroom_prone: Constant | None = None
    floor_separation: Constant | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"profile": self.profile}
        for name in (
            "step_height",
            "headroom_stand",
            "headroom_crouch",
            "headroom_prone",
            "floor_separation",
            "jump_up_max",
            "ledge_grab_max",
            "fall_no_damage",
            "player_width",
            "eye_height",
        ):
            c = getattr(self, name)
            out[name] = c.as_dict() if c else None
        out["eye_height_is_inferred"] = self.eye_height_is_inferred
        return out


def movement_constants(profile_id: str) -> Movement:
    """Read the constants this module needs, or explain which one is missing.

    `step_height` and `headroom_stand` are required — without them there is no definition of
    a walkable cell, and inventing one is exactly the failure the corrections document
    records. The rest are optional, and the checks that need them say so when they are absent.
    """
    mv = profiles.movement(profile_id)
    section = mv.get("movement")
    if not isinstance(section, dict):
        raise AnalysisError(
            f"profile {profile_id} has no `movement:` section, so no clearance can be checked "
            "against it. Every physics constant has to be data (§7.4)."
        )

    step = _constant(section, "step_height")
    stand = _constant(section, "headroom_stand")
    missing = [n for n, c in (("step_height", step), ("headroom_stand", stand)) if c is None]
    if missing or step is None or stand is None:
        raise AnalysisError(
            f"profile {profile_id} states no {' and no '.join(missing)} under `movement:`; "
            "a navigation grid cannot be derived without them, and this module has no "
            "fallback values on purpose"
        )

    # The largest drop that costs no health, without matching on a band name: it is the
    # smallest of the stated maxima, every larger band being defined by the damage it does.
    fall: Constant | None = None
    damage = section.get("fall_damage")
    if isinstance(damage, dict) and isinstance(damage.get("thresholds"), list):
        candidates = []
        for row in damage["thresholds"]:
            if not isinstance(row, dict):
                continue
            v = _number(row.get("max_drop"))
            if v is not None:
                candidates.append(
                    Constant(
                        key="movement.fall_damage.max_drop",
                        value=v,
                        confidence=str(row.get("confidence", "unverified")),
                        source=str(row.get("source", "")),
                        description=str(row.get("description", "")),
                    )
                )
        if candidates:
            fall = min(candidates, key=lambda c: c.value)

    width: Constant | None = None
    box = (mv.get("units") or {}).get("player") if isinstance(mv.get("units"), dict) else None
    box = box.get("bounding_box") if isinstance(box, dict) else None
    if isinstance(box, dict):
        widths = [w for w in (_number(box.get("width_x")), _number(box.get("width_y"))) if w]
        if widths:
            width = Constant(
                key="units.player.bounding_box.width",
                value=max(widths),
                confidence=str(box.get("confidence", "unverified")),
                source=str(box.get("source", "")),
                description="Player bounding-box width.",
            )

    # Eye height is its own small honesty problem. This profile records that the gamepack
    # never states one, so `profiles.standing_height` falls back to the verified standing
    # height — the top of the player's head. Sightlines cast from there are slightly
    # optimistic, and anything derived from them is reported as inferred.
    eye = _constant(section, "eye_height")
    inferred = False
    if eye is None or not eye.verified:
        eye = Constant(
            key="movement.headroom_stand (as eye height)",
            value=stand.value,
            confidence=stand.confidence,
            source=stand.source,
            description="Standing height, used as eye height because the profile states none.",
        )
        inferred = True

    return Movement(
        profile=profile_id,
        step_height=step,
        headroom_stand=stand,
        headroom_crouch=_constant(section, "headroom_crouch"),
        jump_up_max=_constant(section, "jump_up_max"),
        ledge_grab_max=_constant(section, "ledge_grab_max"),
        fall_no_damage=fall,
        player_width=width,
        eye_height=eye,
        eye_height_is_inferred=inferred,
        headroom_prone=_constant(section, "headroom_prone"),
        floor_separation=_constant(section, "floor_separation"),
    )


@dataclass(frozen=True)
class ObjectiveLink:
    """A profile-declared marker that represents its uniquely named objective target."""

    classname: str
    target_key: str
    target_name_key: str
    target_classnames: frozenset[str]
    case_sensitive: bool = True


@dataclass(frozen=True)
class Roles:
    """Which classnames play which gameplay role, entirely from profile data."""

    profile: str
    spawns: frozenset[str]
    objectives: frozenset[str]
    team_key: str
    group_key: str
    team_labels: tuple[str, ...]
    category_of: dict[str, str]
    # (flag value, team label) pairs per classname, from `teams.spawnflags`.
    team_flags: dict[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)
    # Spawns without a group key take the nearest entity of this class as their group.
    group_nearest: str = ""
    group_label_key: str = ""
    # Categories whose brushes change state during a round (blown, built), from
    # `roles.state_changing`. Solid by default; `open_state_changing` treats them as open.
    state_changing: frozenset[str] = frozenset()
    objective_links: tuple[ObjectiveLink, ...] = ()

    def team_of(self, classname: str, keys: dict[str, str]) -> str:
        """The team an entity belongs to.

        Three sources, because profiles describe all three conventions: a team *key* on the
        entity, a team *spawnflag* on classes the profile lists, and per-team *classnames* for
        the stock team entities. The labels come from the profile, so matching them inside a
        classname stays data-driven.
        """
        value = (keys.get(self.team_key) or "").strip().lower() if self.team_key else ""
        if value:
            return value
        flags = self.team_flags.get(classname)
        if flags:
            bits = parse_spawnflags(keys.get("spawnflags"))
            for flag, label in flags:
                if bits & flag:
                    return label
            return UNASSIGNED_TEAM
        lowered = classname.lower()
        for label in self.team_labels:
            if label and label in lowered:
                return label
        return UNASSIGNED_TEAM

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "spawn_classnames": sorted(self.spawns),
            "objective_classnames": sorted(self.objectives),
            "team_key": self.team_key,
            "group_key": self.group_key,
            "team_labels": list(self.team_labels),
            "team_spawnflags": {c: [list(p) for p in v] for c, v in self.team_flags.items()},
            "spawn_groups_by_nearest": self.group_nearest or None,
        }


def _role_keys(data: dict) -> tuple[str, str]:
    """The team and group key names, taken from the profile's own rule parameters.

    The rules already have to name these keys to be evaluable, so reading them back is better
    than a second declaration that could drift — and better than a guess in code.
    """
    team = group = ""
    for rule in data.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        params = rule.get("params")
        if not isinstance(params, dict):
            continue
        team = team or str(params.get("team_key") or "")
        group = group or str(params.get("group_key") or "")
    return team, group


def _team_labels(data: dict, entities: list[dict], team_key: str) -> tuple[str, ...]:
    """Values the team key may take, from the entity key definition, an enum, or `teams:`.

    A profile whose spawns carry no team key names its sides under `teams.sides` instead, so
    the classname fallback in `Roles.team_of` still has labels to match.
    """
    labels: list[str] = []

    def add(values: Any) -> None:
        if isinstance(values, list):
            for v in values:
                text = str(v).strip().lower()
                if text and text not in labels:
                    labels.append(text)

    for entity in entities:
        for key in entity.get("keys") or []:
            if not isinstance(key, dict) or str(key.get("name")) != team_key:
                continue
            add(key.get("values"))
            enum = data.get("enums", {}).get(str(key.get("enum") or ""))
            if isinstance(enum, dict):
                add([item.get("value") for item in enum.get("items") or []])
    for rule in data.get("rules") or []:
        params = rule.get("params") if isinstance(rule, dict) else None
        if isinstance(params, dict) and str(params.get("key")) == team_key:
            add(params.get("values"))
    teams = data.get("teams")
    if isinstance(teams, dict) and isinstance(teams.get("sides"), list):
        add([s.get("label") for s in teams["sides"] if isinstance(s, dict)])
    return tuple(labels)


def roles(profile_id: str) -> Roles:
    """Discover spawn and objective classnames, and the team and group keys.

    §7.3's balance report needs to know which entities are spawns and which are objectives.
    That is game knowledge, so it is read here from the profile's `entities` categories and
    cross-checked against its `focus` section — never written down in this file, which
    `tools/seam_lint.py` enforces.
    """
    data = profiles.load(profile_id)
    entities = profiles.entities(profile_id)

    category_of = {str(e.get("classname")): str(e.get("category", "")) for e in entities}
    spawns = {c for c, cat in category_of.items() if SPAWN_ROLE in cat}
    objectives = {c for c, cat in category_of.items() if cat == OBJECTIVE_ROLE}

    # The focus section is the verification record, and it names classnames the entity list
    # may not categorize. Only entries confirmed present, and only single classnames — a
    # slash-joined name there is a list of names checked for and *not* found. Where the
    # entity list already has a category, that wins: a topic can mention spawns while being
    # about something that merely stands near one.
    for entry in data.get("focus") or []:
        if not isinstance(entry, dict) or entry.get("present") is not True:
            continue
        classname = str(entry.get("classname", "")).strip()
        if not classname or "/" in classname or classname in category_of:
            continue
        topic = str(entry.get("topic", "")).lower()
        if SPAWN_ROLE in topic:
            spawns.add(classname)
        elif OBJECTIVE_ROLE in topic or "flag" in topic:
            objectives.add(classname)

    team_key, group_key = _role_keys(data)
    if not team_key:
        # No rule names it. Fall back to the spawn classes' own key definitions: a key that
        # declares a closed set of values on a spawn entity is the one that names sides.
        for entity in entities:
            if SPAWN_ROLE not in str(entity.get("category", "")):
                continue
            for key in entity.get("keys") or []:
                values = key.get("values") if isinstance(key, dict) else None
                if isinstance(values, list) and len(values) >= 2:
                    team_key = str(key.get("name"))
                    break
            if team_key:
                break

    # `roles:` covers what a category cannot say. An objective that is also a trigger volume
    # must stay categorized `trigger` so its brushes are not collision, yet still count here.
    extra = data.get("roles") if isinstance(data.get("roles"), dict) else {}
    objectives |= {str(c) for c in extra.get("objectives_also") or []}
    grouping = extra.get("spawn_groups") if isinstance(extra.get("spawn_groups"), dict) else {}

    team_flags: dict[str, tuple[tuple[int, str], ...]] = {}
    teams = data.get("teams") if isinstance(data.get("teams"), dict) else {}
    for classname, pairs in (teams.get("spawnflags") or {}).items():
        if isinstance(pairs, list):
            team_flags[str(classname)] = tuple(
                (int(p["flag"]), str(p["label"]).lower())
                for p in pairs
                if isinstance(p, dict) and "flag" in p and "label" in p
            )

    return Roles(
        profile=profile_id,
        spawns=frozenset(spawns),
        objectives=frozenset(objectives),
        team_key=team_key,
        group_key=group_key,
        team_labels=_team_labels(data, entities, team_key),
        category_of=category_of,
        team_flags=team_flags,
        group_nearest=str(grouping.get("nearest") or ""),
        group_label_key=str(grouping.get("label_key") or ""),
        state_changing=frozenset(str(c) for c in extra.get("state_changing") or []),
        objective_links=tuple(
            ObjectiveLink(
                classname=str(link["classname"]),
                target_key=str(link["target_key"]),
                target_name_key=str(link["target_name_key"]),
                target_classnames=frozenset(str(c) for c in link["target_classnames"]),
                case_sensitive=bool(link.get("case_sensitive", True)),
            )
            for link in extra.get("objective_links") or []
        ),
    )


def _clamp(severity: str, confidence: str) -> str:
    """The rule engine's clamp: unverified reasoning can only ever be `info`."""
    return severity if confidence == "verified" else "info"


def _finding(
    code: str,
    severity: str,
    message: str,
    confidence: str,
    source: str,
    fix: str = "",
) -> dict[str, Any]:
    return Finding(
        code=code,
        severity=_clamp(severity, confidence),
        message=message,
        confidence=confidence,
        rule_source=source,
        fix_hint=fix,
    ).as_dict()


def _summary(findings: list[dict]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "info")}


# ---------------------------------------------------------------------------
# Solids — brushes as intersections of half-spaces
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Solid:
    """One brush as an intersection of half-spaces `n·p <= d`, plus its bounds.

    The kernel hands over exact vertices, not planes, so the planes are re-derived here as the
    supporting planes of the vertex set. For the common case — an axis-aligned box — they are
    read straight off the bounds instead, which is both faster and exactly right.
    """

    planes: tuple[tuple[float, float, float, float], ...]
    mins: tuple[float, float, float]
    maxs: tuple[float, float, float]
    entity: int
    primitive: int
    is_box: bool
    approximated: bool
    #: The shaders on this brush's faces, so a caller can ask *what kind* of solid stopped
    #: the player. Carried rather than re-read, because the only other way to answer
    #: "is this cell held up by a clip brush" is a second pass over the map.
    shaders: tuple[str, ...] = ()
    #: True for one collision facet of a patch (see `_facet_solid`), not a brush.
    patch: bool = False
    #: A patch facet's front face, its corners in order. The grid rasterizes this surface
    #: rather than sampling the thin prism at cell centres.
    facet: tuple[tuple[float, float, float], ...] = ()

    def contains(self, x: float, y: float, z: float) -> bool:
        if not (
            self.mins[0] <= x <= self.maxs[0]
            and self.mins[1] <= y <= self.maxs[1]
            and self.mins[2] <= z <= self.maxs[2]
        ):
            return False
        return all(a * x + b * y + c * z <= d for a, b, c, d in self.planes)

    def z_span(self, x: float, y: float) -> tuple[float, float] | None:
        """The interval of z inside this brush on the vertical line through `(x, y)`.

        Convexity is what makes this possible, and it is why voxelizing a brush costs one
        interval per column rather than one test per cell: for fixed x and y each half-space
        becomes a bound on z, so the whole column is decided in one pass over the planes.
        """
        lo, hi = self.mins[2], self.maxs[2]
        for a, b, c, d in self.planes:
            rest = d - a * x - b * y
            if c > PLANE_EPS:
                bound = rest / c
                if bound < hi:
                    hi = bound
            elif c < -PLANE_EPS:
                bound = rest / c
                if bound > lo:
                    lo = bound
            elif rest < 0.0:
                return None
        return (lo, hi) if lo <= hi else None


def _axis_planes(
    mins: tuple[float, float, float], maxs: tuple[float, float, float]
) -> tuple[tuple[float, float, float, float], ...]:
    return (
        (1.0, 0.0, 0.0, maxs[0]),
        (-1.0, 0.0, 0.0, -mins[0]),
        (0.0, 1.0, 0.0, maxs[1]),
        (0.0, -1.0, 0.0, -mins[1]),
        (0.0, 0.0, 1.0, maxs[2]),
        (0.0, 0.0, -1.0, -mins[2]),
    )


def _body_obstacles(solids: list[Solid], radius: float, height: float) -> list[Solid]:
    """Expand obstacles by the reflected player box, with the query point at its feet.

    Face-plane expansion encloses the true Minkowski sum, so missing bevel planes can
    reject a clear angled corner but cannot make an obstructed box appear clear.
    """
    seen: dict[bytes, tuple[float, float, float, float]] = {}
    return [
        Solid(
            planes=_interned(
                (
                    (a, b, c, d + radius * (abs(a) + abs(b)) + height * max(0.0, -c))
                    for a, b, c, d in solid.planes
                ),
                seen,
            ),
            mins=(solid.mins[0] - radius, solid.mins[1] - radius, solid.mins[2] - height),
            maxs=(solid.maxs[0] + radius, solid.maxs[1] + radius, solid.maxs[2]),
            entity=solid.entity,
            primitive=solid.primitive,
            is_box=solid.is_box,
            approximated=solid.approximated or not solid.is_box,
            shaders=solid.shaders,
            patch=solid.patch,
            facet=solid.facet,
        )
        for solid in solids
    ]


def _interned(
    planes, seen: dict[bytes, tuple[float, float, float, float]]
) -> tuple[tuple[float, float, float, float], ...]:
    """`planes` as a tuple whose planes are shared with every equal plane already in `seen`.

    Neighbouring brushes share faces, so a map's planes repeat about four times over; one
    tuple per distinct plane holds the same numbers at a quarter of the objects. Keyed by
    the packed doubles, so a plane is only ever replaced by a bit-identical one (`-0.0`
    stays `-0.0`).
    """
    return tuple(seen.setdefault(_PLANE.pack(*plane), plane) for plane in planes)


_PLANE = struct.Struct("=4d")


def _hull_planes_cached(
    vertices: list[tuple[float, float, float]], tolerance: float
) -> tuple[tuple[float, float, float, float], ...] | None:
    """`_hull_planes`, remembered: a map's grids in several stances and door states each
    collect the same brushes, and the hull is the costly part of collecting one.

    The cache holds vertices and planes as packed doubles, not tuples of floats: it outlives
    every grid, and as objects a full-size map's entries cost more than its grid does.
    """
    packed = _hull_planes_packed(array("d", [c for v in vertices for c in v]).tobytes(), tolerance)
    if packed is None:
        return None
    flat = array("d")
    flat.frombytes(packed)
    return tuple(zip(flat[0::4], flat[1::4], flat[2::4], flat[3::4], strict=True))


@lru_cache(maxsize=1 << 16)
def _hull_planes_packed(vertices: bytes, tolerance: float) -> bytes | None:
    flat = array("d")
    flat.frombytes(vertices)
    planes = _hull_planes(list(zip(flat[0::3], flat[1::3], flat[2::3], strict=True)), tolerance)
    if planes is None:
        return None
    return array("d", [c for plane in planes for c in plane]).tobytes()


def _hull_planes(
    vertices: list[tuple[float, float, float]], tolerance: float
) -> tuple[tuple[float, float, float, float], ...] | None:
    """Supporting half-spaces of a convex vertex set.

    Every triple of vertices spans a candidate plane; it is a face of the hull exactly when
    every vertex lies on one side of it. O(V^4) with a small constant, which is why the caller
    caps V. Returns None when fewer than four planes survive, which means the vertex set was
    not a solid we can reason about.
    """
    planes: dict[tuple[int, int, int, int], tuple[float, float, float, float]] = {}
    n = len(vertices)
    for i in range(n):
        ax, ay, az = vertices[i]
        for j in range(i + 1, n):
            bx, by, bz = vertices[j]
            ux, uy, uz = bx - ax, by - ay, bz - az
            for k in range(j + 1, n):
                cx, cy, cz = vertices[k]
                vx, vy, vz = cx - ax, cy - ay, cz - az
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                # Twice the triangle's area. Against a tolerance this small the only triples it
                # rejects are collinear ones, which span no plane and would normalize to noise.
                length = math.sqrt(nx * nx + ny * ny + nz * nz)
                if length <= tolerance:
                    continue
                nx, ny, nz = nx / length, ny / length, nz / length
                d = nx * ax + ny * ay + nz * az
                above = below = 0.0
                for px, py, pz in vertices:
                    side = nx * px + ny * py + nz * pz - d
                    if side > above:
                        above = side
                    elif side < below:
                        below = side
                if above <= tolerance:
                    plane = (nx, ny, nz, d)
                elif below >= -tolerance:
                    plane = (-nx, -ny, -nz, -d)
                else:
                    continue
                key = (
                    round(plane[0] * 1e6),
                    round(plane[1] * 1e6),
                    round(plane[2] * 1e6),
                    round(plane[3] * 1e4),
                )
                planes.setdefault(key, plane)
    return tuple(planes.values()) if len(planes) >= 4 else None


def _is_box(
    vertices: list[tuple[float, float, float]],
    mins: tuple[float, float, float],
    maxs: tuple[float, float, float],
) -> bool:
    """Whether the vertex set is exactly the eight corners of its own bounding box.

    Float equality is correct here: both sides come from the same exact vertices, so a corner
    either is the extreme the bounds were taken from or is not.
    """
    if len(vertices) != 8:
        return False
    return all(v[axis] in (mins[axis], maxs[axis]) for v in vertices for axis in (0, 1, 2))


def _nonsolid_shader(shader: str, fragments: tuple[str, ...]) -> bool:
    lowered = shader.lower()
    return any(f in lowered for f in fragments)


# ---------------------------------------------------------------------------
# Patches — the engine's collision facets, as thin solids
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShaderContents:
    """Which shaders stop a player, read from the shader scripts the compiler would see.

    A patch has one shader, and that shader's surfaceparms decide whether the compiled patch
    collides at all: q3map2 writes the shader's contents into the BSP (`games.cpp:132-173`)
    and the engine skips a patch whose contents miss the trace's mask (`cm_trace.c:876`).
    """

    #: Lowercased full shader name (`textures/...`) to its surfaceparms.
    parms: dict[str, tuple[str, ...]]
    #: Whether any shader script could be read. Without them a name is all there is to go on.
    resolved: bool
    sources: tuple[str, ...]
    clearing: frozenset[str]
    player_clip: frozenset[str]

    def blocks_player(self, shader: str) -> bool | None:
        """True or False when a script defines the shader; None when none does."""
        from .optimize import SHADER_NAME_PREFIX  # noqa: PLC0415

        parms = self.parms.get((SHADER_NAME_PREFIX + shader).lower())
        if parms is None:
            return None
        if any(p in self.player_clip for p in parms):
            return True
        return not any(p in self.clearing for p in parms)


_BASEGAME_PARMS: dict[tuple, dict[str, tuple[str, ...]]] = {}


def _basegame_parms(directory: Path) -> dict[str, tuple[str, ...]]:
    """Surfaceparms of every shader the base game's archives define, cached per archive set."""
    from . import optimize  # noqa: PLC0415

    try:
        stamp = tuple(
            (p.name, p.stat().st_mtime_ns, p.stat().st_size)
            for p in sorted(directory.iterdir())
            if p.suffix.lower() == ".pk3"
        )
    except OSError:
        return {}
    key = (str(directory), stamp)
    hit = _BASEGAME_PARMS.get(key)
    if hit is None:
        index = optimize.basegame_index([directory])
        hit = {name: tuple(s["surfaceparms"]) for name, s in index["shaders"].items()}
        _BASEGAME_PARMS.clear()
        _BASEGAME_PARMS[key] = hit
    return hit


def _archive_parms(archive: Path) -> dict[str, tuple[str, ...]]:
    """Surfaceparms of every shader one pk3's `scripts/*.shader` define, first definition
    kept (q3map2 reads one archive's scripts in turn and the first definition of a name wins,
    shaders.cpp:749)."""
    import zipfile  # noqa: PLC0415

    from . import optimize  # noqa: PLC0415

    found: dict[str, tuple[str, ...]] = {}
    try:
        with zipfile.ZipFile(archive) as z:
            members = sorted(
                n
                for n in z.namelist()
                if n.lower().startswith("scripts/") and n.lower().endswith(".shader")
            )
            for member in members:
                try:
                    parsed = optimize.parse_shader_text(
                        z.read(member).decode("latin-1"), f"{archive.name}:{member}"
                    )
                except optimize.OptimizeError:
                    continue
                for shader in parsed:
                    found.setdefault(shader["name"].lower(), tuple(shader["surfaceparms"]))
    except (OSError, zipfile.BadZipFile):
        return {}
    return found


def shader_contents(profile_id: str, map_path: str | Path | None = None) -> ShaderContents:
    """The shader scripts a compile of `map_path` would read: the project's own, then the game's.

    The project's are the loose `scripts/*.shader` beside its `maps/` folder; the game's are
    the scripts inside the base game's archives (`NRC_FS_BASEPATH` plus the profile's
    `packaging.basegame`). Either may be missing; `resolved` says whether any was read. The
    profile's compile-only shaders (`compile_shaders`) come last, as the compiler sees them.
    """
    from . import optimize  # noqa: PLC0415

    data = profiles.load(profile_id)
    collision = data.get("collision") if isinstance(data.get("collision"), dict) else {}
    extra = collision.get("solid_clearing_surfaceparms") or []
    clearing = frozenset([*SOLID_CLEARING_SURFACEPARMS, *(str(p).lower() for p in extra)])
    parms: dict[str, tuple[str, ...]] = {}
    sources: list[str] = []
    if map_path is not None:
        folder = Path(map_path).resolve().parent
        root = folder.parent if folder.name.lower() == "maps" else folder
        scripts = root / "scripts"
        if scripts.is_dir():
            for script in sorted(scripts.glob("*.shader")):
                try:
                    parsed = optimize.parse_shader_file(script)
                except (OSError, optimize.OptimizeError):
                    continue
                for shader in parsed:
                    parms.setdefault(shader["name"].lower(), tuple(shader["surfaceparms"]))
                sources.append(str(script))
        # Archives kept beside `maps/` (a map built on another package's art, packed with
        # `depends`): compile_map puts that folder on q3map2's -fs_pakpath, which opens them
        # after the base game and so ahead of it, the last-named archive first (vfs.cpp:109,
        # :124, :178-184), as the engine prefers later archives.
        for archive in sorted(root.glob("*.pk3"), key=lambda p: p.name.upper(), reverse=True):
            found = _archive_parms(archive)
            if found:
                for name, value in found.items():
                    parms.setdefault(name, value)
                sources.append(str(archive))
    base = gamepath.basepath()
    basegame = (data.get("packaging") or {}).get("basegame")
    if base and basegame and (Path(base) / str(basegame)).is_dir():
        directory = Path(base) / str(basegame)
        found = _basegame_parms(directory)
        if found:
            for name, value in found.items():
                parms.setdefault(name, value)
            sources.append(str(directory))
    # Last, as tools/q3map2.py gives them to the compiler: the profile's compile-only shaders
    # (terrain alphaMod volumes and the like), which the map uses but no archive defines. A
    # handful of editor shaders, so they do not make the rest of the map's names resolved.
    resolved = bool(sources)
    compile_only = profiles.profiles_dir() / str(
        (data.get("compile_shaders") or {}).get("compile_shader_file") or ""
    )
    if compile_only.is_file():
        try:
            for shader in optimize.parse_shader_file(compile_only):
                parms.setdefault(shader["name"].lower(), tuple(shader["surfaceparms"]))
            sources.append(str(compile_only))
        except (OSError, optimize.OptimizeError):
            pass
    return ShaderContents(
        parms=parms,
        resolved=resolved,
        sources=tuple(sources),
        clearing=clearing,
        player_clip=frozenset(PLAYER_CLIP_SURFACEPARMS),
    )


def _cross(
    u: tuple[float, float, float], v: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def _facet_solid(
    normal: tuple[float, float, float],
    dist: float,
    points: list[tuple[float, float, float]],
    thickness: float,
    entity: int,
    primitive: int,
    shader: str,
) -> list[Solid]:
    """A patch facet as a prism: its front face where the engine puts it, `thickness` behind.

    Bounded by the facet plane, the plane `thickness` behind it, one plane through each edge
    perpendicular to the facet, and the six axial planes of its bounds — the same axial bevels
    the engine adds to every facet (`cm_patch.c:1017`), which keep a player-box expansion of a
    sharp triangle corner from reaching further than the corner itself. A quad that is not
    convex is split into its two triangles.
    """
    nx, ny, nz = normal
    count = len(points)
    edges: list[tuple[float, float, float, float]] = []
    for k in range(count):
        a, b = points[k], points[(k + 1) % count]
        m = _cross((b[0] - a[0], b[1] - a[1], b[2] - a[2]), normal)
        length = math.sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2])
        if length < 1e-9:
            continue  # a degenerate edge bounds nothing
        m = (m[0] / length, m[1] / length, m[2] / length)
        d = m[0] * a[0] + m[1] * a[1] + m[2] * a[2]
        sides = [m[0] * p[0] + m[1] * p[1] + m[2] * p[2] - d for p in points]
        if max(sides) > 1e-6:
            m, d = (-m[0], -m[1], -m[2]), -d
            sides = [-s for s in sides]
            if max(sides) > 1e-6:
                if count == 4:
                    # Not convex: the two triangles, as the engine would split the cell.
                    first = [points[0], points[1], points[2]]
                    second = [points[2], points[3], points[0]]
                    return [
                        *_facet_solid(normal, dist, first, thickness, entity, primitive, shader),
                        *_facet_solid(normal, dist, second, thickness, entity, primitive, shader),
                    ]
                return []
        edges.append((m[0], m[1], m[2], d))
    if len(edges) < 3:
        return []
    corners = [
        *points,
        *((p[0] - nx * thickness, p[1] - ny * thickness, p[2] - nz * thickness) for p in points),
    ]
    mins = (
        min(p[0] for p in corners),
        min(p[1] for p in corners),
        min(p[2] for p in corners),
    )
    maxs = (
        max(p[0] for p in corners),
        max(p[1] for p in corners),
        max(p[2] for p in corners),
    )
    planes = (
        (nx, ny, nz, dist),
        (-nx, -ny, -nz, thickness - dist),
        *edges,
        *_axis_planes(mins, maxs),
    )
    return [
        Solid(
            planes=planes,
            mins=mins,
            maxs=maxs,
            entity=entity,
            primitive=primitive,
            is_box=False,
            approximated=False,
            shaders=(shader,),
            patch=True,
            facet=tuple(points),
        )
    ]


def _clip_polygon(
    points: list[tuple[float, float, float]], axis: int, bound: float, keep_below: bool
) -> list[tuple[float, float, float]]:
    """Sutherland-Hodgman against one axial plane, keeping `p[axis] <= bound` or `>= bound`."""
    out: list[tuple[float, float, float]] = []
    count = len(points)
    for k in range(count):
        a, b = points[k], points[(k + 1) % count]
        da = (a[axis] - bound) if keep_below else (bound - a[axis])
        db = (b[axis] - bound) if keep_below else (bound - b[axis])
        if da <= 0:
            out.append(a)
        if (da < 0 < db) or (db < 0 < da):
            t = da / (da - db)
            out.append(
                (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)
            )
    return out


def _rasterize_facet(
    solid: Solid,
    occupancy: bytearray,
    origin: tuple[float, float, float],
    dims: tuple[int, int, int],
    cell: float,
    tops: dict[int, float] | None = None,
) -> None:
    """Mark every cell a patch facet passes through as solid.

    A brush is sampled at cell centres because it has volume; a facet has none, so centre
    sampling would let a wall of facets fall between columns. Instead the facet, moved
    `PATCH_RASTER_SHIFT` behind its front, is clipped to each column it overlaps, and every
    cell its z-range in that column touches is filled. That reads a patch surface up to one
    cell nearer the player than a brush face at the same height would read — the
    conservative side — and the exact checks downstream (body clearance, `movement_check`)
    measure the real facet, not the cells.
    """
    nx, ny, nz = dims
    ox, oy, oz = origin
    a, b, c, _ = solid.planes[0]
    shift = PATCH_RASTER_SHIFT
    polygon = [(p[0] - a * shift, p[1] - b * shift, p[2] - c * shift) for p in solid.facet]
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    cx0 = max(0, math.floor((min(xs) - ox) / cell))
    cx1 = min(nx - 1, math.floor((max(xs) - ox) / cell))
    cy0 = max(0, math.floor((min(ys) - oy) / cell))
    cy1 = min(ny - 1, math.floor((max(ys) - oy) / cell))
    for ix in range(cx0, cx1 + 1):
        x_lo = ox + ix * cell
        strip = _clip_polygon(polygon, 0, x_lo, keep_below=False)
        strip = _clip_polygon(strip, 0, x_lo + cell, keep_below=True) if strip else strip
        if not strip:
            continue
        base_x = ix * ny
        for iy in range(cy0, cy1 + 1):
            y_lo = oy + iy * cell
            piece = _clip_polygon(strip, 1, y_lo, keep_below=False)
            piece = _clip_polygon(piece, 1, y_lo + cell, keep_below=True) if piece else piece
            if not piece:
                continue
            # A facet that only touches the column's edge (a floor ending on a cell boundary)
            # is not in it: the piece must reach inside the open square.
            mx = sum(p[0] for p in piece) / len(piece)
            my = sum(p[1] for p in piece) / len(piece)
            if not (x_lo < mx < x_lo + cell and y_lo < my < y_lo + cell):
                continue
            zs = [p[2] for p in piece]
            iz0 = max(0, math.floor((min(zs) - oz) / cell))
            iz1 = min(nz - 1, max(iz0, math.ceil((max(zs) - oz) / cell) - 1))
            if iz0 > iz1:
                continue
            start = (base_x + iy) * nz
            occupancy[start + iz0 : start + iz1 + 1] = _SOLID * (iz1 - iz0 + 1)
            if tops is not None:
                top = max(zs)
                if tops.get(start + iz1, -math.inf) < top:
                    tops[start + iz1] = top


def _collect_patches(
    game_map: Any,
    solids: list[Solid],
    provenance: dict[str, Any],
    skipped_entities: set[int],
    contents: ShaderContents | None,
    nonsolid_fragments: tuple[str, ...],
    thickness: float,
) -> None:
    """Add every player-blocking patch's collision facets to `solids`, counting what was not."""
    if not hasattr(game_map, "patch_collision"):
        provenance["patches"] = provenance["patches_unmodelled"] = len(game_map.patches())
        return
    examples: list[str] = []
    for patch in game_map.patch_collision():
        provenance["patches"] += 1
        entity, primitive = int(patch["entity"]), int(patch["primitive"])
        shader = str(patch.get("shader") or "")
        if entity in skipped_entities:
            provenance["patches_nonsolid_entity"] += 1
            continue
        blocks = contents.blocks_player(shader) if contents is not None else None
        if blocks is None:
            if contents is None or not contents.resolved:
                provenance["patches_shader_unresolved"] += 1
                blocks = not _nonsolid_shader(shader, (*nonsolid_fragments, "nonsolid"))
            else:
                blocks = True  # no script: the compiler's default contents are solid
        if not blocks:
            provenance["patches_nonsolid_shader"] += 1
            continue
        if patch.get("error"):
            provenance["patches_unmodelled"] += 1
            if len(examples) < 5:
                examples.append(f"entity {entity} primitive {primitive}: {patch['error']}")
            continue
        made = 0
        for normal, dist, points in patch["facets"]:
            for solid in _facet_solid(
                (float(normal[0]), float(normal[1]), float(normal[2])),
                float(dist),
                [(float(p[0]), float(p[1]), float(p[2])) for p in points],
                thickness,
                entity,
                primitive,
                shader,
            ):
                solids.append(solid)
                made += 1
        provenance["patch_facets"] += made
        provenance["patches_used"] += 1 if made else 0
    provenance["patch_unmodelled_examples"] = examples


def collect_solids(
    game_map: Any,
    role_map: Roles | None = None,
    *,
    max_hull_vertices: int = MAX_HULL_VERTICES,
    nonsolid_fragments: tuple[str, ...] = NONSOLID_SHADER_FRAGMENTS,
    open_state_changing: bool = False,
    patch_thickness: float = PATCH_THICKNESS,
    contents: ShaderContents | None = None,
    nonsolid_entities: frozenset[int] = frozenset(),
) -> tuple[list[Solid], dict[str, Any]]:
    """Every collision brush and patch facet in the map, as half-space solids, plus what was
    left out.

    The provenance dictionary is not decoration. Brushes the kernel declines to evaluate are
    invisible to everything downstream, and a caller that cannot see how many there were has
    no way to judge an "unreachable" answer.

    Patches enter as the engine's own collision facets (`nrc_solid::patch::collision`), each a
    prism `patch_thickness` deep behind its blocking face. `contents` says which patch shaders
    stop a player; without it (or when it read no scripts) a patch is judged by its shader's
    name, as brushes are, and the count of such patches is reported.

    `nonsolid_entities` leaves out the brushes of those entity indices, for a caller that
    walks the map in one particular state of its doors and objectives.

    A brush whose plane points are off the integer grid (every brush of a map decompiled
    from a BSP, whose writer prints three decimals) cannot be derived exactly; it enters
    from the kernel's floating-point path instead (`nrc_core::approx_geometry`, good to
    0.01 unit) and is counted in `brushes_float_evaluated`. Only brushes that path also
    declines (collinear points, planes enclosing nothing) are `brushes_indeterminate`.
    """
    nonsolid_categories = set(NONSOLID_ROLES)
    if open_state_changing and role_map:
        nonsolid_categories |= role_map.state_changing
    solids: list[Solid] = []
    provenance = {
        "brushes": 0,
        "brushes_used": 0,
        "brushes_indeterminate": 0,
        "brushes_float_evaluated": 0,
        "brushes_bounds_approximated": 0,
        "brushes_nonsolid_shader": 0,
        "brushes_nonsolid_entity": 0,
        "patches": 0,
        "patches_used": 0,
        "patch_facets": 0,
        "patches_nonsolid_shader": 0,
        "patches_nonsolid_entity": 0,
        "patches_shader_unresolved": 0,
        "patches_unmodelled": 0,
        "unrecognized_primitives": 0,
        "entities_unknown_to_profile": 0,
    }
    indeterminate_examples: list[str] = []
    skipped_entities: set[int] = set()
    # One object per distinct plane and shader list across the map (see `_interned`).
    planes_seen: dict[bytes, tuple[float, float, float, float]] = {}
    shaders_seen: dict[tuple[str, ...], tuple[str, ...]] = {}

    for entity in game_map.entities(with_keys=False):
        index = int(entity["index"])
        classname = str(entity.get("classname") or "")
        category = role_map.category_of.get(classname) if role_map else None
        skip = category in nonsolid_categories or index in nonsolid_entities
        if skip:
            skipped_entities.add(index)
        if role_map and category is None and int(entity.get("brushes") or 0):
            provenance["entities_unknown_to_profile"] += 1

        primitive = 0
        limit = int(entity.get("brushes") or 0) + int(entity.get("patches") or 0) + 4096
        while primitive < limit:
            try:
                geometry = game_map.brush_geometry(index, primitive, approximate=True)
            except ValueError as e:
                message = str(e)
                if "has no primitive" in message:
                    break
                if "patch" not in message:  # patches are collected below
                    provenance["unrecognized_primitives"] += 1
                primitive += 1
                continue
            primitive += 1
            provenance["brushes"] += 1

            if skip:
                provenance["brushes_nonsolid_entity"] += 1
                continue
            shaders = [str(s) for s in geometry.get("shaders") or []]
            # q3map2 ORs the contents of a brush's sides (map.cpp:517-537): it stops a player
            # only if some side's shader does. A side counts as clear by its name, or when
            # the shader scripts define it without solid contents (an alphaMod volume,
            # water); a shader no script defines stays solid, as the compiler makes it.
            if shaders and all(
                _nonsolid_shader(s, nonsolid_fragments)
                or (contents is not None and contents.blocks_player(s) is False)
                for s in shaders
            ):
                provenance["brushes_nonsolid_shader"] += 1
                continue
            if not geometry.get("usable"):
                provenance["brushes_indeterminate"] += 1
                if len(indeterminate_examples) < 5:
                    indeterminate_examples.append(
                        f"entity {index} primitive {primitive - 1}: {geometry.get('reason')}"
                    )
                continue

            bounds = geometry.get("bounds")
            if not bounds:
                provenance["brushes_indeterminate"] += 1
                continue
            if geometry.get("approximate"):
                provenance["brushes_float_evaluated"] += 1
            mins = (float(bounds[0][0]), float(bounds[0][1]), float(bounds[0][2]))
            maxs = (float(bounds[1][0]), float(bounds[1][1]), float(bounds[1][2]))
            vertices = [(float(v[0]), float(v[1]), float(v[2])) for v in geometry["vertices"]]

            box = _is_box(vertices, mins, maxs)
            approximated = False
            if box:
                planes = _axis_planes(mins, maxs)
            else:
                extent = max(maxs[i] - mins[i] for i in range(3))
                planes_or_none = (
                    _hull_planes_cached(vertices, max(extent, 1.0) * 1e-7)
                    if len(vertices) <= max_hull_vertices
                    else None
                )
                if planes_or_none is None:
                    planes = _axis_planes(mins, maxs)
                    approximated = True
                    provenance["brushes_bounds_approximated"] += 1
                else:
                    planes = planes_or_none

            names = tuple(shaders)
            solids.append(
                Solid(
                    planes=_interned(planes, planes_seen),
                    mins=mins,
                    maxs=maxs,
                    entity=index,
                    primitive=primitive - 1,
                    is_box=box,
                    approximated=approximated,
                    shaders=shaders_seen.setdefault(names, names),
                )
            )
            provenance["brushes_used"] += 1

    provenance["indeterminate_examples"] = indeterminate_examples
    _collect_patches(
        game_map,
        solids,
        provenance,
        skipped_entities,
        contents,
        nonsolid_fragments,
        patch_thickness,
    )
    return solids, provenance


# ---------------------------------------------------------------------------
# Broad phase and raycasting
# ---------------------------------------------------------------------------


class BucketIndex:
    """Uniform 2D buckets over brush bounding boxes, for ordered ray queries.

    2D and not 3D on purpose: maps are wide and flat, so bucketing the vertical axis buys
    little and costs memory. A brush spanning more than `max_buckets` of them is held aside and
    tested on every query, which is the right trade for the handful of map-sized slabs that
    would otherwise fill the index.
    """

    def __init__(
        self,
        solids: list[Solid],
        bucket: float = DEFAULT_BUCKET,
        max_buckets: int = MAX_BUCKETS_PER_SOLID,
    ) -> None:
        self.bucket = float(bucket)
        self.cells: dict[tuple[int, int], list[int]] = {}
        self.everywhere: list[int] = []
        for i, solid in enumerate(solids):
            x0 = math.floor(solid.mins[0] / self.bucket)
            x1 = math.floor(solid.maxs[0] / self.bucket)
            y0 = math.floor(solid.mins[1] / self.bucket)
            y1 = math.floor(solid.maxs[1] / self.bucket)
            if (x1 - x0 + 1) * (y1 - y0 + 1) > max_buckets:
                self.everywhere.append(i)
                continue
            for bx in range(x0, x1 + 1):
                for by in range(y0, y1 + 1):
                    self.cells.setdefault((bx, by), []).append(i)

    def column(self, x: float, y: float) -> list[int]:
        return self.everywhere + self.cells.get(
            (math.floor(x / self.bucket), math.floor(y / self.bucket)), []
        )

    def along(
        self, p0: tuple[float, float, float], p1: tuple[float, float, float]
    ) -> list[tuple[int, int]]:
        """Buckets the segment crosses, in order, by 2D grid traversal.

        Ordered so that a blocked ray can stop at the first obstruction it meets instead of
        testing the whole corridor. Traversal rather than sampling, because a sampled step can
        skip a bucket the segment clips through, and a skipped bucket is a missed wall.
        """
        b = self.bucket
        x, y = math.floor(p0[0] / b), math.floor(p0[1] / b)
        endx, endy = math.floor(p1[0] / b), math.floor(p1[1] / b)
        out = [(x, y)]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]

        step_x = 1 if dx > 0 else -1
        step_y = 1 if dy > 0 else -1
        t_x = math.inf if dx == 0 else (((x + (dx > 0)) * b) - p0[0]) / dx
        t_y = math.inf if dy == 0 else (((y + (dy > 0)) * b) - p0[1]) / dy
        d_x = math.inf if dx == 0 else abs(b / dx)
        d_y = math.inf if dy == 0 else abs(b / dy)

        # Bounded by the Manhattan distance in buckets, so a degenerate direction cannot spin.
        for _ in range(abs(endx - x) + abs(endy - y)):
            if t_x <= t_y:
                x += step_x
                t_x += d_x
            else:
                y += step_y
                t_y += d_y
            out.append((x, y))
            if (x, y) == (endx, endy):
                break
        if out[-1] != (endx, endy):
            # Rounding could in principle leave the walk one bucket short of where the segment
            # ends. Missing a bucket means missing a wall, so add it back rather than trust it.
            out.append((endx, endy))
        return out


def _segment_enters(
    solid: Solid,
    p0: tuple[float, float, float],
    delta: tuple[float, float, float],
    eps: float = PLANE_EPS,
) -> float | None:
    """Where the segment `p0 + t*delta`, `t` in [0,1], first enters the brush interior.

    Half-space clipping: each plane trims the parameter interval, and what survives is the
    part of the segment inside the brush. The interior is taken `eps` inside each plane so a
    ray running exactly along a surface is not treated as an obstruction.
    """
    lo, hi = 0.0, 1.0
    px, py, pz = p0
    dx, dy, dz = delta
    for a, b, c, d in solid.planes:
        distance = (d - eps) - (a * px + b * py + c * pz)
        rate = a * dx + b * dy + c * dz
        if -PLANE_EPS < rate < PLANE_EPS:
            if distance < 0.0:
                return None
            continue
        t = distance / rate
        if rate > 0.0:
            if t < hi:
                hi = t
        elif t > lo:
            lo = t
        if lo >= hi:
            return None
    return lo


def _aabb_misses(
    solid: Solid, lo: tuple[float, float, float], hi: tuple[float, float, float]
) -> bool:
    return any(hi[i] < solid.mins[i] or lo[i] > solid.maxs[i] for i in range(3))


def segment_clear(
    solids: list[Solid],
    index: BucketIndex,
    p0: tuple[float, float, float],
    p1: tuple[float, float, float],
) -> bool:
    """Whether nothing solid lies between the two points."""
    delta = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
    # The bounds test (`_aabb_misses`) written out: it runs for every candidate brush of
    # every segment, tens of times per clearance test, and a call per brush dominated it.
    lx, hx = (p0[0], p1[0]) if p0[0] <= p1[0] else (p1[0], p0[0])
    ly, hy = (p0[1], p1[1]) if p0[1] <= p1[1] else (p1[1], p0[1])
    lz, hz = (p0[2], p1[2]) if p0[2] <= p1[2] else (p1[2], p0[2])
    seen: set[int] = set()
    for i in index.everywhere:
        seen.add(i)
        solid = solids[i]
        mn, mx = solid.mins, solid.maxs
        if hx < mn[0] or lx > mx[0] or hy < mn[1] or ly > mx[1] or hz < mn[2] or lz > mx[2]:
            continue
        if _segment_enters(solid, p0, delta) is not None:
            return False
    for key in index.along(p0, p1):
        for i in index.cells.get(key, ()):
            if i in seen:
                continue
            seen.add(i)
            solid = solids[i]
            mn, mx = solid.mins, solid.maxs
            if hx < mn[0] or lx > mx[0] or hy < mn[1] or ly > mx[1] or hz < mn[2] or lz > mx[2]:
                continue
            if _segment_enters(solid, p0, delta) is not None:
                return False
    return True


def ray_distance(
    solids: list[Solid],
    index: BucketIndex,
    origin: tuple[float, float, float],
    direction: tuple[float, float, float],
    max_distance: float,
) -> float:
    """Distance from `origin` along a unit `direction` to the first brush, or `max_distance`.

    Every candidate is tested, not just the first hit found, because the nearest surface is
    the answer here — this is what measures a passage width.
    """
    end = tuple(origin[i] + direction[i] * max_distance for i in range(3))
    delta = tuple(end[i] - origin[i] for i in range(3))
    lo = tuple(min(origin[i], end[i]) for i in range(3))
    hi = tuple(max(origin[i], end[i]) for i in range(3))
    best = 1.0
    seen: set[int] = set()
    for key in index.along(origin, end):
        for i in index.cells.get(key, ()):
            if i in seen:
                continue
            seen.add(i)
            solid = solids[i]
            if _aabb_misses(solid, lo, hi):
                continue
            t = _segment_enters(solid, origin, delta)
            if t is not None and t < best:
                best = t
    for i in index.everywhere:
        if i in seen:
            continue
        solid = solids[i]
        if _aabb_misses(solid, lo, hi):
            continue
        t = _segment_enters(solid, origin, delta)
        if t is not None and t < best:
            best = t
    return best * max_distance


def column_intervals(
    solids: list[Solid], index: BucketIndex, x: float, y: float
) -> list[tuple[float, float]]:
    """Merged intervals of solid z on the vertical line through `(x, y)`, exactly.

    The grid answers "is this cell solid" to within a cell; this answers "where exactly is the
    floor and the ceiling", which is what a clearance measured against a verified constant has
    to be based on.
    """
    spans: list[tuple[float, float]] = []
    for i in index.column(x, y):
        span = solids[i].z_span(x, y)
        if span is not None:
            spans.append(span)
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for lo, hi in spans[1:]:
        last_lo, last_hi = merged[-1]
        if lo <= last_hi:
            if hi > last_hi:
                merged[-1] = (last_lo, hi)
        else:
            merged.append((lo, hi))
    return merged


def floor_and_ceiling(
    intervals: list[tuple[float, float]], z: float
) -> tuple[float | None, float | None]:
    """The top of the solid at or below `z`, and the bottom of the solid above it."""
    floor = ceiling = None
    for lo, hi in intervals:
        if hi <= z + PLANE_EPS:
            floor = hi
        elif lo > z - PLANE_EPS:
            ceiling = lo
            break
        else:
            # z is inside this interval: the point is embedded in a brush.
            return hi, None
    return floor, ceiling


# ---------------------------------------------------------------------------
# The navigation grid
# ---------------------------------------------------------------------------


class ColumnRuns:
    """A grid's solid cells, stored as each column's runs of solid z.

    `offsets[ix * ny + iy] .. offsets[ix * ny + iy + 1]` indexes `bounds`, which holds that
    column's runs flattened as half-open `[start, end)` pairs, ascending and maximal (two runs
    never touch). A cell is solid exactly when an odd number of bounds lie at or below it, so a
    lookup is one bisection over a handful of numbers. Memory follows the map's surfaces, not
    its volume: a column of open air costs four bytes however tall the map is.
    """

    __slots__ = ("bounds", "dims", "offsets")

    def __init__(self, dims: tuple[int, int, int], offsets: array, bounds: array) -> None:
        if len(offsets) != dims[0] * dims[1] + 1:
            raise ValueError("offsets must hold one entry per column, plus one")
        self.dims = dims
        self.offsets = offsets
        self.bounds = bounds

    @classmethod
    def from_dense(cls, dims: tuple[int, int, int], solid: bytes | bytearray) -> ColumnRuns:
        """The runs of a dense `(ix * ny + iy) * nz + iz` byte grid."""
        nx, ny, nz = dims
        offsets = array("I", [0])
        bounds = array("I")
        for column in range(nx * ny):
            start = column * nz
            end = start + nz
            at = solid.find(_SOLID, start, end)
            while at >= 0:
                stop = solid.find(b"\x00", at, end)
                stop = end if stop < 0 else stop
                bounds.append(at - start)
                bounds.append(stop - start)
                at = solid.find(_SOLID, stop, end) if stop < end else -1
            offsets.append(len(bounds))
        return cls(dims, offsets, bounds)

    def runs(self, ix: int, iy: int) -> list[tuple[int, int]]:
        """The column's solid runs as `(start, end)` cell ranges, end exclusive."""
        nx, ny, _nz = self.dims
        if not (0 <= ix < nx and 0 <= iy < ny):
            return []
        k = ix * ny + iy
        flat = self.bounds[self.offsets[k] : self.offsets[k + 1]]
        return list(zip(flat[::2], flat[1::2], strict=True))

    def is_solid(self, ix: int, iy: int, iz: int) -> bool:
        nx, ny, nz = self.dims
        if not (0 <= ix < nx and 0 <= iy < ny and 0 <= iz < nz):
            return False
        k = ix * ny + iy
        lo = self.offsets[k]
        return (bisect.bisect_right(self.bounds, iz, lo, self.offsets[k + 1]) - lo) % 2 == 1

    def count(self) -> int:
        """Solid cells in the whole grid."""
        b = self.bounds
        return sum(b[1::2]) - sum(b[::2])

    def dense(self) -> bytearray:
        """The grid as one byte per cell, `(ix * ny + iy) * nz + iz`. Costs the full volume."""
        nx, ny, nz = self.dims
        out = bytearray(nx * ny * nz)
        b = self.bounds
        for column in range(nx * ny):
            base = column * nz
            for k in range(self.offsets[column], self.offsets[column + 1], 2):
                out[base + b[k] : base + b[k + 1]] = _SOLID * (b[k + 1] - b[k])
        return out

    def nbytes(self) -> int:
        return self.offsets.itemsize * len(self.offsets) + self.bounds.itemsize * len(self.bounds)


class WalkColumns(Mapping):
    """A grid's standing cells, read like `{(ix, iy): (iz, ...)}` but stored flat.

    `offsets[ix * ny + iy] .. offsets[ix * ny + iy + 1]` indexes `zs` (that column's walkable
    cells, ascending) and `rests` (where the body comes to rest on each, see `RestHeights`);
    `keys_` lists the columns that have any, in `(ix, iy)` order. A dict of tuples costs an
    object per column and per height; this costs 4 bytes a column and 12 a standing cell.
    `get` builds the column's tuple on each call.
    """

    __slots__ = ("cell", "dims", "keys_", "offsets", "oz", "rests", "zs")

    def __init__(
        self,
        dims: tuple[int, int, int],
        offsets: array,
        zs: array,
        rests: array,
        oz: float,
        cell: float,
    ) -> None:
        if len(offsets) != dims[0] * dims[1] + 1 or not len(zs) == len(rests) == offsets[-1]:
            raise ValueError("offsets must hold one entry per column, plus one, ending at len(zs)")
        self.dims = dims
        self.offsets = offsets
        self.zs = zs
        self.rests = rests
        self.oz = oz
        self.cell = cell
        self.keys_ = array(
            "I", (k for k in range(len(offsets) - 1) if offsets[k] != offsets[k + 1])
        )

    @classmethod
    def from_dict(
        cls,
        dims: tuple[int, int, int],
        columns: dict[tuple[int, int], tuple[int, ...]],
        stand_z: dict[tuple[int, int, int], float],
        oz: float,
        cell: float,
    ) -> WalkColumns:
        """The flat form of `{(ix, iy): zs}` and `{(ix, iy, iz): rest}` (rest defaults to
        the cell's lower boundary, `oz + iz * cell`)."""
        nx, ny, _nz = dims
        offsets, zs, rests = array("I", [0]), array("I"), array("d")
        for k in range(nx * ny):
            ix, iy = divmod(k, ny)
            for iz in columns.get((ix, iy), ()):
                zs.append(iz)
                rests.append(stand_z.get((ix, iy, iz), oz + iz * cell))
            offsets.append(len(zs))
        return cls(dims, offsets, zs, rests, oz, cell)

    def _span(self, ix: int, iy: int) -> tuple[int, int]:
        nx, ny, _nz = self.dims
        if not (0 <= ix < nx and 0 <= iy < ny):
            return 0, 0
        if ix.__class__ is not int or iy.__class__ is not int:
            # A dict would find a whole float (43.0 == 43) and miss any other.
            if ix != int(ix) or iy != int(iy):
                return 0, 0
            ix, iy = int(ix), int(iy)
        k = ix * ny + iy
        return self.offsets[k], self.offsets[k + 1]

    def get(self, key, default=None):
        try:
            ix, iy = key
        except (TypeError, ValueError):
            return default
        a, b = self._span(ix, iy)
        return tuple(self.zs[a:b]) if a < b else default

    def __getitem__(self, key):
        column = self.get(key)
        if column is None:
            raise KeyError(key)
        return column

    def __contains__(self, key) -> bool:
        return self.get(key) is not None

    def __iter__(self):
        ny = self.dims[1]
        for k in self.keys_:
            yield divmod(k, ny)

    def __len__(self) -> int:
        return len(self.keys_)

    def items(self):
        ny, offsets, zs = self.dims[1], self.offsets, self.zs
        for k in self.keys_:
            yield divmod(k, ny), tuple(zs[offsets[k] : offsets[k + 1]])

    def cell_count(self) -> int:
        return len(self.zs)

    def rest(self, ix: int, iy: int, iz: int) -> float | None:
        """Where the body rests on walkable cell `(ix, iy, iz)`; None when it is not one."""
        a, b = self._span(ix, iy)
        zs = self.zs
        for i in range(a, b):
            if zs[i] == iz:
                return self.rests[i]
        return None

    def heights(self) -> RestHeights:
        return RestHeights(self)

    def __eq__(self, other) -> bool:
        if isinstance(other, WalkColumns):
            return (
                self.dims == other.dims
                and self.offsets == other.offsets
                and self.zs == other.zs
                and self.rests == other.rests
            )
        return Mapping.__eq__(self, other)

    __hash__ = None  # type: ignore[assignment]

    def nbytes(self) -> int:
        return sum(a.itemsize * len(a) for a in (self.offsets, self.zs, self.rests, self.keys_))


class RestHeights(Mapping):
    """`{(ix, iy, iz): z}` for the walkable cells whose body rests off the cell's lower
    boundary (`origin.z + iz * cell`), read from `WalkColumns` without a dict of its own."""

    __slots__ = ("_count", "walk")

    def __init__(self, walk: WalkColumns) -> None:
        self.walk = walk
        self._count: int | None = None

    def get(self, key, default=None):
        try:
            ix, iy, iz = key
        except (TypeError, ValueError):
            return default
        walk = self.walk
        z = walk.rest(ix, iy, iz)
        if z is None or z == walk.oz + iz * walk.cell:
            return default
        return z

    def __getitem__(self, key):
        z = self.get(key)
        if z is None:
            raise KeyError(key)
        return z

    def __contains__(self, key) -> bool:
        return self.get(key) is not None

    def items(self):
        walk = self.walk
        ny, offsets, zs, rests = walk.dims[1], walk.offsets, walk.zs, walk.rests
        oz, cell = walk.oz, walk.cell
        for k in walk.keys_:
            ix, iy = divmod(k, ny)
            for i in range(offsets[k], offsets[k + 1]):
                iz = zs[i]
                if rests[i] != oz + iz * cell:
                    yield (ix, iy, iz), rests[i]

    def __iter__(self):
        for key, _z in self.items():
            yield key

    def __len__(self) -> int:
        if self._count is None:
            self._count = sum(1 for _ in self.items())
        return self._count

    def __bool__(self) -> bool:
        if self._count is not None:
            return self._count > 0
        return next(iter(self.items()), None) is not None

    def __eq__(self, other) -> bool:
        if isinstance(other, RestHeights):
            return self.walk == other.walk
        return Mapping.__eq__(self, other)

    __hash__ = None  # type: ignore[assignment]


_MOVE_PACK = struct.Struct("<hhh")


def unpack_moves(cell: tuple[int, int, int], packed: bytes, size: float) -> list:
    """`(dest, distance)` pairs from `NavGrid.pack_moves`, distances as `neighbours` gives."""
    ix, iy, iz = cell
    return [
        ((ix + dx, iy + dy, iz + rise), math.sqrt(dx * dx + dy * dy + rise * rise) * size)
        for dx, dy, rise in _MOVE_PACK.iter_unpack(packed)
    ]


@dataclass
class NavGrid:
    """A voxelized walkable surface, with the provenance needed to judge it.

    `occupancy` holds the solid cells sparsely, as each column's runs (`ColumnRuns`);
    `is_solid` reads it. `solid` still gives the old dense view, one byte per cell indexed
    `(ix * ny + iy) * nz + iz`, but builds it on demand at the full volume's cost.
    """

    map_path: str
    profile: str
    cell: float
    origin: tuple[float, float, float]
    dims: tuple[int, int, int]
    occupancy: ColumnRuns
    #: Walkable cells per column, read like `{(ix, iy): (iz, ...)}` (`WalkColumns`).
    columns: WalkColumns
    movement: Movement
    step_up_cells: int
    fall_cells: int
    headroom_cells: int
    solids: list[Solid] = field(default_factory=list)
    index: BucketIndex | None = None
    # The parsed map, kept so the reports can read entities without re-reading the file.
    game_map: Any = None
    geometry: dict[str, Any] = field(default_factory=dict)
    counts: dict[str, Any] = field(default_factory=dict)
    restricted: list[tuple[int, int, int]] = field(default_factory=list)
    crouch_only_total: int = 0
    too_low_total: int = 0
    findings: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stance: str = "stand"
    clearance_solids: list[Solid] = field(default_factory=list)
    clearance_index: BucketIndex | None = None
    clearance_restricted: list[tuple[int, int, int]] = field(default_factory=list)
    #: Where the body rests on a walkable cell, when that is not the cell's lower boundary:
    #: a floor whose top is off the cell grid, or one with something under the body's
    #: footprint it rises onto (see `build_navgrid`). `centre` reads it. Read like
    #: `{(ix, iy, iz): z}`; a built grid holds a `RestHeights` view of `columns`.
    stand_z: Mapping = field(default_factory=dict)
    _components: dict[tuple[int, int, int], int] | None = None
    _component_sizes: list[int] = field(default_factory=list)
    #: Moves learned ahead per relation (directed, symmetric): cell -> `pack_moves` bytes.
    _move_memo: tuple[dict, dict] | None = None

    # --- coordinates ------------------------------------------------------

    def centre(self, cell: tuple[int, int, int]) -> tuple[float, float, float]:
        """World position of a walkable cell: centred horizontally, on its floor vertically.

        "On its floor" is where the player box comes to rest there (`stand_z`), which is the
        cell's lower boundary only when the floor's top is on the cell grid.
        """
        ix, iy, iz = cell
        if isinstance(self.columns, WalkColumns):
            z = self.columns.rest(ix, iy, iz)
        else:
            z = self.stand_z.get((ix, iy, iz)) if self.stand_z else None
        return (
            self.origin[0] + (ix + 0.5) * self.cell,
            self.origin[1] + (iy + 0.5) * self.cell,
            self.origin[2] + iz * self.cell if z is None else z,
        )

    def cell_at(self, position: tuple[float, float, float]) -> tuple[int, int, int]:
        return (
            math.floor((position[0] - self.origin[0]) / self.cell),
            math.floor((position[1] - self.origin[1]) / self.cell),
            math.floor((position[2] - self.origin[2]) / self.cell),
        )

    @property
    def solid(self) -> bytearray:
        """The dense byte grid, `(ix * ny + iy) * nz + iz`, built from `occupancy` per call."""
        return self.occupancy.dense()

    def is_solid(self, ix: int, iy: int, iz: int) -> bool:
        return self.occupancy.is_solid(ix, iy, iz)

    def is_walkable(self, cell: tuple[int, int, int]) -> bool:
        return cell[2] in self.columns.get((cell[0], cell[1]), ())

    def walkable_cells(self) -> list[tuple[int, int, int]]:
        """Every walkable cell, in a deterministic order."""
        return [(x, y, z) for (x, y) in sorted(self.columns) for z in self.columns[(x, y)]]

    # --- graph ------------------------------------------------------------

    def neighbours(self, cell: tuple[int, int, int], *, symmetric: bool = False):
        """Walkable neighbours and the world distance to each.

        A move is allowed when the destination column has a walkable cell within the profile's
        step height above, or its no-damage fall distance below. Diagonals are corner-checked
        so a path cannot pass through the join between two touching brushes.

        The relation is **directed**, because the game is: a 64-unit ledge can be walked off
        and not climbed back up. `symmetric=True` relaxes the rise limit to the fall limit in
        both directions, which is what component labelling needs.

        Moves learned ahead (`learn_moves`, the same computation done in worker processes)
        are answered from memory, in the same order with the same distances.
        """
        memo = self._move_memo
        if memo is not None:
            packed = memo[symmetric].get(cell)
            if packed is not None:
                return unpack_moves(cell, packed, self.cell)
        return self._walk_moves(cell, symmetric)

    def pack_moves(self, cell: tuple[int, int, int], symmetric: bool) -> bytes:
        """`neighbours(cell)` packed as `(dx, dy, rise)` int16 triples, for `learn_moves`."""
        return b"".join(
            _MOVE_PACK.pack(d[0] - cell[0], d[1] - cell[1], d[2] - cell[2])
            for d, _ in self._walk_moves(cell, symmetric)
        )

    def learn_moves(self, cells, symmetric: bool, packed) -> None:
        """Keep moves computed elsewhere (`pack_moves` of each of `cells`, in order)."""
        if self._move_memo is None:
            self._move_memo = ({}, {})
        self._move_memo[symmetric].update(zip(cells, packed, strict=True))

    def forget_moves(self) -> None:
        self._move_memo = None

    def _walk_moves(self, cell: tuple[int, int, int], symmetric: bool):
        ix, iy, iz = cell
        up = self.fall_cells if symmetric else self.step_up_cells
        width = self.movement.player_width
        # Body clearance removes the cells against a riser. A step or drop can still cross
        # that narrow band: lift, move horizontally, then lower, checking the swept body.
        # Longer links are only allowed at a change of height, never across a flat gap.
        reach = max(1, math.ceil(width.value / (2 * self.cell)) + 1) if width else 1
        for distance in range(1, reach + 1):
            for ux, uy in _NEIGHBOURS:
                dx, dy = ux * distance, uy * distance
                column = self.columns.get((ix + dx, iy + dy))
                if not column:
                    continue
                for iz2 in column:
                    rise = iz2 - iz
                    if not -self.fall_cells <= rise <= up or (distance > 1 and rise == 0):
                        continue
                    dest = (ix + dx, iy + dy, iz2)
                    if self.clearance_index is not None:
                        if not self._body_move_clear(cell, dest):
                            continue
                    elif (
                        dx
                        and dy
                        and (self.is_solid(ix + dx, iy, iz) or self.is_solid(ix, iy + dy, iz))
                    ):
                        continue
                    yield dest, math.sqrt(dx * dx + dy * dy + rise * rise) * self.cell

    def _body_move_clear(self, source, target) -> bool:
        """A step-shaped swept box, also used in reverse for weak components.

        The higher endpoint is the horizontal leg; testing the vertical legs prevents a
        rise from passing through a ceiling. Intermediate columns need floor at one of the
        endpoint levels, so widening the riser transition does not invent a jump over a gap.
        """
        assert self.clearance_index is not None
        p0, p1 = self.centre(source), self.centre(target)
        highest = max(p0[2], p1[2])
        a, b = (p0[0], p0[1], highest), (p1[0], p1[1], highest)
        span = max(abs(target[0] - source[0]), abs(target[1] - source[1]))
        if span > 1:
            for part in range(1, span):
                ix = source[0] + (target[0] - source[0]) * part // span
                iy = source[1] + (target[1] - source[1]) * part // span
                if not any(self.is_solid(ix, iy, z - 1) for z in (source[2], target[2])):
                    return False
        return all(
            segment_clear(self.clearance_solids, self.clearance_index, start, end)
            for start, end in ((p0, a), (a, b), (b, p1))
            if start != end
        )

    def components(self) -> dict[tuple[int, int, int], int]:
        """Connected components of the walkable graph, labelled once and cached.

        Worth the one pass: without it every unreachable query costs a full A* that exhausts
        its whole component before admitting defeat, and unreachable pairs are common (an
        interior and a rooftop rarely connect).

        Labelled over the *symmetric* relation, so "different components" is a sound proof of
        unreachability and "same component" only a strong hint. Over the directed relation it
        would be neither: walking off a ledge would split a map that is perfectly connected in
        the direction the caller asked about.
        """
        if self._components is not None:
            return self._components
        labels: dict[tuple[int, int, int], int] = {}
        sizes: list[int] = []
        for start in self.walkable_cells():
            if start in labels:
                continue
            label = len(sizes)
            labels[start] = label
            size = 0
            stack = [start]
            while stack:
                current = stack.pop()
                size += 1
                for nxt, _cost in self.neighbours(current, symmetric=True):
                    if nxt not in labels:
                        labels[nxt] = label
                        stack.append(nxt)
            sizes.append(size)
        self._components = labels
        self._component_sizes = sizes
        return labels

    def component_sizes(self) -> list[int]:
        self.components()
        return self._component_sizes

    def nearest_walkable(
        self,
        position: tuple[float, float, float],
        *,
        radius: float | None = None,
        vertical: float | None = None,
    ) -> tuple[tuple[int, int, int], float] | None:
        """The walkable cell closest to a world position, and how far away it was.

        Entity origins are not navmesh positions: a spawn sits at its own reference point,
        which may be at the player's feet or at the centre of an editor box, and its floor may
        be a fraction of a cell below. So every lookup snaps, and returns the distance it
        moved so a caller can see when the snap was too generous to believe.
        """
        limit = self.cell * 4 if radius is None else radius
        drop = self.movement.headroom_stand.value if vertical is None else vertical
        cells = max(1, math.ceil(limit / self.cell))
        ix, iy, _iz = self.cell_at(position)
        best: tuple[tuple[int, int, int], float] | None = None
        for ring in range(cells + 1):
            if best is not None:
                # A wider ring cannot beat a hit already inside a narrower one.
                break
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) != ring:
                        continue
                    for iz in self.columns.get((ix + dx, iy + dy), ()):
                        point = self.centre((ix + dx, iy + dy, iz))
                        if abs(point[2] - position[2]) > drop:
                            continue
                        distance = math.dist(point, position)
                        if distance <= limit and (best is None or distance < best[1]):
                            best = ((ix + dx, iy + dy, iz), distance)
        return best

    # --- reporting --------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        headroom = getattr(self.movement, f"headroom_{self.stance}")
        separation = self.movement.floor_separation
        body_height = headroom.value + (separation.value if separation else 0.0)
        return {
            "map": self.map_path,
            "profile": self.profile,
            "stance": self.stance,
            "player_width": self.movement.player_width.as_dict()
            if self.movement.player_width
            else None,
            "clearance_model": "expanded_brush_main_body_box" if self.clearance_index else "point",
            "cell": self.cell,
            "origin": list(self.origin),
            "dims": list(self.dims),
            "counts": self.counts,
            "movement": self.movement.as_dict(),
            "step_up_cells": self.step_up_cells,
            "fall_cells": self.fall_cells,
            "headroom_cells": self.headroom_cells,
            "voxel_headroom_units": self.headroom_cells * self.cell,
            "body_clearance_height_units": body_height,
            "effective_headroom_units": max(self.headroom_cells * self.cell, body_height),
            "geometry": self.geometry,
            "findings": self.findings,
            "notes": self.notes,
        }


#: Grids keyed by the file they were built from, newest last. Two entries, because the
#: comparison that needs more than one — before against after — never comes through here.
_GRID_CACHE: dict[tuple, NavGrid] = {}
_GRID_CACHE_MAX = 2
#: Hits and misses, so a test can prove the cache works without timing anything.
_GRID_CACHE_STATS = {"hits": 0, "misses": 0, "bypassed": 0}


def navgrid_cache_stats() -> dict[str, int]:
    """Hits, misses and bypasses since the last clear. For tests and diagnostics."""
    return dict(_GRID_CACHE_STATS)


def clear_navgrid_cache() -> None:
    _GRID_CACHE.clear()
    for k in _GRID_CACHE_STATS:
        _GRID_CACHE_STATS[k] = 0


def cached_navgrid(
    map_path: str | Path,
    cell: float = DEFAULT_CELL,
    profile_id: str | None = None,
    **kwargs: Any,
) -> NavGrid:
    """`build_navgrid`, reused across calls when it is provably the same grid.

    Voxelizing is the expensive step, and the five gameplay reports each want the same grid
    for the same map. Building it five times is five times the cost for one answer.

    **An in-memory map is never cached.** `game_map=` means "voxelize this parsed map, not
    the file at `map_path`" — which is how `playspace.diff` measures an unsaved edit against
    the file it is about to replace. Both sides of that comparison carry the *same* path and
    the *same* mtime, so any key built from the file would serve the baseline grid for the
    edited map, and `map_save`'s playable-space gate would compare a map against itself and
    pass unconditionally. A gate that always passes is worse than no gate, because it is
    believed. So that call bypasses the cache entirely rather than being keyed around.

    Everything the grid depends on is in the key: the file's identity and mtime, and every
    parameter that changes the result.
    """
    if kwargs.get("game_map") is not None:
        _GRID_CACHE_STATS["bypassed"] += 1
        return build_navgrid(map_path, cell=cell, profile_id=profile_id, **kwargs)

    path = Path(map_path).resolve()
    try:
        stat = path.stat()
        stamp: tuple = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        # Unreadable here means build_navgrid will raise a better error than we could.
        return build_navgrid(map_path, cell=cell, profile_id=profile_id, **kwargs)

    key = (
        str(path),
        stamp,
        cell,
        profile_id,
        kwargs.get("max_cells", MAX_CELLS),
        kwargs.get("max_hull_vertices", MAX_HULL_VERTICES),
        kwargs.get("bucket", DEFAULT_BUCKET),
        bool(kwargs.get("open_state_changing", False)),
        kwargs.get("stance", "stand"),
        frozenset(kwargs.get("nonsolid_entities") or ()),
    )
    hit = _GRID_CACHE.get(key)
    if hit is not None:
        _GRID_CACHE_STATS["hits"] += 1
        return hit

    _GRID_CACHE_STATS["misses"] += 1
    grid = build_navgrid(map_path, cell=cell, profile_id=profile_id, **kwargs)
    _GRID_CACHE[key] = grid
    while len(_GRID_CACHE) > _GRID_CACHE_MAX:
        _GRID_CACHE.pop(next(iter(_GRID_CACHE)))
    return grid


def navgrid_summary(grid: NavGrid) -> dict[str, Any]:
    """The grid's own report, plus how much of it is one connected space.

    `as_dict()` answers "how big is this grid"; the connectivity numbers answer the question
    a designer actually has, which is whether the walkable space is *one* space. A map whose
    standable cells fall into six islands is not a map with six rooms — it is a map five of
    whose rooms cannot be reached, and every distance measured across it is meaningless.

    `reachable_share` is therefore the number to read first. Anything below 1.0 means some
    walkable floor is unreachable from the largest region, and `balance_report`'s path
    lengths should be treated with suspicion until that is explained.
    """
    data = dict(grid.as_dict())
    sizes = sorted(grid.component_sizes(), reverse=True)
    walkable = grid.counts["walkable_cells"]
    data["components"] = len(sizes)
    data["component_sizes"] = sizes[:10]
    data["largest_component_cells"] = sizes[0] if sizes else 0
    data["reachable_share"] = round(sizes[0] / walkable, 4) if sizes and walkable else 0.0
    data["walkable_share_of_volume"] = (
        round(walkable / grid.counts["cells"], 6) if grid.counts["cells"] else 0.0
    )
    return data


RESTRICTED_SAMPLE_CAP = 4096
"""Floor cells kept as examples in `NavGrid.restricted` and `clearance_restricted`."""


def _sparse_kernel_available() -> bool:
    try:
        return hasattr(kernel(), "navgrid_voxelize")
    except KernelUnavailable:
        return False


def _voxelize(
    solids: list[Solid],
    clearance_solids: list[Solid] | None,
    origin: tuple[float, float, float],
    dims: tuple[int, int, int],
    cell: float,
    headroom_cells: int,
    crouch_cells: int,
    step: float,
    *,
    engine: str = "auto",
) -> dict[str, Any]:
    """Occupancy and floors for `build_navgrid`: the kernel's sparse pass, else the reference.

    `engine` is `auto`, `kernel` or `python`. Both give the same cells, counts and samples;
    the kernel does it one x-slab at a time without a byte per cell of the whole map, and
    tests the standing points against the body obstacles natively instead of in Python.
    """
    if engine not in ("auto", "kernel", "python"):
        raise ValueError(f"engine must be auto, kernel or python, got {engine!r}")
    if engine == "python" or (engine == "auto" and not _sparse_kernel_available()):
        return _voxelize_python(
            solids, clearance_solids, origin, dims, cell, headroom_cells, crouch_cells, step
        )

    def convex(rows: list[Solid]) -> list:
        return [(list(s.planes), s.mins, s.maxs) for s in rows]

    raw = kernel().navgrid_voxelize(
        convex([s for s in solids if not s.facet]),
        [(s.planes[0][:3], list(s.facet)) for s in solids if s.facet],
        convex(clearance_solids) if clearance_solids is not None else None,
        origin,
        dims,
        cell,
        headroom_cells,
        crouch_cells,
        RESTRICTED_SAMPLE_CAP,
        PATCH_RASTER_SHIFT,
        step,
    )
    offsets, bounds = array("I"), array("I")
    offsets.frombytes(raw["offsets"])
    bounds.frombytes(raw["bounds"])
    walk_offsets, walk_z, walk_rest = array("I"), array("I"), array("d")
    walk_offsets.frombytes(raw["walk_offsets"])
    walk_z.frombytes(raw["walk_z"])
    walk_rest.frombytes(raw["walk_rest"])
    return {
        "occupancy": ColumnRuns(dims, offsets, bounds),
        "walk": WalkColumns(dims, walk_offsets, walk_z, walk_rest, origin[2], cell),
        "restricted": raw["restricted"],
        "clearance_restricted": raw["clearance_restricted"],
        "solid_cells": raw["solid_cells"],
        "floor_cells": raw["floor_cells"],
        "crouch_only": raw["crouch_only"],
        "too_low": raw["too_low"],
        "body_blocked": raw["body_blocked"],
    }


def _rest_height(
    obstacles: list[Solid],
    candidates: list[int],
    x: float,
    y: float,
    top: float,
    step: float,
) -> float | None:
    """Where a player box dropped onto `(x, y)` comes to rest: the lowest height at or above
    the floor's own `top` that is clear of every body obstacle, rising onto each one that
    holds it. None when that is more than `step` above the floor.

    The engine's box rests on the highest thing under its footprint and steps up onto what
    is within its step height (`PM_StepSlideMove`), so a 1-unit strip of rubble or a floor
    plate is stood on, not a wall. Each obstacle is convex, so it holds an interval of the
    vertical line; rising to the highest top of those holding the body never skips a free
    height, and taking the highest of a set keeps the answer independent of their order.
    """
    z = top
    while True:
        point = (x, y, z)
        held = False
        rise = -math.inf
        for i in candidates:
            solid = obstacles[i]
            if not _aabb_misses(solid, point, point) and (
                _segment_enters(solid, point, (0.0, 0.0, 0.0)) is not None
            ):
                span = solid.z_span(x, y)
                if span is None:
                    return None
                held = True
                rise = max(rise, span[1])
        if not held:
            return z
        if rise <= z:
            # Two obstacles sharing a top can round either side of it: rise past the noise.
            rise = z + REST_NUDGE
        if rise - top > step:
            return None
        z = rise


def _voxelize_python(
    solids: list[Solid],
    clearance_solids: list[Solid] | None,
    origin: tuple[float, float, float],
    dims: tuple[int, int, int],
    cell: float,
    headroom_cells: int,
    crouch_cells: int,
    step: float,
) -> dict[str, Any]:
    """The dense reference voxel pass: one byte per cell of the whole map.

    Kept as the definition the kernel's sparse pass is tested against, and as the fallback
    for a kernel built without it.

    - occupancy is `O(B * columns_in_brush_bounds * planes)` — per brush, not per cell. Each
      column of each brush's bounding box yields one exact z-interval (a brush is convex, so
      its intersection with a vertical line is an interval) which fills as one byte-slice
      assignment.
    - walkability is `O(nx * ny)` Python iterations, each doing a few C-level byte searches
      over its column, rather than `O(nx * ny * nz)`.
    """
    nx, ny, nz = dims
    solid = bytearray(nx * ny * nz)
    tops: dict[int, float] = {}
    ox, oy, oz = origin
    for brush in solids:
        if brush.facet:
            _rasterize_facet(brush, solid, origin, dims, cell, tops)
            continue
        # Cells whose centre can lie inside this brush, from its bounding box.
        cx0 = max(0, math.ceil((brush.mins[0] - ox) / cell - 0.5))
        cx1 = min(nx - 1, math.floor((brush.maxs[0] - ox) / cell - 0.5))
        cy0 = max(0, math.ceil((brush.mins[1] - oy) / cell - 0.5))
        cy1 = min(ny - 1, math.floor((brush.maxs[1] - oy) / cell - 0.5))
        if cx0 > cx1 or cy0 > cy1:
            continue
        for ix in range(cx0, cx1 + 1):
            x = ox + (ix + 0.5) * cell
            base_x = ix * ny
            for iy in range(cy0, cy1 + 1):
                y = oy + (iy + 0.5) * cell
                span = brush.z_span(x, y)
                if span is None:
                    continue
                lo = math.ceil((span[0] - oz) / cell - 0.5)
                hi = math.floor((span[1] - oz) / cell - 0.5)
                if lo > hi:
                    # Thinner than a cell here, between two centres: a 4-unit floor slab over
                    # a void would vanish, and the floor with it. Fill the cell it lies in.
                    if span[1] <= span[0]:
                        continue
                    lo = hi = math.floor(((span[0] + span[1]) / 2 - oz) / cell)
                iz0 = max(0, lo)
                iz1 = min(nz - 1, hi)
                if iz0 > iz1:
                    continue
                start = (base_x + iy) * nz
                solid[start + iz0 : start + iz1 + 1] = _SOLID * (iz1 - iz0 + 1)
                if tops.get(start + iz1, -math.inf) < span[1]:
                    tops[start + iz1] = span[1]

    clearance_index = BucketIndex(clearance_solids) if clearance_solids is not None else None
    clearance_restricted: list[tuple[int, int, int]] = []
    body_blocked_total = 0
    stand_z: dict[tuple[int, int, int], float] = {}

    columns: dict[tuple[int, int], tuple[int, ...]] = {}
    restricted: list[tuple[int, int, int]] = []
    crouch_only_total = 0
    too_low_total = 0
    floor_cells = 0
    restricted_sample_cap = RESTRICTED_SAMPLE_CAP

    for ix in range(nx):
        base_x = ix * ny
        for iy in range(ny):
            start = (base_x + iy) * nz
            end = start + nz
            if solid.find(_SOLID, start, end) < 0:
                continue  # nothing solid in this column, so no floor either
            walkable: list[int] = []
            at = start
            while True:
                found = solid.find(_SOLID_THEN_EMPTY, at, end)
                if found < 0:
                    break
                stand_on = found + 1  # the empty cell resting on that solid one
                at = stand_on
                floor_cells += 1
                obstruction = solid.find(_SOLID, stand_on, end)
                clear = (end if obstruction < 0 else obstruction) - stand_on
                if clear >= headroom_cells:
                    boundary = oz + (stand_on - start) * cell
                    if clearance_index is None or not clearance_solids:
                        rest = boundary
                    else:
                        x, y = ox + (ix + 0.5) * cell, oy + (iy + 0.5) * cell
                        rest = _rest_height(
                            clearance_solids,
                            clearance_index.column(x, y),
                            x,
                            y,
                            tops.get(stand_on - 1, boundary),
                            step,
                        )
                    if rest is None:
                        body_blocked_total += 1
                        if len(clearance_restricted) < restricted_sample_cap:
                            clearance_restricted.append((ix, iy, stand_on - start))
                    else:
                        walkable.append(stand_on - start)
                        if rest != boundary:
                            stand_z[(ix, iy, stand_on - start)] = rest
                    continue
                # Every floor without room to stand is a candidate for `movement_check`,
                # whichever side of crouch height the cell count falls on. The split below is
                # only a summary: both thresholds are rounded up to whole cells, so a genuine
                # crouch space can land in either bucket and only exact measurement can say.
                if clear >= crouch_cells:
                    crouch_only_total += 1
                else:
                    too_low_total += 1
                if len(restricted) < restricted_sample_cap:
                    restricted.append((ix, iy, stand_on - start))
            if walkable:
                columns[(ix, iy)] = tuple(walkable)

    return {
        "occupancy": ColumnRuns.from_dense(dims, solid),
        "walk": WalkColumns.from_dict(dims, columns, stand_z, oz, cell),
        "restricted": restricted,
        "clearance_restricted": clearance_restricted,
        "solid_cells": solid.count(1),
        "floor_cells": floor_cells,
        "crouch_only": crouch_only_total,
        "too_low": too_low_total,
        "body_blocked": body_blocked_total,
    }


def build_navgrid(
    map_path: str | Path,
    cell: float = DEFAULT_CELL,
    profile_id: str | None = None,
    *,
    max_cells: int = MAX_CELLS,
    max_hull_vertices: int = MAX_HULL_VERTICES,
    bucket: float = DEFAULT_BUCKET,
    game_map: Any = None,
    open_state_changing: bool = False,
    stance: str = "stand",
    nonsolid_entities: frozenset[int] = frozenset(),
) -> NavGrid:
    """Voxelize a map into a walkable grid.

    `game_map` voxelizes an already-parsed map instead of re-reading `map_path` from disk.
    That is what lets a caller compare the map it is holding against the file it is about to
    overwrite: without it, "after" would have to be written before it could be measured, which
    is the wrong order for a check whose job is to decide whether to write at all. `map_path`
    is still used to label the result and to word errors.

    A cell is **solid** when its centre is inside any brush, and **walkable** when it is empty,
    the cell below it is solid, and the selected stance's body box fits above it. `stance`
    is `stand`, `crouch` or `prone`; missing profile dimensions are refused, never guessed.

    Complexity, for B brushes and a grid of nx x ny x nz cells:

    - occupancy is `O(B * columns_in_brush_bounds * planes)` — per brush, not per cell. Each
      column of each brush's bounding box yields one exact z-interval (a brush is convex, so
      its intersection with a vertical line is an interval). The kernel fills those into one
      x-slab of `ny * nz` bytes at a time and keeps each column only as its solid runs.
    - each standing place is tested once against the expanded body obstacles that reach its
      column, natively, and comes back flat (`WalkColumns`), not as Python objects.
    - memory is 4 bytes per column plus 8 per solid run (`ColumnRuns`), not a byte per cell,
      and 4 bytes per column plus 12 per standing cell (`WalkColumns`, which also holds the
      resting heights `stand_z` reads): a full-size map at cell 16 holds a few megabytes.

    Raises `AnalysisError` when the map needs more than `max_cells` cells (counting its whole
    bounding box), naming the cap and the cell size that would fit — silently coarsening the
    grid would change every distance in the report without anyone noticing.
    """
    profile = resolve_profile(profile_id)
    movement = movement_constants(profile)
    if not math.isfinite(cell) or cell <= 0:
        raise AnalysisError(f"cell size must be positive, got {cell}")
    if stance not in ("stand", "crouch", "prone"):
        raise AnalysisError("stance must be stand, crouch or prone")
    headroom = getattr(movement, f"headroom_{stance}")
    if headroom is None or not math.isfinite(headroom.value) or headroom.value <= 0:
        raise AnalysisError(f"profile {profile} states no positive headroom_{stance}")
    separation = movement.floor_separation
    if separation and (not math.isfinite(separation.value) or separation.value < 0):
        raise AnalysisError(f"profile {profile} floor_separation must be finite and non-negative")
    body_height = headroom.value + (separation.value if separation else 0.0)

    path = _resolve_map_path(map_path)
    if game_map is None:
        game_map = load_map(path)
    role_map = roles(profile)
    solids, provenance = collect_solids(
        game_map,
        role_map,
        max_hull_vertices=max_hull_vertices,
        open_state_changing=open_state_changing,
        contents=shader_contents(profile, path),
        nonsolid_entities=frozenset(nonsolid_entities),
    )
    if not solids:
        raise AnalysisError(
            f"{path.name} has no brush the kernel can evaluate "
            f"({provenance['brushes']} brush(es) seen, "
            f"{provenance['brushes_indeterminate']} indeterminate), so there is nothing to "
            "voxelize"
        )

    # Restrict to the geometry's own bounds, aligned to the cell size so cells line up with
    # the authoring grid rather than with an arbitrary corner.
    mins = tuple(min(s.mins[i] for s in solids) for i in range(3))
    maxs = tuple(max(s.maxs[i] for s in solids) for i in range(3))
    origin = tuple(math.floor(mins[i] / cell) * cell for i in range(3))
    dims = tuple(max(1, math.ceil((maxs[i] - origin[i]) / cell)) for i in range(3))
    nx, ny, nz = dims
    total = nx * ny * nz
    if total > max_cells:
        needed = cell * (total / max_cells) ** (1.0 / 3.0)
        raise AnalysisError(
            f"{path.name} needs {nx}x{ny}x{nz} = {total:,} cells at cell={cell:g}, over the "
            f"max_cells cap of {max_cells:,}. Use cell>={math.ceil(needed)} (or raise "
            f"max_cells, which costs time in proportion). The map measures "
            f"{maxs[0] - mins[0]:.0f}x{maxs[1] - mins[1]:.0f}x{maxs[2] - mins[2]:.0f} units."
        )

    width = movement.player_width
    # Keep voxel rounding of the nominal body height. The exact expanded brush test then
    # reserves the engine's floor separation without adding a whole extra vertical cell.
    # A profile without width has no expanded body test, so include it in the cell test.
    headroom_cells = max(1, math.ceil((headroom.value if width else body_height) / cell))
    crouch = movement.headroom_crouch
    crouch_cells = max(1, math.ceil(crouch.value / cell)) if crouch else headroom_cells
    clearance_solids = _body_obstacles(solids, width.value / 2, body_height) if width else []
    clearance_index = BucketIndex(clearance_solids, bucket) if width else None

    if not _sparse_kernel_available() and total > DENSE_MAX_CELLS:
        raise AnalysisError(
            f"{path.name} needs {total:,} cells at cell={cell:g}, and this kernel build has no "
            f"sparse voxel pass, so the dense fallback is capped at {DENSE_MAX_CELLS:,} cells "
            "(max_cells). Rebuild the kernel (mise run kernel:build), or use cell>="
            f"{math.ceil(cell * (total / DENSE_MAX_CELLS) ** (1.0 / 3.0))}."
        )
    voxels = _voxelize(
        solids,
        clearance_solids if width else None,
        origin,
        dims,
        cell,
        headroom_cells,
        crouch_cells,
        movement.step_height.value,
    )
    occupancy = voxels["occupancy"]
    columns: WalkColumns = voxels["walk"]
    restricted: list[tuple[int, int, int]] = voxels["restricted"]
    clearance_restricted: list[tuple[int, int, int]] = voxels["clearance_restricted"]
    crouch_only_total = voxels["crouch_only"]
    too_low_total = voxels["too_low"]
    floor_cells = voxels["floor_cells"]
    body_blocked_total = voxels["body_blocked"]

    step_up_cells = max(0, math.floor(movement.step_height.value / cell))
    fall = movement.fall_no_damage
    fall_cells = max(step_up_cells, math.floor(fall.value / cell)) if fall else step_up_cells

    walkable_total = columns.cell_count()
    counts = {
        "cells": total,
        "solid_cells": voxels["solid_cells"],
        "floor_cells": floor_cells,
        "walkable_cells": walkable_total,
        "walkable_columns": len(columns),
        "crouch_only_floor_cells": crouch_only_total,
        "too_low_floor_cells": too_low_total,
        "body_blocked_floor_cells": body_blocked_total,
        "solids": len(solids),
    }

    findings: list[dict] = []
    if provenance["brushes_indeterminate"]:
        share = provenance["brushes_indeterminate"] / max(1, provenance["brushes"])
        findings.append(
            _finding(
                "NAV_GEOMETRY_INCOMPLETE",
                "warning",
                f"{provenance['brushes_indeterminate']} of {provenance['brushes']} brush(es) "
                f"({share:.0%}) have no geometry the kernel can derive, exactly or in floating "
                "point (collinear plane points, too many faces, or planes that enclose "
                "nothing), so they are absent from this grid. Space they occupy reads as "
                "empty, so a path may cross a wall and a clearance may look larger than it "
                "is. Examples: " + "; ".join(provenance.get("indeterminate_examples") or []),
                "verified",
                "nrc_mcp.analysis: kernel returned no geometry for these brushes",
                "fix or delete the offending brushes (map_validate names them), then re-run",
            )
        )
    if provenance["patches_unmodelled"]:
        examples = "; ".join(provenance.get("patch_unmodelled_examples") or [])
        findings.append(
            _finding(
                "NAV_PATCHES_IGNORED",
                "warning",
                f"{provenance['patches_unmodelled']} solid patch(es) are not in this grid "
                "because their control grid is one the engine refuses or cannot hold "
                f"({examples}). The engine stops loading a map over such a patch, so fix it "
                "before trusting any route here.",
                "verified",
                "nrc_solid::patch::collision mirrors CM_GeneratePatchCollide "
                "(qcommon/cm_patch.c:1451-1475, MAX_FACETS at cm_patch.h:77)",
                "give the patch odd dimensions of at least 3x3, or split it",
            )
        )
    if provenance["patches_shader_unresolved"]:
        findings.append(
            _finding(
                "NAV_PATCH_CONTENTS_GUESSED",
                "info",
                f"{provenance['patches_shader_unresolved']} patch(es) were judged solid or "
                "not by shader name alone, because no shader script could be read (no project "
                "scripts/ folder and no base game at NRC_FS_BASEPATH). A decal or trim whose "
                "script says nonsolid is then a wall here.",
                "verified",
                "nrc_mcp.analysis.shader_contents read no scripts",
                "set NRC_FS_BASEPATH to the game install, then re-run",
            )
        )
    if provenance["brushes_bounds_approximated"]:
        findings.append(
            _finding(
                "NAV_BRUSH_APPROXIMATED",
                "info",
                f"{provenance['brushes_bounds_approximated']} brush(es) have more than "
                f"{max_hull_vertices} vertices and were treated as their bounding boxes, which "
                "over-fills the space around them",
                "verified",
                f"nrc_mcp.analysis: max_hull_vertices={max_hull_vertices}",
                "raise max_hull_vertices to trade time for accuracy",
            )
        )
    if not walkable_total:
        findings.append(
            _finding(
                "NAV_NO_WALKABLE_SPACE",
                "warning",
                f"no cell fits the {stance} player box with {body_height:g} units of clear "
                f"space above a floor (needed {headroom_cells} cell(s) of "
                f"{cell:g}, so {headroom_cells * cell:g} units). Either the map has no "
                "sealed interior at this cell size, or the cell size is too coarse for it.",
                "unverified",
                f"profile {profile}: {headroom.key}; voxel body clearance",
                f"try a smaller cell, e.g. cell={max(1, int(cell // 2))}",
            )
        )

    notes = [
        "A voxel grid is not an AAS navmesh: it models a player box walking and "
        "falling, and knows nothing about jumps, ladders, water or teleporters. "
        "Expanded face planes conservatively block some angled brush corners.",
        "the grid spans the geometry's own bounds, so the outermost top surface has no space "
        "modelled above it and is never walkable — correct for a sealed map's sky shell, and "
        "wrong for a map whose highest surface is a rooftop players use",
        f"{stance} headroom {headroom.value:g} units "
        f"({headroom.confidence}, {headroom.source}) is "
        f"enforced as {headroom_cells} cell(s) = {headroom_cells * cell:g} units, so the grid "
        "under-reports walkable space rather than over-reporting it",
        f"step up {movement.step_height.value:g} units -> {step_up_cells} cell(s); "
        f"descent limit {fall.value:g} units -> {fall_cells} cell(s)"
        if fall
        else f"step up {movement.step_height.value:g} units -> {step_up_cells} cell(s); the "
        "profile states no fall tolerance, so descents are limited to the step height",
    ]
    if not width:
        notes.append(
            "the profile states no player width; body clearance is unchecked, so routes model a point"
        )
    if separation:
        notes.append(
            f"profile floor separation {separation.value:g} units ({separation.confidence}, "
            f"{separation.source}) raises the nominal {stance} height {headroom.value:g} to "
            f"{body_height:g} units for body clearance; exact-fit openings are not assumed passable"
        )
    if stance == "prone":
        notes.append(
            "prone uses the main body box only; leg collision, orientation and stance transitions are not modelled"
        )
    if provenance["patches_used"]:
        notes.append(
            f"{provenance['patches_used']} patch(es) are modelled as the engine's own "
            f"{provenance['patch_facets']} collision facets: the curve subdivided only until "
            f"each chord is within {PATCH_SUBDIVIDE_DISTANCE:g} units of it, as the engine does, "
            "so a small pipe collides as a polygon well inside its drawn curve. A facet "
            "fills every cell it passes through, so a patch floor or ceiling can read up to a "
            "cell nearer than a brush face at the same height would. Facets block from both "
            "sides, as the engine's bevelled facets did against a player box in testing"
        )

    return NavGrid(
        map_path=str(path),
        profile=profile,
        cell=float(cell),
        origin=origin,
        dims=dims,
        occupancy=occupancy,
        columns=columns,
        movement=movement,
        step_up_cells=step_up_cells,
        fall_cells=fall_cells,
        headroom_cells=headroom_cells,
        solids=solids,
        index=BucketIndex(solids, bucket),
        game_map=game_map,
        geometry=provenance,
        counts=counts,
        restricted=restricted,
        crouch_only_total=crouch_only_total,
        too_low_total=too_low_total,
        findings=findings,
        notes=notes,
        stance=stance,
        clearance_solids=clearance_solids,
        clearance_index=clearance_index,
        clearance_restricted=clearance_restricted,
        stand_z=columns.heights(),
    )


# ---------------------------------------------------------------------------
# A*
# ---------------------------------------------------------------------------


def _decimate(points: list[list[float]], max_points: int) -> list[list[float]]:
    """Keep the corners of a path, then thin what is left to fit `max_points`."""
    if len(points) <= 2:
        return points
    kept = [points[0]]
    for previous, current, following in zip(points, points[1:], points[2:], strict=False):
        before = tuple(round(current[i] - previous[i], 6) for i in range(3))
        after = tuple(round(following[i] - current[i], 6) for i in range(3))
        if before != after:
            kept.append(current)
    kept.append(points[-1])
    if len(kept) <= max_points:
        return kept
    stride = math.ceil(len(kept) / (max_points - 1))
    thinned = kept[::stride]
    if thinned[-1] != kept[-1]:
        thinned.append(kept[-1])
    return thinned


def _astar_python(
    grid: NavGrid,
    start_cell: tuple[int, int, int],
    goal_cell: tuple[int, int, int],
    max_nodes: int,
) -> tuple[str, int, float | None, list[tuple[int, int, int]]]:
    """The reference search: `(status, expanded, cost, cells)`, status found/exhausted/budget.

    Kept as the definition the native search (`routesearch`) is tested against, and as the
    fallback when the loaded kernel predates it.
    """
    goal_point = grid.centre(goal_cell)

    def heuristic(cell: tuple[int, int, int]) -> float:
        return math.dist(grid.centre(cell), goal_point)

    open_heap: list[tuple[float, int, tuple[int, int, int]]] = [
        (heuristic(start_cell), 0, start_cell)
    ]
    best: dict[tuple[int, int, int], float] = {start_cell: 0.0}
    came: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    closed: set[tuple[int, int, int]] = set()
    expanded = 0
    tie = 0

    while open_heap:
        _priority, _tie, current = heapq.heappop(open_heap)
        if current in closed:
            continue
        closed.add(current)
        expanded += 1
        if current == goal_cell:
            break
        if expanded > max_nodes:
            return "budget", expanded, None, []
        cost_here = best[current]
        for neighbour, step in grid.neighbours(current):
            if neighbour in closed:
                continue
            candidate = cost_here + step
            if candidate < best.get(neighbour, math.inf):
                best[neighbour] = candidate
                came[neighbour] = current
                tie += 1
                heapq.heappush(open_heap, (candidate + heuristic(neighbour), tie, neighbour))

    if goal_cell not in best:
        return "exhausted", expanded, None, []
    cells = [goal_cell]
    while cells[-1] != start_cell:
        cells.append(came[cells[-1]])
    cells.reverse()
    return "found", expanded, best[goal_cell], cells


def _astar(
    grid: NavGrid,
    start_cell: tuple[int, int, int],
    goal_cell: tuple[int, int, int],
    max_nodes: int,
    bound: float | None = None,
    engine: str = "auto",
) -> tuple[str, int, bool, float | None, list[tuple[int, int, int]]]:
    """`(status, expanded, pruned, cost, cells)`, by the native search when the kernel has it.

    `engine` is `auto`, `native` or `python`. `bound` (native only) prunes every node whose
    straight-line lower bound on a route through it exceeds the bound.
    """
    from . import routesearch  # noqa: PLC0415

    if engine == "python" or (engine == "auto" and not routesearch.available()):
        if bound is not None:
            raise AnalysisError("a search bound needs the native route search; rebuild the kernel")
        status, expanded, cost, cells = _astar_python(grid, start_cell, goal_cell, max_nodes)
        return status, expanded, False, cost, cells
    with routesearch.LOCK:
        graph, expand = routesearch.step_graph(grid)
        status, expanded, pruned, cost, path = graph.search(
            [(0, *start_cell)],
            [(0, *goal_cell)],
            [grid.centre(goal_cell)],
            expand,
            max_nodes=max_nodes,
            budget="after_goal_test",
            bound=bound,
        )
    return status, expanded, pruned, cost, [(x, y, z) for _layer, x, y, z in path]


def path_distance(
    grid: NavGrid,
    from_xyz: tuple[float, float, float],
    to_xyz: tuple[float, float, float],
    *,
    max_nodes: int = MAX_A_STAR_NODES,
    max_path_points: int = 48,
    snap_radius: float | None = None,
    max_detour: float | None = None,
    use_components: bool = True,
    engine: str = "auto",
    _snapped: tuple | None = None,
) -> dict[str, Any]:
    """Shortest walkable distance between two world positions, by A* over the grid.

    Steps up to the profile's step height and drops within its no-damage fall distance are
    allowed between neighbouring columns; the cost of a move is its true 3D length, so the
    returned distance is in world units and stairs cost what they actually cost. The heuristic
    is straight-line distance, which never exceeds the remaining path and so cannot make A*
    return a long route.

    Complexity is `O(E log V)` over the walkable graph, with at most 8 horizontal neighbours
    per cell. A definite "no" comes from the cached component labelling rather than from
    exhausting the search; a route blocked only in one direction — a ledge that can be walked
    off but not climbed — still costs a full search of its component. Never raises: an
    unreachable pair is a result, not an error.

    The search runs in the kernel (`routesearch`), which learns each cell's edges once per
    grid, so later queries over the same grid get cheaper. `max_detour` bounds the answer:
    a route longer than `max_detour` times the straight line is not looked for (the search
    stays inside the ellipse such a route could use) and the pair reports
    `exceeds_max_detour`. `use_components=False` skips labelling the whole grid; a pair in
    different components is then found unreachable by the search (or the bound) instead,
    with the same answer and a less specific reason.
    """
    if _snapped is None:
        start = grid.nearest_walkable(from_xyz, radius=snap_radius)
        goal = grid.nearest_walkable(to_xyz, radius=snap_radius)
    else:
        start, goal = _snapped
    limit = grid.cell * 4 if snap_radius is None else snap_radius
    if start is None or goal is None:
        which = "start" if start is None else "goal"
        return {
            "reachable": False,
            "reason": (
                f"no walkable cell within {limit:g} units of the {which} position "
                f"{list(from_xyz if start is None else to_xyz)}"
            ),
            "distance": None,
            "nodes_expanded": 0,
            "path": [],
        }

    (start_cell, start_snap), (goal_cell, goal_snap) = start, goal
    labels = grid.components() if use_components else None
    if labels is not None and labels.get(start_cell) != labels.get(goal_cell):
        return {
            "reachable": False,
            "reason": (
                "the two positions are in different connected components of the walkable "
                "grid; at this cell size nothing links them"
            ),
            "distance": None,
            "nodes_expanded": 0,
            "path": [],
            "from_cell": list(start_cell),
            "to_cell": list(goal_cell),
            "snap_distance": [round(start_snap, 2), round(goal_snap, 2)],
            "components": [labels.get(start_cell), labels.get(goal_cell)],
        }

    straight = math.dist(from_xyz, to_xyz)
    bound = None
    if max_detour is not None:
        # The reported ratio divides the cell-to-cell cost by the requested straight line,
        # so bound that product, but never below the gap between the snapped cells.
        bound = max(
            max_detour * straight, math.dist(grid.centre(start_cell), grid.centre(goal_cell))
        )
    status, expanded, pruned, cost, cells = _astar(
        grid, start_cell, goal_cell, max_nodes, bound=bound, engine=engine
    )
    if status == "budget":
        return {
            "reachable": False,
            "reason": (
                f"gave up after expanding {max_nodes:,} nodes (max_nodes); the grid may be "
                "finer than this search budget allows"
            ),
            "distance": None,
            "nodes_expanded": expanded,
            "path": [],
        }
    if status == "exhausted":
        if pruned:
            reason = (
                f"no walkable route within max_detour={max_detour:g} times the straight line "
                f"({bound:.2f} units); a longer one was not looked for"
            )
        elif labels is not None:
            reason = (
                "no walkable route leads there, though the two are in the same component — "
                "usually a one-way drop, which can be walked off but not climbed back up"
            )
        else:
            reason = (
                "no walkable route leads there: the two are not connected, or only by a "
                "one-way drop that can be walked off but not climbed back up"
            )
        row = {
            "reachable": False,
            "reason": reason,
            "distance": None,
            "nodes_expanded": expanded,
            "path": [],
            "from_cell": list(start_cell),
            "to_cell": list(goal_cell),
        }
        if pruned:
            row["exceeds_max_detour"] = True
        return row

    assert cost is not None
    points = [[round(v, 2) for v in grid.centre(c)] for c in cells]
    decimated = _decimate(points, max_path_points)

    return {
        "reachable": True,
        "distance": round(cost, 2),
        "straight_line": round(straight, 2),
        "detour_ratio": round(cost / straight, 3) if straight > 0 else None,
        "nodes_expanded": expanded,
        "path_cells": len(cells),
        "path": decimated,
        "path_decimated": len(decimated) < len(points),
        "from_cell": list(start_cell),
        "to_cell": list(goal_cell),
        "snap_distance": [round(start_snap, 2), round(goal_snap, 2)],
    }


#: Most pairs one `path_distances` call takes.
MAX_LINKS = 20_000
#: Fewest pairs for which a batch learns its cells ahead, in parallel.
PREFETCH_MIN_LINKS = 32


def path_distances(
    grid: NavGrid,
    pairs: list,
    *,
    max_nodes: int = MAX_A_STAR_NODES,
    snap_radius: float | None = None,
    max_detour: float | None = None,
    with_paths: bool = False,
    max_path_points: int = 48,
    use_components: bool | None = None,
    engine: str = "auto",
    workers: int | None = None,
) -> list[dict[str, Any]]:
    """`path_distance` for many `(from_xyz, to_xyz)` pairs over one grid, in order.

    The batch form of a link check: each cell's edges are learned once and shared by every
    pair, and an endpoint that repeats (a waypoint with several links) snaps once. Rows are
    `path_distance` results, without `path` unless `with_paths`.

    `use_components` defaults to whether the grid has labelled its components already:
    labelling costs a pass over every cell, which a batch of short links on a large grid
    would otherwise pay for nothing. Reachability and distances are the same either way.

    A large batch first learns the cells around its links in `workers` forked processes
    (default `routesearch.workers()`; 1 = learn lazily, one cell at a time, as a single
    query does). The graph is the same either way, and so are the answers.
    """
    from . import routesearch  # noqa: PLC0415

    if len(pairs) > MAX_LINKS:
        raise AnalysisError(f"at most {MAX_LINKS:,} pairs per call, got {len(pairs):,}")
    if engine != "python" and routesearch.available() and len(pairs) >= PREFETCH_MIN_LINKS:
        graph, expand = routesearch.step_graph(grid)
        near = routesearch.cells_near(grid, pairs, routesearch.link_pad(grid, max_detour))
        routesearch.learn(graph, expand, [(0, *c) for c in near], processes=workers)
    if use_components is None:
        use_components = getattr(grid, "_components", None) is not None
    snaps: dict[tuple, Any] = {}

    def snap(point: tuple[float, float, float]):
        if point not in snaps:
            snaps[point] = grid.nearest_walkable(point, radius=snap_radius)
        return snaps[point]

    rows = []
    for a, b in pairs:
        a, b = tuple(a), tuple(b)
        row = path_distance(
            grid,
            a,
            b,
            max_nodes=max_nodes,
            max_path_points=max_path_points,
            snap_radius=snap_radius,
            max_detour=max_detour,
            use_components=use_components,
            engine=engine,
            _snapped=(snap(a), snap(b)),
        )
        if not with_paths:
            row.pop("path", None)
            row.pop("path_decimated", None)
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Entity collection shared by the reports
# ---------------------------------------------------------------------------


def _entity_keys(entity: dict) -> dict[str, str]:
    """Keys as the game reads them: any case, the last of a duplicate (`rules._keys`)."""
    return _rule_keys({"keys": [(k, str(v)) for k, v in entity.get("keys") or []]})


@dataclass
class Marker:
    """A spawn point or objective, with the role information the profile gave it."""

    entity: int
    classname: str
    origin: tuple[float, float, float]
    team: str
    group: str
    #: Bounds of a brush entity with no origin key; None for point entities. Paths to a brush
    #: objective go to its floor footprint (`_approach_points`), not to the centre in `origin`.
    bounds: tuple[list[float], list[float]] | None = None
    #: Other entities represented by this objective's approach area and team.
    linked_entities: tuple[int, ...] = ()

    @property
    def entity_indices(self) -> tuple[int, ...]:
        return (self.entity, *self.linked_entities)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entity": self.entity,
            "classname": self.classname,
            "origin": [round(v, 2) for v in self.origin],
            "team": self.team,
            "group": self.group,
            **({"linked_entities": list(self.linked_entities)} if self.linked_entities else {}),
        }


def _brush_centre(game_map: Any, entity: dict) -> tuple[float, float, float] | None:
    """The centre of a brush entity's brushes, for an entity with no origin key.

    Objectives are often brush entities — a constructible, a trigger volume — and without this
    they would be counted as markers without a position and dropped from every report.
    """
    bounds = _brush_bounds(game_map, entity)
    if bounds is None:
        return None
    lo, hi = bounds
    return ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)


def _approach_points(
    bounds: tuple[list[float], list[float]], margin: float
) -> tuple[tuple[float, float, float], ...]:
    """Where a player stands to use a brush objective: on the floor of its footprint.

    The centre of the brushes is the wrong goal. A trigger's centre floats half its height up,
    and a solid objective's centre is inside it - both snap to the walkable top of whatever is
    nearest, often the objective's own roof, which no spawn can reach. So the footprint's
    bottom centre (inside a trigger volume) and a floor point just outside each side (beside a
    solid) are tried, and the objective counts as reached from the nearest of them.
    """
    lo, hi = bounds
    cx, cy, z = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]
    return (
        (cx, cy, z),
        (lo[0] - margin, cy, z),
        (hi[0] + margin, cy, z),
        (cx, lo[1] - margin, z),
        (cx, hi[1] + margin, z),
    )


def _path_to_marker(navgrid: NavGrid, start: tuple[float, float, float], marker: Marker) -> dict:
    """`path_distance` to a marker: its origin, or the nearest reachable approach point of a
    brush objective. With none reachable, the result of the first (the footprint centre) is
    returned, so the reason names a real place rather than the last side tried."""
    if marker.bounds is None:
        return path_distance(navgrid, start, marker.origin)
    results = [
        path_distance(navgrid, start, p) for p in _approach_points(marker.bounds, navgrid.cell)
    ]
    reached = [r for r in results if r["reachable"] and r.get("distance") is not None]
    return min(reached, key=lambda r: r["distance"]) if reached else results[0]


def _brush_bounds(game_map: Any, entity: dict) -> tuple[list[float], list[float]] | None:
    """Axis-aligned bounds of a brush entity's usable brushes and patches, or None."""
    index = int(entity["index"])
    lo = [math.inf] * 3
    hi = [-math.inf] * 3
    for primitive in range(int(entity.get("brushes") or 0) + int(entity.get("patches") or 0)):
        try:
            geometry = game_map.brush_geometry(index, primitive, approximate=True)
        except ValueError:
            continue
        bounds = geometry.get("bounds") if geometry.get("usable") else None
        if not bounds:
            continue
        for axis in range(3):
            lo[axis] = min(lo[axis], float(bounds[0][axis]))
            hi[axis] = max(hi[axis], float(bounds[1][axis]))
    if lo[0] == math.inf:
        return None
    return lo, hi


def _group_linked_objectives(
    objectives: list[Marker], entities: list[dict], role_map: Roles
) -> list[Marker]:
    """Keep one approach marker for an explicit, unambiguous profile-declared pair.

    Names, proximity and class alone never imply duplication. Count every named entity and
    referring marker, including ones without usable geometry, so ambiguous or broken links
    stay visible. Multi-target and shared-target arrangements need separate modelling.
    """
    by_index = {marker.entity: marker for marker in objectives}
    claimed: set[int] = set()
    represented: set[int] = set()
    for link in role_map.objective_links:
        targets: dict[str, list[int]] = {}
        sources: dict[str, list[int]] = {}
        for entity in entities:
            keys = _entity_keys(entity)
            name = keys.get(link.target_name_key, "")
            reference = keys.get(link.target_key, "")
            if not link.case_sensitive:
                name, reference = name.casefold(), reference.casefold()
            index = int(entity["index"])
            if name:
                targets.setdefault(name, []).append(index)
            if reference and entity.get("classname") == link.classname:
                sources.setdefault(reference, []).append(index)
        for name, source_ids in sources.items():
            target_ids = targets.get(name, [])
            if len(source_ids) != 1 or len(target_ids) != 1:
                continue
            source = by_index.get(source_ids[0])
            target = by_index.get(target_ids[0])
            if (
                source is None
                or target is None
                or target.classname not in link.target_classnames
                or source.entity == target.entity
                or claimed.intersection((source.entity, target.entity))
            ):
                continue
            source.linked_entities = (target.entity,)
            claimed.update((source.entity, target.entity))
            represented.add(target.entity)
    return [marker for marker in objectives if marker.entity not in represented]


def _markers(game_map: Any, role_map: Roles) -> tuple[list[Marker], list[Marker], int]:
    spawns: list[Marker] = []
    objectives: list[Marker] = []
    # (origin, group name) of each entity spawns are grouped around, when the profile says so.
    anchors: list[tuple[tuple[float, float, float], str]] = []
    without_origin = 0
    entities = game_map.entities()
    for entity in entities:
        classname = str(entity.get("classname") or "")
        is_spawn = classname in role_map.spawns
        is_objective = classname in role_map.objectives
        is_anchor = bool(role_map.group_nearest) and classname == role_map.group_nearest
        if not (is_spawn or is_objective or is_anchor):
            continue
        origin = entity.get("origin")
        bounds = None
        if origin:
            point = (float(origin[0]), float(origin[1]), float(origin[2]))
        else:
            bounds = _brush_bounds(game_map, entity) if entity.get("brushes") else None
            point = None
            if bounds is not None:
                lo, hi = bounds
                point = ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2)
        if point is None:
            without_origin += int(is_spawn or is_objective)
            continue
        keys = _entity_keys(entity)
        if is_anchor:
            name = keys.get(role_map.group_label_key, "") if role_map.group_label_key else ""
            label = f"entity {entity['index']}"
            anchors.append((point, f"{name} ({label})" if name else label))
        if not (is_spawn or is_objective):
            continue
        marker = Marker(
            entity=int(entity["index"]),
            classname=classname,
            origin=point,
            team=role_map.team_of(classname, keys),
            group=str(keys.get(role_map.group_key, "")) if role_map.group_key else "",
            bounds=bounds,
        )
        (spawns if is_spawn else objectives).append(marker)

    # A profile without a group key groups spawns by the nearest anchor instead, which is how
    # its engine picks among them. A spawn with a group key keeps it.
    if anchors:
        for marker in spawns:
            if not marker.group:
                marker.group = min(anchors, key=lambda a: math.dist(a[0], marker.origin))[1]
    return spawns, _group_linked_objectives(objectives, entities, role_map), without_origin


def _resolve_map_path(map_path: str | Path) -> Path:
    path = Path(map_path)
    return path if path.is_absolute() else repo_root() / path


# ---------------------------------------------------------------------------
# balance_report
# ---------------------------------------------------------------------------


def _symmetry(
    points: list[Marker], centre: tuple[float, float], tolerance: float
) -> dict[str, Any]:
    """Whether mirroring the map about its centre maps the marker set onto itself.

    Three candidate transforms, all about the vertical axis: mirror in x, mirror in y, and the
    180-degree rotation that is both. A CTF map is usually one of them, and which one tells a
    reader something a distance table does not — that the two halves are meant to be identical,
    so any asymmetry in the distances is a defect rather than a design choice.

    A match requires the counterpart to belong to a *different* team where teams are known,
    which is what distinguishes a mirrored map from a map whose own half is symmetric.
    """
    if len(points) < 2:
        return {"tested": False, "reason": "not enough spawn or objective markers to test"}

    transforms = {
        "mirror_x": lambda p: (2 * centre[0] - p[0], p[1], p[2]),
        "mirror_y": lambda p: (p[0], 2 * centre[1] - p[1], p[2]),
        "rotate_180": lambda p: (2 * centre[0] - p[0], 2 * centre[1] - p[1], p[2]),
    }
    teams = {m.team for m in points} - {UNASSIGNED_TEAM}
    results = []
    for name, transform in transforms.items():
        matched = 0
        residuals = []
        for marker in points:
            image = transform(marker.origin)
            best = None
            for other in points:
                if teams and marker.team != UNASSIGNED_TEAM and other.team == marker.team:
                    continue
                distance = math.dist(image, other.origin)
                if best is None or distance < best:
                    best = distance
            if best is not None and best <= tolerance:
                matched += 1
                residuals.append(best)
        results.append(
            {
                "transform": name,
                "matched": matched,
                "of": len(points),
                "fraction": round(matched / len(points), 3),
                "mean_residual": round(sum(residuals) / len(residuals), 2) if residuals else None,
            }
        )
    best_result = max(results, key=lambda r: (r["matched"], -(r["mean_residual"] or 0.0)))
    return {
        "tested": True,
        "centre": [round(centre[0], 2), round(centre[1], 2)],
        "tolerance": tolerance,
        "candidates": results,
        "best": best_result,
        "symmetric": best_result["fraction"] >= 0.9,
    }


def balance_report(
    map_path: str | Path,
    profile_id: str | None = None,
    *,
    cell: float = DEFAULT_CELL,
    grid: NavGrid | None = None,
    max_paths: int = 64,
    symmetry_tolerance: float | None = None,
    open_state_changing: bool = False,
    stance: str = "stand",
) -> dict[str, Any]:
    """Per-team traversal distance from each spawn group to each objective (§7.3).

    `open_state_changing` walks the map as it is once the brushes the profile lists under
    `roles.state_changing` (blown doors, built bridges) have changed state, by leaving them out
    of collision. Without it the map is measured as it starts the round, where a door the
    attackers must blow cuts them off from what is behind it.

    Distances are walked, not straight-line: one A* run per spawn group per objective, using
    the group's centroid, which keeps the number of searches proportional to the number of
    *groups* rather than to the number of spawn points. Asymmetry between teams is the number
    that matters — a map where one team reaches an objective in half the distance is broken
    however pretty it is.

    Which classnames are spawns, which are objectives, and which keys carry team and group all
    come from the profile. There is no traversal *time* here, only distance: §7.3 asks for
    time, and the profile states no player speed, so estimating one would be inventing the
    constant this project exists to avoid inventing.
    """
    try:
        # A supplied grid is the source of truth for which profile this report is about: it was
        # built against one, and resolving again could quietly pick a different one.
        navgrid = grid or cached_navgrid(
            map_path,
            cell=cell,
            profile_id=profile_id,
            open_state_changing=open_state_changing,
            stance=stance,
        )
    except AnalysisError as e:
        return {"error": str(e), "profile": profile_id or ""}
    profile = navgrid.profile

    # The grid already holds the parsed map, so reports never re-read the file. When a caller
    # supplies a grid, that grid's map is the one described.
    game_map, path = navgrid.game_map, Path(navgrid.map_path)
    role_map = roles(profile)
    spawns, objectives, without_origin = _markers(game_map, role_map)

    findings: list[dict] = list(navgrid.findings)
    notes = [
        "distances are walked over the voxel grid, so they are approximate; the straight-line "
        "distance is reported next to each for comparison",
        "no traversal time is estimated: the profile states no player movement speed, and "
        "guessing one would defeat the point of reading constants from data",
    ]
    if open_state_changing and grid is None:
        opened = sorted(role_map.state_changing)
        notes.append(
            "walked with state-changing brushes open ("
            + (", ".join(opened) if opened else "the profile lists none")
            + "): the map once objectives have been blown or built, not as it starts"
        )

    if not spawns:
        findings.append(
            _finding(
                "BALANCE_NO_SPAWNS",
                "info",
                f"no entity in the map has a classname the profile categorizes as a spawn "
                f"({len(role_map.spawns)} such classnames known)",
                "verified",
                f"profile {profile} entity categories",
            )
        )
    if not objectives:
        findings.append(
            _finding(
                "BALANCE_NO_OBJECTIVES",
                "info",
                "no entity in the map has a classname the profile categorizes as an objective, "
                "so there is nothing to measure distances to",
                "verified",
                f"profile {profile} entity categories",
            )
        )

    # Group spawns by (team, group). Both come from profile-named keys; a spawn with neither
    # still forms its own group so nothing is silently dropped.
    groups: dict[tuple[str, str], list[Marker]] = {}
    for spawn in spawns:
        groups.setdefault((spawn.team, spawn.group), []).append(spawn)

    group_rows = []
    for (team, group), members in sorted(groups.items()):
        centroid = tuple(sum(m.origin[i] for m in members) / len(members) for i in range(3))
        group_rows.append(
            {
                "team": team,
                "group": group,
                "spawns": len(members),
                "classnames": sorted({m.classname for m in members}),
                "centroid": [round(v, 2) for v in centroid],
                "_centroid": centroid,
            }
        )

    distances: list[dict[str, Any]] = []
    budget = max_paths
    for row in group_rows:
        for objective in objectives:
            if budget <= 0:
                break
            budget -= 1
            result = _path_to_marker(navgrid, row["_centroid"], objective)
            distances.append(
                {
                    "team": row["team"],
                    "group": row["group"],
                    "objective_entity": objective.entity,
                    "objective_entities": list(objective.entity_indices),
                    "objective": objective.classname,
                    "objective_team": objective.team,
                    "reachable": result["reachable"],
                    "distance": result.get("distance"),
                    "straight_line": round(math.dist(row["_centroid"], objective.origin), 2),
                    "reason": result.get("reason"),
                }
            )
    truncated = budget <= 0 and len(group_rows) * max(1, len(objectives)) > max_paths
    if truncated:
        notes.append(
            f"stopped after {max_paths} path searches (max_paths); "
            f"{len(group_rows)} group(s) x {len(objectives)} objective(s) would need "
            f"{len(group_rows) * len(objectives)}"
        )

    # Per team, per objective classname: the spread over that team's spawn groups.
    per_team: dict[str, dict[str, Any]] = {}
    for row in distances:
        if not row["reachable"] or row["distance"] is None:
            continue
        bucket = per_team.setdefault(row["team"], {})
        entry = bucket.setdefault(row["objective"], {"distances": []})
        entry["distances"].append(row["distance"])
    for team_entry in per_team.values():
        for objective_name, entry in team_entry.items():
            values = entry.pop("distances")
            team_entry[objective_name] = {
                "groups": len(values),
                "min": round(min(values), 2),
                "mean": round(sum(values) / len(values), 2),
                "max": round(max(values), 2),
            }

    real_teams = sorted(t for t in per_team if t != UNASSIGNED_TEAM)

    # Nearest distance to each objective *entity* per team, rather than per classname: a map
    # with two bombsites has two different answers and averaging them hides both.
    nearest_by_objective: dict[int, dict[str, float]] = {}
    for row in distances:
        if not row["reachable"] or row["distance"] is None:
            continue
        bucket = nearest_by_objective.setdefault(row["objective_entity"], {})
        if row["distance"] < bucket.get(row["team"], math.inf):
            bucket[row["team"]] = row["distance"]

    asymmetry = []
    for objective in objectives:
        reach = {
            team: value
            for team, value in nearest_by_objective.get(objective.entity, {}).items()
            if team in real_teams
        }
        if len(reach) < 2:
            continue
        low, high = min(reach.values()), max(reach.values())
        asymmetry.append(
            {
                "objective_entity": objective.entity,
                "objective": objective.classname,
                "objective_team": objective.team,
                # A per-team objective is *meant* to be nearer its own team; a neutral one is
                # not, which is why only the neutral ones can be a finding.
                "neutral": objective.team == UNASSIGNED_TEAM,
                "nearest_by_team": reach,
                "difference": round(high - low, 2),
                "ratio": round(high / low, 3) if low > 0 else None,
            }
        )

    for row in asymmetry:
        ratio = row["ratio"]
        if not row["neutral"] or ratio is None or ratio < 1.25:
            continue
        findings.append(
            _finding(
                "BALANCE_OBJECTIVE_ASYMMETRIC",
                "warning",
                f"{row['objective']} (entity {row['objective_entity']}) belongs to no team, but "
                f"the nearest spawn group per team differs by {row['difference']:g} units "
                f"(ratio {ratio:g}) — {row['nearest_by_team']}",
                "unverified",
                "nrc_mcp.analysis: derived from voxel path distances, threshold 1.25 is a "
                "heuristic and is not stated anywhere",
                "move a spawn group, or add a route, until the two are comparable",
            )
        )

    # The CTF-style comparison the per-objective table cannot make: each team should be about
    # as far from *its own* objective as the other team is from theirs, and about as far from
    # the enemy's. Comparing raw distances to one flag instead would flag every correct CTF
    # map, because being nearer your own flag is the point.
    owned = [o for o in objectives if o.team != UNASSIGNED_TEAM]
    mirrored: dict[str, Any] = {}
    if owned and len(real_teams) >= 2:
        by_role: dict[str, dict[str, float]] = {"to_own_objective": {}, "to_enemy_objective": {}}
        for team in real_teams:
            for role, wanted in (("to_own_objective", True), ("to_enemy_objective", False)):
                values = [
                    nearest_by_objective.get(o.entity, {})[team]
                    for o in owned
                    if (o.team == team) is wanted and team in nearest_by_objective.get(o.entity, {})
                ]
                if values:
                    by_role[role][team] = round(min(values), 2)
        for role, values in by_role.items():
            if len(values) < 2:
                continue
            low, high = min(values.values()), max(values.values())
            mirrored[role] = {
                "by_team": values,
                "difference": round(high - low, 2),
                "ratio": round(high / low, 3) if low > 0 else None,
            }
        for role, entry in mirrored.items():
            ratio = entry["ratio"]
            if ratio is None or ratio < 1.25:
                continue
            findings.append(
                _finding(
                    "BALANCE_TEAM_ROLE_ASYMMETRIC",
                    "warning",
                    f"the two teams are not comparably placed relative to their objectives: "
                    f"{role.replace('_', ' ')} is {entry['by_team']} (ratio {ratio:g}, "
                    f"{entry['difference']:g} units apart)",
                    "unverified",
                    "nrc_mcp.analysis: derived from voxel path distances, threshold 1.25 is a "
                    "heuristic and is not stated anywhere",
                    "even up the two halves, or state the asymmetry as a design choice",
                )
            )

    unreachable = [row for row in distances if not row["reachable"]]
    if unreachable:
        findings.append(
            _finding(
                "BALANCE_OBJECTIVE_UNREACHABLE",
                "warning",
                f"{len(unreachable)} spawn-group-to-objective pair(s) have no walkable route "
                f"on the grid, e.g. {unreachable[0]['objective']} from group "
                f"{unreachable[0]['group']!r}: {unreachable[0]['reason']}",
                "unverified",
                "nrc_mcp.analysis: absent brushes or a coarse cell size can both cause this",
                "check the pair by eye before believing it; then look for a missing route",
            )
        )

    markers = spawns + objectives
    if markers:
        xs = [m.origin[0] for m in markers]
        ys = [m.origin[1] for m in markers]
        map_centre = ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)
    else:
        map_centre = (0.0, 0.0)
    symmetry = _symmetry(
        markers,
        map_centre,
        navgrid.cell if symmetry_tolerance is None else symmetry_tolerance,
    )
    if symmetry.get("tested") and not symmetry["symmetric"] and len(real_teams) >= 2:
        findings.append(
            _finding(
                "BALANCE_NOT_MIRROR_SYMMETRIC",
                "info",
                f"no mirror or rotation about the marker-set centre maps the spawns and "
                f"objectives of one team onto the other's (best: {symmetry['best']['transform']}, "
                f"{symmetry['best']['matched']}/{symmetry['best']['of']} matched). Plenty of "
                "good maps are deliberately asymmetric; this is only a flag for the ones that "
                "meant to be symmetric.",
                "unverified",
                "nrc_mcp.analysis: symmetry heuristic",
            )
        )

    for row in group_rows:
        row.pop("_centroid", None)

    return {
        "map": str(path),
        "profile": profile,
        "roles": role_map.as_dict(),
        "grid": navgrid.as_dict(),
        "spawn_groups": group_rows,
        "objectives": [o.as_dict() for o in objectives],
        "spawn_count": len(spawns),
        "markers_without_origin": without_origin,
        "distances": distances,
        "per_team": per_team,
        "asymmetry": asymmetry,
        "team_role_balance": mirrored,
        "symmetry": symmetry,
        "findings": findings,
        "summary": _summary(findings),
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# sightline_report
# ---------------------------------------------------------------------------


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return ordered[position]


def sightline_report(
    map_path: str | Path,
    samples: int = 200,
    profile_id: str | None = None,
    *,
    cell: float = DEFAULT_CELL,
    grid: NavGrid | None = None,
    max_rays: int = 20_000,
    bucket_units: float = 512.0,
    seed: int = 0,
    long_lane_units: float | None = None,
    top: int = 10,
) -> dict[str, Any]:
    """Sightlines between sampled walkable positions, at eye height from the profile (§7.3).

    Positions are sampled evenly over the walkable set, then pairs are raycast against the
    exact brush half-spaces. Two things come out of it: the distribution of clear sightline
    lengths, and the points that see the most of the map. §7.3's reason for wanting this is
    specific — Urban Terror is sniper-sensitive, so a long lane that nothing contests decides
    fights before players reach them.

    Complexity is `O(rays * candidate brushes)`; the broad phase keeps the candidate count to
    the brushes whose bounding boxes lie along the ray, and a blocked ray stops at its first
    obstruction. `samples` positions imply `samples * (samples - 1) / 2` pairs, so `max_rays`
    caps the work and switches to a random subset of pairs when it bites.

    Eye height is whatever the profile says, and this profile says nothing: it records that
    the gamepack states no eye height, so the verified standing height is used instead and
    every finding here is reported as inferred.
    """
    try:
        # A supplied grid is the source of truth for which profile this report is about: it was
        # built against one, and resolving again could quietly pick a different one.
        navgrid = grid or cached_navgrid(map_path, cell=cell, profile_id=profile_id)
    except AnalysisError as e:
        return {"error": str(e), "profile": profile_id or ""}
    profile = navgrid.profile

    walkable = navgrid.walkable_cells()
    findings: list[dict] = list(navgrid.findings)
    eye = navgrid.movement.eye_height
    confidence = "unverified" if navgrid.movement.eye_height_is_inferred else eye.confidence

    if len(walkable) < 2:
        findings.append(
            _finding(
                "SIGHT_NO_SAMPLES",
                "info",
                f"the grid has {len(walkable)} walkable position(s), so there is nothing to "
                "sight along",
                "verified",
                "nrc_mcp.analysis",
            )
        )
        return {
            "map": navgrid.map_path,
            "profile": profile,
            "grid": navgrid.as_dict(),
            "eye_height": eye.as_dict(),
            "eye_height_is_inferred": navgrid.movement.eye_height_is_inferred,
            "samples": len(walkable),
            "walkable_cells": len(walkable),
            "rays": 0,
            "findings": findings,
            "summary": _summary(findings),
        }

    rng = random.Random(seed)
    if len(walkable) > samples:
        # Even stride rather than a random draw: an evenly spread sample covers the map, and a
        # random one clumps in whichever room has the most floor.
        stride = len(walkable) / samples
        chosen = [walkable[int(i * stride)] for i in range(samples)]
    else:
        chosen = walkable

    points = []
    for cell_index in chosen:
        x, y, floor_z = navgrid.centre(cell_index)
        points.append((x, y, floor_z + eye.value))

    count = len(points)
    total_pairs = count * (count - 1) // 2
    sampled_pairs = total_pairs > max_rays
    if not sampled_pairs:
        pairs = [(i, j) for i in range(count) for j in range(i + 1, count)]
    else:
        # Drawn rather than enumerated-then-sampled: the pair list of a large sample would be
        # millions of tuples held only to throw most of them away.
        drawn: set[tuple[int, int]] = set()
        while len(drawn) < max_rays:
            i, j = rng.randrange(count), rng.randrange(count)
            if i != j:
                drawn.add((min(i, j), max(i, j)))
        pairs = sorted(drawn)

    solids, index = navgrid.solids, navgrid.index
    assert index is not None
    visible_count = [0] * len(points)
    lengths: list[float] = []
    lanes: list[tuple[float, int, int]] = []
    blocked = 0
    for i, j in pairs:
        if segment_clear(solids, index, points[i], points[j]):
            length = math.dist(points[i], points[j])
            lengths.append(length)
            visible_count[i] += 1
            visible_count[j] += 1
            lanes.append((length, i, j))
        else:
            blocked += 1

    lanes.sort(reverse=True)
    size = [
        max(p[0] for p in points) - min(p[0] for p in points),
        max(p[1] for p in points) - min(p[1] for p in points),
    ]
    threshold = max(size) * 0.5 if long_lane_units is None else long_lane_units
    long_lanes = [entry for entry in lanes if entry[0] >= threshold]

    histogram: dict[str, int] = {}
    for length in lengths:
        low = int(length // bucket_units) * int(bucket_units)
        histogram[f"{low}-{low + int(bucket_units)}"] = (
            histogram.get(f"{low}-{low + int(bucket_units)}", 0) + 1
        )

    tested_per_point = (len(pairs) * 2 / len(points)) if points else 0.0
    ranked = sorted(range(len(points)), key=lambda i: -visible_count[i])
    power = [
        {
            "position": [round(v, 2) for v in points[i]],
            "cell": list(chosen[i]),
            "sees": visible_count[i],
            "fraction_of_sampled": round(visible_count[i] / max(1, len(points) - 1), 3),
        }
        for i in ranked[:top]
    ]

    if long_lanes:
        longest = long_lanes[0]
        findings.append(
            _finding(
                "SIGHT_LONG_LANE",
                "warning",
                f"{len(long_lanes)} sampled sightline(s) run {threshold:.0f} units or more "
                f"with nothing in the way; the longest is {longest[0]:.0f} units, from "
                f"{[round(v) for v in points[longest[1]]]} to "
                f"{[round(v) for v in points[longest[2]]]}. Long uncontested lanes favour the "
                "player who holds one and are hard to approach.",
                # The threshold is a heuristic, so this never warns, whatever the eye height.
                "unverified",
                f"profile {profile}: eye height {eye.key} = {eye.value:g} "
                f"({eye.confidence}); the 50%-of-map-extent lane threshold is a heuristic",
                "break the lane with cover, or give the far end a flanking approach",
            )
        )
    if power and power[0]["fraction_of_sampled"] >= 0.5:
        findings.append(
            _finding(
                "SIGHT_DOMINANT_POSITION",
                "info",
                f"one sampled position sees {power[0]['fraction_of_sampled']:.0%} of the other "
                f"sampled positions ({power[0]['position']}); a point that sees half the map "
                "usually needs either cover on the approaches to it or no easy way in",
                confidence,
                f"profile {profile}: eye height {eye.key}",
            )
        )

    return {
        "map": navgrid.map_path,
        "profile": profile,
        "grid": navgrid.as_dict(),
        "eye_height": eye.as_dict(),
        "eye_height_is_inferred": navgrid.movement.eye_height_is_inferred,
        "samples": len(points),
        "walkable_cells": len(walkable),
        "rays": len(pairs),
        "pairs_sampled": sampled_pairs,
        "rays_per_point": round(tested_per_point, 1),
        "clear": len(lengths),
        "blocked": blocked,
        "clear_fraction": round(len(lengths) / len(pairs), 4) if pairs else None,
        "length_histogram": dict(
            sorted(histogram.items(), key=lambda kv: int(kv[0].split("-")[0]))
        ),
        "length_percentiles": {
            "p50": round(_percentile(lengths, 0.50), 1),
            "p90": round(_percentile(lengths, 0.90), 1),
            "p99": round(_percentile(lengths, 0.99), 1),
            "max": round(max(lengths), 1) if lengths else 0.0,
        },
        "long_lane_threshold": round(threshold, 1),
        "long_lanes": len(long_lanes),
        "longest_lanes": [
            {
                "length": round(length, 1),
                "from": [round(v, 2) for v in points[i]],
                "to": [round(v, 2) for v in points[j]],
            }
            for length, i, j in lanes[:top]
        ],
        "power_positions": power,
        "findings": findings,
        "summary": _summary(findings),
        "notes": [
            "sightlines are cast between sampled floor positions at eye height, so they say "
            "nothing about crouched, prone or airborne lines",
            "a clear sightline's length here is the distance between the two sampled points, "
            "not how far the eye could see past them",
        ],
    }


# ---------------------------------------------------------------------------
# movement_check
# ---------------------------------------------------------------------------


def movement_check(
    map_path: str | Path,
    profile_id: str | None = None,
    *,
    cell: float = DEFAULT_CELL,
    grid: NavGrid | None = None,
    samples: int = 400,
    seed: int = 0,
    top: int = 10,
) -> dict[str, Any]:
    """Clearances against the profile's verified movement constants (§7.3).

    Each check names the constant it used and that constant's confidence, and the severity is
    clamped by it: a finding resting on anything unverified is `info`, whatever it would
    otherwise have been. That clamp is the mechanism that kept three wrong spawn rules out of
    this project, and it is why this file has no numbers of its own. The design document's own
    56-unit standing height is the cautionary example — the gamepack says 69.375, and a check
    built on 56 would pass corridors nobody can stand up in.

    Measurements are exact even though the sampling is not: the grid picks the positions, then
    every clearance is measured against the brush half-spaces at that position. So a reported
    height is a real height, not a multiple of the cell size.

    Passing a `grid` reuses an existing one and describes *that* map, which is how a caller runs
    several reports over one voxelization.
    """
    try:
        # A supplied grid is the source of truth for which profile this report is about: it was
        # built against one, and resolving again could quietly pick a different one.
        navgrid = grid or cached_navgrid(map_path, cell=cell, profile_id=profile_id)
    except AnalysisError as e:
        return {"error": str(e), "profile": profile_id or ""}
    profile = navgrid.profile

    movement = navgrid.movement
    solids, index = navgrid.solids, navgrid.index
    assert index is not None
    rng = random.Random(seed)
    findings: list[dict] = list(navgrid.findings)
    checks: list[dict[str, Any]] = []
    not_checked: list[dict[str, str]] = []

    # --- standing and crouch headroom ---------------------------------------
    #
    # The grid found the candidates — floor cells without room to stand — and the exact column
    # geometry decides which they are. It has to be that way round: the grid requires headroom
    # in whole cells, so at cell=16 it would call a 70-unit corridor crouch-only, and a check
    # that reported that as a defect would be wrong about a real map.
    crouch = movement.headroom_crouch
    stand = movement.headroom_stand
    separation = movement.floor_separation
    extra_height = separation.value if separation else 0.0
    stand_clearance = stand.value + extra_height
    crouch_clearance = crouch.value + extra_height if crouch else None
    candidates = navgrid.restricted + navgrid.clearance_restricted
    if len(candidates) > samples:
        candidates = rng.sample(candidates, samples)

    crouch_only = []
    below_crouch = []
    for cell_index in candidates:
        x, y, floor_z = navgrid.centre(cell_index)
        intervals = column_intervals(solids, index, x, y)
        floor, ceiling = floor_and_ceiling(intervals, floor_z + navgrid.cell * 0.5)
        if floor is None or ceiling is None:
            continue
        clearance = ceiling - floor
        row = {
            "position": [round(x, 2), round(y, 2), round(floor, 2)],
            "clearance": round(clearance, 3),
        }
        if clearance < stand_clearance:
            (
                below_crouch
                if crouch_clearance is not None and clearance < crouch_clearance
                else crouch_only
            ).append(row)

    crouch_only.sort(key=lambda r: r["clearance"])
    below_crouch.sort(key=lambda r: r["clearance"])
    checks.append(
        {
            "check": "standing_headroom",
            "constant": stand.as_dict(),
            "floor_separation": separation.as_dict() if separation else None,
            "required_clearance": stand_clearance,
            "sampled": len(candidates),
            "candidates_total": navgrid.crouch_only_total
            + navgrid.too_low_total
            + navgrid.counts.get("body_blocked_floor_cells", 0),
            "crouch_only_positions": len(crouch_only),
            "narrowest": crouch_only[:top],
        }
    )
    if crouch:
        checks.append(
            {
                "check": "crouch_headroom",
                "constant": crouch.as_dict(),
                "floor_separation": separation.as_dict() if separation else None,
                "required_clearance": crouch_clearance,
                "below_crouch_height": len(below_crouch),
                "lowest": below_crouch[:top],
            }
        )
    else:
        not_checked.append(
            {
                "check": "crouch_headroom",
                "reason": f"profile {profile} states no crouch height under `movement:`",
            }
        )

    if crouch_only:
        findings.append(
            _finding(
                "MOVE_CROUCH_ONLY_SPACE",
                "warning",
                f"{len(crouch_only)} sampled floor position(s) have less than "
                f"{stand_clearance:g} units of headroom (lowest {crouch_only[0]['clearance']:g} at "
                f"{crouch_only[0]['position']}); a player cannot stand there",
                stand.confidence if not separation or separation.verified else "unverified",
                f"profile {profile}: {stand.key} = {stand.value:g} ({stand.confidence}, "
                f"{stand.source})",
                "raise the ceiling, or make the crouch section deliberate",
            )
        )

    # --- step height --------------------------------------------------------
    walkable = navgrid.walkable_cells() + navgrid.clearance_restricted
    step = movement.step_height
    jump = movement.jump_up_max
    if len(walkable) > samples:
        stride = len(walkable) / samples
        step_samples = [walkable[int(i * stride)] for i in range(samples)]
    else:
        step_samples = walkable

    # Each rise is classified against the highest limit the profile states, so the report
    # distinguishes a step from a jump from a ledge grab. Anything above every stated limit is
    # a wall rather than a rise anyone would attempt, and is left out rather than counted.
    grab = movement.ledge_grab_max
    window = (grab or jump or step).value
    steps_ok = needs_jump = needs_grab = 0
    marginal: list[dict[str, Any]] = []
    for cell_index in step_samples:
        x, y, floor_z = navgrid.centre(cell_index)
        here = column_intervals(solids, index, x, y)
        my_floor, _ceiling = floor_and_ceiling(here, floor_z + navgrid.cell * 0.5)
        if my_floor is None:
            continue
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx_ = x + dx * navgrid.cell
            ny_ = y + dy * navgrid.cell
            there = column_intervals(solids, index, nx_, ny_)
            if not there:
                continue
            # The lowest surface within reach is the one we would actually try to get onto.
            tops = [hi for _lo, hi in there if my_floor - navgrid.cell <= hi <= my_floor + window]
            if not tops:
                continue
            rise = min(tops) - my_floor
            if rise <= step.value:
                steps_ok += 1
            elif jump and rise <= jump.value:
                needs_jump += 1
                if rise <= step.value + navgrid.cell:
                    marginal.append(
                        {
                            "position": [round(x, 2), round(y, 2), round(my_floor, 2)],
                            "rise": round(rise, 3),
                        }
                    )
            else:
                needs_grab += 1

    checks.append(
        {
            "check": "step_height",
            "constant": step.as_dict(),
            "jump_constant": jump.as_dict() if jump else None,
            "ledge_grab_constant": grab.as_dict() if grab else None,
            "sampled_positions": len(step_samples),
            "steps_within_step_height": steps_ok,
            "steps_needing_a_jump": needs_jump,
            "steps_needing_a_ledge_grab": needs_grab,
            "marginal_steps": sorted(marginal, key=lambda r: r["rise"])[:top],
        }
    )
    if marginal:
        findings.append(
            _finding(
                "MOVE_STEP_MARGINAL",
                "info",
                f"{len(marginal)} sampled edge(s) rise just over the {step.value:g}-unit step "
                f"limit (smallest {min(r['rise'] for r in marginal):g}); a player has to jump "
                "for what looks like a step",
                "unverified",
                f"profile {profile}: {step.key} = {step.value:g} ({step.confidence}); "
                "'just over' is one cell and is a heuristic of this module",
                f"drop the rise to {step.value:g} units or make it clearly a jump",
            )
        )

    # --- passage widths -----------------------------------------------------
    width = movement.player_width
    if width is None:
        not_checked.append(
            {
                "check": "doorway_width",
                "reason": f"profile {profile} states no player bounding-box width",
            }
        )
        narrow: list[dict[str, Any]] = []
    else:
        probe_height = crouch.value if crouch else stand.value
        max_probe = max(256.0, width.value * 8)
        narrow = []
        for cell_index in step_samples:
            x, y, floor_z = navgrid.centre(cell_index)
            eye = (x, y, floor_z + probe_height)
            span_x = ray_distance(solids, index, eye, (1.0, 0.0, 0.0), max_probe) + ray_distance(
                solids, index, eye, (-1.0, 0.0, 0.0), max_probe
            )
            span_y = ray_distance(solids, index, eye, (0.0, 1.0, 0.0), max_probe) + ray_distance(
                solids, index, eye, (0.0, -1.0, 0.0), max_probe
            )
            tight = min(span_x, span_y)
            if tight < width.value * 2:
                narrow.append(
                    {
                        "position": [round(x, 2), round(y, 2), round(floor_z, 2)],
                        "width": round(tight, 2),
                        "axis": "x" if span_x <= span_y else "y",
                        "measured_at_height": round(probe_height, 3),
                    }
                )
        narrow.sort(key=lambda r: r["width"])
        impassable = [r for r in narrow if r["width"] < width.value]
        checks.append(
            {
                "check": "passage_width",
                "constant": width.as_dict(),
                "measured_at": (crouch or stand).as_dict(),
                "sampled_positions": len(step_samples),
                "narrower_than_player": len(impassable),
                "narrower_than_two_players": len(narrow),
                "narrowest": narrow[:top],
            }
        )
        if impassable:
            findings.append(
                _finding(
                    "MOVE_PASSAGE_NARROWER_THAN_PLAYER",
                    "warning",
                    f"{len(impassable)} sampled position(s) sit in a gap narrower than the "
                    f"{width.value:g}-unit player (narrowest {impassable[0]['width']:g} at "
                    f"{impassable[0]['position']}); the grid calls it walkable because it "
                    "models a point, but nobody fits",
                    width.confidence,
                    f"profile {profile}: {width.key} = {width.value:g} ({width.confidence}, "
                    f"{width.source})",
                    "widen the gap or seal it, so the map does not promise a route it lacks",
                )
            )

    # --- what the profile cannot support ------------------------------------
    for check, reason in (
        (
            "walljump_spacing",
            "no opposing-surface spacing for wall-jumps is stated in the profile; §7.3 asks "
            "for it and the gamepack measurements do not contain it",
        ),
        (
            "slide_runout",
            "no slide deceleration or runout length is stated in the profile",
        ),
        (
            "ladder_placement",
            "the profile records that no ladder entity exists and that ladders appear to be a "
            "shader property, unproven; nothing here can be checked yet",
        ),
    ):
        not_checked.append({"check": check, "reason": reason})

    return {
        "map": navgrid.map_path,
        "profile": profile,
        "constants": movement.as_dict(),
        "grid": navgrid.as_dict(),
        "checks": checks,
        "not_checked": not_checked,
        "findings": findings,
        "summary": _summary(findings),
        "notes": [
            "positions come from the voxel grid, but every clearance is measured against exact "
            "brush geometry at that position, so the numbers are not multiples of the cell size",
            "severity is clamped by the confidence of the constant used: nothing resting on an "
            "unverified figure can be worse than info",
            "passage widths are probed along the x and y axes only, so a diagonal corridor "
            "measures wider than it is; the narrow cases it does report are real",
        ],
    }


# ---------------------------------------------------------------------------
# spawn_safety
# ---------------------------------------------------------------------------


def spawn_safety(
    map_path: str | Path,
    profile_id: str | None = None,
    *,
    cell: float = DEFAULT_CELL,
    grid: NavGrid | None = None,
    exit_radius: float = 256.0,
    max_paths: int = 32,
) -> dict[str, Any]:
    """Exits and enemy proximity per spawn point (§7.3).

    "Number of exits" is measured by walking the grid out to `exit_radius` and counting how
    many *separate* groups of cells the frontier falls into: one exit means a spawn that can be
    held by standing in a doorway. Enemy proximity is straight-line for every spawn — exact and
    free — plus a walked distance per team pair, which is where the A* budget goes.

    §7.3 asks for time-to-first-contact. That needs a movement speed the profile does not
    state, so this reports distance and says which one it is, rather than converting through a
    number nobody has verified.
    """
    try:
        # A supplied grid is the source of truth for which profile this report is about: it was
        # built against one, and resolving again could quietly pick a different one.
        navgrid = grid or cached_navgrid(map_path, cell=cell, profile_id=profile_id)
    except AnalysisError as e:
        return {"error": str(e), "profile": profile_id or ""}
    profile = navgrid.profile

    game_map, path = navgrid.game_map, Path(navgrid.map_path)
    role_map = roles(profile)
    spawns, _objectives, without_origin = _markers(game_map, role_map)
    findings: list[dict] = list(navgrid.findings)

    if not spawns:
        findings.append(
            _finding(
                "SPAWN_NONE_FOUND",
                "info",
                "no entity in the map has a classname the profile categorizes as a spawn",
                "verified",
                f"profile {profile} entity categories",
            )
        )
        return {
            "map": str(path),
            "profile": profile,
            "spawns": [],
            "findings": findings,
            "summary": _summary(findings),
        }

    radius_cells = max(1, math.ceil(exit_radius / navgrid.cell))
    rows: list[dict[str, Any]] = []
    off_grid = 0
    path_budget = max_paths
    walked_between: dict[tuple[str, str], dict[str, Any]] = {}

    for spawn in spawns:
        snapped = navgrid.nearest_walkable(spawn.origin)
        row: dict[str, Any] = spawn.as_dict()
        if snapped is None:
            off_grid += 1
            row.update({"on_walkable_grid": False, "exits": None})
            rows.append(row)
            continue
        start, snap_distance = snapped
        row["on_walkable_grid"] = True
        row["snap_distance"] = round(snap_distance, 2)

        # Breadth-first to the radius, then count the connected groups of frontier cells.
        seen = {start}
        frontier: set[tuple[int, int, int]] = set()
        queue = [start]
        while queue:
            current = queue.pop()
            if max(abs(current[0] - start[0]), abs(current[1] - start[1])) >= radius_cells:
                frontier.add(current)
                continue
            for neighbour, _cost in navgrid.neighbours(current):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append(neighbour)

        exits = 0
        unvisited = set(frontier)
        while unvisited:
            exits += 1
            stack = [unvisited.pop()]
            while stack:
                current = stack.pop()
                for neighbour, _cost in navgrid.neighbours(current):
                    if neighbour in unvisited:
                        unvisited.discard(neighbour)
                        stack.append(neighbour)
        row["exits"] = exits
        row["exit_radius"] = exit_radius
        row["reachable_cells_within_radius"] = len(seen)

        enemies = [s for s in spawns if s.team != spawn.team and s.team != UNASSIGNED_TEAM]
        if spawn.team == UNASSIGNED_TEAM:
            enemies = [s for s in spawns if s.entity != spawn.entity]
        if enemies:
            nearest = min(enemies, key=lambda s: math.dist(s.origin, spawn.origin))
            row["nearest_enemy_spawn"] = {
                "entity": nearest.entity,
                "team": nearest.team,
                "straight_line": round(math.dist(nearest.origin, spawn.origin), 2),
            }
            pair = tuple(sorted((spawn.team, nearest.team)))
            if pair not in walked_between and path_budget > 0:
                path_budget -= 1
                result = path_distance(navgrid, spawn.origin, nearest.origin)
                walked_between[pair] = {
                    "teams": list(pair),
                    "reachable": result["reachable"],
                    "distance": result.get("distance"),
                    "reason": result.get("reason"),
                }
            if pair in walked_between:
                row["nearest_enemy_spawn"]["walked_between_teams"] = walked_between[pair]
        rows.append(row)

    single_exit = [r for r in rows if r.get("exits") == 1]
    dead_end = [r for r in rows if r.get("exits") == 0]
    if single_exit:
        findings.append(
            _finding(
                "SPAWN_SINGLE_EXIT",
                "warning",
                f"{len(single_exit)} spawn(s) have one way out within {exit_radius:g} units "
                f"(e.g. entity {single_exit[0]['entity']} at {single_exit[0]['origin']}); one "
                "exit is one place an enemy has to stand to hold the whole spawn",
                "unverified",
                "nrc_mcp.analysis: exit counting over the voxel grid; no profile rule states a "
                "required number of exits",
                "add a second route out, or move the spawn",
            )
        )
    if dead_end:
        findings.append(
            _finding(
                "SPAWN_NO_EXIT",
                "warning",
                f"{len(dead_end)} spawn(s) have no walkable route beyond {exit_radius:g} units "
                f"(e.g. entity {dead_end[0]['entity']} at {dead_end[0]['origin']})",
                "unverified",
                "nrc_mcp.analysis: exit counting over the voxel grid",
                "check the spawn is not sealed in; the grid may also be missing brushes",
            )
        )
    if off_grid:
        findings.append(
            _finding(
                "SPAWN_OFF_WALKABLE_GRID",
                "warning",
                f"{off_grid} spawn(s) have no walkable cell near their origin, so nothing can "
                "be measured from them. Either they are floating, sealed in, or their floor is "
                "part of geometry this grid could not evaluate.",
                "unverified",
                "nrc_mcp.analysis: navgrid snap failed for these entities",
                "open the map at that position and check what the player would stand on",
            )
        )

    return {
        "map": str(path),
        "profile": profile,
        "grid": navgrid.as_dict(),
        "exit_radius": exit_radius,
        "spawns": rows,
        "spawn_count": len(spawns),
        "spawns_without_origin": without_origin,
        "walked_between_teams": list(walked_between.values()),
        "findings": findings,
        "summary": _summary(findings),
        "notes": [
            "no time-to-first-contact is estimated: the profile states no player movement "
            "speed, so only distances are reported",
            f"exits are counted as separate groups of walkable cells on the {exit_radius:g}-unit "
            "frontier, which is a proxy for routes, not a count of doorways",
        ],
    }
