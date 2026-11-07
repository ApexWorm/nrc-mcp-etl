"""The walk grid at full-map scale: the kernel's sparse voxel pass against the dense reference.

`build_navgrid` voxelizes through `nrc_py.navgrid_voxelize` (one x-slab at a time, each column
kept as its solid runs). `analysis._voxelize_python` is the dense definition it replaced;
every test here that compares the two demands they agree cell for cell, float for float.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from nrc_mcp import analysis, kernel
from test_analysis import box, room, wedge, write_map
from test_patch_collision import pipe, with_patches

PROFILE = "etl"
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def kern():
    try:
        k = kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    if not hasattr(k, "navgrid_voxelize"):
        pytest.skip("kernel built without navgrid_voxelize; run mise run kernel:build")
    return k


def _both(monkeypatch, path, cell, stance="stand"):
    """The grid built by the kernel, and by the dense Python reference."""
    kernel_grid = analysis.build_navgrid(path, cell=cell, profile_id=PROFILE, stance=stance)
    with monkeypatch.context() as m:
        m.setattr(analysis, "_sparse_kernel_available", lambda: False)
        m.setattr(analysis, "DENSE_MAX_CELLS", 10**9)
        dense_grid = analysis.build_navgrid(path, cell=cell, profile_id=PROFILE, stance=stance)
    return kernel_grid, dense_grid


def _assert_same(a: analysis.NavGrid, b: analysis.NavGrid) -> None:
    assert a.dims == b.dims and a.origin == b.origin
    assert a.columns == b.columns
    assert a.stand_z == b.stand_z
    assert a.counts == b.counts
    assert a.restricted == b.restricted
    assert a.clearance_restricted == b.clearance_restricted
    assert list(a.occupancy.offsets) == list(b.occupancy.offsets)
    assert list(a.occupancy.bounds) == list(b.occupancy.bounds)


def _mixed_map(kern, tmp_path: Path) -> Path:
    """A room holding every kind of floor the voxel pass treats specially."""
    brushes = [
        *room(0, 0, 1024, 512),
        # a 4-unit slab over a void (thinner than a cell: no centre inside it)
        box((64, 300, 60), (256, 450, 64)),
        # a floor whose top is off the cell grid, and a 1-unit strip of rubble on it
        box((320, 64, 0), (512, 256, 5)),
        box((400, 64, 5), (404, 256, 6)),
        # a slope, and a box to stand beside
        wedge(),
        box((600, 300, 0), (640, 340, 40)),
        # a low roof: crouch space only
        box((700, 64, 40), (900, 200, 56)),
    ]
    path = write_map(tmp_path, brushes)
    return with_patches(kern, path, (pipe(800, 24, 24, y0=256, length=200), "t/pipe"))


@pytest.mark.parametrize("stance", ["stand", "crouch", "prone"])
def test_the_kernel_pass_matches_the_dense_reference(kern, tmp_path, monkeypatch, stance):
    path = _mixed_map(kern, tmp_path)
    for cell in (8, 16, 24):
        a, b = _both(monkeypatch, path, cell, stance)
        _assert_same(a, b)
        assert a.counts["walkable_cells"] > 0


FIXTURES = [
    "bench/tapes/fixtures/interior_room.map",
    "bench/tapes/fixtures/seed_slab.map",
    "bench/scenarios/fixtures/plaza_building.map",
    "bench/tapes/fixtures/nrcroom/maps/nrcroom.map",
]


@pytest.mark.parametrize("fixture", FIXTURES)
def test_the_kernel_pass_matches_the_dense_reference_on_the_fixture_maps(
    kern, monkeypatch, fixture
):
    path = REPO / fixture
    if not path.is_file():
        pytest.skip(f"{fixture} is not in this checkout")
    for stance in ("stand", "prone"):
        _assert_same(*_both(monkeypatch, path, 16, stance))


def test_the_kernel_pass_matches_the_dense_reference_on_a_real_map(kern, monkeypatch):
    """A whole Splash Damage map, at a cell the dense reference can afford in a unit test."""
    path = REPO / "corpus/real/railgun_final.map"
    if not path.is_file():
        pytest.skip("corpus not imported (mise run corpus:import)")
    a, b = _both(monkeypatch, path, 48)
    _assert_same(a, b)
    assert a.counts["walkable_cells"] > 1000


def test_column_runs_answer_like_the_dense_bytes(kern, tmp_path):
    grid = analysis.build_navgrid(_mixed_map(kern, tmp_path), cell=16, profile_id=PROFILE)
    dense = grid.solid
    nx, ny, nz = grid.dims
    assert analysis.ColumnRuns.from_dense(grid.dims, dense).bounds == grid.occupancy.bounds
    assert grid.occupancy.count() == dense.count(1) == grid.counts["solid_cells"]
    for ix in range(nx):
        for iy in range(ny):
            for iz in range(nz):
                expected = dense[(ix * ny + iy) * nz + iz] == 1
                assert grid.is_solid(ix, iy, iz) is expected
    assert not grid.is_solid(-1, 0, 0) and not grid.is_solid(0, 0, nz)


def _stand(grid, x, y, z_hint):
    """The walkable cell under (x, y) nearest z_hint, and where the body rests on it."""
    ix, iy, _ = grid.cell_at((x, y, 0))
    zs = grid.columns.get((ix, iy), ())
    if not zs:
        return None
    iz = min(zs, key=lambda z: abs(grid.centre((ix, iy, z))[2] - z_hint))
    return grid.centre((ix, iy, iz))[2]


def test_a_floor_thinner_than_a_cell_is_still_a_floor(kern, tmp_path):
    grid = analysis.build_navgrid(_mixed_map(kern, tmp_path), cell=16, profile_id=PROFILE)
    # The slab from 60 to 64 holds no cell centre at cell 16 (56 and 72 are either side).
    assert _stand(grid, 160, 375, 64) == pytest.approx(64)


def test_the_body_rests_on_an_off_grid_floor_and_steps_onto_rubble(kern, tmp_path):
    grid = analysis.build_navgrid(_mixed_map(kern, tmp_path), cell=16, profile_id=PROFILE)
    # Floor top at 5: the cell boundary is 0, the body stands at 5, not inside the brush.
    assert _stand(grid, 344, 160, 5) == pytest.approx(5)
    # Within half the player's width of the 1-unit strip, the body rests on top of it.
    width = analysis.movement_constants(PROFILE).player_width.value
    near = 400 - width / 2 + 4
    assert _stand(grid, near, 160, 6) == pytest.approx(6)


def test_a_slope_is_stood_on(kern, tmp_path):
    """The wedge rises 64 over 128 (27 degrees): its whole run is standable, not body-blocked
    as it was when the box was tested at the cell boundary under the surface."""
    grid = analysis.build_navgrid(_mixed_map(kern, tmp_path), cell=16, profile_id=PROFILE)
    heights = [_stand(grid, x, 64, 64 * (1 - x / 128)) for x in (40, 56, 72, 88, 104)]
    assert all(h is not None for h in heights), heights
    assert heights == sorted(heights, reverse=True), "the body sits higher up the slope"


def test_a_full_size_map_fits_the_default_cap(kern, tmp_path):
    """ET: Legacy's Supply Depot is 6912 x 6912 x 3456 units: 40M cells at cell 16, over the
    old 12M dense cap. The sparse grid builds it at the default cap."""
    path = write_map(
        tmp_path,
        [
            box((-3456, -3456, -16), (3456, 3456, 0)),
            box((-3456, -3456, 3424), (3456, 3456, 3440)),
            box((0, 0, 0), (64, 64, 64)),
        ],
    )
    grid = analysis.build_navgrid(path, cell=16, profile_id=PROFILE)
    assert grid.counts["cells"] > 40_000_000 > analysis.DENSE_MAX_CELLS
    assert grid.counts["walkable_cells"] > 180_000
    assert grid.occupancy.nbytes() < 4_000_000


def test_a_kernel_without_the_sparse_pass_falls_back_to_the_dense_cap(kern, tmp_path, monkeypatch):
    path = write_map(tmp_path, room(0, 0, 4096, 4096))
    monkeypatch.setattr(analysis, "_sparse_kernel_available", lambda: False)
    with pytest.raises(analysis.AnalysisError) as excinfo:
        analysis.build_navgrid(path, cell=4, profile_id=PROFILE)
    assert "kernel:build" in str(excinfo.value)


@pytest.mark.skipif(not os.environ.get("NRC_SLOW_TESTS"), reason="set NRC_SLOW_TESTS=1")
@pytest.mark.parametrize("name", ["oasis", "goldrush", "radar", "battery", "fueldump", "railgun"])
def test_every_real_map_builds_at_cell_16(kern, name):
    path = REPO / f"corpus/real/{name}_final.map"
    if not path.is_file():
        pytest.skip("corpus not imported")
    grid = analysis.build_navgrid(path, cell=16, profile_id=PROFILE)
    assert grid.counts["walkable_cells"] > 10_000
