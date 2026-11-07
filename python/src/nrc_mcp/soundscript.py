"""Sound scripts and speaker scripts: the text files that give a map its ambient sound.

A profile that has them describes them under `sound:`. Two kinds, read by different parsers in
the engine, and both are checked here:

- the **speaker script** (one per map): a root keyword, then one braced entry per speaker
  with keys such as the sound file, origin and a name. Read with the bot library's tokenizer,
  where `{`, `}` and `-` are punctuation and a quoted string is one token. A mistake stops the
  file being read: the speakers before it stay, the rest are lost.
- **sound scripts** (a list of shared files plus one per map): `name { sound <file> ... }`
  blocks. Read with the whitespace tokenizer the map script uses. A mistake stops the client.

And what names them: an entity key, a map script action that plays a sound or a sound script,
and the map script actions that switch a speaker by name. Every keyword, key, path and limit
comes from the profile; this module knows only the shapes.
"""

from __future__ import annotations

import os
import zipfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import mapscript, profiles
from .rules import Finding


def spec(profile_id: str) -> dict | None:
    s = profiles.load(profile_id).get("sound")
    return s if isinstance(s, dict) else None


def map_root(path: str | Path) -> Path:
    """The folder that holds `maps/`, which game paths are relative to (the map's own folder
    for a map kept outside one)."""
    p = Path(path)
    return p.parent.parent if p.parent.name.lower() == "maps" else p.parent


# ---------------------------------------------------------------------------
# Tokenizing and parsing
# ---------------------------------------------------------------------------


class _Tok:
    __slots__ = ("kind", "line", "text")

    def __init__(self, text: str, kind: str, line: int):
        self.text, self.kind, self.line = text, kind, line


def _pc_tokens(text: str) -> list[_Tok]:
    """The bot library's tokens: names, numbers, quoted strings (quotes stripped, as
    `PC_ReadTokenHandle` does) and single-character punctuation. `//` and `/* */` are
    comments."""
    out: list[_Tok] = []
    i, n, line = 0, len(text), 1
    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += text.count("\n", i, end)
            i = end
        elif c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(_Tok(text[i + 1 : j], "string", line))
            line += text.count("\n", i, j)
            i = j + 1
        elif c.isdigit() or (c == "." and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isalnum() or text[j] == "."):
                j += 1
            out.append(_Tok(text[i:j], "number", line))
            i = j
        elif c.isalpha() or c == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            out.append(_Tok(text[i:j], "name", line))
            i = j
        else:
            out.append(_Tok(c, "punct", line))
            i += 1
    return out


def parse_speaker_script(text: str, sp: dict) -> tuple[list[dict], tuple[int, str] | None]:
    """The speakers a speaker script defines, and the first mistake as (line, message).

    The engine stops at the first mistake and keeps what it had stored, so the speakers
    returned are exactly the ones it would load.
    """
    toks = _pc_tokens(text)
    pos = 0
    speakers: list[dict] = []
    keys = {str(k).lower(): v for k, v in (sp.get("keys") or {}).items()}
    limit = int(sp.get("max_entries") or 0)

    def take() -> _Tok | None:
        nonlocal pos
        if pos >= len(toks):
            return None
        pos += 1
        return toks[pos - 1]

    def last_line() -> int:
        return toks[min(pos, len(toks)) - 1].line if toks else 1

    def number(want_int: bool) -> float | None:
        t = take()
        neg = t is not None and t.text == "-"
        if neg:
            t = take()
        if t is None or t.kind != "number":
            return None
        try:
            v = float(t.text)
        except ValueError:
            return None
        v = int(v) if want_int else v
        return -v if neg else v

    root = take()
    if root is None or root.text.lower() != str(sp.get("root", "")).lower():
        return [], (root.line if root else 1, f"expected '{sp.get('root')}'")
    t = take()
    if t is None or t.text != "{":
        return [], (last_line(), "expected '{'")
    entry = str(sp.get("entry", "")).lower()
    while (t := take()) is not None and t.text != "}":
        if t.text.lower() != entry:
            return speakers, (t.line, f"unknown token '{t.text}'")
        start = t.line
        b = take()
        if b is None or b.text != "{":
            return speakers, (last_line(), "expected '{'")
        speaker: dict[str, Any] = {"line": start}
        while (k := take()) is not None and k.text != "}":
            rule = keys.get(k.text.lower())
            if rule is None:
                return speakers, (k.line, f"unknown token '{k.text}'")
            kind = rule.get("type")
            name = k.text.lower()
            if kind == "string":
                v = take()
                if v is None:
                    return speakers, (last_line(), f"expected {name} value")
                speaker[name] = v.text
            elif kind == "vec3":
                vec = [number(False) for _ in range(3)]
                if any(x is None for x in vec):
                    return speakers, (k.line, f"expected {name} vector")
                speaker[name] = vec
            elif kind == "int":
                v = number(True)
                if v is None:
                    return speakers, (k.line, f"expected {name} value")
                lo, hi = rule.get("min"), rule.get("max")
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    return speakers, (k.line, f"{name} value {v} is invalid")
                speaker[name] = v
            elif kind == "enum":
                v = take()
                values = [str(x).lower() for x in rule.get("values") or []]
                if v is None:
                    return speakers, (last_line(), f"expected {name} value")
                if v.text.lower() not in values:
                    return speakers, (v.line, f"unknown {name} value '{v.text}'")
                speaker[name] = v.text.lower()
        if limit and len(speakers) >= limit:
            return speakers, (start, f"more than {limit} speakers; the rest are not stored")
        speakers.append(speaker)
    return speakers, None


