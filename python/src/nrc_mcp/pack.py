"""Packaging and release checks (§6.4).

The spec's instruction here is "wrap, don't reimplement", and it is right: q3map2 already
traces every shader, texture, model and sound a BSP references, and reimplementing that
tracing would mean maintaining a second, worse copy of the engine's resource model.

So this module drives `-pk3` and `-repack`, and reads their output. Three verified details
shape the code:

- `-pk3` and `-repack` are consumed with `takeFront`, so each **must be the first argument**,
  and the filename with `takeBack`, so it **must be last**. `tools/q3map2.py` handles both.
- Success is written as `<name>_autopacked.pk3`; **failure** as `<name>_FAILEDpack.pk3`. That
  naming is a free pass/fail oracle for "did every referenced resource exist", and it is the
  single most valuable check here.
- `repack.exclude` is resolved next to the q3map2 binary, not next to the map, and applies to
  `-repack` only.
- The archive is written to the engine path, the first base path (`autopk3.cpp:564`,
  `path_init.cpp:83-84`), which with `-fs_basepath` is the game install. `pack_pk3` moves it
  out, next to the folder that holds `maps/`.

q3map2 also packs only what it traces: it never adds a map's script, and finds an arena only
on its own search paths. Without `<game>.exclude` beside the binary it copies base-game
textures in too. So `pack_pk3` rewrites the archive afterwards, from the profile's
`packaging` section: the companion files and arena found beside the map go in, and entries the
base game's own archives already hold at the same path come out.

`ship_check` is driven by the profile's `packaging` section, because naming conventions,
levelshot sizes and reserved shader prefixes are properties of a game, not of this tool.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

from . import profiles
from .kernel import repo_root

#: Written by q3map2's packer. Verified in `tools/quake3/q3map2/autopk3.cpp`.
SUCCESS_SUFFIX = "_autopacked.pk3"
FAILURE_SUFFIX = "_FAILEDpack.pk3"


class PackError(RuntimeError):
    pass


def _q3map2(args: list[str], target: Path, timeout: int = 1800) -> dict[str, Any]:
    """Run the q3map2 wrapper, which handles argument order and path translation."""
    cmd = [
        sys.executable,
        str(repo_root() / "tools" / "q3map2.py"),
        "--flags",
        *args,
        "--",
        str(target),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace", timeout=timeout, check=False
    )
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def pack_pk3(
    bsp: str | Path,
    complevel: int | None = None,
    png: bool = False,
    profile_id: str | None = None,
) -> dict[str, Any]:
    """Build a pk3 for a compiled BSP with `q3map2 -pk3`, then fix up what it produced.

    The packer's own naming is the oracle: if it produces `_FAILEDpack.pk3`, some resource the
    BSP references does not exist on disk, and the map will show missing textures on a server
    even though it compiled. That is reported as a failure regardless of exit code.

    With `profile_id`, the archive also gains the profile's companion files and arena that sit
    beside the map, and loses what the base game already ships (when `NRC_FS_BASEPATH` is set).
    Either way it ends up next to the map's folder, never in the game install.
    """
    target = Path(bsp)
    if not target.is_file():
        raise PackError(f"{target} does not exist — compile the map first")

    flags = ["-pk3"]
    if png:
        flags.append("-png")
    if complevel is not None:
        if not -1 <= complevel <= 10:
            raise PackError("complevel must be between -1 and 10")
        flags += ["-complevel", str(complevel)]

    pkg = _packaging(profile_id)
    base = os.environ.get("NRC_FS_BASEPATH") or ""
    # q3map2 removes its previous outputs before packing (autopk3.cpp:564-567); do the same where
    # ours land, or a stale failure marker from an earlier run would read as this run's.
    for suffix in (SUCCESS_SUFFIX, FAILURE_SUFFIX):
        _destination(target, f"{target.stem}{suffix}", base).unlink(missing_ok=True)

    run = _q3map2(flags, target)

    # The packer reports where it wrote the archive; that report is authoritative.
    produced = _find_packages(target, _packer_text(run))
    basegame = Path(base) / str(pkg["basegame"]) if base and pkg.get("basegame") else None

    moved: list[Path] = []
    added: list[str] = []
    dropped: list[str] = []
    for p in produced:
        dest = _destination(target, p.name, base)
        if p.name.endswith(SUCCESS_SUFFIX):
            a, d = _fix_up(p, dest, target, pkg, basegame)
            added += a
            dropped += d
        elif p.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            p.replace(dest)
        if p.resolve() != dest.resolve() and p.exists():
            p.unlink()
        moved.append(dest)
    failed = [p for p in moved if p.name.endswith(FAILURE_SUFFIX)]
    good = [p for p in moved if p.name.endswith(SUCCESS_SUFFIX)]

    return {
        "ok": run["ok"] and not failed and bool(good),
        "packages": [str(p) for p in good],
        "failed_packages": [str(p) for p in failed],
        "missing_resources": bool(failed),
        "added_companions": added,
        "dropped_basegame": dropped,
        "hint": (
            f"a {FAILURE_SUFFIX} package means the BSP references a resource that is not on "
            "disk; run repack_analyze to see what it asked for"
        )
        if failed
        else "",
        "returncode": run["returncode"],
        "output_tail": run["stdout"][-2000:] or run["stderr"][-2000:],
    }


def _packaging(profile_id: str | None) -> dict:
    if not profile_id:
        return {}
    try:
        pkg = profiles.load(profile_id).get("packaging")
    except profiles.ProfileError as e:
        raise PackError(str(e)) from e
    return pkg if isinstance(pkg, dict) else {}


def _map_root(bsp: Path) -> Path:
    """The folder that holds `maps/`, which the archive's paths are relative to."""
    return bsp.parent.parent if bsp.parent.name.lower() == "maps" else bsp.parent


