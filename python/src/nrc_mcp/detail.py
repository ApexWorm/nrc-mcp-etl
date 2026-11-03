"""Compact default responses for the analysis tools, and `detail="full"` for everything.

The gameplay reports were written to be complete, and a complete report is expensive: a
`balance_report` was ~18 KB of JSON per call, and an ETL session calls it a dozen times.
Most of that is the same every call — the profile's movement constants with their
descriptions, the voxel grid's own model notes, the role table — and none of it is what
an agent acts on. The summary keeps what is acted on and says what it left out.

Three rules, which every summarizer here follows and the tests pin:

- **Findings are never lost silently.** Every `error` finding is kept, always, however many
  there are. Other findings are kept up to a limit, warnings before info, and the number
  left out is reported per severity. `summary` (the counts) is never touched.
- **What is dropped is named.** A summary carries `omitted`: each dropped field, and for a
  capped list how many entries were left out. Nothing disappears without a line saying so.
- **Long lists keep the rows that need attention first** — unreachable routes, spawns with
  fewest exits, failed playtest steps — and say how they were ordered.

`detail="full"` returns exactly what the tool returned before summaries existed.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

Detail = Literal["summary", "full"]

#: Findings other than errors kept in a summary. Errors are never capped.
FINDINGS_LIMIT = 10
#: Rows kept from a capped list.
ROWS_LIMIT = 12

FULL_HINT = 'call again with detail="full" for the omitted fields'
_SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def wants_full(detail: str) -> bool:
    if detail not in ("summary", "full"):
        raise ToolError(f'detail must be "summary" or "full", not {detail!r}')
    return detail == "full"


def constant(value: Any) -> Any:
    """A profile constant as key/value/confidence: the description and source live in the profile.

    Applied recursively, so it reaches constants nested anywhere in a report. A constant
    is recognised by shape, which is exactly what `analysis.Constant.as_dict()` produces.
    """
    if isinstance(value, dict):
        if {"key", "value", "confidence", "source", "description"} <= value.keys():
            out = {"key": value["key"], "value": value["value"], "confidence": value["confidence"]}
            if value.get("approximate"):
                out["approximate"] = True
            return out
        return {k: constant(v) for k, v in value.items()}
    if isinstance(value, list):
        return [constant(v) for v in value]
    return value


def _key(finding: dict) -> tuple:
    return (finding.get("code"), finding.get("message"))


def findings(items: list[dict], omitted: dict, field: str = "findings") -> list[dict]:
    """Every error, then warnings and info up to `FINDINGS_LIMIT`; the rest counted by severity."""
    errors = [f for f in items if f.get("severity") == "error"]
    rest = sorted(
        (f for f in items if f.get("severity") != "error"),
        key=lambda f: _SEVERITY_ORDER.get(f.get("severity"), 3),
    )
    kept_rest = rest[:FINDINGS_LIMIT]
    left = rest[FINDINGS_LIMIT:]
    if left:
        counts: dict[str, int] = {}
        for f in left:
            counts[f.get("severity", "?")] = counts.get(f.get("severity", "?"), 0) + 1
        omitted[field] = {"omitted_by_severity": counts, "errors_omitted": 0}
    kept = set(map(id, errors + kept_rest))
    # Original order among what is kept: the producing analysis orders by relevance.
    return [f for f in items if id(f) in kept]


def grid(g: dict, reported: set[tuple], omitted: dict, field: str = "grid") -> dict:
    """The voxel grid's size and coverage, without the constants and model notes.

    Grid findings already present in the report's own `findings` are not repeated; any
    that are not stay here, so a grid-level problem is never dropped.
    """
    out = {
        k: g[k]
        for k in (
            "stance",
            "cell",
            "dims",
            "counts",
            "effective_headroom_units",
        )
        if k in g
    }
    geometry = g.get("geometry")
    if isinstance(geometry, dict):
        out["geometry"] = {k: v for k, v in geometry.items() if v not in (0, [], None)}
    left = [f for f in g.get("findings", []) if _key(f) not in reported]
    if left:
        out["findings"] = left
    omitted.setdefault(field, "movement constants, origin, cell budgets and model notes")
    return out


def rows(
    items: list, omitted: dict, field: str, *, order: str, key=None, limit: int = ROWS_LIMIT
) -> list:
    """The first `limit` rows by `key` (stable), with the count left out and how they were ordered."""
    ranked = sorted(items, key=key) if key is not None else list(items)
    if len(ranked) <= limit:
        return ranked
    omitted[field] = {"shown": limit, "total": len(ranked), "ordered_by": order}
    return ranked[:limit]


def _finish(out: dict, omitted: dict) -> dict:
    out["detail"] = "summary"
    if omitted:
        out["omitted"] = {**omitted, "how": FULL_HINT}
    return out


def _drop(report: dict, out: dict, omitted: dict, field: str, why: str) -> None:
    out.pop(field, None)
    if field in report:
        omitted[field] = why


def _reported(report: dict) -> set[tuple]:
    return {_key(f) for f in report.get("findings", [])}


# ---------------------------------------------------------------------------
# Per-tool summaries
# ---------------------------------------------------------------------------


def balance_report(report: dict, model_notes: frozenset[str] = frozenset()) -> dict:
    omitted: dict = {}
    out = constant(dict(report))
    out["grid"] = grid(report["grid"], _reported(report), omitted)
    out["findings"] = findings(report.get("findings", []), omitted)
    _drop(report, out, omitted, "roles", "the profile's role table (profile_summary has it)")
    if isinstance(report.get("symmetry"), dict):
        sym = report["symmetry"]
        out["symmetry"] = {k: sym[k] for k in ("tested", "symmetric", "best") if k in sym}
        if "candidates" in sym:
            omitted["symmetry.candidates"] = "every transform tried; `best` is kept"
    out["distances"] = rows(
        report.get("distances", []),
        omitted,
        "distances",
        order="unreachable first, then longest walk",
        key=lambda d: (bool(d.get("reachable")), -(d.get("distance") or 0)),
    )
    out["asymmetry"] = rows(
        report.get("asymmetry", []),
        omitted,
        "asymmetry",
        order="largest difference between teams first",
        key=lambda a: -(a.get("difference") or 0),
    )
    rb = report.get("reinforcement_balance")
    if isinstance(rb, dict):
        brief = constant({k: v for k, v in rb.items() if k not in ("stages", "notes")})
        stages = []
        for i, stage in enumerate(rb.get("stages", [])):
            s = {
                k: stage[k]
                for k in (
                    "name",
                    "paths_requested",
                    "paths_measured",
                    "truncated",
                    "teams_without_selected_spawns",
                )
                if k in stage
            }
            s["spawn_groups"] = [
                {
                    k: g[k]
                    for k in (
                        "team",
                        "group",
                        "spawn_entities",
                        "available_slots",
                        "requested_players",
                        "slot_shortfall",
                        "overlapping_pairs",
                    )
                    if k in g
                }
                for g in stage.get("spawn_groups", [])
            ]
            s["respawn"] = {
                team: {k: r[k] for k in ("seconds", "source", "confidence") if k in r}
                for team, r in (stage.get("respawn") or {}).items()
            }
            s["routes"] = rows(
                [
                    {
                        k: r[k]
                        for k in (
                            "team",
                            "group",
                            "representative_spawn_entity",
                            "objective_entity",
                            "objective_entities",
                            "reachable",
                            "distance",
                            "reason",
                            "arrival_seconds_estimate",
                        )
                        if k in r
                    }
                    for r in stage.get("routes", [])
                ],
                omitted,
                f"reinforcement_balance.stages[{i}].routes",
                order="unreachable first, then original order",
                key=lambda r: bool(r.get("reachable")),
                limit=ROWS_LIMIT * 2,
            )
            stages.append(s)
        brief["stages"] = stages
        notes = rb.get("notes", [])
        specific = [n for n in notes if n and n not in model_notes]
        if specific:
            brief["notes"] = specific
        if len(specific) < len([n for n in notes if n]):
            omitted["reinforcement_balance.notes"] = (
                f"{len(notes) - len(specific)} fixed model caveats (estimates, unverified)"
            )
        omitted["reinforcement_balance.stages[].selection_source/spacing_source"] = (
            "provenance strings"
        )
        out["reinforcement_balance"] = brief
    return _finish(out, omitted)


def spawn_safety(report: dict) -> dict:
    omitted: dict = {}
    out = constant(dict(report))
    out["grid"] = grid(report["grid"], _reported(report), omitted)
    out["findings"] = findings(report.get("findings", []), omitted)
    spawns = []
    for s in report.get("spawns", []):
        row = {k: v for k, v in s.items() if k not in ("classname", "exit_radius")}
        enemy = row.get("nearest_enemy_spawn")
        if isinstance(enemy, dict) and isinstance(enemy.get("walked_between_teams"), dict):
            walked = enemy["walked_between_teams"]
            enemy = {k: v for k, v in enemy.items() if k != "walked_between_teams"}
            enemy["walked_between_teams"] = (
                walked.get("distance") if walked.get("reachable") else None
            )
            row["nearest_enemy_spawn"] = enemy
        spawns.append(row)
    if spawns:
        omitted["spawns[]"] = (
            "classname and exit_radius (top-level); nearest_enemy_spawn.walked_between_teams is "
            "the walked distance, or null when unreachable (see walked_between_teams)"
        )
    out["spawns"] = rows(
        spawns,
        omitted,
        "spawns",
        order="off the walkable grid, then fewest exits, then nearest enemy spawn",
        key=lambda r: (
            bool(r.get("on_walkable_grid", True)),
            r.get("exits") if r.get("exits") is not None else -1,
            (r.get("nearest_enemy_spawn") or {}).get("straight_line") or float("inf"),
        ),
    )
    return _finish(out, omitted)


def movement_check(report: dict) -> dict:
    omitted: dict = {}
    out = constant(dict(report))
    out["grid"] = grid(report["grid"], _reported(report), omitted)
    out["findings"] = findings(report.get("findings", []), omitted)
    _drop(
        report,
        out,
        omitted,
        "constants",
        "the profile's movement constants; each check names the one it used",
    )
    _drop(report, out, omitted, "notes", "fixed method caveats (see the tool description)")
    return _finish(out, omitted)


def sightline_report(report: dict) -> dict:
    omitted: dict = {}
    out = constant(dict(report))
    out["grid"] = grid(report["grid"], _reported(report), omitted)
    out["findings"] = findings(report.get("findings", []), omitted)
    _drop(report, out, omitted, "notes", "fixed method caveats (see the tool description)")
    return _finish(out, omitted)


def route_report(report: dict) -> dict:
    omitted: dict = {}
    out = constant(dict(report))
    grids = report.get("grids") or {}
    out["grids"] = {s: grid(g, set(), omitted, "grids") for s, g in grids.items()}
    _drop(
        report,
        out,
        omitted,
        "notes",
        f"{len(report.get('notes', []))} fixed model caveats (see the tool description)",
    )
    if report.get("segments"):
        # `segments` carries the same vertices, grouped by stance, plus each leg's distance.
        _drop(report, out, omitted, "path", "the same vertices as `segments`, one row per vertex")
    return _finish(out, omitted)


def engine_playtest(report: dict) -> dict:
    """Every step's verdict; full state only where a step failed, and for the final state."""
    omitted: dict = {}
    out = dict(report)
    _drop(report, out, omitted, "scenario", "the scenario you sent")
    _drop(report, out, omitted, "command", "the engine command line")
    steps = []
    for s in report.get("steps", []):
        action = s.get("action")
        name = action.get("action") if isinstance(action, dict) else action
        if not s.get("ok", True):
            steps.append(s)
            continue
        row: dict = {"index": s.get("index"), "action": name, "ok": s.get("ok")}
        for k in ("screenshot", "seconds", "error"):
            if k in s:
                row[k] = s[k]
        steps.append(row)
    if steps:
        out["steps"] = steps
        omitted["steps[]"] = (
            "the action echo and observed state of passing steps; failed steps are complete. "
            "result_path holds the full receipt"
        )
    return _finish(out, omitted)
