"""Sheru yes/no approvals: policy, broker, and the ToolExecutor hook."""

from __future__ import annotations

import json
import threading

import pytest

from openjarvis.core.types import ToolCall, ToolResult
from openjarvis.sheru import approvals
from openjarvis.tools._stubs import BaseTool, ToolExecutor, ToolSpec


class _Tool(BaseTool):
    def __init__(self, name: str, requires_confirmation: bool = False) -> None:
        self.tool_id = name
        self._spec = ToolSpec(name=name, description="t", requires_confirmation=requires_confirmation,
                              parameters={"type": "object", "properties": {}})
        self.ran = 0

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def execute(self, **params) -> ToolResult:
        self.ran += 1
        return ToolResult(tool_name=self._spec.name, content="ok", success=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
    approvals.invalidate()
    approvals._listeners.clear()

    def write(body: str) -> None:
        (tmp_path / "config.toml").write_text(body)
        approvals.invalidate()

    yield write
    approvals._listeners.clear()
    approvals.invalidate()


def test_config_edits_are_picked_up_without_restart(home, monkeypatch):
    home(ON)
    assert approvals.settings().enabled
    home("")                                          # edit config.toml...
    monkeypatch.setattr(approvals, "_RECHECK_SECONDS", 0.0)
    assert not approvals.settings().enabled           # ...seen on the next check


ON = '[sheru.approvals]\nenabled = true\ndialog = false\ntimeout_seconds = 2\n'


def _answer_with(value: bool) -> None:
    approvals.add_listener(
        lambda req: threading.Timer(0.05, approvals.decide, (req.id, value, "test")).start())


def _run(tool: _Tool, args: dict | None = None, **kw) -> ToolResult:
    ex = ToolExecutor([tool], **kw)
    return ex.execute(ToolCall(id="1", name=tool.spec.name, arguments=json.dumps(args or {})))


def test_disabled_keeps_upstream_behaviour(home):
    home("")                                          # no [sheru] section
    assert approvals.gate(_Tool("file_write").spec, {}) is None
    t = _Tool("shell_exec", requires_confirmation=True)
    assert not _run(t).success and t.ran == 0         # upstream: no callback -> refused
    t = _Tool("shell_exec", requires_confirmation=True)
    assert _run(t, interactive=True, confirm_callback=lambda p: True).success


@pytest.mark.parametrize("name,args,expected", [
    ("calendar_upcoming", {}, False),
    ("file_read", {}, False),
    ("web_search", {}, False),
    ("file_write", {}, True),
    ("shell_exec", {}, True),
    ("channel_send", {}, True),
    ("http_request", {"method": "GET"}, False),
    ("http_request", {"method": "post"}, True),
    ("gmail_send_message", {}, True),                 # MCP / our tools: verb in the name
    ("calendar.create_event", {}, True),
    ("mac_click", {}, True),
    ("gmail_search", {}, False),
    ("typescript_lint", {}, False),                   # "type" only counts as a whole word
])
def test_policy(home, name, args, expected):
    home(ON)
    assert approvals.needs_approval(_Tool(name).spec, args) is expected


def test_config_overrides(home):
    home(ON + 'always_ask = ["web_search"]\nnever_ask = ["file_write"]\n')
    assert approvals.needs_approval(_Tool("web_search").spec, {})
    assert not approvals.needs_approval(_Tool("file_write").spec, {})


def test_reads_run_without_asking(home):
    home(ON)
    asked = []
    approvals.add_listener(asked.append)
    t = _Tool("calendar_upcoming")
    assert _run(t).success and t.ran == 1 and asked == []


def test_yes_runs_the_tool_even_without_a_callback(home):
    home(ON)
    _answer_with(True)
    t = _Tool("shell_exec", requires_confirmation=True)
    assert _run(t).success and t.ran == 1


def test_no_blocks_the_tool_even_when_call_site_auto_approves(home):
    home(ON)
    _answer_with(False)
    t = _Tool("file_write")
    r = _run(t, {"path": "/tmp/x"}, interactive=True, confirm_callback=lambda p: True)
    assert not r.success and "denied by user" in r.content and t.ran == 0


def test_no_answer_means_no(home):
    home('[sheru.approvals]\nenabled = true\ndialog = false\ntimeout_seconds = 0.2\n')
    req = approvals.ask("file_write", {"path": "x"})
    assert req.answer is False and req.answered_by == "timeout"
    assert approvals.pending() == []


def test_question_and_first_answer_wins(home):
    home(ON)
    seen = []

    def listener(req):
        seen.append(req.question)
        assert [p.id for p in approvals.pending()] == [req.id]
        threading.Timer(0.05, lambda: (approvals.decide(req.id, True, "voice"),
                                       approvals.decide(req.id, False, "dialog"))).start()

    approvals.add_listener(listener)
    req = approvals.ask("gmail_send_message", {"to": "a@b.c", "body": "hi " * 200})
    assert req.answer is True and req.answered_by == "voice"
    assert "gmail_send_message" in seen[0] and "to: a@b.c" in seen[0] and seen[0].endswith("Allow it?")
    assert len(seen[0]) < 400                         # long args are trimmed
    assert approvals.decide(req.id, False) is False  # already gone
