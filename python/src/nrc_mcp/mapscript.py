"""Map scripts: the text file some games pair with a `.map` to drive its gameplay.

A profile that has one describes it under `mapscript:` — where the file sits, which entity key
names a block, which events and actions the engine accepts, and which action arguments name
something in the map. This module parses the file and checks it against the map's entities.
It knows the *shape* of such a script and nothing else: every word it checks against comes
from the profile, which `tools/seam_lint.py` enforces.

The shape, as the engine parses it:

    [entity] <name> {
        <event> [params...] {
            <action> [args to the end of the line]
            <brace action> { args... }
        }
    }

Tokens split on whitespace, `"quoted strings"` are one token, and `//` and `/* */` are
comments. Action arguments run to the end of the line, so line numbers matter.

A script is only half of the contract. A block runs for the entity whose name key matches it,
a reference runs only when its action does, and the engine reports most of these mistakes only
at the moment they happen — which is why they are worth finding before the map ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import profiles, script_lifetime
from .rules import Finding, clamp_severity, parse_spawnflags


class ScriptSyntaxError(ValueError):
    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass
class Token:
    text: str
    line: int
    #: Character offsets into the text, end exclusive (a quoted token includes its quotes).
    start: int = 0
    end: int = 0


@dataclass
class Action:
    name: str
    args: list[str]
    line: int


@dataclass
class Event:
    name: str
    params: list[str]
    line: int
    actions: list[Action] = field(default_factory=list)
    #: Offsets of the event's name and of the end of its closing brace, for editing in place.
    start: int = 0
    end: int = 0


@dataclass
class Block:
    name: str
    line: int
    events: list[Event] = field(default_factory=list)
    #: Offsets of the block's first token, of its closing brace, and of that brace's end.
    start: int = 0
    close: int = 0
    end: int = 0

    def has_event(self, name: str, params: list[str] | None = None) -> bool:
        want = [p.lower() for p in params] if params is not None else None
        return any(
            e.name == name and (want is None or [p.lower() for p in e.params] == want)
            for e in self.events
        )


def tokenize(text: str) -> list[Token]:
    """Split a script the way the engine's tokenizer does."""
    out: list[Token] = []
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
            end = text.find('"', i + 1)
            end = n if end < 0 else end
            out.append(Token(text[i + 1 : end], line, i, min(end + 1, n)))
            line += text.count("\n", i, end)
            i = end + 1
        else:
            start = i
            while i < n and not text[i].isspace():
                i += 1
            out.append(Token(text[start:i], line, start, i))
    return out


def parse(text: str, brace_actions: frozenset[str] = frozenset()) -> list[Block]:
    """Parse a script into blocks, or raise `ScriptSyntaxError` where the engine would stop."""
    tokens = tokenize(text)
    pos = 0

    def take(what: str) -> Token:
        nonlocal pos
        if pos >= len(tokens):
            last = tokens[-1].line if tokens else 1
            raise ScriptSyntaxError(f"end of script found, {what} expected", last)
        t = tokens[pos]
        pos += 1
        return t

    def brace(t: Token, ch: str) -> bool:
        # The engine tests only a token's first character, so `}ll` closes a block. A
        # shipped map relies on exactly that.
        return t.text[:1] == ch

    blocks: list[Block] = []
    while pos < len(tokens):
        t = take("a name")
        if brace(t, "}"):
            raise ScriptSyntaxError("'}' found, but not expected", t.line)
        if brace(t, "{"):
            raise ScriptSyntaxError("'{' found, a name expected", t.line)
        first = t
        if t.text.lower() == "entity":
            t = take("a name")
        block = Block(t.text, t.line, start=first.start)
        if not brace(take("'{'"), "{"):
            raise ScriptSyntaxError(f"'{{' expected after {block.name!r}", t.line)
        while True:
            t = take("'}'")
            if brace(t, "}"):
                block.close, block.end = t.start, t.end
                break
            event = Event(t.text.lower(), [], t.line, start=t.start)
            while not brace(t := take("'{'"), "{"):
                event.params.append(t.text)
            while not brace(t := take("'}'"), "}"):
                action = Action(t.text.lower(), [], t.line)
                if action.name in brace_actions:
                    if not brace(take("'{'"), "{"):
                        raise ScriptSyntaxError(f"'{{' expected after {action.name}", t.line)
                    while not brace(a := take("'}'"), "}"):
                        action.args.append(a.text)
                else:
                    while pos < len(tokens) and tokens[pos].line == action.line:
                        action.args.append(tokens[pos].text)
                        pos += 1
                event.actions.append(action)
            event.end = t.end
            block.events.append(event)
        blocks.append(block)
    return blocks


