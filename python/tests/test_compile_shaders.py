"""Compile-only shaders: editor shaders real maps use whose definitions ship with the mapper's
editor setup, not the game. The profile owns them (`compile_shaders`), the compile wrapper hands
them to q3map2 at its lowest priority, and the audits count them as supplied, never shipped."""

from __future__ import annotations

import os
import stat
import struct
import zipfile
from pathlib import Path

import pytest
from nrc_mcp import optimize, pack, profiles

ROOT = Path(__file__).resolve().parents[2]
ALPHA = "textures/editor/alpha_000"


def _load_tool(name: str):
    import importlib.util
    import sys

    path = ROOT / "tools" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _box(lo, hi, shader="common/caulk") -> str:
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    planes = [
        ((x0, y0, z1), (x0, y0 + 1, z1), (x0 + 1, y0, z1)),  # top, normal +z
        ((x0, y0, z0), (x0 + 1, y0, z0), (x0, y0 + 1, z0)),  # bottom
        ((x0, y0, z0), (x0, y0, z0 + 1), (x0 + 1, y0, z0)),  # y min
        ((x0, y1, z0), (x0 + 1, y1, z0), (x0, y1, z0 + 1)),  # y max
        ((x0, y0, z0), (x0, y0 + 1, z0), (x0, y0, z0 + 1)),  # x min
        ((x1, y0, z0), (x1, y0, z0 + 1), (x1, y0 + 1, z0)),  # x max
    ]
    lines = [
        " ".join(f"( {p[0]} {p[1]} {p[2]} )" for p in pts) + f" {shader} 0 0 0 0.5 0.5 0 0 0"
        for pts in planes
    ]
    return "{\n" + "\n".join(lines) + "\n}\n"


def _room(inner: str) -> str:
    walls = [
        ((-16, -16, -16), (528, 528, 0)),
        ((-16, -16, 256), (528, 528, 272)),
        ((-16, -16, 0), (0, 528, 256)),
        ((512, -16, 0), (528, 528, 256)),
        ((0, -16, 0), (512, 0, 256)),
        ((0, 512, 0), (512, 528, 256)),
    ]
    world = "".join(_box(lo, hi) for lo, hi in walls) + inner
    return (
        '{\n"classname" "worldspawn"\n'
        + world
        + '}\n{\n"classname" "info_player_deathmatch"\n"origin" "64 64 32"\n}\n'
    )


def _bsp_shader(bsp: Path, name: str) -> tuple[int, int, int]:
    """(contents, brushes using it, draw surfaces drawing it) for one shader in an IBSP."""
    b = bsp.read_bytes()

    def lump(i):
        off, ln = struct.unpack("<ii", b[8 + 8 * i : 16 + 8 * i])
        return b[off : off + ln]

    sh = lump(1)
    names = [sh[i : i + 64].split(b"\0")[0].decode() for i in range(0, len(sh), 72)]
    idx = names.index(name)
    contents = struct.unpack("<i", sh[idx * 72 + 68 : idx * 72 + 72])[0]
    br = lump(8)
    brushes = sum(
        1 for i in range(0, len(br), 12) if struct.unpack("<i", br[i + 8 : i + 12])[0] == idx
    )
    ds = lump(13)
    draws = sum(1 for i in range(0, len(ds), 104) if struct.unpack("<i", ds[i : i + 4])[0] == idx)
    return contents, brushes, draws


def test_the_etl_profile_owns_its_compile_only_shaders():
    found = optimize.compile_only_shaders("etl")
    assert {
        "textures/editor/alpha_000",
        "textures/editor/alpha_100",
        "textures/common/hintskip",
    } <= set(found)
    for entry in found.values():
        assert entry["confidence"] in ("verified", "unverified") and entry["role"]
    section = profiles.load("etl")["compile_shaders"]
    for s in section["shaders"]:
        assert s.get("source"), f"{s['name']} says where its definition comes from"
    assert optimize.compile_only_shaders(None) == {}
    assert optimize.compile_only_shaders("urt43") == {}, "a profile without the section has none"


