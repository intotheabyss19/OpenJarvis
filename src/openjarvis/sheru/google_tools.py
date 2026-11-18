"""Live Gmail / Google Calendar / Google Tasks tools for the agent.

Upstream OpenJarvis only pulls Google data into the morning digest or the knowledge store; these let the
agent answer "what's on tomorrow", "anything from Shubham", "read that mail", and propose a calendar event or
an email draft. Reads run freely; the two tools that change something (``calendar_create_event``,
``gmail_create_draft``) are marked ``requires_confirmation`` so Sheru approvals ask yes/no first.

Uses the token from ``jarvis connect gdrive`` (``~/.openjarvis/connectors/google.json``). The OAuth scope is
gmail.modify, so nothing here can send mail or delete it permanently: drafts are as far as it goes.
"""
from __future__ import annotations

import base64
import html
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Dict, List, Optional

import httpx

from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolResult
from openjarvis.tools._stubs import BaseTool, ToolSpec

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3"
TASKS = "https://tasks.googleapis.com/tasks/v1"


class GoogleError(RuntimeError):
    pass


# --------------------------------------------------------------------------- HTTP + token

class _Google:
    """Tiny Google REST client: cached access token, one refresh-and-retry on 401."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._token_at = 0.0

    @staticmethod
    def token_path() -> str:
        return str(get_config_dir() / "connectors" / "google.json")

    def _access_token(self, force: bool = False) -> str:
        from openjarvis.connectors.oauth import load_tokens, refresh_google_token

        with self._lock:
            if self._token and not force and time.time() - self._token_at < 45 * 60:
                return self._token
            tok = refresh_google_token(self.token_path())
            if not tok:
                tok = None if force else (load_tokens(self.token_path()) or {}).get("access_token")
            if not tok:
                raise GoogleError("Google isn't connected (run `jarvis connect gdrive`).")
            self._token, self._token_at = tok, time.time()
            return tok

    def request(self, method: str, url: str, **kw: Any) -> Any:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {self._access_token(force=attempt > 0)}"}
            try:
                r = httpx.request(method, url, headers=headers, timeout=20.0, **kw)
            except httpx.HTTPError as exc:
                raise GoogleError(f"network error talking to Google: {exc}") from exc
            if r.status_code == 401 and attempt == 0:
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {}).get("message", r.text)
                except ValueError:
                    msg = r.text
                raise GoogleError(f"Google API {r.status_code}: {str(msg)[:300]}")
            return r.json() if r.content else {}
        raise GoogleError("Google rejected the token twice; reconnect with `jarvis connect gdrive`.")

    def get(self, url: str, **params: Any) -> Any:
        return self.request("GET", url, params=params)

    def post(self, url: str, body: Dict[str, Any], **params: Any) -> Any:
        return self.request("POST", url, json=body, params=params)


google = _Google()


# --------------------------------------------------------------------------- helpers

def _local_tz():
    return datetime.now().astimezone().tzinfo


def parse_when(value: Any, *, default: Optional[datetime] = None) -> datetime:
    """'2026-11-19', '2026-11-19T15:00', '2026-11-19 15:00+05:30', 'today', 'tomorrow' -> aware datetime."""
    if value in (None, ""):
        if default is None:
            raise ValueError("missing date/time")
        return default
    s = str(value).strip().lower()
    today = datetime.now(_local_tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    if s in ("now",):
        return datetime.now(_local_tz())
    if s in ("today",):
        return today
    if s in ("tomorrow",):
        return today + timedelta(days=1)
    if s in ("yesterday",):
        return today - timedelta(days=1)
    s = str(value).strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=_local_tz())


def _ok(name: str, text: str, **meta: Any) -> ToolResult:
    return ToolResult(tool_name=name, content=text, success=True, metadata=meta)


def _err(name: str, exc: Exception) -> ToolResult:
    return ToolResult(tool_name=name, content=f"Error: {exc}", success=False)


def _short_date(dt: datetime) -> str:
    now = datetime.now(_local_tz())
    if dt.date() == now.date():
        return dt.strftime("today %H:%M")
    if dt.year == now.year:
        return dt.strftime("%a %d %b %H:%M")
    return dt.strftime("%d %b %Y")


def _headers(msg: Dict[str, Any]) -> Dict[str, str]:
    return {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}


def _sender(h: Dict[str, str]) -> str:
    name, addr = parseaddr(h.get("from", ""))
    return f"{name} <{addr}>" if name else addr


def _msg_date(msg: Dict[str, Any], h: Dict[str, str]) -> Optional[datetime]:
    try:
        return datetime.fromtimestamp(int(msg["internalDate"]) / 1000, _local_tz())
    except (KeyError, ValueError):
        try:
            return parsedate_to_datetime(h["date"]).astimezone(_local_tz())
        except (KeyError, TypeError, ValueError):
            return None


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def message_text(payload: Dict[str, Any]) -> str:
    """Best plain-text body of a Gmail message payload (text/plain preferred, else de-HTML'd text/html)."""
    plain: List[str] = []
    rich: List[str] = []

    def walk(p: Dict[str, Any]) -> None:
        mime = p.get("mimeType", "")
        data = p.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_b64(data))
        elif data and mime == "text/html":
            rich.append(_html_to_text(_b64(data)))
        for part in p.get("parts", []) or []:
            walk(part)

    walk(payload)
    text = "\n".join(plain) if plain else "\n".join(rich)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _event_time(t: Dict[str, Any]) -> tuple[Optional[datetime], bool]:
    if "dateTime" in t:
        return parse_when(t["dateTime"]).astimezone(_local_tz()), False
    if "date" in t:
        return datetime.combine(date.fromisoformat(t["date"]), datetime.min.time(), _local_tz()), True
    return None, False


