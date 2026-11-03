"""Yes/no approval for tool calls that send, write, delete or run things.

Enabled by a ``[sheru.approvals]`` section in ``$OPENJARVIS_HOME/config.toml``::

    [sheru.approvals]
    enabled = true
    timeout_seconds = 120        # no answer in time -> denied
    always_ask = []              # extra tool names that must ask
    never_ask = []               # tool names that never ask (overrides everything else)
    dialog = true                # native macOS Yes/No dialog

When enabled, ``ToolExecutor`` calls :func:`gate` for every tool call, which ignores the
auto-approve callbacks that OpenJarvis passes in at various call sites. Reads run freely;
anything else waits for a yes/no. Answers can come from the macOS dialog or from any
other front end (voice, phone channel) via :func:`pending` / :func:`decide`; the first
answer wins.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from openjarvis.core.paths import get_config_dir

logger = logging.getLogger(__name__)

# Built-in tools that change something outside the conversation.
ASK_TOOLS = frozenset({
    "shell_exec", "docker_shell_exec", "code_interpreter", "code_interpreter_docker", "repl",
    "file_write", "apply_patch", "git_commit",
    "channel_send", "browser_click", "browser_type",
    "agent_spawn", "agent_kill", "agent_send",
    "execute_pending_actions", "skill_manage",
})

# For tools we don't know by name (MCP servers, our own Mac tools): ask when the name
# contains an action verb.
_VERB_RE = re.compile(
    r"(^|[_.\-])(send|reply|forward|write|delete|remove|trash|archive|create|update|edit|"
    r"move|rename|commit|push|exec|execute|run|click|type|press|kill|spawn|post|"
    r"schedule|invite|cancel|accept|decline|pay|buy|order|install|uninstall|quit|shutdown)"
    r"($|[_.\-])",
    re.I,
)

_SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}


# --------------------------------------------------------------------------- config

@dataclass
class _Settings:
    enabled: bool = False
    timeout_seconds: float = 120.0
    always_ask: frozenset = frozenset()
    never_ask: frozenset = frozenset()
    dialog: bool = True


_RECHECK_SECONDS = 2.0     # gate() runs on every tool call; get_config_dir() does file I/O
_cache: Dict[str, Any] = {"key": None, "settings": _Settings(), "env": None, "checked": float("-inf")}


def invalidate() -> None:
    _cache.update(key=None, checked=float("-inf"))


def settings() -> _Settings:
    """Read ``[sheru.approvals]``: look at config.toml at most every couple of seconds
    (or right away if the home dir env changes), re-parse only when it changed."""
    env = (os.environ.get("OPENJARVIS_HOME"), os.environ.get("XDG_DATA_HOME"))
    now = time.monotonic()
    if _cache["env"] == env and now - _cache["checked"] < _RECHECK_SECONDS:
        return _cache["settings"]
    _cache["env"], _cache["checked"] = env, now
    path = get_config_dir() / "config.toml"
    try:
        key = (str(path), path.stat().st_mtime_ns)
    except OSError:
        _cache.update(key=None, settings=_Settings())
        return _cache["settings"]
    if _cache["key"] != key:
        try:
            raw = tomllib.loads(path.read_text()).get("sheru", {}).get("approvals", {})
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("sheru approvals: can't read %s (%s); approvals disabled", path, exc)
            raw = {}
        _cache["settings"] = _Settings(
            enabled=bool(raw.get("enabled", False)),
            timeout_seconds=float(raw.get("timeout_seconds", 120)),
            always_ask=frozenset(raw.get("always_ask", [])),
            never_ask=frozenset(raw.get("never_ask", [])),
            dialog=bool(raw.get("dialog", True)),
        )
        _cache["key"] = key
    return _cache["settings"]


# --------------------------------------------------------------------------- policy

def needs_approval(spec: Any, params: Any, cfg: Optional[_Settings] = None) -> bool:
    cfg = cfg or settings()
    name = spec.name
    if name in cfg.never_ask:
        return False
    if name in cfg.always_ask or name in ASK_TOOLS or getattr(spec, "requires_confirmation", False):
        return True
    if name == "http_request":
        method = str((params or {}).get("method", "GET")).upper() if isinstance(params, dict) else "GET"
        return method not in _SAFE_HTTP_METHODS
    return bool(_VERB_RE.search(name))


# --------------------------------------------------------------------------- broker

@dataclass
class Request:
    id: str
    tool: str
    args: Dict[str, Any]
    question: str
    created: float = field(default_factory=time.time)
    answer: Optional[bool] = None
    answered_by: str = ""
    _done: threading.Event = field(default_factory=threading.Event, repr=False)


_lock = threading.Lock()
_pending: Dict[str, Request] = {}
_listeners: List[Callable[[Request], None]] = []


def add_listener(fn: Callable[[Request], None]) -> None:
    """Get told about each new request (e.g. the voice front end, to speak the question)."""
    _listeners.append(fn)


def pending() -> List[Request]:
    with _lock:
        return list(_pending.values())


def decide(request_id: str, approve: bool, by: str = "api") -> bool:
    """Answer a pending request. Returns False if it was already answered or doesn't exist."""
    with _lock:
        req = _pending.get(request_id)
        if req is None or req._done.is_set():
            return False
        req.answer, req.answered_by = bool(approve), by
        req._done.set()
    return True


