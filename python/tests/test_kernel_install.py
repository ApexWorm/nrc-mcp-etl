"""`kernel:build` installs the extension without disturbing a process that has it loaded.

A loaded extension is a shared mapping of the file. The old install step `cp`-ed over it in
place, which rewrites those mapped pages under the running process (two tasks that both depend
on `kernel:build`, test:unit and bench:dogfood, crashed each other that way). These tests map
the installed file the way the dynamic loader does and check what the mapping sees.
"""

from __future__ import annotations

import importlib.util
import mmap
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _tool():
    path = ROOT / "tools" / "kernel_install.py"
    spec = importlib.util.spec_from_file_location("_tool_kernel_install", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _mapped(path: Path):
    f = open(path, "rb")  # noqa: SIM115 - kept open as long as the mapping, like a loaded library
    return f, mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ, flags=mmap.MAP_SHARED)


def test_the_old_in_place_copy_rewrote_a_loaded_library(tmp_path):
    """The hazard itself, so the fix below is measured against something real."""
    dest = tmp_path / "nrc_py.so"
    dest.write_bytes(b"A" * 4096)
    (tmp_path / "new.so").write_bytes(b"B" * 4096)
    f, view = _mapped(dest)
    shutil.copyfile(tmp_path / "new.so", dest)  # what `cp` does: same inode, truncated, rewritten
    assert view[:1] == b"B", "the running process's code changed under it"
    view.close()
    f.close()


def test_a_changed_library_is_renamed_into_place(tmp_path):
    ki = _tool()
    dest = tmp_path / "nrc_py.so"
    dest.write_bytes(b"A" * 4096)
    built = tmp_path / "libnrc_py.so"
    built.write_bytes(b"B" * 4096)
    built.chmod(0o755)
    f, view = _mapped(dest)
    before = dest.stat().st_ino
    assert ki.install(built, dest) == "installed"
    assert view[:4096] == b"A" * 4096, "a process that loaded the old file keeps it intact"
    assert dest.read_bytes() == b"B" * 4096 and dest.stat().st_ino != before
    assert dest.stat().st_mode & 0o111, "still executable"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["libnrc_py.so", "nrc_py.so"]
    view.close()
    f.close()


def test_an_unchanged_library_is_not_touched(tmp_path):
    ki = _tool()
    dest = tmp_path / "nrc_py.so"
    built = tmp_path / "libnrc_py.so"
    dest.write_bytes(b"same")
    built.write_bytes(b"same")
    inode = dest.stat().st_ino
    f, view = _mapped(dest)
    assert ki.install(built, dest) == "unchanged"
    assert dest.stat().st_ino == inode and view[:] == b"same"
    view.close()
    f.close()


def test_installs_wait_for_each_other(tmp_path):
    """One build-and-install at a time: a second waits on the lock the first holds."""
    import fcntl

    ki = _tool()
    lock = tmp_path / ".kernel-install.lock"
    with ki.exclusive(lock):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl, sys; f = open(sys.argv[1], 'a+')\n"
                "try:\n    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)\nexcept OSError:\n"
                "    sys.exit(3)\n",
                str(lock),
            ],
            check=False,
        )
        assert probe.returncode == 3, "the lock is held while the first install runs"
    with open(lock, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)  # and released afterwards


def test_no_build_installs_what_target_holds(tmp_path):
    ki = _tool()
    (tmp_path / "release").mkdir()
    (tmp_path / "release" / "libnrc_py.so").write_bytes(b"lib")
    dest = tmp_path / "out.so"
    assert ki.main(["--no-build", "--target-dir", str(tmp_path), "--dest", str(dest)]) == 0
    assert dest.read_bytes() == b"lib"
    empty = tmp_path / "empty"
    (empty / "release").mkdir(parents=True)
    assert ki.main(["--no-build", "--target-dir", str(empty), "--dest", str(dest)]) == 1
