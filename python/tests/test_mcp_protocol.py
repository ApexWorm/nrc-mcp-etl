"""The surface as a *client* sees it, over a real stdio connection.

Every other test in this repository calls the tool functions directly as Python. That
misses everything the MCP protocol layer adds — titles, safety annotations, the `_meta`
group, output schemas, prompts, resource templates — because none of it exists until the
SDK builds the wire representation. A tool can be perfectly correct as a function and
still reach the agent unannotated or not at all.

So this file is the only place that asserts on what actually crosses the wire. It spawns
`python -m nrc_mcp` as a subprocess and talks to it the way any client would.

It needs the compiled kernel, because the server refuses to start without one
(`nrc_mcp.server.main`). Without it the module skips rather than fails: a missing kernel
is a "run `mise run kernel:build`" problem, not a protocol regression.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "python" / "src"


def _kernel_available() -> bool:
    return (SRC / "nrc_py.so").is_file()


pytestmark = pytest.mark.skipif(
    not _kernel_available(),
    reason="the compiled kernel is absent; run `mise run kernel:build`",
)


def _connect(fn):
    """Run `fn(session)` against a freshly spawned server, and return its result."""
    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    env.setdefault("NRC_ROOT", str(ROOT))
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "nrc_mcp"], env=env, cwd=str(ROOT)
    )

    async def main():
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            return await fn(session)

    return anyio.run(main)


@pytest.fixture(scope="module")
def listing() -> dict:
    """One server spawn for the whole module — starting it costs about a second."""

    async def gather(session):
        tools = await session.list_tools()
        resources = await session.list_resources()
        templates = await session.list_resource_templates()
        try:
            prompts = (await session.list_prompts()).prompts
        except Exception:  # noqa: BLE001 — a server with no prompts may refuse the method
            prompts = []
        return {
            "tools": tools.tools,
            "resources": resources.resources,
            "templates": templates.resource_templates,
            "prompts": prompts,
        }

    return _connect(gather)


def test_the_server_starts_and_lists_tools(listing):
    assert listing["tools"], "the server exposed no tools at all"


def test_the_protocol_surface_matches_the_python_surface(listing):
    """What crosses the wire must be what the module registered — no more, no less."""
    from nrc_mcp import server

    over_the_wire = sorted(t.name for t in listing["tools"])
    assert over_the_wire == sorted(server.TOOL_NAMES)


def test_every_tool_reaches_the_client_with_a_description(listing):
    """Description is how an agent chooses. One missing is a tool that cannot be picked."""
    bare = [t.name for t in listing["tools"] if not (t.description or "").strip()]
    assert not bare, f"tools that arrive with no description: {bare}"


def test_every_tool_has_an_input_schema(listing):
    missing = [t.name for t in listing["tools"] if not t.input_schema]
    assert not missing, f"tools with no input schema: {missing}"


def test_the_documented_resources_resolve(listing):
    """The static resources the README and `--describe` promise a client."""
    uris = {str(r.uri) for r in listing["resources"]}
    for expected in ("nrc://tasks", "nrc://conventions", "nrc://corrections"):
        assert expected in uris, f"{expected} is not served; got {sorted(uris)}"


def test_the_profile_resource_is_a_template(listing):
    """`nrc://profile/{profile_id}` is parameterised, so it lists as a template."""
    patterns = {t.uri_template for t in listing["templates"]}
    assert any("nrc://profile/" in p for p in patterns), f"got templates {sorted(patterns)}"


def test_reading_a_resource_returns_content():
    async def read(session):
        return await session.read_resource("nrc://conventions")

    result = _connect(read)
    text = "".join(getattr(c, "text", "") for c in result.contents)
    assert "representation tiers" in text, "nrc://conventions did not serve conventions.md"


# ---------------------------------------------------------------------------
# Annotations and grouping
# ---------------------------------------------------------------------------
#
# None of this is visible from Python: `annotations` and `_meta` are built by the SDK when
# it renders a tool for the wire. A missing `readOnlyHint` is invisible to every other test
# in the repository and highly visible to a host deciding whether to auto-approve a call.

#: Tools that change something. Everything else must arrive as read-only.
WRITERS = {
    "map_new",
    "map_save",
    "entity_place",
    "entity_set_keys",
    "entity_remove_keys",
    "script_set_event",
    "script_remove",
    "objective_place",
    "solid_commit",
    "brush_set_detail",
    "solid_edit_param",
    "model_place",
    "pack_pk3",
    "arena_write",
    "objdata_write",
    "commandmap_make",
    "levelshot_make",
    "compile_map",
    "compile_ab",
    "task_run",
    "bench_run",
    "upstream_fetch",
    "pr_plan_status",
}

#: Tools that can lose something that was already there.
DESTRUCTIVE = {
    "map_save",
    "entity_set_keys",
    "entity_remove_keys",
    "script_set_event",
    "script_remove",
    "arena_write",
    "objdata_write",
    "commandmap_make",
    "levelshot_make",
    "task_run",
}


def test_every_tool_carries_a_title_and_all_four_hints(listing):
    for t in listing["tools"]:
        ann = t.annotations
        assert ann is not None, f"{t.name} has no annotations at all"
        assert (t.title or "").strip(), f"{t.name} has no title"
        for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
            assert getattr(ann, hint) is not None, f"{t.name} leaves {hint} unset"


def test_read_only_is_declared_honestly(listing):
    """The annotation a host reads before deciding whether to ask permission.

    A writer that arrives as read-only gets auto-approved, which is how a tool that edits
    the open map ends up running without anyone being asked.
    """
    claimed_readonly = {t.name for t in listing["tools"] if t.annotations.read_only_hint}
    lying = WRITERS & claimed_readonly
    assert not lying, f"these tools change something but claim to be read-only: {sorted(lying)}"

    over_claimed = {t.name for t in listing["tools"] if not t.annotations.read_only_hint} - WRITERS
    assert not over_claimed, (
        f"these are marked as writing but are not in the writer set: {sorted(over_claimed)}"
    )


def test_destructive_is_narrower_than_writing(listing):
    marked = {t.name for t in listing["tools"] if t.annotations.destructive_hint}
    assert marked == DESTRUCTIVE, f"destructive set drifted: {sorted(marked)}"
    assert marked <= WRITERS, "a tool cannot be destructive without writing"


def test_the_one_networked_tool_says_so(listing):
    """`openWorldHint` is about reaching off this machine, and exactly one tool does."""
    open_world = {t.name for t in listing["tools"] if t.annotations.open_world_hint}
    assert open_world == {"upstream_fetch"}, f"got {sorted(open_world)}"


def test_every_tool_belongs_to_a_group(listing):
    """`_meta.group` is what NRC_MCP_GROUPS filters on, so an ungrouped tool is unreachable."""
    groups = {}
    for t in listing["tools"]:
        meta = t.meta or {}
        group = meta.get("group")
        assert group, f"{t.name} carries no group in _meta"
        groups.setdefault(group, []).append(t.name)
    assert set(groups) == {
        "map",
        "render",
        "sculpt",
        "assets",
        "compiling",
        "gameplay",
        "shipping",
        "project",
        "maintenance",
    }, f"group vocabulary drifted: {sorted(groups)}"


def test_the_server_sends_its_conventions_at_initialize():
    """The three conventions have to arrive before the first call, not on request."""

    async def grab(session):
        return session  # the handshake result is captured below

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "nrc_mcp"], env=env, cwd=str(ROOT)
    )

    async def main():
        async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
            return await session.initialize()

    result = anyio.run(main)
    text = result.instructions or ""
    assert "One open map" in text
    assert "Nothing writes without being asked" in text
    assert "Confidence is enforced" in text


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_the_spec_prompts_are_served(listing):
    """§8.4's four names. §11.3 names `asset_pipeline` as a self-dev lever, so the names
    are an interface, not a label — renaming one breaks a reference elsewhere."""
    names = {p.name for p in listing["prompts"]}
    assert names == {
        "asset_pipeline",
        "optimize_pass",
        "new_map_bootstrap",
        "gametype_retrofit",
    }, f"got {sorted(names)}"


def test_every_prompt_has_a_title_and_description(listing):
    for p in listing["prompts"]:
        assert (p.title or "").strip(), f"{p.name} has no title"
        assert (p.description or "").strip(), f"{p.name} has no description"


def test_a_prompt_renders_with_its_argument():
    async def get(session):
        return await session.get_prompt("asset_pipeline", {"feature": "a rooftop water tank"})

    result = _connect(get)
    text = "".join(getattr(m.content, "text", "") for m in result.messages if hasattr(m, "content"))
    assert "a rooftop water tank" in text
    assert "asset_plan" in text, "the prompt must name the decision step"
    assert "Patches cannot be authored" in text, "it must still disclose the limit"


def test_the_solid_ir_reference_is_a_resource(listing):
    uris = {str(r.uri) for r in listing["resources"]}
    assert "nrc://solid-ir" in uris, f"got {sorted(uris)}"