def _short(v: Any, n: int = 160) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def question_for(tool: str, args: Dict[str, Any]) -> str:
    lines = [f"Jarvis wants to use {tool}."]
    for k, v in list(args.items())[:6]:
        lines.append(f"{k}: {_short(v)}")
    lines.append("Allow it?")
    return "\n".join(lines)


def ask(tool: str, args: Any, cfg: Optional[_Settings] = None) -> Request:
    """Block until someone answers yes/no (or the timeout passes -> no)."""
    cfg = cfg or settings()
    args = args if isinstance(args, dict) else {"input": args}
    req = Request(id=uuid.uuid4().hex[:8], tool=tool, args=args, question=question_for(tool, args))
    with _lock:
        _pending[req.id] = req
    try:
        for fn in list(_listeners):
            try:
                fn(req)
            except Exception:
                logger.exception("sheru approvals: listener failed")
        dialog = _start_dialog(req, cfg.timeout_seconds) if cfg.dialog else None
        if not req._done.wait(cfg.timeout_seconds):
            decide(req.id, False, by="timeout")
        if dialog is not None and dialog.poll() is None:
            dialog.kill()                      # answered elsewhere; close the dialog
    finally:
        with _lock:
            _pending.pop(req.id, None)
    logger.info("sheru approvals: %s %s (by %s)", tool, "approved" if req.answer else "denied", req.answered_by)
    return req


def gate(spec: Any, params: Any) -> Optional[bool]:
    """Called by ToolExecutor. None = Sheru approvals off (use upstream behavior);
    True = go ahead; False = the user said no (or didn't answer)."""
    cfg = settings()
    if not cfg.enabled:
        return None
    if not needs_approval(spec, params, cfg):
        return True
    return bool(ask(spec.name, params, cfg).answer)


# --------------------------------------------------------------------------- macOS dialog

_DIALOG_SCRIPT = (
    "on run argv\n"
    "  set r to display dialog (item 1 of argv) with title \"Jarvis\" buttons {\"No\", \"Yes\"} "
    "default button \"No\" with icon caution giving up after (item 2 of argv as integer)\n"
    "  if gave up of r then return \"timeout\"\n"
    "  return button returned of r\n"
    "end run"
)


def _start_dialog(req: Request, timeout: float) -> Optional[subprocess.Popen]:
    try:
        proc = subprocess.Popen(
            ["osascript", "-e", _DIALOG_SCRIPT, req.question, str(int(timeout))],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError as exc:
        logger.warning("sheru approvals: no dialog (%s)", exc)
        return None

    def _wait() -> None:
        out = (proc.communicate()[0] or "").strip()
        if out in ("Yes", "No"):
            decide(req.id, out == "Yes", by="dialog")

    threading.Thread(target=_wait, name=f"sheru-approval-{req.id}", daemon=True).start()
    return proc
