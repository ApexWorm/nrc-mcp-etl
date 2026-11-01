"""Phase 3: the declarative rule engine, BSP introspection, and packaging checks."""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from nrc_mcp import bsp, kernel, pack, profiles, rules


def ent(index: int, classname: str, **keys) -> dict:
    """A map entity in the shape the kernel hands over."""
    return {
        "index": index,
        "classname": classname,
        "origin": keys.pop("origin", None),
        "keys": [("classname", classname), *keys.items()],
        "brushes": 0,
        "patches": 0,
    }


# ---------------------------------------------------------------------------
# Rule engine — check types
# ---------------------------------------------------------------------------


def run(rule: dict, entities: list[dict], monkeypatch) -> list[rules.Finding]:
    """Evaluate one synthetic rule, bypassing the on-disk profile."""
    monkeypatch.setattr(profiles, "load", lambda _pid: {"rules": [rule]})
    return rules.evaluate("test", entities)


def test_require_any_passes_when_one_is_present(monkeypatch):
    rule = {
        "id": "R",
        "check": "require_any",
        "severity": "error",
        "confidence": "verified",
        "message": "need {expected}",
        "params": {"classnames": ["a_thing", "b_thing"]},
    }
    assert run(rule, [ent(0, "b_thing")], monkeypatch) == []
    f = run(rule, [ent(0, "unrelated")], monkeypatch)
    assert len(f) == 1
    assert f[0].severity == "error"
    assert "a_thing or b_thing" in f[0].message


def test_count_exact_reports_both_too_few_and_too_many(monkeypatch):
    rule = {
        "id": "R",
        "check": "count_exact",
        "severity": "error",
        "confidence": "verified",
        "message": "found {found} want {expected}",
        "params": {"classname": "site", "count": 2},
    }
    assert run(rule, [ent(0, "site"), ent(1, "site")], monkeypatch) == []
    assert "found 1 want 2" in run(rule, [ent(0, "site")], monkeypatch)[0].message
    three = [ent(i, "site") for i in range(3)]
    assert "found 3 want 2" in run(rule, three, monkeypatch)[0].message


def test_applies_when_keeps_a_rule_silent_on_maps_that_do_not_use_it(monkeypatch):
    rule = {
        "id": "R",
        "check": "count_exact",
        "severity": "error",
        "confidence": "verified",
        "message": "x",
        "applies_when": {"classname_present": "site"},
        "params": {"classname": "site", "count": 2},
    }
    # No sites at all: the map does not use this mode, so silence.
    assert run(rule, [ent(0, "other")], monkeypatch) == []
    assert len(run(rule, [ent(0, "site")], monkeypatch)) == 1


def test_distinct_group_per_team_is_the_corrected_survivor_rule(monkeypatch):
    rule = {
        "id": "R",
        "check": "distinct_group_per_team",
        "severity": "error",
        "confidence": "verified",
        "message": "{found} shared: {detail}",
        "params": {
            "classname": "spawn",
            "gametype_key": "gt",
            "gametype": "4",
            "group_key": "group",
            "team_key": "team",
        },
    }
    # Separate groups per team: correct, and must NOT be flagged — the design document's
    # version of this rule would have failed exactly this arrangement.
    good = [
        ent(0, "spawn", gt="4,5,8", team="blue", group="1"),
        ent(1, "spawn", gt="4,5,8", team="red", group="2"),
    ]
    assert run(rule, good, monkeypatch) == []

    shared = [
        ent(0, "spawn", gt="4,5,8", team="blue", group="A"),
        ent(1, "spawn", gt="4,5,8", team="red", group="A"),
    ]
    f = run(rule, shared, monkeypatch)
    assert len(f) == 1
    assert f[0].entities == [0, 1]

    # A spawn not offered to gametype 4 is out of scope even if it shares a group.
    other_mode = [
        ent(0, "spawn", gt="7", team="blue", group="A"),
        ent(1, "spawn", gt="7", team="red", group="A"),
    ]
    assert run(rule, other_mode, monkeypatch) == []


