#!/usr/bin/env python3
"""Build the Rust kernel and install its Python extension without breaking a process using it.

`kernel:build` is a dependency of several tasks (`test:unit`, `bench:dogfood`, ...), so two of
them started side by side both build and install `python/src/nrc_py.so`. Copying over that file
in place (`cp` opens the existing file and truncates it) rewrites the pages every running Python
process has mapped, and those processes crash mid-run with SIGBUS or a segfault. So here:

- one build-and-install at a time, under an exclusive lock on `target/.kernel-install.lock`;
- the copy is skipped when the built library is byte-identical to the installed one (only
  its timestamp is refreshed, for mise's sources/outputs check);
- otherwise the library is written to a temporary file beside the destination and renamed
  over it (`os.replace`, atomic on POSIX). A process that already loaded the old file keeps
  its inode; only processes started afterwards see the new one.

    python3 tools/kernel_install.py            # cargo build --release, then install
    python3 tools/kernel_install.py --no-build # install what target/release holds
"""

from __future__ import annotations

import argparse
import contextlib
import filecmp
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("NRC_ROOT") or Path(__file__).resolve().parents[1])
#: Cargo names the cdylib libnrc_py.so on Linux and libnrc_py.dylib on macOS; Python imports
#: it as nrc_py.so on both.
BUILT = ("libnrc_py.so", "libnrc_py.dylib")


@contextlib.contextmanager
def exclusive(lock_path: Path):
    """Hold an exclusive lock on `lock_path` (released when the process exits, even on a crash)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as f:
        try:
            import fcntl  # noqa: PLC0415 - POSIX only; elsewhere the install is still atomic
        except ImportError:
            yield
            return
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def install(built: Path, dest: Path) -> str:
    """Put `built` at `dest` atomically. Returns "unchanged" or "installed"."""
    if dest.is_file() and filecmp.cmp(built, dest, shallow=False):
        # Fresh for mise's sources/outputs check; a timestamp is not the mapped content.
        os.utime(dest)
        return "unchanged"
    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as out, open(built, "rb") as src:
            shutil.copyfileobj(src, out)
        shutil.copymode(built, tmp)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return "installed"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--no-build", action="store_true", help="skip cargo build")
    ap.add_argument("--dest", type=Path, default=ROOT / "python" / "src" / "nrc_py.so")
    ap.add_argument("--target-dir", type=Path, default=ROOT / "target")
    args = ap.parse_args(argv)

    with exclusive(args.target_dir / ".kernel-install.lock"):
        if not args.no_build:
            subprocess.run(["cargo", "build", "--release"], cwd=ROOT, check=True)
        built = next(
            (
                args.target_dir / "release" / n
                for n in BUILT
                if (args.target_dir / "release" / n).is_file()
            ),
            None,
        )
        if built is None:
            print(
                "kernel:build: cargo produced no libnrc_py.{so,dylib} in target/release",
                file=sys.stderr,
            )
            return 1
        print(
            f"kernel:build: {install(built, args.dest)} {args.dest.relative_to(ROOT) if args.dest.is_relative_to(ROOT) else args.dest}"
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
