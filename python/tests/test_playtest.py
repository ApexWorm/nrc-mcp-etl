"""Fresh telemetry, bounded actions and cleanup are the playtest driver's contract."""

from __future__ import annotations

import copy
import io
import json
import stat
import subprocess
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from nrc_mcp import playtest, profiles


@pytest.fixture
def adapter():
    return copy.deepcopy(playtest.spec("etl"))


@pytest.mark.parametrize(
    "step",
    [
        {"action": []},
        {"action": "exec", "command": "quit"},
        {"action": "join", "team": [], "role": "engineer"},
        {"action": "join", "team": "blue; quit", "role": "engineer"},
        {"action": "weapon", "name": {}},
        {"action": "hold", "controls": [{}], "seconds": 1},
        {"action": "hold", "controls": ["attack"], "seconds": float("inf")},
        {"action": "hold", "controls": ["attack"], "seconds": 1, "until": []},
        {"action": "hold", "controls": ["attack", "attack"], "seconds": 1},
        {"action": "screenshot", "name": "../escape"},
        {"action": "position", "origin": [1, 2, float("nan")]},
        {"action": "wait", "until": [{"scope": {}, "field": "alive", "value": True}]},
        {"action": "wait", "until": [{"scope": "player", "field": "bogus", "value": True}]},
        {
            "action": "wait",
            "until": [{"scope": "entity", "name": "unwatched", "field": "present", "value": True}],
        },
        {
            "action": "wait",
            "until": [
                {"scope": "player", "field": "health", "op": "near", "value": 0, "tolerance": -1}
            ],
        },
        {"action": "wait", "until": [], "timeout": True},
    ],
)
def test_invalid_scenarios_are_rejected_before_launch(adapter, step):
    with pytest.raises(playtest.PlaytestError):
        playtest.validate({"steps": [step]}, adapter)


def test_profile_example_is_discoverable_and_valid(adapter):
    assert playtest.validate(adapter["example"], adapter)["steps"]
    contract = playtest.describe("etl")
    assert "hold" in contract["actions"]
    assert "alive" in contract["conditions"]["fields"]["player"]
    assert "prone" in contract["controls"]


def test_telemetry_does_not_accept_echoes_wrong_tokens_or_partial_json():
    state = {"player": {"alive": True}, "game": {}, "entities": {}, "ack": 3}
    line = "   1250 NRC_PLAYTEST secret " + json.dumps(state)
    reader = playtest.Telemetry("secret")
    for invalid in [
        "echo " + line,
        line.replace("secret", "wrong"),
        line[:-1],
        "NRC_PLAYTEST secret []",
    ]:
        assert not reader.feed(invalid)
    assert reader.version == 0
    assert reader.feed(line)
    assert reader.snapshot == state


def test_absence_requires_seen_entity_and_missing_values_never_pass():
    condition = {"scope": "entity", "name": "bridge", "field": "present", "value": False}
    assert not playtest.matches({"entities": {}}, condition)
    assert not playtest.matches(
        {"entities": {"bridge": {"present": False, "seen": False}}}, condition
    )
    assert playtest.matches({"entities": {"bridge": {"present": False, "seen": True}}}, condition)
    assert not playtest.matches(
        {"player": {}}, {"scope": "player", "field": "health", "op": "ne", "value": 0}
    )
    assert not playtest.matches(
        {"player": {"alive": 1}}, {"scope": "player", "field": "alive", "value": True}
    )
    assert playtest.matches(
        {"player": {"angles": [0, -180, 0]}},
        {
            "scope": "player",
            "field": "angles.1",
            "op": "angle_near",
            "value": 180,
            "tolerance": 0.1,
        },
    )


def test_wait_ignores_old_snapshots_and_unacknowledged_commands(tmp_path):
    proc = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
    driver = playtest.Driver(
        proc,
        tmp_path / "game.log",
        tmp_path,
        "token",
        {},
        "m",
        time.monotonic() + 2,
        "sync {sequence}",
    )
    snapshot = {"player": {"alive": True}, "game": {}, "entities": {}, "ack": 0}
    driver.telemetry.feed("NRC_PLAYTEST token " + json.dumps(snapshot))
    driver.send(["join"])
    # A newly read line can still predate the command. Its acknowledgement must match too.
    (tmp_path / "game.log").write_text("NRC_PLAYTEST token " + json.dumps(snapshot) + "\n")
    with pytest.raises(playtest.PlaytestError, match="timed out"):
        driver.wait([{"scope": "player", "field": "alive", "value": True}], 0.1)
    snapshot["ack"] = 1
    with (tmp_path / "game.log").open("a") as f:
        f.write("NRC_PLAYTEST token " + json.dumps(snapshot) + "\n")
    assert driver.wait([{"scope": "player", "field": "alive", "value": True}], 0.2)["player"][
        "alive"
    ]


