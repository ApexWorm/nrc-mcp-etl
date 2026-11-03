"""Profile-declared script owner lifetimes, without simulating a game's script VM.

Only explicit waits and direct same-owner calls are followed. Runtime branches, uncertain
durations and other blocking actions reduce a finding to an informational risk. A call to
another entity does not inherit the caller's lifetime.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .rules import Finding

if TYPE_CHECKING:
    from .mapscript import Action, Block, Event


def _matches(action: Action, rule: dict) -> bool:
    return action.name == rule.get("action") and all(
        int(index) < len(action.args)
        and action.args[int(index)].casefold() == str(value).casefold()
        for index, value in (rule.get("args_equal") or {}).items()
    )


def _duration(action: Action, rule: dict) -> tuple[int, int] | None:
    """A known duration range. Non-literal inputs stay unknown instead of being guessed."""
    args = action.args
    pos = int(rule.get("duration_arg", 0))
    if len(args) <= pos:
        return None
    indices = [pos]
    if args[pos].casefold() == rule.get("range_keyword"):
        indices = [int(rule["minimum_arg"]), int(rule["maximum_arg"])]
    if any(i >= len(args) or not re.fullmatch(r"[+-]?\d+", args[i]) for i in indices):
        return None
    values = [int(args[i]) for i in indices]
    if len(values) == 2 and values[0] > values[1]:
        return None
    lo, hi = min(values), max(values)
    return (lo, hi) if lo >= 0 else None


def check(
    spec: dict, entities: list[dict], blocks: dict[str, Block], filename: str
) -> list[Finding]:
    """Find delayed continuations that can outlive their entity, with profile evidence.

    This is a warning about lifetime hazards, not proof a script event executes. Frame
    pacing, server overrides, branches and engine special states are not simulated.
    """
    model = spec.get("lifetime") or {}
    if not model:
        return []
    block_key = str(spec.get("block_key", "scriptname")).casefold()
    known_actions = set(spec.get("actions") or [])
    uncertain_actions = set(model.get("uncertain_actions") or [])
    waits = model.get("wait_actions") or []
    blockers = model.get("conditional_wait_actions") or []
    calls = model.get("call_actions") or []
    removals = model.get("removal_actions") or []
    implied = {item["classname"]: item["name"] for item in spec.get("implied_names") or []}
    owners: dict[str, list[dict]] = {}
    for entity in entities:
        keys = {}
        for key, value in entity.get("keys") or []:
            keys.setdefault(key.casefold(), value)
        name = implied.get(entity.get("classname"), keys.get(block_key, "")).strip().casefold()
        if name in blocks:
            owners.setdefault(name, []).append(entity)
    findings: list[Finding] = []
    seen = set()
    visits_left = 20_000
    budget_reported = False

    def inspect(
        block: Block,
        event: Event,
        owner,
        context,
        uncertain=False,
        chain=(),
        visiting=frozenset(),
        elapsed_min=0,
    ):
        nonlocal visits_left, budget_reported
        identity = (block.name.casefold(), event.line)
        if identity in visiting or len(visiting) >= 32:
            return True  # Do not infer continuation through recursion.
        visiting = visiting | {identity}
        for number, action in enumerate(event.actions):
            visits_left -= 1
            if visits_left < 0:
                if not budget_reported:
                    budget_reported = True
                    findings.append(
                        Finding(
                            "MAPSCRIPT_LIFETIME_INCOMPLETE",
                            "info",
                            f"{filename}: lifetime analysis stopped at its 20000-action budget; "
                            "the remaining same-owner call graph has not been checked.",
                            "unverified",
                            "nrc_mcp.script_lifetime: bounded static analysis",
                            [],
                            "simplify repeated same-owner trigger chains or verify the omitted paths in game",
                        )
                    )
                return True
            for rule in removals:
                if _matches(action, rule):
                    context = rule
                    elapsed_min = 0
            wait = next((rule for rule in waits if _matches(action, rule)), None)
            blocker = next((rule for rule in blockers if _matches(action, rule)), None)
            if context and (wait or blocker):
                duration = _duration(action, wait) if wait else None
                if duration is None:
                    uncertain = True
                else:
                    elapsed_min += duration[0]
                # A trailing wait has no continuation to lose. It still yields a caller's
                # event, so return True below and do not invent a return from that call.
                if number + 1 < len(event.actions):
                    deadline = float(context["remove_after_ms"])
                    exceeds = duration is not None and elapsed_min >= deadline
                    risk = uncertain or not exceeds
                    confidence = (
                        "unverified" if risk else str(context.get("confidence", "unverified"))
                    )
                    key = (
                        owner.get("index"),
                        block.name.casefold(),
                        action.line,
                        context.get("locus"),
                    )
                    if key not in seen:
                        seen.add(key)
                        following = event.actions[number + 1]
                        label = " ".join([event.name, *event.params])
                        trail = " -> ".join((*chain, f"{block.name}.{label}"))
                        reason = (
                            f"cumulative literal wait is at least {elapsed_min} ms"
                            if exceeds
                            else "the continuation depends on timing or runtime control flow"
                        )
                        idx = int(owner.get("index", -1))
                        findings.append(
                            Finding(
                                "MAPSCRIPT_LIFETIME_RISK" if risk else "MAPSCRIPT_LIFETIME_WAIT",
                                "info" if confidence != "verified" else "warning",
                                f"{filename}:{action.line}: {trail}: {action.name} may outlive "
                                f"its owner ({owner.get('classname')}, entity {idx}); "
                                f"removal is scheduled after {deadline:g} ms and {reason}. "
                                f"The following {following.name} at line {following.line} "
                                "can be discarded. Runtime branches, frame pacing and "
                                "engine special states are not simulated.",
                                confidence,
                                f"profile script lifetime: {context.get('locus', '')}; "
                                f"{model.get('wait_locus', '')}",
                                [idx] if idx >= 0 else [],
                                str(context.get("fix_hint") or model.get("fix_hint") or ""),
                            )
                        )
                # Once a wait is encountered, callers are replaced rather than resumed.
                # Continue within this event to check cumulative waits and later actions.
            call = next((rule for rule in calls if _matches(action, rule)), None)
            if call and context:
                target_arg, label_arg = int(call["target_arg"]), int(call["label_arg"])
                if len(action.args) > max(target_arg, label_arg):
                    target = action.args[target_arg].casefold()
                    same = target == block.name.casefold() or target in call.get("self_names", [])
                    if same:
                        label = action.args[label_arg].casefold()
                        child = next(
                            (
                                e
                                for e in block.events
                                if e.name == call["event"]
                                and [p.casefold() for p in e.params] == [label]
                            ),
                            None,
                        )
                        if child and inspect(
                            block,
                            child,
                            owner,
                            context,
                            uncertain,
                            (
                                *chain,
                                f"{block.name}.{' '.join([event.name, *event.params])}:{action.line}",
                            ),
                            visiting,
                            elapsed_min,
                        ):
                            return True
                    else:
                        # Another owner's immediate event may change this owner's state
                        # or script. Its own waits do not inherit the caller's lifetime.
                        uncertain = True
            if action.name in uncertain_actions or action.name not in known_actions:
                uncertain = True
        return any(any(_matches(a, r) for r in (*waits, *blockers)) for a in event.actions)

    for name, members in owners.items():
        if visits_left < 0:
            break
        block = blocks[name]
        for owner in members:
            processed = set()
            for event in block.events:
                signature = (event.name, tuple(p.casefold() for p in event.params))
                if signature in processed:
                    continue  # The engine uses the first matching event.
                processed.add(signature)
                context = next(
                    (
                        r
                        for r in model.get("event_lifetimes") or []
                        if r.get("classname") == owner.get("classname")
                        and event.name in r.get("events", [])
                    ),
                    None,
                )
                inspect(block, event, owner, context, uncertain=int(owner.get("index", -1)) < 0)
    return findings
