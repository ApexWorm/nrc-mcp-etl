"""Map scripts: the parser, and the cross-check against the map's entities.

The parser tests pin the engine's own tokenizer behaviour, including the quirks a shipped map
relies on. The check tests run against a synthetic `mapscript:` spec, so they say what the
module does rather than what one game's profile happens to contain; the last ones run the
shipped ET: Legacy profile end to end.

Scripts are written one statement per line on purpose: an action's arguments run to the end of
its line, so `wait 1 } }` would swallow both braces — in the engine as here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nrc_mcp import mapscript, profiles


def ent(index: int, classname: str, **keys) -> dict:
    return {
        "index": index,
        "classname": classname,
        "keys": [("classname", classname), *keys.items()],
    }


SPEC = {
    "confidence": "verified",
    "file": "{map}.script",
    "block_key": "scriptname",
    "implied_names": [{"classname": "manager", "name": "game_manager"}],
    "brace_actions": ["create"],
    "spawning_actions": ["create"],
    "events": ["spawn", "trigger", "death"],
    "actions": ["wait", "trigger", "setstate", "alertentity", "setautospawn", "create"],
    "references": [
        {
            "action": "trigger",
            "arg": 0,
            "resolves": "block",
            "special": ["self", "global"],
            "event": "trigger",
            "event_label_arg": 1,
            "severity": "warning",
        },
        {"action": "alertentity", "arg": 0, "resolves": "targetname", "severity": "error"},
        {"action": "setstate", "arg": 0, "resolves": "targetname", "severity": "warning"},
        {
            "action": "setautospawn",
            "arg": 0,
            "resolves": [{"key": "description", "classname": "marker"}],
            "case_insensitive": True,
            "severity": "error",
        },
    ],
    "entity_events": [
        {
            "classname": "relay",
            "label_key": "target",
            "block_keys": ["ainame", "scriptname"],
            "event": "trigger",
            "also_targets": "targetname",
            "severity": "warning",
        }
    ],
}


@pytest.fixture
def spec(monkeypatch):
    monkeypatch.setattr(profiles, "load", lambda _pid: {"mapscript": SPEC})


def check(tmp_path: Path, script: str | None, entities: list[dict]) -> dict[str, list]:
    if script is not None:
        (tmp_path / "m.script").write_text(script)
    out: dict[str, list] = {}
    for f in mapscript.check("test", entities, tmp_path / "m.map"):
        out.setdefault(f.code, []).append(f)
    return out


def block(name: str, *events: tuple[str, list[str]]) -> str:
    """A block in the layout mappers write: braces and statements each on their own line."""
    body = "".join(
        f"  {ev}\n  {{\n" + "".join(f"    {a}\n" for a in acts) + "  }\n" for ev, acts in events
    )
    return f"{name}\n{{\n{body}}}\n"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_tokens_skip_comments_keep_quotes_and_count_lines():
    tokens = mapscript.tokenize('a // gone\n"two words" /* also\ngone */ b\n')
    assert [(t.text, t.line) for t in tokens] == [("a", 1), ("two words", 2), ("b", 3)]


def test_a_script_parses_into_blocks_events_and_actions():
    script = "entity " + block(
        "door",
        ("spawn", ["wait 50", "trigger self open"]),
        ("trigger open", ["setstate gate default"]),
    )
    (door,) = mapscript.parse(script)
    assert door.name == "door"
    assert [(e.name, e.params) for e in door.events] == [("spawn", []), ("trigger", ["open"])]
    assert [(a.name, a.args) for a in door.events[0].actions] == [
        ("wait", ["50"]),
        ("trigger", ["self", "open"]),
    ]
    assert door.has_event("trigger", ["OPEN"]), "event labels compare without regard to case"


def test_action_arguments_end_at_the_line_and_braced_actions_take_a_list():
    script = """x
{
  spawn
  {
    create
    {
      scriptName "made"
      classname "thing"
    }
    wait 5
  }
}
"""
    (b,) = mapscript.parse(script, frozenset({"create"}))
    assert [(a.name, a.args) for a in b.events[0].actions] == [
        ("create", ["scriptName", "made", "classname", "thing"]),
        ("wait", ["5"]),
    ]


def test_a_token_starting_with_a_brace_is_a_brace():
    """The engine tests only the first character; a shipped map closes a block with `}ll`."""
    script = """a
{
  spawn
  {
    wait 1
  }
}ll
b
{
  spawn
  {
    wait 2
  }
}
"""
    first, second = mapscript.parse(script)
    assert (first.name, second.name) == ("a", "b")


@pytest.mark.parametrize(
    "text, message",
    [
        ("a\n{\n  spawn\n  {\n    wait 1\n  }\n", "end of script"),
        ("}\n", "not expected"),
        ("{\na\n}\n", "name expected"),
        ("a\nspawn\n{\n}\n", "'{' expected"),
    ],
)
def test_syntax_the_engine_refuses_is_a_syntax_error(text, message):
    with pytest.raises(mapscript.ScriptSyntaxError, match=message):
        mapscript.parse(text)


# ---------------------------------------------------------------------------
# Checking against the map
# ---------------------------------------------------------------------------


def test_no_mapscript_section_means_no_findings(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "load", lambda _pid: {})
    assert mapscript.check("test", [], tmp_path / "m.map") == []


def test_a_missing_script_is_reported(tmp_path, spec):
    found = check(tmp_path, None, [])
    assert found["MAPSCRIPT_MISSING"][0].severity == "warning"


def test_a_syntax_error_stops_the_game(tmp_path, spec):
    found = check(tmp_path, "a\n{\n  spawn\n  {\n    wait 1\n  }\n", [])
    assert found["MAPSCRIPT_SYNTAX"][0].severity == "error"


def test_blocks_run_for_the_entity_that_names_them(tmp_path, spec):
    script = "".join(block(n, ("spawn", ["wait 1"])) for n in ("door", "ghost", "game_manager"))
    found = check(tmp_path, script, [ent(0, "func_door", ScriptName="DOOR"), ent(1, "manager")])
    (dead,) = found["MAPSCRIPT_BLOCK_WITHOUT_ENTITY"]
    assert "'ghost'" in dead.message, "key and name both match without regard to case"


def test_unknown_words_stop_the_game_only_in_a_block_that_runs(tmp_path, spec):
    script = block("door", ("sp4wn", ["wait 1"]), ("spawn", ["w8 1"])) + block(
        "ghost", ("sp4wn", ["w8 1"])
    )
    found = check(tmp_path, script, [ent(3, "func_door", scriptname="door")])
    severities = sorted(
        (f.code, f.severity)
        for f in found["MAPSCRIPT_UNKNOWN_EVENT"] + found["MAPSCRIPT_UNKNOWN_ACTION"]
    )
    assert severities == [
        ("MAPSCRIPT_UNKNOWN_ACTION", "error"),
        ("MAPSCRIPT_UNKNOWN_ACTION", "warning"),
        ("MAPSCRIPT_UNKNOWN_EVENT", "error"),
        ("MAPSCRIPT_UNKNOWN_EVENT", "warning"),
    ]
    live = [f for f in found["MAPSCRIPT_UNKNOWN_EVENT"] if f.severity == "error"]
    assert live[0].entities == [3]


def test_references_resolve_and_repeats_are_reported_once(tmp_path, spec):
    script = block(
        "game_manager",
        (
            "spawn",
            [
                "alertentity bell",
                "alertentity nobell",
                "setstate gate default",
                "setstate nogate default",
                "setstate nogate invisible",
                'setautospawn "axis yard" 0',
                'setautospawn "Nowhere" 1',
            ],
        ),
    )
    entities = [
        ent(0, "manager"),
        ent(1, "bell_thing", targetname="bell"),
        ent(2, "gate_thing", targetname="gate"),
        ent(3, "marker", description="Axis Yard"),
    ]
    found = check(tmp_path, script, entities)
    msgs = sorted((f.severity, f.message) for f in found["MAPSCRIPT_UNRESOLVED_REFERENCE"])
    assert [s for s, _ in msgs] == ["error", "error", "warning"]
    assert any("'nobell'" in m for _, m in msgs)
    assert any("'Nowhere'" in m for _, m in msgs), "case folds, spelling does not"
    (gate,) = [m for _, m in msgs if "nogate" in m]
    assert "line 8, 9" in gate, "one finding per name, every line listed"


def test_a_trigger_needs_the_event_it_names(tmp_path, spec):
    script = block(
        "game_manager",
        (
            "spawn",
            [
                "trigger door open",
                "trigger door shut",
                "trigger self x",
                "trigger global x",
                "trigger nobody x",
            ],
        ),
    ) + block("door", ("trigger open", ["wait 1"]))
    found = check(tmp_path, script, [ent(0, "manager"), ent(1, "d", scriptname="door")])
    (missing,) = found["MAPSCRIPT_MISSING_EVENT"]
    assert "door shut" in missing.message
    (unknown,) = found["MAPSCRIPT_UNRESOLVED_REFERENCE"]
    assert "'nobody'" in unknown.message, "self and global are not block names"


def test_a_reference_that_never_runs_is_reported_one_step_milder(tmp_path, spec):
    script = (
        block("game_manager", ("spawn", ["alertentity shared", "trigger door shut"]))
        + block("ghost", ("spawn", ["alertentity shared", "alertentity only_dead"]))
        + block("door", ("trigger open", ["wait 1"]))
        + block("game_manager", ("spawn", ["alertentity second_copy"]))
    )
    found = check(tmp_path, script, [ent(0, "manager"), ent(1, "d", scriptname="door")])
    refs = {f.message.split("'")[1]: f for f in found["MAPSCRIPT_UNRESOLVED_REFERENCE"]}
    assert refs["shared"].severity == "error", "one live use is enough"
    assert refs["only_dead"].severity == "warning"
    assert "never run" in refs["only_dead"].message
    assert "second_copy" not in refs, "the engine reads only the first block of a name"
    (missing,) = found["MAPSCRIPT_MISSING_EVENT"]
    assert missing.severity == "warning", "trigger is a warning when it runs"


def test_entities_the_script_creates_are_real(tmp_path, spec):
    script = """game_manager
{
  spawn
  {
    create
    {
      scriptName "made"
      targetname "made_t"
    }
    setstate made_t default
  }
}
""" + block("made", ("spawn", ["wait 1"]))
    found = check(tmp_path, script, [ent(0, "manager")])
    assert "MAPSCRIPT_BLOCK_WITHOUT_ENTITY" not in found
    assert "MAPSCRIPT_UNRESOLVED_REFERENCE" not in found


def test_a_relay_label_must_be_an_event_or_an_entity(tmp_path, spec):
    script = block("door", ("trigger open", ["wait 1"]))
    entities = [
        ent(0, "d", scriptname="door"),
        ent(1, "relay", scriptname="door", target="open"),  # the door's own event
        ent(2, "relay", ainame="door", scriptname="mine", target="open"),  # ainame first
        ent(3, "relay", scriptname="door", target="lamp"),  # an entity's name: a relay
        ent(4, "l", targetname="lamp"),
        ent(5, "relay", scriptname="door", target="nothing"),
    ]
    found = check(tmp_path, script, entities)
    (lost,) = found["MAPSCRIPT_MISSING_EVENT"]
    assert lost.entities == [5]


# ---------------------------------------------------------------------------
# The shipped ET: Legacy profile
# ---------------------------------------------------------------------------


def _etl() -> str:
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    return "etl"


def test_the_etl_profile_knows_the_engines_words():
    spec = profiles.load(_etl())["mapscript"]
    assert len(spec["events"]) == 18
    assert len(spec["actions"]) == 86
    assert {"spawn", "trigger", "death"} <= set(spec["events"])


def test_the_etl_profile_checks_a_small_map_end_to_end(tmp_path):
    pid = _etl()
    (tmp_path / "m.script").write_text(
        block(
            "game_manager",
            ("spawn", ["wait 50", 'setautospawn "Axis Yard" 0', "alertentity missing_thing"]),
        )
    )
    entities = [
        ent(0, "worldspawn"),
        ent(1, "script_multiplayer"),
        ent(2, "team_WOLF_objective", description="Axis Yard", spawnflags="1"),
    ]
    found = [f for f in mapscript.check(pid, entities, tmp_path / "m.map") if f.severity != "info"]
    assert [(f.code, f.severity) for f in found] == [("MAPSCRIPT_UNRESOLVED_REFERENCE", "error")]
    assert "missing_thing" in found[0].message


@pytest.mark.parametrize(
    ("action", "bad", "good"),
    [
        # The script a Tier 2 run wrote for a map with no objectives; the server stopped.
        ("wm_number_of_objectives", "0", "1"),
        ("wm_number_of_objectives", "9", "8"),
        ("wm_setwinner", "2", "-1"),
        ("wm_set_defending_team", "-1", "0"),
        ("wm_objective_status", "0 0 1", "1 0 1"),
        ("wm_objective_status", "1 0 3", "1 0 2"),
        ("wm_voiceannounce", '2 "x"', '1 "x"'),
    ],
)
def test_the_etl_profile_refuses_the_numbers_the_engine_stops_on(tmp_path, action, bad, good):
    pid = _etl()
    entities = [ent(0, "worldspawn"), ent(1, "script_multiplayer")]

    def ranged(args: str) -> list:
        text = block("game_manager", ("spawn", [f"{action} {args}"]))
        found = mapscript.check(pid, entities, tmp_path / "m.map", text=text)
        return [f for f in found if f.code == "MAPSCRIPT_ARGUMENT_RANGE"]

    (finding,) = ranged(bad)
    assert finding.severity == "error"
    assert not ranged(good)


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------

EDITABLE = """// The gate and its trigger.
game_manager
{
\tspawn
\t{
\t\twait 50   // let the map settle
\t}
}

