# ET: Legacy at full size

What the pipeline does with the six original Splash Damage map sources (`wolf_et_map_source.zip`
from wolffiles.eu, under id Software's limited-use licence: used locally, never committed).
Point `NRC_MAPSRC` at the unpacked folder and run `mise run corpus:import` to reproduce.

| Map | Brushes | Patches | Entities | Round-trip | Profile errors | Playable compile | Pack | Engine (server + client) |
| --- | ---: | ---: | ---: | --- | ---: | --- | --- | --- |
| battery | 11,833 | 704 | 1,981 | identical | 0 | leaks (see below) | missing resources | loads clean |
| fueldump | 13,665 | 166 | 2,868 | identical | 0 | 25 s | missing resources | loads clean |
| goldrush | 16,610 | 573 | 2,720 | identical | 0 | 28 s | missing resources | loads clean |
| oasis | 19,243 | 178 | 2,309 | identical | 0 | 24 s | missing resources | loads clean |
| radar | 10,210 | 271 | 1,790 | identical | 0 | 34 s | missing resources | loads clean |
| railgun | 14,735 | 303 | 1,385 | identical | 0 | 16 s | missing resources | loads clean |

- **Round-trip:** every source re-serializes byte-identically (about a second each), and all six
  compile to identical BSPs from the original and the re-serialized copy (`mise run test:diff`).
- **Profile:** with each map's shipped script beside it, `validate_profile` reports no errors.
  The remaining warnings are real: script blocks no entity runs, names that resolve to nothing,
  one dynamite decoy that cannot be destroyed.
- **Compile:** the `iterate` preset (bsp, fast vis, fast light) on 12 cores.
- **Pack:** the packer marks every map `_FAILEDpack` because the sources reference editor-only
  models the game does not ship (`models/mapobjects/light/lantern.md3` and others). The package is
  still fixed up: the script goes in, and 113 to 157 files the base game already ships come out.
- **battery leaks** from a light near the south shore. q3map2 reads the source directly here, so
  this is not serialization; the likeliest cause is a missing editor model that sealed the hull.
- **Engine:** each pack loads in ET: Legacy 2.86.2 with its script, server and client, the client
  joining as a spectator, with no line meaning the game stopped (`mise run game:smoke --client`).

## What running them changed

- `COORD_OUT_OF_BOUNDS` judged plane points, so oasis reported 18 false errors; it now judges a
  brush's corners. The one oasis brush really outside the world is still reported.
- `wm_set_main_objective` took a number in every stock script, the old syntax the engine ignores
  on purpose; it is no longer reported as an unresolved name.
- Terrain index images are found beside the map (the compile wrapper adds the map's folder as a
  search path); `import_corpus` and the differential harness carry them along.
- `pack_pk3` moved the packer's output with a rename that fails across filesystems.

## Grid findings on existing maps

`validate` used to report every one of these maps' angled brushes as an error - 6,000 to 13,000
`BRUSH_OFF_GRID` per map - although neither q3map2 nor the engine stops on them. Grid findings
(off-grid, too thin) are now errors only for geometry added or reshaped since `map_open`; on
brushes the map was opened with they are one summary warning per code. Opened, oasis goes from
9,820 errors to 1: the brush that really lies outside the world.
