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
from .rules import Finding, parse_spawnflags


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
    #: A line break the engine sees comes before it: one outside comments and quoted strings.
    newline: bool = False
    #: Written in quotes; the only way a token can be empty.
    quoted: bool = False


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


def tokenize(text: str, high_bytes_split: bool = False) -> list[Token]:
    """Split a script the way the engine's tokenizer does (`COM_ParseExt`).

    `text` holds the file's bytes one character each (see `decode`). Every character at or
    below a space is whitespace, `high_bytes_split` adds 0x80-0xff (a signed `char`), and a NUL
    ends the script. Only a line feed is a line break: a lone carriage return is whitespace. A
    line break inside a `/* */` comment or a quoted string is not one the engine sees, which
    is what `Token.newline` records. `\\"` inside a quoted string is the engine's nested
    string: it reads as a plain `"` and does not end the token.
    """
    text = text.split("\0", 1)[0]
    out: list[Token] = []
    i, n, line, newline = 0, len(text), 1, False

    def space(ch: str) -> bool:
        o = ord(ch)
        return o <= 32 or (high_bytes_split and o >= 128)

    while i < n:
        c = text[i]
        if space(c):
            if c == "\n":
                line += 1
                newline = True
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            line += text.count("\n", i, end)
            i = end
        elif c == '"':
            word, end = _quoted(text, i)
            out.append(Token(word, line, i, end, newline, quoted=True))
            line += text.count("\n", i, end)
            newline = False
            i = end
        else:
            start = i
            while i < n and not space(text[i]):
                i += 1
            out.append(Token(text[start:i], line, start, i, newline))
            newline = False
    return out


def _quoted(text: str, i: int) -> tuple[str, int]:
    """The quoted token opening at `i` and the offset after it, as `COM_ParseExt` reads it
    (qcommon/q_shared.c:705-759), including its nested `\\"...\\"` string."""
    n = len(text)

    def at(k: int) -> str:
        return text[k] if k < n else "\0"

    word: list[str] = []
    j = i + 1
    while True:
        c = at(j)
        j += 1
        if c == "\\" and at(j) == '"':
            word.append('"')
            j += 1
            while True:
                c = at(j)
                j += 1
                if c == "\0":
                    break
                if c == "\\" and at(j) == '"':
                    word.append('"')
                    j += 1
                    c = at(j)
                    j += 1
                    break
                word.append(c)
        if c in ('"', "\0"):
            return "".join(word), min(j, n)
        word.append(c)


def high_bytes_split(spec: dict) -> bool:
    """Whether the profile's engine reads bytes 0x80-0xff as whitespace (a signed `char`)."""
    return str((spec.get("tokenizer") or {}).get("high_bytes", "")).lower() == "whitespace"


def decode(data: bytes | None) -> str:
    """A script's bytes as text, one character per byte, so an edit writes back every byte it
    did not touch exactly as it was: line endings, any encoding, invalid sequences."""
    return (data or b"").decode("latin-1")


def encode(text: str) -> bytes:
    """The inverse of `decode`."""
    return text.encode("latin-1")


def from_user(text: str) -> str:
    """A caller's text in the `decode` domain: written to the file as UTF-8."""
    return str(text).encode("utf-8").decode("latin-1")


def to_user(text: str) -> str:
    """Script text from the `decode` domain for display, read as UTF-8 where it is."""
    return text.encode("latin-1").decode("utf-8", errors="replace")


def parse_spec(text: str, spec: dict) -> list[Block]:
    """`parse` with the brace actions and tokenizer options the profile states."""
    return parse(text, brace_actions(spec), high_bytes_split(spec))


