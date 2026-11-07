"""A successful check must describe the edits being saved, and save must preserve others' work."""

import json
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from nrc_mcp import project_state
from nrc_mcp import server as srv
from nrc_mcp.surface import compiling


@pytest.fixture
def opened(tmp_path, monkeypatch):
    monkeypatch.setenv("NRC_PROFILE", "etl")
    fixture = Path(__file__).resolve().parents[2] / "bench/tapes/fixtures/nrcroom"
    shutil.copytree(fixture, tmp_path / "project")
    path = tmp_path / "project/maps/nrcroom.map"
    srv.map_open(str(path))
    yield path
    srv.SESSION.map = None
    srv.SESSION.path = None


def test_compile_snapshots_unsaved_geometry_and_script(opened, monkeypatch, tmp_path):
    original = opened.read_bytes()
    srv.entity_set_keys(0, {"message": "unsaved revision"})
    srv.script_set_event("gate", "trigger", ['wm_announce "staged"'], ["test"])
    monkeypatch.setattr(compiling, "repo_root", lambda: tmp_path)
    seen = {}

    def compile_task(name, args, **kwargs):
        target = Path(args[0])
        seen["source"] = target.read_text()
        seen["script"] = target.with_suffix(".script").read_text()
        return {"ok": True, "stdout": "", "task": name}

    monkeypatch.setattr(compiling, "run_mise_task", compile_task)
    result = srv.compile_map()
    assert "unsaved revision" in seen["source"]
    assert 'wm_announce "staged"' in seen["script"]
    assert opened.read_bytes() == original
    assert result["revision"] == srv.SESSION.receipt()
    assert Path(result["build_directory"]).joinpath("revision.json").is_file()


def test_compile_does_not_adopt_a_script_created_externally_after_open(
    opened, monkeypatch, tmp_path
):
    path = tmp_path / "fresh.map"
    srv.map_new(str(path))
    path.with_suffix(".script").write_text("// appeared after map_open\n")
    monkeypatch.setattr(compiling, "repo_root", lambda: tmp_path)

    def compile_task(name, args, **kwargs):
        assert not Path(args[0]).with_suffix(".script").exists()
        return {"ok": True, "stdout": ""}

    monkeypatch.setattr(compiling, "run_mise_task", compile_task)
    assert not srv.compile_map()["revision"]["companions"]


@pytest.mark.parametrize("companion", [False, True])
def test_save_refuses_external_map_or_script_edits(opened, companion):
    script = opened.with_suffix(".script")
    srv.entity_set_keys(0, {"message": "our edit"})
    srv.script_set_event("gate", "trigger", ['wm_announce "ours"'], ["test"])
    external = script if companion else opened
    external.write_bytes(external.read_bytes() + b"\n// human editor\n")
    before = (opened.read_bytes(), script.read_bytes())
    with pytest.raises(ToolError, match="changed on disk"):
        srv.map_save(check_playable_space=False)
    assert (opened.read_bytes(), script.read_bytes()) == before
    assert srv.SESSION.pending_files


def test_save_writes_geometry_and_script_then_updates_revision(opened):
    srv.entity_set_keys(0, {"message": "new"})
    srv.script_set_event("gate", "trigger", ['wm_announce "new"'], ["test"])
    report = srv.map_save(check_playable_space=False)
    assert "new" in opened.read_text()
    assert 'wm_announce "new"' in opened.with_suffix(".script").read_text()
    assert not report["revision"]["dirty"]
    assert not srv.SESSION.pending_files
    srv.entity_set_keys(0, {"message": "again"})
    assert srv.map_save(check_playable_space=False)["written"]


def test_failed_second_replace_rolls_back_map_and_keeps_staged_edits(opened, monkeypatch):
    script = opened.with_suffix(".script")
    before = (opened.read_bytes(), script.read_bytes())
    srv.entity_set_keys(0, {"message": "new"})
    srv.script_set_event("gate", "trigger", ['wm_announce "new"'], ["test"])
    replace = project_state.os.replace

    def fail_script(src, dest):
        if Path(dest) == script and str(src).endswith(".pending"):
            raise OSError("injected script write failure")
        replace(src, dest)

    monkeypatch.setattr(project_state.os, "replace", fail_script)
    with pytest.raises(ToolError, match="save failed"):
        srv.map_save(check_playable_space=False)
    assert (opened.read_bytes(), script.read_bytes()) == before
    assert srv.SESSION.pending_files


def test_checkpoint_restores_geometry_and_staged_script(opened):
    source = srv.SESSION.map.source()
    script = opened.with_suffix(".script").read_bytes()
    srv.map_checkpoint("before")
    srv.entity_set_keys(0, {"message": "discard"})
    srv.script_set_event("gate", "trigger", ['wm_announce "discard"'], ["test"])
    srv.map_restore("before")
    assert srv.SESSION.map.source() == source
    assert srv.SESSION.read_file(opened.with_suffix(".script")) == script
    assert not srv.SESSION.pending_files