def test_gametype_lists_split_on_commas_spaces_and_semicolons():
    assert rules._gametype_list("4,5,8") == {"4", "5", "8"}
    assert rules._gametype_list("4 5 8") == {"4", "5", "8"}
    assert rules._gametype_list("4;5, 8") == {"4", "5", "8"}
    assert rules._gametype_list("") == set()
    # A single value must not be read as characters.
    assert rules._gametype_list("10") == {"10"}


def test_forbid_gametype(monkeypatch):
    rule = {
        "id": "R",
        "check": "forbid_gametype",
        "severity": "error",
        "confidence": "verified",
        "message": "{found} bad",
        "params": {"classname": "spawn", "gametype_key": "gt", "gametype": "0"},
    }
    assert run(rule, [ent(0, "spawn", gt="3,4")], monkeypatch) == []
    assert len(run(rule, [ent(0, "spawn", gt="0,3")], monkeypatch)) == 1


def test_key_in_enum_and_key_required(monkeypatch):
    enum_rule = {
        "id": "R",
        "check": "key_in_enum",
        "severity": "warning",
        "confidence": "verified",
        "message": "{found} bad {key}",
        "params": {"classname": "spawn", "key": "team", "values": ["red", "blue"]},
    }
    assert run(enum_rule, [ent(0, "spawn", team="red")], monkeypatch) == []
    assert len(run(enum_rule, [ent(0, "spawn", team="green")], monkeypatch)) == 1
    # A missing key is not a wrong key; key_required is the check for that.
    assert run(enum_rule, [ent(0, "spawn")], monkeypatch) == []

    req_rule = {
        **enum_rule,
        "check": "key_required",
        "params": {"classname": "spawn", "key": "team"},
    }
    assert len(run(req_rule, [ent(0, "spawn")], monkeypatch)) == 1


def test_min_spacing_measures_within_groups_only(monkeypatch):
    rule = {
        "id": "R",
        "check": "min_spacing",
        "severity": "info",
        "confidence": "verified",
        "message": "{found} close: {detail}",
        "params": {"classname": "spawn", "group_key": "group", "distance": 32},
    }
    close = [
        ent(0, "spawn", group="1", origin=[0.0, 0.0, 0.0]),
        ent(1, "spawn", group="1", origin=[8.0, 0.0, 0.0]),
    ]
    assert len(run(rule, close, monkeypatch)) == 1

    # The same two positions in different groups are not compared.
    other = [
        ent(0, "spawn", group="1", origin=[0.0, 0.0, 0.0]),
        ent(1, "spawn", group="2", origin=[8.0, 0.0, 0.0]),
    ]
    assert run(rule, other, monkeypatch) == []


def test_group_size(monkeypatch):
    rule = {
        "id": "R",
        "check": "group_size",
        "severity": "info",
        "confidence": "verified",
        "message": "{found} wrong: {detail}",
        "params": {"classname": "spawn", "group_key": "group", "count": 2},
    }
    ok = [ent(0, "spawn", group="1"), ent(1, "spawn", group="1")]
    assert run(rule, ok, monkeypatch) == []
    assert len(run(rule, [ent(0, "spawn", group="1")], monkeypatch)) == 1


# ---------------------------------------------------------------------------
# The confidence clamp — the mechanism that matters most
# ---------------------------------------------------------------------------


def test_an_unverified_rule_can_never_produce_an_error(monkeypatch):
    """§7: unverified rules must not fail a build.

    This is the guard that would have stopped the design document's wrong Team Survivor rule
    from failing correct maps.
    """
    rule = {
        "id": "R",
        "check": "count_exact",
        "severity": "error",
        "confidence": "unverified",
        "message": "x",
        "params": {"classname": "site", "count": 2},
    }
    f = run(rule, [ent(0, "site")], monkeypatch)
    assert len(f) == 1
    assert f[0].severity == "info", "an unverified rule must be downgraded"
    assert f[0].confidence == "unverified"


