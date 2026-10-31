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

from . import profiles
from .rules import Finding


class ScriptSyntaxError(ValueError):
    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line


@dataclass
class Token:
    text: str
    line: int


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


@dataclass
class Block:
    name: str
    line: int
    events: list[Event] = field(default_factory=list)

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
            out.append(Token(text[i + 1 : end], line))
            line += text.count("\n", i, end)
            i = end + 1
        else:
            start = i
            while i < n and not text[i].isspace():
                i += 1
            out.append(Token(text[start:i], line))
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
        if t.text.lower() == "entity":
            t = take("a name")
        block = Block(t.text, t.line)
        if not brace(take("'{'"), "{"):
            raise ScriptSyntaxError(f"'{{' expected after {block.name!r}", t.line)
        while True:
            t = take("'}'")
            if brace(t, "}"):
                break
            event = Event(t.text.lower(), [], t.line)
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
    return asked if confidence == "verified" else "info"


def script_path(spec: dict, map_path: str | Path) -> Path:
    """Where the script for this `.map` lives: beside it, named from the profile's pattern."""
    p = Path(map_path)
    return p.with_name(str(spec.get("file", "{map}.script")).format(map=p.stem))


def check(profile_id: str, entities: list[dict], map_path: str | Path | None) -> list[Finding]:
    """Findings for the open map's script. Nothing at all if the profile has no `mapscript:`."""
    spec = profiles.load(profile_id).get("mapscript")
    if not isinstance(spec, dict) or map_path is None:
        return []
    conf = str(spec.get("confidence", "unverified"))
    source = f"profile {profile_id} mapscript ({spec.get('locus', '')})".replace(" ()", "")
    path = script_path(spec, map_path)

    def finding(code: str, asked: str, message: str, ents=(), hint: str = "", c: str = conf):
        listed = [i for i in ents if i >= 0][:64]
        return Finding(code, _severity(asked, c), message, c, source, listed, hint)

    if not path.is_file():
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
            path.read_text(errors="replace"),
            frozenset(str(a).lower() for a in spec.get("brace_actions") or []),
        )
    except ScriptSyntaxError as e:
        return [finding("MAPSCRIPT_SYNTAX", "error", f"{path.name}: {e}; the game stops at load")]

    # Entities the script spawns itself are as real as the map's own, for names and blocks.
    # Index -1 marks them; they are never listed in a finding.
    entities = entities + _spawned(spec, blocks)
    key = str(spec.get("block_key", "scriptname")).lower()
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

    out += _references(spec, blocks, entities, by_name, path.name, finding)
    out += _entity_events(spec, entities, by_name, path.name, finding)
    return out


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


def _references(spec, blocks, entities, by_name, filename, finding) -> list[Finding]:
    """One finding per name that resolves to nothing, listing every line that uses it."""
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
        for b in blocks:
            for ev in b.events:
                for act in ev.actions:
                    if act.name != name or len(act.args) <= arg:
                        continue
                    value = act.args[arg]
                    if not ok(value):
                        unresolved.setdefault(value, []).append(act.line)
                        continue
                    target = by_name.get(value.lower())
                    if label_arg is None or target is None or len(act.args) <= int(label_arg):
                        continue
                    label = act.args[int(label_arg)]
                    if not target.has_event(event, [label]):
                        no_event.setdefault((target.name, label), []).append(act.line)
        for value, lines in unresolved.items():
            out.append(
                finding(
                    "MAPSCRIPT_UNRESOLVED_REFERENCE",
                    sev,
                    f"{filename} line {_lines(lines)}: {name} {value!r} names nothing "
                    f"({rule.get('describe', rule.get('resolves'))}); "
                    f"{rule.get('consequence', '')}".rstrip("; "),
                    c=conf,
                )
            )
        for (block, label), lines in no_event.items():
            out.append(
                finding(
                    "MAPSCRIPT_MISSING_EVENT",
                    sev,
                    f"{filename} line {_lines(lines)}: {name} {block} {label} - block "
                    f"{block!r} has no '{event} {label}' event, so nothing happens",
                    c=conf,
                )
            )
    return out
