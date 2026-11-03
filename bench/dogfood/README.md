# Playable MCP examples

These scenarios author maps through the actual stdio MCP client, then validate, render,
compile and package them. Each run writes a new directory under `out/dogfood/` with the
source project, tool arguments/responses, timing, images and a result summary. Existing
projects and evidence are preserved. They require the ET profile, game assets and compiler
configured as described in the root README.

```sh
mise run bench:dogfood -- depot
mise run bench:dogfood -- constructible
mise run bench:dogfood -- movement
mise run bench:dogfood -- frontline [--one-way]
mise run bench:dogfood -- convoy
mise run bench:dogfood -- gates [--bots [--strip FLAG]]
mise run bench:dogfood -- patches build
mise run bench:dogfood -- patches stock
```

| Scenario | Exercises |
| --- | --- |
| `depot` | Two routes through a warehouse, six spawns per team, dynamite objective and scripted win; built with the batch tools (`solids_commit`, `spawn_cluster`, `entities_place`) |
| `constructible` | Workshop pit, stairs, constructible bridge, horizontal pipe, parametric editing and checkpoint restoration |
| `movement` | Standing stair/arch route, crouch tunnel, prone vent, route changes after lowering a ceiling and restoring it |
| `frontline` | `objective_place` checkpoint (or `checkpoint_one_way`) with forward spawns, a neutral `command_post`, and an Axis-only door (`allowteams`) |
| `gates` | A corridor of gates, each the only way on: an Allies-only door, an unbuilt constructible bridge over a lethal trench, a wall Allies dynamite; seeds the bot waypoints, and with `--bots` runs bots and reports where they were before and after each gate changed |
| `convoy` | A truck the Allies escort along a path with a curved bend (`path_place`, `objective_place` escort), and Axis orders they steal and deliver (`objective_place` steal) |
| `patches build` | Eight lanes, one `patch_commit` surface each; `route_report` and an `engine_playtest` walk per lane, side by side |
| `supply_mirror` | A real map mirrored left-right: `map_mirror` on ET: Legacy's `etl_supply.map`, compiled with its release pk3 beside it and packed with `depends` (none of its art is copied), then an engine smoke and objective flow (`scenarios/supply_mirror*.json`) and Omni-bot runs with seeded waypoints and with the add-on's own mirrored `etl_supply.way`/`.gm`. Needs `/mnt/media_raid/etl_supply.map` and the pk3 (`--map`, `--pk3`) |
| `patches stock` | Drops a player onto Oasis barrel caps (patches) in the engine and records where it settles |

The depot once placed every brush and entity with its own call: 67 calls and 84,392 bytes of
response text. Committing its 12 solids with one `solids_commit`, each team's spawns and region
marker with one `spawn_cluster`, and the script entity, camera and eight lights with one
`entities_place` brings it to 35 calls and 75,864 bytes; the spawn positions are unchanged.
Most of the remaining bytes are `balance_report`, `spawn_safety` and `profile_summary`.

These are small development maps. Compile and package success do not establish that an
objective works in the engine or that a layout is balanced in multiplayer. Coarse cells can
hide low passages. The first movement map had a
40-unit prone vent that looked passable to analysis but blocked a real player: collision
keeps the body slightly above the floor. The example now uses a 48-unit prone vent and a
56-unit crouch tunnel, with a regression for a ceiling that exactly matches the body height.

Analysis used to skip patches (`NAV_PATCHES_IGNORED`). It now collides with the facets the
engine builds from them (`CM_GeneratePatchCollide`, ported in `nrc_solid::patch::collision`).
`patches build` checks that against ET: Legacy 2.86.2, one engine run per lane, all
`route_report` at cell 8:

