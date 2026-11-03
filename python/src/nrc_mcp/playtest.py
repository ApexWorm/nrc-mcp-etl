"""Bounded gameplay scenarios driven by fresh engine telemetry.

The driver knows actions and predicates, not game commands or entity conventions. A profile
supplies those and a read-only observer. Every run has an isolated home and retained evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import engine, profiles
from .kernel import repo_root


class PlaytestError(engine.EngineError):
    pass


OPERATORS = ("eq", "ne", "gt", "gte", "lt", "lte", "near", "angle_near")
ACTIONS = {
    "join": ({"team", "role"}, set()),
    "position": ({"origin"}, {"angles"}),
    "aim": (set(), {"angles", "at"}),
    "weapon": ({"name"}, set()),
    "hold": ({"controls"}, {"until", "seconds"}),
    "wait": ({"until"}, set()),
    "assert": ({"until"}, set()),
    "screenshot": ({"name"}, set()),
}


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise PlaytestError(f"{name} must be a finite number between {low} and {high}")
    if not math.isfinite(value) or not low <= value <= high:
        raise PlaytestError(f"{name} must be a finite number between {low} and {high}")
    return float(value)


def _vector(value: Any, name: str) -> list[float]:
    if not isinstance(value, list) or len(value) != 3:
        raise PlaytestError(f"{name} must have three finite numbers")
    return [_number(v, name, -1_000_000, 1_000_000) for v in value]


def _target(at: Any, adapter: dict, watch: list) -> None:
    """An `aim` target: a fixed point, or a three-component field of an observation."""
    if isinstance(at, list):
        _vector(at, "aim at")
        return
    if not isinstance(at, dict) or set(at) - {"scope", "field", "name", "offset"}:
        raise PlaytestError(
            "aim at is [x, y, z] or {scope, field, optional name and offset} naming a vector field"
        )
    scope, field = at.get("scope"), at.get("field")
    fields = adapter["fields"].get(scope) if isinstance(scope, str) else None
    if (
        not isinstance(field, str)
        or not fields
        or any(f"{field}.{i}" not in fields for i in range(3))
    ):
        raise PlaytestError("aim at must name a vector field (field.0..2) the profile declares")
    if scope == "entity" and at.get("name") not in watch:
        raise PlaytestError("aim at an entity needs a name included in watch")
    if scope != "entity" and "name" in at:
        raise PlaytestError("name is only valid for an entity")
    if "offset" in at:
        _vector(at["offset"], "aim at offset")
    if not adapter.get("eye_height"):
        raise PlaytestError("the profile names no eye height field, so aim cannot look at a point")


def _look(snapshot: dict, at: Any, adapter: dict) -> list[float]:
    """Angles (pitch, yaw, roll) from the player's eye to `at` in this observation."""
    player = snapshot.get("player", {})
    position, height = player.get("position"), player.get(adapter["eye_height"])
    if not isinstance(position, list) or not isinstance(height, (int, float)):
        raise PlaytestError("the observation has no player position and eye height to aim from")
    if isinstance(at, list):
        point = list(at)
    else:
        row = (
            snapshot.get("entities", {}).get(at["name"], {})
            if at["scope"] == "entity"
            else snapshot.get(at["scope"], {})
        )
        point = row.get(at["field"]) if isinstance(row, dict) else None
        if not isinstance(point, list) or len(point) != 3:
            raise PlaytestError(f"the observation has no {at['scope']} {at['field']} to aim at")
        point = [p + o for p, o in zip(point, at.get("offset", [0, 0, 0]), strict=True)]
    eye = [position[0], position[1], position[2] + height]
    dx, dy, dz = (point[i] - eye[i] for i in range(3))
    # Positive pitch looks down.
    pitch = -math.degrees(math.atan2(dz, math.hypot(dx, dy)))
    return [round(pitch, 2), round(math.degrees(math.atan2(dy, dx)), 2), 0.0]


