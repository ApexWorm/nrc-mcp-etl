"""A declarative rule engine for game-specific validation.

Every rule this module can evaluate is **data in a profile**, never code here. That is not
tidiness: `tools/seam_lint.py` fails the build if a game-specific string appears in
`python/src`, so a validator that mentioned a classname would not compile the project. The
constraint forces the right design — adding a rule means editing YAML, and a second game
means a second YAML.

Each rule carries a `confidence`. Only `verified` rules may produce an `error`; anything
`unverified` is downgraded to `info` no matter what the profile asks for. That mechanism
exists because it was needed: three of the four Urban Terror spawn rules the design document
called verified turned out to be wrong when checked against the shipped gamepack, and one
would have failed correct maps (`docs/spec-corrections.md`).

# Check types

`require_any`
    At least one entity from `classnames` must exist.
`count_exact`
    `classname` must appear exactly `count` times.
`count_min` / `count_max`
    Bounds on the number of entities of `classname`.
`key_in_enum`
    Entities of `classname` must have `key` in `values`.
`key_required`
    Entities of `classname` must have `key` at all.
`distinct_group_per_team`
    For entities of `classname` selected by a gametype list, no `group_key` value may be
    shared between two different `team_key` values.
`group_size`
    Each `group_key` bucket should hold `count` entities.
`min_spacing`
    Entities sharing a `group_key` must be at least `distance` apart.
`forbid_gametype`
    Entities of `classname` must not list `gametype` in `gametype_key`.
`flag_count_min`
    At least `count` entities of `classname` must have spawnflag value `flag` set.
`flag_requires_key`
    Entities of `classname` with spawnflag value `flag` set must have `key`.
`targets_resolve`
    Every value of each key in `keys` must name an entity's `targetname_key`. Keys in
    `list_keys` hold several names, split on `separator`. `classname` narrows the entities
    checked; without it every entity is. `exclude_classnames` skips classes whose key of the
    same name means something else.
`flags_any`
    Entities of `classname` must have at least one of the spawnflag values in `flags`.
`targeted_by`
    Entities of `classname` (optionally only those with every flag in `when_flags_all` and none
    in `when_flags_none`) must be named, by their `name_key`, in the `by_key` of some entity of
    `by_classname`.
`list_targets`
    Each `separator`-terminated entry of `keys` on entities of `classname` that names an entity
    must name one of `target_classes`. Unterminated trailing text is not an entry.
`list_counts_match`
    On entities of `classname` where both of the two `keys` are set, their resolved entries
    must be equally many.
`key_pattern`
    Where entities of `classname` have `key`, its value must match the regular expression
    `pattern` in full.

Every check may carry `applies_when: {classname_present: X}` so a rule about one gametype
stays silent on maps that do not use it. `classname_absent: X` (or a list, all absent) and
`flag_present: {classname: X, flag: N}` (or a list of those, any matching) narrow it further.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

from . import profiles

SEVERITIES = ("info", "warning", "error")


@dataclass
class Finding:
    code: str
    severity: str
    message: str
    confidence: str
    rule_source: str
    entities: list[int] = field(default_factory=list)
    fix_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "confidence": self.confidence,
            "rule_source": self.rule_source,
            "entities": self.entities,
            "fix_hint": self.fix_hint,
        }


# ---------------------------------------------------------------------------
# Entity helpers
# ---------------------------------------------------------------------------


class _Keys(dict):
    """Key names matched without regard to case, as the engines' spawn parsers match them."""

    def get(self, key, default=None):
        return super().get(str(key).lower(), default)

    def __contains__(self, key) -> bool:
        return super().__contains__(str(key).lower())


def _keys(entity: dict) -> _Keys:
    """Entity keys as a mapping, first occurrence winning.

    The kernel hands over ordered pairs because `.map` key order is meaningful and duplicates
    occur; rule evaluation does not care about order, and taking the first duplicate matches
    what the engine's own lookup does. Names compare case-insensitively: a shipped map that
    writes `Track` for `track` loads fine, so a rule must read it too.
    """
    out = _Keys()
    for k, v in entity.get("keys") or []:
        out.setdefault(str(k).lower(), v)
    return out


def _of_class(entities: list[dict], classname: str) -> list[dict]:
    return [e for e in entities if e.get("classname") == classname]