# ---------------------------------------------------------------------------
# Checking against the map
# ---------------------------------------------------------------------------


def _keys(entity: dict) -> dict[str, str]:
    """Entity keys by lowercased name: the engine matches key names case-insensitively."""
    out: dict[str, str] = {}
    for k, v in entity.get("keys") or []:
        out.setdefault(str(k).lower(), v)
    return out


def _severity(asked: str, confidence: str) -> str:
    return clamp_severity(asked, confidence)


def _stated(spec: dict, key: str) -> str:
    """A naming convention the profile must state: code has no default for it.

    The script's file name and the key that names a block are the game's conventions, so a
    default here would be one game's vocabulary living in code (§7.4).
    """
    value = spec.get(key)
    if not isinstance(value, str) or not value:
        raise profiles.ProfileError(f"the profile's mapscript section must state `{key}`")
    return value


def block_key(spec: dict) -> str:
    """The entity key whose value names the script block an entity runs, lowercased."""
    return _stated(spec, "block_key").lower()


def script_path(spec: dict, map_path: str | Path) -> Path:
    """Where the script for this `.map` lives: beside it, named from the profile's pattern."""
    p = Path(map_path)
    return p.with_name(_stated(spec, "file").format(map=p.stem))


def brace_actions(spec: dict) -> frozenset[str]:
    return frozenset(str(a).lower() for a in spec.get("brace_actions") or [])


def names(spec: dict, entities: list[dict]) -> dict[str, list[int]]:
    """Block names the map's entities run, lowercased, with the entities that run each."""
    key = block_key(spec)
    named: dict[str, list[int]] = {}
    for e in entities:
        v = _keys(e).get(key, "").strip()
        if v:
            named.setdefault(v.lower(), []).append(int(e["index"]))
    # Classes the engine names itself, whatever their key says.
    for implied in spec.get("implied_names") or []:
        if isinstance(implied, dict):
            for e in entities:
                if e.get("classname") == implied.get("classname"):
                    named.setdefault(str(implied.get("name", "")).lower(), []).append(
                        int(e["index"])
                    )
    return named


def check(
    profile_id: str,
    entities: list[dict],
    map_path: str | Path | None,
    text: str | None = None,
) -> list[Finding]:
    """Findings for the open map's script. Nothing at all if the profile has no `mapscript:`.

    `text` checks that script instead of the file on disk, for an edit not yet written.
    """
    spec = profiles.load(profile_id).get("mapscript")
    if not isinstance(spec, dict) or map_path is None:
        return []
    conf = str(spec.get("confidence", "unverified"))
    source = f"profile {profile_id} mapscript ({spec.get('locus', '')})".replace(" ()", "")
    path = script_path(spec, map_path)

    def finding(code: str, asked: str, message: str, ents=(), hint: str = "", c: str = conf):
        listed = [i for i in ents if i >= 0][:64]
        return Finding(code, _severity(asked, c), message, c, source, listed, hint)

    if text is None and not path.is_file():
        return [
            finding(
                "MAPSCRIPT_MISSING",
                str(spec.get("missing_severity", "warning")),
                f"no map script at {path.name} beside the map; {spec.get('missing_note', '')}",
                hint=f"create {path.name} next to the .map",
            )
        ]

    try:
        blocks = parse(
            path.read_text(errors="replace") if text is None else text,
            brace_actions(spec),
        )
    except ScriptSyntaxError as e:
        return [finding("MAPSCRIPT_SYNTAX", "error", f"{path.name}: {e}; the game stops at load")]

    # Entities the script spawns itself are as real as the map's own, for names and blocks.
    # Index -1 marks them; they are never listed in a finding.
    entities = entities + _spawned(spec, blocks)
    key = block_key(spec)
    named = names(spec, entities)
    by_name: dict[str, Block] = {}
    out: list[Finding] = []
    for b in blocks:
        if b.name.lower() in by_name:
            out.append(
                finding(
                    "MAPSCRIPT_DUPLICATE_BLOCK",
                    "warning",
                    f"{path.name}:{b.line}: a second block named {b.name!r}; only the first runs",
                )
            )
        else:
            by_name[b.name.lower()] = b

    events = {str(x).lower() for x in spec.get("events") or []}
    actions = {str(x).lower() for x in spec.get("actions") or []}
    for b in by_name.values():
        live = b.name.lower() in named
        if not live:
            out.append(
                finding(
                    "MAPSCRIPT_BLOCK_WITHOUT_ENTITY",
                    "warning",
                    f"{path.name}:{b.line}: block {b.name!r} names no entity's {key}, so it "
                    "never runs",
                    hint=f"set {key} {b.name!r} on the entity it was written for, or remove it",
                )
            )
        for ev in b.events:
            if events and ev.name not in events:
                out.append(
                    finding(
                        "MAPSCRIPT_UNKNOWN_EVENT",
                        "error" if live else "warning",
                        f"{path.name}:{ev.line}: unknown event {ev.name!r} in block {b.name!r}"
                        + ("; the game stops at load" if live else ""),
                        named.get(b.name.lower(), []),
                    )
                )
            for act in ev.actions:
                if actions and act.name not in actions:
                    out.append(
                        finding(
                            "MAPSCRIPT_UNKNOWN_ACTION",
                            "error" if live else "warning",
                            f"{path.name}:{act.line}: unknown action {act.name!r} in block "
                            f"{b.name!r}" + ("; the game stops at load" if live else ""),
                            named.get(b.name.lower(), []),
                        )
                    )

    live = {n for n in by_name if n in named}
    out += _references(spec, live, entities, by_name, path.name, finding)
    out += _entity_events(spec, entities, by_name, path.name, finding)
    out += _arg_ranges(spec, live, by_name, path.name, finding)
    out += _required_actions(spec, live, by_name, path.name, finding)
    out += _objective_requirements(spec, entities, by_name, finding)
    out += script_lifetime.check(spec, entities, by_name, path.name)
    return out