def parse_sound_scripts(text: str, ss: dict) -> tuple[list[dict], tuple[int, str] | None]:
    """The named sound scripts in one file, each with its sound files, and the first mistake
    as (line, message). Any mistake stops the client, so a file with one loads nothing."""
    directive = str(ss.get("sound_directive", "")).lower()
    per_block = int(ss.get("max_sounds_per_block") or 0)
    scripts: list[dict] = []
    current: dict | None = None
    in_block = False
    count = 0
    toks = mapscript.tokenize(text)
    i = 0
    while i < len(toks):
        t = toks[i]
        i += 1
        if t.text == "{":
            if in_block:
                return scripts, (t.line, f"no concluding '}}' in sound {current['name']!r}")
            if current is None:
                return scripts, (t.line, "'{' found but not expected")
            in_block, count = True, 0
            continue
        if t.text == "}":
            if not in_block:
                return scripts, (t.line, "'}' unexpected")
            in_block = False
            scripts.append(current)
            current = None
            continue
        if not in_block:
            if current is not None:
                return scripts, (t.line, f"'{t.text}' unexpected after sound {current['name']!r}")
            current = {"name": t.text, "line": t.line, "sounds": []}
            continue
        if t.text.lower() == directive:
            if per_block and count >= per_block:
                return scripts, (t.line, f"more than {per_block} sounds in one block")
            if i >= len(toks):
                return scripts, (t.line, "empty sound file name")
            current["sounds"].append((toks[i].text, toks[i].line))
            count += 1
            i += 1
    if in_block or current is not None:
        name = current["name"] if current else "?"
        return scripts, (toks[-1].line if toks else 1, f"no concluding '}}' in sound {name!r}")
    return scripts, None


# ---------------------------------------------------------------------------
# What the base game ships
# ---------------------------------------------------------------------------


def basegame_dir(profile_id: str) -> Path | None:
    base = os.environ.get("NRC_FS_BASEPATH") or ""
    name = (profiles.load(profile_id).get("packaging") or {}).get("basegame")
    if not base or not name:
        return None
    d = Path(base) / str(name)
    return d if d.is_dir() else None


@lru_cache(maxsize=4)
def _basegame_cached(path: str, stamp: tuple, directory: str, wanted_ext: str) -> dict:
    files: set[str] = set()
    texts: dict[str, str] = {}
    for archive in sorted(Path(path).iterdir()):
        if archive.suffix.lower() != ".pk3" or not archive.is_file():
            continue
        try:
            with zipfile.ZipFile(archive) as z:
                for info in z.infolist():
                    low = info.filename.replace("\\", "/").lower()
                    if low.endswith("/"):
                        continue
                    files.add(low)
                    # Later archives win, as the engine's search order has it.
                    if low.startswith(directory) or low.endswith(wanted_ext):
                        texts[low] = z.read(info).decode("latin-1")
        except (zipfile.BadZipFile, OSError):
            continue
    return {"files": files, "texts": texts}