def _gametype_list(value: str) -> set[str]:
    """Parse a gametype key value.

    The convention is a comma-separated list, but real maps use spaces and semicolons too, so
    all three separate. Tokens are kept as strings: the profile states ids as integers and a
    map states them as text, and comparing normalized strings avoids inventing a numeric
    parse that could silently drop a malformed entry.
    """
    out: set[str] = set()
    for token in value.replace(";", ",").replace(" ", ",").split(","):
        token = token.strip()
        if token:
            out.add(token)
    return out


def _origin(entity: dict) -> tuple[float, float, float] | None:
    o = entity.get("origin")
    if isinstance(o, (list, tuple)) and len(o) == 3:
        try:
            return (float(o[0]), float(o[1]), float(o[2]))
        except (TypeError, ValueError):
            return None
    return None


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _applies(rule: dict, entities: list[dict]) -> bool:
    cond = rule.get("applies_when")
    if not isinstance(cond, dict):
        return True
    present = cond.get("classname_present")
    if present and not _of_class(entities, str(present)):
        return False
    absent = cond.get("classname_absent")
    absent_names = absent if isinstance(absent, list) else [absent] if absent else []
    if any(_of_class(entities, str(name)) for name in absent_names):
        return False
    # `flag_present`: some entity of a class has a spawnflag set. A list means any of them.
    wanted = cond.get("flag_present")
    if wanted:
        options = wanted if isinstance(wanted, list) else [wanted]
        return any(
            spawnflags(e) & int(o.get("flag", 0))
            for o in options
            if isinstance(o, dict)
            for e in _of_class(entities, str(o.get("classname", "")))
        )
    return True