def _inside(path: Path, root: str) -> bool:
    if not root:
        return False
    try:
        path.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return False
    return True


def _destination(bsp: Path, name: str, basepath: str) -> Path:
    """Next to the map's folder, unless that is inside the game install."""
    root = _map_root(bsp)
    if _inside(root, basepath):
        return repo_root() / "out" / bsp.stem / name
    return root / name


def _companion_paths(stem: str, pkg: dict) -> list[list[str]]:
    """Archive paths the profile lists for a map, each with the alternatives that satisfy it."""
    out: list[list[str]] = []
    for comp in pkg.get("companions") or []:
        if not isinstance(comp, dict) or not comp.get("path"):
            continue
        want = str(comp["path"]).format(map=stem)
        base = want.rsplit(".", 1)[0]
        out.append([want] + [f"{base}.{x}" for x in comp.get("any_extension") or []])
    arena = pkg.get("arena")
    if isinstance(arena, dict) and arena.get("directory") and arena.get("extension"):
        out.append([f"{arena['directory']}/{stem}.{arena['extension']}"])
    return out


def basegame_entries(basegame: Path) -> set[str]:
    """Every path the base game's archives hold, lowercased, as the engine matches them."""
    names: set[str] = set()
    if not basegame.is_dir():
        return names
    for archive in sorted(basegame.iterdir()):
        if archive.suffix.lower() != ".pk3" or not archive.is_file():
            continue
        try:
            with zipfile.ZipFile(archive) as z:
                names.update(n.lower() for n in z.namelist() if not n.endswith("/"))
        except zipfile.BadZipFile:
            continue
    return names


def _fix_up(
    produced: Path, dest: Path, bsp: Path, pkg: dict, basegame: Path | None
) -> tuple[list[str], list[str]]:
    """Rewrite the packer's archive at `dest`: companions in, base-game duplicates out."""
    shipped = basegame_entries(basegame) if basegame else set()
    root = _map_root(bsp)
    added: list[str] = []
    dropped: list[str] = []
    tmp = dest.with_name(dest.name + ".tmp")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(produced) as src, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as out:
        present: set[str] = set()
        for info in src.infolist():
            name = info.filename
            # The map's own BSP is never dropped, even if a base archive happens to hold one.
            if name.lower() in shipped and not name.lower().endswith(
                f"maps/{bsp.stem}.bsp".lower()
            ):
                dropped.append(name)
                continue
            out.writestr(info, src.read(info))
            present.add(name.lower())
        for options in _companion_paths(bsp.stem, pkg):
            if any(o.lower() in present for o in options):
                continue
            for o in options:
                f = root / o
                if f.is_file():
                    out.write(f, o)
                    present.add(o.lower())
                    added.append(o)
                    break
    tmp.replace(dest)
    return added, dropped


