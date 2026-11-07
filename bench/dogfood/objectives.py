"""Generate a playtest from each example map's objectives and run it in the engine, over MCP.

    mise run bench:dogfood -- objectives depot [--map <.map> --pk3 <.pk3>]

Without --map/--pk3 it uses the newest build of that example under out/dogfood/<example>/
(build it first with `mise run bench:dogfood -- <example>`). Reports, per objective, whether a
test was generated, whether it ran, and whether it passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from dogfood_session import REPO, DogfoodSession

EXAMPLES = {
    "depot": "depot",
    "workshop": "constructible",
    "frontline": "frontline",
    "frontline_one_way": "frontline-one-way",
    "convoy": "convoy",
}


def newest(example: str) -> tuple[str, str]:
    runs = sorted((REPO / "out/dogfood" / EXAMPLES[example]).glob("*/"))
    for run in reversed(runs):
        for name in ("result.json", "summary.json"):
            path = run / name
            if path.is_file():
                data = json.loads(path.read_text())
                if data.get("map") and data.get("package"):
                    return data["map"], data["package"]
    raise SystemExit(f"no build of {example}; run `mise run bench:dogfood -- {EXAMPLES[example]}`")


async def main(example: str, map_path: str | None, package: str | None):
    if not (map_path and package):
        map_path, package = newest(example)
    async with DogfoodSession("objectives-" + example) as run:
        await run.call("map_open", path=map_path)
        generated = await run.call("objective_playtests")
        rows = {
            t["objective"]: {"kind": t["kind"], "generated": True, "ran": False, "passed": False}
            for t in generated["tests"]
        }
        for s in generated["skipped"]:
            key = s.get("objective") or f"entity {s['entity']}"
            rows[key] = {"kind": s.get("kind"), "generated": False, "reason": s["reason"]}
        receipts = []
        for i, r in enumerate(generated["runs"]):
            response = await run.call(
                "engine_playtest",
                pk3=package,
                scenario=r["scenario"],
                profile_id="etl",
                timeout_seconds=r["timeout_seconds"],
                include_images=False,
            )
            result = response if "ok" in response else json.loads(response["notes"][0])
            run.artifact(f"run-{i}", result)
            receipts.append(result.get("result_path"))
            failed_at = None if result["ok"] else _failed_step(result)
            # Steps belong to the objectives in order; a failure fails its objective and
            # leaves the ones after it unrun.
            bounds = _bounds(generated["tests"], r["objectives"])
            for name in r["objectives"]:
                lo, hi = bounds[name]
                row = rows[name]
                row["ran"] = failed_at is None or failed_at >= lo
                row["passed"] = result["ok"] or (failed_at is not None and failed_at > hi)
                if failed_at is not None and lo <= failed_at <= hi:
                    row["failed"] = {"step": failed_at, "error": result.get("error")}
                row["result"] = result.get("result_path")
        out = {"map": map_path, "package": package, "objectives": rows, "receipts": receipts}
        print(json.dumps({"result": run.artifact("result", out), "objectives": rows}, indent=2))
        if not all(r.get("passed") for r in rows.values()):
            raise SystemExit(1)


def _failed_step(result: dict) -> int | None:
    for step in result.get("steps", []):
        if not step.get("ok"):
            return step["index"]
    return len(result.get("steps", []))


def _bounds(tests: list[dict], names: list[str]) -> dict[str, tuple[int, int]]:
    """Step index range of each objective in a packed run (its steps plus a screenshot)."""
    by_name = {t["objective"]: len(t["steps"]) + 1 for t in tests}
    out, at = {}, 0
    for name in names:
        out[name] = (at, at + by_name[name] - 1)
        at += by_name[name]
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("example", choices=sorted(EXAMPLES))
    parser.add_argument("--map", help="the example's .map (default: the newest build)")
    parser.add_argument("--pk3", help="its package (default: the newest build)")
    args = parser.parse_args()
    asyncio.run(main(args.example, args.map, args.pk3))