def test_a_malformed_rule_is_reported_rather_than_crashing_validation(monkeypatch):
    unknown = {"id": "R", "check": "no_such_check", "confidence": "verified"}
    f = run(unknown, [ent(0, "x")], monkeypatch)
    assert f[0].code == "RULE_UNKNOWN_CHECK"
    assert "count_exact" in f[0].message

    bad_params = {
        "id": "R",
        "check": "count_exact",
        "confidence": "verified",
        "message": "x",
        "params": {"classname": "s", "count": "not a number"},
    }
    f = run(bad_params, [ent(0, "s")], monkeypatch)
    assert f[0].code == "RULE_MALFORMED"


def test_a_broken_message_template_still_reports_the_facts(monkeypatch):
    rule = {
        "id": "R",
        "check": "count_exact",
        "severity": "error",
        "confidence": "verified",
        "message": "found {nonexistent_field}",
        "params": {"classname": "site", "count": 2},
    }
    f = run(rule, [ent(0, "site")], monkeypatch)
    assert "facts:" in f[0].message, "a profile typo must not hide the finding"


def test_spawnflags_parse_the_way_the_engine_does():
    """`atoi`: a leading integer, whatever follows, and 0 for nothing at all."""
    assert rules.parse_spawnflags("6") == 6
    assert rules.parse_spawnflags(" 14 ") == 14
    assert rules.parse_spawnflags("3abc") == 3
    assert rules.parse_spawnflags("") == 0
    assert rules.parse_spawnflags("x") == 0
    assert rules.parse_spawnflags(None) == 0


def test_key_names_match_without_regard_to_case(monkeypatch):
    """A shipped map writes `Track`; the engine reads it as `track`, so a rule must too."""
    rule = {
        "id": "R",
        "check": "key_required",
        "severity": "error",
        "confidence": "verified",
        "message": "x",
        "params": {"classname": "zone", "key": "track"},
    }
    assert run(rule, [ent(0, "zone", Track="the gate")], monkeypatch) == []
    assert run(rule, [ent(0, "zone")], monkeypatch)


def test_flag_count_min_needs_enough_entities_with_the_flag(monkeypatch):
    rule = {
        "id": "R",
        "check": "flag_count_min",
        "severity": "warning",
        "confidence": "verified",
        "message": "{found} of {of}",
        "params": {"classname": "spawn", "flag": 2, "count": 1},
    }
    assert run(rule, [ent(0, "spawn", spawnflags="3"), ent(1, "spawn")], monkeypatch) == []
    f = run(rule, [ent(0, "spawn", spawnflags="1"), ent(1, "spawn")], monkeypatch)
    assert f[0].message == "0 of 2"
    assert f[0].entities == [0, 1]


def test_flag_requires_key_accepts_any_of_several_keys(monkeypatch):
    rule = {
        "id": "R",
        "check": "flag_requires_key",
        "severity": "error",
        "confidence": "verified",
        "message": "{found} without {key}",
        "params": {"classname": "zone", "flag": 4, "keys": ["override", "spawnitem"]},
    }
    ok = [
        ent(0, "zone", spawnflags="14", override="text"),
        ent(1, "zone", spawnflags="4", spawnitem="text"),
        ent(2, "zone", spawnflags="3"),  # flag not set: nothing required
    ]
    assert run(rule, ok, monkeypatch) == []
    f = run(rule, [ent(5, "zone", spawnflags="4", override="  ")], monkeypatch)
    assert f[0].message == "1 without override or spawnitem"
    assert f[0].entities == [5]


