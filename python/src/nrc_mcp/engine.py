"""Load a packed map in the game's own engine and read the log: the only oracle that counts.

The validators say what the profile says the engine does. This asks the engine. A rule marked
`error` must be one the engine really stops on, and a map that validates clean must really
load - both were wrong once, and only loading the map showed it.

Everything game-specific - binary names, arguments, where the log lands, which lines mean the
game stopped - comes from the profile's `engine:` section. Nothing here knows a game.

Two levels:

- **server**: start the dedicated server on the map, let it run, read the log. Catches errors
  at load and in the script's first seconds.
- **client**: also start the game client on the map, which joins as a spectator. Some errors
  happen only when a player connects. Needs a display; under Linux an `Xvfb` on PATH is used
  when there is none.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from . import gamepath, procs, profiles


class EngineError(RuntimeError):
    """The smoke test could not be run at all, as distinct from a map that failed it."""


def spec(profile_id: str) -> dict:
    data = profiles.load(profile_id).get("engine")
    if not isinstance(data, dict):
        raise EngineError(f"profile {profile_id} describes no engine to load maps in")
    return data


def find_binary(names: list[str], basepath: str | None, override: str | None = None) -> Path:
    if override:
        p = Path(override)
        if p.is_file():
            return p
        raise EngineError(f"{override} does not exist")
    for name in names:
        if basepath and (Path(basepath) / name).is_file():
            return Path(basepath) / name
        found = shutil.which(name)
        if found:
            return Path(found)
    where = f"in {basepath} or on PATH" if basepath else "on PATH (NRC_FS_BASEPATH is not set)"
    raise EngineError(f"none of {', '.join(names)} found {where}")


def map_in(pk3: Path) -> str:
    """The one map a package carries, by its BSP."""
    with zipfile.ZipFile(pk3) as z:
        bsps = [n for n in z.namelist() if re.fullmatch(r"maps/[^/]+\.bsp", n, re.I)]
    if len(bsps) != 1:
        raise EngineError(f"{pk3.name} holds {len(bsps)} maps; the smoke test needs exactly one")
    return Path(bsps[0]).stem


def _expand(args: list[str], subs: dict[str, str]) -> list[str]:
    return [str(a).format(**subs) for a in args]


def read_log(text: str, eng: dict, mapname: str) -> dict[str, Any]:
    """What a log says: whether the map loaded, and every line meaning the game stopped."""
    lines = text.splitlines()
    loaded = str(eng.get("loaded", "")).format(map=mapname)
    fatal = [re.compile(p) for p in eng.get("fatal") or []]
    noted = [re.compile(p) for p in eng.get("noted") or []]
    return {
        "loaded": bool(loaded) and any(loaded in line for line in lines),
        "fatal": [line.strip() for line in lines if any(p.search(line) for p in fatal)],
        "noted": [line.strip() for line in lines if any(p.search(line) for p in noted)][:40],
    }


def _start_display() -> tuple[subprocess.Popen | None, dict[str, str]]:
    # Only an X11 desktop needs a display named; Windows and macOS clients open their own window.
    if os.environ.get("DISPLAY") or os.name == "nt" or sys.platform == "darwin":
        return None, {}
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise EngineError("the client needs a display: set DISPLAY or put Xvfb on PATH")
    for n in range(90, 110):
        if not Path(f"/tmp/.X11-unix/X{n}").exists():
            break
    proc = subprocess.Popen(
        [xvfb, f":{n}", "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 10
    while not Path(f"/tmp/.X11-unix/X{n}").exists():
        if time.time() > deadline or proc.poll() is not None:
            proc.kill()
            raise EngineError("Xvfb did not start")
        time.sleep(0.1)
    return proc, {"DISPLAY": f":{n}"}


def _run(cmd: list[str], cwd: Path, seconds: float, env: dict[str, str]) -> int | None:
    """Run for `seconds`, then stop it. None if it was still running (that is the good case).

    However the wait ends - the timeout, an interrupt, an error - the engine and anything it
    started are stopped before this returns (`procs.kill_tree`, which also works on Windows).
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env={**os.environ, **env},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **procs.group_kwargs(),
    )
    try:
        return proc.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return None
    finally:
        procs.kill_tree(proc)


