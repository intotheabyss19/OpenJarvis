"""Offline tests for the Sheru Google tools (Google HTTP calls are faked)."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta

import pytest

from openjarvis.sheru import approvals, google_tools as g

TOOLS = {"gmail_search": g.GmailSearchTool, "gmail_read": g.GmailReadTool, "gmail_create_draft": g.GmailDraftTool,
         "calendar_events": g.CalendarEventsTool, "calendar_create_event": g.CalendarCreateTool,
         "tasks_list": g.TasksListTool}


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_parse_when_words_and_naive_times_are_local():
    today = g.parse_when("today")
    assert today.tzinfo is not None and today.hour == 0
    assert g.parse_when("tomorrow") - today == timedelta(days=1)
    assert g.parse_when("2026-11-19T15:00").utcoffset() == datetime.now().astimezone().utcoffset()
    with pytest.raises(ValueError):
        g.parse_when("")


def test_message_text_prefers_plain_and_strips_html():
    payload = {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": _b64("<p>Hi <b>there</b></p><script>x()</script>")}},
        {"mimeType": "text/plain", "body": {"data": _b64("Hi there")}},
    ]}
    assert g.message_text(payload) == "Hi there"
    html_only = {"mimeType": "text/html", "body": {"data": _b64("<style>a{}</style><p>Line&nbsp;one</p><p>two</p>")}}
    assert g.message_text(html_only).split() == ["Line", "one", "two"]


def test_format_event_all_day_and_timed():
    ev = {"summary": "Exams", "start": {"dateTime": "2026-11-23T09:00:00+05:30"},
          "end": {"dateTime": "2026-11-23T10:00:00+05:30"}, "attendees": [{"email": "a@x.com"}, {"self": True}]}
    assert "Exams" in g.format_event(ev) and "with a@x.com" in g.format_event(ev)
    day = {"summary": "Holiday", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}
    assert g.format_event(day, "Holidays").endswith("(all day)  Holiday [Holidays]")


def test_calendar_events_dedupes_across_calendars(monkeypatch):
    item = {"summary": "Guru Nanak Jayanti", "start": {"date": "2026-11-24"}, "end": {"date": "2026-11-25"}}

    def fake_get(url, **params):
        if url.endswith("calendarList"):
            return {"items": [{"id": "primary", "primary": True},
                              {"id": "h1", "selected": True, "summary": "Holidays A"},
                              {"id": "h2", "selected": True, "summary": "Holidays B"}]}
        return {"items": [] if "primary" in url else [item]}

    monkeypatch.setattr(g.google, "get", fake_get)
    r = TOOLS["calendar_events"]().execute(start="2026-11-24", days=1)
    assert r.success and r.content.count("Guru Nanak Jayanti") == 1


def test_gmail_read_rejects_bad_ids():
    r = TOOLS["gmail_read"]().execute(message_id="../../etc")
    assert not r.success


def test_only_create_tools_ask():
    cfg = approvals._Settings(enabled=True)
    asks = {n for n in ("gmail_search", "gmail_read", "calendar_events", "tasks_list",
                        "calendar_create_event", "gmail_create_draft")
            if approvals.needs_approval(TOOLS[n]().spec, {}, cfg)}
    assert asks == {"calendar_create_event", "gmail_create_draft"}


def test_draft_reply_threads_and_fills_recipient(monkeypatch):
    sent = {}
    monkeypatch.setattr(g.google, "get", lambda url, **p: {
        "threadId": "t1", "payload": {"headers": [
            {"name": "From", "value": "Shubham <s@x.com>"}, {"name": "Subject", "value": "Plan"},
            {"name": "Message-ID", "value": "<m1@x>"}]}})
    monkeypatch.setattr(g.google, "post", lambda url, body, **p: sent.update(body) or {"id": "d1"})
    r = TOOLS["gmail_create_draft"]().execute(body="Sounds good", reply_to_id="abc12345")
    raw = base64.urlsafe_b64decode(sent["message"]["raw"]).decode()
    assert r.success and sent["message"]["threadId"] == "t1"
    assert "To: Shubham <s@x.com>" in raw and "Subject: Re: Plan" in raw and "In-Reply-To: <m1@x>" in raw
