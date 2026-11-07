"""Bot map goals and map scripts: the profile's rules, the two writers against their readers
and the add-on's shipped files, spot choice on small maps, and the tool's contract.

The engine side (bench/dogfood/README.md): in ET: Legacy 2.86.2 with Omni-bot 0.93 the add-on
loads the generated goal files ("N Goals Loaded") and runs the generated map scripts with no
error logged; their triggers fire on the game's own objective events.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import botgoals, botnav, botplay, kernel, profiles
from nrc_mcp import objective_tests as objtests
from nrc_mcp import server as srv
from test_analysis import box, point_entity, room, write_map
from test_bot_waypoints import _brush_entity


@pytest.fixture(autouse=True)
def _etl(monkeypatch):
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    monkeypatch.setenv("NRC_PROFILE", "etl")
    yield
    srv.SESSION.map = None
    srv.SESSION.path = None


def bots():
    return botnav.spec("etl")


def sections():
    return botgoals.spec(bots())


def stock_nav() -> Path | None:
    base = os.environ.get("NRC_FS_BASEPATH")
    nav = Path(base) / "legacy" / "omni-bot" / "et" / "nav" if base else None
    return nav if nav and nav.is_dir() else None


# --------------------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------------------


def test_every_objective_kind_has_script_rules_over_its_own_parts():
    goals, script, spots = sections()
    kinds = profiles.load("etl")["objectives"]["kinds"]
    assert set(script["kinds"]) == set(kinds)
    real = list(goals["teams"])
    for kind, rule in script["kinds"].items():
        find = kinds[kind]["playtest"]["find"]
        parts = {"anchor", *(find.get("parts") or {})}
        groups = {str(g["at"]) for g in rule["groups"]}
        assert groups <= parts, kind
        for g in rule["groups"]:
            for words in (g["teams"], g["initially"]):
                for w in words:
                    assert w in ("owner", "other", *real), (kind, w)
        for ev in rule["events"]:
            # YAML 1.1 reads on/off as booleans: the keys must stay enable/disable.
            assert set(ev) <= {"name", "trigger", "announce", "enable", "disable"}, ev
            assert ("trigger" in ev) != ("announce" in ev), ev
            for entry in [*ev.get("enable", []), *ev.get("disable", [])]:
                assert entry["group"] in groups, (kind, ev["name"])
    assert {t["bit"] for t in goals["teams"].values()} == {2, 4}
    assert goals["version"] == 1 and goals["table"] == "_MG"
    assert spots["confidence"] == "unverified"


# --------------------------------------------------------------------------------------
# Writers and readers
# --------------------------------------------------------------------------------------


def _spots():
    return [
        botgoals.Spot(
            "DEFEND",
            "DEFEND_gate_axis_1",
            ["axis"],
            (100.0, -20.5, 24.0),
            aims=[(1.0, 0.0, 0.0), (0.6, 0.8, 0.0)],
            stance="crouch",
            camp_time=(20, 40),
            objective="gate",
            group="anchor",
            initially=["axis"],
        ),
        botgoals.Spot(
            "SNIPE",
            "SNIPE_allies_1",
            ["allies"],
            (-512.25, 64.0, 88.0),
            aims=[(0.0, -1.0, 0.0)],
            camp_time=(60, 120),
            initially=["allies"],
        ),
        botgoals.Spot(
            "MOBILEMORTAR",
            "MOBILEMORTAR_both_1",
            ["axis", "allies"],
            (0.0, 0.0, 24.0),
            aims=[(0.0, 0.5, 0.866)],
            initially=["axis", "allies"],
        ),
    ]


def test_goal_table_reads_back():
    goals, script, _ = sections()
    text = botgoals.goals_text(_spots(), goals, "test")
    table = botgoals.read_goals(text, goals)
    assert table.pop("Version") == 1
    assert set(table) == {"DEFEND_gate_axis_1", "SNIPE_allies_1", "MOBILEMORTAR_both_1"}
    d = table["DEFEND_gate_axis_1"]
    assert d["Position"] == (100.0, -20.5, 24.0)
    assert d["AimVectors"] == {0: (1.0, 0.0, 0.0), 1: (0.6, 0.8, 0.0)}
    assert (d["MinCampTime"], d["MaxCampTime"], d["Stance"]) == (20, 40, "crouch")
    assert d["TeamAvailability"] == 2
    assert table["SNIPE_allies_1"]["TeamAvailability"] == 4
    assert "Stance" not in table["SNIPE_allies_1"]
    # The mortar reads its aim from MortarAim and has no camp time or stance.
    m = table["MOBILEMORTAR_both_1"]
    assert m["MortarAim"] == {0: (0.0, 0.5, 0.866)} and "AimVectors" not in m
    assert m["TeamAvailability"] == 6 and "MinCampTime" not in m


def test_goal_reader_reads_the_shipped_goal_files():
    nav = stock_nav()
    if nav is None:
        pytest.skip("no ET: Legacy install with the Omni-bot add-on (NRC_FS_BASEPATH)")
    files = sorted(nav.glob("*_goals.gm"))
    assert len(files) > 600
    for f in files:
        # Every one is a literal table with Version = 1, the layout the writer follows.
        table = botgoals.read_goals(f.read_text(errors="replace"), sections()[0])
        assert table.get("Version") == 1, f.name
    oasis = botgoals.read_goals((nav / "oasis_goals.gm").read_text(), sections()[0])
    snipe = oasis["SNIPE_p2axis_old_city_oasis"]
    assert snipe["TeamAvailability"] == 2 and snipe["Position"] == (6321.25, 8087.3, -205.731)


def _hooks():
    on = [("anchor", "axis")]
    return [
        botgoals.Hook("gate", "dynamite", "planted", 'Planted at the "Gate".', "game"),
        botgoals.Hook("gate", "dynamite", "destroyed", "The gate is down!", "map", off=on),
        botgoals.Hook("gate", "dynamite", "rebuilt", "Back up", "map", on=on),
        botgoals.Hook("gate", "dynamite", "silent", None, "map", reason="no wm_announce"),
    ]


def test_map_script_reads_back():
    goals, script, _ = sections()
    spots = _spots()
    spots[0].initially = []
    text = botgoals.script_text_for(_hooks(), spots, goals, script, "nrc: ", "test")
    parsed = botgoals.read_script(text, script)
    assert parsed["globals"] == ["Map", "OnMapLoad"]
    names = [t for t, _f in parsed["triggers"]]
    # Quotes survive; a hook with no trigger name registers nothing.
    assert names == ['Planted at the "Gate".', "The gate is down!", "Back up"]
    funcs = parsed["functions"]
    destroyed = funcs[dict(parsed["triggers"])["The gate is down!"]]
    assert destroyed["switches"] == [("TEAM.AXIS", False, ["DEFEND_gate_axis_1"])]
    assert destroyed["messages"] == ["nrc: gate destroyed"]
    assert funcs[dict(parsed["triggers"])["Back up"]]["switches"] == [
        ("TEAM.AXIS", True, ["DEFEND_gate_axis_1"])
    ]
    assert funcs[dict(parsed["triggers"])['Planted at the "Gate".']]["switches"] == []
    # Spots that start off are switched off at map load, per team.
    assert funcs["OnMapLoad"]["switches"] == [("TEAM.AXIS", False, ["DEFEND_gate_axis_1"])]


def test_script_reader_rejects_what_the_writer_never_makes():
    with pytest.raises(botgoals.BotGoalsError, match="unbalanced|unclosed"):
        botgoals.read_script("global OnMapLoad = function() { x = { 1, };", sections()[1])
    with pytest.raises(botgoals.BotGoalsError, match="does not define"):
        botgoals.read_script(
            'global OnMapLoad = function() { OnTrigger( "a", Map.nope ); };', sections()[1]
        )
    with pytest.raises(botgoals.BotGoalsError):
        botgoals.read_goals("global _MG = { A = Vec3(1, 2), };", sections()[0])


# --------------------------------------------------------------------------------------
# GameMonkey: the whole language, byte for byte, and mirroring
# --------------------------------------------------------------------------------------

# Every construct of the grammar the shipped scripts use (gmscriptex gmParser.y): switch with
# case labels, obj:method() calls, this and member, fork, foreach with key and value, for,
# dowhile, default parameters, table(), [index] fields, trailing commas, adjacent strings,
# escapes, backquoted and character constants, hex and float forms, compound assignment, and
# comments everywhere (an unclosed block comment runs to the end).
GM_ALL = """// header\r
global Map =\r
{\r
\tDebug = 0, /* inline */ Count = 0x1F + 0b101 % 3,\r
\tBounds = AABB(-397.000,2155.000,-126.000,-175.000,2540.000,-52.000),\r
\tTruck = function( trigger, extra = 2.5e1f )\r
\t{\r
\t\tswitch( Map.Count )\r
\t\t{\r
\t\t\tcase 1: // first\r
\t\t\t{\r
\t\t\t\tSetAvailableMapGoals( TEAM.ALLIES, false, "ESCORT_truck" );\r
\t\t\t}\r
\t\t\tcase TEAM.AXIS:\r
\t\t\tdefault:\r
\t\t\t{\r
\t\t\t\tMap.Count -= 1;\r
\t\t\t}\r
\t\t}\r
\t\tif ( !Map.Debug && trigger.Action != "x" ) { return; }\r
\t\telse if ( Map.Debug == null or -.5 < 1. ) { yield(); }\r
\t\telse { sleep(1); }\r
\t\tUtil.MapDebugPrint( "a \\"q\\" " "b\\065" );\r
\t},\r
\tRegion = function() { bot:SetFacing( Vec3(1, 0, 0), true ); member m = this.x; },\r
};\r
\r
global OnMapLoad = function()\r
{\r
\tlocal t = table( 1, 2, 3 );\r
\tforeach ( k and v in { [0] = 'ab', `back``tick`, } ) { t[k] = ~v; }\r
\tfor ( i = 0; i < 3; i += 1 ) { fork th { sleep(0.1); } }\r
\tdowhile ( false ) { break; }\r
\tOnTrigger( "the truck has moved", Map.Truck );\r
\tUtil.SetGoalPosition( 29.874, 2384.431, -103.875, "PLANT_Gate" );\r
\tVec3( x, 0, 0 );\r
};\r
/* unclosed"""


def test_gamemonkey_reads_the_whole_language_and_writes_it_back_byte_for_byte():
    script = botgoals.gm_parse(GM_ALL)
    assert script.emit() == GM_ALL
    calls = {script.compact(n.kids[0]) for n, _f in script.walk() if n.kind == "call"}
    assert {"SetAvailableMapGoals", "Util.MapDebugPrint", "OnTrigger", "AABB"} <= calls
    assert any(n.kind == "method" and n.value == "SetFacing" for n, _f in script.walk())
    parsed = botgoals.read_script(GM_ALL, sections()[1])
    assert parsed["globals"] == ["Map", "OnMapLoad"]
    assert parsed["triggers"] == [("the truck has moved", "Truck")]
    truck = parsed["functions"]["Truck"]
    # A switch with a single name, inside a case; adjacent strings join, escapes decode
    # (the digits of a backslash escape are decimal, gmCodeTree.cpp:510-521).
    assert truck["switches"] == [("TEAM.ALLIES", False, ["ESCORT_truck"])]
    assert truck["messages"] == ['a "q" bA']
    assert set(parsed["functions"]) == {"Truck", "Region", "OnMapLoad"}


@pytest.mark.parametrize(
    "text, where",
    [
        # Shapes of the four shipped scripts the add-on's compiler refuses.
        ("global A = function() { }\nglobal B = 1;", "line 2: expected ';'"),
        ("f = function() { g()\n if (x) { } };", "line 2: expected ';'"),
        ("M = { a = function() { p(); } }, };", "expected ';'"),
        ("M = { a = function() { else { } } };", "expected a value"),
        ("x = 1 $ 2;", "unexpected character '$' at byte 6"),
        ('x = "open;', "unexpected character"),
        # Assignment is a statement, a field index an integer (gmParser.y:393, :775).
        ("a = b = 1;", "expected ';'"),
        ('t = { ["k"] = 1 };', "an integer index"),
        ("t = table( 1, );", "a field"),
        ("if (x) y();", "expected '{'"),
    ],
)
def test_gamemonkey_refuses_what_the_addon_compiler_refuses(text, where):
    with pytest.raises(botgoals.BotGoalsError, match=re.escape(where)):
        botgoals.gm_parse(text)


def test_gamemonkey_mirror_negates_literal_coordinates_in_place():
    coords = bots()["coordinates"]
    out, report = botgoals.gm_mirror(GM_ALL, coords, 0)
    assert "AABB(175.000,2155.000,-126.000,397.000,2540.000,-52.000)" in out
    assert "Util.SetGoalPosition( -29.874, 2384.431" in out
    # A zero stays as written; a computed component is reported, not guessed.
    assert "Vec3(1, 0, 0)" not in out and "Vec3(-1, 0, 0)" in out
    assert report["points"] == 2 and report["boxes"] == 1
    assert [n["call"] for n in report["not_mirrored"]] == ["Vec3( x, 0, 0 )"]
    # Only those numbers change: mirroring twice gives the file back.
    assert botgoals.gm_mirror(out, coords, 0)[0] == GM_ALL
    zero, _ = botgoals.gm_mirror("v = Vec3(-0.0, 0, 5);", coords, 0)
    assert zero == "v = Vec3(-0.0, 0, 5);"
    y, _ = botgoals.gm_mirror("v = Vec3(1, 2, 3); b = AABB(1,2,3,4,5,6);", coords, 1)
    assert y == "v = Vec3(1, -2, 3); b = AABB(1,-5,3,4,-2,6);"


def test_gamemonkey_mirror_off_the_origin_reflects_positions_and_turns_directions():
    coords = bots()["coordinates"]
    text = (
        'Util.SetGoalPosition( 10.5, 20, 30, "g" );\n'
        'Util.SetGoalOffset( 16, 0, 0, "g" );\n'
        "b = AABB(-50, 0, 0, 200, 8, 8);\n"
    )
    out, report = botgoals.gm_mirror(text, coords, 0, at=100)
    assert out == (
        'Util.SetGoalPosition( 189.5, 20, 30, "g" );\n'
        'Util.SetGoalOffset( -16, 0, 0, "g" );\n'
        "b = AABB(0, 0, 0, 250, 8, 8);\n"
    )
    assert (report["points"], report["boxes"]) == (2, 1)
    # Back through the same plane is the file again.
    assert botgoals.gm_mirror(out, coords, 0, at=100)[0] == text
    # A vector constructor may hold a place or a direction: refused, naming the line.
    with pytest.raises(botgoals.BotGoalsError, match=r"line 2: .*Vec3\( 1, 0, 0 \)"):
        botgoals.gm_mirror("x = 1;\nFacing = Vec3( 1, 0, 0 );\n", coords, 0, at=100)
    # Through the origin it is still the sign flip, positions and directions alike.
    assert botgoals.gm_mirror(text, coords, 0, at=0)[0] == botgoals.gm_mirror(text, coords, 0)[0]


def omnibot_root() -> Path | None:
    nav = stock_nav()
    return nav.parent.parent if nav else None


# The shipped scripts with syntax errors (a missing ';' or a stray brace or 'else'), which the
# add-on's compiler refuses too: 1338 of the 1339 et/nav scripts compile with gmscriptex.
GM_REFUSED = {
    "et/incomplete_navs/with_script/2skilled.gm": "line 11:",
    "et/incomplete_navs/with_script/canyon_depths_b5.gm": "line 1033:",
    "et/incomplete_navs/with_script/fatal_mill_b4.gm": "line 174:",
    "et/nav/wolken3_final.gm": "line 340:",
}


def _check_shipped_script(args: tuple[str, str]) -> tuple[str, str | None]:
    """One shipped script: parsed, written back byte for byte, mirrored in x and back to the
    same bytes; a goal table's positions mirror, a map script reads. Returns the error."""
    root, name = args
    goals_spec, script_spec, _ = sections()
    coords = bots()["coordinates"]
    path = Path(root) / name
    text = path.read_bytes().decode("latin-1")
    try:
        script = botgoals.gm_parse(text)
    except botgoals.BotGoalsError as e:
        return name, str(e)
    if script.emit() != text:
        return name, "not written back byte for byte"
    mirrored, _report = botgoals.gm_mirror(script, coords, 0, check=False)
    flipped = botgoals.gm_parse(mirrored)
    if botgoals.gm_mirror(flipped, coords, 0, check=False)[0] != text:
        return name, "mirrored twice is not the file"
    if path.parent.name in ("nav", "with_script", "no_script", "priority_maps"):
        if path.name.endswith("_goals.gm"):
            table = botgoals.read_goals(text, goals_spec)
            back = botgoals.read_goals(mirrored, goals_spec)
            for key, row in table.items():
                if isinstance(row, dict) and isinstance(row.get("Position"), tuple):
                    x, y, z = row["Position"]
                    if back[key]["Position"] != (-x, y, z):
                        return name, f"{key} did not mirror"
        else:
            botgoals.read_script(text, script_spec, strict=False)
    return name, "ok"


def test_gamemonkey_reads_mirrors_and_writes_back_every_shipped_script():
    root = omnibot_root()
    if root is None:
        pytest.skip("no ET: Legacy install with the Omni-bot add-on (NRC_FS_BASEPATH)")
    from concurrent.futures import ProcessPoolExecutor

    names = sorted(f.relative_to(root).as_posix() for f in root.rglob("*.gm"))
    assert len(names) > 2000
    with ProcessPoolExecutor(max_workers=min(16, os.cpu_count() or 1)) as pool:
        results = dict(pool.map(_check_shipped_script, [(str(root), n) for n in names]))
    refused = {n: e for n, e in results.items() if e != "ok"}
    assert set(refused) == set(GM_REFUSED), refused
    for name, where in GM_REFUSED.items():
        assert refused[name].startswith(where), refused[name]


GMCHECK = os.environ.get("NRC_GM_CHECK")


@pytest.mark.skipif(not GMCHECK, reason="NRC_GM_CHECK names no GameMonkey checker binary")
def test_written_scripts_compile_with_the_addon_script_engine(tmp_path):
    # NRC_GM_CHECK: a GameMonkey compiler built from the gmscriptex sources Omni-bot 0.93
    # embeds; `--run` executes a file (the goal table needs only Vec3).
    goals, script, _ = sections()
    spots = _spots()
    (tmp_path / "m_goals.gm").write_text(botgoals.goals_text(spots, goals, "t"))
    (tmp_path / "m.gm").write_text(
        botgoals.script_text_for(_hooks(), spots, goals, script, "nrc: ", "t")
    )
    run = subprocess.run([GMCHECK, "--run", str(tmp_path / "m_goals.gm")], capture_output=True)
    assert run.returncode == 0, run.stderr
    assert b"_MG entries: 4" in run.stdout
    check = subprocess.run([GMCHECK, str(tmp_path / "m.gm")], capture_output=True)
    assert check.returncode == 0, check.stderr


def test_trigger_names_are_cut_where_the_game_cuts_them():
    _goals, script, _ = sections()
    assert script["trigger_bytes"] == 71
    text = "x" * 100
    found = objtests.Found("dynamite", "o", "axis", {"team": "axis"}, {"trigger": [], "anchor": []})
    rules = {"kinds": {"dynamite": {"groups": [], "events": [{"name": "e", "trigger": text}]}}}
    (hook,) = botgoals.hooks([found], "", "etl", {**script, **rules}, ["axis", "allies"])
    assert hook.trigger == "x" * 71


def test_announcements_are_followed_through_script_calls():
    _goals, script, _ = sections()
    text = (
        "relay\n{\n\tdeath\n\t{\n\t\ttrigger game_manager relay_down\n\t}\n}\n"
        'game_manager\n{\n\ttrigger relay_down\n\t{\n\t\twm_announce "Relay gone"\n\t}\n}\n'
        "crate\n{\n\tdeath\n\t{\n\t}\n}\n"
    )
    real = ["axis", "allies"]
    parts = {"trigger": [{"keys": [["track", "Relay Room"]], "index": 1}], "anchor": []}
    relay = objtests.Found("dynamite", "relay", "axis", {"team": "axis"}, parts)
    crate = objtests.Found("dynamite", "crate", "axis", {"team": "axis"}, parts)
    got = {
        (h.objective, h.event): h for h in botgoals.hooks([relay, crate], text, "etl", script, real)
    }
    assert got["relay", "planted"].trigger == "Planted at Relay Room."
    assert got["relay", "defused"].trigger == "Defused at Relay Room."
    assert got["relay", "destroyed"].trigger == "Relay gone"
    assert got["relay", "destroyed"].off == [("anchor", "axis")]
    assert got["crate", "destroyed"].trigger is None
    assert "wm_announce" in got["crate", "destroyed"].reason


def test_team_words():
    real = ["axis", "allies"]
    assert botgoals._teams_of(["owner"], "axis", real) == ["axis"]
    assert botgoals._teams_of(["owner"], "neutral", real) == real
    assert botgoals._teams_of(["other"], "axis", real) == ["allies"]
    with pytest.raises(botgoals.BotGoalsError):
        botgoals._teams_of(["red"], "axis", real)


# --------------------------------------------------------------------------------------
# A small map: two rooms joined by a doorway, a dynamite objective in the Axis room
# --------------------------------------------------------------------------------------

SCRIPT = 'gate\n{\n\tdeath\n\t{\n\t\twm_announce "The gate has been destroyed!"\n\t}\n}\n'


def _two_rooms(tmp_path: Path) -> Path:
    maps = tmp_path / "maps"
    maps.mkdir()
    path = write_map(
        maps,
        [
            *room(0, 0, 2048, 768),
            # A wall across the middle with one doorway.
            box((1008, 16, 0), (1040, 320, 256)),
            box((1008, 448, 0), (1040, 752, 256)),
        ],
        [
            point_entity("team_CTF_redspawn", (1900, 384, 24)),
            point_entity("team_CTF_bluespawn", (120, 384, 24)),
            _brush_entity(
                "trigger_objective_info",
                box((1600, 600, 0), (1760, 752, 128), "common/trigger"),
                track="the Gate",
                target="gate",
                spawnflags="1",
            ),
            _brush_entity(
                "func_explosive",
                box((1648, 688, 0), (1712, 752, 96)),
                targetname="gate",
                scriptname="gate",
                constructible_class="3",
            ),
        ],
        name="duel.map",
    )
    (maps / "duel.script").write_text(SCRIPT)
    return path


def test_tool_writes_goals_and_script_beside_the_waypoints(tmp_path):
    path = _two_rooms(tmp_path)
    srv.map_open(str(path))
    report = srv.bot_waypoints_write(spacing=96, detail="full")
    nav = tmp_path / "omni-bot" / "et" / "nav"
    assert sorted(p.name for p in nav.iterdir()) == ["duel.gm", "duel.way", "duel_goals.gm"]
    goals = report["goals"]
    assert goals["files"] == {
        "et/nav/duel_goals.gm": str(nav / "duel_goals.gm"),
        "et/nav/duel.gm": str(nav / "duel.gm"),
    }
    assert goals["objectives"] == [{"objective": "gate", "kind": "dynamite", "team": "axis"}]
    defend = [s for s in goals["spots"] if s["type"] == "DEFEND"]
    assert defend and all(s["teams"] == ["axis"] and s["initially"] == ["axis"] for s in defend)
    wf = botnav.read((nav / "duel.way").read_bytes(), bots()["format"])
    for s in defend:
        # On a waypoint, in the Axis room, standing, near the gate but out of its box.
        w = wf.waypoints[s["why"]["waypoint"]]
        assert list(w.position) == s["position"]
        assert s["position"][0] > 1040
        assert 96 <= s["why"]["distance"] <= 768
        assert s["why"]["route_points_seen"] > 0
        # Facing the way the Allies come in, from the doorway side (west).
        (aim,) = s["aims"]
        assert math.isclose(math.hypot(*aim), 1.0, abs_tol=1e-3) and aim[0] < 0
    triggers = {t["event"]: t for t in goals["triggers"]}
    assert triggers["planted"]["trigger"] == "Planted at the Gate."
    assert triggers["destroyed"]["trigger"] == "The gate has been destroyed!"
    assert triggers["destroyed"]["off"] == sorted(s["name"] for s in defend)
    assert goals["not_hooked"] == []
    table = botgoals.read_goals((nav / "duel_goals.gm").read_text(), sections()[0])
    assert {k for k in table if k != "Version"} == {s["name"] for s in goals["spots"]}
    parsed = botgoals.read_script((nav / "duel.gm").read_text(), sections()[1])
    assert ("The gate has been destroyed!", "gate_destroyed") in parsed["triggers"]
    # The summary leaves out why each spot was chosen.
    summary = srv.bot_waypoints_write(spacing=96)
    assert all("why" not in s for s in summary["goals"]["spots"])


def test_caller_spots_join_the_chosen_ones(tmp_path):
    path = _two_rooms(tmp_path)
    srv.map_open(str(path))
    mine = {
        "type": "attack",
        "team": "allies",
        "position": [900, 384, 0],
        "aim_at": [1700, 700, 64],
        "name": "doorway",
        "objective": "gate",
        "camp_time": [5, 10],
    }
    report = srv.bot_waypoints_write(spacing=96, spots=[mine], auto_spots=False, detail="full")
    (spot,) = report["goals"]["spots"]
    assert spot["name"] == "ATTACK_doorway_1" and spot["source"] == "caller"
    assert spot["teams"] == ["allies"] and spot["camp_time"] == [5, 10]
    # The gate's spots start on for the Axis only, so an Allied spot on it starts off.
    assert spot["initially"] == []
    assert spot["position"][2] == 24.0 and abs(spot["position"][0] - 900) <= 16
    assert spot["aims"][0][0] > 0
    for bad, match in (
        ({**mine, "type": "PATROL"}, "type must be one of"),
        ({**mine, "team": "red"}, "team must be one of"),
        ({k: v for k, v in mine.items() if k != "aim_at"}, "needs aim_at"),
        ({**mine, "yaw": 90}, "not both"),
        ({**mine, "objective": "nothing"}, "is none of"),
        ({**mine, "camp_time": [10, 5]}, "camp_time"),
        ({**mine, "stance": "crouch", "type": "MOBILEMG42"}, "stance"),
        ({**mine, "colour": "red"}, "unknown keys"),
    ):
        with pytest.raises(ToolError, match=match):
            srv.bot_waypoints_write(spacing=96, spots=[bad], auto_spots=False)


def test_unannounced_objective_events_are_named(tmp_path):
    path = _two_rooms(tmp_path)
    (path.parent / "duel.script").write_text("gate\n{\n\tdeath\n\t{\n\t}\n}\n")
    srv.map_open(str(path))
    report = srv.bot_waypoints_write(spacing=96)
    (row,) = report["goals"]["not_hooked"]
    assert row["event"] == "destroyed" and "wm_announce" in row["not_hooked"]
    assert any("destroyed is not hooked" in w for w in report["warnings"])


# --------------------------------------------------------------------------------------
# What bot_playtest reads back
# --------------------------------------------------------------------------------------


def test_script_report_reads_the_logs_and_spot_occupancy(tmp_path):
    goals, script, _ = sections()
    spots = _spots()[:2]
    (tmp_path / "m_goals.gm").write_text(botgoals.goals_text(spots, goals, "t"))
    hooks = [
        botgoals.Hook("gate", "dynamite", "destroyed", "Down!", "map", off=[("anchor", "axis")])
    ]
    (tmp_path / "m.gm").write_text(
        botgoals.script_text_for(hooks, spots, goals, script, "nrc: ", "t")
    )
    sample = (
        '{t:>8} NRC_BOTS tok {{"time":{t},"bots":[{{"client":1,"team":"red","dead":false,'
        '"stance":"stand","position":[{x},-20.0,24.0]}}]}}'
    )
    game = "\n".join(
        [
            "       0 ^22 Goals Loaded, 0 Goals Deferred. elapsed time: 0.00 seconds",
            sample.format(t=2000, x=100),
            sample.format(t=4000, x=110),
            "    5000 ^2^bnrc: gate destroyed",
            sample.format(t=6000, x=900),
            sample.format(t=8000, x=105),
        ]
    )
    addon = (
        "I 11/03 13:08:57 Running script: nav/m.gm\nW 11/03 x goal query for X has no results\n"
        # Defusing enabled before anything is planted: expected, listed apart.
        "W 11/03 x SetAvailableMapGoals: goal query for DEFUSE_gate.* has no results\n"
    )
    files = [tmp_path / "m.way", tmp_path / "m_goals.gm", tmp_path / "m.gm"]
    rep = botplay.script_report(bots(), files, "m", game, addon, "tok", 2000)
    assert rep["goals_loaded"] == [{"loaded": 2, "deferred": 0}]
    assert rep["goal_errors"] == [] and len(rep["script_errors"]) == 1
    assert [w.split("for ")[1] for w in rep["script_warnings"]] == ["DEFUSE_gate.* has no results"]
    assert rep["triggers_registered"] == 1
    (msg,) = rep["script_messages"]
    assert msg == {
        "time": 5.0,
        "message": "nrc: gate destroyed",
        "enabled": [],
        "disabled": ["DEFEND_gate_axis_1"],
    }
    rows = {r["name"]: r for r in rep["spots"]}
    assert rows["DEFEND_gate_axis_1"]["occupied"] == [[2.0, 4.0], [8.0, 8.0]]
    # The red (Axis) bot never counts on an Allied spot.
    assert rows["SNIPE_allies_1"]["samples"] == 0
    assert (
        botplay.script_report(bots(), [tmp_path / "m.way"], "m", game, addon, "tok", 2000) is None
    )