def _arg_ranges(spec, live, by_name, filename, finding) -> list[Finding]:
    """Numeric action arguments the engine refuses outside a range, when the action runs."""
    out: list[Finding] = []
    for rule in spec.get("arg_ranges") or []:
        if not isinstance(rule, dict):
            continue
        name, arg = str(rule.get("action", "")).lower(), int(rule.get("arg", 0))
        lo, hi = float(rule.get("min", float("-inf"))), float(rule.get("max", float("inf")))
        for b in by_name.values():
            for ev in b.events:
                for act in ev.actions:
                    if act.name != name or len(act.args) <= arg:
                        continue
                    try:
                        v = float(act.args[arg])
                    except ValueError:
                        v = 0.0  # the engine reads it with atoi: anything else is 0
                    if lo <= v <= hi:
                        continue
                    sev = str(rule.get("severity", "warning"))
                    out.append(
                        finding(
                            "MAPSCRIPT_ARGUMENT_RANGE",
                            sev if b.name.lower() in live else _milder(sev),
                            f"{filename} line {act.line}: {name} {act.args[arg]!r} is outside "
                            f"{rule.get('min', '')}..{rule.get('max', '')}; "
                            f"{rule.get('consequence', '')}".rstrip("; "),
                        )
                    )
    return out


def _required_actions(spec, live, by_name, filename, finding) -> list[Finding]:
    """Actions a running block should call, such as a win condition in the manager's spawn."""
    out: list[Finding] = []
    for rule in spec.get("required_actions") or []:
        if not isinstance(rule, dict):
            continue
        block = str(rule.get("block", "")).lower()
        b = by_name.get(block)
        if b is None or block not in live:
            continue  # a missing or dead block is reported elsewhere
        event = str(rule.get("event", "")).lower()
        action = str(rule.get("action", "")).lower()
        if any(e.name == event and any(a.name == action for a in e.actions) for e in b.events):
            continue
        out.append(
            finding(
                "MAPSCRIPT_REQUIRED_ACTION",
                str(rule.get("severity", "info")),
                f"{filename}: block {b.name!r} never calls {action} in its {event} event; "
                f"{rule.get('consequence', '')}".rstrip("; "),
                hint=str(rule.get("fix_hint", "")),
            )
        )
    return out


