"""Every test starts without an open map.

The session is process-wide, and `map_open` refuses to drop a map with unsaved work, so a
test that leaves edits behind would otherwise make the next test's `map_open` fail.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _fresh_session():
    yield
    from nrc_mcp.runtime import SESSION, Session

    SESSION.__dict__.update(Session().__dict__)