def test_alpha_volumes_open_with_volume_first():
    """map.cpp:833 recognises a volume brush only when `q3map_alphaMod volume` is the shader's
    first colorMod, and nonsolid is what keeps players from walking into a wall."""
    text = (ROOT / "profiles" / "etl-compile.shader").read_text()
    for sh in optimize.parse_shader_file(ROOT / "profiles" / "etl-compile.shader"):
        if not sh["name"].startswith("textures/editor/alpha_"):
            continue
        body = text[text.index(sh["name"]) :].split("}", 1)[0]
        mods = [
            ln.strip()
            for ln in body.splitlines()
            if ln.strip().lower().startswith("q3map_alphamod")
        ]
        assert mods[0].lower() == "q3map_alphamod volume", sh["name"]
        assert "surfaceparm nonsolid" in body and "surfaceparm nodraw" in body


def test_the_wrapper_hands_them_to_compile_stages_only(tmp_path, monkeypatch):
    q3 = _load_tool("q3map2")
    seen = tmp_path / "seen"
    seen.mkdir()
    fake = tmp_path / "q3map2"
    # Records each call's flags and copies whatever compile-shader archive it was given.
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{seen}/calls"\n'
        'prev=""; stage=bsp; case "$*" in *-pk3*) stage=pk3;; esac\n'
        "for a; do\n"
        '  if [ "$prev" = "-fs_pakpath" ] && [ -f "$a/nrc_compile_shaders.pk3" ]; then\n'
        f'    cp "$a/nrc_compile_shaders.pk3" "{seen}/$stage.pk3"; echo "$a" >> "{seen}/dirs"\n'
        "  fi\n"
        '  prev="$a"; m="$a"\n'
        "done\n"
        'printf "IBSP" > "${m%.map}.bsp"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(q3, "find_q3map2", lambda: fake)
    monkeypatch.setattr(q3, "general_flags", lambda: [])
    monkeypatch.setenv("NRC_PROFILE", "etl")
    monkeypatch.setenv("NRC_ROOT", str(ROOT))
    m = tmp_path / "maps" / "m.map"
    m.parent.mkdir()
    m.write_text('{\n"classname" "worldspawn"\n}\n')
    r = q3.compile_map(m, [["-bsp", "-meta"], ["-pk3"]], stage_dir=None, verbose=False, keep=False)
    assert r["ok"]
    calls = (seen / "calls").read_text().splitlines()
    assert len(calls) == 2
    assert (seen / "bsp.pk3").is_file() and not (seen / "pk3.pk3").exists(), (
        "the packer never sees them, so they can never ship"
    )
    with zipfile.ZipFile(seen / "bsp.pk3") as z:
        assert z.namelist() == ["scripts/nrc_compile_etl.shader"]
        assert ALPHA in z.read("scripts/nrc_compile_etl.shader").decode()
    # Last on the search path, after the map's own folder: the lowest priority.
    args = calls[0].split()
    pak = [args[i + 1] for i, a in enumerate(args) if a == "-fs_pakpath"]
    assert pak[-1] == (seen / "dirs").read_text().strip() and pak[0] == str(tmp_path)
    assert not Path(pak[-1]).exists(), "the throwaway folder is removed"

    monkeypatch.setenv("NRC_PROFILE", "urt43")
    (seen / "bsp.pk3").unlink()
    q3.compile_map(m, [["-bsp", "-meta"]], stage_dir=None, verbose=False, keep=False)
    assert not (seen / "bsp.pk3").exists(), "a profile without compile shaders adds nothing"


def _q3map2() -> Path:
    exe = Path(os.environ.get("Q3MAP2") or "")
    if not exe.is_file():
        pytest.skip("Q3MAP2 is unset or not built; a real compile is needed")
    return exe