def basegame(profile_id: str) -> dict | None:
    """Every file the base game's archives hold (lowercased), and the text of its sound
    scripts and speaker scripts. None when the base game is not known."""
    d = basegame_dir(profile_id)
    sp = spec(profile_id)
    if d is None or sp is None:
        return None
    stamp = tuple(
        (p.name, p.stat().st_mtime_ns) for p in sorted(d.iterdir()) if p.suffix.lower() == ".pk3"
    )
    ss = sp.get("sound_scripts") or {}
    spk = sp.get("speaker_script") or {}
    ext = "." + str(spk.get("path", "")).rsplit(".", 1)[-1].lower()
    return _basegame_cached(str(d), stamp, str(ss.get("directory", "")).lower() + "/", ext)


# ---------------------------------------------------------------------------
# A map's own files
# ---------------------------------------------------------------------------


def _own(root: Path, rel: str) -> Path | None:
    for r in dict.fromkeys([rel, rel.lower()]):
        if (root / r).is_file():
            return root / r
    return None


def _is_file_name(value: str, sp: dict) -> bool:
    low = value.lower()
    return any(f".{str(e).lower()}" in low for e in sp.get("file_extensions") or [])


def _script_blocks(profile_id: str, map_path: Path, text: str | None) -> list:
    ms = profiles.load(profile_id).get("mapscript")
    if not isinstance(ms, dict):
        return []
    if text is None:
        p = mapscript.script_path(ms, map_path)
        if not p.is_file():
            return []
        text = mapscript.decode(p.read_bytes())
    try:
        return mapscript.parse_spec(text, ms)
    except mapscript.ScriptSyntaxError:
        return []  # mapscript.check reports it


def _actions(blocks: list, rules: list) -> list[tuple[str, str, int]]:
    """(action, argument, line) for every action a rule names."""
    want = {str(r.get("action", "")).lower(): int(r.get("arg", 0)) for r in rules or []}
    out = []
    for b in blocks:
        for ev in b.events:
            for act in ev.actions:
                arg = want.get(act.name)
                if arg is not None and len(act.args) > arg:
                    out.append((act.name, act.args[arg], act.line))
    return out


def sound_files(profile_id: str, map_or_bsp: str | Path) -> dict[str, str]:
    """Sound files the map's own speaker script, sound scripts and map script name, each with
    where it is named. What q3map2's packer never reads, and a pack must carry."""
    sp = spec(profile_id)
    if sp is None:
        return {}
    path = Path(map_or_bsp)
    root, stem = map_root(path), path.stem
    out: dict[str, str] = {}
    spk = sp.get("speaker_script") or {}
    f = _own(root, str(spk.get("path", "")).format(map=stem)) if spk.get("path") else None
    if f is not None:
        speakers, _ = parse_speaker_script(f.read_text(errors="replace"), spk)
        key = str(spk.get("sound_key", "")).lower()
        for s in speakers:
            if s.get(key):
                out.setdefault(s[key], str(f.relative_to(root)))
    ss = sp.get("sound_scripts") or {}
    f = _own(root, str(ss.get("map_file", "")).format(map=stem)) if ss.get("map_file") else None
    if f is not None:
        scripts, _ = parse_sound_scripts(f.read_text(errors="replace"), ss)
        for s in scripts:
            for name, _line in s["sounds"]:
                out.setdefault(name, str(f.relative_to(root)))
    refs = sp.get("references") or {}
    blocks = _script_blocks(profile_id, path.with_suffix(".map"), None)
    for _act, value, _line in _actions(blocks, refs.get("script_actions")):
        if _is_file_name(value, sp) and not value.startswith(str(sp.get("custom_prefix", "*"))):
            out.setdefault(value, "the map script")
    return out


# ---------------------------------------------------------------------------
# Checking
# ---------------------------------------------------------------------------