def parse(
    text: str, brace_actions: frozenset[str] = frozenset(), high_bytes_split: bool = False
) -> list[Block]:
    """Parse a script into blocks, or raise `ScriptSyntaxError` where the engine would stop.

    The engine reads the file once per entity with a script name (g_script.c:343-623): it
    skips every block before that entity's own by counting braces, and reads its own block
    action by action. Both readings are made here. Block boundaries come from the skipping
    one; a block that ends somewhere else when it is read as the running block - a brace on
    an action's line, which is that action's argument - is a syntax error, because every
    entity whose block comes later reads the rest of the file out of step.
    """
    tokens = tokenize(text, high_bytes_split)
    blocks: list[Block] = []
    for first, name, close in _skim(tokens):
        block, end = _read_block(tokens, first, name, brace_actions)
        if end != close:
            raise ScriptSyntaxError(
                f"block {block.name!r} ends at line {tokens[end].line} when it runs but at line "
                f"{tokens[close].line} when the engine skips it to reach a later block; a brace "
                "on an action's line is that action's argument, not the end of the event",
                tokens[min(end, close)].line,
            )
        blocks.append(block)
    return blocks


_EMPTY = 'an empty "" token, which the engine reads as the end of the script'


def _skim(tokens: list[Token]) -> list[tuple[int, int, int]]:
    """Each block's first token, name token and closing brace, read the way the engine skips
    blocks that are not the running entity's (g_script.c:380-418, 589-619)."""
    spans: list[tuple[int, int, int]] = []
    want_name, want_script, bracket = True, False, 0
    first = name = -1
    pos, n = 0, len(tokens)
    while pos < n:
        t = tokens[pos]
        pos += 1
        if not t.text:
            raise ScriptSyntaxError(
                f"{_EMPTY}{'' if want_name else '; a closing brace is still expected'}", t.line
            )
        if t.text[0] == "}":
            if want_name:
                raise ScriptSyntaxError("'}' found, but not expected", t.line)
            spans.append((first, name, pos - 1))
            want_name, first = True, -1
        elif t.text[0] == "{":
            if want_name:
                raise ScriptSyntaxError("'{' found, a name expected", t.line)
            want_script = False
        elif want_name:
            if first < 0:
                first = pos - 1
            if t.text.lower() == "entity":
                continue  # the optional keyword before a name, as often as it is written
            name, want_name, want_script = pos - 1, False, True
        elif want_script:
            raise ScriptSyntaxError(
                f"'{{' expected after {tokens[name].text!r}, found {t.text!r}", t.line
            )
        else:  # an event of a block being skipped: everything to its brace level's end
            while True:
                if pos >= n:
                    raise ScriptSyntaxError("end of script found, '}' expected", t.line)
                t = tokens[pos]
                pos += 1
                if not t.text:
                    raise ScriptSyntaxError(_EMPTY, t.line)
                if t.text[0] == "{":
                    bracket += 1
                elif t.text[0] == "}":
                    bracket -= 1
                    if not bracket:
                        break
    if not want_name:
        last = tokens[-1].line if tokens else 1
        raise ScriptSyntaxError("end of script found, '}' expected", last)
    return spans


def _read_block(
    tokens: list[Token], first: int, name: int, brace_actions: frozenset[str]
) -> tuple[Block, int]:
    """One block as the engine reads its running entity's (g_script.c:396-587), and the index
    of the brace that ends it there."""
    pos = name + 1

    def take(what: str) -> Token:
        nonlocal pos
        if pos >= len(tokens):
            last = tokens[-1].line if tokens else 1
            raise ScriptSyntaxError(f"end of script found, {what} expected", last)
        t = tokens[pos]
        pos += 1
        if not t.text:
            raise ScriptSyntaxError(f"{_EMPTY}; {what} expected", t.line)
        return t

    head = tokens[name]
    block = Block(head.text, head.line, start=tokens[first].start)
    while True:
        t = take("'}'")
        if t.text[0] == "}":
            block.close, block.end = t.start, t.end
            return block, pos - 1
        if t.text[0] == "{":
            continue  # the block's own brace; the engine accepts any number of them
        event = Event(t.text.lower(), [], t.line, start=t.start)
        while (t := take("'{'")).text[0] != "{":
            event.params.append(t.text)
        while (t := take("'}'")).text[0] != "}":
            action = Action(t.text.lower(), [], t.line)
            if action.name in brace_actions:
                if take("'{'").text[0] != "{":
                    raise ScriptSyntaxError(f"'{{' expected after {action.name}", t.line)
                while True:
                    if pos >= len(tokens):  # the engine never returns from this one
                        raise ScriptSyntaxError(
                            f"end of script found, '}}' expected after {action.name}", t.line
                        )
                    a = tokens[pos]
                    pos += 1
                    if a.text[:1] == "}":
                        break
                    action.args.append(a.text)
            else:
                # The rest of the line, as the engine sees lines; an empty "" ends it too.
                while pos < len(tokens) and not tokens[pos].newline:
                    a = tokens[pos]
                    pos += 1
                    if not a.text:
                        break
                    action.args.append(a.text)
            event.actions.append(action)
        event.end = t.end
        block.events.append(event)


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


