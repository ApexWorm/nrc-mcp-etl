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
```

| Scenario | Exercises |
| --- | --- |
| `depot` | Two routes through a warehouse, six spawns per team, dynamite objective and scripted win; built with the batch tools (`solids_commit`, `spawn_cluster`, `entities_place`) |
| `constructible` | Workshop pit, stairs, constructible bridge, horizontal pipe, parametric editing and checkpoint restoration |
| `movement` | Standing stair/arch route, crouch tunnel, prone vent, route changes after lowering a ceiling and restoring it |
| `frontline` | `objective_place` checkpoint (or `checkpoint_one_way`) with forward spawns, a neutral `command_post`, and an Axis-only door (`allowteams`) |
| `convoy` | A truck the Allies escort along a path with a curved bend (`path_place`, `objective_place` escort), and Axis orders they steal and deliver (`objective_place` steal) |

The depot once placed every brush and entity with its own call: 67 calls and 84,392 bytes of
response text. Committing its 12 solids with one `solids_commit`, each team's spawns and region
marker with one `spawn_cluster`, and the script entity, camera and eight lights with one
`entities_place` brings it to 35 calls and 75,864 bytes; the spawn positions are unchanged.
Most of the remaining bytes are `balance_report`, `spawn_safety` and `profile_summary`.

These are small development maps. Compile and package success do not establish that an
objective works in the engine or that a layout is balanced in multiplayer. Analysis omits
patch collision, and coarse cells can hide low passages. The first movement map had a
40-unit prone vent that looked passable to analysis but blocked a real player: collision
keeps the body slightly above the floor. The example now uses a 48-unit prone vent and a
56-unit crouch tunnel, with a regression for a ceiling that exactly matches the body height.

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