#: q3map2's own report of where it put the archive, e.g. `Saved to /path/to/x_autopacked.pk3`.
#: Verified against `tools/quake3/q3map2/autopk3.cpp`.
_SAVED_TO = re.compile(r"^\s*Saved to (.+\.pk3)\s*$", re.MULTILINE)


def _packer_text(run: dict[str, Any]) -> str:
    """The compiler's own console output, out of the wrapper's JSON envelope.

    `tools/q3map2.py` reports structured JSON on stdout and keeps q3map2's console output in
    each stage's `tail`. Scanning the envelope as raw text does not work — inside JSON the
    line breaks are escaped, so anything matching line by line silently finds nothing. That
    is how a successful pack came back reported as a failure.
    """
    text = (run.get("stdout") or "") + (run.get("stderr") or "")
    try:
        envelope = json.loads(run.get("stdout") or "")
    except (ValueError, TypeError):
        return text
    parts: list[str] = []
    for stage in envelope.get("stages", []):
        if not isinstance(stage, dict):
            continue
        # `tail` is a list of lines, but has been a string; accept either rather than
        # depend on which, since this is a seam between two of our own tools.
        tail = stage.get("tail") or ""
        parts.append("\n".join(tail) if isinstance(tail, list) else str(tail))
    parts.append(text)
    return "\n".join(p for p in parts if p)


def _find_packages(bsp: Path, output: str = "") -> list[Path]:
    """Look for packages the packer may have written.

    **The packer's own report comes first.** q3map2 resolves the output path through its VFS
    against the engine base, not against the cwd and not against the BSP — so a map compiled
    outside a game tree gets its pk3 written somewhere neither the caller nor this module
    would think to look. Globbing alone reported a perfectly good pack as a failure, which is
    the worst of both answers: the archive exists and the tool says it does not.

    The glob stays as a fallback, for a q3map2 whose output was truncated or reworded.
    """
    stem = bsp.stem
    candidates: list[Path] = []
    for m in _SAVED_TO.finditer(output or ""):
        p = Path(m.group(1).strip())
        if p.is_file():
            candidates.append(p)
    roots = [bsp.parent, _map_root(bsp), repo_root() / "out" / stem, repo_root() / "out"]
    for root in roots:
        if root.is_dir():
            candidates += list(root.glob(f"{stem}*.pk3"))
    # Deduplicate while keeping order stable for reproducible output.
    seen: set[str] = set()
    out = []
    for c in candidates:
        k = str(c.resolve())
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


#: Lines of `-repack -analyze` output that name a resource. The flag exists to dump what a
#: BSP asks the filesystem for, which is why parsing it beats tracing dependencies ourselves.
_RESOURCE_RE = re.compile(
    r"(?:^|\s)((?:textures|models|sound|scripts|env|gfx|sprites)/[\w\-./]+\.\w+)"
)


def repack_analyze(bsp: str | Path) -> dict[str, Any]:
    """Dump the resources a BSP references, via `-repack -analyze`."""
    target = Path(bsp)
    if not target.is_file():
        raise PackError(f"{target} does not exist")

    run = _q3map2(["-repack", "-analyze"], target)
    text = run["stdout"] + run["stderr"]

    found: dict[str, list[str]] = {}
    for m in _RESOURCE_RE.finditer(text):
        res = m.group(1)
        found.setdefault(res.split("/", 1)[0], []).append(res)
    for k in found:
        found[k] = sorted(set(found[k]))

    missing = sorted(
        {
            line.strip()
            for line in text.splitlines()
            if "not found" in line.lower() or "missing" in line.lower()
        }
    )

    return {
        "ok": run["ok"],
        "resources_by_kind": {k: len(v) for k, v in sorted(found.items())},
        "resources": {k: v[:200] for k, v in sorted(found.items())},
        "total_resources": sum(len(v) for v in found.values()),
        "reported_missing": missing[:40],
        "returncode": run["returncode"],
        "note": (
            "parsed from q3map2's own resource dump rather than traced independently — the "
            "compiler's view of what a BSP needs is the one that matters"
        ),
    }