def spec(profile_id: str) -> dict:
    data = engine.spec(profile_id).get("playtest")
    if not isinstance(data, dict):
        raise PlaytestError(f"profile {profile_id} does not support gameplay scenarios")
    return data


def describe(profile_id: str) -> dict:
    """Discoverable scenario contract and the selected profile's supported inputs."""
    adapter = spec(profile_id)
    return {
        "profile": profile_id,
        "scenario": {"watch": "optional list of target/script names", "steps": "1..128 actions"},
        "actions": {
            k: {"required": sorted(a), "optional": sorted(b | {"label", "timeout"})}
            for k, (a, b) in ACTIONS.items()
        },
        "teams": list(adapter["teams"]),
        "roles": list(adapter["roles"]),
        "weapons": list(adapter["weapons"]),
        "controls": list(adapter["controls"]),
        "control_behavior": {
            name: {
                "mode": control.get("mode", "hold"),
                "note": control.get("note", "Active while held; release stops the input."),
            }
            for name, control in adapter["controls"].items()
        },
        "conditions": {
            "shape": {
                "scope": "player|game|entity",
                "field": "one of fields below",
                "op": "eq",
                "value": "scalar",
                "name": "required for entity",
                "tolerance": "required for near or angle_near",
            },
            "fields": adapter["fields"],
            "operators": OPERATORS,
            "until": "non-empty list; all predicates must match the same fresh snapshot",
            "absent_entity": "present=false also requires the observer to have seen that name earlier",
        },
        "limits": {
            "steps": 128,
            "watch": 32,
            "step_timeout_seconds": 120,
            "total_timeout_seconds": 600,
            "hold_seconds": 60,
        },
        "notes": [
            "join waits for a living player of the requested team and role",
            "position and aim wait for a living stationary player, and are recorded separately from gameplay",
            "aim takes angles, or `at`: a point [x, y, z] or a vector field of the latest observation "
            '({"scope": "game", "field": "charge_position"}, or an entity\'s "position" with its name), '
            "optionally with an offset; it looks there from the player's eye",
            "hold has either until or seconds; held controls release on success and failure",
            "wait observes a condition until timeout; assert checks one fresh acknowledged snapshot",
            "screenshots and all failed runs are retained; arbitrary console commands are not accepted",
            "the observer only reads state; this is a single-client scenario, not multiplayer balance testing",
        ],
        "example": adapter.get("example", {}),
    }


