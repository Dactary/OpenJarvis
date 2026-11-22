"""Tool confirmation through the approval queue (API server)."""

from __future__ import annotations

import threading
import time

from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.server.tool_confirmation import (
    ACTION_TYPE,
    enable_tool_confirmation,
    make_confirm_callback,
)
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec
from openjarvis.tools.approval_store import (
    STATUS_APPROVED,
    STATUS_DENIED,
    STATUS_EXECUTED,
    STATUS_EXPIRED,
    ApprovalStore,
)

PROMPT = "Allow execution of tool 'shell_exec' with args {'command': 'dir'}?"


def _answer_when_pending(db_path: str, status: str) -> threading.Thread:
    """Play the user: wait for the request to appear, then decide."""
    # Create the database first: on the server it exists long before a tool
    # asks, and two connections creating it at once would race on WAL setup.
    ApprovalStore(db_path).close()

    def run() -> None:
        store = ApprovalStore(db_path)
        try:
            for _ in range(200):
                pending = store.list_pending()
                if pending:
                    assert pending[0].action_type == ACTION_TYPE
                    assert pending[0].payload["tool"] == "shell_exec"
                    assert pending[0].payload["arguments"] == {"command": "dir"}
                    store.update_status(pending[0].id, status)
                    return
                time.sleep(0.01)
        finally:
            store.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def _callback(db_path: str, timeout_s: float = 5.0):
    return make_confirm_callback(
        timeout_s=timeout_s,
        store_factory=lambda: ApprovalStore(db_path),
        sleep=lambda _s: time.sleep(0.01),
    )


def _only_action(db_path: str):
    store = ApprovalStore(db_path)
    try:
        row = store._conn.execute("SELECT id FROM pending_actions").fetchall()
        assert len(row) == 1
        return store.get_action(row[0][0])
    finally:
        store.close()


def test_approved_request_runs_and_is_consumed(tmp_path):
    db = str(tmp_path / "approvals.db")
    user = _answer_when_pending(db, STATUS_APPROVED)
    assert _callback(db)(PROMPT) is True
    user.join(2)
    assert _only_action(db).status == STATUS_EXECUTED


def test_denied_request_is_refused(tmp_path):
    db = str(tmp_path / "approvals.db")
    user = _answer_when_pending(db, STATUS_DENIED)
    assert _callback(db)(PROMPT) is False
    user.join(2)


def test_no_answer_is_a_refusal(tmp_path):
    db = str(tmp_path / "approvals.db")
    assert _callback(db, timeout_s=0.05)(PROMPT) is False
    assert _only_action(db).status == STATUS_EXPIRED


def test_store_failure_fails_closed():
    def broken():
        raise OSError("disk gone")

    assert make_confirm_callback(store_factory=broken)(PROMPT) is False


class _EchoTool(BaseTool):
    tool_id = "echo_test"

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(name="echo_test", description="echo", parameters={})

    def execute(self, **params) -> ToolResult:
        return ToolResult(tool_name="echo_test", content="ran", success=True)


class _Agent:
    def __init__(self) -> None:
        self._executor = ToolExecutor([_EchoTool()])


def test_named_tool_needs_confirmation_once_enabled():
    agent = _Agent()
    call = ToolCall(id="1", name="echo_test", arguments="{}")
    assert agent._executor.execute(call).content == "ran"  # not sensitive by spec

    assert enable_tool_confirmation(agent, ["echo_test"]) is True
    agent._executor._confirm_callback = lambda _prompt: False
    denied = agent._executor.execute(call)
    assert denied.success is False
    assert "denied" in denied.content

    agent._executor._confirm_callback = lambda _prompt: True
    assert agent._executor.execute(call).content == "ran"


def test_agent_without_executor_is_left_alone():
    assert enable_tool_confirmation(object(), ["echo_test"]) is False
