"""Placing point entities and setting keys on any entity, through the tool surface.

Before these tools a playable map meant hand-editing the `.map` for every spawn point,
script entity and trigger key. Every test runs once per profile on disk, and every classname
and key it uses is read from that profile — the tools are game-agnostic, so the tests that
prove it have to be as well.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel, profiles
from nrc_mcp import server as srv

ROOM = Path("bench/tapes/fixtures/interior_room.map")

#: Inside the room fixture: the floor top is z=16, the walls are 16 thick.
INSIDE = [256, 256, 40]

#: Keys every entity format has, which profile entries do not repeat.
GENERIC = {"classname", "origin", "spawnflags"}


@pytest.fixture(autouse=True)
def _kernel_and_clean_session():
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    yield
    srv.SESSION.map = None
    srv.SESSION.path = None
    srv.SESSION.warnings = []
    srv.SESSION.opened_round_trip = None


@pytest.fixture(params=profiles.available() or ["(none)"])
def pid(request, monkeypatch) -> str:
    """Each profile on disk in turn, made the active one."""
    if request.param == "(none)":
        pytest.skip("no profile on disk")
    monkeypatch.setenv("NRC_PROFILE", request.param)
    return request.param


@pytest.fixture
def room(tmp_path: Path) -> Path:
    root = Path(__file__).resolve().parents[2]
    dst = tmp_path / "work.map"
    shutil.copyfile(root / ROOM, dst)
    dst.chmod(0o644)
    srv.map_open(str(dst))
    return dst


def _own_keys(pid: str, ent: dict) -> list[str]:
    return [k for k in profiles.declared_keys(pid, ent) if k not in GENERIC]


def _point(pid: str) -> dict:
    """A verified point entity that declares at least one key of its own."""
    for e in profiles.verified_entities(pid):
        if e.get("kind") == "point" and _own_keys(pid, e):
            return e
    pytest.skip(f"profile {pid} declares no point entity with keys")


def _brush(pid: str) -> dict:
    """A verified brush entity, other than the world, that declares a key of its own."""
    for e in profiles.verified_entities(pid):
        if e.get("kind") == "brush" and e["classname"] != "worldspawn" and _own_keys(pid, e):
            return e
    pytest.skip(f"profile {pid} declares no brush entity with keys")


def _keys(index: int) -> list[tuple[str, str]]:
    return [tuple(kv) for kv in srv.query_entities()["entities"][index]["keys"]]


# ---------------------------------------------------------------------------
# entity_place
# ---------------------------------------------------------------------------


def test_a_placed_entity_is_in_the_map_and_saves_where_it_was_opened(pid, room):
    ent = _point(pid)
    key = _own_keys(pid, ent)[0]
    before = srv.query_entities()["count"]

    out = srv.entity_place(ent["classname"], INSIDE, keys={key: "1"})

    assert out["entity"] == before
    assert out["notes"] == [], "a declared classname and key must not draw a note"
    assert _keys(before) == [("classname", ent["classname"]), ("origin", "256 256 40"), (key, "1")]

    # No path: the map has to remember where it came from, and know it was edited.
    saved = srv.map_save()
    assert saved["path"] == str(room)
    assert saved["modified_since_open"] is True
    srv.map_open(str(room))
    assert _keys(before)[0] == ("classname", ent["classname"])


def test_an_unknown_key_or_classname_is_a_note_not_a_refusal(pid, room):
    ent = _point(pid)
    out = srv.entity_place(ent["classname"], INSIDE, keys={"nrc_no_such_key": "1"})
    assert ("nrc_no_such_key", "1") in _keys(out["entity"]), "the key is written anyway"
    assert len(out["notes"]) == 1
    assert "nrc_no_such_key" in out["notes"][0]
    assert _own_keys(pid, ent)[0] in out["notes"][0], "the note lists what the profile declares"

    out = srv.entity_place("nrc_no_such_class", INSIDE)
    assert _keys(out["entity"])[0] == ("classname", "nrc_no_such_class")
    assert "nrc_no_such_class" in out["notes"][0]


def test_keys_from_a_shared_key_block_are_known(pid, room):
    for e in profiles.verified_entities(pid):
        blocks = e.get("key_blocks") or []
        block = (profiles.load(pid).get("key_blocks") or {}).get(blocks[0]) if blocks else None
        if e.get("kind") == "point" and block and block.get("keys"):
            key = block["keys"][0]["name"]
            out = srv.entity_place(e["classname"], INSIDE, keys={key: "1"})
            assert out["notes"] == [], f"{key} comes from key block {blocks[0]}"
            return
    pytest.skip(f"profile {pid} has no point entity using a key block")


def test_a_value_outside_the_declared_list_is_noted(pid, room):
    for e in profiles.verified_entities(pid):
        if e.get("kind") != "point":
            continue
        for name, k in profiles.declared_keys(pid, e).items():
            if k.get("values"):
                ok = srv.entity_place(e["classname"], INSIDE, keys={name: str(k["values"][0])})
                assert ok["notes"] == []
                bad = srv.entity_place(e["classname"], INSIDE, keys={name: "nrc_not_listed"})
                assert any("nrc_not_listed" in n for n in bad["notes"])
                return
    pytest.skip(f"profile {pid} lists values for no point-entity key")


def test_a_brush_class_is_refused_as_a_point_entity(pid, room):
    before = srv.query_entities()["count"]
    with pytest.raises(ToolError, match="solid_commit"):
        srv.entity_place(_brush(pid)["classname"], INSIDE)
    assert srv.query_entities()["count"] == before


def test_numbers_and_vectors_are_spelt_the_way_a_map_spells_them(pid, room):
    out = srv.entity_place(
        _point(pid)["classname"],
        [64.0, -32, 1234.5678],
        keys={"angles": [0, 90.0, 0], "nrc_flag": True, "nrc_scale": 0.1},
    )
    keys = dict(_keys(out["entity"]))
    assert keys["origin"] == "64 -32 1234.5678", "a coordinate must not be rounded"
    assert keys["angles"] == "0 90 0"
    assert keys["nrc_flag"] == "1"
    assert keys["nrc_scale"] == "0.1"


def test_an_off_grid_origin_is_noted_and_never_snapped(pid, room):
    out = srv.entity_place(_point(pid)["classname"], [252, 256, 40])
    assert dict(_keys(out["entity"]))["origin"] == "252 256 40"
    assert any("off the 8-unit grid" in n for n in out["notes"])
    assert not any("grid" in n for n in srv.entity_place(_point(pid)["classname"], INSIDE)["notes"])


def test_classname_and_origin_belong_in_their_own_arguments(pid, room):
    with pytest.raises(ToolError, match="own arguments"):
        srv.entity_place(_point(pid)["classname"], INSIDE, keys={"origin": "0 0 0"})


def test_a_value_the_format_cannot_hold_is_refused_before_anything_is_written(pid, room):
    before = srv.query_entities()["count"]
    with pytest.raises(ToolError, match="no escapes"):
        srv.entity_place(_point(pid)["classname"], INSIDE, keys={"nrc_text": 'say "hi"'})
    with pytest.raises(ToolError, match="worldspawn"):
        srv.entity_place("worldspawn", INSIDE)
    assert srv.query_entities()["count"] == before


def test_without_a_profile_nothing_is_checked_and_that_is_said(pid, room, monkeypatch):
    monkeypatch.delenv("NRC_PROFILE")
    out = srv.entity_place("nrc_no_such_class", INSIDE)
    assert out["notes"] == [
        "no profile is selected (NRC_PROFILE), so the classname and keys were not checked"
    ]


def test_model_place_goes_through_the_same_path_and_stays_saveable(pid, room):
    """It used to re-parse the map from a string, which forgot the path it saves to."""
    out = srv.model_place("models/nrc/crate.md3", INSIDE)
    assert out["entity"] == out["entities_now"] - 1
    saved = srv.map_save()
    assert saved["path"] == str(room)
    assert saved["modified_since_open"] is True


# ---------------------------------------------------------------------------
# entity_set_keys and entity_remove_keys
# ---------------------------------------------------------------------------


def test_keys_can_be_set_on_a_brush_entity_built_by_solid_commit(pid, room):
    ent = _brush(pid)
    key = _own_keys(pid, ent)[0]
    srv.solid_commit(
        {"op": "box", "min": [64, 64, 16], "max": [128, 128, 96]},
        label="volume",
        target_classname=ent["classname"],
    )
    index = srv.query_entities(classname=ent["classname"])["entities"][0]["index"]

    first = srv.entity_set_keys(index, {key: "a"})
    assert first["classname"] == ent["classname"]
    assert first["changed"] == [{"key": key, "value": "a", "previous": None}]
    assert first["notes"] == []

    again = srv.entity_set_keys(index, {key: "b"})
    assert again["changed"][0]["previous"] == "a", "an overwrite reports what it replaced"
    assert _keys(index) == [("classname", ent["classname"]), (key, "b")]


def test_keys_can_be_set_on_the_worldspawn(pid, room):
    world = profiles.entity_def(pid, "worldspawn")
    if world is None or not _own_keys(pid, world):
        pytest.skip(f"profile {pid} declares no worldspawn keys")
    key = _own_keys(pid, world)[0]
    out = srv.entity_set_keys(0, {key: "1", "nrc_no_such_key": "1"})
    assert [c["key"] for c in out["changed"]] == [key, "nrc_no_such_key"]
    assert len(out["notes"]) == 1 and "nrc_no_such_key" in out["notes"][0]
    assert _keys(0)[1:] == [(key, "1"), ("nrc_no_such_key", "1")]


def test_setting_a_key_removes_the_later_duplicate_the_engine_would_read(pid, tmp_path):
    cn = _point(pid)["classname"]
    path = tmp_path / "dup.map"
    path.write_text(
        '{\n"classname" "worldspawn"\n}\n'
        f'{{\n"classname" "{cn}"\n"angle" "90"\n"origin" "0 0 0"\n"angle" "180"\n}}\n'
    )
    srv.map_open(str(path))
    out = srv.entity_set_keys(1, {"angle": "270"})
    assert out["changed"][0]["previous"] == "180", "the last copy is the one that was read"
    assert out["duplicates_removed"] == {"angle": 1}
    assert _keys(1) == [("classname", cn), ("angle", "270"), ("origin", "0 0 0")]


def test_a_refused_set_changes_nothing(pid, room):
    before = _keys(0)
    with pytest.raises(ToolError, match="no escapes"):
        srv.entity_set_keys(0, {"nrc_fine": "1", "nrc_broken": "two\nlines"})
    assert _keys(0) == before, "the valid key must not have been applied either"

    with pytest.raises(ToolError, match="worldspawn"):
        srv.entity_set_keys(0, {"classname": "nrc_something_else"})
    point = srv.entity_place(_point(pid)["classname"], INSIDE)["entity"]
    with pytest.raises(ToolError, match="second worldspawn"):
        srv.entity_set_keys(point, {"classname": "worldspawn"})


def test_an_entity_that_does_not_exist_is_named(pid, room):
    count = srv.query_entities()["count"]
    with pytest.raises(ToolError, match="query_entities"):
        srv.entity_set_keys(count, {"nrc_key": "1"})
    with pytest.raises(ToolError, match="query_entities"):
        srv.entity_remove_keys(-1, ["nrc_key"])


def test_the_classname_can_change_but_not_disappear(pid, room):
    point = srv.entity_place("nrc_no_such_class", INSIDE)["entity"]
    out = srv.entity_set_keys(point, {"classname": _point(pid)["classname"]})
    assert out["notes"] == [], "the notes describe the classname it has now"
    with pytest.raises(ToolError, match="classname"):
        srv.entity_remove_keys(point, ["classname"])


def test_removing_keys_reports_what_was_there(pid, room):
    point = srv.entity_place(_point(pid)["classname"], INSIDE, keys={"nrc_a": "1", "nrc_b": "2"})
    out = srv.entity_remove_keys(point["entity"], ["nrc_a", "nrc_missing"])
    assert out["removed"] == ["nrc_a"]
    assert out["absent"] == ["nrc_missing"]
    assert [k for k, _ in _keys(point["entity"])] == ["classname", "origin", "nrc_b"]
    # Safe to repeat: the second time it is simply absent.
    assert srv.entity_remove_keys(point["entity"], ["nrc_a"])["absent"] == ["nrc_a"]


def test_a_key_block_that_applies_to_all_counts_for_every_entity(monkeypatch):
    """Keys the engine parses for any classname, in one block nobody has to name."""
    data = {
        "key_blocks": {
            "everywhere": {
                "applies_to_all": True,
                "case_insensitive": True,
                "keys": [{"name": "scriptName", "type": "string"}, {"name": "angle"}],
            },
            "named_only": {"keys": [{"name": "_cs"}]},
            "flag_is_not_true": {"applies_to_all": "yes", "keys": [{"name": "nope"}]},
        },
        "entities": [{"classname": "spawn", "keys": [{"name": "angle", "type": "own"}]}],
    }
    monkeypatch.setattr(profiles, "load", lambda _pid: data)
    declared = profiles.declared_keys("test", profiles.entity_def("test", "spawn"))
    assert {"scriptName", "scriptname", "angle"} <= set(declared)
    assert declared["angle"]["type"] == "own", "a class's own declaration wins"
    assert "_cs" not in declared, "an ordinary block still has to be named"
    assert "nope" not in declared, "only applies_to_all: true counts"
    data["key_blocks"]["everywhere"]["case_insensitive"] = False
    assert "scriptname" not in profiles.declared_keys("test", profiles.entity_def("test", "spawn"))


def test_no_urt43_entity_gains_keys_from_an_applies_to_all_block():
    if "urt43" not in profiles.available():
        pytest.skip("no urt43 profile on disk")
    blocks = profiles.load("urt43").get("key_blocks") or {}
    assert not any(b.get("applies_to_all") for b in blocks.values() if isinstance(b, dict))


# ---------------------------------------------------------------------------
# map_new
# ---------------------------------------------------------------------------


def test_map_new_creates_a_worldspawn_only_map_and_opens_it(tmp_path):
    path = tmp_path / "fresh" / "m.map"
    r = srv.map_new(str(path))
    assert r["created"] is True
    assert path.read_text() == '{\n"classname" "worldspawn"\n}\n'
    assert r["round_trip"]["identical"], "what it writes is what the kernel writes back"
    assert srv.SESSION.path == path
    assert [e["classname"] for e in srv.query_entities()["entities"]] == ["worldspawn"]


def test_map_new_never_overwrites(tmp_path):
    path = tmp_path / "taken.map"
    path.write_text("// somebody's work\n")
    with pytest.raises(ToolError, match="already exists"):
        srv.map_new(str(path))
    assert path.read_text() == "// somebody's work\n"
    assert srv.SESSION.path is None, "nothing was opened"
    with pytest.raises(ToolError, match=r"\.map"):
        srv.map_new(str(tmp_path / "notes.txt"))


def test_a_map_built_from_map_new_saves_where_it_was_created(pid, tmp_path):
    path = tmp_path / "built.map"
    srv.map_new(str(path))
    srv.solid_commit(
        {
            "op": "hollow",
            "solid": {"op": "box", "min": [0, 0, 0], "max": [512, 512, 256]},
            "thickness": 16,
        },
        label="room",
    )
    point = _point(pid)
    srv.entity_place(point["classname"], INSIDE)
    saved = srv.map_save(allow_non_identical=True)
    assert saved["written"]
    assert [e["classname"] for e in kernel.load_map(path).entities()][-1] == point["classname"]
