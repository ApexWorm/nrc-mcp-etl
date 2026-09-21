"""The tool surface, one module per group.

**Import order is listing order.** Tools register as these modules are imported, and
`tools/tier2.py` caches the tool-schema prefix between turns — that prefix has to be
byte-identical or the cache never reads. Order is therefore behaviour, not layout.

Which is why the modules are named in a tuple and imported through `importlib`, rather
than written as a plain `from . import a, b, c`. An import block is exactly the thing an
import sorter rewrites: the first version of this file said the order mattered and was
then quietly alphabetised by `ruff format`, which reordered the whole surface. A tuple is
data, and no formatter touches it.

The groups match the README's own headings, so the generated tool table and the prose
around it describe the same thing.
"""

from __future__ import annotations

import importlib
from types import ModuleType

#: Registration order. Changing it re-orders the tool list every client sees.
ORDER = (
    "mapfile",
    "render",
    "sculpt",
    "assets",
    "compiling",
    "gameplay",
    "shipping",
    "project",
    "maintenance",
)

#: The imported modules, in `ORDER`. Importing them is what registers their tools.
MODULES: tuple[ModuleType, ...] = tuple(
    importlib.import_module(f".{name}", __name__) for name in ORDER
)


def get(name: str):
    """The plain Python function behind a tool name, or None if there is no such tool.

    `tools/tapes.py` replays recorded sequences by calling these directly, with no protocol
    in the way — so a tape exercises the same code an agent reaches, minus the transport.
    """
    for mod in MODULES:
        fn = vars(mod).get(name)
        if callable(fn):
            return fn
    return None
