"""The server object, the session, and the preconditions every tool shares.

Separated from `server.py` so the tool modules in `surface/` can import what they need
without importing the module that imports *them*. `server.py` binds the surface together;
this is what the surface binds to.
"""

from __future__ import annotations

import inspect
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer as _Server
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from . import profiles
from .project_state import digest, read_optional

INSTRUCTIONS = """\
Authoring, analysing and shipping levels for a Quake-3-engine game.

**One open map.** Almost everything here operates on a session map opened with `map_open`,
rather than taking a path each time. Open a map first; tools that need one say so.

**Stage edits before saving.** `map_new` creates a file; map/script/solid edits are then
staged until `map_save`. Save checks round-trip fidelity, playable space and external edits.
`map_checkpoint` / `map_restore` undo staged work together; saves clear checkpoints.

**Confidence is enforced, not advertised.** Findings carry a `confidence`, and a rule the
profile marks unverified is downgraded so it can never fail a build. Three of the four
spawn rules the design document called verified turned out to be wrong, and one would have
failed correct maps — read `nrc://corrections` before trusting a rule.

**Use the supported representations.** `patch_commit` authors cylinders, arches and bevels;
patches do not seal a map. Shader contents determine their player collision, which the analysis
grid omits; clip brushes can simplify collision. There is no measured dimension
corpus. Travel estimates state their speed and reinforcement assumptions; they are not
engine measurements. `nrc://conventions` lists the rest.
"""


def compact_json(text: str) -> str:
    """JSON text re-serialized without indentation; anything that is not JSON, unchanged.

    The SDK renders a returned dict with `indent=2`, and on the gameplay reports that
    whitespace was ~30% of every response. A model reads compact JSON exactly as well, so
    the indentation is pure cost. Re-serializing a parsed document is lossless: same keys,
    same order, same numbers.
    """
    stripped = text.lstrip()
    if not stripped.startswith(("{", "[")):
        return text
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except ValueError:
        return text