def test_failed_objective_geometry_does_not_leave_partial_entities(opened):
    source = srv.SESSION.map.source()
    with pytest.raises(ValueError):
        srv.objective_place(
            "dynamite",
            "broken",
            "Broken",
            "axis",
            objective={"op": "not_an_operator"},
            trigger={"min": [16, 16, 0], "max": [64, 64, 64]},
        )
    assert srv.SESSION.map.source() == source
    assert not srv.SESSION.pending_files


def test_save_copy_keeps_companions_and_becomes_current_path(opened, tmp_path):
    original = opened.read_bytes()
    target = tmp_path / "copy.map"
    srv.entity_set_keys(0, {"message": "copy"})
    srv.map_save(str(target), check_playable_space=False)
    assert srv.SESSION.path == target
    assert target.with_suffix(".script").read_bytes() == opened.with_suffix(".script").read_bytes()
    assert opened.read_bytes() == original
    second = tmp_path / "second.map"
    srv.map_save(str(second), check_playable_space=False)
    assert second.with_suffix(".script").read_bytes() == target.with_suffix(".script").read_bytes()
    assert set(srv.SESSION.baseline_files) == {
        second,
        second.with_suffix(".script"),
        second.with_suffix(".solids.json"),
    }


def test_analysis_observes_unsaved_geometry_and_reuses_exact_revision(opened):
    first = srv.navgrid_stats(cell=32, profile_id="etl")
    srv.solid_commit(
        {"op": "box", "min": [64, 64, 0], "max": [256, 256, 128]}, "obstruction", remember=False
    )
    after = srv.navgrid_stats(cell=32, profile_id="etl")
    assert first["revision"]["sha256"] != after["revision"]["sha256"]
    assert first["counts"]["walkable_cells"] != after["counts"]["walkable_cells"]
    assert srv.navgrid_stats(cell=32, profile_id="etl")["revision"] == after["revision"]


def test_balance_reads_staged_reinforcement_times(opened):
    before = opened.with_suffix(".script").read_bytes()
    srv.script_set_event(
        "game_manager",
        "spawn",
        ["wm_axis_respawntime 17", "wm_allied_respawntime 23", "wm_setwinner 0"],
    )
    result = srv.balance_report(cell=32, team_sizes={"red": 8, "blue": 8})
    stage = result["reinforcement_balance"]["stages"][0]
    assert stage["respawn"]["red"]["seconds"] == 17
    assert stage["respawn"]["blue"]["seconds"] == 23
    assert opened.with_suffix(".script").read_bytes() == before
    assert result["revision"]["staged_files"]


def test_cached_snapshot_entities_do_not_change_with_the_session(opened):
    srv.navgrid_stats(cell=32, profile_id="etl")
    grid = next(iter(srv.SESSION.grids.values()))
    before = grid.game_map.source()
    srv.entity_set_keys(0, {"message": "newer entity revision"})
    assert grid.game_map.source() == before
    assert grid.game_map.source() != srv.SESSION.map.source()


def test_leak_trace_follows_the_snapshot_and_labels_a_stale_build(opened, monkeypatch, tmp_path):
    monkeypatch.setattr(compiling, "repo_root", lambda: tmp_path)

    def compile_task(name, args, **kwargs):
        lin = Path(args[0]).with_suffix(".lin")
        lin.write_text("128 128 64\n128 128 1024\n")
        return {"ok": True, "stdout": json.dumps({"leaked": True, "artifacts": [str(lin)]})}

    monkeypatch.setattr(compiling, "run_mise_task", compile_task)
    result = srv.compile_map()
    leak = srv.leak_trace()
    assert leak["revision"] == result["revision"]
    assert not leak["stale"]
    srv.entity_set_keys(0, {"message": "after compile"})
    assert srv.leak_trace()["stale"]