def test_targets_resolve_reports_names_that_name_nothing(monkeypatch):
    rule = {
        "id": "R",
        "check": "targets_resolve",
        "severity": "warning",
        "confidence": "verified",
        "message": "{found}: {detail}",
        "params": {
            "keys": ["target"],
            "list_keys": ["stages"],
            "separator": ";",
            "exclude_classnames": ["label_user"],
        },
    }
    good = [
        ent(0, "door", targetname="d1"),
        ent(1, "button", target="d1"),
        ent(2, "builder", stages="d1;d2;"),
        ent(3, "door", targetname="d2"),
        ent(4, "label_user", target="not_an_entity"),
    ]
    assert run(rule, good, monkeypatch) == []
    bad = [
        ent(0, "door", targetname="d1"),
        ent(1, "button", target="D1"),
        ent(2, "b", stages="d1;dx"),
    ]
    f = run(rule, bad, monkeypatch)
    assert "2:" in f[0].message, "names are case-sensitive, and each list entry counts"
    assert f[0].entities == [1, 2]


def test_applies_when_flag_present_needs_one_entity_with_the_flag(monkeypatch):
    rule = {
        "id": "R",
        "check": "count_min",
        "severity": "error",
        "confidence": "verified",
        "message": "x",
        "applies_when": {
            "flag_present": [{"classname": "red", "flag": 2}, {"classname": "blue", "flag": 2}]
        },
        "params": {"classname": "marker", "count": 1},
    }
    assert run(rule, [ent(0, "red"), ent(1, "blue", spawnflags="1")], monkeypatch) == []
    assert run(rule, [ent(0, "red"), ent(1, "blue", spawnflags="3")], monkeypatch)
    assert run(rule, [ent(0, "red", spawnflags="2"), ent(1, "marker")], monkeypatch) == []


def test_the_etl_profile_refuses_a_map_a_spectator_cannot_join():
    """Found by loading a map in etlded: team spawns alone crash the server on first connect."""
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    team_spawns = [
        ent(0, "worldspawn"),
        ent(1, "team_CTF_redspawn", spawnflags="3"),
        ent(2, "team_CTF_bluespawn", spawnflags="3"),
    ]

    def codes(entities):
        return {f.code: f.severity for f in rules.evaluate("etl", entities)}

    assert codes(team_spawns)["ETL_SPECTATOR_POINT_REQUIRED"] == "error"
    for fallback in ("info_player_intermission", "info_player_deathmatch", "info_player_start"):
        assert "ETL_SPECTATOR_POINT_REQUIRED" not in codes([*team_spawns, ent(3, fallback)])


def test_the_etl_profile_errors_only_on_the_script_multiplayer_count_the_engine_stops_on():
    """Checked in etlded: two stop the game at load, none only prints a complaint."""
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    world = ent(0, "worldspawn")
    gm = [ent(i, "script_multiplayer") for i in (1, 2)]

    def codes(entities):
        return {
            f.code: f.severity
            for f in rules.evaluate("etl", entities)
            if f.code.startswith("ETL_SCRIPT_MULTIPLAYER")
        }

    assert codes([world]) == {"ETL_SCRIPT_MULTIPLAYER_REQUIRED": "warning"}
    assert codes([world, gm[0]]) == {}
    assert codes([world, *gm]) == {"ETL_SCRIPT_MULTIPLAYER_AT_MOST_ONE": "error"}


# ---------------------------------------------------------------------------
# The shipped profile's rules, against real maps
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def pid() -> str:
    av = profiles.available()
    if not av:
        pytest.skip("no profiles on disk")
    # The profile the suite runs against; mise sets it, so a second profile on disk
    # does not silently change which one is tested.
    return os.environ.get("NRC_PROFILE") or av[0]


def test_shipped_rules_are_all_well_formed(pid):
    rs = profiles.load(pid).get("rules") or []
    assert rs, "the profile should ship rules"
    for r in rs:
        assert r.get("id"), r
        assert r["check"] in rules.CHECKS, f"{r['id']} uses unknown check {r.get('check')}"
        assert r.get("confidence") in {"verified", "unverified"}, r["id"]
        assert r.get("rule_source"), f"{r['id']} has no rule_source"
        assert r.get("message"), f"{r['id']} has no message"


