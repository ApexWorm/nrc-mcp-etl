# Patches to the vendored netradiant-custom

Portability fixes applied on top of upstream `Garux/netradiant-custom`, in order, by
`mise run vendor:patch`.

They live here rather than in `vendor/` because `vendor:clone` does
`git reset --hard origin/master` on every run. A fix applied by hand in the vendored tree
survives until the next fetch and not one moment longer, which is the difference between
a reproducible build and a state one machine happens to be in.

## The patches

**`0001-move-entities-on-bsp-merge.patch`** — `convert_bsp.cpp`, `MergeBSPMain`.

`entity_t` holds a `std::forward_list<parseMesh_t>`; `parseMesh_t` holds a `mesh_t`, which
owns its vertex array and therefore deletes copy assignment on purpose. So `entity_t` is
move-only *by design*. Merging two BSPs copied entities into a vector, which asks
`vector::insert` for the copy-assignment path — libstdc++ never instantiates it, libc++
does, so this compiled on Linux and failed on macOS with "copy assignment operator is
implicitly deleted".

Fixed by moving instead of copying. `bsp` is a local holding moved-from globals and
`bsp.entities` is dead after that line, so a move is what was meant — it also skips a deep
copy of every patch mesh in the merged file. This is a fix upstream would want on any
platform, not a macOS workaround.

**`0002-mbspc-recognise-apple-silicon.patch`** — `tools/mbspc/qcommon/q_platform.h`.

The `__APPLE__` block handles ppc, i386 and x86_64. On Apple Silicon it falls through with
`ARCH_STRING` and the endianness undefined, and the guards below fire as "Architecture not
supported" — every mbspc translation unit, on every arm64 Mac. Adds an `__arm64__` /
`__aarch64__` branch alongside the x86_64 one.

## Working on them

```bash
mise run vendor:patch           # apply (idempotent; re-run freely)
mise run vendor:check           # syntax-check q3map2 without building or linking
mise run vendor:check -- --all  # also mbspc
```

`vendor:check` is the fast feedback loop: it compiles every translation unit with
`-fsyntax-only`, no linker and no artefacts, in a few seconds. Both patches above were
found with it. It answers "would this compile here", not "is q3map2 built" — linking and
runtime behaviour are not covered.

To re-cut a patch after upstream moves, edit the file in `vendor/netradiant-custom`, then:

```bash
git -C vendor/netradiant-custom diff -- <path> > contrib/patches/000N-<name>.patch
```

`vendor:patch` **fails loudly** if a patch no longer applies, rather than building
something subtly different. That is the intended behaviour: upstream moved, and someone has
to look.

## Upstream

Both are worth sending upstream — neither is nrc-mcp-specific and 0001 is a small
performance win on any platform. `docs/pr-plan.md` tracks contribution status; these are
not in it yet.

## Build dependencies (macOS)

`vendor:build-tools` additionally needs, via Homebrew: `pkg-config`, `glib`, `libpng`,
`jpeg-turbo`, `assimp`, `minizip`. The task adds jpeg-turbo's include and library paths
itself, because Homebrew keeps it keg-only and the Makefile has no pkg-config entry for it;
everything else is found through pkg-config.
