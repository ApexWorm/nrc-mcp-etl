"""Where the game is installed: the one place that reads it from the environment.

`NRC_FS_BASEPATH`, falling back to `URT_BASEPATH` (its name before there was a second
profile, README "Environment"). `tools/q3map2.py` resolves the same two names in the same
order, so the compiler, the packer and the engine always agree on which install they use:
when they did not, a package q3map2 wrote into the install was not recognised as being there
and stayed in it.
"""

from __future__ import annotations

import os

#: Read in this order; the first one set wins. Kept in step with `tools/q3map2.py`.
BASEPATH_VARIABLES = ("NRC_FS_BASEPATH", "URT_BASEPATH")


def basepath() -> str:
    """The game install's path, or "" when none is set."""
    for name in BASEPATH_VARIABLES:
        value = os.environ.get(name)
        if value:
            return value
    return ""
