"""Load a map with its bot waypoints in the real engine, with bots, and watch them.

The waypoint codec is pinned by round-tripping shipped files, but only the add-on itself can
say it accepts a seeded file, and only bots moving can say the graph is usable. This runs the
dedicated server on the packed map with the bot add-on enabled, in a throwaway game home and
a throwaway copy of the add-on folder (its library and scripts, with only the given waypoint
files in its nav folder), and reads three things back:

- the add-on's own log: did it load the file, and how many waypoints;
- a read-only observer module: every bot's position, team and stance every interval;
- the game log: objective events (planted, defused, built, captured, a team winning).

Everything game- and add-on-specific comes from the profile (`engine:` and `bots:`).
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

from . import botnav, engine, profiles


def _expand(args: list, subs: dict[str, str]) -> list[str]:
    return [str(a).format(**subs) for a in args]


def _samples(text: str, token: str) -> list[dict]:
    out = []
    pattern = re.compile(rf"NRC_BOTS {re.escape(token)} (\{{.*\}})\s*$")
    for line in text.splitlines():
        m = pattern.search(line)
        if m:
            try:
                out.append(json.loads(m.group(1)))
            except ValueError:
                continue
    return out


def movement(samples: list[dict], waypoints: list[botnav.Waypoint], reach: float) -> list[dict]:
    """Per bot: how far it travelled alive, how far it got from where it was first seen, the
    stances it was seen in, and how many distinct waypoints it passed within `reach` of."""
    tracks: dict[int, list[dict]] = {}
    for packet in samples:
        for bot in packet.get("bots") or []:
            tracks.setdefault(int(bot["client"]), []).append({**bot, "time": packet["time"]})
    rows = []
    for client, track in sorted(tracks.items()):
        travelled = 0.0
        for a, b in zip(track, track[1:], strict=False):
            if not a.get("dead") and not b.get("dead"):
                step = math.dist(a["position"], b["position"])
                # A respawn teleports; anything faster than a sprint between samples is one.
                if step < 2000:
                    travelled += step
        visited = set()
        for s in track:
            for i, w in enumerate(waypoints):
                if math.dist(s["position"], w.position) <= reach:
                    visited.add(i)
        rows.append(
            {
                "client": client,
                "team": track[-1].get("team"),
                "samples": len(track),
                "travelled": round(travelled, 1),
                "farthest_from_first_sample": round(
                    max(math.dist(track[0]["position"], s["position"]) for s in track), 1
                ),
                "stances_seen": sorted({s["stance"] for s in track if s.get("stance")}),
                "waypoints_passed": len(visited),
            }
        )
    return rows


def run(
    pk3: str | Path,
    profile_id: str,
    waypoint_files: list[str | Path],
    *,
    seconds: float = 60,
    bots: int = 6,
    addon_dir: str | Path | None = None,
    port: int = 0,
    interval_ms: int = 2000,
    keep: bool = False,
) -> dict[str, Any]:
    pk3 = Path(pk3)
    if not pk3.is_file():
        raise engine.EngineError(f"{pk3} does not exist; run pack_pk3 first")
    eng = engine.spec(profile_id)
    spec = botnav.spec(profile_id)
    play = spec.get("playtest") or {}
    pkg = profiles.load(profile_id).get("packaging") or {}
    basepath = os.environ.get("NRC_FS_BASEPATH")
    if not basepath:
        raise engine.EngineError("NRC_FS_BASEPATH is not set, so the engine has no game data")
    source = Path(
        str(addon_dir) if addon_dir else str(spec["install_dir"]).format(basepath=basepath)
    )
    if not source.is_dir():
        raise engine.EngineError(f"no bot add-on folder at {source}")
    mapname = engine.map_in(pk3)
    files = [Path(f) for f in waypoint_files]
    for f in files:
        if not f.is_file():
            raise engine.EngineError(f"{f} does not exist; run bot_waypoints_write first")
    wanted = botnav.install_path(spec, mapname)
    if not any(f.name == wanted.name for f in files):
        raise engine.EngineError(
            f"the add-on looks for {wanted.name}; none of the files given is named that"
        )

    root = Path(tempfile.mkdtemp(prefix="nrc-bots-"))
    try:
        addon = root / "addon"
        for part in spec.get("copy") or []:
            src = source / str(part)
            if src.is_dir():
                shutil.copytree(src, addon / str(part))
            elif src.is_file():
                (addon / str(part)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(src, addon / str(part))
        nav = addon / wanted.parent
        nav.mkdir(parents=True, exist_ok=True)
        for f in files:
            shutil.copy(f, nav / f.name)
        log_rel = Path(str(spec["log"]).format(map=mapname))
        (addon / log_rel.parent).mkdir(parents=True, exist_ok=True)
        config = addon / str(spec["config_path"])
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(str(spec.get("config") or ""))

        home = root / "home"
        gamedir = home / str(pkg.get("basegame") or eng.get("gamedir") or "")
        gamedir.mkdir(parents=True, exist_ok=True)
        shutil.copy(pk3, gamedir / pk3.name)
        token = secrets.token_hex(6)
        observer = profiles.profiles_dir() / str(play["observer"])
        target = home / str(play["observer_target"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            observer.read_text()
            .replace("__NRC_TOKEN__", token)
            .replace("__NRC_INTERVAL__", str(int(interval_ms)))
        )

        subs = {
            "home": str(home),
            "basepath": basepath,
            "map": mapname,
            "omnibot_path": str(addon),
            "port": str(int(port)),
            "bots": str(int(bots)),
        }
        server_args = list(eng["server_args"])
        at = server_args.index("{map}") - 1  # the map command and its argument go last
        args = [
            *_expand(server_args[:at], subs),
            *_expand(play.get("args") or [], subs),
            *_expand(server_args[at:], subs),
            *_expand(play.get("after_map") or [], subs),
        ]
        binary = engine.find_binary(eng["server"], basepath, os.environ.get("NRC_ENGINE_SERVER"))
        code = engine._run([str(binary), *args], home, seconds, {})

        game_log = home / str(eng["log"]).format(**subs)
        text = game_log.read_text(errors="replace") if game_log.is_file() else ""
        addon_log = addon / log_rel
        addon_text = addon_log.read_text(errors="replace") if addon_log.is_file() else ""
        loaded_rx = re.compile(str(spec["loaded"]).format(map=re.escape(mapname)))
        loads = [int(m.group(1)) for m in loaded_rx.finditer(addon_text)]
        errors = [
            line.strip()
            for line in addon_text.splitlines()
            if any(
                re.search(str(p).format(map=re.escape(mapname)), line)
                for p in spec.get("load_errors") or []
            )
        ]
        event_rx = [re.compile(str(p)) for p in play.get("events") or []]
        events = [
            line.strip()
            for line in text.splitlines()
            if "NRC_BOTS" not in line and any(p.search(line) for p in event_rx)
        ]
        version_rx = re.compile(str(play.get("version_line") or "$^"))
        version = [m.group(1) for m in map(version_rx.search, text.splitlines()) if m][:1]
        samples = _samples(text, token)
        waypoints = []
        for f in files:
            if f.name == wanted.name:
                waypoints = botnav.read(f.read_bytes(), spec["format"]).waypoints
        reach = float(spec["format"]["default_radius"]) * 2
        moves = movement(samples, waypoints, reach)
        log_check = engine.read_log(text, eng, mapname)
        moved = [m for m in moves if m["travelled"] > 0 and m["waypoints_passed"] > 1]
        report = {
            "ok": bool(loads) and not errors and not log_check["fatal"] and bool(moved),
            "map": mapname,
            "package": str(pk3),
            "addon": version[0] if version else None,
            "addon_source": str(source),
            "waypoints_loaded": loads,
            "load_errors": errors,
            "map_loaded": log_check["loaded"],
            "fatal": log_check["fatal"],
            "exit": code,
            "seconds": seconds,
            "bots_requested": bots,
            "bots_seen": len(moves),
            "bots_moving": len(moved),
            "samples": len(samples),
            "bots": moves,
            "events": events,
            "waypoint_reach": reach,
            "home": str(root) if keep else None,
            "note": (
                "bots_moving counts bots that travelled and passed within waypoint_reach of more "
                "than one waypoint. Events are the game's own objective messages. This is a "
                "bot run, not a balance verdict: bot skill, classes and the add-on's goal "
                "choices decide what they attempt."
            ),
        }
        if keep:
            report["game_log"] = str(game_log)
            report["addon_log"] = str(addon_log)
        return report
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
