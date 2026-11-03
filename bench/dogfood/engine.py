"""Exercise ET engineer controls in an isolated client home and retain evidence.

This is a development gameplay probe: devmap and setviewpos prepare the player;
ordinary team/weapon/+attack commands perform construction or plant dynamite.
The optional Lua module only reads state and writes logs. It never calls scripts,
changes entities, grants weapons, or damages objectives.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "python/src"))
from nrc_mcp import engine, profiles  # noqa: E402

OBSERVER = r"""
local watched = __WATCHED__
local last = {}
local missiles = {}
local nextSample = 0
local function read(n, key)
  local ok, value = pcall(et.gentity_get, n, key)
  if not ok or value == nil then return "?" end
  if type(value) == "table" then
    local out = {}
    for i = 1, 3 do out[i] = tostring(value[i] or "?") end
    return table.concat(out, ",")
  end
  return tostring(value)
end
local function emit(text)
  et.G_Print("DOGFOOD " .. text .. "\n")
end
local function entities(label, changesOnly)
  local found = {}
  for n = 64, 1021 do
    if read(n, "inuse") == "1" then
      local name = read(n, "targetname")
      local script = read(n, "scriptName")
      if read(n, "s.weapon") == "15" then
        local missile = "id=" .. n .. " health=" .. read(n, "health")
          .. " origin=" .. read(n, "r.currentOrigin")
        if not changesOnly or missile ~= missiles[n] then emit("DYNAMITE " .. label .. " " .. missile) end
        missiles[n] = missile
      end
      if watched[name] or watched[script] then
        found[n] = true
        local state = "id=" .. n .. " target=" .. name .. " script=" .. script
          .. " class=" .. read(n, "classname") .. " health=" .. read(n, "health")
          .. " state=" .. read(n, "entstate") .. " progress=" .. read(n, "s.angles2")
          .. " origin=" .. read(n, "r.currentOrigin")
        if not changesOnly or state ~= last[n] then emit("ENTITY " .. label .. " " .. state) end
        last[n] = state
      end
    end
  end
  for n, previous in pairs(last) do
    if type(n) == "number" and not found[n] and previous ~= "absent" then
      emit("ENTITY " .. label .. " id=" .. n .. " absent=true previous=" .. previous)
      last[n] = "absent"
    end
  end
  local winner = et.trap_GetConfigstring(et.CS_MULTI_MAPWINNER)
  local game = "gamestate=" .. et.trap_Cvar_Get("gamestate") .. " winner=" .. winner
  if not changesOnly or game ~= last.game then emit("GAME " .. label .. " " .. game) end
  last.game = game
end
function et_InitGame(time, seed, restart)
  emit("OBSERVER_READY time=" .. time)
end
function et_RunFrame(time)
  if time >= nextSample then
    entities("time=" .. time, true)
    nextSample = time + 500
  end
end
function et_ClientCommand(client, command)
  if command ~= "dogfood_probe" then return 0 end
  local label = et.trap_Argv(1)
  emit("PLAYER " .. label .. " client=" .. client
    .. " team=" .. read(client, "sess.sessionTeam")
    .. " class=" .. read(client, "sess.playerType")
    .. " health=" .. read(client, "health")
    .. " pm_type=" .. read(client, "ps.pm_type")
    .. " viewheight=" .. read(client, "ps.viewheight")
    .. " weapon=" .. read(client, "ps.weapon")
    .. " origin=" .. read(client, "ps.origin")
    .. " angles=" .. read(client, "ps.viewangles"))
  entities(label, false)
  return 1