def brace_actions(spec: dict) -> frozenset[str]:
    return frozenset(str(a).lower() for a in spec.get("brace_actions") or [])


def names(spec: dict, entities: list[dict]) -> dict[str, list[int]]:
    """Block names the map's entities run, lowercased, with the entities that run each."""
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
        blocks = parse_spec(decode(path.read_bytes()) if text is None else text, spec)
    except ScriptSyntaxError as e:
        return [finding("MAPSCRIPT_SYNTAX", "error", f"{path.name}: {e}; the game stops at load")]

    key = str(spec.get("block_key", "scriptname")).lower()
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

    # Entities the script spawns itself are as real as the map's own, for names and blocks.
    # Index -1 marks them; they are never listed in a finding.
    entities = entities + _spawned(spec, entities, by_name)
    named = names(spec, entities)

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
    block_key = str(spec.get("block_key", "scriptname")).lower()
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
            block = by_name.get(k.get(block_key, "").strip().lower())
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


def _spawned(spec: dict, entities: list[dict], by_name: dict[str, Block]) -> list[dict]:
    """Entities created by the script's own spawning actions, as key/value pairs.

    Only a block that runs spawns anything: the first of its name, named by an entity of the
    map or by one an earlier spawn created, followed to a fixed point. A spawn in a dead or
    duplicate block must not hide a reference that resolves to nothing at run time.
    """
    spawning = {str(a).lower() for a in spec.get("spawning_actions") or []}
    out: list[dict] = []
    done: set[str] = set()
    while live := [n for n in names(spec, entities + out) if n in by_name and n not in done]:
        for name in live:
            done.add(name)
            for ev in by_name[name].events:
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
        # Names meaning the entity running the action: its own block is the target.
        itself = {str(s).lower() for s in rule.get("self") or []}
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
                    target = b if value.lower() in itself else by_name.get(value.lower())
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
    """`text` if the engine reads it as one plain word: printable ASCII, no quotes, braces or
    comment openers. Bytes past ASCII are whitespace to it on a signed-`char` build."""
    text = str(text).strip()
    if (
        not text
        or any(not 33 <= ord(c) <= 126 or c in '{}"' for c in text)
        or "//" in text
        or "/*" in text
    ):
        raise ScriptEditError(
            f"{what} {text!r} must be one word of printable ASCII, with no braces, quotes or "
            "comments"
        )
    return text


