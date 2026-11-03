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
```

| Scenario | Exercises |
| --- | --- |
| `depot` | Two routes through a warehouse, six spawns per team, dynamite objective and scripted win; built with the batch tools (`solids_commit`, `spawn_cluster`, `entities_place`) |
| `constructible` | Workshop pit, stairs, constructible bridge, horizontal pipe, parametric editing and checkpoint restoration |
| `movement` | Standing stair/arch route, crouch tunnel, prone vent, route changes after lowering a ceiling and restoring it |

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
```

These call `engine_playtest` over stdio MCP, retain its JSON receipt and returned images,
and use the example packages recorded in `out/dogfood/report.json`. The scenarios live in
`bench/dogfood/scenarios/`; read `nrc://playtest/etl` for supported actions and conditions.
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