def test_no_shipped_rule_produces_an_error_unless_verified(pid):
    for r in profiles.load(pid).get("rules") or []:
        if rules._effective_severity(r) == "error":
            assert r["confidence"] == "verified", f"{r['id']} errors on unverified grounds"


def test_rules_catch_a_real_defect_in_a_real_map(pid):
    """A shipped map really does share Team Survivor spawn groups between teams.

    Groups 'A' and 'B' each hold eight blue and eight red spawns for gametype 4. Keeping this
    as a test means the corrected rule stays wired up, and it also demonstrates the class of
    bug the spec says ships regularly.
    """
    p = kernel.repo_root() / "corpus" / "real" / "ut4_megastructunnel.map"
    if not p.is_file():
        pytest.skip("real corpus not imported")
    m = kernel.load_map(p)
    found = rules.evaluate(pid, m.entities())
    codes = {f.code: f for f in found}
    assert "UT_TS_SEPARATE_GROUPS" in codes, f"expected the group rule to fire, got {list(codes)}"
    assert codes["UT_TS_SEPARATE_GROUPS"].severity == "error"


def test_a_clean_map_produces_no_rule_errors(pid):
    p = kernel.repo_root() / "corpus" / "real" / "ut4_woolis.map"
    if not p.is_file():
        pytest.skip("real corpus not imported")
    m = kernel.load_map(p)
    errors = [f for f in rules.evaluate(pid, m.entities()) if f.severity == "error"]
    assert not errors, f"unexpected errors: {[(f.code, f.message) for f in errors]}"


def test_unknown_classnames_are_reported_but_never_fatal(pid):
    found = rules.unknown_classnames(pid, [ent(0, "definitely_not_a_real_classname")])
    assert len(found) == 1
    assert found[0].severity == "warning", "the profile may simply be older than the map"


# ---------------------------------------------------------------------------
# BSP introspection
# ---------------------------------------------------------------------------


def write_lumps(dirpath: Path, lumps: dict) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    for name, value in lumps.items():
        (dirpath / name).write_text(json.dumps(value))
    return dirpath


def test_report_reads_lumps_and_builds_a_shader_histogram(tmp_path: Path):
    d = write_lumps(
        tmp_path / "m",
        {
            "shaders.json": {
                "shader#0": {"shader": "textures/a/wall"},
                "shader#1": {"shader": "textures/common/caulk"},
            },
            "DrawSurfaces.json": {
                "DrawSurface#0": {
                    "shaderNum": 0,
                    "surfaceType": 1,
                    "numVerts": 4,
                    "numIndexes": 6,
                    "lightmapNum": [-3, -3, -3, -3],
                },
                "DrawSurface#1": {
                    "shaderNum": 0,
                    "surfaceType": 1,
                    "numVerts": 8,
                    "numIndexes": 12,
                    "lightmapNum": [0, -3, -3, -3],
                },
                "DrawSurface#2": {
                    "shaderNum": 0,
                    "surfaceType": 2,
                    "numVerts": 9,
                    "numIndexes": 0,
                    "lightmapNum": [-3, -3, -3, -3],
                },
            },
            "leafs.json": {
                "leaf#0": {"cluster": 0, "area": 0},
                "leaf#1": {"cluster": 1, "area": 0},
            },
            "models.json": {
                "model#0": {
                    "minmax": {"mins": [0, 0, 0], "maxs": [64, 64, 64]},
                    "numBSPSurfaces": 3,
                    "numBSPBrushes": 2,
                }
            },
        },
    )
    r = bsp.report(d)
    assert r["counts"]["draw_surfaces"] == 3
    assert r["counts"]["shaders"] == 2
    assert r["surface_types"] == {"planar": 2, "patch": 1}
    assert r["lightmapped_surfaces"] == 1
    assert r["vertex_lit_surfaces"] == 2
    assert r["top_shaders"][0] == {
        "shader": "textures/a/wall",
        "surfaces": 3,
        "verts": 21,
        "indexes": 18,
    }
    # caulk is never drawn, so it is referenced by no surface.
    assert r["unreferenced_shaders"] == ["textures/common/caulk"]
    assert r["worldspawn_model"]["brushes"] == 2


