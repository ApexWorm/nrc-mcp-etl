# nrc-mcp

An MCP server for designing, sculpting, optimizing and shipping levels for
[NetRadiant-custom](https://github.com/Garux/netradiant-custom), targeting Urban Terror and ET: Legacy.

`nrc-mcp` owns the `.map` file. It parses and writes it losslessly, derives geometry with exact
arithmetic, drives `q3map2`, and exposes all of it to an agent as **80 tools, 7 resources and 4 prompts** —
grouped, safety-annotated, and narrowable to the subset a session actually needs.
A human gets the same capabilities through a CLI and a set of `mise` tasks.

It is not an editor and not a plugin. It reads and writes the same files NetRadiant does, so you can
keep the editor open and use both.

---

## Contents

- [Requirements](#requirements) · [Install](#install) · [Configure](#configure)
- [Connect an MCP client](#connect-an-mcp-client)
- [Your first session](#your-first-session)
- [Tool reference](#tool-reference) · [Resources](#resources)
- [Reading the output](#reading-the-output) — confidence, and what a warning means
- [Command line](#command-line)
- [How it works](#how-it-works)
- [Limits](#limits) — what it will refuse to do
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)

---

## Requirements

**Always needed**

- [mise](https://mise.jdx.dev) — the only build/run interface. It installs and pins everything else.
- A POSIX shell. Linux, macOS or WSL. Native Windows is untested.

That is enough for parsing, validation, rendering, sculpting, analysis and the whole test suite.
No GPU, no display, no game install, no editor.

**Needed only to compile, package or read BSPs**

- `q3map2` — from a NetRadiant-custom install, or built from source with `mise run vendor:build`
  (`vendor:build-tools` skips the Qt5 editor and builds only the compilers, which is what CI
  and this project need).
- A game installation, for `-fs_basepath`. For Urban Terror that is the directory containing
  `q3ut4/`.

Upstream does not build unmodified on Apple Silicon; two portability patches live in
[`contrib/patches/`](contrib/patches/README.md) and are applied automatically by the vendor
build. `mise run vendor:check` syntax-checks all 116 translation units in a few seconds
without linking, which is the quick way to find out whether a new platform needs a third.
On macOS the build also wants `brew install pkg-config glib libpng jpeg-turbo assimp minizip`.

**Optional**

- NetRadiant-custom itself, if you want to see the map in the editor. Its gamepack is also the
  source the Urban Terror profile was extracted from.
- Blender, for the mesh handoff. `nrc-mcp` never launches it; it writes a brief and validates what
  comes back.

## Install

```sh
git clone <this repo> && cd nrc-mcp
mise trust                    # approve this repo's mise config
mise install                  # rust, python 3.12, uv — pinned
mise run bootstrap            # build the kernel, generate the corpus
mise run test                 # prove it works before trusting anything
```

`mise run test` should end with:

```
  syntactic: 49/49 byte-identical (3343 brushes, 10 patches)
  semantic: 6/6 compiled BSPs identical

GATE GREEN
```

Alongside 265 Rust tests, 272 Python tests and 5 scenario tapes. The `semantic` line only appears if a compiler is
configured; without one it is skipped, not failed.

**If the gate is red, stop.** Nothing downstream of the kernel is trustworthy while a map does not
survive a round-trip. See [Troubleshooting](#troubleshooting).

## Configure

Machine-specific paths go in **`mise.local.toml`**, which is gitignored. Nothing in `mise.toml`
needs editing.

Two game profiles ship: `urt43` (Urban Terror 4.3, the default) and `etl` (ET: Legacy). The
profile decides the entity rules, the movement constants and the `-game` q3map2 is run with.

```toml
[env]
# Urban Terror. The basepath is the directory that CONTAINS q3ut4/, not q3ut4 itself.
NRC_FS_BASEPATH = "/mnt/c/Program Files/UrbanTerror43"

# The compiler.
Q3MAP2 = "/mnt/c/Program Files/NetRadiant/NetRadiant-custom-20240309/q3map2.exe"

# Set to "windows" when q3map2 is a .exe reached from WSL. Omit on native Linux/macOS.
NRC_Q3MAP2_MODE = "windows"

# Your own .map sources. Read-only: corpus:import copies out, never writes back.
NRC_MAPSRC = "/mnt/c/Program Files/UrbanTerror43/q3ut4/maps"
```

```toml
[env]
# ET: Legacy. The basepath is the directory that CONTAINS etmain/.
NRC_PROFILE     = "etl"
NRC_FS_BASEPATH = "/mnt/c/Program Files (x86)/ETLegacy"
Q3MAP2          = "/mnt/c/Program Files/NetRadiant/NetRadiant-custom-20260114/q3map2.exe"
NRC_Q3MAP2_MODE = "windows"
```

| Variable | Required for | Notes |
| --- | --- | --- |
| `NRC_PROFILE` | — | Active game profile: `urt43` (default) or `etl`. Set it in `mise.local.toml`, or per command from the shell (`NRC_PROFILE=etl mise run test`); a `mise.local.toml` value wins over the shell. |
| `NRC_FS_BASEPATH` | compiling, packaging | Passed as `-fs_basepath`. No default; without it q3map2 uses its own. |
| `NRC_FS_GAME` | mods | Passed as `-fs_game` when set. |
| `Q3MAP2` | compiling, BSP reports | Defaults to the source build under `vendor/`. |
| `NRC_Q3MAP2_GAME` | — | Overrides the profile's `-game` (`quake3` for `urt43`, `et` for `etl`). |
| `NRC_Q3MAP2_MODE` | WSL only | `windows` makes every path translate through `wslpath`. |
| `NRC_MAPSRC` | `corpus:import` | Optional. The corpus works without your maps. |
| `NRC_SELFDEV` | — | Set to `1` to allow the opt-in self-tuning loop. Off by default. |

`URT_BASEPATH` and `URT_MAPSRC`, the names these had before there was a second profile, are
still read when the new names are unset.

**What the ET profile checks.** `validate_profile` checks spawn markers, active spawns,
winner cameras, objective triggers and dangling `target` names. When `<map>.script` sits beside the `.map`, it
checks the script against the map: syntax the engine refuses, unknown events and actions, blocks
no entity runs, and `trigger` / `alertentity` / `setstate` / `setautospawn` arguments that name
nothing. Lifetime checks warn when a delayed continuation may outlive its entity, including
long waits in an explosive's death event. Short waits and uncertain control flow produce
informational risks rather than claimed execution failures.
It also checks the map's speaker script (`sound/maps/<map>.sps`) and sound scripts
(`sound/scripts/<map>.sounds`) beside it: their syntax, the sound files they name, sound script
names used by a `target_speaker` or by `playsound` and the voice announcements (only the shared
list and the map's own file are loaded, so another map's names do not resolve), and the speakers
`enablespeaker` / `disablespeaker` / `togglespeaker` name. `pack_pk3` packs both files and the
sounds of the map's own they name. `balance_report` and `spawn_safety` group ET spawns by their nearest
`team_WOLF_objective`, read objective ownership from spawnflags, and measure to
`trigger_objective_info` volumes. `ship_check` looks for the script, objdata, command map,
tracemap and location file. Prone routes use the main body box only; the legs and turning footprint remain unmodelled.
The `_lms` script variant is not selected automatically.

Check what resolved:

```sh
mise run info
```

### If q3map2 is a Windows .exe and you are in WSL

This is a supported and tested configuration, and the one thing worth knowing is that
**`q3map2.exe` cannot read `/home/...` paths and rejects UNC paths like
`\\wsl$\Ubuntu\home\...` outright.** `tools/q3map2.py` handles it: arguments are translated with
`wslpath -w`, and a map that lives on the Linux side is staged to a Windows-side directory before
compiling, then results are copied back. You do not have to do anything except set
`NRC_Q3MAP2_MODE = "windows"`.

The consequence to expect: compiling a map under `/home` is slower than compiling one already on
`/mnt/c`, because of the staging copy.

### Testing an ET map in the engine

`pack_pk3` writes `<map>_autopacked.pk3` next to the folder that holds `maps/`. Put it in an
`etmain/` under a folder of your choosing and point the server's home there. ET: Legacy's home
folder is `~/.etlegacy`, not the current directory, so pass `+set fs_homepath`. Pass
`+set dedicated 1` too: the default, 2, advertises the server to the public master servers.

```sh
mkdir -p ~/etl-test/etmain && cp mymap_autopacked.pk3 ~/etl-test/etmain/
etlded +set dedicated 1 +set fs_homepath ~/etl-test +set fs_basepath /path/to/etlegacy \
       +set sv_pure 0 +set developer 1 +set logfile 2 +devmap mymap
```

The log is written to `~/etl-test/legacy/etconsole.log`. "Can't find mapscripts/mymap.script" is
expected: the game looks there first, then in `maps/`.

Or let the repo do it, in a throwaway home folder, and read the log for you:

```sh
mise run game:smoke -- mymap_autopacked.pk3            # dedicated server: loads, script starts
mise run game:smoke -- mymap_autopacked.pk3 --client   # also join as a spectator (needs a display or Xvfb)
```

It fails on any line meaning the game stopped (a `G_Error`, a crash), and lists the game's
non-fatal complaints. Some errors only happen when a player connects - a map with no
intermission point runs fine until then - so use `--client` before shipping.

For gameplay checks, call `engine_playtest` through MCP. Read `nrc://playtest/etl` for the
supported actions, conditions and a bridge-building example. A minimal scenario is:

```python
engine_playtest(
    pk3="/path/to/mymap_autopacked.pk3", profile_id="etl",
    scenario={"steps": [
        {"action": "join", "team": "blue", "role": "engineer", "timeout": 40},
        {"action": "screenshot", "name": "alive_spawn"},
    ]},
)
```

Joins wait for a living player. `hold`, `wait` and `assert` can use fresh player, entity and
game-state predicates, so a construction test waits for `built=true` and then intermission
instead of guessing a frame count. Held controls are released on failure. Each run retains
its isolated game home, command/state trace, screenshots and result under `out/playtests/`;
the MCP response includes the receipt and up to three screenshot images. Position/aim setup
is explicitly recorded. This uses a single local client with POSIX console input and Lua
telemetry; it does not measure multiplayer balance.

`objective_playtests` writes those scenarios for you from the open map's objectives, whoever
placed them: it recognises each objective by the links and flags its kind's profile template
names, picks standing positions on floor the player box rests on and a walkable route reaches
from the team's spawns, and returns `runs` to pass to `engine_playtest` (a test whose script
ends the round closes its run) plus `skipped` objectives with the reason. For ET: Legacy,
dynamite is planted, aimed at and armed until the explosive is gone; constructibles and
command posts are built; a checkpoint is captured and retaken, proved by where the attackers
respawn; an escort vehicle is followed point by point, checked to stop without its escort,
and seen to the end of its path; a stolen item is picked up and delivered; and where the
script ends the round, the test waits for intermission and the winner it sets.
`mise run bench:dogfood -- objectives <example>` generates and runs them for an example map.

**Bots.** ET: Legacy servers fill teams with Omni-bot, and Omni-bot adds no bot at all to a map
it has no waypoints for. `bot_waypoints_write` seeds the open map's `.way` file from the
stand/crouch/prone grids and stages it at `omni-bot/et/nav/<map>.way` beside the map's folder:
copy that `et/` into the server's Omni-bot folder (the `omnibot_path` cvar; ET: Legacy's
`etlded_bot.x86_64.sh` uses `./legacy/omni-bot`), or pass `omnibot_path` to write it there.
Omni-bot reads waypoints only from its own folder, never from the map's pk3. Objective goals
(dynamite, construction, flags, checkpoints) come from the game itself; the waypoints only have
to reach them. `bot_playtest` then runs the packed map on a dedicated server with that file and
nothing else in the add-on's nav folder, adds bots and reports what they did. On the dogfood
maps (see `bench/dogfood/README.md`) the add-on loaded every seeded file, bots walked them,
crawled the prone vent and crouched through the tunnel, planted, built, captured and stole
the objectives, and escorted the convoy truck to its gate.

Jumps and ladders are seeded too: a ledge within the jump's rise or a gap the player box's arc
clears (at the slowest and fastest run speeds) gets a take-off waypoint flagged `JUMP` and a
one-way link to its landing, and a `surfaceparm ladder` brush gets `CLIMB` waypoints at its foot
and top; on the traversal course Axis bots jumped the ledge and the gap, climbed the ladder and
planted the radio. Brushes that change state are seeded in every state and gated with the
add-on's own runtime tests: a door a player opens gets a doorway waypoint flagged `DOOR` (plus
the team's flags for an `allowteams` door), blown walls and locked doors get `BLOCKWALL` links,
and an unbuilt constructible deck is no floor at round start, spanned by `BLOCKBRIDGE` links
that the add-on closes until it is built. No waypoint stands on a deck or inside a wall, since
the add-on would make it the objective's unreachable goal. The `gates` dogfood map shows all
three in the engine.

Beside the `.way` the same call writes `<map>_goals.gm` (map goals) and `<map>.gm` (the map
script), which the add-on runs at every map load. Defend spots are chosen per objective part
a team holds (the thing to blow up, a held flag, an item and its delivery place): standing
waypoints that see where the other team uses it, ranked by how much of that team's shortest
routes in they see, facing them. Snipe spots per team see the most of the enemy's routes at
long range from the team's own side. Pass `spots` for your own (DEFEND, ATTACK, CAMP, SNIPE,
MOBILEMG42, MOBILEMORTAR). The script hooks each objective's events by the trigger names the
game sends (its "Planted at <track>." popups and the map script's own `wm_announce` texts) and
switches spots per team: defenders of a destroyed objective stand down, a built one is
defended, a captured flag changes hands, a stolen item's defenders move to its delivery place.
A seeded set is a starting point; hand-tune it in Omni-bot's own editor.

## Connect an MCP client

The server speaks JSON-RPC on stdio.

**Claude Code**

```sh
claude mcp add nrc -- mise -C /absolute/path/to/nrc-mcp run mcp:serve
```

The `-C` is what lets the server start from anywhere; drop it if you always launch from inside the
repo.

**Any client with a JSON config** (`claude_desktop_config.json`, `.mcp.json`, …)

```json
{
  "mcpServers": {
    "nrc": {
      "command": "mise",
      "args": ["run", "mcp:serve"],
      "cwd": "/absolute/path/to/nrc-mcp"
    }
  }
}
```

`cwd` matters — it is how `mise` finds the config that supplies every path. If your client cannot set
a working directory, put it in the arguments instead:
`"args": ["-C", "/absolute/path/to/nrc-mcp", "run", "mcp:serve"]`.

**Verify without a client**

```sh
mise run mcp:tools      # print the whole surface, and the limits, and exit
mise run mcp:inspect    # the official MCP inspector, in a browser
```

`mise run mcp:serve` writes only protocol on stdout — diagnostics go to stderr — so it is safe to
point a client straight at it.

**Serve only what the session needs.** Every listed tool is sent to the model at the start of
every session (the full list is ~87 KB of JSON). Name a session preset in `NRC_MCP_GROUPS`
and the server lists only that subset:

| Preset | For | Tools | `tools/list` |
| --- | --- | --- | --- |
| `build` | blocking out and wiring a map: solids, patches, entities, objectives, script, textures, renders | 36 | ~40 KB |
| `analyse` | measuring how it plays: routes, balance, spawns, movement, sightlines, lighting, playable space | 25 | ~28 KB |
| `ship` | compiling, packing, release files, ship check, `objective_playtests`, `engine_playtest` and bot waypoints | 28 | ~30 KB |
| `level` | all three, start to finish | 62 | ~74 KB |

Each preset also carries the core every session uses: `map_open`/`map_new`/`map_save`,
checkpoints, `map_stats`, `query_entities`, `validate`, `validate_profile` and
`profile_summary`. The presets are drawn from the recorded dogfood sessions, which split into
exactly these phases. Names combine with each other and with the groups below
(`NRC_MCP_GROUPS=analyse,ship`, `NRC_MCP_GROUPS=build,assets`), and `NRC_MCP_READ_ONLY=1`
still applies on top. For Claude Code:

```sh
claude mcp add nrc-ship -e NRC_MCP_GROUPS=ship -- mise -C /absolute/path/to/nrc-mcp run mcp:serve
```

or in a JSON config, `"env": {"NRC_MCP_GROUPS": "analyse"}` beside `"command"`. The lists live in
`python/src/nrc_mcp/surface/__init__.py` (`PRESETS`); `mise run mcp:tools` prints them.

**Responses are summaries by default.** The gameplay reports (`balance_report`, `spawn_safety`,
`movement_check`, `sightline_report`, `route_report`, `route_render`) and `engine_playtest` take
`detail="summary"` (the default) or `detail="full"`. A summary keeps counts, verdicts and the
rows that need attention first, and drops what is the same on every call — the profile's
constants with their descriptions, the voxel grid's model notes, the scenario echo. It never
drops an `error` finding; other findings beyond ten are counted per severity, and every
dropped field or capped list is named in the response's `omitted`. Tool results are sent as
compact JSON.

## Your first session

A complete pass, in the order that works. Every step is a tool call; the shell equivalents are shown
where one exists.

**1 — Open a map.** Everything else operates on the currently open map.

```json
{"tool": "map_open", "path": "corpus/real/ut4_woolis.map"}
```

**2 — Look at the numbers.**

```json
{"tool": "map_stats", "grid": 8}
```

Counts, bounds, a shader histogram, and grid alignment. `grid` is the authoring grid you want
alignment measured against — it changes the report, never the map.

**3 — Look at the map.** Sculpting blind fails, so do this early and often.

```json
{"tool": "render_contact_sheet"}
{"tool": "render_topdown", "overlay": "structural"}
```

The image comes back **in the response**, not as a file — there is no output path to choose. Counts,
dimensions, scale and warnings arrive as structured data alongside it, so you read an exact number
instead of reading your own render. Use the `nrc render` CLI when you want a PNG on disk.

Orthographic views are **wireframe** on purpose: a filled top-down of a sealed map shows you the
underside of its sky brush and nothing else. Wireframe gives a real floor plan, with rooms and stairs
legible through the ceiling. Perspective views render solid. Pass `solid=true` to a top-down only
when you know the geometry is open.

Overlays: `structural` separates structural from detail brushes, brush entities and patches; `caulk`
shows which surfaces are never drawn in game; `off_grid` marks vertices that miss the grid.

**4 — Validate, in two passes.** Geometry and file format first, then the game's own rules.

```json
{"tool": "validate", "grid": 8}
{"tool": "validate_profile"}
```

**5 — Build something.** Geometry is described as an intersection of half-spaces, so a non-convex
brush is not expressible. Compile it, look at it, then commit it.

A room is a hollowed box, which is **6 brushes** — floor, ceiling, four walls:

```json
{"tool": "solid_compile", "ir": {
  "op": "hollow",
  "solid": {"op": "box", "min": [0, 0, 0], "max": [512, 512, 256]},
  "thickness": 16}}
```

A doorway is a subtraction, and it comes out as **3 brushes** — left column, right column, lintel —
which is what a mapper would draw by hand:

```json
{"tool": "solid_compile", "ir": {
  "op": "subtract",
  "from": {"op": "box", "min": [0, 0, 0], "max": [512, 16, 256]},
  "cut": [{"op": "box", "min": [224, -8, 0], "max": [288, 24, 112]}]}}
```

The cutter deliberately overshoots the wall on both faces (`y` from `-8` to `24` through a 16-thick
wall). If a doorway compiles to more pieces than you expect, that overshoot is usually what is
missing. Composing the two — cutting the same doorway out of the whole shell — gives 10 brushes
rather than 8, because the cutter also crosses the brushes adjoining that wall; `solid_compile`
reports the count before anything is committed, which is the point of running it first.

```json
{"tool": "solid_preview", "ir": {"...": "..."}, "view": "sheet"}
{"tool": "solid_commit", "ir": {"...": "..."}, "label": "north_room",
 "textures": {"default": "caulk", "faces": {"floor": "concrete_01"}}}
{"tool": "map_save"}
```

`label` is required, and it is worth choosing well: it names the shape in the recorded sidecar and is
how you edit it later. Default `textures.default` to `caulk` and override only the faces a player can
see — the compiler discards caulk faces, so a caulked hidden face costs nothing.

`solid_help` is the full operator reference. `solid_list` and `solid_edit_param` then let you change a
committed shape by one field: `solid_edit_param("north_room", "solid.max[1]", 640)` makes the room
deeper and replaces its tracked brushes. Use `solid_param_preview` first; `map_save` persists
the geometry and sidecar together. Manually changed generated brushes are preserved by refusing
a stale replacement.

**6 — Compile.** Start with `draft`; it is the fastest thing that still tells you the truth about
whether the map seals.

```json
{"tool": "compile_map", "path": "maps/mymap.map", "preset": "draft"}
```

Presets are `draft`, `iterate`, `quality`, `final`. If it leaks, `leak_trace` reads the pointfile
and tells you where the hole is.

**7 — Optimize, then ship.**

```json
{"tool": "structural_audit"}
{"tool": "ship_check", "target": "mymap"}
{"tool": "pack_pk3", "bsp": "out/mymap.bsp"}
```

`structural_audit` is the biggest single lever on compile time and runtime visibility cost:
structural brushes generate portals, and most brushes do not need to be structural.

### Building an ET: Legacy map with the tools

The whole map can be made through tool calls, with nothing written by hand. With
`NRC_PROFILE=etl`, a working order:

| Step | Tools |
| --- | --- |
| Start the file | `map_new` |
| Rooms, walls, trim | `solid_commit` (`textures` per face direction; `detail` for trim and props), `solids_commit` for many at once, `patch_commit` for columns, arches and bevels |
| Spawns, markers, intermission, lights | `spawn_cluster` (a team's spawns at checked standing positions plus their `team_WOLF_objective` marker, optionally named for a script to toggle); `entities_place` for `script_multiplayer`, `info_player_intermission`, `light` and the rest in one call |
| Objectives | `objective_place` (`dynamite`, `constructible`, `command_post`, `checkpoint`, `checkpoint_one_way`, `escort`, `steal`): trigger, objective, spawn flag, spawns, links and script events in one call; `path_place` first lays the path an escorted vehicle follows |
| The map script | `script_set_event` (respawn times, `wm_setwinner`, `setautospawn`, the objectives' events), `script_show` to read it back |
| Check | `validate`, `validate_profile`, `spawn_safety`, `balance_report`, `lighting_report`, `structural_audit` then `brush_set_detail` |
| Release files | `arena_write`, `objdata_write`, `commandmap_make`, `levelshot_make` |
| Build and ship | `map_save`, `compile_map`, `pack_pk3`, `ship_check`, then `mise run game:smoke -- <pk3> --client` |

The `etl-build-from-nothing` tape is this list run end to end, and `--compile` loads the result in
the engine with a client joined. `validate_profile` only calls something an error when ET: Legacy
stops on it; each such rule names the engine source line and was checked by loading a map.

### ET balance and revision-safe editing

`balance_report` now measures the current unsaved map. For the `etl` profile it adds
`reinforcement_balance`: active spawn groups, overlapping spawn boxes, capacity against a
requested team size, route travel estimates, and arrival ranges including reinforcement waits.
Team IDs are `red` (Axis) and `blue` (Allies).

```json
{"tool": "balance_report", "cell": 16, "team_sizes": {"red": 12, "blue": 12}}
{"tool": "navgrid_stats", "cell": 16, "stance": "crouch"}
{"tool": "balance_report", "stance": "prone", "run_speed": 60}
```

The last call supplies a hypothetical effective speed, not a verified prone speed. Standing uses
verified default run speed; script-dependent or conflicting wave assignments are marked unknown.
Override effective server settings with `run_speed` and `respawn_seconds`. Optional `stages` select
explicit `spawn_entities` and `objective_entities`, with a unique `name` and optional wave overrides.
These selections do not change collision or simulate objective progression.

Use `map_checkpoint` before editing and `map_restore` to undo staged geometry, script and solid
sidecar changes together. `map_save` saves them with conflict checks and rollback on a failed write;
saving clears checkpoints. `compile_map` uses a retained session snapshot and returns a revision
receipt. Gameplay reports use the same in-memory revision even before saving.

## Tool reference

80 tools in 9 groups. `mise run mcp:tools` prints the list live, grouped, with a `*` against
every tool that changes something — that listing is generated from the running server, so it
cannot drift from what the server actually serves.

Read and write are always separate tools. `solid_param_preview` reports what a parameter change
would do and `solid_edit_param` replaces its tracked geometry; `model_place_plan` builds the entity and
`model_place` adds it; `upstream_diff` reads the local baseline and `upstream_fetch` goes to the
network. A tool that is safe or unsafe depending on one of its arguments cannot be annotated
honestly, and a client deciding whether to ask permission reads that annotation.

**The map**

| Tool | Does |
| --- | --- |
| `map_open` | Open a `.map` for analysis. Everything else works on it. |
| `map_new` | Create an empty `.map` (just a worldspawn) and open it. Never overwrites an existing file. |
| `map_stats` | Counts, bounds, shader histogram, grid alignment. |
| `map_save` | Write it back. Refuses if the map did not round-trip when opened, or if the write would destroy playable space. |
| `query_entities` | Entities, filterable by classname. |
| `entity_place` | Add a point entity — a spawn, a script or objective marker. Keys the profile does not declare come back as notes. |
| `entities_place` | Add many point entities in one call, all or none, with a compact reply: new indices and each distinct note once. |
| `entity_set_keys` · `entity_remove_keys` | Set or remove keys on any entity, brush entities and worldspawn included. |
| `brush_geometry` | Exact vertices and derived properties of one brush. |
| `validate` | Geometry and file-format findings. |
| `validate_profile` | Entities against the game profile's rules, and the map script, speaker script and sound scripts where the game has them. |
| `script_show` · `script_set_event` · `script_remove` | Read and edit the map script beside the `.map`, for a game whose maps carry one. Unknown events and actions are refused before anything is written; everything outside the edited event is left as it was. |
| `objective_place` | Build a whole objective in one call - its trigger, the objective entity, the links and weapon class the engine needs, and its script events - from the profile's recipes (for ET: Legacy, `dynamite`, `constructible`, a neutral or one team's `command_post`, and a forward spawn flag with its spawn region and spawns, `checkpoint` or `checkpoint_one_way`; and `escort` and `steal`). Point entities go in `points` by role. Team-only doors are not a kind: give a door the `allowteams` key, which `validate_profile` checks. |
| `map_mirror` | Mirror the whole map in x or y: brushes stay valid (plane points reordered), textures show the mirror image, patches keep facing out, and the profile's keys follow — origins, facings (`a` → `180 - a`), command-map corners, the swing of rotating doors. Vehicles the game turns to face their path are given a half turn instead. Models face the mirrored way but are not themselves mirrored; script coordinates are left to you. |
| `path_place` | Lay a path for a mover: a chain of linked path points, straight or curved by control points, with long straight legs optionally split. Its `start` is what `objective_place(kind="escort")` follows. |
| `profile_summary` | What the profile knows, and how much of it is verified. |

**Seeing it**

| Tool | Does |
| --- | --- |
| `render_topdown` | Top-down (XY) view. |
| `render_camera` | Perspective, or a front/side orthographic view. |
| `render_contact_sheet` | Three orthographic views plus a perspective, in one image. |
| `render_player_eye` | What a standing player sees from a floor position. |

**Sculpting**

| Tool | Does |
| --- | --- |
| `solid_help` | Every operator, its fields, and the on-grid caveat. |
| `solid_compile` | Compile geometry and report it, touching nothing. |
| `solid_preview` | Compile and render, without committing. |
| `solid_commit` | Compile and add the brushes to the open map. `textures` picks a shader per face direction and can make them detail; `new_entity` makes a second door or trigger of a class. |
| `solids_commit` | Commit many solids in one call, all or none (sidecar included); a shared `textures` default and a compact per-label reply. |
| `brush_set_detail` | Mark existing brushes detail (or structural again); takes `structural_audit` findings directly. |
| `patch_commit` | Curved surfaces as patches: a column, an arch, a quarter-round bevel. |
| `solid_inspect` · `solid_list` | Structure of a recorded shape; everything recorded. |
| `solid_param_preview` | Show what changing one parameter would produce. |
| `solid_edit_param` | Replace unchanged generated brushes and stage the edited tree. |

**Meshes** — the Blender handoff

| Tool | Does |
| --- | --- |
| `asset_plan` | Brush, patch or mesh? Decides, and says why. |
| `blender_brief` | A numerically complete brief plus a ready-to-send prompt. |
| `model_import` | Validate an exported mesh against the brief that asked for it. |
| `model_place_plan` | Build the entity that *would* put it in the world. |
| `model_place` | Add that entity to the open map. |
| `model_make_clip` | Fit a convex collision hull, returned as geometry you can commit. |

**Compiling and optimizing**

| Tool | Does |
| --- | --- |
| `compile_map` | Run q3map2 with a named preset. |
| `engine_playtest` | Run bounded gameplay actions with state assertions; retain logs and return screenshots. |
| `bot_playtest` | Run the packed map on a dedicated server with the bot add-on and its seeded waypoints only (a throwaway copy of the add-on folder), add bots, and report what the add-on loaded, how far each bot travelled along the waypoints, the stances seen, and the game's objective events. With the staged map goals and map script: goals loaded, script errors, the script's messages with the goals each switched, and when bots of each spot's team stood on it. |
| `bsp_report` · `bsp_entity_diff` | Read a compiled BSP; compare its entities to the source. |
| `structural_audit` | Brushes marked structural that need not be. |
| `hint_suggest` | Hint brush planes, proposed from a portal file. |
| `leak_trace` | Where the map leaks, from the pointfile, and what on the path cannot seal: a brush with a see-through side, detail, a model. |
| `shader_audit` | Shader references against the scripts on disk; a name nothing supplies is an error once the base game is scanned. |
| `texture_search` | Base-game texture names by keyword, so faces use textures every server has. |
| `compile_ab` · `ab_history` | Compile two variants and diff what matters; every past comparison. |

**Gameplay analysis** — all constants read from the profile, never hardcoded

| Tool | Does |
| --- | --- |
| `navgrid_stats` | Build the walkable grid; report size and coverage. |
| `route_report` · `route_render` | Find and draw routes between positions or spawn/objective entities, including stance changes and the exact path used for cost. |
| `balance_report` | Per-team distance from each spawn group to each objective. |
| `sightline_report` | Sightline length distribution and power positions. |
| `movement_check` | Clearances against verified movement constants. |
| `spawn_safety` | Exits per spawn, and distance to the nearest enemy spawn. |
| `spawn_cluster` | Place N team spawns in an area at positions checked for floor, headroom and support with the full player box, facing a yaw, plus the region marker; classnames, flags and script names from the profile's `spawn_cluster` recipe. `group` names them for a script (`alertentity`) to toggle. |
| `lighting_report` | Lights, ambient and minimum light as q3map2 reads them; warns on a map that would compile black, notes walkable areas far from any light. |
| `playable_space_diff` | What an edit did to the space a player can stand in. |
| `objective_playtests` | An `engine_playtest` scenario for every objective in the open map, recognised from its entities and script: who plays it, where they stand (reachable floor), what they do and what proves it; plus the objectives it could not test and why. Templates are the profile's (`objectives.kinds.*.playtest`). |

`route_report` and `route_render` accept either coordinates (`start`, `end`) or entity
indices (`start_entity`, `end_entity`). Mixed routes start/end standing by default and
change stance only where both body boxes fit. Their default cost is weighted distance:
stand 1, crouch 1.5, prone 2, plus 32 per change. These are planning preferences, not speeds
or travel times; override `stance_costs` and `transition_cost` explicitly. The report records
snapped endpoints, source revision, path legs, transitions and limitations. Search-budget
exhaustion returns `reachable=null`, distinct from a proven lack of route in the grid.
The overlay uses the same map snapshot and marks stand/crouch/prone, endpoints and changes.

**Shipping**

| Tool | Does |
| --- | --- |
| `ship_check` | Naming, levelshot, arena file, companion files, package contents. |
| `arena_write` · `objdata_write` | The arena file (the map in the menu: name, briefing, gametypes) and the limbo objective descriptions. |
| `commandmap_make` · `levelshot_make` | The command map - map coordinates on the worldspawn, a top-down section image and its shaders - and the loading-screen levelshot. |
| `pack_pk3` | Build the release archive. Names the packer misses but the base game or the map supplies do not fail it; `ignore` lets named resources be missing. |
| `bot_waypoints_write` | Seed the bot add-on's waypoint file (for ET: Legacy an Omni-bot `.way`) from the stand/crouch/prone grids: connected waypoints (walking off a ledge one way), crouch/prone flags, one-way jump links up ledges and across gaps the player box's arc clears (`JUMP` take-off, listed in `jumps`), `CLIMB` foot and top waypoints on ladder brushes (`ladders`), doorway waypoints that open doors (`DOOR`, team flags for team doors), wall links closed until a wall is blown and bridge links closed until a constructible deck is built (`BLOCKWALL` / `BLOCKBRIDGE`), named spawn and objective waypoints, and whether each objective is reached from each team's spawns. Also writes the map goals (defend spots per held objective part, snipe spots per team, chosen by line of sight over the teams' shortest routes; plus caller `spots`) and a map script that switches them on the game's objective triggers. Staged beside the map's folder for the server's `omnibot_path`; the add-on never reads them from the pk3. |
| `repack_analyze` | Every resource the BSP references, and which of them nothing supplies (what fails a pack). |

**The toolchain itself**

| Tool | Does |
| --- | --- |
| `task_list` · `task_run` | Every capability, discovered from mise; run one. |
| `bench_run` | The fitness suite. |
| `selfdev_protected` | The paths self-modification may never touch, and their hash pins. |
| `upstream_diff` · `upstream_fetch` | Upstream drift, against the local baseline or a fresh fetch. |
| `pr_plan_status` | The contribution plan. |

## Resources

| URI | Contents |
| --- | --- |
| `nrc://tasks` | The live mise task list — capability discovery, always current. |
| `nrc://profile/{id}` | A game profile as YAML. `nrc://profile/urt43` and `nrc://profile/etl` ship. |
| `nrc://playtest/{id}` | Supported gameplay actions, state fields, limits and an example scenario. |
| `nrc://conventions` | Representation tiers, caulk, grid discipline, authoring order. |
| `nrc://corrections` | Claims from the design document that did not survive verification. |
| `nrc://solid-ir` | The Solid IR operator reference, without spending a tool call. |
| `map://current/summary` | The open map. |

## Prompts

Four workflows, in the order that works. Each is a sequence you could assemble from the tool list
and would probably assemble slightly wrong — "preview before you commit", "measure the win rather
than assuming it", "read the profile before sizing anything" are all learned.

| Prompt | For |
| --- | --- |
| `asset_pipeline` | The brush/patch/mesh decision and the Blender round trip. |
| `optimize_pass` | Find vis cost, then *confirm* it with a measured A/B. |
| `new_map_bootstrap` | Start from nothing and keep it sealed and measurable. |
| `gametype_retrofit` | Add a gametype's spawns and objectives to an existing map. |

## Narrowing the surface

The full tool inventory adds substantial description text to every session. Most sessions need a
fraction of it, so the server can be told which groups or session presets (see
[Connect an MCP client](#connect-an-mcp-client)) to expose:

```bash
NRC_MCP_GROUPS=build               # a level-design session: 36 tools, ~40 KB listed
NRC_MCP_GROUPS=map,render,sculpt   # by group instead: 33 tools
NRC_MCP_READ_ONLY=1                # a review session: every writer dropped
```

Groups are `map`, `render`, `sculpt`, `assets`, `compiling`, `gameplay`, `shipping`, `project`
and `maintenance`; presets are `build`, `analyse`, `ship` and `level`. The default is
everything. An unknown group name or a selection that would
leave no tools at all stops the server with an error rather than quietly serving the wrong
surface — a zero-tool server is indistinguishable from a broken one.

It is an environment variable and not a flag because the server is launched through
`mise run mcp:serve`, and a task's signature is a public interface here (§1.4).

**Read `nrc://corrections` before trusting a rule.** The design document was written partly from
recollection; several of its claims were wrong, including three it listed as verified.

## Reading the output

**Findings carry a severity, and severity is earned.** A rule may only fail your build if its
`confidence` is `verified` — meaning it was checked against the gamepack or the engine source.
Anything `unverified` is clamped to `info` and can never fail anything, however plausible it looks.

That mechanism is not bureaucratic. The design document asserted that Team Survivor needs dedicated
spawn entities; the gamepack says otherwise, and a validator built on that claim would have failed
correct maps. `nrc://corrections` records each one.

**A warning is sometimes about the tool, not your map.** `BRUSH_OFF_GRID` on a rotated prism is
unavoidable — a `.map` stores *planes*, and a brush's vertices are wherever three planes meet, which
for anything angled is not on the grid and cannot be made so. The same finding on axis-aligned
geometry is a real defect. `validate` reports the count; deciding needs to know which shape produced
it.

**`BRUSH_NOT_EXACT` means "excluded", not "broken".** See [Limits](#limits).

## Command line

Every capability is a mise task, and the task list is the agent's action surface, so anything an
agent did is a command you can paste into a shell.

```sh
mise tasks                       # all 42
mise run info                    # resolved paths and versions
mise run render corpus/real/ut4_dofa.map out/dofa.png
mise run compile:draft maps/mymap.map
mise run test:diff               # the round-trip gate alone
mise run test:tapes              # end-to-end scenarios over the MCP surface
mise run bench:tier2 -- --check   # Tier 2 wiring, without spending anything
mise run bench                   # the fitness suite
mise run watch                   # re-run checks on change
```

There is also a direct kernel binary, useful in scripts and pipes. `mise run kernel:build` puts it at
`target/release/nrc`; it is not installed onto `PATH`, so either call it by path or
`export PATH="$PWD/target/release:$PATH"`.

```sh
nrc roundtrip <file.map>...          # verify byte-identical load/save
nrc stats <file.map> [--grid N]      # JSON
nrc validate <file.map> [--grid N]   # JSON; exit 1 if findings
nrc normalize <file.map> --write     # re-serialize in place (refuses without --write)
nrc render <file.map> --out x.png --view top --overlay structural
```

Exit codes: `0` clean, `1` findings or did not round-trip, `2` tool error. Add `--quiet` for JSON
only, `--pretty` to indent it.

## How it works

```
agent ──MCP──► nrc-mcp ──► nrc-core (Rust)   .map I/O, exact geometry, validators
                    │
                    ├──► mise run <task> ──► q3map2 / mbspc / cargo / uv
                    └──► profiles/*.yaml      the only game-specific layer
```

**Lossless before anything else.** Load and re-save any `.map` byte-identically. `mise run test:diff`
checks it two ways: parse and re-serialize and require identical bytes, then compile both the
original and the re-serialized copy and compare the geometry lumps of the resulting BSPs. Current
state over 49 maps — 8 real Urban Terror sources, upstream's 18 pathological regression maps and 23
synthetic ones — is **49/49 byte-identical and 6/6 compiled BSPs identical**.

Getting there meant discovering things the format is not documented with: the exact float formatting
(`%10.10lf`, trailing zeroes stripped, so `-0` is a real literal), the leading newline every file
this fork writes begins with, and trailing whitespace preserved verbatim — one real map ends
`}\r\n\r\n\r\n` and was the last holdout. Numbers remember the text they were parsed from, so an
untouched map reproduces its own bytes and a modified one differs only where it was modified.
Comments, key order, duplicate keys, line endings, layer records and primitives whose syntax is
unrecognized all survive.

**Two gates, guarding two different things.** The round-trip gate protects the *file*. It says
nothing about the *map*, and for a long time nothing did: an edit could fill a room with
playerclip and every check stayed green — it round-tripped, validated, compiled and sealed, and
was unplayable. `map_save` now also compares the map it is about to write against the file it is
about to replace, and refuses on either of two findings that have no legitimate form:

- `PLAYSPACE_INTERIOR_SEALED_BY_CLIP` — floor that had a ceiling over it is now inside a clip
  volume. A roof is sky-exposed by definition, so this is the inside of the map being closed off,
  and clip is what makes it invisible until someone walks into it.
- `PLAYSPACE_CLIP_SURFACE_WALKABLE` — you can now stand on top of a clip volume, held up by
  nothing else. Capping something with clip does not remove the surface, it raises it.

Sealing floor with ordinary geometry is a warning, not an error: putting a wall in a room is
authoring, and a gate that blocks every real edit is a gate that gets switched off. Pass
`acknowledge_regression=true` when you meant it. `playable_space_diff` runs the same comparison
on demand, between any two maps.

**Exact predicates, or an honest refusal.** Coplanarity, convexity, plane identity and grid
membership use integer and rational arithmetic, not epsilons. Brush vertices come from intersecting
every triple of face planes exactly and keeping the points that satisfy every half-space, so
convexity is guaranteed rather than checked. Where the input is off-grid the kernel reports
`Indeterminate` instead of guessing, because a guessed side is a sliver and a sliver is a leak three
weeks later.

**Subtraction that a human would recognize.** `A \ B = ⋃ᵢ (A ∩ h₁ ∩ … ∩ hᵢ₋₁ ∩ ¬hᵢ)` over `B`'s
half-spaces: every term is convex by construction, and terms that would be slivers are *exactly*
empty and vanish. Adjacent pieces then merge where their union is genuinely convex, by an exact test
— for `P` and `Q` sharing plane `h`, the union is convex iff every other plane of `P` contains all
of `Q` and vice versa. That has to be exact, because merging wrongly fills the doorway back in.

**mise as the action surface.** The server never shells out to a raw command; it calls
`mise run <task>`. Capability discovery is therefore free, and new abilities need no server code.

**One game-specific layer, enforced.** Entity ontology, gametype ids, spawn rules and movement
constants live in `profiles/*.yaml` as data. The Urban Terror profile covers 95 entity classes and
carries an explicit confidence marker on 895 entries — 868 verified against the gamepack, 27 marked
unverified and therefore unable to fail a build.
`mise run test:seam` fails the build if a game-specific string appears in code, and it derives its
forbidden vocabulary from the profile itself, so it cannot fall behind.

### Layout

```
crates/nrc-core/     the kernel: lex, parse, write, math, exact, winding, validate, stats
crates/nrc-solid/    half-space geometry: CSG, convex merge, brush emission
crates/nrc-render/   headless rasterizer: ortho and perspective, PNG out, no GPU
crates/nrc-cli/      `nrc` — roundtrip / stats / validate / normalize / render
crates/nrc-py/       PyO3 bindings; the server's in-process kernel
python/src/nrc_mcp/  the MCP server
tools/               corpus import, the differential harness, the q3map2 driver, the seam lint,
                     the scenario-tape runner
bench/tapes/         end-to-end scenarios: tool sequence in, invariants out
bench/scenarios/     Tier 2 briefs: a model drives the surface, the invariants grade it
profiles/            game profiles — the only game-specific layer
corpus/              real, upstream-regression and synthetic maps
contrib/mcpbridge/   an editor plugin for live editor state (never compiled — see docs/)
docs/                design notes and the spec corrections
```

## Limits

Ask for any of these and you get an honest refusal rather than a plausible number.

**Rotated and decompiled geometry is approximate.** This is the cost of the exact-predicate design
and it shows up on real maps. Any coordinate that is not an exact integer within world bounds is
refused by the exact predicates, which report `Indeterminate` rather than picking a side. Geometry
whose *plane-defining points* are off-grid therefore cannot be evaluated exactly, and rotated brushes
and maps decompiled from a BSP (`q3map2 -convert -format map` prints every plane point to three
decimals) are the common cases. `validate` reports them as `BRUSH_NOT_EXACT`.

The gameplay analyses (navgrid, routes, sightlines, play-space) read such brushes from the kernel's
floating-point path instead (`nrc_core::approx_geometry`: q3map2-style winding clipping, plane
tolerance 0.01u, corners welded and snapped to the integer grid within 0.1u) and count them as
`brushes_float_evaluated`; on-grid brushes are untouched. Only a brush that path also declines
(collinear points, planes enclosing nothing, a corner past the world limit) is left out, counted as
`brushes_indeterminate` and warned about. Measured on maps decompiled from the stock BSPs against
their Splash Damage sources at cell 32: oasis went from 8,544 of 21,072 brushes left out and 62% of
walkable cells shared with the source to none left out and 97.3% shared (99.4% with the compiler's
model-clip brushes, which the source map does not contain, set aside); goldrush 98.4% to 99.95%,
radar 84.8% to 99.1%. Most of the residue is cell centres lying exactly on a vertical face, which
the last bit of a rebuilt plane decides either way.

The honest summary: fully precise about axis-aligned and 45° geometry, partly blind to arbitrarily
rotated geometry, and every report says which it is looking at. Closing the gap means either snapping
input — which changes the map — or adaptive floating-point predicates. `crates/nrc-core/src/exact.rs`
explains why the integer route came first.

**No bot navigation.** Omni-bot waypoints are not generated or checked, so a map built here has no
bots until someone waypoints it in game.

For playable examples built through real MCP calls, see [the dogfood scenarios](bench/dogfood/README.md).
They retain source maps, packages, tool receipts and engine gameplay evidence under `out/dogfood/`.

**Old sources meet a newer compiler.** Splash Damage's released ET sources round-trip, compile and
pack, and load in the engine. battery's source leaks under today's q3map2, which counts a
`surfaceparm alphashadow` grate as see-through and so does not let it close the bunker floor;
`leak_trace` names that brush. See `docs/et-full-size-maps.md`.

**Limited patch authoring.** `patch_commit` makes cylinders, arches and quarter-round bevels as
`patchDef2`; free-form curves and terrain are still editor work. Patches do not seal the map.
Their shader contents determine player collision. The movement analysis collides with patches as
the engine does: it ports `CM_GeneratePatchCollide` (subdivide until each chord is within 16 units of
the curve, one facet per grid cell) and reads the shader's surfaceparms from the project's and the
base game's scripts. `bench/dogfood/patches.py` walks eight patch lanes in the engine; the analysis
agrees on seven and is stricter on the eighth, a 45-degree slope (a grid limitation brush ramps share).
`NAV_PATCHES_IGNORED` now means a patch the engine itself would refuse to load.

**No measured reference dimensions.** There is no corpus of width/height/length distributions per
space category, so sizing comes from the profile's verified constants. Related: no cover density or
peek-angle analysis.

**Estimated traversal time.** ET balance reports use the profile run speed, staged script
reinforcement intervals and optional explicit overrides. These are planning estimates, not engine
measurements. Combat, sprint stamina, acceleration, moving obstacles and prone legs/turning are not
simulated. Stage selections share one collision snapshot; they do not execute scripts.

**No live editor state.** `contrib/mcpbridge` is a complete JSON-RPC plugin for NetRadiant-custom,
and **it has never been compiled** — there is no Qt5 environment here and the host compiler cannot
build that codebase at all. It is offered for review, not for use. `docs/editor-bridge.md` explains
the design; `docs/pr-plan.md` tracks readiness and reports this as unmet.

**The model-in-the-loop test has never been run.** `mise run test:tapes` runs five scenarios end
to end — build from scratch, continue an existing level, refactor one region, an optimisation
pass, and one negative control that reproduces the `ut4_dofa` clip failure and requires
`map_save` to refuse it. Every step is a real call into the server, and none of them prove that a
*model* would make those calls: a tape is the tool sequence with the model removed, which is
exactly why it costs nothing.

`mise run bench:tier2` is the harness that closes that gap — a model driving this MCP over stdio
against small fixtures, graded by the same invariants. Its free modes are exercised
(`--check` starts the server, lists the tools, and verifies every scenario's goal is false on
the untouched fixture). **`--run` has never executed**: it spends tokens, and that is the
owner's call to make, so nothing here reports a Tier 2 score and none should be inferred. Three
ET: Legacy scenarios - build a map from nothing, add an objective, ship a map - are ready for it.
`bench/scenarios/README.md` covers the cost controls.

**No kernel self-modification.** The exact predicates, the differential harness, the fitness
definitions, the corpus and every verified rule are hash-pinned in `bench/protected.json`. The
opt-in self-tuning loop can only touch the prompt and resource layer, where a mistake cannot corrupt
a map. `mise run selfdev:protected` verifies the pins; if it fails, treat every fitness score as
meaningless until you know why.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `mise run test:diff` fails on a map | The kernel could not reproduce that file's bytes. Nothing downstream is trustworthy. Run `nrc roundtrip <file>` for the first differing offset. |
| `semantic: skipped` | No compiler configured. Set `Q3MAP2` in `mise.local.toml`, or `mise run vendor:build`. Not a failure. |
| q3map2 exits immediately, or complains about the path | Almost always WSL path translation. Set `NRC_Q3MAP2_MODE = "windows"`. UNC paths (`\\wsl$\...`) are rejected by q3map2 itself and staging exists to avoid them. |
| `-fs_basepath` errors, or no shaders found | `NRC_FS_BASEPATH` must be the directory *containing* the game directory (`q3ut4`, `etmain`), not that directory itself. |
| MCP client shows no tools | The server needs to start in the repo. Set `cwd`, or use `mise -C /path/to/nrc-mcp run mcp:serve`. Check `mise run mcp:tools` works first. |
| A rule fires that you believe is wrong | Check `nrc://corrections` — some of the design document's rules were wrong. If it is `info`, it is `unverified` by construction and cannot fail a build. |
| `BRUSH_OFF_GRID` on curves and prisms | Expected. A `.map` stores planes; angled geometry has off-grid vertices necessarily. Judge it only on axis-aligned geometry. |
| Analysis reports fewer brushes than the map has | Brushes with no derivable geometry (collinear plane points, planes enclosing nothing) are excluded; the count and examples are in the report. Off-grid plane points (`BRUSH_NOT_EXACT`) are read in floating point and counted as `brushes_float_evaluated`. See [Limits](#limits). |
| A top-down render is one flat grey rectangle | You forced `--solid` on a sealed map and are looking at the sky brush. Orthographic views default to wireframe for this reason. |
| `mise install` fails on an env var | An `mise.local.toml` value referenced from `mise.toml`. Machine paths belong only in `mise.local.toml`. |
| `selfdev:protected` fails | Something that defines *correct* changed. Treat all scores as meaningless and find out why before re-pinning. |

## Contributing

- `mise run ci` must be green. If `test:diff` is red, fix that first.
- The kernel has **no dependencies**, on purpose. A plane-intersection bug arriving through a
  transitive update is the failure mode this project can least afford.
- Task names are an API. Renaming one breaks the agent's action surface.
- Nothing in `python/src` parses `.map` text. Twice a module reached for a second parser because an
  accessor was missing; both times the fix was to add the accessor to the kernel.
- Rules carry a `confidence`, and only `verified` may fail a build. Verify against the gamepack or
  the engine source — not against documentation, and not against recollection.
- `mise run test:seam` keeps game-specific strings out of code. It reads its vocabulary from the
  profile, so it cannot fall behind.

## Licence

GPL-2.0-or-later, matching NetRadiant-custom.
