"""The gameplay analysis tools, as the MCP surface exposes them.

`python/tests/test_analysis.py` covers the analysis engine directly. Nothing covered the
thin tool layer on top of it, and that is precisely where `navgrid_stats` was reading an
attribute `NavGrid` does not have — running the whole voxelization and then reporting zero
walkable cells, every time, on every map.

The lesson generalises: a tool is not tested by testing the module underneath it.
"""

from __future__ import annotations

import os

import pytest
from nrc_mcp import analysis, server
from test_analysis import room, write_map


@pytest.fixture(scope="module")
def pid() -> str:
    from nrc_mcp import profiles

    available = profiles.available()
    if not available:
        pytest.skip("no profiles on disk")
    # The profile the suite runs against; mise sets it, so a second profile on disk
    # does not silently change which one is tested.
    return os.environ.get("NRC_PROFILE") or available[0]


@pytest.fixture
def open_room(tmp_path, pid):
    """A single 480x480 room, opened in the session the tools read from."""
    path = write_map(tmp_path, room(0, 0, 480, 480, height=256), name="room.map")
    server.map_open(str(path))
    yield path
    server.SESSION.map = None
    server.SESSION.path = None


def test_navgrid_stats_reports_the_space_a_player_can_stand_in(open_room, pid):
    """The W-series regression: a walkable room must not report zero walkable cells.

    This failed before the fix by reading `NavGrid.walkable` — an attribute that has never
    existed — through a `getattr` default, so the answer was always zero and always
    confident about it.
    """
    result = server.navgrid_stats(cell=16, profile_id=pid)

    assert "error" not in result, result
    assert result["counts"]["walkable_cells"] > 0, (
        "a 480x480 room 256 units tall is walkable; reporting zero means the tool is not "
        "reading the grid it just spent the time to build"
    )
    assert result["cell"] == 16


def test_navgrid_stats_agrees_with_the_engine_underneath(open_room, pid):
    """The tool must report the grid's own numbers, not a paraphrase of them."""
    direct = analysis.build_navgrid(open_room, cell=16, profile_id=pid)
    result = server.navgrid_stats(cell=16, profile_id=pid)
    assert result["counts"]["walkable_cells"] == direct.counts["walkable_cells"]
    assert result["dims"] == list(direct.dims)


# ---------------------------------------------------------------------------
# The navgrid cache
# ---------------------------------------------------------------------------


def test_the_five_reports_share_one_grid(open_room, pid):
    """Voxelizing is the expensive step; asking five questions must not pay it five times.

    Asserted with the cache's own counters rather than a stopwatch: a timing comparison
    cannot tell a cache hit from a warm page cache or a faster machine.
    """
    analysis.clear_navgrid_cache()
    for _ in range(5):
        server.navgrid_stats(cell=16, profile_id=pid)

    stats = analysis.navgrid_cache_stats()
    assert stats["misses"] == 1, f"built the grid {stats['misses']} times, expected once"
    assert stats["hits"] == 4


def test_editing_the_map_invalidates_the_cached_grid(open_room, pid, tmp_path):
    """A cache keyed on a path alone would answer for the file as it used to be."""
    analysis.clear_navgrid_cache()
    first = server.navgrid_stats(cell=16, profile_id=pid)["counts"]["walkable_cells"]

    # Rewrite the same path with a larger room; mtime and size both move.
    open_room.write_text("".join(room(0, 0, 960, 480, height=256)).join(("{\n", "}\n")))
    server.map_open(str(open_room))
    second = server.navgrid_stats(cell=16, profile_id=pid)["counts"]["walkable_cells"]

    assert second != first, "the grid did not change when the file did"


def test_an_unsaved_map_is_never_served_from_the_cache(tmp_path, pid):
    """The defect this cache was one line away from introducing.

    `playspace.diff` measures an unsaved edit against the file it is about to overwrite, and
    asks for both grids **under the same path with the same mtime** — the only difference is
    `game_map=`. Key the cache on the file and the second call returns the first's grid, the
    save gate compares a map against itself, and it passes unconditionally. It would have
    looked like a working gate forever.
    """
    from nrc_mcp import kernel
    from nrc_mcp.playspace import _grid_for

    # `before` is a small room on disk. `after` is a much larger one, held only in memory
    # and deliberately given the *same* path — which is exactly the shape `map_save` uses.
    path = write_map(tmp_path, room(0, 0, 480, 480, height=256), name="before.map")
    bigger = write_map(tmp_path, room(0, 0, 1280, 480, height=256), name="after.map")
    parsed = kernel.parse_map(bigger.read_text())

    analysis.clear_navgrid_cache()
    on_disk = _grid_for(str(path), 16, pid)
    in_memory = _grid_for((str(path), parsed), 16, pid)

    assert analysis.navgrid_cache_stats()["bypassed"] >= 1, (
        "the in-memory side must bypass the cache entirely, not be keyed around it"
    )
    assert in_memory is not on_disk, "the unsaved map was served the file's cached grid"
    assert in_memory.counts["walkable_cells"] > on_disk.counts["walkable_cells"], (
        "the in-memory map is over twice the floor area of the file at the same path, so a "
        "cache that keyed on the path would report them identical — and map_save's "
        "playable-space gate would compare the map against itself and always pass"
    )