def test_lumps_sort_numerically_not_lexically(tmp_path: Path):
    """`#10` must not sort before `#9`, or every per-index reading is silently wrong."""
    d = write_lumps(
        tmp_path / "m",
        {
            "shaders.json": {f"shader#{i}": {"shader": f"s{i}"} for i in range(12)},
            "DrawSurfaces.json": {"DrawSurface#0": {"shaderNum": 11, "surfaceType": 1}},
        },
    )
    r = bsp.report(d)
    assert r["top_shaders"][0]["shader"] == "s11"


def test_compiler_headroom_only_reports_limits_that_still_exist(tmp_path: Path):
    """The classic ceilings on brushes and planes were removed upstream.

    Reporting headroom against a limit the compiler no longer has would be inventing a
    constraint, so those keys must be absent and the omission explained.
    """
    d = write_lumps(tmp_path / "m", {"leafs.json": {"leaf#0": {"cluster": 0, "area": 0}}})
    r = bsp.report(d)
    assert set(r["compiler_limits"]) == {"areas", "leafs", "visclusters", "lighting_bytes"}
    assert "brushes" not in r["compiler_limits"]
    assert any("dynamically sized" in n for n in r["notes"])
    assert "q3map2.h" in r["compiler_limit_source"]


def test_engine_limits_come_from_the_profile_and_carry_confidence(tmp_path: Path, pid):
    d = write_lumps(
        tmp_path / "m",
        {
            "Brushes.json": {f"brush#{i}": {} for i in range(10)},
            "leafs.json": {"leaf#0": {"cluster": 0, "area": 0}},
        },
    )
    r = bsp.report(d, pid)
    el = r.get("engine_limits")
    assert el, "the profile should state engine limits"
    assert el["brushes"]["used"] == 10
    assert el["brushes"]["confidence"] == "unverified"
    assert any("decide whether the map loads" in n for n in r["notes"])


def test_a_missing_lump_directory_says_how_to_make_one(tmp_path: Path):
    with pytest.raises(bsp.BspError, match="bsp:json-unpack"):
        bsp.report(tmp_path / "nope")


def test_an_empty_directory_is_rejected_rather_than_reported_as_zero(tmp_path: Path):
    d = tmp_path / "empty"
    d.mkdir()
    with pytest.raises(bsp.BspError, match="no recognizable BSP lumps"):
        bsp.report(d)


def test_report_on_the_real_corpus_bsp_if_one_has_been_built():
    out = kernel.repo_root() / "out"
    dirs = [p for p in out.glob("*/*") if p.is_dir() and (p / "shaders.json").is_file()]
    if not dirs:
        pytest.skip("no unpacked BSP available; run mise run bsp:json-unpack")
    r = bsp.report(dirs[0])
    assert r["counts"]["draw_surfaces"] > 0
    assert r["counts"]["brushes"] > 0


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------


def make_pk3(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"x")
    return path


def test_ship_check_flags_a_package_missing_everything(tmp_path: Path, pid):
    pk3 = make_pk3(tmp_path / "ut4_test.pk3", ["maps/ut4_test.bsp"])
    r = pack.ship_check(tmp_path / "ut4_test.bsp", pid, pk3=pk3)
    codes = {f["code"] for f in r["findings"]}
    assert "SHIP_NO_LEVELSHOT" in codes
    assert "SHIP_NO_ARENA" in codes
    # Naming is fine here, so that must not fire.
    assert "SHIP_MAP_NAME" not in codes


