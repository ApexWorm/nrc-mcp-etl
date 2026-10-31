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
    found = mapscript.check(pid, entities, tmp_path / "m.map")
    assert [(f.code, f.severity) for f in found] == [("MAPSCRIPT_UNRESOLVED_REFERENCE", "error")]
    assert "missing_thing" in found[0].message