def schema_without_titles(schema: Any) -> Any:
    """A JSON schema minus pydantic's generated `title`s, which only restate the names.

    Every parameter got `"title": "Profile Id"` for `profile_id`, and every tool
    `"title": "<name>Arguments"`: about a quarter of the listed input schema, and not one
    of them says anything the property name does not. Keys *inside* `properties` are
    parameter names, so a parameter that is itself called `title` survives.
    """
    if isinstance(schema, list):
        return [schema_without_titles(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k == "title" and isinstance(v, str):
            continue
        if k in ("properties", "$defs", "definitions") and isinstance(v, dict):
            out[k] = {name: schema_without_titles(sub) for name, sub in v.items()}
        else:
            out[k] = schema_without_titles(v)
    return out


class _CompactServer(_Server):
    """The SDK server, sending tool results as compact JSON, one tool call at a time.

    The SDK runs each request in its own task and every sync tool on a worker thread
    (`FuncMetadata.call_fn_with_arg_validation`: `anyio.to_thread.run_sync`), so two calls
    from one client run at once. Every tool shares one `Session`: `map_open` swapping the map
    while `map_save` reads its baseline, or two edits each staging a sidecar they read before
    the other wrote, would lose work. Calls therefore queue on one lock. The cost: a long call
    - a compile, an engine run - holds the others until it returns; the server itself still
    answers pings and listings meanwhile.
    """

    _calls: anyio.Lock | None = None

    async def call_tool(self, name, arguments, context=None):
        if self._calls is None:
            self._calls = anyio.Lock()
        async with self._calls:
            result = await super().call_tool(name, arguments, context)
        if isinstance(result, CallToolResult):
            result.content = [
                TextContent(type="text", text=compact_json(c.text), annotations=c.annotations)
                if isinstance(c, TextContent)
                else c
                for c in result.content
            ]
        return result


mcp = _CompactServer("nrc-mcp", instructions=INSTRUCTIONS)


def tool(
    group: str,
    title: str,
    *,
    writes: bool = False,
    destructive: bool = False,
    idempotent: bool | None = None,
    open_world: bool = False,
):
    """Register a tool, with the group and safety hints a client needs to decide anything.

    A thin adapter over `mcp.tool()`, not a registry: the SDK's `ToolManager` already stores
    every tool, and duplicating that here would mean keeping a parallel copy in step with it
    by hand. All this does is make the arguments impossible to forget.

    And forgetting is the failure that matters. `readOnlyHint` is what a host reads when it
    decides whether to run something without asking, so a tool that writes and does not say
    so gets auto-approved. There is no default for it here: every tool states `writes`
    explicitly, or the annotation would be a guess with a safety consequence.

    Args:
        group: which part of the surface this belongs to. Rides in `_meta` and is what
            `NRC_MCP_GROUPS` filters on.
        title: a short human-readable name, shown by clients in place of the function name.
        writes: the tool changes something — the open map, the sidecar, a file, a build
            artefact. `readOnlyHint` is its inverse.
        destructive: the change can *lose* something that was there. Narrower than `writes`:
            adding a brush is a write, overwriting a `.map` is destructive.
        idempotent: calling twice with the same arguments is the same as calling once.
            Defaults to true for readers and false for writers, which is right far more
            often than not; pass it explicitly where that is wrong.
        open_world: the tool reaches outside this machine.
    """
    # No `title` here: the tool's own `title` carries it, and clients read that first.
    annotations = ToolAnnotations(
        readOnlyHint=not writes,
        destructiveHint=destructive,
        idempotentHint=(not writes) if idempotent is None else idempotent,
        openWorldHint=open_world,
    )

    def register(fn):
        # The docstring is the agent-facing description. Python keeps its source
        # indentation, which every client would otherwise receive and pay for on every
        # line; `cleandoc` removes exactly that and nothing else.
        mcp.tool(
            title=title,
            description=inspect.cleandoc(fn.__doc__ or ""),
            annotations=annotations,
            meta={"group": group},
        )(fn)
        registered = mcp._tool_manager.get_tool(fn.__name__)
        registered.parameters = schema_without_titles(registered.parameters)
        return fn

    return register


@dataclass
class Session:
    """The one open map, plus what we know about it."""

    map: Any = None
    path: Path | None = None
    grid: int = 8
    warnings: list[str] = field(default_factory=list)
    #: The round-trip result taken at `map_open`, before anything could edit the map.
    #:
    #: This, not the live one, is the gate. Once a tool has added a brush the live check
    #: differs by construction — that is the edit — and reading it as a kernel failure made
    #: `map_save` refuse every real change and taught callers to pass the override every
    #: time, which switched the guard off for the case it exists to catch.
    opened_round_trip: dict | None = None
    #: Fingerprints of the brushes the map had at `map_open`. Grid findings on those are the
    #: map's history, not this session's work, and `validate` reports them as warnings.
    opened_brushes: frozenset[int] | None = None
    baseline_files: dict[Path, bytes | None] = field(default_factory=dict)
    pending_files: dict[Path, bytes] = field(default_factory=dict)
    checkpoints: dict[str, tuple[str, dict[Path, bytes]]] = field(default_factory=dict)
    grids: dict[tuple, Any] = field(default_factory=dict)
    last_compile: dict | None = None
    #: The map's source as last opened or saved, to tell an unsaved edit from a file the
    #: kernel merely re-serializes differently.
    saved_source: str | None = None

    def unsaved(self) -> list[str]:
        """What `map_save` has not written yet: the map's own edits and each staged file."""
        if self.map is None:
            return []
        out = ["map edits"] if self.map.source() != self.saved_source else []
        return out + [p.name for p in sorted(self.pending_files)]

    def read_file(self, path: Path) -> bytes | None:
        path = path.resolve()
        if path not in self.baseline_files:
            self.baseline_files[path] = read_optional(path)
        return self.pending_files.get(path, self.baseline_files[path])

    def stage_file(self, path: Path, data: bytes) -> None:
        path = path.resolve()
        self.read_file(path)
        self.pending_files[path] = data

    def receipt(self) -> dict:
        source = self.require().source().encode("utf-8")
        return {
            "path": str(self.path),
            "sha256": digest(source),
            "dirty": source != self.baseline_files.get(self.path.resolve()) if self.path else True,
            "companions": {
                str(p): digest(data)
                for p, data in sorted({**self.baseline_files, **self.pending_files}.items())
                if data is not None and p != self.path
            },
            "staged_files": [str(p) for p in sorted(self.pending_files)],
        }

    def require(self):
        if self.map is None:
            raise ToolError(
                "no map is open — call map_open(path) first. "
                "Use task_list() to see what else the project can do."
            )
        return self.map


SESSION = Session()


def active_profile() -> str:
    return os.environ.get("NRC_PROFILE", "")


def _require_path() -> Path:
    """The open map's path on disk, for tools that read the file rather than the parse."""
    if SESSION.path is None:
        raise ToolError("no map is open — call map_open(path) first.")
    return SESSION.path


def _profile_error_text(exc: Exception) -> str:
    """A profile failure, with the list of profiles that would have worked.

    `ToolError` carries a string and nothing else, so anything the caller needs in order to
    recover has to be *in* that string. These sites used to return
    `{"error": ..., "available": [...]}`; dropping the second key while converting them
    would have quietly turned an actionable error into a dead end.
    """
    available = ", ".join(profiles.available()) or "none on disk"
    return f"{exc}. Profiles available: {available}"


def _require_profile(profile_id: str | None) -> str:
    """The profile id to use, or an error naming what there is to choose from.

    Guessing here is not an option: the movement constants live in the profile, and the
    design document's own standing height was wrong for this game by 13 units (§7.4,
    `nrc://corrections`). A tool with no profile has no numbers, not default numbers.
    """
    pid = profile_id or active_profile()
    if not pid:
        available = ", ".join(profiles.available()) or "none on disk"
        raise ToolError(
            "no profile selected, and the movement constants live there. Set NRC_PROFILE "
            f"or pass profile_id. Available: {available}"
        )
    return pid


def _add_entity(keys: list[tuple[str, str]]) -> int:
    """Append an entity to the open map and return its index — the one path that does.

    The map is edited in place, so it keeps the path `map_save` writes to and the bytes it
    was opened from, which is what lets `map_save` see the edit and check playable space.
    """
    m = SESSION.require()
    try:
        return m.add_entity(keys)
    except ValueError as e:
        raise ToolError(str(e)) from e


def _kernel():
    from .kernel import kernel

    return kernel()