def check(
    profile_id: str,
    entities: list[dict],
    map_path: str | Path | None,
    script_text: str | None = None,
) -> list[Finding]:
    """Findings for the map's speaker script, sound scripts and the names that point into
    them. Nothing at all if the profile has no `sound:` section or the map is not on disk.

    Whether a file exists is judged only when the base game is known (`NRC_FS_BASEPATH`);
    otherwise only syntax and names are checked.
    """
    sp = spec(profile_id)
    if sp is None or map_path is None:
        return []
    path = Path(map_path)
    root, stem = map_root(path), path.stem
    conf = str(sp.get("confidence", "unverified"))
    base = basegame(profile_id)
    out: list[Finding] = []

    def finding(code, severity, message, ents=(), hint="", locus=""):
        src = f"profile {profile_id} sound ({locus})".replace(" ()", "")
        sev = severity if conf == "verified" else "info"
        return Finding(code, sev, message, conf, src, [i for i in ents if i >= 0][:64], hint)

    def file_exists(name: str) -> bool | None:
        if _own(root, name) is not None:
            return True
        if base is None:
            return None
        return name.lower() in base["files"]

    def base_text(rel: str) -> str | None:
        return base["texts"].get(rel.lower()) if base else None

    # --- the speaker script ---------------------------------------------------
    spk = sp.get("speaker_script") or {}
    speakers: list[dict] | None = None
    sps_rel = str(spk.get("path", "")).format(map=stem)
    sps = _own(root, sps_rel) if spk.get("path") else None
    if sps is not None:
        speakers, err = parse_speaker_script(sps.read_text(errors="replace"), spk)
        if err:
            out.append(
                finding(
                    "SPEAKER_SCRIPT_SYNTAX",
                    "warning",
                    f"{sps_rel}:{err[0]}: {err[1]}; the game stops reading the file there, so "
                    f"only the {len(speakers)} speaker(s) before it load",
                    locus=str(spk.get("locus", "")),
                )
            )
        key = str(spk.get("sound_key", "")).lower()
        name_key = str(spk.get("name_key", "")).lower()
        longest = (spk.get("keys") or {}).get(name_key, {}).get("max_length")
        missing: dict[str, list[int]] = {}
        for s in speakers:
            f = s.get(key)
            if f and file_exists(f) is False:
                missing.setdefault(f, []).append(s["line"])
            tn = s.get(name_key) or ""
            if longest and len(tn) > int(longest):
                out.append(
                    finding(
                        "SPEAKER_NAME_TOO_LONG",
                        "warning",
                        f"{sps_rel}:{s['line']}: {name_key} {tn!r} is cut to {longest} "
                        "characters when loaded, so no script action finds it by its full name",
                        locus=str(spk.get("locus", "")),
                    )
                )
        for f, lines in sorted(missing.items()):
            out.append(
                finding(
                    "SOUND_FILE_MISSING",
                    "warning",
                    f"{sps_rel} line {_lines(lines)}: {key} {f!r} is neither beside the map nor "
                    "in the base game; the speaker plays nothing",
                    hint="put the file under the map's folder at that path, or name one the "
                    "base game ships",
                )
            )
    elif spk.get("path") and base_text(sps_rel) is not None:
        speakers, _ = parse_speaker_script(base_text(sps_rel) or "", spk)

    # --- sound scripts ----------------------------------------------------------
    ss = sp.get("sound_scripts") or {}
    known: set[str] = set()
    directory = str(ss.get("directory", "")).strip("/")
    listing = base_text(str(ss.get("file_list", ""))) if ss.get("file_list") else None
    for listed in mapscript.tokenize(listing or ""):
        scripts, _ = parse_sound_scripts(base_text(f"{directory}/{listed.text}") or "", ss)
        known |= {s["name"].lower() for s in scripts}
    own_rel = str(ss.get("map_file", "")).format(map=stem)
    own = _own(root, own_rel) if ss.get("map_file") else None
    if own is not None:
        scripts, err = parse_sound_scripts(own.read_text(errors="replace"), ss)
        if err:
            out.append(
                finding(
                    "SOUND_SCRIPT_SYNTAX",
                    "error",
                    f"{own_rel}:{err[0]}: {err[1]}; the client stops loading the map",
                    locus=str(ss.get("locus", "")),
                )
            )
        known |= {s["name"].lower() for s in scripts}
        missing = {}
        for s in scripts:
            for f, line in s["sounds"]:
                if file_exists(f) is False:
                    missing.setdefault(f, []).append(line)
        for f, lines in sorted(missing.items()):
            out.append(
                finding(
                    "SOUND_FILE_MISSING",
                    "warning",
                    f"{own_rel} line {_lines(lines)}: sound {f!r} is neither beside the map nor "
                    "in the base game; the sound script plays nothing for it",
                )
            )
    elif ss.get("map_file") and base_text(own_rel) is not None:
        scripts, _ = parse_sound_scripts(base_text(own_rel) or "", ss)
        known |= {s["name"].lower() for s in scripts}
    names_known = base is not None or own is not None

    # --- names in the map and its script ------------------------------------------
    refs = sp.get("references") or {}
    custom = str(sp.get("custom_prefix", "*"))
    # (kind, value) -> where it is named, the entities naming it, the script lines naming it
    unresolved: dict[tuple[str, str], tuple[str, list[int], list[int]]] = {}

    def resolve(value: str, where: str, ent: int | None, line: int | None) -> None:
        if not value or value.startswith(custom):
            return
        if _is_file_name(value, sp):
            if file_exists(value) is not False:
                return
            kind = "file"
        elif not names_known or value.lower() in known:
            return
        else:
            kind = "script"
        _, ents, lines = unresolved.setdefault((kind, value), (where, [], []))
        if ent is not None:
            ents.append(ent)
        if line is not None:
            lines.append(line)

    for rule in refs.get("entity_keys") or []:
        cls, key = rule.get("classname"), str(rule.get("key", "")).lower()
        for e in entities:
            if cls and e.get("classname") != cls:
                continue
            v = next((v for k, v in e.get("keys") or [] if str(k).lower() == key), "")
            resolve(v.strip(), f"{cls} {key}", int(e.get("index", -1)), None)
    blocks = _script_blocks(profile_id, path, script_text)
    ms = profiles.load(profile_id).get("mapscript")
    script_name = mapscript.script_path(ms, path).name if isinstance(ms, dict) else "map script"
    for act, value, line in _actions(blocks, refs.get("script_actions")):
        resolve(value, f"{script_name} {act}", None, line)

    for (kind, value), (where, ents, lines) in sorted(unresolved.items()):
        at = f" (line {_lines(sorted(lines))})" if lines else ""
        if kind == "file":
            out.append(
                finding(
                    "SOUND_FILE_MISSING",
                    "warning",
                    f"{where}{at}: sound file {value!r} is neither beside the map nor in the "
                    "base game; it plays nothing",
                    ents,
                )
            )
        else:
            out.append(
                finding(
                    "SOUND_SCRIPT_UNRESOLVED",
                    "warning",
                    f"{where}{at}: {value!r} has no file extension, so it names a sound script, "
                    f"and no sound script loaded for this map defines it. Only the files "
                    f"{ss.get('file_list')} lists and {own_rel} are loaded; another map's sound "
                    "scripts are not",
                    ents,
                    hint=f"define it in {own_rel}, or name a sound file",
                    locus=str(ss.get("locus", "")),
                )
            )

    # --- speakers switched by name -------------------------------------------------
    switched = _actions(blocks, refs.get("speaker_actions"))
    if switched:
        name_key = str(spk.get("name_key", "")).lower()
        have = {str(s.get(name_key, "")).lower() for s in speakers or []}
        bad: dict[str, list[int]] = {}
        for _act, value, line in switched:
            if value.lower() not in have:
                bad.setdefault(value, []).append(line)
        for value, lines in sorted(bad.items()):
            reason = (
                f"no speaker in {sps_rel} has that {spk.get('name_key')}"
                if speakers is not None
                else f"the map has no speaker script ({sps_rel})"
            )
            out.append(
                finding(
                    "SPEAKER_UNRESOLVED",
                    "warning",
                    f"{script_name} line {_lines(sorted(lines))}: speaker {value!r} - {reason}, "
                    "so the action does nothing, silently",
                    hint=f"add a speaker with that {spk.get('name_key')} to {sps_rel}, or "
                    "correct the name",
                    locus=str(refs.get("speaker_locus", "")),
                )
            )
    return [_known_missing(f, profile_id) for f in out]


def _known_missing(f: Finding, profile_id: str) -> Finding:
    """A missing file the profile lists as one the base game itself never shipped is info."""
    if f.code != "SOUND_FILE_MISSING":
        return f
    for a in (profiles.load(profile_id).get("packaging") or {}).get("allowed_missing") or []:
        path = str(a.get("path", "")) if isinstance(a, dict) else ""
        if path and f"{path!r}".lower() in f.message.lower():
            note = " ".join(str(a.get("reason", "")).split())
            return Finding(
                f.code,
                "info",
                f"{f.message}. A known gap in the base game itself: {note}",
                f.confidence,
                f.rule_source,
                f.entities,
                f.fix_hint,
            )
    return f


def _lines(lines: list[int]) -> str:
    shown = ", ".join(str(n) for n in lines[:6])
    return shown + (f" (+{len(lines) - 6} more)" if len(lines) > 6 else "")
