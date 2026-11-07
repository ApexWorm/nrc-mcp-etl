#!/usr/bin/env python3
"""Drive q3map2, including across the WSL/Windows boundary.

Every compile in this project goes through here rather than through a raw command line,
because on this class of machine the compiler is not a peer of the rest of the toolchain.
The kernel, the MCP server and the repo live in WSL; Urban Terror, NetRadiant-custom and
therefore ``q3map2.exe`` live on the Windows side. Three consequences, all handled here so
that no caller has to think about them:

**Paths must be translated.** ``q3map2.exe`` cannot read ``/home/...``. Arguments become
Windows paths via ``wslpath -w``.

**UNC paths do not work.** ``\\\\wsl.localhost\\...`` is accepted by Windows but q3map2
treats it as *relative* and prefixes its own working directory, producing a
"Script file ... was not found" that names a path which is half cwd and half argument.
Verified empirically, not assumed. So a map living in WSL is **staged** into a directory
on the Windows filesystem, compiled there, and its artefacts copied back.

**Staging is also the safe default.** q3map2 writes ``.bsp``/``.prt``/``.srf`` next to the
``.map``. Compiling in a staging directory keeps build output out of the repo and out of
the user's game installation, which is somebody's actual Urban Terror install.

Argument order is not free either: ``-json`` and ``-pk3`` must be the **first** argument
(``main.cpp`` uses ``takeFront``) and the map file must be **last** (``takeBack``).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# Stage flag sets. Sequences run in order, each a separate q3map2 invocation, because
# q3map2 is one stage per process.
#
# `-maxshaderinfo` is deliberately absent: it existed briefly upstream and was removed
# ("refactor to work without -maxshaderinfo limit"), so passing it is an error now.
PRESETS: dict[str, list[list[str]]] = {
    "draft": [["-bsp", "-meta"]],
    "iterate": [
        ["-bsp", "-meta"],
        ["-vis", "-fast"],
        ["-light", "-fast", "-samples", "1", "-bounce", "0"],
    ],
    "quality": [
        ["-bsp", "-meta"],
        ["-vis"],
        [
            "-light",
            "-fast",
            "-filter",
            "-samples",
            "2",
            "-bounce",
            "4",
            "-patchshadows",
            "-nobouncestore",
        ],
    ],
    "final": [
        ["-bsp", "-meta"],
        ["-vis", "-saveprt"],
        [
            "-light",
            "-filter",
            "-samples",
            "3",
            "-bounce",
            "8",
            "-patchshadows",
            "-dirty",
            "-nobouncestore",
        ],
    ],
}

# Files q3map2 may produce beside the .map, worth copying back from staging.
ARTIFACT_SUFFIXES = (".bsp", ".prt", ".srf", ".lin", ".pk3", ".json")


class Q3Map2Error(RuntimeError):
    pass


@dataclass
class StageResult:
    flags: list[str]
    returncode: int
    seconds: float
    # q3map2 is chatty; keep the lines that matter rather than megabytes of shader loading.
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tail: list[str] = field(default_factory=list)
    # Every line, kept only on request (--full-output): -repack -analyze and -pk3 report their
    # findings as lists far longer than the tail.
    output: list[str] = field(default_factory=list)
    # The BSP stage's flood fill found the outside, or found no entity to flood from at all.
    leaked: bool = False


#: Printed by `Leak_feedback` whenever the flood fill fails (bsp.cpp:301-303) - for a real leak,
#: and for a map with no entity inside it (portals.cpp:589-591). Only a real leak also writes a
#: `.lin` pointfile, so the pointfile alone is not the signal.
LEAK_BANNER = "******* leaked *******"


def is_wsl() -> bool:
    if os.environ.get("NRC_Q3MAP2_MODE") == "windows":
        return True
    if os.environ.get("NRC_Q3MAP2_MODE") == "native":
        return False
    return (
        "microsoft" in Path("/proc/version").read_text().lower()
        if Path("/proc/version").exists()
        else False
    )


def to_windows_path(p: Path) -> str:
    """Translate a path for the Windows binary, or return it unchanged when native."""
    if not is_wsl():
        return str(p)
    out = subprocess.run(["wslpath", "-w", str(p)], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise Q3Map2Error(f"wslpath failed for {p}: {out.stderr.strip()}")
    return out.stdout.strip()


def find_q3map2() -> Path:
    raw = os.environ.get("Q3MAP2")
    if not raw:
        raise Q3Map2Error(
            "Q3MAP2 is unset. Point it at a q3map2 binary in mise.local.toml, or run "
            "`mise run vendor:build-tools` to build one."
        )
    p = Path(raw)
    if not p.exists():
        raise Q3Map2Error(f"Q3MAP2={p} does not exist")
    return p


def find_windows_workdir() -> Path | None:
    """A staging directory on the Windows filesystem, or None if we do not need one."""
    if not is_wsl():
        return None
    override = os.environ.get("NRC_WIN_WORKDIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d

    candidates: list[Path] = []
    users = Path("/mnt/c/Users")
    if users.is_dir():
        skip = {"All Users", "Default", "Default User", "Public", "desktop.ini"}
        for u in sorted(users.iterdir()):
            if u.name in skip or not u.is_dir():
                continue
            candidates.append(u / "AppData" / "Local" / "Temp" / "nrc-mcp")
    candidates.append(Path("/mnt/c/Windows/Temp/nrc-mcp"))

    for c in candidates:
        try:
            c.mkdir(parents=True, exist_ok=True)
            probe = c / ".write-test"
            probe.write_text("ok")
            probe.unlink()
            return c
        except OSError:
            continue
    raise Q3Map2Error(
        "no writable directory found on the Windows filesystem for staging. Set "
        "NRC_WIN_WORKDIR in mise.local.toml to a path under /mnt/c that you can write to."
    )


def needs_staging(map_path: Path) -> bool:
    """True when the map lives somewhere the Windows binary cannot address."""
    if not is_wsl():
        return False
    # Anything under a DrvFs mount already has a drive-letter path.
    return not str(map_path.resolve()).startswith("/mnt/")


def profile_q3map2_game() -> str | None:
    """The active profile's `editor.q3map2_type`, the `-game` its gamepack compiles with.

    Read with a regex rather than a YAML parser for the same reason `seam_lint` does: this
    tool must run before the Python environment is synced. The key is unique in a profile.
    """
    profile = os.environ.get("NRC_PROFILE", "").strip()
    if not profile:
        return None
    path = Path(os.environ.get("NRC_ROOT", ".")) / "profiles" / f"{profile}.yaml"
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"^\s*q3map2_type:\s*['\"]?([A-Za-z0-9_]+)", text, re.M)
    return m.group(1) if m else None


#: The member and archive the profile's compile-only shaders are handed to q3map2 in. A name
#: no game or map uses, since q3map2 loads one shader file per file name (vfs.cpp:194-224).
COMPILE_SHADER_ARCHIVE = "nrc_compile_shaders.pk3"
COMPILE_SHADER_MEMBER = "scripts/nrc_compile_{profile}.shader"

#: Stages that read a BSP to pack or list its resources. They get no compile-only shaders, so
#: what a pack carries never depends on them.
PACKING_STAGES = ("-pk3", "-repack", "-json")


def profile_compile_shaders() -> Path | None:
    """The active profile's compile-only shader script (`compile_shaders.compile_shader_file`),
    if it names one: shaders real maps use whose definitions come with the mapper's editor
    setup rather than the game. Read with a regex like `profile_q3map2_game`.
    """
    profile = os.environ.get("NRC_PROFILE", "").strip()
    if not profile:
        return None
    profiles = Path(os.environ.get("NRC_ROOT", ".")) / "profiles"
    try:
        text = (profiles / f"{profile}.yaml").read_text(errors="replace")
    except OSError:
        return None
    m = re.search(r"^\s*compile_shader_file:\s*['\"]?([\w./-]+)", text, re.M)
    if not m:
        return None
    path = profiles / m.group(1)
    return path if path.is_file() else None


_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)


def shader_blocks(text: str) -> list[tuple[str, str]]:
    """(name, text) of each top-level shader definition in a shader script, comments dropped.

    The same grammar `ParseShaderFile` reads (shaders.cpp:809-830): a name token, then a braced
    body; a `:q3map` suffix on the name marks a compiler-only shader and is not part of it.
    """
    text = _COMMENT.sub("", text)
    blocks: list[tuple[str, str]] = []
    i, n = 0, len(text)
    while i < n:
        m = re.compile(r"\s*([^\s{}]+)\s*\{").match(text, i)
        if not m:
            break
        depth, j = 1, m.end()
        while j < n and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        name = m.group(1)
        if name.lower().endswith(":q3map"):
            name = name[: -len(":q3map")]
        blocks.append((name, text[m.start(1) : j]))
        i = j
    return blocks


def shader_search_dirs(map_path: Path) -> list[Path]:
    """The folders whose loose scripts and pk3s q3map2 reads shaders from for this map: the
    base game's folder, the mod's, and the map's own root (path_init.cpp:412-424). The base
    game folder is the profile's `packaging.basegame`; without it only the map's root counts.
    """
    dirs: list[Path] = []
    base = os.environ.get("NRC_FS_BASEPATH") or os.environ.get("URT_BASEPATH")
    profile = os.environ.get("NRC_PROFILE", "").strip()
    if base and profile:
        try:
            text = (
                Path(os.environ.get("NRC_ROOT", ".")) / "profiles" / f"{profile}.yaml"
            ).read_text(errors="replace")
        except OSError:
            text = ""
        m = re.search(r"^\s*basegame:\s*['\"]?([\w.-]+)", text, re.M)
        if m:
            dirs.append(Path(base) / m.group(1))
        if os.environ.get("NRC_FS_GAME"):
            dirs.append(Path(base) / os.environ["NRC_FS_GAME"])
    root = map_path.parent.parent if map_path.parent.name.lower() == "maps" else map_path.parent
    dirs.append(root)
    return [d for d in dirs if d.is_dir()]


def defined_shader_names(dirs: list[Path]) -> set[str]:
    """Lowercased names every shader script under `dirs` defines, loose or in a pk3."""
    names: set[str] = set()

    def add(text: str) -> None:
        names.update(name.lower() for name, _ in shader_blocks(text))

    for d in dirs:
        for f in sorted((d / "scripts").glob("*.shader")):
            try:
                add(f.read_text(errors="replace"))
            except OSError:
                continue
        for archive in sorted(d.glob("*.pk3")):
            try:
                with zipfile.ZipFile(archive) as z:
                    for member in z.namelist():
                        low = member.lower()
                        if low.startswith("scripts/") and low.endswith(".shader"):
                            add(z.read(member).decode("latin-1"))
            except (OSError, zipfile.BadZipFile):
                continue
    return names


def write_compile_shader_archive(directory: Path, defined: set[str]) -> dict | None:
    """Put the profile's compile-only shaders that nothing else defines in a pk3 of their own.

    A fallback, never an override. Ordering cannot make it one: loose scripts are read before
    archives (vfs.cpp:194-224), but q3map2 puts each archive it opens *ahead* of those opened
    before it (vfs.cpp:109, :124), so an archive on the last -fs_pakpath would beat the base
    game's and a dependency's, and the first definition of a name wins (shaders.cpp:749). So
    every name `defined` already holds (see `defined_shader_names`) is left out.
    """
    script = profile_compile_shaders()
    if script is None:
        return None
    blocks = shader_blocks(script.read_text(errors="replace"))
    supplied = [(name, body) for name, body in blocks if name.lower() not in defined]
    report = {
        "file": str(script),
        "supplied": [name for name, _ in supplied],
        "defined_elsewhere": [name for name, _ in blocks if name.lower() in defined],
    }
    if supplied:
        member = COMPILE_SHADER_MEMBER.format(profile=os.environ.get("NRC_PROFILE", "").strip())
        with zipfile.ZipFile(directory / COMPILE_SHADER_ARCHIVE, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(member, "\n\n".join(body for _, body in supplied) + "\n")
    return report


def general_flags() -> list[str]:
    """Build the general flags from the environment and the active profile.

    No game name, mod directory or install path is hardcoded here. That is the §7.4 seam:
    this file must stay game-agnostic, and `tools/seam_lint.py` fails the build if a
    game-specific string appears in it. `-game` comes from `NRC_Q3MAP2_GAME`, then the
    profile. `NRC_FS_GAME` unset simply means no `-fs_game`, which is the correct
    game-neutral behaviour rather than a guess.
    """
    flags = ["-game", os.environ.get("NRC_Q3MAP2_GAME") or profile_q3map2_game() or "quake3"]

    base = os.environ.get("NRC_FS_BASEPATH") or os.environ.get("URT_BASEPATH")
    if base:
        bp = Path(base)
        if bp.is_dir():
            flags += ["-fs_basepath", to_windows_path(bp)]
        else:
            print(
                f"warning: game basepath {bp} is not a directory; shaders will not resolve",
                file=sys.stderr,
            )
    game_dir = os.environ.get("NRC_FS_GAME")
    if game_dir:
        flags += ["-fs_game", game_dir]
    return flags


def map_root_flags(map_path: Path, staged: bool) -> list[str]:
    """The map's own folder as a search path, so files it keeps beside itself resolve.

    A terrain's index image (`_indexmap`, or the older `alphamap`, map.cpp:1337-1338), custom
    textures and shaders are looked up through q3map2's search paths, not beside the map. The
    folder holding `maps/` - or the map's folder, for a flat layout - is added with
    `-fs_pakpath` (path_init.cpp:358-359, :420-424). A staged Windows compile only gets it
    when the folder is reachable from Windows.
    """
    root = map_path.parent.parent if map_path.parent.name.lower() == "maps" else map_path.parent
    if staged and not str(root).startswith("/mnt/"):
        return []
    return ["-fs_pakpath", to_windows_path(root)]


def classify(line: str) -> str | None:
    low = line.lower()
    if low.startswith("************ error") or low.startswith("error:"):
        return "error"
    if "warning" in low:
        return "warning"
    return None


def run_stage(
    exe: Path, general: list[str], flags: list[str], win_map: str, verbose: bool
) -> StageResult:
    # `-json`/`-pk3` are consumed with takeFront and must lead; the map file is taken with
    # takeBack and must trail. Everything else sits between.
    lead: list[str] = []
    rest = list(flags)
    if rest and rest[0] in ("-json", "-pk3", "-repack"):
        lead = [rest.pop(0)]

    cmd = [str(exe), *lead, *general, *rest, win_map]
    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace", check=False)
    elapsed = time.monotonic() - t0

    out = (proc.stdout or "") + (proc.stderr or "")
    lines = out.splitlines()
    res = StageResult(flags=flags, returncode=proc.returncode, seconds=elapsed)
    for ln in lines:
        kind = classify(ln)
        if kind == "error":
            res.errors.append(ln.strip())
        elif kind == "warning":
            res.warnings.append(ln.strip())
    res.tail = [ln for ln in lines[-25:] if ln.strip()]
    res.leaked = any(LEAK_BANNER in ln for ln in lines)
    res.output = lines

    if verbose:
        print(out, file=sys.stderr)
    return res


def compile_map(
    map_path: Path,
    stages: list[list[str]],
    *,
    stage_dir: Path | None,
    verbose: bool,
    keep: bool,
    full_output: bool = False,
) -> dict:
    exe = find_q3map2()
    map_path = map_path.resolve()
    if not map_path.is_file():
        raise Q3Map2Error(f"{map_path} does not exist")

    staged = False
    work_map = map_path
    workdir: Path | None = None

    if needs_staging(map_path):
        root = stage_dir or find_windows_workdir()
        if root is None:
            raise Q3Map2Error("staging required but no Windows work directory available")
        root.mkdir(parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix=f"{map_path.stem}-", dir=root))
        work_map = workdir / map_path.name
        shutil.copyfile(map_path, work_map)
        staged = True

    general = general_flags() + map_root_flags(map_path, staged)
    win_map = to_windows_path(work_map)

    # The profile's compile-only shaders, in a throwaway folder q3map2 can address: the
    # system temp directory, or on WSL (where a Windows binary cannot read it) the staging
    # directory or the map's own folder. Removed afterwards.
    shader_dir: Path | None = None
    compile_shaders: dict | None = None
    compile_general = general
    if profile_compile_shaders() and any(not s or s[0] not in PACKING_STAGES for s in stages):
        near = (workdir or work_map.parent) if is_wsl() else None
        shader_dir = Path(tempfile.mkdtemp(prefix="nrc-compile-shaders-", dir=near))
        compile_shaders = write_compile_shader_archive(
            shader_dir, defined_shader_names(shader_search_dirs(map_path))
        )
        if compile_shaders and compile_shaders["supplied"]:
            compile_general = [*general, "-fs_pakpath", to_windows_path(shader_dir)]

    results: list[StageResult] = []
    try:
        for flags in stages:
            packing = bool(flags) and flags[0] in PACKING_STAGES
            r = run_stage(exe, general if packing else compile_general, flags, win_map, verbose)
            results.append(r)
            if r.returncode != 0:
                break
    finally:
        if shader_dir is not None:
            shutil.rmtree(shader_dir, ignore_errors=True)

    # Collect artefacts before any cleanup, and copy them out of staging so a compile is
    # reproducible from the repo without reaching into a Windows temp directory.
    artifacts: list[str] = []
    out_dir = Path(
        os.environ.get("NRC_BUILD_OUTPUT")
        or Path(os.environ.get("NRC_ROOT", ".")) / "out" / map_path.stem
    )
    for suffix in ARTIFACT_SUFFIXES:
        produced = work_map.with_suffix(suffix)
        if produced.exists():
            if staged:
                out_dir.mkdir(parents=True, exist_ok=True)
                dest = out_dir / produced.name
                shutil.copyfile(produced, dest)
                artifacts.append(str(dest))
            else:
                artifacts.append(str(produced))

    # `-json` unpacks the BSP into a *directory* of per-lump files beside the input
    # (Brushes.json, DrawSurfaces.json, planes.json, entities.json, …), not a single file.
    # That directory is the whole point of the flag, so it has to come back too.
    lump_dir = work_map.with_suffix("")
    if lump_dir.is_dir():
        if staged:
            dest_dir = out_dir / lump_dir.name
            if dest_dir.exists():
                shutil.rmtree(dest_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            shutil.copytree(lump_dir, dest_dir)
            artifacts.append(str(dest_dir))
        else:
            artifacts.append(str(lump_dir))

    if staged and workdir and not keep:
        shutil.rmtree(workdir, ignore_errors=True)

    ok = all(r.returncode == 0 for r in results) and bool(results)
    leaked = any(r.leaked for r in results)
    hint = ""
    if leaked:
        has_pointfile = any(a.lower().endswith(".lin") for a in artifacts)
        hint = (
            "the map leaks: leak_trace reads the .lin pointfile to show where"
            if has_pointfile
            else "q3map2 reports a leak but wrote no pointfile, which means no entity sits "
            "inside the sealed space to flood from - place a spawn, a light or any point "
            "entity inside. Until then there is no .prt, so -vis cannot run."
        )
    return {
        "map": str(map_path),
        "ok": ok,
        "leaked": leaked,
        "hint": hint,
        "staged": staged,
        "q3map2": str(exe),
        "windows_mode": is_wsl(),
        "artifacts": artifacts,
        **({"compile_shaders": compile_shaders} if compile_shaders else {}),
        "total_seconds": round(sum(r.seconds for r in results), 3),
        "stages": [
            {
                "flags": r.flags,
                "returncode": r.returncode,
                "seconds": round(r.seconds, 3),
                "leaked": r.leaked,
                "error_count": len(r.errors),
                "warning_count": len(r.warnings),
                "errors": r.errors[:20],
                "warnings": r.warnings[:20],
                "tail": r.tail if r.returncode != 0 else r.tail[-6:],
                **({"output": r.output} if full_output else {}),
            }
            for r in results
        ],
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--preset", choices=sorted(PRESETS), help="named compile preset (§6.1)")
    g.add_argument(
        "--json-unpack",
        action="store_true",
        help="dump a .bsp to JSON (note: q3map2 never parses -unpack; unpack "
        "is the default for -json, so we do not pass it)",
    )
    g.add_argument(
        "--flags", nargs=argparse.REMAINDER, help="raw q3map2 flags for one stage, for experiments"
    )
    ap.add_argument(
        "--stage-dir", type=Path, default=None, help="override the Windows staging directory"
    )
    ap.add_argument(
        "--keep-staging",
        action="store_true",
        help="leave the staging directory in place for inspection",
    )
    ap.add_argument("-v", "--verbose", action="store_true", help="stream q3map2 output")
    ap.add_argument(
        "--full-output", action="store_true", help="include every output line of each stage"
    )
    ap.add_argument("target", type=Path, help="the .map (or .bsp for --json-unpack)")
    args = ap.parse_args()

    if args.preset:
        stages = PRESETS[args.preset]
    elif args.json_unpack:
        stages = [["-json"]]
    else:
        stages = [list(args.flags or [])]
        if not stages[0]:
            ap.error("--flags needs at least one flag")

    try:
        result = compile_map(
            args.target,
            stages,
            stage_dir=args.stage_dir,
            verbose=args.verbose,
            keep=args.keep_staging,
            full_output=args.full_output,
        )
    except Q3Map2Error as e:
        print(f"q3map2: {e}", file=sys.stderr)
        return 2

    print(json.dumps(result, indent=2))
    if not result["ok"]:
        for s in result["stages"]:
            if s["returncode"] != 0:
                print(f"\nq3map2 failed on {' '.join(s['flags'])}:", file=sys.stderr)
                for ln in s["errors"] or s["tail"]:
                    print(f"  {ln}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