def format_event(ev: Dict[str, Any], cal_name: str = "") -> str:
    start, all_day = _event_time(ev.get("start", {}))
    end, _ = _event_time(ev.get("end", {}))
    title = ev.get("summary") or "(no title)"
    if start is None:
        when = "?"
    elif all_day:
        last = (end - timedelta(days=1)) if end else start
        when = start.strftime("%a %d %b") + ("" if last.date() == start.date() else last.strftime(" – %a %d %b")) + " (all day)"
    else:
        when = start.strftime("%a %d %b %H:%M") + (end.strftime("–%H:%M") if end else "")
    bits = [f"{when}  {title}"]
    if ev.get("location"):
        bits.append(f"@ {ev['location']}")
    others = [a.get("displayName") or a.get("email") for a in ev.get("attendees", []) if not a.get("self")]
    if others:
        bits.append("with " + ", ".join(others[:5]) + (" +more" if len(others) > 5 else ""))
    if ev.get("hangoutLink"):
        bits.append("(Meet link)")
    if cal_name:
        bits.append(f"[{cal_name}]")
    return " ".join(bits)


# --------------------------------------------------------------------------- Gmail

@ToolRegistry.register("gmail_search")
class GmailSearchTool(BaseTool):
    tool_id = "gmail_search"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_search",
            description=(
                "Search the user's Gmail and list matching emails (newest first): id, date, sender, subject, "
                "snippet. Uses Gmail search syntax, e.g. 'is:unread in:inbox', 'from:shubham newer_than:7d', "
                "'subject:invoice', 'has:attachment from:bank'. Use gmail_read with an id for the full text."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Gmail search query. Default: is:unread in:inbox"},
                    "max_results": {"type": "integer", "description": "1-25, default 10"},
                },
            },
            category="email",
            timeout_seconds=40.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        q = (params.get("query") or "is:unread in:inbox").strip()
        n = max(1, min(int(params.get("max_results") or 10), 25))
        try:
            listing = google.get(f"{GMAIL}/messages", q=q, maxResults=n)
            ids = [m["id"] for m in listing.get("messages", [])]
            if not ids:
                return _ok(self.tool_id, f"No emails match '{q}'.", count=0)
            with ThreadPoolExecutor(max_workers=min(8, len(ids))) as pool:
                msgs = list(pool.map(lambda i: google.get(
                    f"{GMAIL}/messages/{i}", format="metadata", metadataHeaders=["From", "Subject", "Date"]), ids))
            total = listing.get("resultSizeEstimate", len(ids))
            lines = [f"{len(ids)} of ~{total} emails matching '{q}':"]
            for m in msgs:
                h = _headers(m)
                dt = _msg_date(m, h)
                flag = " (unread)" if "UNREAD" in m.get("labelIds", []) else ""
                snippet = html.unescape(m.get("snippet", ""))[:140]
                lines.append(f"- id={m['id']} | {_short_date(dt) if dt else '?'} | {_sender(h)} | "
                             f"{h.get('subject', '(no subject)')}{flag} — {snippet}")
            return _ok(self.tool_id, "\n".join(lines), count=len(ids))
        except Exception as exc:  # noqa: BLE001
            return _err(self.tool_id, exc)