# ---------------------------------------------------------------------------
# ship_check
# ---------------------------------------------------------------------------


def _finding(code: str, severity: str, message: str, confidence: str, fix: str = "") -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "confidence": confidence,
        "fix_hint": fix,
    }


def ship_check(
    map_or_bsp: str | Path,
    profile_id: str,
    pk3: str | Path | None = None,
) -> dict[str, Any]:
    """The release checklist (§6.4).

    Everything game-specific comes from the profile's `packaging` section. An unverified
    convention produces `info`, never a failure — the same clamp the rule engine applies, for
    the same reason.
    """
    target = Path(map_or_bsp)
    stem = target.stem
    findings: list[dict] = []

    try:
        pkg = profiles.load(profile_id).get("packaging")
    except profiles.ProfileError as e:
        raise PackError(str(e)) from e
    if not isinstance(pkg, dict):
        return {
            "findings": [
                _finding(
                    "SHIP_NO_PACKAGING_PROFILE",
                    "info",
                    f"profile {profile_id} states no packaging conventions, so nothing "
                    "game-specific can be checked",
                    "verified",
                )
            ],
            "summary": {"error": 0, "warning": 0, "info": 1},
        }

    def sev(asked: str, confidence: str) -> str:
        return asked if confidence == "verified" else "info"

    # --- name convention -----------------------------------------------------
    pattern = pkg.get("map_name_pattern")
    if isinstance(pattern, str):
        conf = str(pkg.get("map_name_confidence", "unverified"))
        if not re.match(pattern, stem):
            findings.append(
                _finding(
                    "SHIP_MAP_NAME",
                    sev("warning", conf),
                    f"map name {stem!r} does not match the release convention {pattern!r}"
                    f" — {pkg.get('map_name_note', '')}",
                    conf,
                    "rename the .map and .bsp before release",
                )
            )

    # --- the packer's own oracle ---------------------------------------------
    # No packer output to read here: ship_check inspects what a previous `pack_pk3` left
    # behind rather than running one, so the glob is all there is.
    failed = [p for p in _find_packages(target) if p.name.endswith(FAILURE_SUFFIX)]
    if failed:
        findings.append(
            _finding(
                "SHIP_FAILED_PACK",
                "error",
                f"the packer produced {failed[0].name}: the BSP references a resource that is "
                "not on disk, so the map will show missing content on a server",
                "verified",
                "run repack_analyze to list what it asked for",
            )
        )

    # --- pk3 contents --------------------------------------------------------
    archive = Path(pk3) if pk3 else None
    if archive is None:
        good = [p for p in _find_packages(target) if p.name.endswith(SUCCESS_SUFFIX)]
        archive = good[0] if good else None

    if archive is None or not archive.is_file():
        findings.append(
            _finding(
                "SHIP_NO_PACKAGE",
                "info",
                "no pk3 found to inspect; run pack_pk3 first to check its contents",
                "verified",
            )
        )
    else:
        findings += _check_archive(archive, stem, pkg, sev)

    order = {"error": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: (order[f["severity"]], f["code"]))
    return {
        "target": str(target),
        "package": str(archive) if archive else None,
        "profile": profile_id,
        "findings": findings,
        "summary": {
            s: sum(1 for f in findings if f["severity"] == s) for s in ("error", "warning", "info")
        },
    }