def validate(scenario: dict, adapter: dict) -> dict:
    """Reject typos and command injection before launching anything or creating output."""
    if not isinstance(scenario, dict) or set(scenario) - {"watch", "steps"}:
        raise PlaytestError("scenario has only watch and steps fields")
    watch = scenario.get("watch", [])
    if (
        not isinstance(watch, list)
        or len(watch) > 32
        or any(
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}", name)
            for name in watch
        )
    ):
        raise PlaytestError("watch must contain at most 32 simple target/script names")
    steps = scenario.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 128:
        raise PlaytestError("steps must contain 1..128 actions")
    for index, step in enumerate(steps):
        if (
            not isinstance(step, dict)
            or not isinstance(step.get("action"), str)
            or step["action"] not in ACTIONS
        ):
            raise PlaytestError(f"step {index}: unknown action; choose {', '.join(ACTIONS)}")
        action = step["action"]
        required, optional = ACTIONS[action]
        if required - set(step) or set(step) - required - optional - {"action", "label", "timeout"}:
            raise PlaytestError(
                f"step {index} {action}: expected fields {sorted(required)} plus {sorted(optional | {'label', 'timeout'})}"
            )
        if "label" in step and (not isinstance(step["label"], str) or len(step["label"]) > 128):
            raise PlaytestError("label must be text of at most 128 characters")
        _number(step.get("timeout", 30), "timeout", 0.1, 120)
        if action == "join" and (
            not isinstance(step["team"], str)
            or not isinstance(step["role"], str)
            or step["team"] not in adapter["teams"]
            or step["role"] not in adapter["roles"]
        ):
            raise PlaytestError("join team/role must be listed by the profile playtest resource")
        if action in {"position", "aim"}:
            if "origin" in step:
                _vector(step["origin"], "origin")
            _vector(step.get("angles", [0, 0, 0]), "angles (pitch, yaw, roll)")
        if action == "aim":
            if ("angles" in step) == ("at" in step):
                raise PlaytestError("aim takes exactly one of angles or at")
            if "at" in step:
                _target(step["at"], adapter, watch)
        if action == "weapon" and (
            not isinstance(step["name"], str) or step["name"] not in adapter["weapons"]
        ):
            raise PlaytestError("weapon name must be listed by the profile playtest resource")
        if action == "screenshot" and (
            not isinstance(step["name"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", step["name"])
        ):
            raise PlaytestError(
                "screenshot name must be 1..64 letters, digits, underscores or hyphens"
            )
        if action == "hold":
            controls = step["controls"]
            if (
                not isinstance(controls, list)
                or not controls
                or any(not isinstance(c, str) for c in controls)
                or len(set(controls)) != len(controls)
                or any(c not in adapter["controls"] for c in controls)
            ):
                raise PlaytestError(
                    "controls must be a non-empty list of unique profile control names"
                )
            if ("until" in step) == ("seconds" in step):
                raise PlaytestError("hold requires exactly one of until or seconds")
            if "seconds" in step:
                _number(step["seconds"], "hold seconds", 0.01, 60)
        if "until" in step:
            conditions = step["until"]
            if not isinstance(conditions, list) or not 1 <= len(conditions) <= 32:
                raise PlaytestError("until must be a non-empty list of at most 32 predicates")
            for c in conditions:
                if (
                    not isinstance(c, dict)
                    or set(c) - {"scope", "name", "field", "op", "value", "tolerance"}
                    or not {"scope", "field", "value"} <= set(c)
                ):
                    raise PlaytestError(
                        "a condition needs scope, field, value and optional op/name/tolerance"
                    )
                scope = c["scope"]
                if (
                    not isinstance(scope, str)
                    or not isinstance(c["field"], str)
                    or scope not in adapter["fields"]
                    or c["field"] not in adapter["fields"][scope]
                ):
                    raise PlaytestError("condition scope/field is not declared by the profile")
                if scope == "entity" and c.get("name") not in watch:
                    raise PlaytestError("entity conditions require a name included in watch")
                if scope != "entity" and "name" in c:
                    raise PlaytestError("name is only valid for entity conditions")
                op = c.get("op", "eq")
                if op not in OPERATORS:
                    raise PlaytestError(f"unknown condition operator {op}")
                if c["value"] is None or not isinstance(c["value"], (str, bool, int, float)):
                    raise PlaytestError("condition value must be a non-null scalar")
                if isinstance(c["value"], (float, int)) and not isinstance(c["value"], bool):
                    _number(c["value"], "condition value", -1e12, 1e12)
                if op not in {"eq", "ne"}:
                    _number(c["value"], "numeric condition value", -1e12, 1e12)
                if op in {"near", "angle_near"}:
                    _number(c.get("tolerance"), "near tolerance", 0, 1e6)
                elif "tolerance" in c:
                    raise PlaytestError("tolerance is only valid for near or angle_near")
    return {"watch": list(dict.fromkeys(watch)), "steps": steps}


def matches(snapshot: dict, condition: dict) -> bool:
    scope = condition["scope"]
    row = (
        snapshot.get("entities", {}).get(condition["name"], {})
        if scope == "entity"
        else snapshot.get(scope, {})
    )
    if not isinstance(row, dict):
        return False
    if (
        scope == "entity"
        and condition["field"] == "present"
        and row.get("present") is False
        and not row.get("seen")
    ):
        return False
    value: Any = row
    for key in condition["field"].split("."):
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdecimal() and int(key) < len(value):
            value = value[int(key)]
        else:
            return False
    if value is None:
        return False
    expected = condition["value"]
    op = condition.get("op", "eq")
    if op in {"eq", "ne"}:
        same = value == expected and isinstance(value, bool) == isinstance(expected, bool)
        return same if op == "eq" else not same
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        return False
    return {
        "gt": lambda: value > expected,
        "gte": lambda: value >= expected,
        "lt": lambda: value < expected,
        "lte": lambda: value <= expected,
        "near": lambda: abs(value - expected) <= condition["tolerance"],
        "angle_near": lambda: abs((value - expected + 180) % 360 - 180) <= condition["tolerance"],
    }[op]()


class Telemetry:
    """Only complete, run-tagged observer lines are snapshots; echoed text is not."""

    def __init__(self, token: str):
        self.pattern = re.compile(
            r"^\s*(?:\d+\s+)?NRC_PLAYTEST " + re.escape(token) + r" (\{.*\})$"
        )
        self.version = 0
        self.snapshot: dict = {}

    def feed(self, line: str) -> bool:
        match = self.pattern.fullmatch(line)
        if not match:
            return False
        try:
            snapshot = json.loads(match[1])
        except json.JSONDecodeError:
            return False
        if not isinstance(snapshot, dict) or not all(
            isinstance(snapshot.get(k), dict) for k in ("player", "game", "entities")
        ):
            return False
        ack = snapshot.get("ack")
        if isinstance(ack, bool) or not isinstance(ack, int) or ack < 0:
            return False
        self.version += 1
        self.snapshot = snapshot
        return True


class Driver:
    def __init__(
        self,
        proc,
        log: Path,
        output: Path,
        token: str,
        eng: dict,
        mapname: str,
        deadline: float,
        sync_command: str,
    ):
        self.proc, self.log, self.output = proc, log, output
        self.telemetry = Telemetry(token)
        self.eng, self.mapname, self.deadline = eng, mapname, deadline
        self.offset, self.partial = 0, ""
        self.sync_command, self.sequence = sync_command, 0
        self.deadline_kind = "total playtest"
        self.observations: list[tuple[int, dict]] = []
        self.last_match: tuple[int, dict] = (0, {})

    def send(self, commands: list[str], *, sync=True):
        if sync:
            self.sequence += 1
            commands = [*commands, self.sync_command.format(sequence=self.sequence)]
        command = "; ".join(commands)
        with (self.output / "commands.jsonl").open("a") as f:
            f.write(json.dumps({"elapsed_monotonic": time.monotonic(), "command": command}) + "\n")
        try:
            self.proc.stdin.write(command + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise PlaytestError("engine command pipe closed") from e

    def poll(self):
        self.observations = []
        if self.log.is_file():
            with self.log.open(errors="replace") as f:
                f.seek(self.offset)
                chunk = f.read(65536)
                self.offset = f.tell()
            lines = (self.partial + chunk).split("\n")
            self.partial = lines.pop()
            if len(self.partial) > 65536:
                raise PlaytestError("engine emitted an oversized log line")
            for line in lines:
                if self.telemetry.feed(line):
                    self.observations.append((self.telemetry.version, self.telemetry.snapshot))
                    with (self.output / "telemetry.jsonl").open("a") as f:
                        f.write(
                            json.dumps(
                                {"observation": self.telemetry.version, **self.telemetry.snapshot}
                            )
                            + "\n"
                        )
                if any(re.search(p, line) for p in self.eng.get("fatal", [])):
                    raise PlaytestError(f"engine stopped: {line.strip()}")
        if self.proc.poll() is not None:
            raise PlaytestError(f"engine exited during scenario with status {self.proc.returncode}")
        if time.monotonic() >= self.deadline:
            raise PlaytestError(f"{self.deadline_kind} deadline expired")

    def wait(
        self, conditions: list[dict], timeout: float, *, after: int | None = None, assert_once=False
    ):
        baseline = self.telemetry.version if after is None else after
        until = min(time.monotonic() + timeout, self.deadline)
        while time.monotonic() < until:
            self.poll()
            eligible = [
                (version, state)
                for version, state in self.observations
                if version > baseline and state.get("ack", 0) >= self.sequence
            ]
            if eligible:
                version, snapshot = eligible[0] if assert_once else eligible[-1]
                if all(matches(snapshot, c) for c in conditions):
                    self.last_match = (version, snapshot)
                    return snapshot
                if assert_once:
                    raise PlaytestError("assertion did not match the next fresh engine snapshot")
            time.sleep(0.05)
        raise PlaytestError(f"state wait timed out after {timeout:g}s: {json.dumps(conditions)}")

    def duration(self, seconds: float):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            self.poll()
            time.sleep(min(0.05, max(0, until - time.monotonic())))


def _predicate(scope: str, field: str, value: Any, **kwargs) -> dict:
    return {"scope": scope, "field": field, "value": value, **kwargs}


def _step(driver: Driver, step: dict, adapter: dict, home: Path, index: int) -> dict:
    action, timeout = step["action"], step.get("timeout", 30)
    before = driver.telemetry.version
    if action == "join":
        driver.send(
            [
                adapter["commands"]["join"].format(
                    team=adapter["teams"][step["team"]], role=adapter["roles"][step["role"]]
                )
            ]
        )
        driver.wait(
            [
                _predicate("player", "team", step["team"]),
                _predicate("player", "role", step["role"]),
                _predicate("player", "alive", True),
            ],
            timeout,
            after=before,
        )
    elif action in {"position", "aim"}:
        snapshot = driver.wait(
            [_predicate("player", "alive", True), _predicate("player", "speed", 1, op="lt")],
            timeout,
        )
        position = step.get("origin", snapshot["player"]["position"])
        angles = (
            _look(snapshot, step["at"], adapter) if "at" in step else step.get("angles", [0, 0, 0])
        )
        driver.send(
            [
                adapter["commands"]["position"].format(
                    x=position[0],
                    y=position[1],
                    z=position[2],
                    pitch=angles[0],
                    yaw=angles[1],
                    roll=angles[2],
                )
            ]
        )
        driver.wait(
            [
                _predicate("player", f"position.{i}", value, op="near", tolerance=2)
                for i, value in enumerate(position)
            ]
            + [
                _predicate("player", f"angles.{i}", value, op="angle_near", tolerance=1)
                for i, value in enumerate(angles)
            ],
            timeout,
        )
    elif action == "weapon":
        weapon = adapter["weapons"][step["name"]]
        driver.send([weapon["command"]])
        driver.wait([_predicate("player", "weapon", step["name"])], timeout, after=before)
    elif action == "hold":
        driver.wait([_predicate("player", "alive", True)], timeout)
        start = driver.telemetry.version
        driver.send([adapter["controls"][name]["press"] for name in step["controls"]])
        succeeded = False
        try:
            if "until" in step:
                driver.wait(step["until"], timeout, after=start)
            else:
                driver.wait([_predicate("player", "alive", True)], timeout, after=start)
                driver.duration(step["seconds"])
                driver.wait([_predicate("player", "alive", True)], timeout)
            succeeded = True
        finally:
            driver.send(
                [adapter["controls"][name]["release"] for name in reversed(step["controls"])]
            )
            if succeeded:
                completed = driver.last_match
                driver.wait([], timeout)
                driver.last_match = completed
    elif action in {"wait", "assert"}:
        driver.wait(step["until"], timeout, assert_once=action == "assert")
    elif action == "screenshot":
        name = f"{index:03d}-{step['name']}"
        path = home / adapter["screenshot_path"].format(name=name)
        driver.send([adapter["commands"]["screenshot"].format(name=name)])
        until = min(time.monotonic() + timeout, driver.deadline)
        while time.monotonic() < until:
            driver.poll()
            if path.is_file() and path.read_bytes().endswith(b"\xff\xd9"):
                return {"screenshot": str(path)}
            time.sleep(0.05)
        raise PlaytestError("engine did not produce the requested screenshot")
    return {"observation": driver.last_match[0], "state": driver.last_match[1]}


def _inside(home: Path, relative: str) -> Path:
    """Profile typos must not write outside the isolated engine home."""
    candidate = (home / relative).resolve()
    if (
        Path(relative).is_absolute()
        or not candidate.is_relative_to(home.resolve())
        or candidate == home.resolve()
    ):
        raise PlaytestError(f"profile path escapes isolated home: {relative}")
    return candidate


def _stop(proc, *, group: bool):
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            if group:
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def run(
    pk3: str | Path,
    profile_id: str,
    scenario: dict,
    *,
    timeout_seconds: float = 180,
    depends: list[str | Path] | None = None,
) -> dict:
    """Run a scenario in an isolated home, retaining success and failure evidence.

    `depends` are other archives the map needs (its textures and models); they are linked into
    the game home beside the package.
    """
    _number(timeout_seconds, "timeout_seconds", 1, 600)
    adapter = spec(profile_id)
    scenario = validate(scenario, adapter)
    if os.name != "posix":
        raise PlaytestError("this console transport currently requires a POSIX host")
    package = Path(pk3).resolve()
    if not package.is_file():
        raise PlaytestError(f"{package} does not exist; pack the map first")
    mapname = engine.map_in(package)
    others = [Path(d).resolve() for d in depends or []]
    for d in others:
        if not d.is_file():
            raise PlaytestError(f"{d} does not exist")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", mapname):
        raise PlaytestError(
            "package map name must contain only letters, digits, underscores or hyphens"
        )
    basepath = os.environ.get("NRC_FS_BASEPATH")
    if not basepath:
        raise PlaytestError("NRC_FS_BASEPATH is not set")
    eng = engine.spec(profile_id)
    virtual_home = repo_root() / "out/playtests/path-validation-home"
    for relative in [
        profiles.load(profile_id)["packaging"]["basegame"],
        eng["log"],
        adapter["observer_target"],
        adapter["screenshot_path"].format(name="probe"),
        *[f["path"] for f in eng.get("client_files", [])],
    ]:
        _inside(virtual_home, relative)
    binary = engine.find_binary(eng["client"], basepath, os.environ.get("NRC_ENGINE_CLIENT"))
    observer_path = (profiles.profiles_dir() / adapter["observer"]).resolve()
    if not observer_path.is_relative_to(profiles.profiles_dir().resolve()):
        raise PlaytestError("observer must live beneath profiles/")
    observer = observer_path.read_text()
    runs = repo_root() / "out/playtests"
    runs.mkdir(parents=True, exist_ok=True)
    import fcntl

    with (runs / ".engine.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise PlaytestError("another gameplay scenario is using the engine") from e
        return _run(
            package,
            profile_id,
            scenario,
            timeout_seconds,
            adapter,
            eng,
            binary,
            basepath,
            observer,
            runs,
            mapname,
            others,
        )


def _run(
    package,
    profile_id,
    scenario,
    timeout_seconds,
    adapter,
    eng,
    binary,
    basepath,
    observer,
    runs,
    mapname,
    depends=(),
):
    output = Path(tempfile.mkdtemp(prefix=f"{mapname}-", dir=runs))
    home = output / "home"
    package_dir = _inside(home, profiles.load(profile_id)["packaging"]["basegame"])
    package_dir.mkdir(parents=True)
    shutil.copy2(package, package_dir / package.name)
    for other in depends:
        (package_dir / other.name).symlink_to(other)
    token = uuid.uuid4().hex
    result = {
        "ok": False,
        "profile": profile_id,
        "map": mapname,
        "package": str(package),
        "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        "output": str(output),
        "scenario": scenario,
        "steps": [],
        "screenshots": [],
        "setup_actions": [
            i for i, s in enumerate(scenario["steps"]) if s["action"] in {"position", "aim"}
        ],
        "note": "Single-client engine probe; positioning/aim are explicit setup and observer only reads state.",
    }
    (output / "scenario.json").write_text(json.dumps(scenario, indent=2) + "\n")
    for item in eng.get("client_files", []):
        path = _inside(home, item["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(item["text"])
    observer_target = _inside(home, adapter["observer_target"])
    observer_target.parent.mkdir(parents=True, exist_ok=True)
    watched = "{" + ",".join("[" + json.dumps(name) + "]=true" for name in scenario["watch"]) + "}"
    observer_target.write_text(
        observer.replace("__NRC_TOKEN__", token).replace("__NRC_WATCHED__", watched)
    )
    subs = {"home": str(home), "basepath": basepath, "map": mapname}
    command = [str(binary), *adapter.get("args", []), *engine._expand(eng["client_args"], subs)]
    result["command"] = command
    (output / "command.json").write_text(json.dumps(command, indent=2) + "\n")
    display = proc = driver = None
    started = time.monotonic()
    try:
        display, env = engine._start_display()
        with (output / "process.log").open("w") as log:
            proc = subprocess.Popen(
                command,
                cwd=home,
                env={**os.environ, **env},
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            driver = Driver(
                proc,
                _inside(home, eng["log"]),
                output,
                token,
                eng,
                mapname,
                started + timeout_seconds,
                adapter["commands"]["sync"],
            )
            driver.wait([_predicate("player", "connected", True)], min(45, timeout_seconds))
            driver.send(adapter["bootstrap"], sync=False)
            driver.wait([_predicate("game", "phase", "playing")], min(30, timeout_seconds))
            for index, step in enumerate(scenario["steps"]):
                start = time.monotonic()
                row = {"index": index, "action": step, "ok": False}
                result["steps"].append(row)
                overall_deadline = driver.deadline
                overall_kind = driver.deadline_kind
                driver.deadline = min(overall_deadline, start + step.get("timeout", 30))
                if driver.deadline < overall_deadline:
                    driver.deadline_kind = f"step {index} {step['action']}"
                try:
                    row.update(_step(driver, step, adapter, home, index))
                    row["ok"] = True
                    if "screenshot" in row:
                        result["screenshots"].append(row["screenshot"])
                finally:
                    driver.deadline = overall_deadline
                    driver.deadline_kind = overall_kind
                    row["seconds"] = round(time.monotonic() - start, 3)
            result["ok"] = True
    except (PlaytestError, OSError, ValueError) as e:
        result["error"] = str(e)
    finally:
        if driver:
            result["final_state"] = driver.telemetry.snapshot
        if proc and proc.poll() is None:
            try:
                if driver:
                    driver.send(
                        [c["release"] for c in adapter["controls"].values()]
                        + [adapter["commands"]["quit"]],
                        sync=False,
                    )
            except PlaytestError:
                pass
            _stop(proc, group=True)
        if proc:
            result["exit_code"] = proc.returncode
            if proc.stdin:
                with suppress(OSError):
                    proc.stdin.close()
            result["ok"] = result["ok"] and proc.returncode == 0
        if display:
            with suppress(ProcessLookupError):
                display.terminate()
            _stop(display, group=False)
        text = (
            (home / eng["log"]).read_text(errors="replace") if (home / eng["log"]).exists() else ""
        )
        result.update(engine.read_log(text, eng, mapname))
        result["connected"] = bool(eng.get("client_connected") and eng["client_connected"] in text)
        result["ok"] = (
            result["ok"] and result["loaded"] and result["connected"] and not result["fatal"]
        )
        result["seconds"] = round(time.monotonic() - started, 3)
        result["result_path"] = str(output / "result.json")
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