def _line_action(line: str, spec: dict) -> Action:
    """The action a caller's script line is, as the engine would read it, or a refusal.

    The line is tokenized as the engine tokenizes it; anything that only reads as intended
    because of the line around it - a comment, an open quote, a nested `\\"`, an empty `""`,
    a brace among the arguments, a word with bytes the engine splits on - is refused.
    """
    raw = str(line).strip()
    if not raw or "\n" in raw or "\r" in raw:
        raise ScriptEditError(f"each action is one non-empty line, got {raw!r}")
    shown = to_user(raw)
    if '\\"' in raw:
        raise ScriptEditError(f'{shown!r}: the engine reads \\" as a nested string; drop it')
    toks = tokenize(raw, high_bytes_split(spec))
    prev = 0
    for t in toks:
        gap = raw[prev : t.start]
        if gap.strip(" \t"):
            raise ScriptEditError(
                f"{shown!r}: {gap.strip()!r} is not part of any argument - a comment, or bytes "
                "the engine reads as a space; quote text that holds them"
            )
        if t.quoted and (t.end - t.start < 2 or raw[t.end - 1] != '"'):
            raise ScriptEditError(f"unbalanced quote in {shown!r}")
        if not t.text:
            raise ScriptEditError(
                f'{shown!r}: the engine reads an empty "" as the end of the script'
            )
        prev = t.end
    if raw[prev:].strip(" \t"):
        raise ScriptEditError(f"{shown!r}: {raw[prev:].strip()!r} is a comment; drop it")
    name = toks[0].text.lower()
    known = {str(a).lower() for a in spec.get("actions") or []}
    if known and name not in known:
        raise ScriptEditError(f"unknown action {to_user(name)!r} in {shown!r}")
    rest = toks[1:]
    if name in brace_actions(spec):
        if (
            len(rest) < 2
            or (rest[0].text, rest[-1].text) != ("{", "}")
            or rest[0].quoted
            or rest[-1].quoted
        ):
            raise ScriptEditError(
                f"{name} takes its arguments as one {{ ... }} list, braces apart from the "
                f"words: {shown!r}"
            )
        rest = rest[1:-1]
    if any(t.text[0] in "{}" for t in rest):
        raise ScriptEditError(
            f"{shown!r}: an argument starting with a brace would end the event or open a "
            "block when the engine skips this one, quoted or not"
        )
    return Action(name, [t.text for t in rest], 0)


def validate_event(
    spec: dict, block: str, event: str, params: list[str], actions: list[str]
) -> list[Action]:
    """Refuse what the engine stops on or misreads; the actions as the engine will read them."""
    _word(block, "block name")
    if block.strip().lower() == "entity":
        raise ScriptEditError("'entity' is the keyword before a block name, not a name")
    _word(event, "event name")
    events = {str(e).lower() for e in spec.get("events") or []}
    if events and str(event).lower() not in events:
        raise ScriptEditError(
            f"unknown event {event!r}; the profile lists: {', '.join(sorted(events))}"
        )
    for p in params:
        _word(p, "event parameter")
    return [_line_action(line, spec) for line in actions]


def _newline(text: str) -> str:
    """The line ending the file already uses, for the lines an edit adds."""
    return "\r\n" if "\r\n" in text else "\n"


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


def _shape(blocks: list[Block]) -> list:
    """What the engine reads from a script: names, events, actions and arguments."""
    return [
        [
            b.name.lower(),
            [
                [e.name, [p.lower() for p in e.params], [(a.name, a.args) for a in e.actions]]
                for e in b.events
            ],
        ]
        for b in blocks
    ]


