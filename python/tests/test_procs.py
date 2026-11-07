"""Stopping a child together with everything it started: the compiler behind its wrapper, the
engine. Real processes on POSIX; Windows, which has no process groups, through mocks."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
from nrc_mcp import procs

posix_only = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A zombie still answers kill(0); it has stopped running, which is what matters.
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


@posix_only
def test_a_timeout_ends_the_grandchild_too(tmp_path):
    """A wrapper that runs the real work as its own child (as `tools/q3map2.py` runs q3map2):
    killing only the wrapper on a timeout would leave the work running."""
    pidfile = tmp_path / "grandchild.pid"
    wrapper = (
        "import subprocess, sys\n"
        "p = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "p.wait()\n"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        procs.run([sys.executable, "-c", wrapper], timeout=2)
    grandchild = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _alive(grandchild), "the wrapper's child kept running after the timeout"


@posix_only
def test_a_command_that_finishes_is_returned_like_subprocess_run():
    done = procs.run([sys.executable, "-c", "print('hi')"], timeout=30, text=True)
    assert done.returncode == 0 and done.stdout == "hi\n"


class _FakeProc:
    pid = 4321

    def __init__(self):
        self.returncode = None
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 1
        return 1

    def kill(self):
        self.killed = True


def test_on_windows_the_tree_is_ended_by_taskkill_not_signals(monkeypatch):
    calls = []
    monkeypatch.setattr(procs, "WINDOWS", True)
    monkeypatch.setattr(procs.subprocess, "run", lambda cmd, **k: calls.append(cmd))
    monkeypatch.setattr(procs.os, "killpg", lambda *a: pytest.fail("no killpg on Windows"))
    proc = _FakeProc()
    procs.kill_tree(proc)
    assert calls == [["taskkill", "/PID", "4321", "/T", "/F"]]
    assert proc.returncode == 1


def test_on_windows_a_child_taskkill_missed_is_killed(monkeypatch):
    class Stubborn(_FakeProc):
        def wait(self, timeout=None):
            if timeout is not None and not self.killed:
                raise subprocess.TimeoutExpired("x", timeout)
            self.returncode = 1
            return 1

    monkeypatch.setattr(procs, "WINDOWS", True)

    def no_taskkill(cmd, **k):
        raise OSError("taskkill not found")

    monkeypatch.setattr(procs.subprocess, "run", no_taskkill)
    proc = Stubborn()
    procs.kill_tree(proc, grace=0)
    assert proc.killed and proc.returncode == 1


def test_on_windows_children_start_in_their_own_group(monkeypatch):
    monkeypatch.setattr(procs, "WINDOWS", True)
    kwargs = procs.group_kwargs()
    assert "start_new_session" not in kwargs and kwargs["creationflags"] == 0x200


def test_a_process_that_already_exited_is_left_alone(monkeypatch):
    monkeypatch.setattr(procs.os, "killpg", lambda *a: pytest.fail("signalled an exited group"))
    proc = _FakeProc()
    proc.returncode = 0
    procs.kill_tree(proc)
