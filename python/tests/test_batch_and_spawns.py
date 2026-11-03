"""Batch placement (`entities_place`, `solids_commit`) and `spawn_cluster`.

The batch tools exist to save calls, so the property that matters most is the one a loop of
single calls does not have: all or nothing. A refused item must leave the map, and the
solids sidecar, exactly as they were.

`spawn_cluster` is checked against a room built here with known floors, a crate and a low
ceiling, so every rejection reason has a position that should produce it. Classnames and
flags are read back from the profile, never restated.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel, profiles, spawns
from nrc_mcp import server as srv


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
    srv.SESSION.pending_files = {}
    srv.SESSION.baseline_files = {}
    srv.SESSION.grids = {}


def _box(lo, hi):
    return {"op": "box", "min": lo, "max": hi}


@pytest.fixture
def room(tmp_path, monkeypatch):
    """A sealed 1024 x 768 room, floor top at z=0, ceiling at z=256.

    A crate 64 high at x 384..512, y 384..512; a low shelf (bottom at z=48) over
    x 640..768, y 64..192, too low to stand under.
    """
    monkeypatch.setenv("NRC_PROFILE", "etl")
    if "etl" not in profiles.available():
        pytest.skip("etl profile not on disk")
    srv.map_new(str(tmp_path / "room.map"))
    srv.solids_commit(
        solids=[
            {
                "ir": {
                    "op": "hollow",
                    "solid": _box([-32, -32, -32], [1056, 800, 288]),
                    "thickness": 32,
                },
                "label": "shell",
            },
            {"ir": _box([384, 384, 0], [512, 512, 64]), "label": "crate"},
            {"ir": _box([640, 64, 48], [768, 192, 64]), "label": "shelf"},
        ]
    )
    return tmp_path / "room.map"


def _count() -> int:
    return srv.query_entities(with_keys=False)["count"]


# ---------------------------------------------------------------------------
# entities_place
# ---------------------------------------------------------------------------


def test_entities_place_adds_every_item_in_order(room):
    before = _count()
    out = srv.entities_place(
        entities=[
            {"classname": "light", "origin": [64 * i, 256, 200], "keys": {"light": 300}}
            for i in range(1, 5)
        ]
    )
    assert out["placed"] == 4
    assert out["entities"] == {"first": before, "last": before + 3}
    assert out["classnames"] == {"light": 4}
    assert out["notes"] == []
    ents = srv.query_entities(classname="light")["entities"]
    assert [dict(e["keys"])["origin"] for e in ents] == [f"{64 * i} 256 200" for i in range(1, 5)]


def test_entities_place_groups_a_repeated_note(room):
    out = srv.entities_place(
        entities=[
            {"classname": "light", "origin": [100, 256, 200], "keys": {"nrc_no_such_key": 1}},
            {"classname": "light", "origin": [102, 256, 200], "keys": {"nrc_no_such_key": 1}},
            {"classname": "light", "origin": [128, 256, 200]},
        ]
    )
    by_text = {n["note"]: n["items"] for n in out["notes"]}
    assert len(by_text) == 2, by_text
    assert [0, 1] in by_text.values(), "one note for both unknown keys"
    assert any("off the 8-unit grid" in t and v == [0, 1] for t, v in by_text.items())


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ({"classname": "light", "origin": [0, 0]}, r"entities\[1\]: origin must be three"),
        ({"classname": "func_door", "origin": [0, 0, 0]}, r"entities\[1\]: .*brush entity"),
        ({"classname": "light", "origin": [0, 0, 0], "angle": 90}, r"entities\[1\]: unknown"),
        ({"origin": [0, 0, 0]}, r"entities\[1\]: classname"),
        ({"classname": "light", "origin": [0, 0, 0], "keys": {"origin": "1 2 3"}}, "own arguments"),
    ],
)
def test_entities_place_refuses_everything_for_one_bad_item(room, bad, match):
    before = _count()
    source = srv.SESSION.map.source()
    good = {"classname": "light", "origin": [64, 64, 64]}
    with pytest.raises(ToolError, match=match):
        srv.entities_place(entities=[good, bad, good])
    assert _count() == before
    assert srv.SESSION.map.source() == source


# ---------------------------------------------------------------------------
# solids_commit
# ---------------------------------------------------------------------------


def test_solids_commit_records_each_solid_for_later_editing(room):
    out = srv.solids_commit(
        solids=[
            {"ir": _box([64, 512, 0], [128, 576, 32]), "label": "step_a"},
            {
                "ir": _box([192, 512, 0], [256, 576, 32]),
                "label": "step_b",
                "textures": {"default": "common/caulk", "detail": True},
            },
        ],
        textures={"default": "common/caulk"},
    )
    assert out["committed"] == 2
    assert [s["label"] for s in out["solids"]] == ["step_a", "step_b"]
    assert out["brushes_created"] == 2
    recorded = srv.solid_list()
    names = {s["name"] if isinstance(s, dict) else s for s in recorded["solids"]}
    assert {"step_a", "step_b"} <= names
    srv.map_save()


def test_solids_commit_is_all_or_nothing_sidecar_included(room):
    source = srv.SESSION.map.source()
    staged = dict(srv.SESSION.pending_files)
    with pytest.raises(ToolError, match=r"solids\[1\]"):
        srv.solids_commit(
            solids=[
                {"ir": _box([64, 512, 0], [128, 576, 32]), "label": "fine"},
                {"ir": {"op": "no_such_op"}, "label": "broken"},
            ]
        )
    assert srv.SESSION.map.source() == source
    assert srv.SESSION.pending_files == staged


def test_solids_commit_refuses_a_label_twice_or_one_already_used(room):
    with pytest.raises(ToolError, match="used twice"):
        srv.solids_commit(
            solids=[
                {"ir": _box([64, 512, 0], [128, 576, 32]), "label": "dup"},
                {"ir": _box([192, 512, 0], [256, 576, 32]), "label": "dup"},
            ]
        )
    with pytest.raises(ToolError, match="already exists"):
        srv.solids_commit(solids=[{"ir": _box([64, 512, 0], [128, 576, 32]), "label": "crate"}])


# ---------------------------------------------------------------------------
# spawn_cluster
# ---------------------------------------------------------------------------


def _section() -> dict:
    return spawns.recipe("etl")


def test_spawn_cluster_places_standing_spawns_and_a_marker_from_the_profile(room):
    spec = _section()
    out = srv.spawn_cluster(
        team="blue",
        count=6,
        area={"min": [64, 64, 0], "max": [192, 704, 64]},
        yaw=90,
        label="Loading Bay",
    )
    spawn = spec["spawn"]
    assert out["spawns"]["classname"] == spawn["classname"]["blue"]
    origins = [[float(c) for c in o.split()] for o in out["spawns"]["origins"]]
    assert len(origins) == 6
    assert all(o[2] == spawn["origin_height"] for o in origins), "standing on the z=0 floor"
    for i, a in enumerate(origins):
        for b in origins[i + 1 :]:
            assert max(abs(a[0] - b[0]), abs(a[1] - b[1])) >= spec["default_spacing"]
    keys = out["spawns"]["keys"]
    assert keys[spawn["facing_key"]] == "90"
    assert keys["spawnflags"] == str(spawn["spawnflags"]["active"])

    marker = srv.query_entities(classname=spec["marker"]["classname"])["entities"]
    assert len(marker) == 1
    mkeys = dict(marker[0]["keys"])
    assert mkeys[spec["marker"]["label_key"]] == "Loading Bay"
    assert mkeys["spawnflags"] == str(spec["marker"]["spawnflags"]["blue"])

    # The analysis measures from them: every spawn is on the walkable grid.
    safety = srv.spawn_safety(cell=16)
    assert all(row["on_walkable_grid"] for row in safety["spawns"])
    srv.map_save()


def test_spawn_cluster_stands_on_the_crate_not_inside_it(room):
    out = srv.spawn_cluster(
        team="red", count=4, area={"min": [384, 384, 0], "max": [512, 512, 128]}, label="Crate"
    )
    height = _section()["spawn"]["origin_height"]
    assert all(float(o.split()[2]) == 64 + height for o in out["spawns"]["origins"])


def test_spawn_cluster_names_a_group_a_script_can_toggle(room):
    spec = _section()
    out = srv.spawn_cluster(
        team="axis",
        count=2,
        area={"min": [832, 320, 0], "max": [960, 448, 64]},
        label="Forward Bunker",
        group="fwd_bunker",
        active=False,
    )
    expected_spawn = spawns.names(spec["spawn"], "fwd_bunker")
    expected_marker = spawns.names(spec["marker"], "fwd_bunker")
    assert out["group"]["spawns"] == expected_spawn
    assert out["group"]["marker"] == expected_marker
    key = spec["toggle_key"]
    assert out["group"]["toggle"]["spawns"] == spec["toggle_action"].format(
        name=expected_spawn[key]
    )
    assert out["spawns"]["keys"]["spawnflags"] == str(spec["spawn"]["spawnflags"]["inactive"])
    for k, v in expected_spawn.items():
        assert out["spawns"]["keys"][k] == v

    with pytest.raises(ToolError, match="already uses the name"):
        srv.spawn_cluster(
            team="axis",
            count=1,
            area={"min": [832, 512, 0], "max": [960, 640, 64]},
            label="Again",
            group="fwd_bunker",
        )


def test_spawn_cluster_refuses_where_no_player_fits_and_places_nothing(room):
    before = _count()
    with pytest.raises(ToolError, match="no_floor_with_headroom"):
        srv.spawn_cluster(
            team="red", count=1, area={"min": [672, 96, 0], "max": [736, 160, 32]}, label="Shelf"
        )
    with pytest.raises(ToolError, match="only 4 of"):
        srv.spawn_cluster(
            team="red", count=5, area={"min": [384, 384, 0], "max": [512, 512, 128]}, label="C"
        )
    assert _count() == before


def test_spawn_cluster_keeps_clear_of_existing_spawns(room):
    area = {"min": [384, 384, 0], "max": [512, 512, 128]}
    srv.spawn_cluster(team="red", count=4, area=area, label="Crate")
    with pytest.raises(ToolError, match="near_existing_spawn"):
        srv.spawn_cluster(team="red", count=1, area=area, marker=False)


def test_spawn_cluster_checks_its_arguments_before_measuring(room):
    with pytest.raises(ToolError, match="unknown team"):
        srv.spawn_cluster(team="green", count=1, area={"min": [0, 0, 0], "max": [1, 1, 1]})
    with pytest.raises(ToolError, match="label"):
        srv.spawn_cluster(team="red", count=1, area={"min": [64, 64, 0], "max": [192, 192, 64]})
    with pytest.raises(ToolError, match="must exceed the player width"):
        srv.spawn_cluster(
            team="red",
            count=1,
            area={"min": [64, 64, 0], "max": [192, 192, 64]},
            label="x",
            spacing=32,
        )


def test_a_profile_without_spawn_clusters_says_so(tmp_path, monkeypatch):
    other = [p for p in profiles.available() if not profiles.load(p).get("spawn_cluster")]
    if not other:
        pytest.skip("every profile on disk describes spawn clusters")
    monkeypatch.setenv("NRC_PROFILE", other[0])
    srv.map_new(str(tmp_path / "x.map"))
    with pytest.raises(ToolError, match="no spawn_cluster section"):
        srv.spawn_cluster(team="red", count=1, area={"min": [0, 0, 0], "max": [64, 64, 64]})


def test_lattice_is_centred_and_on_grid():
    assert spawns.lattice(448, 992, 96, 36, 8) == [480, 576, 672, 768, 864, 960]
    assert spawns.lattice(128, 192, 96, 36, 8) == [160]
    assert spawns.lattice(0, 10, 64, 36, 8) == [8], "narrower than a player: one, at the centre"