def test_q3map2_builds_alpha_volumes_as_nonsolid_and_undrawn(tmp_path, monkeypatch):
    """The compile itself: with the profile's definition the volume brush is nonsolid and draws
    nothing; a definition the map ships beside itself still wins over the profile's."""
    _q3map2()
    q3 = _load_tool("q3map2")
    monkeypatch.setenv("NRC_PROFILE", "etl")
    monkeypatch.setenv("NRC_ROOT", str(ROOT))
    monkeypatch.delenv("NRC_FS_BASEPATH", raising=False)
    monkeypatch.delenv("NRC_BUILD_OUTPUT", raising=False)
    volume = _box((128, 128, 0), (256, 256, 64), "editor/alpha_000")
    m = tmp_path / "plain" / "maps" / "m.map"
    m.parent.mkdir(parents=True)
    m.write_text(_room(volume))
    r = q3.compile_map(m, [["-bsp", "-meta"]], stage_dir=None, verbose=False, keep=False)
    assert r["ok"] and not r["leaked"], r["stages"][0]["tail"]
    contents, brushes, draws = _bsp_shader(m.with_suffix(".bsp"), ALPHA)
    assert brushes == 1 and draws == 0
    assert not contents & 1, "CONTENTS_SOLID is cleared: players walk through the volume"

    # The map's own definition comes first (loose scripts/ before any archive).
    own = tmp_path / "own"
    (own / "maps").mkdir(parents=True)
    (own / "scripts").mkdir()
    (own / "scripts" / "m.shader").write_text(f"{ALPHA}\n{{\n\tsurfaceparm nodraw\n}}\n")
    m2 = own / "maps" / "m.map"
    m2.write_text(_room(volume))
    r = q3.compile_map(m2, [["-bsp", "-meta"]], stage_dir=None, verbose=False, keep=False)
    assert r["ok"], r["stages"][0]["tail"]
    contents, _, draws = _bsp_shader(m2.with_suffix(".bsp"), ALPHA)
    assert contents & 1 and draws == 0, "the map's solid nodraw definition won"


def _alpha_map(tmp_path: Path) -> Path:
    m = tmp_path / "maps" / "m.map"
    m.parent.mkdir(parents=True)
    m.write_text(_room(_box((128, 128, 0), (256, 256, 64), "editor/alpha_000")))
    return m


def test_shader_audit_counts_them_as_supplied(tmp_path):
    m = _alpha_map(tmp_path)
    etl = optimize.shader_audit(m, [], "etl")
    assert ALPHA in etl["compile_only"] and ALPHA not in etl["missing"]
    codes = {f["code"] for f in etl["findings"]}
    assert "SHADER_COMPILE_ONLY" in codes and "SHADER_DRAWS_NOTHING" not in codes
    other = optimize.shader_audit(m, [], "urt43")
    assert ALPHA in other["missing"], "only the profile that owns them supplies them"


def test_the_packer_lets_them_be_missing_and_never_ships_them(tmp_path):
    m = _alpha_map(tmp_path)
    bsp = m.with_suffix(".bsp")
    compile_only = optimize.compile_only_shaders("etl")
    out = pack._account_for([ALPHA], bsp, {}, None, [], compile_only=compile_only)
    assert out["missing"] == [] and out["add"] == []
    assert out["allowed"][0]["name"] == ALPHA and "compile-only" in out["allowed"][0]["reason"]
    assert pack._account_for([ALPHA], bsp, {}, None, [])["missing"] == [ALPHA]


def test_repack_analyze_does_not_call_them_unresolved(tmp_path, monkeypatch):
    m = _alpha_map(tmp_path)
    monkeypatch.setattr(
        optimize,
        "basegame_index",
        lambda *_a, **_k: {"archives": ["pak0.pk3"], "shaders": {}, "images": set()},
    )
    monkeypatch.setenv("NRC_FS_BASEPATH", str(tmp_path))
    names = [ALPHA, "textures/invented/nothing"]
    assert pack._unresolved_shaders(m.with_suffix(".bsp"), names, "etl") == [
        "textures/invented/nothing"
    ]


def test_routes_walk_through_alpha_volumes(tmp_path, monkeypatch):
    """The collision model reads the shaders the compiler sees, compile-only ones included."""
    from nrc_mcp import analysis

    monkeypatch.delenv("NRC_FS_BASEPATH", raising=False)
    m = _alpha_map(tmp_path)
    contents = analysis.shader_contents("etl", m)
    assert contents.blocks_player("editor/alpha_000") is False
    assert contents.blocks_player("common/hintskip") is False
    assert analysis.shader_contents("urt43", m).blocks_player("editor/alpha_000") is None
