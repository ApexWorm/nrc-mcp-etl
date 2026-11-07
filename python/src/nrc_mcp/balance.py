"""Reinforcement and objective analysis, with all game conventions in profile data.

These are planning estimates over a supplied geometry snapshot. Script assignments provide
evidence for inputs, not proof that an event executes; explicit stages never simulate scripts.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from . import analysis, mapscript, profiles
from .rules import parse_spawnflags


def _positive(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite positive number")
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{name} must be a finite positive number") from e
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _team_values(values: dict | None, teams: set[str], name: str, *, integer=False) -> dict:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ValueError(f"{name} must map profile team labels to numbers")
    out = {}
    for team, value in values.items():
        if team not in teams:
            raise ValueError(f"unknown team {team!r} in {name}; use {', '.join(sorted(teams))}")
        number = _positive(value, f"{name}.{team}")
        if integer and number != int(number):
            raise ValueError(f"{name}.{team} must be a whole number")
        out[team] = int(number) if integer else number
    return out


def _waves(data: dict, game_map, text: str | None) -> tuple[dict, list[str]]:
    spec = data["balance"]
    script_spec = data.get("mapscript") or {}
    actions = {str(action).lower(): team for team, action in spec["respawn_actions"].items()}
    candidates: dict[str, list[dict]] = defaultdict(list)
    notes = []
    parse_error = False
    if text is not None:
        live = mapscript.names(script_spec, game_map.entities())
        try:
            blocks = mapscript.parse_spec(text, script_spec)
        except mapscript.ScriptSyntaxError as e:
            blocks = []
            parse_error = True
            notes.append(f"script could not be parsed; wave times are unknown: {e}")
        for block in blocks:
            if block.name.lower() not in live:
                continue
            for event in block.events:
                for action in event.actions:
                    team = actions.get(action.name)
                    if team is None:
                        continue
                    try:
                        # The engine expects an integer token, not a floating-point duration.
                        value = int(action.args[0]) if len(action.args) == 1 else 0
                        value = _positive(value, "script wave interval")
                    except (ValueError, IndexError):
                        value = None
                    candidates[team].append(
                        {
                            "seconds": value,
                            "block": block.name,
                            "event": event.name,
                            "params": event.params,
                            "line": action.line,
                        }
                    )
    waves = {}
    for team in spec["respawn_actions"]:
        found = candidates[team]
        values = {item["seconds"] for item in found}
        startup = any(item["event"] == spec["startup_event"] for item in found)
        if parse_error:
            seconds, source = None, "unparseable script"
        elif found and (len(values) != 1 or None in values or not startup):
            seconds, source = None, "ambiguous or event-dependent script assignments"
            notes.append(f"{team}: specify respawn_seconds for the stage being measured")
        elif found:
            seconds, source = next(iter(values)), "script startup assignment (execution assumed)"
        else:
            seconds = spec.get("default_respawn_seconds", {}).get(team)
            source = "profile default (no script assignment found)"
        waves[team] = {
            "seconds": seconds,
            "source": source,
            "confidence": "unverified",
            "candidates": found,
        }
    return waves, notes


#: The reinforcement model's fixed caveats, the same on every call. A summary leaves these
#: out (`nrc_mcp.detail`) and keeps the notes that are specific to the map and script.
MODEL_NOTES = (
    "Travel is distance / constant speed over approximate voxel routes, not an engine measurement. Acceleration, sprint stamina, combat and pauses are not modelled.",
    "Arrival ranges add zero to one reinforcement interval; death phase and wave offset are unknown. They do not predict which team wins.",
    "All stages use the same geometry snapshot; selecting entities does not open doors, build bridges or execute scripts. Measure changed geometry separately.",
    "Spawn capacity counts static slots and overlapping player boxes, not engine spawn selection or live player occupancy. A group's team-size shortfall assumes the entire team chooses that group.",
    "Routes start at the selected spawn nearest its group's centroid, not an average over every slot. The representative entity is included with each route.",
    "Uniquely linked objective entities declared by the profile share the describing marker's approach area and team. Routes list every represented entity; stages may select either member. Ambiguous links remain separate.",
)


def _indices(value: Any, known: set[int], name: str) -> set[int]:
    if not isinstance(value, list) or any(
        isinstance(i, bool) or not isinstance(i, int) for i in value
    ):
        raise ValueError(f"{name} must be a list of entity indices")
    chosen = set(value)
    unknown = chosen - known
    if unknown:
        raise ValueError(f"{name} contains unknown entity indices: {sorted(unknown)}")
    return chosen


def _capacity(members, box: dict, team_size: int | None) -> dict:
    dimensions = [box.get(k) for k in ("width_x", "width_y", "height_stand")]
    usable = box.get("confidence") == "verified" and all(
        isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in dimensions
    )
    overlap = []
    neighbours: dict[int, set[int]] = defaultdict(set)
    if usable:
        for index, a in enumerate(members):
            for b in members[index + 1 :]:
                if all(abs(a.origin[i] - b.origin[i]) < dimensions[i] for i in range(3)):
                    overlap.append([a.entity, b.entity])
                    neighbours[a.entity].add(b.entity)
                    neighbours[b.entity].add(a.entity)
    independent: set[int] = set()
    for member in sorted(members, key=lambda m: (len(neighbours[m.entity]), m.entity)):
        if not neighbours[member.entity] & independent:
            independent.add(member.entity)
    return {
        "spawn_entities": [m.entity for m in members],
        "available_slots": len(members),
        "requested_players": team_size,
        "slot_shortfall": max(0, team_size - len(members)) if team_size is not None else None,
        "overlapping_pairs": overlap if usable else None,
        "non_overlapping_slots_lower_bound": len(independent) if usable else None,
        "spacing_source": box.get("locus") if usable else "no verified player bounding box",
    }


def enrich(
    report: dict,
    grid: analysis.NavGrid,
    *,
    script_text: str | None = None,
    stages: list[dict] | None = None,
    team_sizes: dict | None = None,
    run_speed: float | None = None,
    respawn_seconds: dict | None = None,
    max_paths: int = 64,
) -> dict:
    """Add reinforcement planning to a distance report for profiles that describe it.

    A stage has ``name``, explicit ``spawn_entities`` and optional ``objective_entities``
    and ``respawn_seconds``. It selects available markers on the SAME supplied collision
    snapshot. Omit stages for the static initial-spawn flags. User supplied wave times are
    assumed effective after server overrides. All route times remain estimates.
    """
    data = profiles.load(grid.profile)
    spec = data.get("balance") or {}
    if spec.get("model") != "reinforcement_objectives":
        if any(value is not None for value in (stages, team_sizes, run_speed, respawn_seconds)):
            raise ValueError(f"profile {grid.profile} does not describe reinforcement balance")
        return report
    if isinstance(max_paths, bool) or not isinstance(max_paths, int) or max_paths < 1:
        raise ValueError("max_paths must be a positive whole number")
    if stages is not None and (not isinstance(stages, list) or not stages):
        raise ValueError("stages must be a non-empty list of stage descriptions")
    role_map = analysis.roles(grid.profile)
    spawns, objectives, _ = analysis._markers(grid.game_map, role_map)
    objective_aliases = {index: m.entity for m in objectives for index in m.entity_indices}
    teams = set(spec["respawn_actions"])
    sizes = _team_values(team_sizes, teams, "team_sizes", integer=True)
    overrides = _team_values(respawn_seconds, teams, "respawn_seconds")
    waves, notes = _waves(data, grid.game_map, script_text)
    speed_spec = (data.get("movement") or {}).get("run_speed") or {}
    stance = getattr(grid, "stance", "stand")
    if run_speed is not None:
        speed = _positive(run_speed, "run_speed")
        speed_source = "caller supplied effective speed"
    elif stance != "stand":
        speed, speed_source = None, "stance has no verified travel speed; supply run_speed"
    elif speed_spec.get("confidence") == "verified":
        speed = _positive(speed_spec.get("value"), "profile run_speed")
        speed_source = f"profile default: {speed_spec.get('locus', '')}"
    else:
        speed, speed_source = None, "profile has no verified run speed"
    entities = {int(e["index"]): e for e in grid.game_map.entities()}
    initial = [
        m.entity
        for m in spawns
        if parse_spawnflags(analysis._entity_keys(entities[m.entity]).get("spawnflags"))
        & int(spec["initial_spawn_flag"])
    ]
    requested = stages or [{"name": "initial", "spawn_entities": initial}]
    names = set()
    stage_rows = []
    budget = max_paths
    box = ((data.get("units") or {}).get("player") or {}).get("bounding_box") or {}
    for stage in requested:
        if not isinstance(stage, dict):
            raise ValueError("each stage must be an object")
        unknown_keys = set(stage) - {
            "name",
            "spawn_entities",
            "objective_entities",
            "respawn_seconds",
        }
        if unknown_keys:
            raise ValueError(f"unknown stage fields: {sorted(unknown_keys)}")
        name = stage.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("each stage needs a non-empty unique name")
        names.add(name)
        active = _indices(stage.get("spawn_entities"), {m.entity for m in spawns}, "spawn_entities")
        selected_objectives = _indices(
            stage.get("objective_entities", [m.entity for m in objectives]),
            set(objective_aliases),
            "objective_entities",
        )
        wanted = {objective_aliases[index] for index in selected_objectives}
        intervals = {team: dict(value) for team, value in waves.items()}
        stage_overrides = {
            **overrides,
            **_team_values(stage.get("respawn_seconds"), teams, "respawn_seconds"),
        }
        for team, seconds in stage_overrides.items():
            intervals[team] = {
                "seconds": seconds,
                "source": "caller supplied effective interval",
                "confidence": "unverified",
                "candidates": [],
            }
        grouped = defaultdict(list)
        for marker in spawns:
            if marker.entity in active:
                grouped[(marker.team, marker.group)].append(marker)
        capacities = []
        routes = []
        for (team, group), members in sorted(grouped.items()):
            capacities.append(
                {"team": team, "group": group, **_capacity(members, box, sizes.get(team))}
            )
            centroid = tuple(sum(m.origin[i] for m in members) / len(members) for i in range(3))
            # A centroid can lie in a wall between separate spawn rooms. Measure from an
            # actual selected slot, exposing which one so the approximation is reviewable.
            representative = min(members, key=lambda m: (math.dist(m.origin, centroid), m.entity))
            for objective in objectives:
                if objective.entity not in wanted or budget <= 0:
                    continue
                budget -= 1
                path = analysis._path_to_marker(grid, representative.origin, objective)
                distance = path.get("distance") if path["reachable"] else None
                travel = distance / speed if distance is not None and speed is not None else None
                wave = intervals.get(team, {}).get("seconds")
                routes.append(
                    {
                        "team": team,
                        "group": group,
                        "representative_spawn_entity": representative.entity,
                        "objective_entity": objective.entity,
                        "objective_entities": list(objective.entity_indices),
                        "objective_team": objective.team,
                        "reachable": path["reachable"],
                        "distance": distance,
                        "reason": path.get("reason"),
                        "travel_seconds_estimate": round(travel, 3) if travel is not None else None,
                        "reinforcement_wait_seconds": [0, wave] if wave is not None else None,
                        "arrival_seconds_estimate": [round(travel, 3), round(travel + wave, 3)]
                        if travel is not None and wave is not None
                        else None,
                        "confidence": "unverified",
                    }
                )
        needed = len(grouped) * len(wanted)
        stage_rows.append(
            {
                "name": name,
                "selection_source": "caller supplied available entities"
                if stages
                else "profile initial spawn flag (before script effects)",
                "spawn_groups": capacities,
                "respawn": intervals,
                "routes": routes,
                "paths_requested": needed,
                "paths_measured": len(routes),
                "truncated": needed > len(routes),
                "teams_without_selected_spawns": sorted(teams - {team for team, _ in grouped}),
            }
        )
    result = dict(report)
    # Keep geometric comparisons available, but do not present mirror symmetry or equal
    # distance as fairness criteria for a progression-based attack/defend mission.
    result["findings"] = [
        f
        for f in report.get("findings", [])
        if f.get("code")
        not in {
            "BALANCE_NOT_MIRROR_SYMMETRIC",
            "BALANCE_TEAM_ROLE_ASYMMETRIC",
            "BALANCE_OBJECTIVE_ASYMMETRIC",
        }
    ]
    for stage in stage_rows:
        for group in stage["spawn_groups"]:
            if group["slot_shortfall"]:
                result["findings"].append(
                    analysis._finding(
                        "BALANCE_SPAWN_SLOT_SHORTFALL",
                        "info",
                        f"stage {stage['name']!r}, {group['team']} group {group['group']!r}: "
                        f"{group['available_slots']} spawn slots for {group['requested_players']} "
                        "requested players; engine fallback and occupancy are not modelled",
                        "unverified",
                        "selected spawn entities and caller team size",
                    )
                )
            if group["overlapping_pairs"]:
                result["findings"].append(
                    analysis._finding(
                        "BALANCE_SPAWN_BOX_OVERLAP",
                        "info",
                        f"stage {stage['name']!r}, {group['team']} group {group['group']!r}: "
                        f"{len(group['overlapping_pairs'])} spawn pairs overlap the profile's "
                        "standing player boxes",
                        "unverified",
                        str(group["spacing_source"]),
                    )
                )
    result["summary"] = analysis._summary(result["findings"])
    result["notes"] = [n for n in report.get("notes", []) if not n.startswith("no traversal time")]
    result["notes"].append(
        "Mirror symmetry and equal team distances are descriptive geometry, not fairness "
        "criteria for this profile. Reinforcement timing and objective progression need "
        "playtesting; see reinforcement_balance for explicit assumptions."
    )
    result["reinforcement_balance"] = {
        "model": spec["model"],
        "confidence": "unverified",
        "run_speed": {"units_per_second": speed, "source": speed_source},
        "team_labels": {
            s["label"]: s.get("name", s["label"])
            for s in (data.get("teams") or {}).get("sides", [])
        },
        "stages": stage_rows,
        "notes": [
            *MODEL_NOTES,
            str(spec.get("note", "")),
            *notes,
        ],
    }
    return result