@ToolRegistry.register("gmail_read")
class GmailReadTool(BaseTool):
    tool_id = "gmail_read"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_read",
            description="Read one email in full (headers and plain-text body) by the id from gmail_search.",
            parameters={
                "type": "object",
                "properties": {
                    "message_id": {"type": "string"},
                    "max_chars": {"type": "integer", "description": "body length cap, default 4000"},
                },
                "required": ["message_id"],
            },
            category="email",
        )

    def execute(self, **params: Any) -> ToolResult:
        mid = str(params.get("message_id", "")).strip().removeprefix("id=")
        cap = max(500, min(int(params.get("max_chars") or 4000), 20000))
        if not re.fullmatch(r"[0-9a-fA-F]{8,32}", mid):
            return ToolResult(tool_name=self.tool_id, content="Error: message_id should be the id from gmail_search.",
                              success=False)
        try:
            m = google.get(f"{GMAIL}/messages/{mid}", format="full")
            h = _headers(m)
            dt = _msg_date(m, h)
            body = message_text(m.get("payload", {}))
            if len(body) > cap:
                body = body[:cap] + f"\n…[{len(body) - cap} more characters]"
            files = [p.get("filename") for p in _all_parts(m.get("payload", {})) if p.get("filename")]
            out = [f"From: {_sender(h)}", f"To: {h.get('to', '')}"]
            if h.get("cc"):
                out.append(f"Cc: {h['cc']}")
            out += [f"Date: {dt.strftime('%a %d %b %Y %H:%M') if dt else h.get('date', '')}",
                    f"Subject: {h.get('subject', '(no subject)')}"]
            if files:
                out.append("Attachments: " + ", ".join(files))
            out += ["", body or "(no text body)"]
            return _ok(self.tool_id, "\n".join(out), thread_id=m.get("threadId"))
        except Exception as exc:  # noqa: BLE001
            return _err(self.tool_id, exc)


def _all_parts(p: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = [p]
    for part in p.get("parts", []) or []:
        out += _all_parts(part)
    return out


@ToolRegistry.register("gmail_create_draft")
class GmailDraftTool(BaseTool):
    tool_id = "gmail_create_draft"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="gmail_create_draft",
            description=(
                "Save an email as a DRAFT in Gmail (it is not sent; the user reviews and sends it). To reply, pass "
                "reply_to_id (an id from gmail_search) and the draft joins that thread."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "recipient email(s), comma-separated"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "reply_to_id": {"type": "string", "description": "optional: id of the email being replied to"},
                },
                "required": ["body"],
            },
            category="email",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            msg = EmailMessage()
            to, subject = params.get("to", ""), params.get("subject", "")
            body: Dict[str, Any] = {}
            reply_id = str(params.get("reply_to_id") or "").strip().removeprefix("id=")
            if reply_id:
                orig = google.get(f"{GMAIL}/messages/{reply_id}", format="metadata",
                                  metadataHeaders=["From", "Reply-To", "Subject", "Message-ID", "References"])
                h = _headers(orig)
                to = to or h.get("reply-to") or h.get("from", "")
                subj = h.get("subject", "")
                subject = subject or (subj if subj.lower().startswith("re:") else f"Re: {subj}")
                if h.get("message-id"):
                    msg["In-Reply-To"] = h["message-id"]
                    msg["References"] = (h.get("references", "") + " " + h["message-id"]).strip()
                body["threadId"] = orig.get("threadId")
            if not to:
                raise ValueError("who is it to? pass 'to' or 'reply_to_id'")
            msg["To"], msg["Subject"] = to, subject
            msg.set_content(params.get("body", ""))
            body["raw"] = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            d = google.post(f"{GMAIL}/drafts", {"message": body})
            return _ok(self.tool_id, f"Draft saved in Gmail (to {to}, subject '{subject}'). "
                                     "It has NOT been sent; the user can send it from Gmail.", draft_id=d.get("id"))
        except Exception as exc:  # noqa: BLE001
            return _err(self.tool_id, exc)


# --------------------------------------------------------------------------- Calendar

def _calendars() -> List[Dict[str, Any]]:
    items = google.get(f"{CAL}/users/me/calendarList", minAccessRole="reader").get("items", [])
    return [c for c in items if c.get("selected") or c.get("primary")] or [{"id": "primary", "primary": True}]


