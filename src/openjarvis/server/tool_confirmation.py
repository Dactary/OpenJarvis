"""Tool confirmation for the API server.

On the CLI a sensitive tool (``requires_confirmation``) asks on the terminal.
The server has no terminal, so such tools used to be refused outright. This
module gives the server a confirmation channel: the request is queued in the
existing approval store, where the GUI (approval bell, voice page) shows it,
and the tool call waits until the user approves or denies it. No answer within
the timeout counts as a refusal.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import time
from typing import Any, Callable, Iterable, Optional

from openjarvis.tools.approval_store import (
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    TIER_HIGH,
    ApprovalStore,
)

logger = logging.getLogger(__name__)

ACTION_TYPE = "tool_confirmation"
DEFAULT_TIMEOUT_S = 90.0
POLL_INTERVAL_S = 0.25

# Prompt built by ToolExecutor: "Allow execution of tool 'x' with args {...}?"
_PROMPT_RE = re.compile(r"tool '(?P<tool>[^']+)' with args (?P<args>.*)\?\s*$", re.S)


def _parse_args(match: Optional[re.Match]) -> Any:
    """Recover the arguments dict from its repr so the GUI can show it cleanly."""
    if match is None:
        return {}
    text = match.group("args")
    try:
        value = ast.literal_eval(text)
        json.dumps(value)  # must survive the store's JSON column
        return value
    except Exception:
        return text


def make_confirm_callback(
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    store_factory: Callable[[], ApprovalStore] = ApprovalStore,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[str], bool]:
    """Return a blocking ``confirm_callback`` backed by the approval queue.

    Runs on the agent's worker thread; the event loop stays free to serve the
    approve/deny requests that unblock it.
    """

    def confirm(prompt: str) -> bool:
        match = _PROMPT_RE.search(prompt)
        tool = match.group("tool") if match else "tool"
        payload = {"tool": tool, "arguments": _parse_args(match)}
        store: Optional[ApprovalStore] = None
        try:
            store = store_factory()
            action = store.queue_action(
                ACTION_TYPE,
                prompt,
                payload,
                permission_key=f"{ACTION_TYPE}:{tool}",
                tier=TIER_HIGH,
                ttl_hours=1,
            )
            deadline = clock() + timeout_s
            while clock() < deadline:
                sleep(POLL_INTERVAL_S)
                current = store.get_action(action.id)
                status = current.status if current is not None else STATUS_DENIED
                if status == STATUS_APPROVED:
                    # Consumed here: nothing else may pick it up and run it again.
                    store.update_status(action.id, STATUS_EXECUTED)
                    logger.info("Tool '%s' approved by the user", tool)
                    return True
                if status != STATUS_PENDING:
                    logger.info("Tool '%s' denied by the user (%s)", tool, status)
                    return False
            store.update_status(action.id, STATUS_EXPIRED)
            logger.info("Tool '%s' confirmation timed out: denied", tool)
            return False
        except Exception:
            # Fail closed: an unanswerable question is a refusal.
            logger.exception("Tool confirmation failed; denying '%s'", tool)
            return False
        finally:
            if store is not None:
                try:
                    store.close()
                except Exception:
                    pass

    return confirm


def enable_tool_confirmation(
    agent: object,
    extra_tools: Iterable[str] = (),
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bool:
    """Give an already-built agent a GUI-backed confirmation channel.

    ``extra_tools`` names tools that must be confirmed even though their spec
    does not ask for it. Returns False when the agent has no tool executor.
    """
    executor = getattr(agent, "_executor", None)
    if executor is None or not hasattr(executor, "_confirm_callback"):
        return False
    executor._interactive = True
    executor._confirm_callback = make_confirm_callback(timeout_s=timeout_s)
    executor.require_confirmation_for(extra_tools)
    return True


__all__ = ["ACTION_TYPE", "enable_tool_confirmation", "make_confirm_callback"]
