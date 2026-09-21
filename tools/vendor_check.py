#!/usr/bin/env python3
"""Syntax-check the vendored q3map2 sources without building them.

A full `vendor:build-tools` compiles, archives and links; it takes minutes and needs every
dependency present. Most of what breaks when porting to a new platform is none of those
things — it is a translation unit that will not compile, and `clang -fsyntax-only` answers
that in seconds with no linker and no artefacts.

That distinction matters here because the two portability patches in `contrib/patches/`
were both found this way. `parseMesh_t`'s deleted copy assignment is a *semantic* error
raised during template instantiation, so a syntax-only pass sees it exactly as a real
compile does; `q_platform.h`'s missing Apple Silicon branch is a preprocessor `#error`.
Neither needs a linker to find, and a full build hides both behind whichever one `make -j`
happened to reach first.

What this does **not** check: linking, library availability, or that the binary works.
A green run means "this source would compile here", not "q3map2 is built".

    mise run vendor:check            # q3map2 + common
    mise run vendor:check -- --all   # also mbspc

Exit codes: 0 everything compiles, 1 a translation unit failed, 2 the checker itself broke.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def src_root() -> Path:
    env = os.environ.get("NRC_SRC")
    if env:
        return Path(env)
    root = Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)
    return root / "vendor" / "netradiant-custom"


def pkg_config(*packages: str) -> list[str]:
    """`pkg-config --cflags`, or nothing if the package is absent.

    Absent is not fatal: a missing header shows up as a compile error against the file that
    needs it, which is a better message than this function raising.
    """
    try:
        out = subprocess.run(
            ["pkg-config", "--cflags", *packages],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    return out.stdout.split() if out.returncode == 0 else []


#: Files the Makefile does not build, which a directory glob would otherwise pick up.
#:
#: `cfgq3.c` is not C — it is an AAS bounding-box config that happens to carry a `.c`
#: extension. `gldraw.c` is a Windows-only debug viewer. Several botlib units include
#: `be_aas_main.h`, which exists in the Quake 3 engine tree and not in this one.
EXCLUDED = ("cfgq3.c", "gldraw.c")
EXCLUDED_IF_MISSING_HEADER = "be_aas_main.h"


def q3map2_flags(root: Path) -> list[str]:
    flags = [
        "-fsyntax-only",
        "-std=c++20",
        "-fno-exceptions",
        "-fno-rtti",
        "-w",
        f"-I{root}/tools/quake3/common",
        f"-I{root}/libs",
        f"-I{root}/include",
        "-DASSIMP_INCLUDE(x)=<x>",
        "-DNDEBUG",
        "-DPOSIX",
        "-DXWINDOWS",
        # The real values come from git describe; the checker only needs them to be
        # string literals so the preprocessor is happy.
        '-DRADIANT_VERSION="check"',
        '-DRADIANT_MAJOR_VERSION="0"',
        '-DRADIANT_MINOR_VERSION="0"',
        '-DRADIANT_ABOUTMSG="check"',
        '-DQ3MAP_VERSION="check"',
        '-DRADIANT_EXECUTABLE="check"',
    ]
    flags += pkg_config("glib-2.0", "libpng", "assimp")
    # Homebrew keeps jpeg-turbo keg-only and the Makefile has no pkg-config entry for it.
    jpeg = Path("/opt/homebrew/opt/jpeg-turbo/include")
    if jpeg.is_dir():
        flags.append(f"-I{jpeg}")
    return flags


def mbspc_flags(root: Path) -> list[str]:
    return [
        "-fsyntax-only",
        "-w",
        "-DBSPC",  # gates QDECL in botlib/l_script.h
        "-DNDEBUG",
        "-DPOSIX",
        f"-I{root}/tools/mbspc",
        f"-I{root}/tools/mbspc/mbspc",
        f"-I{root}/tools/mbspc/botlib",
        f"-I{root}/libs",
        f"-I{root}/include",
    ]


def sources(root: Path, include_mbspc: bool) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    for pattern in ("tools/quake3/q3map2/*.cpp", "tools/quake3/common/*.cpp"):
        found += [(p, "q3map2") for p in sorted(root.glob(pattern))]
    if include_mbspc:
        found += [(p, "mbspc") for p in sorted(root.rglob("tools/mbspc/**/*.c"))]
    return [(p, kind) for p, kind in found if p.name not in EXCLUDED]


def check_one(job: tuple[Path, str], root: Path) -> tuple[Path, str]:
    path, kind = job
    cc = ["c++", *q3map2_flags(root)] if kind == "q3map2" else ["cc", *mbspc_flags(root)]
    proc = subprocess.run([*cc, str(path)], capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return path, ""
    if EXCLUDED_IF_MISSING_HEADER in proc.stderr:
        return path, ""
    errors = [ln for ln in proc.stderr.splitlines() if "error:" in ln]
    return path, "\n".join(errors[:4]) or proc.stderr[-400:]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="also check mbspc (C, slower)")
    args = ap.parse_args()

    root = src_root()
    if not (root / "Makefile").is_file():
        print(f"vendor:check: {root} is not a netradiant-custom checkout", file=sys.stderr)
        print("  Run: mise run vendor:clone", file=sys.stderr)
        return 2

    jobs = sources(root, include_mbspc=args.all)
    if not jobs:
        print("vendor:check: found no sources to check", file=sys.stderr)
        return 2

    print(f"syntax-checking {len(jobs)} translation units in {root.name} (no linking)")
    failed = []
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        for path, errors in pool.map(lambda j: check_one(j, root), jobs):
            if errors:
                failed.append(path)
                print(f"\n  FAIL {path.relative_to(root)}")
                for line in errors.splitlines():
                    print(f"       {line}")

    print()
    if failed:
        print(f"  {len(failed)}/{len(jobs)} translation units do not compile")
        print("  If this is a new platform, the fix belongs in contrib/patches/.")
        return 1
    print(f"  all {len(jobs)} translation units compile")
    return 0


if __name__ == "__main__":
    sys.exit(main())