@ToolRegistry.register("calendar_events")
class CalendarEventsTool(BaseTool):
    tool_id = "calendar_events"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_events",
            description=(
                "List the user's Google Calendar events in a date range (all their visible calendars), in local "
                "time. start is a date/time like '2026-11-19', '2026-11-19T15:00', 'today' or 'tomorrow' "
                "(default: now). days is how many days to cover (default 1). Optional text query filters events."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "start": {"type": "string"},
                    "days": {"type": "integer", "description": "1-31, default 1"},
                    "query": {"type": "string"},
                },
            },
            category="calendar",
            timeout_seconds=40.0,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            start = parse_when(params.get("start"), default=datetime.now(_local_tz()))
            days = max(1, min(int(params.get("days") or 1), 31))
            end = (start.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=days)
                   if start.time() != datetime.min.time() else start + timedelta(days=days))
            q = params.get("query") or None
            rows = []
            for cal in _calendars():
                p: Dict[str, Any] = dict(timeMin=start.isoformat(), timeMax=end.isoformat(), singleEvents="true",
                                         orderBy="startTime", maxResults=100)
                if q:
                    p["q"] = q
                for ev in google.get(f"{CAL}/calendars/{_quote(cal['id'])}/events", **p).get("items", []):
                    if ev.get("status") == "cancelled":
                        continue
                    s, _ = _event_time(ev.get("start", {}))
                    name = "" if cal.get("primary") else cal.get("summaryOverride") or cal.get("summary", "")
                    rows.append((s or start, format_event(ev, name)))
            seen, uniq = set(), []                     # e.g. two holiday calendars carrying the same day
            for s, line in sorted(rows, key=lambda r: r[0]):
                key = (s, re.sub(r" \[[^\]]*\]$", "", line))
                if key not in seen:
                    seen.add(key)
                    uniq.append((s, line))
            rows = uniq
            span = start.strftime("%a %d %b %H:%M") + " to " + end.strftime("%a %d %b %H:%M")
            if not rows:
                return _ok(self.tool_id, f"No events from {span}" + (f" matching '{q}'." if q else "."), count=0)
            return _ok(self.tool_id, f"{len(rows)} events from {span}:\n" + "\n".join(f"- {r[1]}" for r in rows),
                       count=len(rows))
        except Exception as exc:  # noqa: BLE001
            return _err(self.tool_id, exc)


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")


@ToolRegistry.register("calendar_create_event")
class CalendarCreateTool(BaseTool):
    tool_id = "calendar_create_event"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="calendar_create_event",
            description=(
                "Add an event to the user's primary Google Calendar. start/end like '2026-11-19T15:00' (local "
                "time); end defaults to one hour after start. A date alone ('2026-11-19') makes an all-day event. "
                "If attendees are given, Google emails them an invite."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start": {"type": "string"},
                    "end": {"type": "string"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "attendees": {"type": "array", "items": {"type": "string"}, "description": "email addresses"},
                },
                "required": ["title", "start"],
            },
            category="calendar",
            requires_confirmation=True,
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            raw_start = str(params["start"]).strip()
            ev: Dict[str, Any] = {"summary": params["title"]}
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_start):
                d0 = date.fromisoformat(raw_start)
                raw_end = str(params.get("end") or "").strip()
                d1 = date.fromisoformat(raw_end) + timedelta(days=1) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_end) \
                    else d0 + timedelta(days=1)
                ev["start"], ev["end"] = {"date": d0.isoformat()}, {"date": d1.isoformat()}
            else:
                s = parse_when(raw_start)
                e = parse_when(params.get("end"), default=s + timedelta(hours=1))
                if e <= s:
                    raise ValueError("end must be after start")
                ev["start"], ev["end"] = {"dateTime": s.isoformat()}, {"dateTime": e.isoformat()}
            for k in ("location", "description"):
                if params.get(k):
                    ev[k] = params[k]
            people = [a.strip() for a in params.get("attendees") or [] if a and "@" in a]
            if people:
                ev["attendees"] = [{"email": a} for a in people]
            made = google.post(f"{CAL}/calendars/primary/events", ev, sendUpdates="all" if people else "none")
            return _ok(self.tool_id, "Added: " + format_event(made), event_id=made.get("id"),
                       link=made.get("htmlLink"))
        except Exception as exc:  # noqa: BLE001
            return _err(self.tool_id, exc)


# --------------------------------------------------------------------------- Tasks

@ToolRegistry.register("tasks_list")
class TasksListTool(BaseTool):
    tool_id = "tasks_list"
    is_local = False

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="tasks_list",
            description="List the user's open Google Tasks (all lists), with due dates.",
            parameters={"type": "object", "properties": {}},
            category="tasks",
        )

    def execute(self, **params: Any) -> ToolResult:
        try:
            lines = []
            for tl in google.get(f"{TASKS}/users/@me/lists", maxResults=50).get("items", []):
                items = google.get(f"{TASKS}/lists/{_quote(tl['id'])}/tasks", showCompleted="false",
                                   maxResults=100).get("items", [])
                for t in items:
                    if not t.get("title", "").strip():
                        continue
                    due = f" (due {t['due'][:10]})" if t.get("due") else ""
                    lines.append(f"- {t['title'].strip()}{due} [{tl.get('title', '')}]")
            if not lines:
                return _ok(self.tool_id, "No open tasks.", count=0)
            return _ok(self.tool_id, f"{len(lines)} open tasks:\n" + "\n".join(lines), count=len(lines))
        except Exception as exc:  # noqa: BLE001
            return _err(self.tool_id, exc)