FAKE = r"""#!/usr/bin/env python3
import json,sys,time,threading,re
from pathlib import Path
home=Path(sys.argv[1]); (home/'mod').mkdir(exist_ok=True)
token=re.search(r'token = "([a-f0-9]+)"',(home/'mod/observer.lua').read_text())[1]
state={'ack':0,'player':{'connected':True,'alive':True,'team':'blue','role':'engineer','health':100},'game':{'phase':'playing','winner':'red'},'entities':{}}
running=True
def inputs():
 global running
 for line in sys.stdin:
  with (home/'inputs.log').open('a') as f:f.write(line)
  m=re.search(r'sync (\d+)',line)
  if m:state['ack']=int(m[1])
  if 'quit' in line:
   running=False
   return
threading.Thread(target=inputs,daemon=True).start()
with (home/'mod/game.log').open('w') as f:
 f.write('Loaded m\nCONNECTED\n');f.flush()
 while running:
  f.write('NRC_PLAYTEST '+token+' '+json.dumps(state)+'\n');f.flush();time.sleep(.02)
(home/'stopped').write_text('clean')
"""


@pytest.fixture
def fake_install(tmp_path, monkeypatch, adapter):
    binary = tmp_path / "fake"
    binary.write_text(FAKE)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles/observer.lua").write_text('local token = "__NRC_TOKEN__"')
    adapter.update(
        observer="observer.lua", observer_target="mod/observer.lua", args=[], bootstrap=["restart"]
    )
    adapter["commands"].update(sync="sync {sequence}", quit="quit")
    eng = {
        "client": ["fake"],
        "client_args": ["{home}"],
        "log": "mod/game.log",
        "loaded": "Loaded {map}",
        "client_connected": "CONNECTED",
        "fatal": ["^FATAL"],
        "playtest": adapter,
    }
    monkeypatch.setattr(
        profiles, "load", lambda _: {"engine": eng, "packaging": {"basegame": "base"}}
    )
    monkeypatch.setattr(profiles, "profiles_dir", lambda: tmp_path / "profiles")
    monkeypatch.setattr(playtest, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(playtest.engine, "_start_display", lambda: (None, {}))
    monkeypatch.setenv("NRC_FS_BASEPATH", str(tmp_path))
    package = tmp_path / "m.pk3"
    with zipfile.ZipFile(package, "w") as f:
        f.writestr("maps/m.bsp", b"fixture")
    return package


def test_failed_held_action_releases_inputs_and_retains_failure(fake_install):
    result = playtest.run(
        fake_install,
        "fake",
        {
            "steps": [
                {
                    "action": "hold",
                    "controls": ["attack", "forward"],
                    "timeout": 0.2,
                    "until": [{"scope": "game", "field": "winner", "value": "blue"}],
                }
            ]
        },
        timeout_seconds=3,
    )
    assert not result["ok"] and result["exit_code"] == 0
    home = Path(result["output"]) / "home"
    inputs = (home / "inputs.log").read_text()
    assert "+attack; +forward" in inputs
    assert "-forward; -attack" in inputs
    assert "quit" in inputs and (home / "stopped").is_file()
    assert Path(result["result_path"]).is_file()
    assert result["steps"][0]["ok"] is False
    assert (Path(result["output"]) / "telemetry.jsonl").is_file()


def test_state_success_and_false_assertion_have_distinct_results(fake_install):
    good = playtest.run(
        fake_install,
        "fake",
        {
            "steps": [
                {"action": "wait", "until": [{"scope": "player", "field": "alive", "value": True}]}
            ]
        },
        timeout_seconds=3,
    )
    assert good["ok"] and good["connected"] and good["loaded"]
    bad = playtest.run(
        fake_install,
        "fake",
        {
            "steps": [
                {
                    "action": "assert",
                    "until": [{"scope": "game", "field": "winner", "value": "blue"}],
                }
            ]
        },
        timeout_seconds=3,
    )
    assert not bad["ok"] and "assertion" in bad["error"]
    assert Path(good["output"]) != Path(bad["output"])


def test_total_deadline_also_stops_a_duration_hold(fake_install):
    result = playtest.run(
        fake_install,
        "fake",
        {"steps": [{"action": "hold", "controls": ["forward"], "seconds": 20}]},
        timeout_seconds=1,
    )
    assert not result["ok"] and result["seconds"] < 2
    assert "-forward" in (Path(result["output"]) / "home/inputs.log").read_text()


def test_step_deadline_includes_duration_and_precondition_wait(fake_install):
    result = playtest.run(
        fake_install,
        "fake",
        {"steps": [{"action": "hold", "controls": ["forward"], "seconds": 20, "timeout": 0.2}]},
        timeout_seconds=3,
    )
    assert not result["ok"] and "step 0 hold deadline" in result["error"]
    assert result["seconds"] < 1
    assert "-forward" in (Path(result["output"]) / "home/inputs.log").read_text()


def test_absence_cannot_bypass_seen_guard_with_not_equal():
    assert not playtest.matches(
        {"entities": {"bridge": {"present": False, "seen": False}}},
        {"scope": "entity", "name": "bridge", "field": "present", "op": "ne", "value": True},
    )


def test_assert_uses_first_acknowledged_snapshot_even_in_a_batch(tmp_path):
    proc = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
    driver = playtest.Driver(
        proc,
        tmp_path / "game.log",
        tmp_path,
        "token",
        {},
        "m",
        time.monotonic() + 2,
        "sync {sequence}",
    )
    with driver.log.open("w") as f:
        for alive in (False, True):
            f.write(
                "NRC_PLAYTEST token "
                + json.dumps({"ack": 0, "player": {"alive": alive}, "game": {}, "entities": {}})
                + "\n"
            )
    with pytest.raises(playtest.PlaytestError, match="assertion"):
        driver.wait([{"scope": "player", "field": "alive", "value": True}], 0.2, assert_once=True)


def test_duration_hold_requires_press_acknowledgement(tmp_path, adapter):
    proc = SimpleNamespace(stdin=io.StringIO(), poll=lambda: None)
    driver = playtest.Driver(
        proc,
        tmp_path / "game.log",
        tmp_path,
        "token",
        {},
        "m",
        time.monotonic() + 2,
        "sync {sequence}",
    )
    driver.log.write_text(
        "NRC_PLAYTEST token "
        + json.dumps({"ack": 0, "player": {"alive": True}, "game": {}, "entities": {}})
        + "\n"
    )
    with pytest.raises(playtest.PlaytestError, match="timed out"):
        playtest._step(
            driver,
            {"action": "hold", "controls": ["forward"], "seconds": 0.01, "timeout": 0.1},
            adapter,
            tmp_path,
            0,
        )
    assert "+forward" in proc.stdin.getvalue() and "-forward" in proc.stdin.getvalue()


def test_process_exit_race_during_group_cleanup_is_harmless(monkeypatch):
    waits = iter([subprocess.TimeoutExpired("fake", 5), 0])

    def wait(timeout):
        result = next(waits)
        if isinstance(result, Exception):
            raise result
        return result

    proc = SimpleNamespace(pid=999999, wait=wait)

    def disappeared(*args):
        raise ProcessLookupError("already exited")

    monkeypatch.setattr(playtest.os, "killpg", disappeared)
    playtest._stop(proc, group=True)


@pytest.mark.parametrize("relative", ["../escape", "/tmp/escape", "mod/../../escape"])
def test_profile_paths_cannot_escape_isolated_home(tmp_path, relative):
    with pytest.raises(playtest.PlaytestError, match="escapes"):
        playtest._inside(tmp_path / "home", relative)


def test_duration_hold_observes_release_before_success(fake_install):
    result = playtest.run(
        fake_install,
        "fake",
        {"steps": [{"action": "hold", "controls": ["forward"], "seconds": 0.01, "timeout": 1}]},
        timeout_seconds=3,
    )
    assert result["ok"]
    assert result["steps"][0]["state"]["ack"] == 1
    assert result["final_state"]["ack"] == 2


def test_invalid_profile_path_fails_before_creating_a_run(fake_install):
    profiles.load("fake")["engine"]["playtest"]["observer_target"] = "../outside.lua"
    with pytest.raises(playtest.PlaytestError, match="escapes"):
        playtest.run(
            fake_install,
            "fake",
            {
                "steps": [
                    {
                        "action": "wait",
                        "until": [{"scope": "player", "field": "alive", "value": True}],
                    }
                ]
            },
        )
    assert not (fake_install.parent / "out/playtests").exists()
