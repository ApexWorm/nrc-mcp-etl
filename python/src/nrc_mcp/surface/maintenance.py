"""Repo upkeep: the fitness suite, upstream drift, self-dev guards (§10, §11).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from ..kernel import repo_root, run_mise_task
from ..runtime import (
    tool,
)


@tool("maintenance", "Run the fitness suite", writes=True)
def bench_run() -> dict:
    """Run the fitness suite and return the scores.

    F1 (kernel correctness) is a **gate**, not a score: a run with it red has no score to compare,
    whatever the other signals say. F4 skips without a compiler and F5 is a declarative proxy for
    the natural-language-brief version; both say so in their output rather than reporting a number
    they cannot justify.

    Also reports whether the protected paths are unchanged. If they are not, treat every score as
    meaningless: the ruler itself was edited.
    """
    r = run_mise_task("bench")
    root = repo_root()
    results = sorted((root / "bench" / "results").glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not results:
        raise ToolError("bench produced no result file. Its output ended:\n" + r["stdout"][-1500:])
    import json as _json

    data = _json.loads(results[-1].read_text())
    return {
        "sha": data["sha"],
        "dirty": data["dirty"],
        "gate_passed": data["gate_passed"],
        "protected_paths_ok": data["protected_paths_ok"],
        "protected_problems": data["protected_problems"],
        "signals": [{k: v for k, v in s.items() if k not in ("cases",)} for s in data["signals"]],
        "result_file": str(results[-1].relative_to(root)),
    }


_UPSTREAM_NOTE = (
    "a breaking change deliberately does not advance the baseline, so it keeps being "
    "reported until it is dealt with"
)


@tool("maintenance", "Upstream drift report")
def upstream_diff() -> dict:
    """Report upstream drift that would break the editor bridge, against the local baseline.

    Hashes the individual declarations in `include/` the plugin binds to, rather than whole files,
    so it fires when something the plugin calls has actually moved instead of on every comment
    change. Also reports new or removed compiler flags, because each new flag is a potential
    optimizer capability.

    Reads what is already on disk. `upstream_fetch` is the one that goes to the network.
    """
    r = run_mise_task("pr:baseline")
    return {"ok": r["ok"], "output": r["stdout"] or r["stderr"], "note": _UPSTREAM_NOTE}


@tool("maintenance", "Fetch upstream and report drift", writes=True, open_world=True)
def upstream_fetch() -> dict:
    """Fetch upstream and report the drift against what was just fetched.

    The same report as `upstream_diff`, but this one **reaches the network** and advances the
    stored baseline. Use `upstream_diff` when you only want to re-read what is already here.
    """
    r = run_mise_task("pr:watch", ["--fetch"])
    return {"ok": r["ok"], "output": r["stdout"] or r["stderr"], "note": _UPSTREAM_NOTE}


@tool("maintenance", "Upstream contribution plan", writes=True)
def pr_plan_status() -> dict:
    """Regenerate and summarize the upstream contribution plan.

    Every number is measured, not asserted. The three criteria that cannot be met from here — never
    compiled, no usage telemetry, no maintainer asked — are reported as unmet rather than glossed.
    Never invent the telemetry: an RPC method with no real-session usage is cut before submission.
    """
    r = run_mise_task("pr:report")
    plan = repo_root() / "docs" / "pr-plan.md"
    return {
        "ok": r["ok"],
        "plan": str(plan),
        "summary": r["stdout"].strip(),
        "content": plan.read_text() if plan.is_file() else None,
    }


@tool("maintenance", "Protected self-dev paths")
def selfdev_protected() -> dict:
    """List the paths self-modification may never touch, and verify their hash pins.

    Read-only, and available whether or not self-dev is enabled: knowing what is frozen is useful
    on its own.
    """
    r = run_mise_task("selfdev:status")
    return {"ok": r["ok"], "status": r["stdout"] or r["stderr"]}