gate
{
\tdeath
\t{
\t\twait 10
\t}
}
"""


def test_the_parser_records_where_each_event_and_block_sits():
    (gm, gate) = mapscript.parse(EDITABLE)
    assert EDITABLE[gm.start : gm.end].startswith("game_manager") and EDITABLE[gm.end - 1] == "}"
    (spawn,) = gm.events
    assert EDITABLE[spawn.start : spawn.end].startswith("spawn")
    assert EDITABLE[spawn.end - 1] == "}" and EDITABLE[gate.close] == "}"


def test_setting_an_event_leaves_the_rest_of_the_file_alone():
    new, did = mapscript.set_event(EDITABLE, SPEC, "game_manager", "spawn", [], ["wait 100"])
    assert did == "replaced event"
    assert new.startswith("// The gate and its trigger.\n")
    assert "wait 100" in new and "let the map settle" not in new
    assert new[new.index("\ngate\n") :] == EDITABLE[EDITABLE.index("\ngate\n") :]


def test_an_added_event_and_block_can_be_removed_back_to_the_original():
    added, did = mapscript.set_event(EDITABLE, SPEC, "gate", "trigger", ["open"], ["wait 5"])
    assert did == "added event"
    assert [e.name for e in mapscript.parse(added)[1].events] == ["death", "trigger"]
    both, did = mapscript.set_event(added, SPEC, "door", "spawn", [], ["wait 1"])
    assert did == "created block"
    back = mapscript.remove(mapscript.remove(both, SPEC, "door"), SPEC, "gate", "trigger", ["open"])
    assert back == EDITABLE


def test_an_event_is_matched_by_its_parameters_without_regard_to_case():
    added, _ = mapscript.set_event(EDITABLE, SPEC, "gate", "trigger", ["open"], ["wait 5"])
    replaced, did = mapscript.set_event(added, SPEC, "GATE", "trigger", ["OPEN"], ["wait 6"])
    assert did == "replaced event"
    assert "\t\twait 5\n" not in replaced and replaced.count("\ttrigger") == 1


def test_a_script_is_created_from_nothing():
    new, did = mapscript.set_event("", SPEC, "game_manager", "spawn", [], ["wait 50"])
    assert did == "created block"
    assert new == "game_manager\n{\n\tspawn\n\t{\n\t\twait 50\n\t}\n}\n"


@pytest.mark.parametrize(
    ("block_name", "event", "actions", "message"),
    [
        ("gate", "explode", ["wait 1"], "unknown event"),
        ("gate", "spawn", ["teleport 1"], "unknown action"),
        ("gate", "spawn", ["wait 1 }"], "would end the event"),
        ("gate", "spawn", ['alertentity "gate'], "unbalanced quote"),
        ("gate", "spawn", ["create origin 0"], "one { ... } list"),
        ("gate", "spawn", ["wait 1\nwait 2"], "one non-empty line"),
        ("my gate", "spawn", ["wait 1"], "one word"),
    ],
)
def test_an_edit_the_engine_would_refuse_is_refused(block_name, event, actions, message):
    with pytest.raises(mapscript.ScriptEditError, match=message):
        mapscript.set_event(EDITABLE, SPEC, block_name, event, [], actions)


def test_a_brace_action_takes_its_list_on_one_line():
    new, _ = mapscript.set_event(
        EDITABLE, SPEC, "gate", "spawn", [], ['create { "classname" "x" "origin" "0 0 0" }']
    )
    (action,) = mapscript.parse(new, frozenset({"create"}))[1].events[-1].actions
    assert action.args == ["classname", "x", "origin", "0 0 0"]


def test_removing_what_is_not_there_says_so():
    with pytest.raises(mapscript.ScriptEditError, match="no block"):
        mapscript.remove(EDITABLE, SPEC, "nothing")
    with pytest.raises(mapscript.ScriptEditError, match="no 'trigger open' event"):
        mapscript.remove(EDITABLE, SPEC, "gate", "trigger", ["open"])


def test_check_can_judge_an_edit_before_it_is_written(tmp_path, spec):
    text = block("game_manager", ("spawn", ["alertentity nowhere"]))
    found = mapscript.check("test", [ent(0, "manager")], tmp_path / "m.map", text=text)
    assert [f.code for f in found] == ["MAPSCRIPT_UNRESOLVED_REFERENCE"]
    assert not (tmp_path / "m.script").exists()


def test_numeric_arguments_out_of_range_stop_the_game_when_they_run(tmp_path, monkeypatch):
    spec = {
        **SPEC,
        "actions": [*SPEC["actions"], "constructible_class"],
        "arg_ranges": [
            {"action": "constructible_class", "arg": 0, "min": 1, "max": 3, "severity": "error"}
        ],
    }
    monkeypatch.setattr(profiles, "load", lambda _pid: {"mapscript": spec})
    text = block("game_manager", ("spawn", ["constructible_class 5"])) + block(
        "dead", ("spawn", ["constructible_class 0"])
    )
    found = {
        f.severity
        for f in mapscript.check("test", [ent(0, "manager")], tmp_path / "m.map", text=text)
        if f.code == "MAPSCRIPT_ARGUMENT_RANGE"
    }
    assert found == {"error", "warning"}, "a dead block is reported one step milder"


def test_a_required_action_is_asked_for_only_in_a_running_block(tmp_path, monkeypatch):
    spec = {
        **SPEC,
        "required_actions": [{"block": "game_manager", "event": "spawn", "action": "setautospawn"}],
    }
    monkeypatch.setattr(profiles, "load", lambda _pid: {"mapscript": spec})
    without = block("game_manager", ("spawn", ["wait 1"]))
    codes = [
        f.code
        for f in mapscript.check("test", [ent(0, "manager")], tmp_path / "m.map", text=without)
    ]
    assert codes == ["MAPSCRIPT_REQUIRED_ACTION"]
    assert not mapscript.check("test", [], tmp_path / "m.map", text=without) or all(
        f.code != "MAPSCRIPT_REQUIRED_ACTION"
        for f in mapscript.check("test", [], tmp_path / "m.map", text=without)
    ), "no manager entity, no running block, nothing asked"


def test_an_objective_requirement_is_met_by_a_key_or_by_its_own_script(tmp_path, monkeypatch):
    spec = {
        **SPEC,
        "actions": [*SPEC["actions"], "constructible_class"],
        "objective_requirements": [
            {
                "classname": "boom",
                "targeted_by": {"classname": "trig", "key": "target", "flags_any": [1, 2]},
                "key_min": {"cls": 2},
                "action": "constructible_class",
                "action_min": 2,
                "severity": "warning",
            }
        ],
    }
    monkeypatch.setattr(profiles, "load", lambda _pid: {"mapscript": spec})
    trig = ent(0, "trig", target="wall", spawnflags="2")

    def unmet(boom, text=""):
        return [
            f.code
            for f in mapscript.check("test", [trig, boom], tmp_path / "m.map", text=text)
            if f.code == "MAPSCRIPT_OBJECTIVE_UNMET"
        ]

    assert unmet(ent(1, "boom", targetname="wall")) == ["MAPSCRIPT_OBJECTIVE_UNMET"]
    assert unmet(ent(1, "boom", targetname="wall", cls="3")) == []
    scripted = block("wall", ("spawn", ["constructible_class 3"]))
    assert unmet(ent(1, "boom", targetname="wall", scriptname="wall"), scripted) == []
    too_low = block("wall", ("spawn", ["constructible_class 1"]))
    assert unmet(ent(1, "boom", targetname="wall", scriptname="wall"), too_low)


def test_a_reference_can_skip_the_old_numeric_syntax(tmp_path, monkeypatch):
    """wm_set_main_objective took a number before it took a name; the engine ignores numbers."""
    spec = {
        **SPEC,
        "actions": [*SPEC["actions"], "main"],
        "references": [
            {
                "action": "main",
                "arg": 0,
                "resolves": [{"key": "target", "classname": "trig"}],
                "severity": "warning",
                "skip_numeric": True,
            }
        ],
    }
    monkeypatch.setattr(profiles, "load", lambda _pid: {"mapscript": spec})
    text = block("game_manager", ("spawn", ["main 1 0", "main nowhere 0"]))
    found = [
        f.message
        for f in mapscript.check("test", [ent(0, "manager")], tmp_path / "m.map", text=text)
        if f.code == "MAPSCRIPT_UNRESOLVED_REFERENCE"
    ]
    assert len(found) == 1 and "nowhere" in found[0]


# ---------------------------------------------------------------------------
# The engine's tokenizer, quirk by quirk (qcommon/q_shared.c COM_ParseExt, g_script.c)
# ---------------------------------------------------------------------------

ETL_LIKE = {
    **SPEC,
    "brace_actions": ["set", "create", "delete"],
    "tokenizer": {"high_bytes": "whitespace"},
    "actions": [*SPEC["actions"], "set", "delete", "print", "wm_announce"],
}


def _acts(text: str, spec: dict = ETL_LIKE) -> list[tuple[str, list[str]]]:
    return [
        (a.name, a.args)
        for b in mapscript.parse_spec(text, spec)
        for e in b.events
        for a in e.actions
    ]


def test_a_lone_carriage_return_is_no_line_break_so_arguments_run_on():
    crlf = "a\r\n{\r\n spawn\r\n {\r\n  wait 5\r\n  wait 6\r\n }\r\n}\r\n"
    assert _acts(crlf) == [("wait", ["5"]), ("wait", ["6"])]
    with pytest.raises(mapscript.ScriptSyntaxError, match="end of script"):
        mapscript.parse_spec(crlf.replace("\r\n", "\r"), ETL_LIKE)


def test_a_line_break_inside_a_comment_or_a_quote_does_not_end_the_arguments():
    text = 'a\n{\n spawn\n {\n  wait 5 /* one\n two */ 6\n  wm_announce "x\ny" z\n }\n}\n'
    assert _acts(text) == [("wait", ["5", "6"]), ("wm_announce", ["x\ny", "z"])]


def test_an_empty_quoted_token_ends_the_arguments_and_reads_as_the_end_elsewhere():
    """In the running block `wait "" 5` is a wait and an action named 5; every entity that
    skips the block reads the "" as the end of the script."""
    running, _ = mapscript._read_block(
        mapscript.tokenize('a\n{\n spawn\n {\n  wait "" 5\n }\n}\n'), 0, 0, frozenset()
    )
    assert [(x.name, x.args) for x in running.events[0].actions] == [("wait", []), ("5", [])]
    for text in ('""\na\n{\n}\n', 'a\n{\n spawn ""\n {\n }\n}\n', 'a\n{\n e\n {\n  w ""\n }\n}\n'):
        with pytest.raises(mapscript.ScriptSyntaxError, match='empty ""'):
            mapscript.parse_spec(text, ETL_LIKE)


def test_bytes_past_ascii_split_words_on_a_signed_char_build_but_not_inside_quotes():
    word = "wait\xe95"
    assert [t.text for t in mapscript.tokenize(word, high_bytes_split=True)] == ["wait", "5"]
    assert [t.text for t in mapscript.tokenize(word)] == [word]
    assert [t.text for t in mapscript.tokenize('"a\xe9b"', high_bytes_split=True)] == ["a\xe9b"]


def test_a_backslash_quote_is_the_engines_nested_string():
    (t,) = mapscript.tokenize(r'"say \"hi\" now"')
    assert t.text == 'say "hi" now'


def test_any_number_of_entity_keywords_may_come_before_a_name():
    (b,) = mapscript.parse("entity Entity gate\n{\n}\n")
    assert b.name == "gate"


def test_a_brace_on_an_action_line_reads_differently_when_the_block_is_skipped():
    """Run, `wait 5 }` is a wait with two arguments; skipped, the `}` ends the event."""
    text = "a\n{\n spawn\n {\n  wait 5 }\n }\n}\nb\n{\n}\n"
    running, _ = mapscript._read_block(mapscript.tokenize(text), 0, 0, frozenset())
    assert running.events[0].actions[0].args == ["5", "}"]
    with pytest.raises(mapscript.ScriptSyntaxError, match="not expected"):
        mapscript.parse(text)
    # Where the skipping reading still makes sense of the rest, the two readings disagree:
    # skipped, the quoted "{x" opens a level and `b` reads as a second event of block a.
    text = 'a\n{\n spawn\n {\n  print "{x"\n }\n}\nb\n{\n}\n}\n'
    with pytest.raises(mapscript.ScriptSyntaxError, match="when it runs"):
        mapscript.parse(text)


# ---------------------------------------------------------------------------
# Edits that used to be written and then broke the file (audit S-F1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("block_name", "params", "actions"),
    [
        ("gate", [], ["set {targetname foo}"]),
        ("gate", [], ['set { "a" "}b" }']),
        ("gate", [], ["set { a b // c }"]),
        ("gate", [], ["wait 50 /*"]),
        ("gate", [], ["wait 50 /* note */"]),
        ("gate", ["/*x"], ["wait 50"]),
        ("entity", [], ["wait 50"]),
        ("gate", [], ["set { a } wait 5 }"]),
        ("gate", [], ['wait ""']),
        ("gate", [], [r'print "a \"b\" c"']),
        ("gate", [], ["wm_announce café"]),
        ("gate", [], ['wm_announce "open']),
        ("café", [], ["wait 50"]),
    ],
)
def test_an_edit_that_would_break_out_of_its_event_is_refused(block_name, params, actions):
    with pytest.raises(mapscript.ScriptEditError):
        mapscript.set_event(EDITABLE, ETL_LIKE, block_name, "trigger", params, actions)


def test_an_edit_is_parsed_again_and_must_read_as_asked(monkeypatch):
    """The last line of defence, past every per-line check: a re-parse that disagrees."""

    def lenient(line, _spec):
        return mapscript.Action(line.split()[0], line.split()[1:], 0)

    monkeypatch.setattr(mapscript, "_line_action", lenient)
    with pytest.raises(mapscript.ScriptEditError, match="differently|could not read"):
        mapscript.set_event(EDITABLE, ETL_LIKE, "gate", "trigger", ["x"], ["wait 5 }"])


def test_text_past_ascii_is_written_as_utf8_inside_quotes():
    new, _ = mapscript.set_event("", ETL_LIKE, "gm", "spawn", [], ['wm_announce "Café"'])
    assert mapscript.encode(new).count("Café".encode()) == 1
    (action,) = mapscript.parse_spec(new, ETL_LIKE)[0].events[0].actions
    assert mapscript.to_user(action.args[0]) == "Café"


RAW = (
    b"// caf\xc3\xa9 \x81 notes\r\ngame_manager\r\n{\r\n\tspawn\r\n\t{\r\n\t\twait 50\r\n"
    b'\t\twm_announce "Caf\xc3\xa9"\r\n\t}\r\n}\r\n\r\ngate\r\n{\r\n\tdeath\r\n\t{\r\n'
    b"\t\twait 10\r\n\t}\r\n}\r\n"
)


def test_an_edit_keeps_every_byte_it_does_not_touch():
    """CRLF line ends, UTF-8 and an undecodable byte all come back as they were (audit S-F2)."""
    text = mapscript.decode(RAW)
    same, did = mapscript.set_event(
        text, ETL_LIKE, "game_manager", "spawn", [], ["wait 50", 'wm_announce "Café"']
    )
    assert did == "unchanged" and mapscript.encode(same) == RAW
    new, did = mapscript.set_event(text, ETL_LIKE, "gate", "death", [], ["wait 20"])
    assert did == "replaced event"
    assert mapscript.encode(new) == RAW.replace(b"wait 10", b"wait 20")
    added, _ = mapscript.set_event(text, ETL_LIKE, "gate", "trigger", ["open"], ["wait 1"])
    assert b"\n" not in mapscript.encode(added).replace(b"\r\n", b""), "new lines match CRLF"
    gone = mapscript.remove(text, ETL_LIKE, "gate")
    assert mapscript.encode(gone) == RAW[: RAW.index(b"\r\n\r\ngate")] + b"\r\n"


def test_every_action_shown_is_a_line_set_event_takes_back():
    """script_show's text for an action parses back to that action (audit S-F6)."""
    text = (
        'a\n{\n spawn\n {\n  wm_announce "Axis Bunker" 0\n  set { "origin" "0 0 1" }\n'
        '  print "//y" "a\xc3\xa9" "/*z"\n  delete { a }\n }\n}\n'
    )
    braced = mapscript.brace_actions(ETL_LIKE)
    actions = [a for b in mapscript.parse_spec(text, ETL_LIKE) for e in b.events for a in e.actions]
    assert len(actions) == 4
    for a in actions:
        line = mapscript.action_text(a, braced)
        new, _ = mapscript.set_event("", ETL_LIKE, "a", "spawn", [], [mapscript.to_user(line)])
        (again,) = mapscript.parse_spec(new, ETL_LIKE)[0].events[0].actions
        assert (again.name, again.args) == (a.name, a.args), line