def test_ship_check_flags_a_bad_map_name(tmp_path: Path, pid):
    pk3 = make_pk3(tmp_path / "MyMap.pk3", ["maps/MyMap.bsp"])
    r = pack.ship_check(tmp_path / "MyMap.bsp", pid, pk3=pk3)
    f = next(x for x in r["findings"] if x["code"] == "SHIP_MAP_NAME")
    # Only a verified convention may warn; an unverified one is reported as info.
    conf = profiles.load(pid)["packaging"]["map_name_confidence"]
    assert f["confidence"] == conf
    assert f["severity"] == ("warning" if conf == "verified" else "info")


def test_ship_check_flags_shadowing_the_base_game(tmp_path: Path, pid):
    pk3 = make_pk3(
        tmp_path / "ut4_x.pk3",
        [
            "maps/ut4_x.bsp",
            "textures/common/caulk.tga",
            "scripts/ut4_x.arena",
            "levelshots/ut4_x.jpg",
        ],
    )
    r = pack.ship_check(tmp_path / "ut4_x.bsp", pid, pk3=pk3)
    f = next(x for x in r["findings"] if x["code"] == "SHIP_SHADOWS_BASEGAME")
    assert f["severity"] == "warning"
    assert "overrides it" in f["message"]


def test_ship_check_errors_on_a_package_with_no_bsp(tmp_path: Path, pid):
    pk3 = make_pk3(tmp_path / "ut4_x.pk3", ["levelshots/ut4_x.jpg", "scripts/ut4_x.arena"])
    r = pack.ship_check(tmp_path / "ut4_x.bsp", pid, pk3=pk3)
    assert any(f["code"] == "SHIP_NO_BSP" and f["severity"] == "error" for f in r["findings"])


def test_ship_check_flags_the_packers_own_failure_marker(tmp_path: Path, pid):
    """`_FAILEDpack.pk3` is q3map2's own oracle for a missing resource."""
    (tmp_path / f"ut4_x{pack.FAILURE_SUFFIX}").write_bytes(b"PK\x03\x04")
    r = pack.ship_check(tmp_path / "ut4_x.bsp", pid)
    f = next(x for x in r["findings"] if x["code"] == "SHIP_FAILED_PACK")
    assert f["severity"] == "error"
    assert f["confidence"] == "verified"


def test_ship_check_findings_are_ordered_worst_first(tmp_path: Path, pid):
    pk3 = make_pk3(tmp_path / "BadName.pk3", ["levelshots/BadName.jpg"])
    r = pack.ship_check(tmp_path / "BadName.bsp", pid, pk3=pk3)
    sev = [f["severity"] for f in r["findings"]]
    order = {"error": 0, "warning": 1, "info": 2}
    assert sev == sorted(sev, key=lambda s: order[s])


def test_ship_check_reports_missing_companion_files(tmp_path: Path, monkeypatch):
    """A required companion missing warns; an optional one is noted; images match any format."""
    monkeypatch.setattr(
        profiles,
        "load",
        lambda _pid: {
            "packaging": {
                "companions": [
                    {"path": "maps/{map}.script", "required": True, "confidence": "verified"},
                    {"path": "maps/{map}.objdata", "required": False, "confidence": "verified"},
                    {
                        "path": "levelshots/{map}_cc.tga",
                        "any_extension": ["jpg", "png"],
                        "confidence": "verified",
                    },
                ]
            }
        },
    )
    pk3 = make_pk3(tmp_path / "m.pk3", ["maps/m.bsp", "levelshots/m_cc.jpg"])
    found = {
        f["message"].split()[1]: f["severity"]
        for f in pack.ship_check(tmp_path / "m.bsp", "test", pk3=pk3)["findings"]
        if f["code"] == "SHIP_NO_COMPANION"
    }
    assert found == {"maps/m.script": "warning", "maps/m.objdata": "info"}

    full = make_pk3(
        tmp_path / "m2.pk3",
        ["maps/m2.bsp", "maps/m2.script", "maps/m2.objdata", "levelshots/m2_cc.tga"],
    )
    r = pack.ship_check(tmp_path / "m2.bsp", "test", pk3=full)
    assert not [f for f in r["findings"] if f["code"] == "SHIP_NO_COMPANION"]


