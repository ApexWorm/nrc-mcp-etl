"""Game profiles — the one layer that knows which game we are serving (§7.4).

Everything game-specific lives in `profiles/*.yaml` as **data**: entity ontology, gametype
ids, spawn rules, movement constants, packaging conventions. No module outside this one
loads them, no module anywhere hardcodes their contents, and `tools/seam_lint.py` fails the
build if a game-specific string appears in code.

Every rule carries a `confidence`. Only `verified` rules — confirmed against a shipped
gamepack, upstream source, or real map files — may produce a hard failure. That distinction
is not bureaucracy: three of the spec's supposedly-verified spawn rules turned out to be
wrong when checked against the actual gamepack, and one of them would have failed correct
maps. See `docs/spec-corrections.md`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from .kernel import repo_root


class ProfileError(RuntimeError):
    pass


def profiles_dir() -> Path:
    return repo_root() / "profiles"


def available() -> list[str]:
    d = profiles_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))


@lru_cache(maxsize=8)
def load(profile_id: str) -> dict[str, Any]:
    """Load a profile by id, e.g. the value of `NRC_PROFILE`."""
    try:
        import yaml
    except ImportError as e:  # pragma: no cover - environment problem, not logic
        raise ProfileError("pyyaml is required to read profiles. Run: mise run py:sync") from e

    path = profiles_dir() / f"{profile_id}.yaml"
    if not path.is_file():
        raise ProfileError(
            f"no profile {profile_id!r} in {profiles_dir()}; available: {available() or 'none'}"
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"{path} is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"{path} should contain a mapping at the top level")
    bad = bad_confidences(data)
    if bad:
        raise ProfileError(
            f"{path}: confidence must be exactly one of {', '.join(CONFIDENCES)}; found "
            + "; ".join(f"{where} = {value!r}" for where, value in bad[:8])
            + (f" (and {len(bad) - 8} more)" if len(bad) > 8 else "")
            + ". Code compares the exact string, and the protected-path pin treats every other "
            "spelling as verified, so a variant like `Verified` would be read two ways."
        )
    return data


#: The only confidence values a profile may state. Code compares them as exact strings.
CONFIDENCES = ("verified", "unverified")


def bad_confidences(data: Any, where: str = "") -> list[tuple[str, Any]]:
    """Every `confidence` (or `*_confidence`) value in `data` that is not exactly a CONFIDENCES
    member, with where it sits."""
    out: list[tuple[str, Any]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            here = f"{where}.{k}" if where else str(k)
            if (k == "confidence" or (isinstance(k, str) and k.endswith("_confidence"))) and (
                v not in CONFIDENCES
            ):
                out.append((here, v))
            out += bad_confidences(v, here)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            out += bad_confidences(v, f"{where}[{i}]")
    return out


def entities(profile_id: str) -> list[dict[str, Any]]:
    data = load(profile_id)
    ents = data.get("entities") or []
    return [e for e in ents if isinstance(e, dict)]


def verified_entities(profile_id: str) -> list[dict[str, Any]]:
    return [e for e in entities(profile_id) if e.get("confidence") == "verified"]


def entity_def(profile_id: str, classname: str) -> dict[str, Any] | None:
    """The profile's entry for `classname`, or None if the profile does not define it."""
    for e in entities(profile_id):
        if e.get("classname") == classname:
            return e
    return None


def declared_keys(profile_id: str, entity: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every key the profile declares on an entity entry, by name.

    Includes the shared `key_blocks` the entry names, which is where a profile puts keys that
    many classes carry alike — leaving them out would report every compiler key as unknown.
    A block marked `applies_to_all` holds keys the engine accepts on every entity, and counts
    for every entry without being named. One marked `case_insensitive` is also matched by its
    lowercase spelling, for an engine that compares key names without regard to case.
    """
    out: dict[str, dict[str, Any]] = {}
    blocks = load(profile_id).get("key_blocks") or {}
    if not isinstance(blocks, dict):
        blocks = {}
    for block in blocks.values():
        if not isinstance(block, dict) or block.get("applies_to_all") is not True:
            continue
        for k in block.get("keys") or []:
            if isinstance(k, dict) and k.get("name"):
                out[str(k["name"])] = k
                if block.get("case_insensitive") is True:
                    out.setdefault(str(k["name"]).lower(), k)
    for block_name in entity.get("key_blocks") or []:
        block = blocks.get(block_name) if isinstance(blocks, dict) else None
        for k in (block or {}).get("keys") or []:
            if isinstance(k, dict) and k.get("name"):
                out[str(k["name"])] = k
    for k in entity.get("keys") or []:
        if isinstance(k, dict) and k.get("name"):
            out[str(k["name"])] = k
    return out


def gametypes(profile_id: str) -> list[dict[str, Any]]:
    data = load(profile_id)
    return [g for g in (data.get("gametypes") or []) if isinstance(g, dict)]


def movement(profile_id: str) -> dict[str, Any]:
    """Player dimensions and movement constants, if the profile states them.

    Read from data, never hardcoded — §7.4 names "physics constants hardcoded into
    `movement_check`" as the most likely way game specifics leak into code.
    """
    data = load(profile_id)
    out = {}
    for key in ("movement", "units", "measurements"):
        section = data.get(key)
        if isinstance(section, dict):
            out[key] = section
    return out


def standing_height(profile_id: str) -> float | None:
    """The player's standing height in world units, if the profile states it.

    This is what a player-eye render is placed at. It must come from here rather than from a
    constant in code: §7.4 names hardcoded physics constants as the most likely seam leak, and
    the design document's own assumed figure was wrong for the first target game by 13 units
    (`docs/spec-corrections.md`).

    Only a `verified` entry is used. An unverified height would put the camera somewhere the
    player cannot actually stand, which is worse than declining to guess.
    """
    mv = movement(profile_id).get("movement")
    if not isinstance(mv, dict):
        return None
    for key in ("eye_height", "headroom_stand"):
        entry = mv.get(key)
        if isinstance(entry, dict) and entry.get("confidence") == "verified":
            v = entry.get("value")
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return float(v)
    return None


def summary(profile_id: str) -> dict[str, Any]:
    """A compact overview, for the profile MCP resource."""
    data = load(profile_id)
    ents = entities(profile_id)
    verified = verified_entities(profile_id)
    unverified_section = data.get("unverified") or []

    by_kind: dict[str, int] = {}
    for e in ents:
        by_kind[str(e.get("kind", "unknown"))] = by_kind.get(str(e.get("kind", "unknown")), 0) + 1

    return {
        "profile": data.get("profile", {}),
        "entity_count": len(ents),
        "verified_entity_count": len(verified),
        "entities_by_kind": by_kind,
        "gametypes": gametypes(profile_id),
        "movement": movement(profile_id),
        "unverified_count": len(unverified_section) if isinstance(unverified_section, list) else 0,
        "sections": sorted(data.keys()),
        "note": (
            "Only `confidence: verified` entries may produce hard validation failures. "
            "See docs/spec-corrections.md for rules that documentation got wrong."
        ),
    }
