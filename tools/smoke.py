#!/usr/bin/env python3
"""Load a packed map in the game's own engine and report what its log says.

    mise run game:smoke -- out/mymap_autopacked.pk3            # dedicated server only
    mise run game:smoke -- out/mymap_autopacked.pk3 --client   # also join as a spectator

Needs NRC_FS_BASEPATH (the game install) and a profile with an `engine:` section. Runs in a
throwaway home folder, so nothing is left in the install or in your own game settings.

Exit codes: 0 the map loaded and nothing stopped the game, 1 it did not, 2 the smoke test could
not run (no engine, no game data, no display for the client).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "python" / "src"))

from nrc_mcp import engine  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("pk3", help="the package pack_pk3 wrote")
    ap.add_argument("--client", action="store_true", help="also start the client, which joins")
    ap.add_argument("--seconds", type=float, default=12, help="how long the server runs")
    ap.add_argument("--client-seconds", type=float, default=40, help="how long the client runs")
    ap.add_argument("--keep", action="store_true", help="keep the home folder and its log")
    ap.add_argument("--profile", default=os.environ.get("NRC_PROFILE", ""))
    args = ap.parse_args()
    try:
        result = engine.smoke(
            args.pk3,
            args.profile,
            seconds=args.seconds,
            client=args.client,
            client_seconds=args.client_seconds,
            keep=args.keep,
        )
    except engine.EngineError as e:
        print(json.dumps({"ok": False, "skipped": str(e)}, indent=2))
        return 2
    print(json.dumps(result, indent=2))
    if not result["ok"]:
        return 1
    # The server passed, but the client asked for could not start: not a full pass.
    return 2 if result.get("client_skipped") else 0


if __name__ == "__main__":
    sys.exit(main())