end
"""


def config(args):
    """Cbuf runs twice per 60fps frame; wait 3600 covers a 30s respawn wave."""
    commands = [
        "wait 180",
        "map_restart 0",
        "wait 180",
        "dogfood_probe begin",
        "cg_draw2D 1",
        "team axis 2",
        f"wait {int(args.axis_wait * 120)}",
        "dogfood_probe axis_spawn",
        "screenshotJPEG 01_axis_spawn",
        "wait 30",
        "team allies 2",
        f"wait {int(args.allies_wait * 120)}",
        "dogfood_probe allies_spawn",
        "screenshotJPEG 02_allies_spawn",
        "wait 30",
    ]
    if args.team == "axis":
        commands += ["team axis 2", f"wait {int(args.axis_wait * 120)}"]
    if args.position:
        commands += [
            "setviewpos " + " ".join(map(str, [*args.position, args.pitch, args.yaw, 0, 0])),
            "wait 90",
        ]
    commands += [
        "weaponbank 5",
        "wait 60",
        "dogfood_probe before",
        "screenshotJPEG 03_before",
        "wait 30",
    ]
    if args.mode == "construct":
        commands += [
            "+attack",
            "wait 180",
            "dogfood_probe building",
            "screenshotJPEG 04_building",
            "wait 3600",
            "-attack",
            "wait 60",
        ]
    elif args.mode == "dynamite":
        commands += [
            "weaponbank 6",
            "wait 60",
            "+attack",
            "wait 30",
            "-attack",
            "wait 120",
            "dogfood_probe thrown",
        ]
        if args.arm_yaw is not None or args.arm_pitch is not None:
            if not args.position:
                raise ValueError("arm angles require --position")
            commands += [
                "setviewpos "
                + " ".join(
                    map(
                        str,
                        [
                            *args.position,
                            args.arm_pitch if args.arm_pitch is not None else args.pitch,
                            args.arm_yaw if args.arm_yaw is not None else args.yaw,
                            0,
                            0,
                        ],
                    )
                ),
                "wait 30",
            ]
        commands += [
            "weaponbank 5",
            "wait 60",
            "+attack",
            "wait 600",
            "-attack",
            "dogfood_probe armed",
            "screenshotJPEG 04_armed",
            "wait 4200",
        ]
    elif args.mode == "movement":
        for lane, y, posture, duration in [
            ("standing", 128, None, 4),
            ("crouch", 384, "movedown", 14),
            ("prone", 640, "prone", 15),
        ]:
            commands += [f"setviewpos 320 {y} 24 0 0 0 0", "wait 60"]
            if posture:
                commands += [
                    "+forward",
                    "wait 360",
                    "-forward",
                    "wait 30",
                    f"dogfood_probe {lane}_blocked",
                    f"screenshotJPEG {lane}_blocked",
                ]
                commands += [f"+{posture}", "wait 60"]
                if posture == "prone":
                    commands += ["-prone", "wait 60"]
            commands += [
                "+forward",
                f"wait {duration * 120}",
                "-forward",
                "wait 30",
                f"dogfood_probe {lane}_exit",
                f"screenshotJPEG {lane}_exit",
                "wait 30",
            ]
            if posture == "movedown":
                commands += ["-movedown", "wait 60"]
    commands += [
        "dogfood_probe after",
        "screenshotJPEG 05_after",
        "wait 60",
        "dogfood_probe completed",
        "wait 30",
        "quit",
    ]
    return "\n".join(commands) + "\n"


def movement_checks(probes):
    """Evaluate the recorded positions and stances, including real alive exits."""
    players = {}
    for line in probes:
        match = re.search(r"DOGFOOD PLAYER (\w+) (.*)", line)
        if match:
            players[match[1]] = dict(re.findall(r"(\w+)=([^ ]+)", match[2]))
    checks = {}
    for lane, y, height in [("standing", 128, 40), ("crouch", 384, 16), ("prone", 640, -8)]:
        row = players.get(lane + "_exit", {})
        position = [float(value) for value in row.get("origin", "0,0,0").split(",")]
        checks[f"{lane}_passed"] = position[0] > 1152 and abs(position[1] - y) < 24
        checks[f"{lane}_alive"] = int(row.get("health", "0")) > 0 and row.get("pm_type") == "0"
        checks[f"{lane}_stance"] = row.get("viewheight") == str(height)
    for lane in ["crouch", "prone"]:
        row = players.get(lane + "_blocked", {})
        x = float(row.get("origin", "9999,0,0").split(",")[0])
        checks[f"standing_blocked_{lane}"] = 320 <= x < 384 and row.get("viewheight") == "40"
    return checks


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("pk3", type=Path)
    p.add_argument("--profile", default="etl")
    p.add_argument(
        "--basepath",
        default=os.environ.get("NRC_FS_BASEPATH"),
        required=not os.environ.get("NRC_FS_BASEPATH"),
    )
    p.add_argument(
        "--mode", choices=["spawn", "construct", "dynamite", "movement"], default="spawn"
    )
    p.add_argument("--team", choices=["axis", "allies"], default="allies")
    p.add_argument("--position", type=float, nargs=3)
    p.add_argument("--pitch", type=float, default=55)
    p.add_argument("--yaw", type=float, default=180)
    p.add_argument("--arm-pitch", type=float)
    p.add_argument("--arm-yaw", type=float)
    p.add_argument(
        "--axis-wait", type=float, default=32.5, help="Seconds to cover the map's Axis wave"
    )
    p.add_argument(
        "--allies-wait", type=float, default=32.5, help="Seconds to cover the map's Allied wave"
    )
    p.add_argument("--watch", action="append", default=[])
    p.add_argument(
        "--expect",
        action="append",
        default=[],
        help="Literal runtime log evidence required for success",
    )
    p.add_argument("--seconds", type=float, default=180)
    p.add_argument("--output", type=Path)
    args = p.parse_args()
    if args.mode in {"construct", "dynamite"} and not args.watch:
        p.error("objective modes require --watch to verify a named entity's state")
    pk3 = args.pk3.resolve(strict=True)
    eng = engine.spec(args.profile)
    game_profile = profiles.load(args.profile)
    mapname = engine.map_in(pk3)
    output = (
        args.output
        or REPO
        / "out/dogfood/engine"
        / (datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f") + "-" + mapname)
    ).resolve()
    output.mkdir(parents=True, exist_ok=False)
    home = output / "home"
    package_dir = home / game_profile["packaging"]["basegame"]
    package_dir.mkdir(parents=True)
    shutil.copy2(pk3, package_dir / pk3.name)
    for item in eng.get("client_files", []):
        path = home / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["text"])
    log_path = home / eng["log"]
    mod_dir = log_path.parent
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "dogfood.cfg").write_text(config(args))
    watched = "{" + ", ".join("[" + json.dumps(name) + "] = true" for name in args.watch) + "}"
    (mod_dir / "dogfood_observer.lua").write_text(OBSERVER.replace("__WATCHED__", watched))
    binary = engine.find_binary(eng["client"], args.basepath, os.environ.get("NRC_ENGINE_CLIENT"))
    subs = {"home": str(home), "basepath": str(Path(args.basepath).resolve()), "map": mapname}
    setup = []
    for key, value in {
        "lua_modules": "dogfood_observer.lua",
        "g_doWarmup": "0",
        "g_warmup": "0",
        "g_scriptDebug": "1",
        "g_teamForceBalance": "0",
        "g_inactivity": "0",
        "g_spectatorInactivity": "0",
        "sv_maxclients": "8",
        "g_log": "dogfood_game.log",
        "g_logSync": "1",
        "g_developer": "1",
        "g_gametype": "2",
    }.items():
        setup += ["+set", key, value]
    command = [
        str(binary),
        *setup,
        *engine._expand(eng["client_args"], subs),
        "+exec",
        "dogfood.cfg",
    ]
    (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    print(f"Engine artifacts: {output}", flush=True)
    display, env = engine._start_display()
    timed_out = False
    try:
        with (output / "process.log").open("w") as process_log:
            proc = subprocess.Popen(
                command,
                cwd=home,
                env={**os.environ, **env},
                stdout=process_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=args.seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    code = proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    code = proc.wait()
    finally:
        if display:
            display.terminate()
            display.wait(timeout=10)
    log = log_path.read_text(errors="replace") if log_path.exists() else ""
    game_log = (
        (mod_dir / "dogfood_game.log").read_text(errors="replace")
        if (mod_dir / "dogfood_game.log").exists()
        else ""
    )
    evidence = log + "\n" + game_log
    frames = sorted(str(path.relative_to(output)) for path in mod_dir.glob("screenshots/*"))
    probes = [line for line in log.splitlines() if "DOGFOOD " in line]
    missing = [expected for expected in args.expect if expected not in evidence]
    status = engine.read_log(log, eng, mapname)
    connected = eng.get("client_connected", "Going from CS_PRIMED to CS_ACTIVE") in log
    joined = {
        team: bool(
            re.search(rf"DOGFOOD PLAYER {team}_spawn .* team={num} class=2 health=[1-9]", log)
        )
        for team, num in [("axis", 1), ("allies", 2)]
    }
    objective_checks = {}
    observed_entities = {}
    for line in probes:
        match = re.search(r"DOGFOOD ENTITY .* id=(\d+) ", line)
        if match:
            observed_entities.setdefault(match[1], []).append(line)
    if args.mode in {"construct", "dynamite"}:
        objective_checks["winner_changed"] = "winner=\\w\\0" in log and "winner=\\w\\1" in log
        objective_checks["round_intermission"] = bool(
            re.search(r"DOGFOOD GAME .* gamestate=3 ", log)
        )
        if args.mode == "construct":
            objective_checks["built_state"] = any(
                any(" state=2 " in row for row in rows)
                and any(" progress=0.0,1.0,0.0" in row for row in rows)
                for rows in observed_entities.values()
            )
            objective_checks["building_state"] = bool(re.search(r"DOGFOOD ENTITY .* state=2 ", log))
        else:
            objective_checks["planted_by_player"] = "Dynamite_Plant: 0 " in game_log
            objective_checks["target_destroyed"] = any(
                re.search(r" health=[1-9]", rows[0])
                and any(
                    "absent=true" in row or re.search(r" health=0(?: |$)| health=-[1-9]", row)
                    for row in rows[1:]
                )
                for rows in observed_entities.values()
            )
    if args.mode == "movement":
        objective_checks = movement_checks(probes)
    result = {
        "map": mapname,
        "package": str(pk3),
        "mode": args.mode,
        "command": command,
        "setup_uses_teleport": bool(args.position) or args.mode == "movement",
        "actions": (
            "ordinary client +forward, +movedown and +prone commands"
            if args.mode == "movement"
            else "ordinary client weaponbank and +attack commands"
        ),
        "observer": "read-only Lua probes",
        "output": str(output),
        "exit_code": code,
        "timed_out": timed_out,
        **status,
        "connected": connected,
        "joined_engineer": joined,
        "completed_config": "DOGFOOD PLAYER completed " in log,
        "missing_expected": missing,
        "screenshots": frames,
        "probes": probes,
        "objective_checks": objective_checks,
    }
    result["ok"] = bool(
        status["loaded"]
        and connected
        and all(joined.values())
        and not status["fatal"]
        and not missing
        and not timed_out
        and result["completed_config"]
        and len(frames) >= 3
        and all(objective_checks.values())
    )
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {key: value for key, value in result.items() if key not in {"command", "probes"}},
            indent=2,
        )
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
