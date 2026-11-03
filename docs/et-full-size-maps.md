# ET: Legacy at full size

What the pipeline does with the six original Splash Damage map sources (`wolf_et_map_source.zip`
from wolffiles.eu, under id Software's limited-use licence: used locally, never committed).
Point `NRC_MAPSRC` at the unpacked folder and run `mise run corpus:import` to reproduce.

| Map | Brushes | Patches | Entities | Round-trip | Profile errors | Playable compile | Pack | Engine (server + client) |
| --- | ---: | ---: | ---: | --- | ---: | --- | --- | --- |
| battery | 11,833 | 704 | 1,981 | identical | 0 | leaks (see below) | packs | loads clean |
| fueldump | 13,665 | 166 | 2,868 | identical | 0 | 25 s | packs | loads clean |
| goldrush | 16,610 | 573 | 2,720 | identical | 0 | 28 s | packs | loads clean |
| oasis | 19,243 | 178 | 2,309 | identical | 0 | 24 s | packs (one stock gap allowed) | loads clean |
| radar | 10,210 | 271 | 1,790 | identical | 0 | 34 s | packs | loads clean |
| railgun | 14,735 | 303 | 1,385 | identical | 0 | 16 s | packs | loads clean |

- **Round-trip:** every source re-serializes byte-identically (about a second each), and all six
  compile to identical BSPs from the original and the re-serialized copy (`mise run test:diff`).
- **Profile:** with each map's shipped script beside it, `validate_profile` reports no errors.
  The remaining warnings are real: script blocks no entity runs, names that resolve to nothing,
  one dynamite decoy that cannot be destroyed.
- **Compile:** the `iterate` preset (bsp, fast vis, fast light) on 12 cores.
- **Pack:** each map packs to `<map>_autopacked.pk3` (draft compile, `pack_pk3` with the `etl`
  profile, 2026-11-03). Why they used to fail is below.
- **battery leaks** under today's q3map2 through a grate in the main bunker floor; see below.
- **Engine:** each pack loads in ET: Legacy 2.86.2 with its script, server and client, the client
  joining as a spectator, with no line meaning the game stopped (`mise run game:smoke --client`).
  With the stock speaker and sound scripts copied in under the source's map name, the client
  loads the packed `<map>_final.sounds` and the script's speaker actions fire.

## Why the packs failed

The packer marked every map `_FAILEDpack`. The earlier guess here, editor-only models such as
`models/mapobjects/light/lantern.md3`, was wrong: q3map2 bakes a `misc_model` into the BSP and its
packer never looks for the model file (`autopk3.cpp:100-160` traces drawn shaders, `noise` and
`music` keys, remaps and fogs, nothing else). What the packer actually could not find:

| Map | Names | Cause |
| --- | --- | --- |
| all six | 1 to 12 shaders, e.g. `textures/metal_misc/ametal_m03_slick`, `models/mapobjects/props_sd/basketsand` | The base game defines each one, with `implicitMap` (or `implicitMask`, `implicitBlend`). The packer does not read those (`autopk3.cpp:466-468`, "unsupported implicit shader"), so it looks for an image named after the shader, finds none, and fails. |
| oasis | `sound/world/brickfall.wav` | A `func_explosive`'s `noise`. No base archive ships it, and the shipped `oasis.bsp` names it too: a gap in the original game, where that wall breaks silently (`g_mover.c:4747-4758`, `snd_dma.c:470-473`). |

`pack_pk3` now checks each name the packer missed. One the base game defines or ships, one the
map's own shader script defines (the script and its images are packed), or a file beside the map
counts as found. One the profile lists under `packaging.allowed_missing` - stock gaps like
brickfall, each with where it is referenced and what the game does without it - or one that
matches the caller's `ignore` list (names or shell patterns) may be missing, and the result says
why. Anything else still fails the pack: railgun with `props/barrel_m01` replaced by
`nrc_test/doesnotexist` on its 10 patches packs as `_FAILEDpack.pk3` with that one name in
`not_found`, and with `ignore=["textures/nrc_test/*"]` as `_autopacked.pk3`.

## battery's leak

The pointfile runs from the south edge of the world, under the main bunker, up into the room
around the leaked light (entity 136 at 2944 -5088 1100). It enters that room through the floor
grate slab at z 988-992: worldspawn brush 368, caulk and `metal_misc/diamond_c_01b` with a face
of `metals_sd/grate_b`. That shader is `surfaceparm alphashadow`, and the et compile table makes
alphashadow translucent (`games.cpp:150`). The compiler ORs the flags of all a brush's sides, so
the grate face makes the whole brush detail and non-opaque (`map.cpp:517-537`): it does not seal,
and its west face (x = 2880) opens straight onto the void under the bunker, since the wall above
starts at z 992.

Checked by compiling: with `metals_sd/grate_b` swapped for an opaque texture on its 55 faces,
battery's draft compile does not leak. The shipped `battery.bsp` has a leaf boundary at the slab's
east edge (x = 2912) that the new compile does not, which suggests the slab was structural there:
the compiler Splash Damage used apparently did not count alphashadow as translucent. That is
read from the BSP, not from that compiler's source.

It was not a missing model. A `cagelight.md3` sits 6 units from the leaked light, but q3map2
makes every brush it builds from a model detail (`model.cpp:445`, `:489`), so a model never
seals, present or missing.

`leak_trace` now reports what on the path cannot seal, from the profile's `sealing:` section:
`LEAK_NONSEALING_BRUSH` for a brush the path passes through with a translucent side (on battery,
brush 368 `metals_sd/grate_b: alphashadow`, and the two clip brushes over the grate),
`LEAK_DETAIL_ON_PATH` for detail brushes, and `LEAK_MODEL_NEAR_PATH` for models within 128 units,
saying they cannot be the hull.

## Speaker and sound scripts

ET reads two kinds of sound file per map, and nothing here handled either:

- `sound/maps/<map>.sps`, the speaker script: ambient speakers, each a `speakerDef` with a sound,
  origin and optional `targetname`. Server and client both load it through the bot library's
  tokenizer (`bg_sscript.c`). A mistake stops reading at that line; the game goes on.
- `sound/scripts/<map>.sounds`, the map's sound scripts: named groups of sound files, used by
  `wm_teamvoiceannounce`, `playsound` and a `target_speaker` `noise` with no file extension. The
  client loads only the files `sound/scripts/filelist.txt` lists and the map's own
  (`cg_sound.c:396-468`). A mistake there stops the client.

`validate_profile` now checks both (grammar, keys and limits in the profile's `sound:` section),
the sound files they name, the sound script names the map and its script use, and the speakers
`enablespeaker` / `disablespeaker` / `togglespeaker` name. `pack_pk3` packs the two files from
beside the map, with any sound of the map's own they name.

The six stock maps ship theirs in `pak0.pk3` under the stock names, so under the source names
(`battery_final` and so on) the voice announcements and speakers resolve to nothing, and
`validate_profile` says so: 20 to 47 unresolved sound script names and 3 to 15 speakers per map.
With
the stock files copied in as `<map>_final.sps` / `.sounds`, every stock file parses, and what is
left are the original game's own slips: `battery_axis_bunker_capture` (the file has `_captured`),
`goldrush_allies_gold_defend`, `goldrush_allies_truckbars_destroy`, `allies_hqt_compost_construc`
(oasis), `axis_hq_construct_compost` and `railgun_axis_capture_depot` (railgun), radar's
`neutral_radio_sound` speaker, and three sound files no archive ships (fueldump's
`hq_reptank.wav` and `hq_tanktunnel.wav`, radar's `sound/world/wolf.wav`), which the profile
lists as stock gaps.

## What running them changed

- `COORD_OUT_OF_BOUNDS` judged plane points, so oasis reported 18 false errors; it now judges a
  brush's corners. The one oasis brush really outside the world is still reported.
- `wm_set_main_objective` took a number in every stock script, the old syntax the engine ignores
  on purpose; it is no longer reported as an unresolved name.
- Terrain index images are found beside the map (the compile wrapper adds the map's folder as a
  search path); `import_corpus` and the differential harness carry them along.
- `pack_pk3` moved the packer's output with a rename that fails across filesystems.
- `pack_pk3` no longer fails on shaders the packer cannot read; see above.

## Grid findings on existing maps

`validate` used to report every one of these maps' angled brushes as an error - 6,000 to 13,000
`BRUSH_OFF_GRID` per map - although neither q3map2 nor the engine stops on them. Grid findings
(off-grid, too thin) are now errors only for geometry added or reshaped since `map_open`; on
brushes the map was opened with they are one summary warning per code. Opened, oasis goes from
9,820 errors to 1: the brush that really lies outside the world.
