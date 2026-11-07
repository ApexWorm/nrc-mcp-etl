#!/usr/bin/env python3
"""The self-optimization loop (§11.2), with §11.4's guardrails as code rather than intentions.

§11.3 is explicit about where to start, and this follows it exactly:

> The highest return per unit of risk is **not** in the kernel. It's in tool descriptions, the
> conventions resource, the asset-brief template, and the pipeline prompt. These are measurable
> through F5/F6, can't corrupt anyone's map, and are trivially revertible.

So `ALLOWED_PATHS` covers the prompt and resource layer and nothing else. Widening it is a
deliberate edit to this file, which is itself protected — and §11.4 requires human review
indefinitely for the exact predicates and for anything that writes user `.map` files, so the kernel
stays out regardless.

Every guardrail §11.4 lists is enforced here:

- **Protected paths** are checked before and after, via `tools/protected.py`.
- **Change is measured against the attempt's base commit**, not the dirty tree: committing an edit
  (and a re-pin) does not hide it, a rename is checked under both names, and the git-ignored files
  that feed the gates (local config, the kernel build, the stored bench baseline) are fingerprinted
  when the attempt is proposed and must be unchanged when it runs.
- **F1 is a gate**, never a weight. A red gate reverts, whatever else improved.
- **Merge requires** F1 green, no signal regressed, and at least one improved.
- **Git-backed with automatic revert**: every attempt is a branch, and a failed gate discards it.
- **Rate-limited**: on demand or nightly, never continuously. An agent rewriting itself in the
  middle of somebody's mapping session is not a feature.
- **Opt-in**: nothing here runs unless `NRC_SELFDEV=1`, so a normal session never sees it.

And **archive, not just a pointer** (§11.2): every attempt is kept with its scores, including the
rejected ones, so a later attempt can branch from a non-latest ancestor. A greedy chain gets stuck,
and the self-improving-agent literature is consistent on that point.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

#: The only paths a self-dev attempt may modify. §11.3's "start with prompts, not code".
#:
#: **Files, not the `surface/` directory.** The tool surface was split out of `server.py`
#: into `python/src/nrc_mcp/surface/*.py`, and the obvious edit here — pointing this at the
#: directory — would have been wrong twice over. `path_allowed` prefix-matches any entry
#: ending in `/`, so a directory entry grants write access to `surface/mapfile.py` and
#: `surface/sculpt.py`, which is to say to `map_save`, `solid_commit` and `pack_pk3`. §11.4
#: requires human review indefinitely for anything that writes user `.map` files, and
#: nothing would have caught the widening: `bench/protected.json` pins no path under
#: `python/src`, and this file is not pinned either.
#:
#: So the listing stays file-by-file, and the files listed are the ones whose *descriptions*
#: drive tool choice without their code touching a user's map. `server.py` carries the
#: prompts and the instructions; the read-only analysis surfaces carry the descriptions.
ALLOWED_PATHS = (
    "python/src/nrc_mcp/server.py",  # prompts, instructions, the derived inventory
    "python/src/nrc_mcp/surface/gameplay.py",  # descriptions only; every tool read-only
    "python/src/nrc_mcp/surface/render.py",  # descriptions only; every tool read-only
    "docs/conventions.md",
    "profiles/",  # unverified entries only; every verified one is hash-pinned
)

ATTEMPTS_DIR = "bench/selfdev"

#: Git-ignored files that feed the tests or the bench, fingerprinted at `propose` and required to
#: be unchanged at `run`. Git cannot see them, so without this an attempt could change the local
#: configuration or swap the built kernel. Entries ending in `/` cover everything under them. The
#: stored bench baseline the attempt is compared against is fingerprinted too (`ignored_inputs`);
#: other bench results are outputs, so running the bench by hand meanwhile does not reject.
IGNORED_INPUTS = (
    "mise.local.toml",
    ".env",
    ".mise/",
    "python/src/",  # the installed kernel module (`*.so`, `*.pyd`); sources are tracked
    "target/release/nrc",
    "target/debug/nrc",
)

#: Relative change below which a noisy signal counts as unchanged. Wall time (F6) moves by more
#: than this between identical runs, so without a margin the merge rule accepts noise as an
#: improvement and rejects it as a regression.
NOISE_TOLERANCE = {"F6": 0.10}
#: Minimum gap between runs. §11.4: "Self-dev runs on demand or nightly, not continuously."
MIN_INTERVAL_SECONDS = 3600


class SelfDevError(RuntimeError):
    pass


def repo_root() -> Path:
    return Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parent.parent)


def enabled() -> bool:
    return os.environ.get("NRC_SELFDEV") == "1"


def require_enabled() -> None:
    if not enabled():
        raise SelfDevError(
            "self-dev is off. Set NRC_SELFDEV=1 to enable it. It is opt-in because a normal "
            "mapping session has no business being able to rewrite the tool it is using."
        )


def git(args: list[str], root: Path, check: bool = False) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise SelfDevError(f"git {' '.join(args)} failed: {out.strip()[:300]}")
    return p.returncode, out


def attempts_dir(root: Path) -> Path:
    d = root / ATTEMPTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def history(root: Path) -> list[dict]:
    return [
        json.loads(f.read_text()) for f in sorted(attempts_dir(root).glob("*.json")) if f.is_file()
    ]


def _last_run_time(root: Path) -> float:
    times = [
        a.get("started_epoch", 0.0)
        for a in history(root)
        if isinstance(a.get("started_epoch"), (int, float))
    ]
    return max(times) if times else 0.0


def check_rate_limit(root: Path, force: bool = False) -> None:
    if force:
        return
    since = time.time() - _last_run_time(root)
    if since < MIN_INTERVAL_SECONDS:
        raise SelfDevError(
            f"last attempt was {int(since / 60)} minutes ago; the minimum gap is "
            f"{MIN_INTERVAL_SECONDS // 60} minutes (§11.4 rate-limits this deliberately). "
            f"Pass --force if you are driving it by hand."
        )


def path_allowed(rel: str) -> tuple[bool, str]:
    """Whether a self-dev attempt may modify `rel`."""
    norm = rel.replace("\\", "/")
    if ".." in norm or norm.startswith("/"):
        return False, "path escapes the repository"
    # Note that `profiles/` being allowed does not make a profile's *verified* rules editable:
    # those are hash-pinned, so a change to one fails the protected-path check instead. The
    # split is deliberate — a profile may gain unverified rules freely, and freeze the rest.
    for allowed in ALLOWED_PATHS:
        if norm == allowed or (allowed.endswith("/") and norm.startswith(allowed)):
            return True, ""
    return False, (
        f"{norm} is outside the prompt and resource layer. §11.3 puts the loop there first "
        f"because that is where the return per unit of risk is highest, and §11.4 requires human "
        f"review indefinitely for the geometric predicates and for anything that writes user "
        f"maps. Allowed: {', '.join(ALLOWED_PATHS)}"
    )


def _parse_name_status(raw: str) -> list[tuple[str, str]]:
    """`git diff --name-status -z` output as (status, path), both sides of a rename or copy."""
    parts = raw.split("\0")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(parts):
        status = parts[i]
        if not status:
            i += 1
            continue
        if status[0] in "RC":
            if i + 2 >= len(parts):
                break
            out.append((status[0], parts[i + 1]))
            out.append((status[0], parts[i + 2]))
            i += 3
        else:
            if i + 1 >= len(parts):
                break
            out.append((status[0], parts[i + 1]))
            i += 2
    return out


def changed_paths(root: Path, base_sha: str) -> list[tuple[str, str]]:
    """Every path that differs from `base_sha`, as (status, path).

    Measured from the attempt's base commit to the working tree, so a change that was committed
    is still a change, and both sides of a rename are listed. Untracked files count (status `?`).
    The attempt records are excluded: writing them is the loop's own bookkeeping. Fails closed:
    a base git cannot resolve is an error, never an empty list.
    """
    if not base_sha:
        raise SelfDevError("the attempt records no base commit, so its changes cannot be measured")
    code, _ = git(["cat-file", "-e", f"{base_sha}^{{commit}}"], root)
    if code != 0:
        raise SelfDevError(f"base commit {base_sha[:12]} is not in this repository")
    p = subprocess.run(
        ["git", "diff", "--name-status", "-z", "-M", base_sha, "--"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if p.returncode != 0:
        raise SelfDevError(f"git diff {base_sha[:12]} failed: {p.stderr.strip()[:300]}")
    out = _parse_name_status(p.stdout)
    u = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if u.returncode != 0:
        raise SelfDevError(f"git ls-files failed: {u.stderr.strip()[:300]}")
    out += [("?", rel) for rel in u.stdout.split("\0") if rel]
    mine = ATTEMPTS_DIR.rstrip("/") + "/"
    return [(st, rel) for st, rel in out if not rel.replace("\\", "/").startswith(mine)]


def baseline_rel(base_sha: str) -> str:
    """Where the bench result for `base_sha` is stored, relative to the repository."""
    return f"bench/results/{base_sha[:7]}.json"


def ignored_inputs(root: Path, base_sha: str = "") -> dict[str, str]:
    """SHA-256 of every existing file under IGNORED_INPUTS and of the attempt's stored baseline,
    by repository-relative path."""
    out: dict[str, str] = {}
    for rel in (*IGNORED_INPUTS, *([baseline_rel(base_sha)] if base_sha else [])):
        base = root / rel
        if rel.endswith("/"):
            files = sorted(f for f in base.rglob("*") if f.is_file()) if base.is_dir() else []
            if rel == "python/src/":
                files = [f for f in files if f.suffix in {".so", ".pyd", ".dylib"}]
        else:
            files = [base] if base.is_file() else []
        for f in files:
            out[f.relative_to(root).as_posix()] = hashlib.sha256(f.read_bytes()).hexdigest()
    return out


def ignored_input_changes(before: dict[str, str], after: dict[str, str]) -> list[str]:
    """What changed among the git-ignored inputs between `propose` and `run`."""
    out = []
    for rel in sorted(set(before) | set(after)):
        if rel not in after:
            out.append(f"{rel}: removed")
        elif rel not in before:
            out.append(f"{rel}: appeared")
        elif before[rel] != after[rel]:
            out.append(f"{rel}: changed")
    return out


def gate_env(pycache: Path) -> dict[str, str]:
    """The environment the gates run in: in-tree `__pycache__` is never read.

    Bytecode caches are git-ignored and outlive the sources they were compiled from, so a stale
    or planted one could stand in for a tracked module. A fresh cache prefix (`pycache`, an empty
    directory) makes Python compile from the sources it can see.
    """
    return {**os.environ, "PYTHONPYCACHEPREFIX": str(pycache)}


def protected_problems(root: Path) -> list[str]:
    sys.path.insert(0, str(root / "tools"))
    import protected as prot  # noqa: PLC0415

    return prot.verify(root)


def run_bench(root: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Run the bench into a fresh directory and read the one result it wrote.

    Picking the newest file in `bench/results/` would read whatever had the latest mtime, which
    a planted file can arrange.
    """
    with tempfile.TemporaryDirectory(prefix="nrc-selfdev-bench-") as td:
        proc = subprocess.run(
            [sys.executable, str(root / "tools" / "bench.py"), "--quiet", "--out", td],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        results = sorted(Path(td).glob("*.json"))
        if len(results) != 1:
            raise SelfDevError(
                f"bench wrote {len(results)} result files, expected 1: {proc.stderr.strip()[:300]}"
            )
        return json.loads(results[0].read_text(encoding="utf-8"))


def score_map(bench: dict) -> dict[str, float]:
    return {
        s["id"]: float(s["score"])
        for s in bench.get("signals", [])
        if "score" in s and not s.get("gate")
    }


def compare(before: dict, after: dict) -> dict[str, Any]:
    """Apply §11.4's merge rule: F1 green, nothing regressed, at least one improved.

    A signal the baseline measured and the attempt did not (skipped, or gone) is a regression:
    losing a measurement must not read as "nothing got worse". A signal only the attempt measured
    improves nothing, since there is nothing to compare it with. A noisy signal's change within
    NOISE_TOLERANCE is unchanged, in either direction.
    """
    b, a = score_map(before), score_map(after)
    lower_better = {"F2", "F6"}
    regressed, improved, unchanged = [], [], []
    for sid in sorted(set(b) | set(a)):
        if sid not in a:
            regressed.append(f"{sid} {b[sid]} -> not measured")
            continue
        if sid not in b:
            unchanged.append(sid)
            continue
        delta = a[sid] - b[sid]
        margin = NOISE_TOLERANCE.get(sid, 0.0) * abs(b[sid])
        if delta == 0 or abs(delta) <= margin:
            unchanged.append(sid)
        elif (delta < 0) == (sid in lower_better):
            improved.append(f"{sid} {b[sid]} -> {a[sid]}")
        else:
            regressed.append(f"{sid} {b[sid]} -> {a[sid]}")

    gate = bool(after.get("gate_passed"))
    accept = gate and not regressed and bool(improved)
    reasons = []
    if not gate:
        reasons.append("F1 is red, and F1 is a gate that can never be traded (§11.4)")
    if regressed:
        reasons.append(f"regressed: {', '.join(regressed)}")
    if not improved:
        reasons.append("nothing improved, so there is no reason to keep the change")
    return {
        "accept": accept,
        "gate_passed": gate,
        "improved": improved,
        "regressed": regressed,
        "unchanged": unchanged,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_propose(root: Path, hypothesis: str, target: str) -> dict:
    """Record an attempt before making it, so a rejected one is still evidence."""
    require_enabled()
    if target not in {
        s["id"]
        for s in json.loads((root / "bench" / "fitness.json").read_text())["signals"].values()
    } | set(json.loads((root / "bench" / "fitness.json").read_text())["signals"]):
        raise SelfDevError(
            f"target must be a fitness signal id (F1..F6), got {target!r}. Proposing against "
            f"something unmeasured is how a loop starts hill-climbing on its own opinion."
        )
    attempt_id = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    base_sha = git(["rev-parse", "HEAD"], root, check=True)[1].strip()
    record = {
        "id": attempt_id,
        "hypothesis": hypothesis,
        "target_signal": target,
        "state": "proposed",
        "base_sha": base_sha,
        "ignored_inputs": ignored_inputs(root, base_sha),
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_epoch": time.time(),
    }
    (attempts_dir(root) / f"{attempt_id}.json").write_text(json.dumps(record, indent=2) + "\n")
    return record


def cmd_run(root: Path, attempt_id: str, force: bool = False) -> dict:
    """Verify, bench and gate the working tree against the attempt's baseline.

    The attempt's *changes* are whatever is already in the working tree: this does not write code
    itself. That separation is deliberate — the thing making edits is a model, and the thing
    judging them must not be.
    """
    require_enabled()
    check_rate_limit(root, force)

    path = attempts_dir(root) / f"{attempt_id}.json"
    if not path.is_file():
        raise SelfDevError(f"no attempt {attempt_id}; run selfdev.py history")
    record = json.loads(path.read_text())

    # 1. Only allowed paths may differ from the base commit — committed or not, under either
    #    name of a rename — and the git-ignored inputs must be as they were at `propose`.
    try:
        changed = changed_paths(root, str(record.get("base_sha") or ""))
    except SelfDevError as e:
        changed, violations = [], [str(e)]
    else:
        violations = []
    for _, rel in changed:
        ok, why = path_allowed(rel)
        if not ok:
            violations.append(why)
    if "ignored_inputs" not in record:
        violations.append("the attempt records no fingerprint of its git-ignored inputs")
    else:
        violations += [
            f"git-ignored input {c} since the attempt was proposed"
            for c in ignored_input_changes(
                record["ignored_inputs"], ignored_inputs(root, str(record.get("base_sha") or ""))
            )
        ]
    if violations:
        record.update(
            {
                "state": "rejected",
                "reason": "modified a disallowed path",
                "violations": violations[:5],
            }
        )
        path.write_text(json.dumps(record, indent=2) + "\n")
        return record

    # 2. Protected paths must be untouched. This is the guard against optimizing the ruler.
    problems = protected_problems(root)
    if problems:
        record.update(
            {"state": "rejected", "reason": "protected paths changed", "violations": problems[:5]}
        )
        path.write_text(json.dumps(record, indent=2) + "\n")
        return record

    with tempfile.TemporaryDirectory(prefix="nrc-selfdev-pyc-") as pyc:
        env = gate_env(Path(pyc))
        # 3. The test suite, which contains F1.
        proc = subprocess.run(
            ["mise", "run", "test"], cwd=root, capture_output=True, text=True, check=False, env=env
        )
        if proc.returncode != 0:
            record.update(
                {
                    "state": "rejected",
                    "reason": "mise run test failed",
                    "output_tail": (proc.stdout + proc.stderr)[-1500:],
                }
            )
            path.write_text(json.dumps(record, indent=2) + "\n")
            return record

        # 4. Bench, and compare against the baseline.
        after = run_bench(root, env)
        baseline_path = root / baseline_rel(record["base_sha"])
        if baseline_path.is_file():
            before = json.loads(baseline_path.read_text())
        else:
            record["note"] = (
                f"no baseline at {baseline_path.name}; this run becomes the baseline and cannot be "
                f"accepted, because there is nothing to compare it against"
            )
            before = None

    if before is None:
        record.update({"state": "baselined", "bench": after})
    else:
        verdict = compare(before, after)
        record.update(
            {
                "state": "accepted" if verdict["accept"] else "rejected",
                "verdict": verdict,
                "reason": "; ".join(verdict["reasons"])
                or "gate green and at least one signal improved",
                "bench": after,
            }
        )
    record["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def cmd_revert(root: Path, attempt_id: str) -> dict:
    """Discard the working-tree changes of a rejected attempt."""
    require_enabled()
    path = attempts_dir(root) / f"{attempt_id}.json"
    if not path.is_file():
        raise SelfDevError(f"no attempt {attempt_id}")
    record = json.loads(path.read_text())

    base = str(record.get("base_sha") or "")
    changed = changed_paths(root, base)
    # Only revert what the attempt was allowed to touch, so an unrelated edit made by a human in
    # the meantime is not thrown away. Reverting means "as at the base commit": a path the base
    # has is restored from it, and one it lacks (added, or a rename's new name) is removed.
    reverted = []
    for _, rel in changed:
        ok, _ = path_allowed(rel)
        if not ok:
            continue
        if git(["cat-file", "-e", f"{base}:{rel}"], root)[0] == 0:
            git(["checkout", base, "--", rel], root, check=True)
        else:
            git(["rm", "-q", "--cached", "--ignore-unmatch", "--", rel], root)
            (root / rel).unlink(missing_ok=True)
        reverted.append(rel)
    record["state"] = "reverted"
    record["reverted_paths"] = reverted
    path.write_text(json.dumps(record, indent=2) + "\n")
    return record


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="whether self-dev is enabled, and what it may touch")
    p = sub.add_parser("propose", help="record a hypothesis and its target signal")
    p.add_argument("hypothesis")
    p.add_argument("--target", required=True, help="fitness signal id, e.g. F5")
    p = sub.add_parser("run", help="verify, bench and gate the working tree")
    p.add_argument("attempt_id")
    p.add_argument("--force", action="store_true", help="ignore the rate limit")
    p = sub.add_parser("revert", help="discard a rejected attempt's changes")
    p.add_argument("attempt_id")
    sub.add_parser("history", help="every attempt, accepted and rejected")
    args = ap.parse_args()

    root = repo_root()
    try:
        if args.cmd == "status":
            print(f"self-dev: {'ENABLED' if enabled() else 'off (set NRC_SELFDEV=1)'}")
            print("\nmay modify (§11.3 — prompts and resources first, not the kernel):")
            for a in ALLOWED_PATHS:
                print(f"  {a}")
            print("\nnever, even by hand, without a human re-pin (§11.4):")
            try:
                for pr in protected_problems(root) or ["(all protected paths unchanged)"]:
                    print(f"  {pr}")
            except Exception as e:  # noqa: BLE001
                print(f"  could not check: {e}")
            print(f"\nrate limit: {MIN_INTERVAL_SECONDS // 60} minutes between attempts")
            attempts = history(root)
            print(f"attempts recorded: {len(attempts)}")
            return 0

        if args.cmd == "propose":
            r = cmd_propose(root, args.hypothesis, args.target)
            print(f"proposed {r['id']} targeting {r['target_signal']}")
            print("make your changes, then: selfdev.py run " + r["id"])
            return 0

        if args.cmd == "run":
            r = cmd_run(root, args.attempt_id, args.force)
            print(f"{r['id']}: {r['state'].upper()} — {r.get('reason', '')}")
            v = r.get("verdict") or {}
            for label in ("improved", "regressed"):
                if v.get(label):
                    print(f"  {label}: {', '.join(v[label])}")
            return 0 if r["state"] in ("accepted", "baselined") else 1

        if args.cmd == "revert":
            r = cmd_revert(root, args.attempt_id)
            print(f"{r['id']}: reverted {len(r['reverted_paths'])} path(s)")
            return 0

        if args.cmd == "history":
            for a in history(root):
                v = a.get("verdict") or {}
                extra = f"  improved={v.get('improved')}" if v.get("improved") else ""
                print(
                    f"  {a['id']}  {a['state']:<10} {a.get('target_signal', '?')}  "
                    f"{a.get('hypothesis', '')[:60]}{extra}"
                )
            return 0
    except SelfDevError as e:
        print(f"selfdev: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
