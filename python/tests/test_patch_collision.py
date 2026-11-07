"""Patches in the movement analysis: the engine's collision facets, not thin air.

The maps are synthetic, built from the same generator `patch_commit` uses, so each test is
about a known curve. Verdicts the real engine was asked about (bench/dogfood/patches.py) are
marked where they are asserted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nrc_mcp import analysis, kernel, routes
from test_analysis import box, face, room, write_map

PROFILE = "etl"


@pytest.fixture(scope="module")
def kern():
    try:
        return kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))


def with_patches(kern, path: Path, *specs: tuple[dict, str]) -> Path:
    """Add `patch_commit` surfaces to a written map and save it in place."""
    game_map = kern.Map.load(str(path))
    for spec, shader in specs:
        kern.patch_commit(game_map, spec, shader, "worldspawn", False, None, None)
    game_map.save(str(path))
    return path


def pipe(x: float, z: float, radius: float, *, y0: float = 0, length: float = 256) -> dict:
    """A cylinder lying across the lane (along y) with its axis at height `z`."""
    return {
        "kind": "cylinder",
        "centre": [x, y0, z],
        "radius": radius,
        "length": length,
        "axis": "y",
    }


def route(path: Path, stances=("stand", "crouch", "prone"), y: float = 128, cell: float = 8):
    built = {
        s: analysis.build_navgrid(path, cell=cell, profile_id=PROFILE, stance=s) for s in stances
    }
    game_map = next(iter(built.values())).game_map
    result = routes.find_route(
        built,
        routes.endpoint(game_map, PROFILE, (64, y, 0), None, "start"),
        routes.endpoint(game_map, PROFILE, (448, y, 0), None, "end"),
    )
    return result, next(iter(built.values()))


# ---------------------------------------------------------------------------
# A pipe on the floor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "radius,reachable",
    [
        # r=8: the engine's collision is a diamond topping out at 16, under the 18-unit step.
        (8, True),
        # r=48: subdivided, steep near the floor and 96 high. Engine: blocked.
        (48, False),
    ],
)
def test_pipe_on_the_floor_is_stepped_over_or_blocks(kern, tmp_path, radius, reachable):
    path = write_map(tmp_path, room(0, 0, 512, 256))
    with_patches(kern, path, (pipe(256, radius, radius), "t/pipe"))
    result, grid = route(path, stances=("stand",))
    assert grid.geometry["patches_used"] == 1
    assert grid.geometry["patch_facets"] >= 4
    assert not [f for f in grid.findings if f["code"] == "NAV_PATCHES_IGNORED"]
    assert result["reachable"] is reachable, result.get("status")


def test_an_inward_facing_pipe_blocks_from_outside_too(kern, tmp_path):
    # Its facets face the axis, so a player outside meets their backs. Engine: still blocked
    # at the same x as the outward pipe, by the facet bevels (bench/dogfood/patches.py).
    path = write_map(tmp_path, room(0, 0, 512, 256))
    with_patches(kern, path, ({**pipe(256, 48, 48), "facing": "in"}, "t/pipe"))
    result, _ = route(path, stances=("stand",))
    assert result["reachable"] is False


def test_the_patch_surface_is_the_engine_chord_not_the_drawn_curve(kern, tmp_path):
    path = write_map(tmp_path, room(0, 0, 512, 256))
    with_patches(kern, path, (pipe(256, 24, 24), "t/pipe"))
    grid = analysis.build_navgrid(path, cell=8, profile_id=PROFILE)
    top = analysis.column_intervals(grid.solids, grid.index, 256, 128)
    # The diamond's top vertex is exactly the curve's top, 48.
    assert top[-2][1] == pytest.approx(48, abs=1e-3)  # single precision, as the engine
    # Half way down the side the chord is inside the drawn circle: at x = 256 + 12 the chord is
    # at 36 where the circle is at 24 + sqrt(24^2 - 12^2) = 44.8.
    side = analysis.column_intervals(grid.solids, grid.index, 268, 128)
    assert side[-2][1] == pytest.approx(36, abs=1e-3)


def test_a_small_pipe_reads_like_the_brush_ridge_it_collides_as(kern, tmp_path):
    """r=24 is drawn 48 high, but r*sqrt(2)/4 < 16 collapses each quarter to one chord: the
    engine collides with a 45-degree diamond, and in the engine (bench/dogfood/patches.py)
    the player walks over it, since 45 degrees is within MIN_WALK_NORMAL 0.7.

    The grid rests the body box on what is under it, rising up to a step, so a 45-degree
    brush ridge of the same size is crossed as the engine crosses it. The diamond is not:
    its lower chords overhang the floor from 0 to 24 units, and a box standing beside it
    would have to rise 30 to clear them. That remains a difference from the engine.
    """
    path = write_map(tmp_path, room(0, 0, 512, 256))
    with_patches(kern, path, (pipe(256, 24, 24), "t/pipe"))
    as_patch, _ = route(path, stances=("stand",))
    cx, h = 256, 24
    ridge = (
        "{\n"
        + "\n".join(
            [
                face((cx - h, 0, 0), (cx - h, 1, 0), (cx, 0, h)),
                face((cx + h, 0, 0), (cx, 0, h), (cx + h, 1, 0)),
                face((0, 0, 0), (1, 0, 0), (0, 1, 0)),
                face((0, 16, 0), (0, 16, 1), (1, 16, 0)),
                face((0, 240, 0), (1, 240, 0), (0, 240, 1)),
            ]
        )
        + "\n}\n"
    )
    brush_path = write_map(tmp_path, [*room(0, 0, 512, 256), ridge], name="ridge.map")
    as_brush, _ = route(brush_path, stances=("stand",))
    assert as_brush["reachable"] is True
    assert as_patch["reachable"] is False


# ---------------------------------------------------------------------------
# An arch over a doorway
# ---------------------------------------------------------------------------


def doorway_room() -> list[str]:
    return [
        *room(0, 0, 512, 256),
        box((240, 16, 0), (272, 64, 256)),
        box((240, 192, 0), (272, 240, 256)),
        box((240, 64, 176), (272, 192, 256)),
    ]


def test_arch_over_a_doorway_leaves_it_open(kern, tmp_path):
    path = write_map(tmp_path, doorway_room())
    arch = {"kind": "arch", "centre": [240, 128, 112], "radius": 64, "length": 32, "axis": "x"}
    with_patches(kern, path, (arch, "t/arch"))
    result, grid = route(path, stances=("stand",))
    assert grid.geometry["patches_used"] == 1
    # Engine: walked through standing.
    assert result["reachable"] is True
    assert {s["stance"] for s in result["segments"]} == {"stand"}
    # The arch is there: near the springing line the opening's ceiling is the arch, not the
    # 176-unit lintel, and the middle stays clear to the crown.
    near_side = analysis.column_intervals(grid.solids, grid.index, 256, 72)
    floor, ceiling = analysis.floor_and_ceiling(near_side, 1)
    assert floor == 0 and 112 < ceiling < 140
    middle = analysis.column_intervals(grid.solids, grid.index, 256, 128)
    assert analysis.floor_and_ceiling(middle, 1) == (0, pytest.approx(176))


def test_an_arch_low_enough_to_matter_forces_a_crouch(kern, tmp_path):
    # Springing at 8 with radius 64: the chords leave less than standing height across the
    # whole 128-unit opening except a strip too narrow for the body box.
    path = write_map(
        tmp_path,
        [
            *room(0, 0, 512, 256),
            box((240, 16, 0), (272, 64, 256)),
            box((240, 192, 0), (272, 240, 256)),
            box((240, 64, 72), (272, 192, 256)),
        ],
    )
    arch = {"kind": "arch", "centre": [240, 128, 0], "radius": 64, "length": 32, "axis": "x"}
    with_patches(kern, path, (arch, "t/arch"))
    standing, _ = route(path, stances=("stand",))
    assert standing["reachable"] is False
    mixed, _ = route(path)
    assert mixed["reachable"] is True
    assert "stand" in {s["stance"] for s in mixed["segments"]}
    assert {s["stance"] for s in mixed["segments"]} - {"stand"}


# ---------------------------------------------------------------------------
# A prone vent with a pipe under its ceiling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "radius,reachable",
    [
        (4, True),  # lowest point 44: clears the 40.125 prone body. Engine: crawled through.
        (16, False),  # lowest point 32. Engine: stopped at the pipe.
    ],
)
def test_patch_under_a_prone_vent_ceiling(kern, tmp_path, radius, reachable):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((192, 16, 48), (320, 240, 256))])
    with_patches(kern, path, (pipe(256, 48, radius), "t/pipe"))
    result, grid = route(path)
    assert grid.geometry["patches_used"] == 1
    assert result["reachable"] is reachable, result.get("status")
    if reachable:
        assert "prone" in {s["stance"] for s in result["segments"]}


def test_without_the_patch_the_vent_is_prone_passable(tmp_path):
    path = write_map(tmp_path, [*room(0, 0, 512, 256), box((192, 16, 48), (320, 240, 256))])
    result, grid = route(path)
    assert grid.geometry["patches"] == 0
    assert result["reachable"] is True


# ---------------------------------------------------------------------------
# Which patches collide
# ---------------------------------------------------------------------------


def project(tmp_path: Path, shader_text: str) -> Path:
    (tmp_path / "maps").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "probe.shader").write_text(shader_text)
    return tmp_path / "maps"


SHADERS = """
textures/t/decal
{
    surfaceparm nonsolid
    surfaceparm trans
}
textures/t/clip
{
    surfaceparm nodraw
    surfaceparm nonsolid
    surfaceparm playerclip
}
textures/t/water
{
    surfaceparm water
}
"""


@pytest.mark.parametrize(
    "shader,blocks", [("t/decal", False), ("t/clip", True), ("t/water", False), ("t/x", True)]
)
def test_patch_contents_come_from_its_shader_script(kern, tmp_path, shader, blocks):
    maps = project(tmp_path, SHADERS)
    path = write_map(maps, room(0, 0, 512, 256))
    with_patches(kern, path, (pipe(256, 48, 48), shader))
    result, grid = route(path, stances=("stand",))
    assert grid.geometry["patches_used"] == (1 if blocks else 0)
    assert grid.geometry["patches_nonsolid_shader"] == (0 if blocks else 1)
    assert grid.geometry["patches_shader_unresolved"] == 0
    assert result["reachable"] is not blocks


def test_unreadable_scripts_fall_back_to_shader_names(kern, tmp_path, monkeypatch):
    monkeypatch.delenv("NRC_FS_BASEPATH", raising=False)
    path = write_map(tmp_path, room(0, 0, 512, 256))
    with_patches(kern, path, (pipe(256, 48, 48), "t/pipe_nonsolid"))
    grid = analysis.build_navgrid(path, cell=8, profile_id=PROFILE)
    assert grid.geometry["patches_shader_unresolved"] == 1
    assert grid.geometry["patches_nonsolid_shader"] == 1
    assert "NAV_PATCH_CONTENTS_GUESSED" in {f["code"] for f in grid.findings}


def test_profile_adds_the_games_own_solid_clearing_parms(tmp_path):
    maps = project(tmp_path, "textures/t/shot\n{\n surfaceparm clipshot\n}\n")
    contents = analysis.shader_contents(PROFILE, maps / "m.map")
    assert contents.blocks_player("t/shot") is False
    assert contents.blocks_player("t/unknown") is None


def test_patches_in_a_trigger_entity_do_not_collide(kern, tmp_path):
    path = write_map(tmp_path, room(0, 0, 512, 256))
    game_map = kern.Map.load(str(path))
    kern.patch_commit(game_map, pipe(256, 48, 48), "t/pipe", "trigger_multiple", True, None, None)
    game_map.save(str(path))
    grid = analysis.build_navgrid(path, cell=8, profile_id=PROFILE)
    assert grid.geometry["patches_nonsolid_entity"] == 1
    assert grid.geometry["patches_used"] == 0


def test_a_patch_the_engine_refuses_is_reported_not_guessed(tmp_path):
    rows = " ".join(
        "( " + " ".join(f"( {256 + 16 * w} {64 * h} 32 0 0 )" for h in range(3)) + " )"
        for w in range(4)
    )
    patch = (
        "{\npatchDef2\n{\nt/pipe\n( 4 3 0 0 0 )\n(\n" + rows + "\n)\n}\n}\n"
    )  # even width: CM_GeneratePatchCollide drops the map (cm_patch.c:1463)
    path = write_map(tmp_path, [*room(0, 0, 512, 256), patch])
    grid = analysis.build_navgrid(path, cell=8, profile_id=PROFILE)
    assert grid.geometry["patches_unmodelled"] == 1
    finding = next(f for f in grid.findings if f["code"] == "NAV_PATCHES_IGNORED")
    assert "even" in finding["message"]


# ---------------------------------------------------------------------------
# Stock maps: patch floors the engine was asked about
# ---------------------------------------------------------------------------

REAL = Path(__file__).resolve().parents[2] / "corpus" / "real"


@pytest.mark.skipif(not (REAL / "oasis_final.map").is_file(), reason="corpus/real not imported")
def test_oasis_barrel_tops_are_floors_where_the_engine_stands():
    """Barrels in Oasis are patch cylinders capped by flat patches, 44 units off the ground.

    ET: Legacy 2.86.2, `bench/dogfood/patches.py stock` (and an earlier direct run): a player
    dropped onto each cap comes to rest with its origin 24.125-24.29 above the cap, the box's
    24-unit mins plus the trace's 0.125 separation, sampled within a frame of landing. The
    analysis puts the floor at the cap; without patch collision it put it 44 units lower.
    """
    path = REAL / "oasis_final.map"
    game_map = kernel.load_map(path)
    contents = analysis.shader_contents(PROFILE, path)
    solids, provenance = analysis.collect_solids(
        game_map, analysis.roles(PROFILE), contents=contents
    )
    assert provenance["patches_unmodelled"] == 0
    index = analysis.BucketIndex(solids)
    brushes = [s for s in solids if not s.patch]
    brush_index = analysis.BucketIndex(brushes)
    for x, y, engine_origin_z in ORACLE_OASIS:
        floor, _ = analysis.floor_and_ceiling(
            analysis.column_intervals(solids, index, x, y), engine_origin_z
        )
        assert 0 <= engine_origin_z - 24 - floor <= 0.3
        bare, _ = analysis.floor_and_ceiling(
            analysis.column_intervals(brushes, brush_index, x, y), engine_origin_z
        )
        assert floor - bare > 40


# (x, y, origin z where the engine's player came to rest), from
# out/dogfood/patches-stock/*/settled.json. Every cap is a flat patch facet at z=-372.
ORACLE_OASIS: list[tuple[float, float, float]] = [
    (9102, 5258, -347.71145629883),
    (9129, 5248, -347.71145629883),
    (9147, 5224, -347.71145629883),
    (9113, 5223, -347.71145629883),
]


@pytest.mark.parametrize(("shader", "blocks"), [("t/volume", False), ("t/wall", True)])
def test_a_brush_whose_scripts_make_it_nonsolid_does_not_block(kern, tmp_path, shader, blocks):
    """An alphaMod volume across a corridor: nodraw, nonsolid, compiled into no collision."""
    maps = project(
        tmp_path,
        "textures/t/volume\n{\n q3map_alphaMod volume\n surfaceparm nodraw\n"
        " surfaceparm nonsolid\n surfaceparm trans\n}\n",
    )
    path = write_map(maps, [*room(0, 0, 512, 256), box((240, 0, 0), (272, 256, 256), shader)])
    result, grid = route(path, stances=("stand",))
    assert grid.geometry["brushes_nonsolid_shader"] == (0 if blocks else 1)
    assert result["reachable"] is not blocks
