"""Run a retained MCP authoring or engine scenario from bench/dogfood."""

from __future__ import annotations

import argparse
import runpy
import sys
from pathlib import Path


def main():
    scenarios = Path(__file__).resolve().parents[1] / "bench/dogfood"
    choices = sorted(p.stem for p in scenarios.glob("*.py") if not p.stem.startswith("dogfood_"))
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("scenario", choices=choices)
    parser.add_argument(
        "arguments", nargs=argparse.REMAINDER, help="Scenario arguments; use <scenario> --help"
    )
    args = parser.parse_args()
    script = scenarios / (args.scenario + ".py")
    sys.path.insert(0, str(scenarios))
    sys.argv = [str(script), *args.arguments]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    main()
