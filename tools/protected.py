#!/usr/bin/env python3
"""Protected paths: the things self-modification may never touch (§11.4).

> Protected paths are immutable to self-dev, hash-pinned and checked pre-merge: the
> differential harness, the fitness definitions and bench runner, the corpus, and every
> `confidence: verified` entry in the rule profile. **Without this the agent optimizes the ruler
> instead of the thing** — the single most likely failure mode.

That is the whole argument, and §13 restates it: "The agent will try to game its own fitness
function. This is not hypothetical; it is the default behaviour of any optimization loop with a
mutable objective."

So the protection is mechanical. Every protected file's SHA-256 is pinned in
`bench/protected.json`, and `--verify` fails if any of them moved. Updating a pin requires
`--repin`, which is not something the self-dev loop calls — it exists for a human who has decided
the change is legitimate.

The list includes every profile's verified content, extracted rather than whole-file hashed, so
a profile can gain new unverified entries without a human in the loop while the verified ones stay
frozen. "Verified content" is every mapping whose *effective* confidence is verified: its own
`confidence`, or else the one it inherits from the nearest enclosing mapping that states one. A
section marked `confidence: verified` therefore pins everything in it — bots, objectives, the map
script, the engine's fatal patterns, packaging — except entries that mark themselves
`confidence: unverified`. Only the exact string `unverified` opts out: any other spelling
(`Verified`, `VERIFIED`, a typo) is pinned as verified, and the profile loader refuses it anyway.

The working rule this implies: **new or changing work goes in as `confidence: unverified`**, which
needs no re-pin and can only ever produce `info` findings. A human promotes it to `verified` after
review and re-pins (`mise run --yes selfdev:repin`) in the same commit. Editing an entry that is
already verified — including tuning a verified `bots:` or `objectives:` value — is a re-pin too,
by design: those values decide what fails a build.

The pin file itself cannot appear in its own hash list — a file cannot contain its own hash. Its
integrity comes from git instead: `verify` checks that `bench/protected.json` is unmodified
relative to `HEAD`, so tampering shows up as a working-tree change a human sees in the diff before
anything merges. That is exactly the "git-backed with automatic revert" guarantee §11.4 asks for,
applied to the one file that cannot be self-protecting.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

#: Files self-dev may never modify. Anything that measures, judges, or defines correctness.
PROTECTED_FILES = (
    # The gate itself, and the kernel invariants it checks.
    "tools/difftest.py",
    "crates/nrc-core/src/exact.rs",
    "crates/nrc-core/src/winding.rs",
    "crates/nrc-core/src/num.rs",
    # The ruler.
    "tools/bench.py",
    "tools/protected.py",
    "bench/fitness.json",
    "bench/labels.json",
    "bench/tasks.json",
    # The seam that keeps the design honest.
    "tools/seam_lint.py",
    # The loop's own guardrails: which paths it may touch, how it detects change, the merge rule.
    # Its docstring always said widening `ALLOWED_PATHS` is "a deliberate edit to this file, which
    # is itself protected"; this entry makes that true.
    "tools/selfdev.py",
)

#: Directories whose contents must not change. The corpus is evidence, not code.
PROTECTED_TREES = ("corpus/synthetic/degenerate",)

#: Profile sections pinned whole, whatever their entries' confidence: the physics and the engine
#: limits are measured facts the geometry checks are built on. Everything else that is verified
#: is pinned by `verified_material`, wherever in the profile it sits.
PROTECTED_PROFILE_SECTIONS = ("movement", "engine_limits")

#: The one spelling that opts an entry out of the pin. Anything else counts as verified.
UNVERIFIED = "unverified"


class ProtectionError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _prune(node: object, verified: bool) -> tuple[bool, object]:
    """`node` reduced to what is verified, and whether anything of it is.

    `verified` is the confidence inherited from the enclosing mapping. A mapping's own
    `confidence` replaces it for the mapping and everything under it, so an unverified entry
    inside a verified section drops out (and may change freely) while a verified entry inside an
    unverified one is kept. In a verified context every scalar and every list is kept as it is,
    empty lists included — `events: []` is a change, not an absence.
    """
    if isinstance(node, dict):
        if "confidence" in node:
            verified = node["confidence"] != UNVERIFIED
        out: dict = {}
        for k, v in node.items():
            keep, pruned = _prune(v, verified)
            if keep:
                out[str(k)] = pruned
        return verified or bool(out), out
    if isinstance(node, list):
        items = []
        for v in node:
            keep, pruned = _prune(v, verified)
            if keep:
                items.append(pruned)
        return verified or bool(items), items
    return verified, node


def verified_material(data: dict) -> dict:
    """Everything in a parsed profile whose effective confidence is verified, plus the sections
    pinned whole. The top level states no confidence, so it is unverified until a section says
    otherwise."""
    _, verified = _prune(data, False)
    material: dict = {"verified": verified}
    for section in PROTECTED_PROFILE_SECTIONS:
        if data.get(section) is not None:
            material[section] = data[section]
    return material


def _verified_rule_digest(root: Path) -> dict[str, str]:
    """A digest per profile of its verified content (see `verified_material`).

    Whole-file hashing a 5000-line profile would mean every documentation fix needed a human
    re-pin, and the loop would learn to avoid touching profiles at all rather than to respect the
    verified entries. Hashing what is verified keeps the incentive pointed the right way: an
    unverified entry is free, and it cannot fail a build.

    A profile that cannot be read or parsed is digested as such rather than skipped, so breaking
    a profile's YAML cannot make its pin disappear.
    """
    out: dict[str, str] = {}
    prof_dir = root / "profiles"
    if not prof_dir.is_dir():
        return out
    try:
        import yaml
    except ImportError:
        return out

    for f in sorted(prof_dir.glob("*.yaml")):
        try:
            # UTF-8 always: the locale's encoding differs between machines, and a pin that
            # depends on it reports a violation on every Windows checkout.
            data = yaml.safe_load(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            out[f.name] = f"unreadable: {type(e).__name__}"
            continue
        if not isinstance(data, dict):
            out[f.name] = "unreadable: not a mapping"
            continue
        out[f.name] = sha(json.dumps(verified_material(data), sort_keys=True, default=str).encode())
    return out


def _tree_digest(d: Path) -> str:
    """A digest of a directory that keeps each path apart from its content.

    Hashing `path + bytes` back to back let `a` + `bX` collide with `ab` + `X`. A canonical JSON
    of path -> content hash cannot.
    """
    listing = {
        f.relative_to(d).as_posix(): sha(f.read_bytes())
        for f in sorted(d.rglob("*"))
        if f.is_file()
    }
    return sha(json.dumps(listing, sort_keys=True).encode())


def compute(root: Path | None = None) -> dict[str, dict[str, str]]:
    """Current hashes of everything protected."""
    root = root or repo_root()
    files: dict[str, str] = {}
    for rel in PROTECTED_FILES:
        p = root / rel
        # A protected file that does not exist yet is recorded as absent rather than skipped, so
        # its later appearance is visible and its deletion cannot pass unnoticed.
        files[rel] = sha(p.read_bytes()) if p.is_file() else "absent"

    trees: dict[str, str] = {}
    for rel in PROTECTED_TREES:
        d = root / rel
        if not d.is_dir():
            trees[rel] = "absent"
            continue
        trees[rel] = _tree_digest(d)

    return {"files": files, "trees": trees, "profiles": _verified_rule_digest(root)}


def pin_path(root: Path) -> Path:
    return root / "bench" / "protected.json"


def load_pins(root: Path) -> dict:
    p = pin_path(root)
    if not p.is_file():
        raise ProtectionError(
            f"{p} does not exist, so nothing is pinned. Run `python tools/protected.py --repin` "
            f"once, by hand, after reviewing what is about to be frozen."
        )
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProtectionError(f"{p} is unreadable: {e}") from e


def _pin_file_modified(root: Path) -> bool:
    """True if the pin file differs from what is committed.

    The pin file cannot hash itself, so this is how tampering with it is caught: a self-dev run
    that rewrote the pins would leave the file dirty relative to HEAD, and that is visible.
    """
    import subprocess as sp

    rel = pin_path(root).relative_to(root).as_posix()
    # Only meaningful for a tracked file in a real repository, and the check must be scoped to
    # *this* root. Git searches upward for a repository, so running it in a synthetic tree can
    # otherwise answer about an enclosing one. An untracked pin file cannot differ from HEAD
    # anyway, and a guard that fires on every fresh checkout gets switched off.
    tracked = sp.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", rel],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(root.parent)},
    )
    if tracked.returncode != 0:
        return False
    diff = sp.run(
        ["git", "-C", str(root), "diff", "--quiet", "HEAD", "--", rel],
        capture_output=True,
        text=True,
        check=False,
    )
    # Exit 1 means differences; 128 means no HEAD, which is not evidence of tampering.
    return diff.returncode == 1


def verify(root: Path | None = None) -> list[str]:
    """Return a list of violations. Empty means everything protected is unchanged."""
    root = root or repo_root()
    pins = load_pins(root)
    now = compute(root)
    problems: list[str] = []

    for kind in ("files", "trees", "profiles"):
        expected = pins.get(kind, {})
        actual = now.get(kind, {})
        for key, want in expected.items():
            got = actual.get(key, "missing")
            if got != want:
                problems.append(
                    f"{kind}/{key}: pinned {want[:12]} but found "
                    f"{got[:12] if got != 'missing' else 'nothing'}"
                )
        for key in actual:
            if key not in expected:
                problems.append(f"{kind}/{key}: not pinned — re-pin deliberately or remove it")

    if _pin_file_modified(root):
        problems.append(
            "bench/protected.json differs from HEAD — the pin file cannot hash itself, so a "
            "change to it is caught here. Commit it deliberately or revert it."
        )
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--verify", action="store_true", help="fail if anything protected changed")
    ap.add_argument("--list", action="store_true", help="show what is protected and why")
    ap.add_argument(
        "--repin",
        action="store_true",
        help="rewrite the pins. For a human who has reviewed the change; self-dev must never "
        "invoke this, and the pin file is itself protected so that re-pinning cannot launder "
        "an edit.",
    )
    args = ap.parse_args()
    root = repo_root()

    if args.list or not (args.verify or args.repin):
        print("Protected from self-modification (§11.4):\n")
        print("  files — the gate, the exact predicates, the ruler, the seam lint")
        for rel in PROTECTED_FILES:
            state = "" if (root / rel).is_file() else "   [absent]"
            print(f"    {rel}{state}")
        print("\n  trees — evidence, not code")
        for rel in PROTECTED_TREES:
            print(f"    {rel}")
        print("\n  profiles — every entry whose effective confidence is verified, anywhere in")
        print(
            f"    the profile, plus {', '.join(PROTECTED_PROFILE_SECTIONS)} whole. Entries marked"
        )
        print("    `confidence: unverified` stay free, so new work goes in unverified and a human")
        print("    promotes it and re-pins")
        if not (args.verify or args.repin):
            return 0

    if args.repin:
        pin_path(root).parent.mkdir(parents=True, exist_ok=True)
        state = compute(root)
        state["note"] = (
            "Hash pins for paths self-dev may not modify (§11.4). Re-pinned only by a human who "
            "has reviewed the change; the loop cannot call --repin."
        )
        pin_path(root).write_text(
            json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        n = len(state["files"]) + len(state["trees"]) + len(state["profiles"])
        print(f"pinned {n} item(s) to {pin_path(root).relative_to(root)}")
        return 0

    try:
        problems = verify(root)
    except ProtectionError as e:
        print(f"protected: {e}", file=sys.stderr)
        return 2

    if problems:
        print(f"PROTECTION VIOLATED — {len(problems)} item(s) changed:\n", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print(
            "\nThese paths define what correct means. A self-dev attempt that changes them is "
            "optimizing the ruler, which §11.4 names as the most likely failure mode. If the "
            "change is legitimate, a human re-pins it with --repin after reviewing it.",
            file=sys.stderr,
        )
        return 1

    print("protected paths unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
