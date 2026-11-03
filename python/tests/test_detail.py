"""Summary-by-default responses: smaller, and never at the cost of something to act on."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import detail
from nrc_mcp import server as srv
from nrc_mcp.runtime import compact_json, schema_without_titles


def _finding(severity: str, n: int) -> dict:
    return {"code": f"X_{severity.upper()}_{n}", "severity": severity, "message": f"{severity} {n}"}


def test_errors_are_never_capped_and_the_rest_are_counted():
    items = (
        [_finding("info", i) for i in range(15)]
        + [_finding("error", i) for i in range(30)]
        + [_finding("warning", i) for i in range(8)]
    )
    omitted: dict = {}
    kept = detail.findings(items, omitted)
    assert sum(f["severity"] == "error" for f in kept) == 30, "an error was dropped"
    # Warnings outrank info for the limited slots.
    assert sum(f["severity"] == "warning" for f in kept) == 8
    assert sum(f["severity"] == "info" for f in kept) == detail.FINDINGS_LIMIT - 8
    assert omitted["findings"]["omitted_by_severity"] == {"info": 15 - (detail.FINDINGS_LIMIT - 8)}
    assert omitted["findings"]["errors_omitted"] == 0
    # The producer's order survives among what is kept.
    assert kept == [f for f in items if f in kept]


def test_a_short_findings_list_is_untouched():
    items = [_finding("warning", 0), _finding("info", 1)]
    omitted: dict = {}
    assert detail.findings(items, omitted) == items
    assert omitted == {}


def test_a_capped_list_says_how_many_rows_it_left_out_and_in_what_order():
    omitted: dict = {}
    rows = [{"d": i} for i in range(30)]
    kept = detail.rows(rows, omitted, "things", order="largest d first", key=lambda r: -r["d"])
    assert [r["d"] for r in kept] == list(range(29, 29 - detail.ROWS_LIMIT, -1))
    assert omitted["things"] == {
        "shown": detail.ROWS_LIMIT,
        "total": 30,
        "ordered_by": "largest d first",
    }


def test_constants_shrink_to_key_value_and_confidence_wherever_they_are():
    full = {
        "key": "movement.step_height",
        "value": 18.0,
        "confidence": "verified",
        "source": "engine",
        "description": "long text",
        "approximate": False,
    }
    shrunk = detail.constant({"a": [{"b": full}], "c": dict(full, approximate=True)})
    assert shrunk["a"][0]["b"] == {
        "key": "movement.step_height",
        "value": 18.0,
        "confidence": "verified",
    }
    assert shrunk["c"]["approximate"] is True, "an approximate figure must still say so"


def test_a_grid_finding_missing_from_the_report_stays_in_the_grid():
    seen = _finding("warning", 1)
    unseen = _finding("error", 2)
    out = detail.grid(
        {"cell": 16, "findings": [seen, unseen], "notes": ["x"]},
        {(seen["code"], seen["message"])},
        {},
    )
    assert out["findings"] == [unseen]
    assert "notes" not in out


def test_failed_playtest_steps_are_kept_whole():
    passing = {
        "index": 0,
        "action": {"action": "join", "team": "red"},
        "ok": True,
        "state": {"big": 1},
    }
    failing = {
        "index": 1,
        "action": {"action": "assert"},
        "ok": False,
        "error": "timeout",
        "state": {"x": 1},
    }
    report = {
        "ok": False,
        "scenario": {"steps": []},
        "command": ["etl"],
        "steps": [passing, failing],
    }
    out = detail.engine_playtest(report)
    assert out["steps"][0] == {"index": 0, "action": "join", "ok": True}
    assert out["steps"][1] == failing
    assert "scenario" not in out and "command" not in out
    assert {"scenario", "command", "steps[]"} <= set(out["omitted"])


def test_spawns_needing_attention_come_first():
    def spawn(entity, exits, grid=True, enemy=500.0):
        return {
            "entity": entity,
            "classname": "spawn",
            "exits": exits,
            "exit_radius": 256,
            "on_walkable_grid": grid,
            "nearest_enemy_spawn": {"entity": 99, "straight_line": enemy},
        }

    report = {
        "grid": {"cell": 16},
        "findings": [],
        "spawns": [spawn(i, 3) for i in range(20)] + [spawn(50, 1), spawn(51, 0, grid=False)],
    }
    out = detail.spawn_safety(report)
    assert [s["entity"] for s in out["spawns"][:2]] == [51, 50]
    assert out["omitted"]["spawns"]["total"] == 22


def test_detail_must_be_summary_or_full():
    with pytest.raises(ToolError, match="summary"):
        detail.wants_full("everything")


def test_compact_json_is_lossless_and_leaves_prose_alone():
    pretty = json.dumps({"a": [1, 2.5, "é"], "b": None}, indent=2)
    assert json.loads(compact_json(pretty)) == json.loads(pretty)
    assert len(compact_json(pretty)) < len(pretty)
    assert compact_json("Solid IR reference\n  - box") == "Solid IR reference\n  - box"
    assert compact_json("{not json") == "{not json"


def test_listed_schemas_carry_no_generated_titles_but_keep_a_parameter_named_title():
    schema = {
        "title": "fArguments",
        "type": "object",
        "properties": {"title": {"title": "Title", "type": "string"}},
    }
    assert schema_without_titles(schema) == {
        "type": "object",
        "properties": {"title": {"type": "string"}},
    }
    for tool in asyncio.run(srv.mcp.list_tools()):
        assert "title" not in tool.input_schema, tool.name
        for name, prop in tool.input_schema.get("properties", {}).items():
            assert "title" not in prop, f"{tool.name}.{name}"


def test_descriptions_are_sent_without_source_indentation():
    for tool in asyncio.run(srv.mcp.list_tools()):
        lines = tool.description.splitlines()
        assert not lines[0].startswith(" "), tool.name
        # A docstring's body lines all shared four spaces of indentation; none should now.
        body = [ln for ln in lines[1:] if ln.strip()]
        assert not body or min(len(ln) - len(ln.lstrip()) for ln in body) == 0, tool.name


@pytest.fixture
def opened(tmp_path, monkeypatch):
    monkeypatch.setenv("NRC_PROFILE", "etl")
    fixture = Path(__file__).resolve().parents[2] / "bench/tapes/fixtures/nrcroom"
    if not fixture.is_dir():
        pytest.skip("tape fixture missing")
    shutil.copytree(fixture, tmp_path / "project")
    path = tmp_path / "project/maps/nrcroom.map"
    srv.map_open(str(path))
    yield path
    srv.SESSION.map = None
    srv.SESSION.path = None


@pytest.mark.parametrize("tool", ["balance_report", "spawn_safety", "movement_check"])
def test_a_summary_is_smaller_and_keeps_every_verdict(opened, tool):
    fn = getattr(srv, tool)
    full = fn(cell=32, detail="full")
    brief = fn(cell=32)
    assert brief["detail"] == "summary" and "detail" not in full
    assert len(json.dumps(brief)) < len(json.dumps(full))
    assert brief["summary"] == full["summary"], "the finding counts must never change"
    errors = [f for f in full["findings"] if f["severity"] == "error"]
    assert all(f in brief["findings"] for f in errors)
    assert brief["revision"] == full["revision"]
    # Every top-level field is either still there or named as omitted.
    for key in full:
        assert key in brief or key in brief["omitted"], f"{tool}: {key} vanished silently"


def test_the_wire_carries_compact_json(opened):
    result = asyncio.run(srv.mcp.call_tool("spawn_safety", {"cell": 32}))
    text = result.content[0].text
    assert "\n" not in text and json.loads(text)["detail"] == "summary"


def test_a_pack_summary_keeps_what_decides_the_pack():
    names = [f"textures/x/{i}" for i in range(30)]
    report = {
        "ok": False,
        "not_found": names,
        "allowed_missing": [{"name": n, "pattern": None, "reason": "r"} for n in names],
        "packer_not_found": names,
        "accounted_for": [{"name": n, "by": "the base game"} for n in names],
        "dropped_basegame": names,
        "output_tail": "x" * 2000,
    }
    brief = detail.pack_pk3(report)
    assert brief["not_found"] == names and len(brief["allowed_missing"]) == 30
    assert len(brief["accounted_for"]) == detail.ROWS_LIMIT
    assert brief["omitted"]["accounted_for"]["total"] == 30
    assert "output_tail" not in brief and "output_tail" in brief["omitted"]
    for key in report:
        assert key in brief or key in brief["omitted"]
