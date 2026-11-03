"""The engine smoke test, without an engine: a fake server that writes the log a real one would.

The log lines are the ones ET: Legacy v2.86.2 wrote when the same maps were loaded for real.
"""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest
from nrc_mcp import engine, profiles

REAL_LOG_GOOD = """\
       0 Loaded entities from maps/m.bsp
       0 ^1ERROR: Can't find hunkusage.dat
       0 ^1ERROR No tracemap found for map
       0 Setting Axis autospawn to Axis Bunker
"""
REAL_LOG_CRASH = """\
       0 Loaded entities from maps/m.bsp
       0 Going from CS_PRIMED to CS_ACTIVE for ETLegacyPlayer
ERROR: Couldn't find a spawn point
       0 ----- Server Shutdown (Server crashed: Couldn't find a spawn point
"""

SPEC = {
    "server": ["fakeded"],
    "client": ["fakeclient"],
    "server_args": ["{home}", "{map}", "server"],
    "client_args": ["{home}", "{map}", "client"],
    "log": "mod/console.log",
    "loaded": "Loaded entities from maps/{map}.bsp",
    "fatal": ["^ERROR: ", "Server crashed"],
    "noted": ["\\^1ERROR No "],
}


def test_the_log_reader_tells_a_stop_from_a_complaint():
    good = engine.read_log(REAL_LOG_GOOD, SPEC, "m")
    assert good == {"loaded": True, "fatal": [], "noted": ["0 ^1ERROR No tracemap found for map"]}
    crash = engine.read_log(REAL_LOG_CRASH, SPEC, "m")
    assert crash["loaded"] and len(crash["fatal"]) == 2
    assert not engine.read_log(REAL_LOG_GOOD, SPEC, "other")["loaded"]


def pk3(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for n in names:
            z.writestr(n, b"x")
    return path


def test_the_map_is_read_from_the_package(tmp_path):
    assert engine.map_in(pk3(tmp_path / "a.pk3", ["maps/m.bsp", "maps/m.script"])) == "m"
    with pytest.raises(engine.EngineError, match="holds 2 maps"):
        engine.map_in(pk3(tmp_path / "b.pk3", ["maps/a.bsp", "maps/b.bsp"]))


@pytest.fixture
def fake_engine(tmp_path, monkeypatch):
    """A 'server' that writes the log it is told to, then idles like a running game."""
    install = tmp_path / "install"
    install.mkdir()
    script = install / "fakeded"
    script.write_text(
        "#!/bin/sh\n"
        'mkdir -p "$1/mod"\n'
        'cat "$1/../log_to_write_$3" > "$1/mod/console.log" 2>/dev/null\n'
        "exec sleep 30\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("NRC_FS_BASEPATH", str(install))
    monkeypatch.setattr(
        profiles, "load", lambda _pid: {"engine": SPEC, "packaging": {"basegame": "base"}}
    )
    monkeypatch.setattr(engine.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


def test_a_clean_load_passes_and_a_crash_fails(fake_engine):
    package = pk3(fake_engine / "m_autopacked.pk3", ["maps/m.bsp"])
    (fake_engine / "log_to_write_server").write_text(REAL_LOG_GOOD)
    r = engine.smoke(package, "test", seconds=1, keep=True)
    assert r["ok"] and r["runs"][0]["exit"] is None, "still running when stopped is the good case"
    assert (fake_engine / "home" / "base" / "m_autopacked.pk3").is_file()

    (fake_engine / "log_to_write_server").write_text(REAL_LOG_CRASH)
    r = engine.smoke(package, "test", seconds=1, keep=True)
    assert not r["ok"]
    assert r["runs"][0]["fatal"][0] == "ERROR: Couldn't find a spawn point"


def test_the_smoke_test_says_why_it_cannot_run(fake_engine, monkeypatch):
    package = pk3(fake_engine / "m_autopacked.pk3", ["maps/m.bsp"])
    monkeypatch.delenv("NRC_FS_BASEPATH")
    with pytest.raises(engine.EngineError, match="NRC_FS_BASEPATH"):
        engine.smoke(package, "test", seconds=1)
    monkeypatch.setenv("NRC_FS_BASEPATH", str(fake_engine / "install"))
    monkeypatch.setattr(profiles, "load", lambda _pid: {})
    with pytest.raises(engine.EngineError, match="describes no engine"):
        engine.smoke(package, "test", seconds=1)


def test_a_missing_binary_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(engine.EngineError, match="none of nothere found"):
        engine.find_binary(["nothere"], str(tmp_path))


def test_nonzero_exit_after_load_is_not_a_pass(fake_engine, monkeypatch):
    package = pk3(fake_engine / "m_autopacked.pk3", ["maps/m.bsp"])

    def crash(cmd, cwd, seconds, env):
        (cwd / "mod").mkdir(exist_ok=True)
        (cwd / "mod/console.log").write_text(REAL_LOG_GOOD)
        return -11

    monkeypatch.setattr(engine, "_run", crash)
    result = engine.smoke(package, "test", keep=True)
    assert result["runs"][0]["loaded"]
    assert not result["ok"]
    assert not result["runs"][0]["exit_ok"]


@pytest.mark.parametrize("connected", [False, True])
def test_client_requires_an_active_connection_marker(fake_engine, monkeypatch, connected):
    package = pk3(fake_engine / "m_autopacked.pk3", ["maps/m.bsp"])
    marker = "Going from CS_PRIMED to CS_ACTIVE"
    monkeypatch.setitem(SPEC, "client_connected", marker)
    monkeypatch.setattr(engine, "find_binary", lambda *a: fake_engine / "install/fakeded")
    monkeypatch.setattr(engine, "_start_display", lambda: (None, {}))

    def run(cmd, cwd, seconds, env):
        (cwd / "mod").mkdir(exist_ok=True)
        (cwd / "mod/console.log").write_text(REAL_LOG_GOOD + (marker if connected else ""))
        return None

    monkeypatch.setattr(engine, "_run", run)
    result = engine.smoke(package, "test", client=True, keep=True)
    assert result["ok"] is connected
    assert result["runs"][1]["connected"] is connected
