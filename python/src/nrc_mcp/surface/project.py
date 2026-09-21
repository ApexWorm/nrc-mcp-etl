"""The mise task surface — everything this project can do (§1.2).

Registered through `nrc_mcp.runtime.tool`, which carries the group and the safety
annotations. See `nrc_mcp.server` for how the surface is assembled and scoped.
"""

from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError

from .. import tasks
from ..kernel import run_mise_task
from ..runtime import (
    tool,
)


@tool("project", "List project tasks")
def task_list() -> dict:
    """Everything this project can do, discovered from mise.

    This is the action surface (§1.2). Prefer a task over improvising a shell command: the
    task is reproducible by a human and recorded as a name plus arguments. Tasks flagged
    `mutates_user_data` need explicit intent before running.
    """
    return {"tasks": tasks.list_tasks()}


@tool("project", "Run a project task", writes=True, destructive=True)
def task_run(name: str, args: list[str] | None = None, acknowledge_mutation: bool = False) -> dict:
    """Run a mise task by name.

    Args:
        name: exact task name from `task_list`.
        args: arguments passed after `--`.
        acknowledge_mutation: required for tasks that mutate user data.
    """
    known = tasks.task_names()
    if name not in known:
        # The near-misses are the whole value of this error, so they go in the message:
        # a ToolError carries no structured payload, and "no such task" on its own tells
        # the caller nothing it did not already know.
        close = sorted(n for n in known if name.split(":")[0] in n)[:8]
        suggestion = f" Did you mean: {', '.join(close)}." if close else ""
        raise ToolError(
            f"no task named {name!r}.{suggestion} Call task_list() for the full surface."
        )
    info = next(t for t in tasks.list_tasks() if t["name"] == name)
    if info["mutates_user_data"] and not acknowledge_mutation:
        raise ToolError(
            f"{name} mutates user data, so it needs explicit intent: re-call with "
            f"acknowledge_mutation=true. What it does: {info['description']}"
        )
    return run_mise_task(name, args)