def smoke(
    pk3: str | Path,
    profile_id: str,
    *,
    seconds: float = 12,
    client: bool = False,
    client_seconds: float = 40,
    keep: bool = False,
) -> dict[str, Any]:
    """Load the map in `pk3` in the engine, in a throwaway home folder, and report the log."""
    pk3 = Path(pk3)
    if not pk3.is_file():
        raise EngineError(f"{pk3} does not exist; run pack_pk3 first")
    eng = spec(profile_id)
    pkg = profiles.load(profile_id).get("packaging") or {}
    basepath = gamepath.basepath()
    if not basepath:
        raise EngineError("NRC_FS_BASEPATH is not set, so the engine has no game data")
    mapname = map_in(pk3)
    home = Path(tempfile.mkdtemp(prefix="nrc-smoke-"))
    try:
        gamedir = home / str(pkg.get("basegame") or eng.get("gamedir") or "")
        gamedir.mkdir(parents=True, exist_ok=True)
        shutil.copy(pk3, gamedir / pk3.name)
        subs = {"home": str(home), "basepath": basepath, "map": mapname}
        log = home / str(eng["log"]).format(**subs)
        runs = []

        server = find_binary(eng["server"], basepath, os.environ.get("NRC_ENGINE_SERVER"))
        # Find the client before the server runs: a client that cannot start must not turn the
        # server's verdict into "could not run", which callers read as a skip, not a failure.
        client_binary: Path | None = None
        client_skipped: str | None = None
        if client:
            try:
                client_binary = find_binary(
                    eng["client"], basepath, os.environ.get("NRC_ENGINE_CLIENT")
                )
            except EngineError as e:
                client_skipped = str(e)
        code = _run([str(server), *_expand(eng["server_args"], subs)], home, seconds, {})
        runs.append(
            {
                "level": "server",
                "binary": str(server),
                "exit": code,
                "exit_ok": code in (None, 0),
                **read_log(log.read_text(errors="replace") if log.is_file() else "", eng, mapname),
            }
        )

        display = env = None
        if client_binary is not None:
            try:
                display, env = _start_display()
            except EngineError as e:
                client_skipped = str(e)
        if client_binary is not None and env is not None:
            binary = client_binary
            log.unlink(missing_ok=True)
            for f in eng.get("client_files") or []:
                target = home / str(f["path"])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(f["text"]))
            try:
                code = _run(
                    [str(binary), *_expand(eng["client_args"], subs)], home, client_seconds, env
                )
            finally:
                if display:
                    display.terminate()
                    display.wait()
            runs.append(
                {
                    "level": "client",
                    "connected": bool(
                        eng.get("client_connected")
                        and log.is_file()
                        and eng["client_connected"] in log.read_text(errors="replace")
                    ),
                    "binary": str(binary),
                    "exit": code,
                    "exit_ok": code in (None, 0),
                    **read_log(
                        log.read_text(errors="replace") if log.is_file() else "", eng, mapname
                    ),
                }
            )

        ok = all(
            r["loaded"]
            and not r["fatal"]
            and r["exit_ok"]
            and (r["level"] != "client" or r["connected"])
            for r in runs
        )
        return {
            "ok": ok,
            "map": mapname,
            "package": str(pk3),
            "seconds": seconds,
            "runs": runs,
            # Asked for, but it could not start: `ok` is the server's verdict alone.
            "client_skipped": client_skipped,
            "home": str(home) if keep else None,
            "note": (
                "the server only proves the map loads and its script starts; pass client=True "
                "to also join as a spectator, which some errors need"
                if not client
                else f"the client could not start ({client_skipped}); only the server was checked"
                if client_skipped
                else "client connection and process exits are checked; this does not test objective completion"
            ),
        }
    finally:
        if not keep:
            shutil.rmtree(home, ignore_errors=True)
