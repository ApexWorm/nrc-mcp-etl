"""Run the saved gameplay examples through the actual MCP tool and retain returned images."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from dogfood_session import REPO, DogfoodSession


async def main(case: str, package: str | None):
    scenario = json.loads((Path(__file__).parent / "scenarios" / (case + ".json")).read_text())
    if package is None:
        report = json.loads((REPO / "out/dogfood/report.json").read_text())
        names = {
            "workshop": "Engineer Workshop",
            "depot": "Relay Depot",
            "movement": "Movement Bunker",
            "frontline": "Frontline Hall",
            "frontline_one_way": "Frontline Hall",
        }
        package = next(row["package"] for row in report["examples"] if row["name"] == names[case])
    async with DogfoodSession("playtest-" + case) as run:
        response = await run.call(
            "engine_playtest", pk3=package, scenario=scenario, profile_id="etl", timeout_seconds=180
        )
        if "notes" in response:
            result = json.loads(response["notes"][0])
            assert response["images"], "MCP must return real screenshot image blocks"
        else:
            result = response
        run.artifact("result", result)
        assert result["ok"], result.get("error", result)
        print(
            json.dumps(
                {
                    "ok": result["ok"],
                    "seconds": result["seconds"],
                    "result": result["result_path"],
                    "wire_images": response.get("images", []),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument(
        "case", choices=["workshop", "depot", "movement", "frontline", "frontline_one_way"]
    )
    parser.add_argument("--pk3", help="Override the package recorded in out/dogfood/report.json")
    args = parser.parse_args()
    asyncio.run(main(args.case, args.pk3))