| Lane | Stance | Analysis | Engine |
| --- | --- | --- | --- |
| pipe r=48 on the floor | stand | blocked | blocked at x=446 (pipe side at 464) |
| pipe r=8 on the floor | stand | crosses | crosses (collides as a diamond 16 high, under the 18 step) |
| pipe r=24 on the floor | stand | blocked | **crosses**: it collides as a 45-degree diamond, a walkable slope; the grid's body test is stricter on slopes than the engine, for brush ramps too |
| arch over a 128x176 doorway | stand | crosses | crosses |
| prone vent, 48 high, pipe r=16 at the ceiling | prone | blocked | blocked at x=496 |
| prone vent, 48 high, pipe r=4 at the ceiling | prone | crosses | crosses |
| pipe r=48, `common/clipshot` (nonsolid) | stand | crosses | crosses |
| pipe r=48 facing inward | stand | blocked | blocked at x=446, as the outward one |

The last lane settles a question the source leaves open: `cm_patch.c:1936` skips a facet hit
through its back plane, but against a player box the facet's bevels decide first, so the
analysis treating facets as solid from both sides matches the engine there.

All examples use an intermission camera with `spawnflags 3`. A neutral camera alone lets
spectators join but can crash at round end when ET selects a camera for the winning team.
Profile validation checks coverage for both possible winners, allowing deathmatch/start
points as the engine's fallback.

The depot hands its delayed victory sequence to a `game_manager` trigger. A destroyed
`func_explosive` is freed on the next frame; putting `wait` followed by `wm_endround` in its
own death event leaves the delayed action unable to resume. Script validation now warns
about this lifetime hazard; short waits and uncertain control flow are informational risks.

The MCP playtest scenarios replace fixed frame waits with fresh state conditions:

```sh
mise run bench:dogfood -- playtest workshop
mise run bench:dogfood -- playtest depot
mise run bench:dogfood -- playtest movement
mise run bench:dogfood -- playtest frontline --pk3 <frontline package>
mise run bench:dogfood -- playtest frontline_one_way --pk3 <frontline --one-way package>
mise run bench:dogfood -- playtest convoy_escort --pk3 /absolute/path/nrc_convoy_autopacked.pk3
mise run bench:dogfood -- playtest convoy_steal --pk3 /absolute/path/nrc_convoy_autopacked.pk3
```

`frontline` proves the flag by where players respawn: Allies touch the flag, switch team
and back, and must reappear at the forward spawns (the capture switched the spawns, handed
the spawn region over and set the autospawn); Axis then retake it and Allies must reappear
at their camp. The same run builds the command post with pliers and checks that the
Axis-only door stays shut for Allies and opens for Axis with the use key.
`frontline_one_way` checks that Axis cannot raise the one-way flag, that Allies can, and
that Axis lowering it takes the spawns back.

These call `engine_playtest` over stdio MCP, retain its JSON receipt and returned images,
and use the example packages recorded in `out/dogfood/report.json` (the convoy scenarios take
the package `convoy` printed). The scenarios live in
`bench/dogfood/scenarios/`; read `nrc://playtest/etl` for supported actions and conditions.

`convoy_escort` stands an Allied player in the moving escort volume, leaves the truck to stop
at a path point, catches up, follows it round the curved segment and sees `arrived` win the
round. `convoy_steal` walks onto the orders (their `stolen` event hides a lamp the observer
watches) and carries them into the delivery volume, whose `death` event wins the round.
`objectives` generates the playtest from a built example's objectives instead of a
hand-written file (`objective_playtests`), runs every resulting scenario with
`engine_playtest`, and reports per objective whether a test was generated, ran and passed:

```sh
mise run bench:dogfood -- objectives depot      # also workshop, frontline, frontline_one_way, convoy
```

It uses the newest build of that example under `out/dogfood/` (or `--map` and `--pk3`). The
generated tests check what the hand-written ones check about each objective, and more: the
depot's charge is aimed at where it landed rather than at fixed angles, the one-way flag
asserts that the holders' touch changed nothing, the escort is followed past every path point
and checked to stop, and the frontline and convoy runs also build the command post and steal
the orders. They do not check things that are not objectives (the frontline's team door) or a
map's own script effects (the convoy's lamp); the stolen orders are proved by the carried item.

