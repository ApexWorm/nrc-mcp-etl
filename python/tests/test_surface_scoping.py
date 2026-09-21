"""Narrowing the tool surface with NRC_MCP_GROUPS and NRC_MCP_READ_ONLY.

Every test here reloads `nrc_mcp.server` in a fresh module, because selecting a surface
*removes* tools from the server and there is exactly one server object per process. A test
that narrowed the shared one would quietly shrink the surface for everything after it.
"""

from __future__ import annotations

import importlib
import sys

import pytest

#: Modules that hold registration state. The server object lives in `runtime`, and the
#: tools register themselves as `surface.*` is imported — so reloading `server` alone would
#: re-run the binding against an already-populated server, leaving whatever the previous
#: test removed still removed.
_STATEFUL = ("nrc_mcp.server", "nrc_mcp.runtime", "nrc_mcp.surface")


def _purge() -> None:
    for name in [n for n in sys.modules if n.startswith(_STATEFUL)]:
        del sys.modules[name]


@pytest.fixture
def server():
    """A freshly imported server, with the full surface registered."""
    _purge()
    mod = importlib.import_module("nrc_mcp.server")
    yield mod
    _purge()


def _names(server) -> set[str]:
    return {t.name for t in server.mcp._tool_manager.list_tools()}


def test_the_default_surface_is_everything(server):
    """No configuration must change nothing: an existing client keeps what it had."""
    before = _names(server)
    result = server.select_surface(None, read_only=False)
    assert set(result["kept"]) == before
    assert result["dropped"] == []


def test_a_group_selection_keeps_only_that_group(server):
    result = server.select_surface("sculpt", read_only=False)
    kept = _names(server)
    assert "solid_commit" in kept
    assert "map_save" not in kept, "map_save is in the map group, not sculpt"
    assert "bench_run" not in kept
    assert set(result["kept"]) == kept


def test_several_groups_can_be_named(server):
    server.select_surface("map,render", read_only=False)
    kept = _names(server)
    assert {"map_open", "render_topdown"} <= kept
    assert "solid_commit" not in kept


def test_read_only_drops_every_writer(server):
    """The point of the switch: a review session that cannot alter anything."""
    server.select_surface(None, read_only=True)
    kept = _names(server)
    for writer in ("map_save", "solid_commit", "pack_pk3", "task_run", "model_place"):
        assert writer not in kept, f"{writer} survived --read-only"
    # The read halves of the split tools are exactly what should remain.
    for reader in ("map_stats", "solid_param_preview", "model_place_plan", "upstream_diff"):
        assert reader in kept


def test_read_only_combines_with_a_group_filter(server):
    """Both filters at once, which is where an over-eager intersection would empty the set."""
    server.select_surface("sculpt", read_only=True)
    kept = _names(server)
    assert "solid_param_preview" in kept
    assert "solid_commit" not in kept and "solid_edit_param" not in kept
    assert "map_open" not in kept


def test_an_unknown_group_refuses_to_start(server):
    """A typo must not quietly serve a surface nobody asked for."""
    with pytest.raises(server.SurfaceError, match="unknown tool group"):
        server.select_surface("sculpt,scuplt", read_only=False)
    # And the error has to say what would have worked.
    with pytest.raises(server.SurfaceError, match="Available:"):
        server.select_surface("nonsense", read_only=False)


def test_an_empty_selection_refuses_to_start(server):
    """`NRC_MCP_GROUPS=,` is a mistake, not a request for an empty server."""
    with pytest.raises(server.SurfaceError, match="names no groups"):
        server.select_surface(",", read_only=False)


def test_no_group_is_made_entirely_of_writers(server):
    """Every group keeps something under `--read-only`, so no combination empties by itself.

    Worth pinning: it is what makes `--read-only` usable with any group filter, and adding
    an all-writer group later would break that quietly.
    """
    for group in server.all_groups():
        readers = [
            t.name
            for t in server.mcp._tool_manager.list_tools()
            if (t.meta or {}).get("group") == group and t.annotations.read_only_hint
        ]
        assert readers, f"group {group!r} is entirely writers, so read-only empties it"


def test_a_selection_that_leaves_nothing_refuses_to_start(server):
    """A zero-tool server is indistinguishable from a broken one, so it is refused.

    No group is all-writers today (see above), so the situation is constructed: narrow to
    one group, drop its readers, then ask for read-only. The guard has to hold for the
    group that eventually *is* all writers.
    """
    server.select_surface("maintenance", read_only=False)
    for t in list(server.mcp._tool_manager.list_tools()):
        if t.annotations.read_only_hint:
            server.mcp.remove_tool(t.name)

    with pytest.raises(server.SurfaceError, match="no tools at all"):
        server.select_surface(None, read_only=True)


def test_the_group_vocabulary_is_derived_not_hardcoded(server):
    groups = server.all_groups()
    assert "sculpt" in groups and "maintenance" in groups
    assert "" not in groups, "a tool with no group would make the vocabulary meaningless"


def test_scoping_actually_saves_context(server):
    """The reason any of this exists: fewer tools is fewer tokens in every session."""
    full = sum(len(t.description or "") for t in server.mcp._tool_manager.list_tools())
    server.select_surface("map,render,sculpt", read_only=False)
    narrowed = sum(len(t.description or "") for t in server.mcp._tool_manager.list_tools())
    assert narrowed < full * 0.7, (
        f"a design-only surface still costs {narrowed} of {full} description characters"
    )