def test_shader_audit_uses_unsaved_brushes(opened, tmp_path):
    before = srv.structural_audit()["totals"]["brushes"]
    shader = "textures/test/unsaved_reference"
    srv.solid_commit(
        {"op": "box", "min": [64, 64, 0], "max": [80, 80, 64]},
        "new_shader",
        textures={"default": shader},
        remember=False,
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    report = srv.shader_audit(shader_dirs=[str(empty)], basegame_dir=str(empty))
    assert shader in json.dumps(report["findings"])
    assert shader not in opened.read_text()
    assert report["revision"] == srv.SESSION.receipt()
    assert srv.structural_audit()["totals"]["brushes"] == before + 1


def test_staged_compiler_keeps_artifacts_with_the_build_snapshot(tmp_path, monkeypatch):
    from test_playspace import _load_tool

    q3 = _load_tool("q3map2")
    source = tmp_path / "snapshot/maps/m.map"
    source.parent.mkdir(parents=True)
    source.write_text('{\n"classname" "worldspawn"\n}\n')
    monkeypatch.setattr(q3, "find_q3map2", lambda: Path("fake"))
    monkeypatch.setattr(q3, "needs_staging", lambda p: True)
    monkeypatch.setattr(q3, "to_windows_path", lambda p: str(p))
    monkeypatch.setattr(q3, "general_flags", lambda: [])
    monkeypatch.setattr(q3, "map_root_flags", lambda *a: [])
    monkeypatch.setenv("NRC_BUILD_OUTPUT", str(source.parent))
    seen = []

    def stage(exe, general, flags, path, verbose):
        seen.append(Path(path).parent)
        Path(path).with_suffix(".bsp").write_bytes(b"IBSP")
        return q3.StageResult(flags=flags, returncode=0, seconds=0)

    monkeypatch.setattr(q3, "run_stage", stage)
    for _ in range(2):
        report = q3.compile_map(
            source, [["-bsp"]], stage_dir=tmp_path / "windows", verbose=False, keep=False
        )
        assert report["artifacts"] == [str(source.with_suffix(".bsp"))]
        assert source.with_suffix(".bsp").read_bytes() == b"IBSP"
    assert seen[0] != seen[1], "each compile needs a fresh Windows staging directory"


def test_opening_another_map_refuses_to_drop_unsaved_work(opened, tmp_path):
    """map_open and map_new used to discard edits without a word (audit M-R5)."""
    other = tmp_path / "other.map"
    other.write_text('{\n"classname" "worldspawn"\n}\n')
    srv.map_open(str(other))  # nothing unsaved yet: opens
    srv.entity_set_keys(0, {"message": "unsaved"})
    for call in (lambda: srv.map_open(str(opened)), lambda: srv.map_new(str(tmp_path / "n.map"))):
        with pytest.raises(ToolError, match=r"unsaved work \(map edits\)"):
            call()
    assert not (tmp_path / "n.map").exists(), "refused before creating anything"
    srv.map_save(check_playable_space=False)
    srv.map_open(str(opened))
    srv.script_set_event("gate", "trigger", ["wait 1"], ["x"])
    with pytest.raises(ToolError, match="nrcroom.script"):
        srv.map_open(str(other))
    assert srv.map_open(str(other), discard_unsaved=True)["path"] == str(other)


def test_a_new_map_does_not_adopt_a_sidecar_left_by_an_earlier_one(tmp_path):
    """A stale <map>.solids.json would block labels and edits it knows nothing true about."""
    path = tmp_path / "maps/fresh.map"
    path.parent.mkdir(parents=True)
    path.with_suffix(".solids.json").write_text(
        json.dumps({"version": 1, "solids": {"room": {"ir": {"op": "box"}, "brushes": 6}}})
    )
    r = srv.map_new(str(path))
    assert any("earlier one" in w for w in r["warnings"])
    box = {"op": "box", "min": [0, 0, 0], "max": [64, 64, 64]}
    srv.solid_commit(box, "room")  # the stale label does not block a new solid
    srv.map_save(check_playable_space=False)
    saved = json.loads(path.with_suffix(".solids.json").read_text())
    assert set(saved["solids"]) == {"room"} and saved["solids"]["room"]["ir"] == box


def test_an_edit_to_a_map_that_did_not_round_trip_still_gets_the_playable_space_check(
    opened, monkeypatch
):
    """allow_non_identical used to switch the playable-space gate off as well (audit M-R3)."""
    from nrc_mcp import playspace

    srv.SESSION.opened_round_trip = {"identical": False, "first_difference": "line 1"}
    srv.entity_set_keys(0, {"message": "edit"})
    seen = []

    def diff(baseline, after):
        seen.append(baseline)
        raise playspace.PlayspaceError("probe")

    monkeypatch.setattr(playspace, "diff", diff)
    r = srv.map_save(allow_non_identical=True)
    assert seen == [opened] and "probe" in r["playable_space_note"]


def test_tool_calls_queue_instead_of_sharing_the_session_at_once():
    """The SDK runs sync tools on worker threads, concurrently; ours take turns."""
    import threading
    import time

    import anyio
    from mcp.server.mcpserver import MCPServer
    from nrc_mcp.runtime import _CompactServer

    def overlaps(server_class) -> bool:
        server = server_class("probe")
        state = {"in": 0, "most": 0, "threads": set()}
        lock = threading.Lock()

        @server.tool()
        def slow() -> str:
            with lock:
                state["in"] += 1
                state["most"] = max(state["most"], state["in"])
                state["threads"].add(threading.get_ident())
            time.sleep(0.2)
            with lock:
                state["in"] -= 1
            return "ok"

        async def main():
            async with anyio.create_task_group() as tg:
                for _ in range(3):
                    tg.start_soon(server.call_tool, "slow", {})

        anyio.run(main)
        assert threading.get_ident() not in state["threads"], "sync tools run off the loop"
        return state["most"] > 1

    assert overlaps(MCPServer), "the SDK itself runs these at once; if not, drop the lock"
    assert not overlaps(_CompactServer)
