"""Revision receipts and staged companion files for one authoring session."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_optional(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def write_batch(changes: dict[Path, bytes], expected: dict[Path, bytes | None]) -> None:
    """Check revisions, prepare all writes, then replace; roll back on a write failure.

    Each replacement is atomic. Multiple filenames cannot be replaced atomically by the
    filesystem; backups are retained and named in the error if rollback itself fails.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    for path in changes:
        if read_optional(path) != expected[path]:
            raise ToolError(
                f"refusing to overwrite {path}: changed on disk since it was read; reopen or save a copy"
            )
    prepared: dict[Path, Path] = {}
    backups: dict[Path, Path] = {}
    written: list[Path] = []

    def temporary(path: Path, data: bytes, suffix: str) -> Path:
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=suffix, dir=path.parent)
        tmp = Path(name)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if path.exists():
                tmp.chmod(path.stat().st_mode & 0o777)
            return tmp
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    recovery: list[str] = []
    try:
        for path, data in changes.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            prepared[path] = temporary(path, data, ".pending")
            if expected[path] is not None:
                backups[path] = temporary(path, expected[path], ".backup")
        # Recheck after preparing files, before replacing any of them.
        for path in changes:
            if read_optional(path) != expected[path]:
                raise ToolError(
                    f"refusing to overwrite {path}: changed on disk while preparing save"
                )
        for path, tmp in prepared.items():
            os.replace(tmp, path)
            written.append(path)
    except BaseException as exc:
        for path in reversed(written):
            try:
                if path in backups:
                    os.replace(backups[path], path)
                else:
                    path.unlink()
            except OSError:
                recovery.append(str(backups.get(path, path)))
        if recovery:
            raise ToolError(
                f"save failed and rollback needs recovery from: {', '.join(recovery)}; {exc}"
            ) from exc
        raise
    finally:
        for tmp in [*prepared.values(), *backups.values()]:
            if str(tmp) not in recovery:
                tmp.unlink(missing_ok=True)