def test_pack_pk3_refuses_a_missing_bsp(tmp_path: Path):
    with pytest.raises(pack.PackError, match="does not exist"):
        pack.pack_pk3(tmp_path / "nope.bsp")


def test_pack_pk3_rejects_an_out_of_range_compression_level(tmp_path: Path):
    (tmp_path / "a.bsp").write_bytes(b"x")
    with pytest.raises(pack.PackError, match="complevel"):
        pack.pack_pk3(tmp_path / "a.bsp", complevel=99)


def test_resource_regex_picks_out_referenced_files():
    text = "reading textures/urt/wall_01.tga\nloading models/props/box.md3\nnoise here"
    hits = {m.group(1) for m in pack._RESOURCE_RE.finditer(text)}
    assert hits == {"textures/urt/wall_01.tga", "models/props/box.md3"}


# ---------------------------------------------------------------------------
# Finding the archive the packer wrote
# ---------------------------------------------------------------------------


def test_the_packers_own_report_locates_the_archive(tmp_path):
    """q3map2 does not put the pk3 where you would guess, so it has to be asked.

    It resolves the output path through its VFS against the engine base — not the cwd and
    not the BSP's directory. A map compiled outside a game tree gets its archive written
    somewhere a glob will not find, and `pack_pk3` then reported a perfectly good pack as a
    failure: `ok: False`, no packages, no failures, empty hint. The archive existed the
    whole time.
    """
    import json

    from nrc_mcp import pack

    archive = tmp_path / "elsewhere" / "mymap_autopacked.pk3"
    archive.parent.mkdir()
    archive.write_bytes(b"PK\x03\x04")
    bsp = tmp_path / "mymap.bsp"
    bsp.write_bytes(b"IBSP")

    # The wrapper reports JSON, and keeps the compiler's console output in stage tails.
    run = {
        "stdout": json.dumps(
            {
                "ok": True,
                "stages": [{"flags": ["-pk3"], "tail": ["  Sounds....", f"Saved to {archive}"]}],
            }
        ),
        "stderr": "",
    }

    found = pack._find_packages(bsp, pack._packer_text(run))
    assert archive in found, f"the packer said where it put it; got {found}"


def test_a_json_envelope_does_not_hide_the_archive(tmp_path):
    """The specific reason the glob-only version failed, pinned on its own.

    Inside JSON the line breaks are escaped, so scanning the envelope as raw text matches
    nothing line by line — and the failure is silent, which is what made it survive.
    """
    import json

    from nrc_mcp import pack

    archive = tmp_path / "x_autopacked.pk3"
    archive.write_bytes(b"PK\x03\x04")
    envelope = json.dumps({"stages": [{"tail": ["noise", f"Saved to {archive}"]}]})

    assert "Saved to" in envelope
    assert not pack._SAVED_TO.search(envelope), (
        "the envelope must still be unmatchable raw — otherwise this test proves nothing"
    )
    assert pack._SAVED_TO.search(pack._packer_text({"stdout": envelope, "stderr": ""}))


def test_packer_text_survives_output_that_is_not_json():
    """A raw-console q3map2, or a wrapper that failed before it could report."""
    from nrc_mcp import pack

    assert "Saved to /tmp/a.pk3" in pack._packer_text(
        {"stdout": "Saved to /tmp/a.pk3\n", "stderr": ""}
    )
    assert pack._packer_text({"stdout": "", "stderr": ""}) == ""