def test_removing_a_block_removes_its_dead_copies_too():
    """Removing only the first would make a forgotten copy the one that runs (audit S-F5)."""
    text = EDITABLE + "\ngate\n{\n\tspawn\n\t{\n\t\twait 1\n\t}\n}\n"
    out = mapscript.remove(text, ETL_LIKE, "gate")
    assert [b.name for b in mapscript.parse(out)] == ["game_manager"]


def test_a_create_in_a_block_that_never_runs_spawns_nothing(tmp_path, spec):
    """Only a live first-of-name block spawns, so a dead one cannot hide an error (S-F4)."""
    made = 'create { "scriptname" "made" "targetname" "lever" }'
    dead = block("nobody", ("spawn", [made])) + block(
        "game_manager", ("spawn", ["alertentity lever"])
    )
    assert "MAPSCRIPT_UNRESOLVED_REFERENCE" in check(tmp_path, dead, [ent(0, "manager")])
    dup = block("game_manager", ("spawn", ["alertentity lever"])) + block(
        "game_manager", ("spawn", [made])
    )
    assert "MAPSCRIPT_UNRESOLVED_REFERENCE" in check(tmp_path, dup, [ent(0, "manager")])
    live = block("game_manager", ("spawn", [made, "alertentity lever"]))
    assert "MAPSCRIPT_UNRESOLVED_REFERENCE" not in check(tmp_path, live, [ent(0, "manager")])
    # A spawned entity's own block runs, and what it spawns is real too: a fixed point.
    chain = block("game_manager", ("spawn", [made])) + block(
        "made", ("spawn", ['create { "targetname" "far" }', "alertentity far"])
    )
    assert "MAPSCRIPT_UNRESOLVED_REFERENCE" not in check(tmp_path, chain, [ent(0, "manager")])


def test_trigger_self_needs_the_event_in_the_running_block(tmp_path, monkeypatch):
    """`trigger self open` runs the caller's own `trigger open` (audit S-F10)."""
    refs = [dict(SPEC["references"][0], self=["self"]), *SPEC["references"][1:]]
    spec = {**SPEC, "references": refs}
    monkeypatch.setattr(profiles, "load", lambda _pid: {"mapscript": spec})
    missing = block("game_manager", ("spawn", ["trigger self open"]))
    assert "MAPSCRIPT_MISSING_EVENT" in check(tmp_path, missing, [ent(0, "manager")])
    there = block("game_manager", ("spawn", ["trigger self open"]), ("trigger open", ["wait 1"]))
    assert "MAPSCRIPT_MISSING_EVENT" not in check(tmp_path, there, [ent(0, "manager")])


def test_the_shipped_profile_reads_the_script_as_the_engine_does():
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    spec = profiles.load("etl")["mapscript"]
    assert mapscript.high_bytes_split(spec)
    trig = next(r for r in spec["references"] if r["action"] == "trigger")
    assert trig.get("self") == ["self"]