def _objective_requirements(spec, entities, by_name, finding) -> list[Finding]:
    """An objective entity that needs a key value - or a script action setting it - to work.

    For example a dynamite target whose weapon class is never raised: dynamite can be planted
    at it and counts down, and the objective survives.
    """
    out: list[Finding] = []
    bkey = block_key(spec)
    for rule in spec.get("objective_requirements") or []:
        if not isinstance(rule, dict):
            continue
        by = rule.get("targeted_by") or {}
        flags_any = [int(f) for f in by.get("flags_any") or []]
        wanted = set()
        for e in entities:
            if e.get("classname") != by.get("classname"):
                continue
            k = _keys(e)
            if flags_any and not any(parse_spawnflags(k.get("spawnflags")) & f for f in flags_any):
                continue
            if v := k.get(str(by.get("key", "target")).lower(), "").strip():
                wanted.add(v.lower())
        for e in entities:
            k = _keys(e)
            if (
                e.get("classname") != rule.get("classname")
                or k.get("targetname", "").strip().lower() not in wanted
            ):
                continue
            if any(
                _int(k.get(str(key).lower())) >= int(low)
                for key, low in (rule.get("key_min") or {}).items()
            ):
                continue
            action = str(rule.get("action", "")).lower()
            block = by_name.get(k.get(bkey, "").strip().lower())
            if (
                action
                and block is not None
                and any(
                    a.name == action
                    and a.args
                    and _int(a.args[0]) >= int(rule.get("action_min", 0))
                    for ev in block.events
                    for a in ev.actions
                )
            ):
                continue
            out.append(
                finding(
                    "MAPSCRIPT_OBJECTIVE_UNMET",
                    str(rule.get("severity", "warning")),
                    f"{e.get('classname')} {k.get('targetname')!r} (entity {e['index']}): "
                    f"{rule.get('message', 'a requirement is not met')}",
                    [int(e["index"])],
                    hint=str(rule.get("fix_hint", "")),
                )
            )
    return out


def _int(value) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def _spawned(spec: dict, blocks: list[Block]) -> list[dict]:
    """Entities created by the script's own spawning actions, as key/value pairs."""
    spawning = {str(a).lower() for a in spec.get("spawning_actions") or []}
    out: list[dict] = []
    for b in blocks:
        for ev in b.events:
            for act in ev.actions:
                if act.name not in spawning:
                    continue
                pairs = list(zip(act.args[0::2], act.args[1::2], strict=False))
                cls = next((v for k, v in pairs if k.lower() == "classname"), "")
                out.append({"index": -1, "classname": cls, "keys": pairs})
    return out


def _entity_events(spec, entities, by_name, filename, finding) -> list[Finding]:
    """Entities that call a script event by label: the block they name must have that event."""
    out: list[Finding] = []
    for rule in spec.get("entity_events") or []:
        if not isinstance(rule, dict):
            continue
        conf = str(rule.get("confidence", spec.get("confidence", "unverified")))
        sev = str(rule.get("severity", "warning"))
        event = str(rule.get("event", "")).lower()
        block_keys = [str(k).lower() for k in rule.get("block_keys") or []]
        # A label that is also an entity's name is the entity being used as a relay, which
        # the engine does as well (`also_targets`), so only a label matching neither is lost.
        relay_key = str(rule.get("also_targets", "")).lower()
        relayed = {v for e in entities if relay_key and (v := _keys(e).get(relay_key, "").strip())}
        missing: dict[tuple[str, str], list[int]] = {}
        for e in entities:
            if e.get("classname") != rule.get("classname"):
                continue
            k = _keys(e)
            label = k.get(str(rule.get("label_key", "")).lower(), "").strip()
            # The engine tries each key in turn and falls back when one names no block.
            names = [k[b].strip() for b in block_keys if k.get(b, "").strip()]
            block = next((n for n in names if n.lower() in by_name), names[0] if names else "")
            if not label or not block or label in relayed:
                continue
            target = by_name.get(block.lower())
            if target is None or not target.has_event(event, [label]):
                missing.setdefault((block, label), []).append(int(e["index"]))
        for (block, label), idx in missing.items():
            why = (
                "has no such block"
                if block.lower() not in by_name
                else (f"block {block!r} has no '{event} {label}' event")
            )
            out.append(
                finding(
                    "MAPSCRIPT_MISSING_EVENT",
                    sev,
                    f"{len(idx)} {rule.get('classname')} call '{event} {label}' on {block!r}, "
                    f"but {filename} {why} and no entity is named {label!r}, so nothing happens",
                    idx,
                    c=conf,
                )
            )
    return out