def set_event(
    text: str, spec: dict, block: str, event: str, params: list[str], actions: list[str]
) -> tuple[str, str]:
    """`text` with one event's actions set, and what was done: replaced, added, created or
    unchanged.

    `text` is in the `decode` domain; `block`, `event`, `params` and `actions` are the
    caller's text, written as UTF-8. The result is parsed again and must read exactly as
    asked - this event with these actions, every other block and event as before - or the
    edit is refused (`ScriptEditError`) and nothing comes back.
    """
    block, event = from_user(block).strip(), from_user(event).strip()
    params = [from_user(p).strip() for p in params]
    actions = [from_user(a).strip() for a in actions]
    wanted = validate_event(spec, block, event, params, actions)
    blocks = parse_spec(text, spec)
    expected = _shape(blocks)
    entry = [event.lower(), [p.lower() for p in params], [(a.name, a.args) for a in wanted]]
    nl = _newline(text)
    b = _find(blocks, block)
    if b is None:
        new_block = f"{block}\n{{\n\t{render_event(event, params, actions)}\n}}\n"
        new = new_block.replace("\n", nl)
        if text.strip():
            new = text.rstrip("\r\n") + nl + nl + new
        expected.append([block.lower(), [entry]])
        did = "created block"
    else:
        at = next(i for i, x in enumerate(blocks) if x is b)
        e = _find_event(b, event, params)
        if e is not None:
            if [(a.name, a.args) for a in e.actions] == entry[2]:
                return text, "unchanged"
            rendered = render_event(event, params, actions, _indent_at(text, e.start) or "\t")
            new = text[: e.start] + rendered.replace("\n", nl) + text[e.end :]
            expected[at][1][next(i for i, x in enumerate(b.events) if x is e)] = entry
            did = "replaced event"
        else:
            # A new event goes last in the block, a blank line after the one before it.
            indent = (_indent_at(text, b.events[-1].start) if b.events else "") or "\t"
            rendered = f"{indent}{render_event(event, params, actions, indent)}\n"
            line_start = text.rfind("\n", 0, b.close) + 1
            if text[line_start : b.close].strip():  # the brace shares its line with other text
                line_start, rendered = b.close, "\n" + rendered
            if b.events:
                rendered = "\n" + rendered
            new = text[:line_start] + rendered.replace("\n", nl) + text[line_start:]
            expected[at][1].append(entry)
            did = "added event"
    try:
        got = _shape(parse_spec(new, spec))
    except ScriptSyntaxError as err:
        raise ScriptEditError(
            f"the engine could not read the script after this edit: {err}"
        ) from err
    if got != expected:
        raise ScriptEditError(
            "the engine would read the script differently from what was asked: "
            + _first_difference(expected, got)
        )
    return new, did


def _first_difference(want: list, got: list) -> str:
    for w, g in zip(want, got, strict=False):
        if w != g:
            return f"expected block {w[0]!r} as {w[1]!r}, it reads as {g!r}"
    return f"expected {len(want)} blocks, it reads as {len(got)}"


def action_text(action: Action, braced: frozenset[str] = frozenset()) -> str:
    """An action as a script line that parses back to the same action.

    An argument is quoted when it would otherwise read differently: empty, holding a space or
    a byte past ASCII, or starting with a brace or a comment opener.
    """

    def word(x: str) -> str:
        plain = x and all(33 <= ord(c) <= 126 for c in x) and x[0] not in '{}"'
        return x if plain and not x.startswith(("//", "/*")) else f'"{x}"'

    args = [word(x) for x in action.args]
    if action.name in braced:
        return " ".join([action.name, "{", *args, "}"])
    return " ".join([action.name, *args])


def remove(
    text: str, spec: dict, block: str, event: str | None = None, params: list[str] | None = None
) -> str:
    """`text` without one event, or without a whole block when `event` is None.

    A whole block goes with every later block of the same name: only the first of a name ever
    runs, so removing it alone would make a forgotten copy the one that runs.
    """
    block = from_user(block).strip()
    blocks = parse_spec(text, spec)
    b = _find(blocks, block)
    if b is None:
        raise ScriptEditError(f"no block named {block!r}")
    if event is None:
        spans = [(x.start, x.end) for x in blocks if x.name.lower() == block.lower()]
    else:
        params = [from_user(p).strip() for p in params or []]
        e = _find_event(b, from_user(event).strip(), params)
        if e is None:
            label = " ".join([event, *params])
            raise ScriptEditError(f"block {block!r} has no {label!r} event")
        spans = [(e.start, e.end)]
    for start, end in reversed(spans):
        text = _cut(text, start, end)
    return text


def _cut(text: str, start: int, end: int) -> str:
    nl = _newline(text)
    # Take the indentation before it and the line break after it, when they are its own.
    line_start = text.rfind("\n", 0, start) + 1
    if not text[line_start:start].strip():
        start = line_start
    brk = text.find("\n", end)
    if brk >= 0 and not text[end:brk].strip():
        end = brk + 1
    # And one blank line, so removing a block or event leaves no gap of its own behind.
    if text[:start].endswith(nl + nl):
        start -= len(nl)
    elif text[end:].startswith(nl):
        end += len(nl)
    return text[:start] + text[end:]
