"""Child processes that can be stopped with everything they started, on any platform.

The compiler is run through a wrapper (`mise run` -> `tools/q3map2.py` -> q3map2) and the
engine through whatever the install ships, so stopping only the process we started leaves the
one doing the work running. Under POSIX each child gets its own session and the whole process
group is signalled; Windows has no process groups to signal (`os.killpg` and `SIGKILL` do not
exist there), so the tree is ended by parent id with `taskkill /T /F`.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from typing import Any

WINDOWS = os.name == "nt"


def group_kwargs() -> dict[str, Any]:
    """`Popen` arguments that make the child the root of a tree `kill_tree` can end."""
    if WINDOWS:
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen, grace: float = 10) -> None:
    """End `proc` and every process it started, then reap it. A no-op once it has exited.

    POSIX: SIGTERM to the group, then SIGKILL after `grace` seconds. Windows: `taskkill /T /F`,
    then `proc.kill()` in case taskkill itself could not run.
    """
    if proc.poll() is not None:
        return
    if WINDOWS:
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            # The group is gone already, or never was one: fall back to the child alone.
            with contextlib.suppress(ProcessLookupError):
                proc.send_signal(sig)
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue
    proc.wait()


def run(cmd: list[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess:
    """`subprocess.run(cmd, capture_output=True, ...)`, except that a timeout ends the whole
    tree the command started before `TimeoutExpired` is raised, not just its first process."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **group_kwargs(), **kwargs
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        kill_tree(proc)
        out, err = proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout, out, err) from e
    except BaseException:
        kill_tree(proc)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
