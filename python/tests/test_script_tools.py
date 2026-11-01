"""The map-script tools, end to end: the real kernel, the shipped ET: Legacy profile, and the
nrcroom fixture, which has been loaded and played in etlded."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import kernel, profiles
from nrc_mcp import server as srv

FIXTURE = Path(__file__).resolve().parents[2] / "bench" / "tapes" / "fixtures" / "nrcroom"


@pytest.fixture(autouse=True)
def _etl(monkeypatch):
    try:
        kernel.kernel()
    except kernel.KernelUnavailable as e:
        pytest.skip(str(e))
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    monkeypatch.setenv("NRC_PROFILE", "etl")
    yield
    srv.SESSION.map = None
    srv.SESSION.path = None


@pytest.fixture
def nrcroom(tmp_path) -> Path:
    shutil.copytree(FIXTURE, tmp_path, dirs_exist_ok=True)
    srv.map_open(str(tmp_path / "maps" / "nrcroom.map"))
    return tmp_path / "maps" / "nrcroom.script"


def test_script_show_says_which_entities_run_each_block(nrcroom):
    r = srv.script_show()
    blocks = {b["block"]: b for b in r["blocks"]}
    assert set(blocks) == {"game_manager", "gate"}
    assert blocks["game_manager"]["runs_for_entities"], "script_multiplayer runs it implicitly"
    assert blocks["gate"]["runs_for_entities"], "the func_explosive's scriptname names it"
    spawn = blocks["game_manager"]["events"][0]
    assert spawn["event"] == "spawn" and "setautospawn Axis Bunker 0" in spawn["actions"]
    assert r["summary"]["error"] == 0


def test_set_event_writes_at_once_and_reports_what_does_not_resolve_yet(nrcroom):
    before = nrcroom.read_text()
    r = srv.script_set_event(
        "gate", "trigger", ['wm_announce "Gate!"', "alertentity lever"], params=["open"]
    )
    assert r["did"] == "added event"
    text = nrcroom.read_text()
    assert text.startswith(before[: before.index("gate\n{")]), "everything before is untouched"
    assert "\ttrigger open\n" in text
    codes = {f["code"]: f["severity"] for f in r["findings"]}
    assert codes.get("MAPSCRIPT_UNRESOLVED_REFERENCE") == "error", "no entity is named lever"
    assert srv.validate_profile()["summary"]["error"] == 1, "validate_profile sees the same"


def test_an_unknown_action_is_refused_and_nothing_is_written(nrcroom):
    before = nrcroom.read_text()
    with pytest.raises(ToolError, match="refused, nothing written: unknown action 'teleport'"):
        srv.script_set_event("gate", "spawn", ["teleport 0 0 0"])
    assert nrcroom.read_text() == before


def test_remove_takes_out_an_event_or_a_block(nrcroom):
    srv.script_remove("gate", "death")
    assert "death" not in nrcroom.read_text()
    r = srv.script_remove("gate")
    assert [b["block"] for b in r["blocks"]] == ["game_manager"]
    with pytest.raises(ToolError, match="no block named 'gate'"):
        srv.script_remove("gate")


def test_a_map_built_from_nothing_gets_its_script_through_the_tools(tmp_path):
    srv.map_new(str(tmp_path / "maps" / "fresh.map"))
    srv.entity_place("script_multiplayer", [0, 0, 64])
    assert srv.script_show()["exists"] is False
    r = srv.script_set_event("game_manager", "spawn", ["wm_axis_respawntime 10", "wait 50"])
    assert r["did"] == "created block"
    assert (tmp_path / "maps" / "fresh.script").read_text().startswith("game_manager\n{\n\tspawn")
    assert r["summary"] == {"error": 0, "warning": 0, "info": 0}


def test_a_profile_without_map_scripts_says_so(nrcroom, monkeypatch):
    if "urt43" not in profiles.available():
        pytest.skip("no urt43 profile on disk")
    monkeypatch.setenv("NRC_PROFILE", "urt43")
    with pytest.raises(ToolError, match="describes no map script"):
        srv.script_show()