def _check_require_any(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    names = [str(n) for n in p.get("classnames") or []]
    found = {n: len(_of_class(entities, n)) for n in names}
    total = sum(found.values())
    return total > 0, {"found": total, "detail": found, "expected": " or ".join(names)}


def _check_count(p: dict, entities: list[dict], mode: str) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    matches = _of_class(entities, cls)
    n = len(matches)
    want = int(p.get("count", 0))
    ok = {"exact": n == want, "min": n >= want, "max": n <= want}[mode]
    return ok, {
        "found": n,
        "expected": want,
        "classname": cls,
        "entities": [e["index"] for e in matches],
    }


def _check_key_in_enum(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    key = str(p.get("key", ""))
    allowed = {str(v) for v in p.get("values") or []}
    bad = []
    for e in _of_class(entities, cls):
        v = _keys(e).get(key)
        if v is not None and str(v) not in allowed:
            bad.append((e["index"], v))
    return not bad, {
        "found": len(bad),
        "expected": sorted(allowed),
        "classname": cls,
        "key": key,
        "entities": [i for i, _ in bad],
        "detail": dict(bad[:8]),
    }


def _check_key_required(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    key = str(p.get("key", ""))
    missing = [e["index"] for e in _of_class(entities, cls) if key not in _keys(e)]
    return not missing, {
        "found": len(missing),
        "classname": cls,
        "key": key,
        "entities": missing,
    }


def _selected_by_gametype(p: dict, entities: list[dict]) -> list[dict]:
    cls = str(p.get("classname", ""))
    gt_key = str(p.get("gametype_key", ""))
    gt = str(p.get("gametype", ""))
    out = []
    for e in _of_class(entities, cls):
        v = _keys(e).get(gt_key)
        if v is not None and gt in _gametype_list(v):
            out.append(e)
    return out


def _check_distinct_group_per_team(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    """No group value may be shared by two different teams.

    This is the corrected Team Survivor rule. The design document said TS needs *dedicated
    spawn entities* and could not share a gametype key — which the gamepack contradicts. What
    it actually requires is that each team's spawns sit in *separate groups*.
    """
    group_key = str(p.get("group_key", ""))
    team_key = str(p.get("team_key", ""))
    by_group: dict[str, set[str]] = {}
    index_by_group: dict[str, list[int]] = {}
    for e in _selected_by_gametype(p, entities):
        k = _keys(e)
        g = k.get(group_key)
        t = k.get(team_key)
        if g is None or t is None:
            continue
        by_group.setdefault(g, set()).add(t)
        index_by_group.setdefault(g, []).append(e["index"])

    shared = {g: sorted(ts) for g, ts in by_group.items() if len(ts) > 1}
    bad_entities = [i for g in shared for i in index_by_group[g]]
    return not shared, {
        "found": len(shared),
        "detail": shared,
        "entities": bad_entities,
        "expected": "one team per group",
    }


def _check_forbid_gametype(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    matches = _selected_by_gametype(p, entities)
    return not matches, {
        "found": len(matches),
        "classname": str(p.get("classname", "")),
        "entities": [e["index"] for e in matches],
        "expected": f"not listed for gametype {p.get('gametype')}",
    }


def _groups(p: dict, entities: list[dict]) -> dict[str, list[dict]]:
    cls = str(p.get("classname", ""))
    group_key = str(p.get("group_key", ""))
    out: dict[str, list[dict]] = {}
    for e in _of_class(entities, cls):
        g = _keys(e).get(group_key)
        if g is not None:
            out.setdefault(g, []).append(e)
    return out


def parse_spawnflags(value: str | None) -> int:
    """A spawnflags value as the engine reads it (`atoi`): a leading integer, else 0."""
    m = re.match(r"\s*([+-]?\d+)", value or "")
    return int(m.group(1)) if m else 0


def spawnflags(entity: dict) -> int:
    return parse_spawnflags(_keys(entity).get("spawnflags"))


def _check_flag_count_min(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    flag = int(p.get("flag", 0))
    want = int(p.get("count", 1))
    members = _of_class(entities, cls)
    flagged = [e for e in members if spawnflags(e) & flag]
    return len(flagged) >= want, {
        "found": len(flagged),
        "of": len(members),
        "expected": want,
        "classname": cls,
        "flag": flag,
        "entities": [e["index"] for e in members][:64],
    }


def _check_flag_requires_key(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    cls = str(p.get("classname", ""))
    flag = int(p.get("flag", 0))
    # Several key names can fill the same field; any one of them satisfies the rule.
    keys = [str(k) for k in p.get("keys") or [p.get("key", "")]]
    missing = [
        e["index"]
        for e in _of_class(entities, cls)
        if spawnflags(e) & flag and not any(_keys(e).get(k, "").strip() for k in keys)
    ]
    return not missing, {
        "found": len(missing),
        "classname": cls,
        "flag": flag,
        "key": " or ".join(keys),
        "entities": missing,
    }


def _check_targets_resolve(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    """References by name that name nothing.

    The engine looks a target up when it fires, not when the map loads, so a dangling name is
    silent until the moment it was meant to do something.
    """
    name_key = str(p.get("targetname_key", "targetname"))
    names = {v for e in entities if (v := _keys(e).get(name_key, "").strip())}
    single = [str(k) for k in p.get("keys") or []]
    lists = [str(k) for k in p.get("list_keys") or []]
    sep = str(p.get("separator", ";"))
    cls = p.get("classname")
    # Classes whose key of the same name means something else, such as a script event label.
    excluded = {str(c) for c in p.get("exclude_classnames") or []}
    dangling: list[tuple[int, str, str]] = []
    for e in entities:
        if (cls and e.get("classname") != cls) or e.get("classname") in excluded:
            continue
        k = _keys(e)
        for key in single + lists:
            raw = k.get(key, "")
            values = raw.split(sep) if key in lists else [raw]
            for v in (v.strip() for v in values):
                if v and v not in names:
                    dangling.append((e["index"], key, v))
    return not dangling, {
        "found": len(dangling),
        "detail": [f"entity {i} {key} {v!r}" for i, key, v in dangling[:8]],
        "entities": sorted({i for i, _, _ in dangling}),
    }


def _check_group_size(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    want = int(p.get("count", 0))
    sizes = {g: len(es) for g, es in _groups(p, entities).items()}
    wrong = {g: n for g, n in sizes.items() if n != want}
    return not wrong, {
        "found": len(wrong),
        "expected": want,
        "detail": wrong,
        "entities": [],
    }


def _check_min_spacing(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    want = float(p.get("distance", 0))
    close: list[tuple[int, int, float]] = []
    for _, members in _groups(p, entities).items():
        pts = [(e["index"], _origin(e)) for e in members]
        pts = [(i, o) for i, o in pts if o is not None]
        for a in range(len(pts)):
            for b in range(a + 1, len(pts)):
                (ia, oa), (ib, ob) = pts[a], pts[b]
                d = math.dist(oa, ob)
                if d < want:
                    close.append((ia, ib, round(d, 2)))
    return not close, {
        "found": len(close),
        "expected": want,
        "detail": [f"{a} and {b} are {d} apart" for a, b, d in close[:8]],
        "entities": sorted({i for a, b, _ in close for i in (a, b)}),
    }


def _flags(e: dict) -> int:
    return parse_spawnflags(_keys(e).get("spawnflags"))


def _check_flags_any(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    want = [int(f) for f in p.get("flags") or []]
    bad = [
        e
        for e in _of_class(entities, str(p.get("classname", "")))
        if not any(_flags(e) & f for f in want)
    ]
    return not bad, {
        "found": len(bad),
        "expected": " or ".join(str(f) for f in want),
        "entities": [e["index"] for e in bad],
    }


def _check_targeted_by(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    name_key = str(p.get("name_key", "targetname"))
    by_key = str(p.get("by_key", "target"))
    all_of = [int(f) for f in p.get("when_flags_all") or []]
    none_of = [int(f) for f in p.get("when_flags_none") or []]
    named = {
        v.lower()
        for e in _of_class(entities, str(p.get("by_classname", "")))
        if (v := _keys(e).get(by_key, "").strip())
    }
    bad = []
    for e in _of_class(entities, str(p.get("classname", ""))):
        f = _flags(e)
        if not all(f & x for x in all_of) or any(f & x for x in none_of):
            continue
        if _keys(e).get(name_key, "").strip().lower() not in named:
            bad.append(e)
    return not bad, {"found": len(bad), "entities": [e["index"] for e in bad]}


def _list_entries(raw: str, sep: str) -> list[str]:
    """Entries the engine reads: only those a separator ends (g_mover.c:5366-5392)."""
    parts = raw.split(sep)
    return [x.strip() for x in parts[:-1] if x.strip()]


def _by_name(entities: list[dict], name_key: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in entities:
        v = _keys(e).get(name_key, "").strip()
        if v:
            out.setdefault(v.lower(), e)
    return out


def _check_list_targets(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    sep = str(p.get("separator", ";"))
    classes = {str(c) for c in p.get("target_classes") or []}
    names = _by_name(entities, str(p.get("name_key", "targetname")))
    bad: list[tuple[int, str, str, str]] = []
    for e in _of_class(entities, str(p.get("classname", ""))):
        for key in p.get("keys") or []:
            for entry in _list_entries(_keys(e).get(str(key), ""), sep):
                hit = names.get(entry.lower())
                if hit is not None and hit.get("classname") not in classes:
                    bad.append((e["index"], str(key), entry, str(hit.get("classname"))))
    return not bad, {
        "found": len(bad),
        "expected": " or ".join(sorted(classes)),
        "detail": [f"entity {i} {k} {v!r} is a {c}" for i, k, v, c in bad[:8]],
        "entities": sorted({i for i, *_ in bad}),
    }


def _check_list_counts_match(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    sep = str(p.get("separator", ";"))
    first, second = (str(k) for k in p.get("keys") or ["", ""])
    names = _by_name(entities, str(p.get("name_key", "targetname")))
    bad: list[tuple[int, int, int]] = []
    for e in _of_class(entities, str(p.get("classname", ""))):
        k = _keys(e)
        if not k.get(first) or not k.get(second):
            continue
        a = [x for x in _list_entries(k[first], sep) if x.lower() in names]
        b = [x for x in _list_entries(k[second], sep) if x.lower() in names]
        if len(a) != len(b):
            bad.append((e["index"], len(a), len(b)))
    return not bad, {
        "found": len(bad),
        "detail": [f"entity {i}: {first} {a}, {second} {b}" for i, a, b in bad[:8]],
        "entities": [i for i, *_ in bad],
    }


def _check_key_pattern(p: dict, entities: list[dict]) -> tuple[bool, dict]:
    key = str(p.get("key", ""))
    pattern = re.compile(str(p.get("pattern", "")))
    bad = [
        (e["index"], v)
        for e in _of_class(entities, str(p.get("classname", "")))
        if (v := _keys(e).get(key)) is not None and not pattern.fullmatch(v)
    ]
    return not bad, {
        "found": len(bad),
        "detail": [f"entity {i} {key} {v!r}" for i, v in bad[:8]],
        "entities": [i for i, _ in bad],
    }


CHECKS = {
    "require_any": lambda p, e: _check_require_any(p, e),
    "count_exact": lambda p, e: _check_count(p, e, "exact"),
    "count_min": lambda p, e: _check_count(p, e, "min"),
    "count_max": lambda p, e: _check_count(p, e, "max"),
    "key_in_enum": lambda p, e: _check_key_in_enum(p, e),
    "key_required": lambda p, e: _check_key_required(p, e),
    "distinct_group_per_team": lambda p, e: _check_distinct_group_per_team(p, e),
    "forbid_gametype": lambda p, e: _check_forbid_gametype(p, e),
    "group_size": lambda p, e: _check_group_size(p, e),
    "min_spacing": lambda p, e: _check_min_spacing(p, e),
    "flag_count_min": lambda p, e: _check_flag_count_min(p, e),
    "flag_requires_key": lambda p, e: _check_flag_requires_key(p, e),
    "targets_resolve": lambda p, e: _check_targets_resolve(p, e),
    "flags_any": lambda p, e: _check_flags_any(p, e),
    "targeted_by": lambda p, e: _check_targeted_by(p, e),
    "list_targets": lambda p, e: _check_list_targets(p, e),
    "list_counts_match": lambda p, e: _check_list_counts_match(p, e),
    "key_pattern": lambda p, e: _check_key_pattern(p, e),
}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


def _effective_severity(rule: dict) -> str:
    """Severity after the confidence clamp.

    An unverified rule can never fail a build. §7 requires it, and the alternative has already
    cost this project once.
    """
    asked = str(rule.get("severity", "warning")).lower()
    if asked not in SEVERITIES:
        asked = "warning"
    if str(rule.get("confidence", "unverified")).lower() != "verified":
        return "info"
    return asked


def _format(template: str, facts: dict) -> str:
    try:
        return template.format(**facts)
    except (KeyError, IndexError, ValueError):
        # A profile typo in a message template must not break validation. Report the
        # template plus the facts so the mistake is obvious and fixable.
        return f"{template}  (facts: {facts})"


def evaluate(profile_id: str, entities: list[dict]) -> list[Finding]:
    """Run every rule in the profile against a map's entities."""
    data = profiles.load(profile_id)
    rules = data.get("rules")
    findings: list[Finding] = []
    if not isinstance(rules, list):
        return findings

    for rule in rules:
        if not isinstance(rule, dict):
            continue
        kind = str(rule.get("check", ""))
        fn = CHECKS.get(kind)
        code = str(rule.get("id", f"RULE_{kind.upper()}"))
        if fn is None:
            findings.append(
                Finding(
                    code="RULE_UNKNOWN_CHECK",
                    severity="info",
                    message=(
                        f"rule {code} asks for check {kind!r}, which this engine does not "
                        f"implement; known checks: {', '.join(sorted(CHECKS))}"
                    ),
                    confidence="verified",
                    rule_source="nrc_mcp.rules",
                )
            )
            continue

        params = rule.get("params") if isinstance(rule.get("params"), dict) else {}
        if not _applies(rule, entities):
            continue
        try:
            ok, facts = fn(params, entities)
        except (TypeError, ValueError) as e:
            findings.append(
                Finding(
                    code="RULE_MALFORMED",
                    severity="info",
                    message=f"rule {code} could not be evaluated: {e}",
                    confidence="verified",
                    rule_source="nrc_mcp.rules",
                )
            )
            continue
        if ok:
            continue

        findings.append(
            Finding(
                code=code,
                severity=_effective_severity(rule),
                message=_format(str(rule.get("message", code)), facts),
                confidence=str(rule.get("confidence", "unverified")),
                rule_source=str(rule.get("rule_source", f"profile {profile_id}")),
                entities=[int(i) for i in facts.get("entities") or []][:64],
                fix_hint=str(rule.get("fix_hint", "")),
            )
        )
    return findings


def unknown_classnames(profile_id: str, entities: list[dict]) -> list[Finding]:
    """Entities whose classname the profile does not define.

    A typo in a classname is silent in game — the entity simply does nothing — so it is worth
    reporting. Never an error: a map may legitimately use an entity from a newer game build
    than the profile was extracted from, and the profile, not the map, would be at fault.
    """
    known = {str(e.get("classname")) for e in profiles.entities(profile_id)}
    measurement = profiles.load(profile_id).get("measurement_entities") or []
    if isinstance(measurement, list):
        known |= {str(m.get("classname")) for m in measurement if isinstance(m, dict)}

    unknown: dict[str, list[int]] = {}
    for e in entities:
        cn = e.get("classname") or ""
        if cn and cn not in known:
            unknown.setdefault(cn, []).append(e["index"])

    return [
        Finding(
            code="ENTITY_CLASSNAME_UNKNOWN",
            severity="warning",
            message=(
                f"{len(idx)} entity/entities use classname {cn!r}, which profile "
                f"{profile_id} does not define — a typo here is silent in game, but the "
                f"profile may simply be older than the map"
            ),
            confidence="verified",
            rule_source=f"profile {profile_id} entity list",
            entities=idx[:64],
        )
        for cn, idx in sorted(unknown.items())
    ]


def summarize(findings: list[Finding]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f.severity == s) for s in SEVERITIES}