def _resolver(rule: dict, entities: list[dict], by_name: dict[str, Block]):
    """A predicate saying whether an argument names what this reference rule expects."""
    special = {str(s).lower() for s in rule.get("special") or []}
    target = rule.get("resolves")
    if target == "block":
        return lambda v: v.lower() in special or v.lower() in by_name
    wanted: set[str] = set()
    for r in target if isinstance(target, list) else [{"key": target}]:
        cls = r.get("classname")
        for e in entities:
            if cls and e.get("classname") != cls:
                continue
            v = _keys(e).get(str(r.get("key", "")).lower(), "").strip()
            if v:
                wanted.add(v.lower() if rule.get("case_insensitive") else v)
    fold = bool(rule.get("case_insensitive"))
    return lambda v: v.lower() in special or (v.lower() if fold else v) in wanted


def _lines(lines: list[int]) -> str:
    shown = ", ".join(str(n) for n in lines[:6])
    return shown + (f" (+{len(lines) - 6} more)" if len(lines) > 6 else "")


def _milder(severity: str) -> str:
    return {"error": "warning", "warning": "info"}.get(severity, severity)


def _references(spec, live, entities, by_name, filename, finding) -> list[Finding]:
    """One finding per name that resolves to nothing, listing every line that uses it.

    Only the first block of each name is read, as the engine does. A name used only in blocks
    that never run cannot fail the game, so it is reported one step milder.
    """
    out: list[Finding] = []
    for rule in spec.get("references") or []:
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("action", "")).lower()
        arg = int(rule.get("arg", 0))
        ok = _resolver(rule, entities, by_name)
        conf = str(rule.get("confidence", spec.get("confidence", "unverified")))
        sev = str(rule.get("severity", "warning"))
        label_arg = rule.get("event_label_arg")
        event = str(rule.get("event", "")).lower()
        unresolved: dict[str, list[int]] = {}
        no_event: dict[tuple[str, str], list[int]] = {}
        runs: set[str | tuple[str, str]] = set()  # names used somewhere that runs
        for b in by_name.values():
            for ev in b.events:
                for act in ev.actions:
                    if act.name != name or len(act.args) <= arg:
                        continue
                    value = act.args[arg]
                    # An old syntax the engine takes a number in and ignores on purpose.
                    if rule.get("skip_numeric") and value.lstrip("-").isdigit():
                        continue
                    if not ok(value):
                        unresolved.setdefault(value, []).append(act.line)
                        if b.name.lower() in live:
                            runs.add(value)
                        continue
                    target = by_name.get(value.lower())
                    if label_arg is None or target is None or len(act.args) <= int(label_arg):
                        continue
                    label = act.args[int(label_arg)]
                    if not target.has_event(event, [label]):
                        no_event.setdefault((target.name, label), []).append(act.line)
                        if b.name.lower() in live:
                            runs.add((target.name, label))
        dead = "; only in blocks that never run"
        for value, lines in unresolved.items():
            out.append(
                finding(
                    "MAPSCRIPT_UNRESOLVED_REFERENCE",
                    sev if value in runs else _milder(sev),
                    f"{filename} line {_lines(sorted(lines))}: {name} {value!r} names nothing "
                    f"({rule.get('describe', rule.get('resolves'))}); "
                    f"{rule.get('consequence', '')}".rstrip("; ")
                    + ("" if value in runs else dead),
                    c=conf,
                )
            )
        for (block, label), lines in no_event.items():
            out.append(
                finding(
                    "MAPSCRIPT_MISSING_EVENT",
                    sev if (block, label) in runs else _milder(sev),
                    f"{filename} line {_lines(sorted(lines))}: {name} {block} {label} - block "
                    f"{block!r} has no '{event} {label}' event, so nothing happens"
                    + ("" if (block, label) in runs else dead),
                    c=conf,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------
#
# Edits are spliced into the text at the offsets the parser recorded, so everything outside the
# event or block being changed - comments, layout, other blocks - is left exactly as it was.


class ScriptEditError(ValueError):
    """An edit the engine could never accept, refused before anything is written."""


def _word(text: str, what: str) -> str:
    text = str(text).strip()
    if not text or any(c.isspace() or c in '{}"' for c in text) or "//" in text:
        raise ScriptEditError(f"{what} {text!r} must be one word, with no braces or quotes")
    return text


def validate_event(
    spec: dict, block: str, event: str, params: list[str], actions: list[str]
) -> None:
    """Refuse what the engine stops on: unknown events and actions, and malformed lines."""
    _word(block, "block name")
    events = {str(e).lower() for e in spec.get("events") or []}
    if events and str(event).lower() not in events:
        raise ScriptEditError(
            f"unknown event {event!r}; the profile lists: {', '.join(sorted(events))}"
        )
    for p in params:
        _word(p, "event parameter")
    known = {str(a).lower() for a in spec.get("actions") or []}
    braced = brace_actions(spec)
    for line in actions:
        line = str(line).strip()
        if not line or "\n" in line or "\r" in line:
            raise ScriptEditError(f"each action is one non-empty line, got {line!r}")
        name = line.split()[0].lower()
        if known and name not in known:
            raise ScriptEditError(f"unknown action {name!r} in {line!r}")
        rest = line[len(name) :].strip()
        if name in braced:
            if not (rest.startswith("{") and rest.endswith("}")) or rest.count("{") != 1:
                raise ScriptEditError(f"{name} takes its arguments as one {{ ... }} list: {line!r}")
        elif "{" in rest or "}" in rest:
            raise ScriptEditError(f"a brace in {line!r} would end the event early")
        if rest.count('"') % 2:
            raise ScriptEditError(f"unbalanced quote in {line!r}")


def render_event(event: str, params: list[str], actions: list[str], indent: str = "\t") -> str:
    head = " ".join([event.lower(), *params])
    body = "".join(f"{indent}\t{a.strip()}\n" for a in actions)
    return f"{head}\n{indent}{{\n{body}{indent}}}"


def _find(blocks: list[Block], block: str) -> Block | None:
    return next((b for b in blocks if b.name.lower() == block.lower()), None)


def _find_event(b: Block, event: str, params: list[str]) -> Event | None:
    want = [p.lower() for p in params]
    return next(
        (e for e in b.events if e.name == event.lower() and [p.lower() for p in e.params] == want),
        None,
    )


def _indent_at(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    lead = text[line_start:offset]
    return lead if lead.strip() == "" else ""


def set_event(
    text: str, spec: dict, block: str, event: str, params: list[str], actions: list[str]
) -> tuple[str, str]:
    """`text` with one event's actions set, and what was done: replaced, added or created."""
    validate_event(spec, block, event, params, actions)
    blocks = parse(text, brace_actions(spec))
    b = _find(blocks, block)
    if b is None:
        new_block = f"{block}\n{{\n\t{render_event(event, params, actions)}\n}}\n"
        if not text.strip():
            return new_block, "created block"
        return text.rstrip("\n") + "\n\n" + new_block, "created block"

    e = _find_event(b, event, params)
    if e is not None:
        rendered = render_event(event, params, actions, _indent_at(text, e.start) or "\t")
        return text[: e.start] + rendered + text[e.end :], "replaced event"

    # A new event goes last in the block, a blank line after the one before it.
    indent = (_indent_at(text, b.events[-1].start) if b.events else "") or "\t"
    rendered = f"{indent}{render_event(event, params, actions, indent)}\n"
    line_start = text.rfind("\n", 0, b.close) + 1
    if text[line_start : b.close].strip():  # the brace shares its line with other text
        line_start, rendered = b.close, "\n" + rendered
    if b.events:
        rendered = "\n" + rendered
    return text[:line_start] + rendered + text[line_start:], "added event"


def remove(
    text: str, spec: dict, block: str, event: str | None = None, params: list[str] | None = None
) -> str:
    """`text` without one event, or without a whole block when `event` is None."""
    blocks = parse(text, brace_actions(spec))
    b = _find(blocks, block)
    if b is None:
        raise ScriptEditError(f"no block named {block!r}")
    if event is None:
        start, end = b.start, b.end
    else:
        e = _find_event(b, event, params or [])
        if e is None:
            label = " ".join([event, *(params or [])])
            raise ScriptEditError(f"block {block!r} has no {label!r} event")
        start, end = e.start, e.end
    # Take the indentation before it and the line break after it, when they are its own.
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():
        start = line_start
    nl = text.find("\n", end)
    if nl >= 0 and not text[end:nl].strip():
        end = nl + 1
    # And one blank line, so removing a block or event leaves no gap of its own behind.
    if text[:start].endswith("\n\n"):
        start -= 1
    elif text[end:].startswith("\n"):
        end += 1
    return text[:start] + text[end:]