The older direct-engine probe below remains useful for reproducing the original experiments.

The optional engine probe uses an isolated game home and records screenshots, engine logs
and read-only Lua state observations. It joins both teams as an engineer, then uses ordinary
weapon and attack commands. `devmap` and optional `setviewpos` prepare the test; reaching the
objective from spawn is not tested by teleporting there. Lua support is required for state
assertions. The maps' final objective scripts award Allies the round; the probe checks that
winner transition and intermission as well as the objective state.
These probe modes are tailored to the examples' coordinates and win conditions.

```sh
mise run bench:dogfood -- engine /absolute/path/nrc_depot_autopacked.pk3 \
  --basepath /absolute/path/to/etlegacy --mode dynamite \
  --position 1640 640 24 --pitch 65 --yaw 0 \
  --arm-pitch 67 --arm-yaw -48 --watch relay \
  --axis-wait 21 --allies-wait 16 --seconds 240

mise run bench:dogfood -- engine /absolute/path/nrc_workshop_autopacked.pk3 \
  --basepath /absolute/path/to/etlegacy --mode construct \
  --position 928 512 24 --pitch 55 --yaw 180 --watch workshop_bridge \
  --axis-wait 21 --allies-wait 16 --seconds 240

mise run bench:dogfood -- engine /absolute/path/nrc_movement_autopacked.pk3 \
  --basepath /absolute/path/to/etlegacy --mode movement \
  --axis-wait 16 --allies-wait 21 --seconds 240
```

Inspect `result.json` and screenshots together. Failed attempts remain on disk with the same
evidence as successful attempts. Native previews show geometry; engine screenshots are needed
to assess actual materials, lighting and player interactions.

## Bot waypoints in the engine

`bot_waypoints_write` then `bot_playtest` (8 bots, 300 s, ET: Legacy 2.86.2 with its bundled
Omni-bot 0.93, cell 16, spacing 128) on each example, driven through the tool functions. Every
run's add-on log said `N Waypoints Loaded from file nav/<map>.way` at each map load, with no
load error, and every bot moved along the graph:

| Example | Waypoints | Flags | Bots travelled (units) | Waypoints passed per bot | Objective events (game log) |
| --- | --- | --- | --- | --- | --- |
| depot | 291 | - | 23,600-35,800 | 72-122 | Allies planted the relay transmitter 3 times, Axis defused once |
| workshop | 201 | 3 CROUCH | 14,100-17,900 | 60-93 | none in this run; an earlier 300 s run: "The Allies built the workshop bridge!", round won |
| frontline | 194 | - | 42,500-58,400 | 43-83 | the forward bunker captured 40 times, both teams |
| convoy | 150 | - | 22,500-27,700 | 43-64 | orders stolen and returned; "The Supply Truck has reached the Axis gate!", round won at 61 s |
| movement | 126 | 16 CROUCH, 23 PRONE | 19,500-30,700 | 51-83 | Axis planted the radio 6 times, Allies defused 5 |

On the movement map the observer saw bots in the crouch tunnel 51 times and in the prone vent
50 times, every sample crouched and prone respectively: the flags make the add-on take the
stance on the way in. Without a waypoint file the add-on loads, logs `Waypoint <map>.way not
found in file or archives` and adds no bot at all. The same seeded files load in the 0.92
Linux release library.

### Jumps and ladders

