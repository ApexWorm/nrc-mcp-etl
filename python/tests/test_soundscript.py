"""Speaker scripts, sound scripts, and the names a map points into them with.

The grammar and every keyword come from the ET: Legacy profile's `sound:` section; the parsers
are checked against the engine's own behaviour (game/bg_sscript.c, cgame/cg_sound.c) on
synthetic files, and against the six stock maps' files when the game is installed.
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest
from nrc_mcp import profiles, soundscript


@pytest.fixture
def sp():
    if "etl" not in profiles.available():
        pytest.skip("no ET: Legacy profile on disk")
    return soundscript.spec("etl")


# ---------------------------------------------------------------------------
# Speaker scripts
# ---------------------------------------------------------------------------


def test_a_speaker_script_parses_the_way_the_engine_reads_it(sp):
    text = """speakerScript
{
    // a comment
    speakerDef {
        noise "sound/world/water_03.wav"
        origin -449.51 198.68 136.19
        targetname "pump"
        looped "on"
        broadcast "no"
        volume 127
        range 800
    }
    speakerDef { noise "sound/world/x.wav" wait 5 random 2 }
}
"""
    speakers, err = soundscript.parse_speaker_script(text, sp["speaker_script"])
    assert err is None
    assert speakers[0]["origin"] == [-449.51, 198.68, 136.19], "a minus is its own token"
    assert speakers[0]["targetname"] == "pump" and speakers[0]["looped"] == "on"
    assert speakers[1]["wait"] == 5


@pytest.mark.parametrize(
    ("text", "kept", "message"),
    [
        ("soundScript { }", 0, "expected 'speakerScript'"),
        ('speakerScript { speakerDef { noise "a.wav" } speakerDef { pitch 3 } }', 1, "pitch"),
        ("speakerScript { speakerDef { looped maybe } }", 0, "unknown looped value"),
        ("speakerScript { speakerDef { volume 70000 } }", 0, "volume value 70000"),
        ("speakerScript { speakerDef { range -1 } }", 0, "range value -1"),
        ("speakerScript { speakerDef { origin 1 2 } }", 0, "expected origin vector"),
    ],
)
def test_a_speaker_script_stops_at_its_first_mistake_and_keeps_what_came_before(
    sp, text, kept, message
):
    speakers, err = soundscript.parse_speaker_script(text, sp["speaker_script"])
    assert err is not None and message in err[1]
    assert len(speakers) == kept


# ---------------------------------------------------------------------------
# Sound scripts
# ---------------------------------------------------------------------------


def test_sound_scripts_parse_into_names_and_files(sp):
    text = (
        "// news\nnews_x\n{\n\tsound sound/vo/x/news.wav\n\tvoice\n\tstreaming\n}\n\n"
        "two { sound a.wav sound b.wav }\n"
    )
    scripts, err = soundscript.parse_sound_scripts(text, sp["sound_scripts"])
    assert err is None
    assert [s["name"] for s in scripts] == ["news_x", "two"]
    assert [f for f, _ in scripts[1]["sounds"]] == ["a.wav", "b.wav"]


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("name { sound a.wav", "no concluding '}'"),
        ("{ sound a.wav }", "'{' found but not expected"),
        ("name { sound a.wav } }", "'}' unexpected"),
        ("one two { }", "'two' unexpected"),
        ("name { sound", "empty sound file name"),
        ("n { " + "sound a.wav " * 17 + "}", "more than 16 sounds"),
    ],
)
def test_a_sound_script_mistake_is_found(sp, text, message):
    _, err = soundscript.parse_sound_scripts(text, sp["sound_scripts"])
    assert err is not None and message in err[1]


# ---------------------------------------------------------------------------
# Checking a map
# ---------------------------------------------------------------------------


@pytest.fixture
def game(tmp_path, monkeypatch, sp):
    """A base game with one shared sound script and one sound, and a map folder beside it."""
    base = tmp_path / "install"
    (base / "etmain").mkdir(parents=True)
    with zipfile.ZipFile(base / "etmain" / "pak0.pk3", "w") as z:
        z.writestr("sound/scripts/filelist.txt", "shared.sounds\n")
        z.writestr("sound/scripts/shared.sounds", "shared_name { sound sound/base/ok.wav }\n")
        z.writestr("sound/scripts/other.sounds", "other_name { sound sound/base/ok.wav }\n")
        z.writestr("sound/base/ok.wav", b"RIFF")
    monkeypatch.setenv("NRC_FS_BASEPATH", str(base))
    work = tmp_path / "work"
    (work / "maps").mkdir(parents=True)
    (work / "sound" / "maps").mkdir(parents=True)
    (work / "sound" / "scripts").mkdir(parents=True)
    (work / "maps" / "m.map").write_text("")
    return work


def _check(work: Path, script: str, entities=()):
    (work / "maps" / "m.script").write_text(script)
    return soundscript.check("etl", list(entities), work / "maps" / "m.map")


def codes(found) -> dict[str, str]:
    return {f.code: f.severity for f in found}


def test_names_resolve_against_the_shared_list_and_the_maps_own_file(game):
    (game / "sound" / "scripts" / "m.sounds").write_text("mine { sound sound/base/ok.wav }\n")
    (game / "sound" / "maps" / "m.sps").write_text(
        'speakerScript { speakerDef { noise "sound/base/ok.wav" targetname "Hum" } }'
    )
    script = (
        "game_manager\n{\n\tspawn\n\t{\n"
        "\t\twm_teamvoiceannounce 0 shared_name\n"
        "\t\tplaysound mine\n"
        "\t\tplaysound sound/base/ok.wav\n"
        "\t\tenablespeaker hum\n"
        "\t}\n}\n"
    )
    assert _check(game, script) == []


def test_another_maps_sound_script_does_not_resolve(game):
    """Only filelist.txt's files and the map's own are loaded (cgame/cg_sound.c:396-468)."""
    found = _check(game, "game_manager\n{\n\tspawn\n\t{\n\t\tplaysound other_name\n\t}\n}\n")
    (f,) = found
    assert f.code == "SOUND_SCRIPT_UNRESOLVED" and f.severity == "warning"
    assert "m.script playsound (line 5)" in f.message


def test_a_speaker_named_by_the_script_must_be_in_the_speaker_script(game):
    script = "game_manager\n{\n\tspawn\n\t{\n\t\tdisablespeaker pump\n\t}\n}\n"
    (f,) = _check(game, script)
    assert f.code == "SPEAKER_UNRESOLVED" and "no speaker script" in f.message
    (game / "sound" / "maps" / "m.sps").write_text(
        'speakerScript { speakerDef { noise "sound/base/ok.wav" targetname "fan" } }'
    )
    (f,) = _check(game, script)
    assert f.code == "SPEAKER_UNRESOLVED" and "no speaker in sound/maps/m.sps" in f.message


def test_missing_sound_files_and_broken_files_are_reported(game):
    (game / "sound" / "scripts" / "m.sounds").write_text("mine { sound sound/none.wav")
    (game / "sound" / "maps" / "m.sps").write_text(
        'speakerScript { speakerDef { noise "sound/gone.wav" } speakerDef { bogus 1 } }'
    )
    speaker = {
        "index": 3,
        "classname": "target_speaker",
        "keys": [["classname", "target_speaker"], ["noise", "sound/alsogone.wav"]],
    }
    found = _check(game, "", [speaker])
    got = codes(found)
    assert got["SOUND_SCRIPT_SYNTAX"] == "error", "the client stops on it"
    assert got["SPEAKER_SCRIPT_SYNTAX"] == "warning", "the game only stops reading the file"
    missing = [f for f in found if f.code == "SOUND_FILE_MISSING"]
    assert {"sound/gone.wav" in f.message for f in missing} == {True, False}
    assert any(f.entities == [3] and "sound/alsogone.wav" in f.message for f in missing)


def test_a_file_beside_the_map_counts(game):
    (game / "sound" / "mine").mkdir()
    (game / "sound" / "mine" / "a.wav").write_bytes(b"RIFF")
    (game / "sound" / "maps" / "m.sps").write_text(
        'speakerScript { speakerDef { noise "sound/mine/a.wav" } }'
    )
    assert _check(game, "") == []
    assert soundscript.sound_files("etl", game / "maps" / "m.bsp") == {
        "sound/mine/a.wav": "sound/maps/m.sps"
    }


def test_a_gap_the_base_game_itself_has_is_info(game):
    (game / "sound" / "maps" / "m.sps").write_text(
        'speakerScript { speakerDef { noise "sound/world/wolf.wav" } }'
    )
    (f,) = _check(game, "")
    assert f.code == "SOUND_FILE_MISSING" and f.severity == "info"
    assert "known gap" in f.message


# ---------------------------------------------------------------------------
# The stock maps' own files, when the game is installed
# ---------------------------------------------------------------------------


def test_every_stock_speaker_and_sound_script_parses(sp):
    base = os.environ.get("NRC_FS_BASEPATH")
    if not base or not (Path(base) / "etmain").is_dir():
        pytest.skip("no ET: Legacy install (NRC_FS_BASEPATH)")
    seen = 0
    for archive in sorted((Path(base) / "etmain").glob("*.pk3")):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                text = None
                if name.endswith(".sps"):
                    text = z.read(name).decode("latin-1")
                    speakers, err = soundscript.parse_speaker_script(text, sp["speaker_script"])
                    assert err is None, (name, err)
                    assert speakers, name
                elif name.endswith(".sounds"):
                    text = z.read(name).decode("latin-1")
                    scripts, err = soundscript.parse_sound_scripts(text, sp["sound_scripts"])
                    assert err is None, (name, err)
                    assert scripts, name
                seen += text is not None
    assert seen >= 12, "six maps ship a speaker script and a sound script each"