def _check_archive(archive: Path, stem: str, pkg: dict, sev) -> list[dict]:
    findings: list[dict] = []
    try:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
    except (OSError, zipfile.BadZipFile) as e:
        return [
            _finding(
                "SHIP_PACKAGE_UNREADABLE",
                "error",
                f"{archive.name} is not a readable zip: {e}",
                "verified",
            )
        ]

    lower = [n.lower() for n in names]
    size = archive.stat().st_size

    # --- size budget ---------------------------------------------------------
    budget = pkg.get("size_budget_bytes")
    if isinstance(budget, int) and size > budget:
        conf = str(pkg.get("size_budget_confidence", "unverified"))
        findings.append(
            _finding(
                "SHIP_PACKAGE_LARGE",
                sev("warning", conf),
                f"{archive.name} is {size / 1048576:.1f} MiB, over the "
                f"{budget / 1048576:.0f} MiB budget — {pkg.get('size_budget_note', '')}",
                conf,
                "audit texture resolutions against in-world texel density",
            )
        )

    # --- levelshot -----------------------------------------------------------
    ls = pkg.get("levelshot")
    if isinstance(ls, dict):
        d = str(ls.get("directory", "levelshots")).lower()
        conf = str(ls.get("confidence", "unverified"))
        if not any(n.startswith(f"{d}/") and stem.lower() in n for n in lower):
            findings.append(
                _finding(
                    "SHIP_NO_LEVELSHOT",
                    sev("warning", conf),
                    f"no levelshot for {stem} under {d}/ in {archive.name}; the server "
                    "browser will show a blank entry",
                    conf,
                    f"add {d}/{stem}.jpg at {ls.get('preferred_size')}",
                )
            )

    # --- arena file ----------------------------------------------------------
    ar = pkg.get("arena")
    if isinstance(ar, dict):
        d = str(ar.get("directory", "scripts")).lower()
        ext = str(ar.get("extension", "arena")).lower()
        conf = str(ar.get("confidence", "unverified"))
        arenas = [n for n in lower if n.startswith(f"{d}/") and n.endswith(f".{ext}")]
        if not arenas:
            findings.append(
                _finding(
                    "SHIP_NO_ARENA",
                    sev("warning", conf),
                    f"no .{ext} file under {d}/ in {archive.name}; without it the map does "
                    f"not appear in the in-game map list. {ar.get('note', '')}",
                    conf,
                    f"add {d}/{stem}.{ext}",
                )
            )

    # --- other files the game looks a map up by -------------------------------
    # Each names a path pattern; a required one missing is a warning, an optional one is noted.
    for comp in pkg.get("companions") or []:
        if not isinstance(comp, dict) or not comp.get("path"):
            continue
        want = str(comp["path"]).format(map=stem).lower()
        # An image the renderer finds under any of several extensions.
        base = want.rsplit(".", 1)[0]
        accepted = {want} | {f"{base}.{x}".lower() for x in comp.get("any_extension") or []}
        if accepted & set(lower):
            continue
        conf = str(comp.get("confidence", "unverified"))
        required = bool(comp.get("required"))
        findings.append(
            _finding(
                "SHIP_NO_COMPANION",
                sev("warning", conf) if required else "info",
                f"no {want} in {archive.name} ({comp.get('role', 'companion file')}"
                f"{'' if required else ', optional'})",
                conf,
                f"add {want}",
            )
        )

    # --- reserved shader prefixes -------------------------------------------
    reserved = pkg.get("reserved_shader_prefixes")
    if isinstance(reserved, list):
        shipped_scripts = [n for n in names if n.lower().startswith("scripts/")]
        clashes: list[str] = []
        for prefix in reserved:
            p = str(prefix).lower().rstrip("/")
            clashes += [
                n
                for n in names
                if n.lower().startswith(f"textures/{p}/") or n.lower() == f"scripts/{p}.shader"
            ]
        if clashes:
            findings.append(
                _finding(
                    "SHIP_SHADOWS_BASEGAME",
                    "warning",
                    f"{len(clashes)} file(s) in {archive.name} sit under a base-game path "
                    f"({', '.join(sorted(set(clashes))[:4])}) — shipping a copy overrides it "
                    "for every other map on the server",
                    "verified",
                    "move assets under a directory named for this map",
                )
            )
        if not shipped_scripts:
            findings.append(
                _finding(
                    "SHIP_NO_SHADER_SCRIPT",
                    "info",
                    f"{archive.name} ships no shader script; fine if the map uses only "
                    "base-game shaders",
                    "verified",
                )
            )

    if not any(n.lower().endswith(".bsp") for n in lower):
        findings.append(
            _finding(
                "SHIP_NO_BSP",
                "error",
                f"{archive.name} contains no .bsp — the package is unusable",
                "verified",
            )
        )
    return findings