`mise run bench:dogfood -- traversal` builds a course on which walking alone reaches nothing
past the start (its `balance_report` route is asserted unreachable): a 40-unit ledge, a 48-unit
gap over a 568-unit shaft that kills, and a 192-unit wall with a `common/ladder` brush, with
the Axis spawn at the bottom and the Allies spawn and the radio at the top. `bot_waypoints_write`
(cell 16) seeds 77 waypoints: 3 ledge jumps and 3 gap jumps (two forward, one back), each a
run-up, a `JUMP` take-off (radius 16) and a one-way link to its landing (`JUMPLOW` on a ledge's),
and the ladder's `CLIMB` foot and top linked both ways; the script asserts all of that, and
`--bots PORT` adds a `bot_playtest` run (it loaded 77 waypoints twice, no error, 8 of 8 bots
moving, in a 180 s run over MCP).

A 240 s run of the same file (8 bots, Omni-bot 0.93, observer sampling every 0.5 s through
`botplay.run`) shows the bots using all three. Counting changes of grounded zone between a
bot's samples within one life:

| Traversal | Axis (spawn at the bottom) | Allies (spawn at the top) |
| --- | --- | --- |
| up the ledge (start floor -> ledge top) | 71, plus 91 samples in the air rising at the ledge | 5 |
| across the gap, outward (ledge top -> far side) | 53, plus 49 samples in the air over the shaft | 17 |
| back across the gap (far side -> ledge top) | 8 | 64, plus 89 samples over the shaft |
| up the ladder (far side -> ladder, ladder -> top floor) | 35 onto it, 7 off at the top; 82 samples on its face, every 40-unit band from 72 to 248 | 6 onto it, 3 off at the top |
| down from the top floor | - | 17 via the ladder, 66 dropping straight to the far side |

Axis planted the radio once (`axis planted "Course Radio"`, Allies defused it): the radio is
reachable from the Axis spawn only through the ledge jump, the gap jump and the ladder. One bot
fell into the shaft in 240 s. Not measured: how many ladder climbs fail (22 Axis samples went
from the ladder back to its foot, which is either a bot slipping off or one turning back), and
the jump distance limits themselves (the course's ledge and gap sit inside them; the limits
come from the arc model, not from the engine).

Doors, constructible bridges and blown walls are covered by the `gates` map below.

### Map goals and map scripts

The same calls with the generated `<map>_goals.gm` and `<map>.gm` installed beside the `.way`
(8 bots, 240-300 s, port 27963; `bot_playtest` switches map-script debugging on in its
throwaway add-on folder, so the script's own lines reach the game log). On every map the
add-on logged `N Goals Loaded, 0 Goals Deferred` at each map load (our spots plus the game's
own objective goals), no goal error, no `E`/`W` line in its log, and registered every
trigger. Each hooked event that happened ran its function:

| Example | Spots | Events the script saw | Switching seen |
| --- | --- | --- | --- |
| depot | 2 DEFEND (Axis), 4 SNIPE | relay planted/defused (3x); in another run destroyed at 228 s | destroyed turned both DEFEND spots off; the round then ended, so no bot was left to watch |
| frontline | 8 DEFEND (flag and command post, per team), 4 SNIPE | the flag captured 35 times | per capture, the holder's flag spots on and the other team's off; DEFEND_fwdflag_allies_1 held in 82% of samples while on, 0% while off |
| convoy | 4 DEFEND (orders, then delivery), 4 SNIPE | orders stolen 5x, returned 5x | the orders spots held in 70% of samples while on, 0 of 4 while off; the delivery spots were on for 2-10 s at a time, too short for a bot to cross the map |
| workshop | 2 DEFEND (Allies, after the bridge is built), 4 SNIPE | bridge built twice | the spots were turned on; the build ends the round 1 s later |
| movement | 2 DEFEND (Allies), no SNIPE (no long sightline) | radio planted 2x, defused once | none to see (planting switches nothing) |

Spots are held because they are goals, not because bots pass by: with no goal file or script
(`scripts=false`) the same positions were stood on in 2 and 8 of 123 samples (depot DEFEND)
against 59 and 42 with them, and the frontline flag spots in 12/7 of 122 against 63/34; the
SNIPE spots in 0-1 samples against 8-28. What the samples cannot show: which goal a bot was
on, or where it aimed. SNIPE goals are for sniper rifles only, so which bots take them
depends on the classes the add-on picks. Every generated file also compiles with the
GameMonkey core Omni-bot 0.93 embeds (`NRC_GM_CHECK`, see `test_bot_goals.py`).

The stance and objective runs above predate door and constructible seeding (below): their
files had no door waypoint and seeded the workshop deck as floor.

### Doors, walls and bridges: the `gates` map

`gates` is built only to test state-changing brushes. West to east: the Axis rooms (spawn,
and a radio to dynamite) | a dynamite wall, the only way out | a hall | a 256-unit trench,
crossed only by an unbuilt Allied constructible bridge (a `trigger_hurt` at its floor kills
whoever falls in) | a hall | an Allies-only `func_door` (`allowteams allies`, use key) | the
Allied spawn room. Allies must open the door, build the bridge and blow the wall; Axis are
shut in until the wall goes. The seeded file: a doorway waypoint flagged
`DOOR|ALLIES|TEAMONLY`, wall and deck cells with no waypoint, and lip-to-lip spans across them
flagged `BLOCKWALL` and `BLOCKBRIDGE` (129 waypoints, one component).

`gates --bots` runs `bot_playtest` (8 bots, 600 s unless noted, ET: Legacy 2.86.2, Omni-bot
0.93, port 27962) and reads the observer's samples (every 2 s) and the game log in order, so
each sample falls in a phase: before the bridge is built, after, after the wall is blown.
`--strip FLAG` removes a flag from every waypoint first, as a control.

| Run | Bridge built | Wall blown | Allied trench deaths before the bridge | Axis samples against the wall before it went | Door crossings (Allies / Axis) |
| --- | --- | --- | --- | --- | --- |
| seeded file | 80.6 s | 127.0 s | 4, all one engineer | 13 of 242 | 294 / 0 |
| without `BLOCKBRIDGE` | 88.0 s | 130.7 s | 17, all four Allies | 12 of 250 | 244 / 0 |
| without `BLOCKWALL` (300 s) | 87.4 s | 135.9 s | 5 | 165 of 262 | 123 / 0 |

What this shows:

- **Door.** Allies path through the doorway waypoint, pressing use, and the door opens: they
  cross it hundreds of times (counted between consecutive samples of one bot). Axis never
  crossed, but they reached the hall beside it only after the wall went and rarely
  (21 samples), so the team flags are seen working once, not tested by a control.
- **Wall.** With `BLOCKWALL`, Axis bots do not crowd the wall while it stands (5% of their
  samples within 48 units of it); without it they press against it (63%), routing through a
  link the add-on never closes. Once it is blown, Axis come through (469 samples in the hall
  beyond it) and the Allies reach the Axis rooms.
- **Bridge.** With `BLOCKBRIDGE`, the only Allied bot to fall in before the bridge was built
  was the engineer sent to build it, walking from the bank lip straight at the
  constructible's position (the add-on approaches a goal off the graph); without it every
  Allied bot walked off the edge, repeatedly. After it is built both teams cross it (deck
  samples in every run). Bots still fall in now and then afterwards, in fights at the edge and
  after Axis blow the bridge up (at 353.6 s in the seeded run).

An earlier version put waypoints on the deck and inside the wall: the add-on then logged
`Path not found ... to BUILD_Trench_Bridge` and `PLANT_Gate_Wall` (92 times each) and nobody
built in 600 s, because it sends a bot to the waypoint nearest a goal whether or not that
waypoint's links are closed. Hence no waypoint stands on a deck or in a wall.

Not verified in the engine: a locked or scripted door seeded as a wall (the same
`BLOCKWALL` mechanism, unit-tested), a constructible barrier that closes a gap once built,
`func_door_rotating`, and that a team doorway keeps the other team out (see above). The
generated map script does not call `Wp.CheckBlockable()` on a build as stock scripts do;
the add-on re-tests blockable links every 2 s anyway, which was enough here.
