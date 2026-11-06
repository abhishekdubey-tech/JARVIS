from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..integrations.google_gmail import create_calendar_event, create_gmail_draft

logger = logging.getLogger("jarvis.actions")


class AssistantIntent(BaseModel):
    action: Literal["chat", "task", "calendar_event", "email_draft"] = "chat"
    title: str = ""
    details: str = ""
    due_date: str = ""
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    estimated_minutes: int = Field(default=30, ge=1, le=1440)
    reminder_at: str = ""
    start_datetime: str = ""
    end_datetime: str = ""
    event_date: str = ""
    all_day: bool = False
    recipient: str = ""
    subject: str = ""
    draft_body: str = ""


def _looks_like_action(text: str) -> bool:
    lowered = text.strip().lower()
    if re.search(r"\b(?:don't|do not|never|cannot|can't|shouldn't|wouldn't|not)\b.{0,35}\b(?:create|add|schedule|book|draft|write|compose|remind)\b", lowered):
        return False
    prefix = r"^(?:(?:okay|ok|hey)\s+)?(?:(?:hey\s+)?jarvis(?:\s+\w+){0,2}[,! ]+)?(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:please\s+)?(?:i\s+(?:want|need)\s+you\s+to\s+)?"
    return re.match(prefix + r"(?:add|create|make|schedule|book|put|draft|write|compose|remind)\b", lowered) is not None


def is_confirmation_only(text: str) -> bool:
    normalized = re.sub(r"[.!?]+$", "", text.strip().lower())
    return re.fullmatch(
        r"(?:yes|yeah|yep|yup|sure|okay|ok|absolutely|go ahead|do it|approved|sounds good)"
        r"(?:\s+(?:please|you can|add it|do that|go ahead|please do))*",
        normalized,
    ) is not None


def follows_calendar_approval_prompt(history) -> bool:
    prior = list(reversed(history))
    last_user = next((item.content.lower() for item in reversed(prior) if item.role == "user"), "")
    last_assistant = next((item.content.lower() for item in reversed(prior) if item.role == "assistant"), "")
    user_asked_for_event = any(word in last_user for word in ("calendar", "event", "birthday", "schedule"))
    assistant_discussed_approval = any(word in last_assistant for word in ("approval", "approvals", "queue", "calendar"))
    return user_asked_for_event and assistant_discussed_approval


def _fallback_intent(text: str) -> AssistantIntent:
    if not _looks_like_action(text):
        return AssistantIntent()
    lowered = text.lower()
    if any(p in lowered for p in ("draft an email", "write an email", "compose an email", "draft a reply")):
        return AssistantIntent(action="email_draft", details=text)
    if any(p in lowered for p in ("create an event", "add an event", "schedule a meeting", "schedule an event", "put this on my calendar", "add to my calendar", "create a calendar event")):
        return AssistantIntent(action="calendar_event", details=text)
    if any(p in lowered for p in ("create a task", "create task", "add a task", "add task", "new task", "task to", "remind me to", "remind me at")):
        cleaned = re.sub(r"(?i)^.*?(?:create(?:\s+a)?\s+task(?:\s+(?:to|of))?|add(?:\s+a)?\s+task(?:\s+(?:to|of))?|new task(?:\s+(?:to|of))?|remind me to|remind me at|task to)\s*", "", text).strip(" .")
        time_match = re.search(r"(?i)\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
        title = re.sub(r"(?i)\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", " ", cleaned)
        title = re.sub(r"(?i)\b(?:today|tomorrow|tonight)\b", " ", title)
        title = re.sub(r"(?i)\b(?:and\s+)?(?:notify|remind)\s+me\b.*$", "", title)
        title = re.sub(r"(?i)^to\s+", "", title.strip())
        title = re.sub(r"\s+", " ", title).strip(" .,;:-")
        due_match = re.search(r"(?i)\b(today|tomorrow)\b", text)
        due_date = ""
        current = datetime.now(ZoneInfo(settings.timezone))
        local_day = current.date()
        if due_match:
            local_day = local_day + timedelta(days=1 if due_match.group(1).lower() == "tomorrow" else 0)
            due_date = local_day.isoformat()
        reminder_at = ""
        if time_match:
            hour = int(time_match.group(1)) % 12
            if time_match.group(3).lower() == "pm":
                hour += 12
            minute = int(time_match.group(2) or 0)
            local_reminder = datetime.combine(local_day, datetime.min.time(), tzinfo=ZoneInfo(settings.timezone)).replace(hour=hour, minute=minute)
            if not due_match and local_reminder <= current:
                local_reminder += timedelta(days=1)
                local_day = local_reminder.date()
                due_date = local_day.isoformat()
            reminder_at = local_reminder.isoformat()
        return AssistantIntent(action="task", title=title[:240], details=text, due_date=due_date, reminder_at=reminder_at)
    return AssistantIntent()


async def _interpret(text: str) -> AssistantIntent:
    fallback = _fallback_intent(text)
    if not settings.gemini_api_key or not _looks_like_action(text):
        return fallback
    try:
        from google import genai
        from google.genai import types

        prompt = (
            "Classify the user's message as chat, task, calendar_event, or email_draft. Only choose an action when the user explicitly asks to create/add/draft it. "
            "Extract only details explicitly stated. For a task, if the user asks for a reminder, set reminder_at to an ISO local date-time in the configured timezone; use today for 'today'/'tonight', tomorrow for 'tomorrow', and do not invent a reminder when no time is given. "
            "For a birthday or other all-day event with a date but no time, set all_day=true and event_date to YYYY-MM-DD. For a timed event, use ISO local date-times in the configured timezone and infer a 60 minute end only when a start is explicit and no duration/end was given. "
            "For an email draft, write a complete polite draft body based on the user's instructions, but never claim it was sent. Leave missing recipient or subject empty. "
            f"Configured timezone: {settings.timezone}. Message: {text}"
        )
        client = genai.Client(api_key=settings.gemini_api_key)
        try:
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=settings.gemini_model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=AssistantIntent,
                    thinking_config=types.ThinkingConfig(thinking_level="low"),
                ),
            )
        finally:
            client.close()
        intent = AssistantIntent.model_validate(response.parsed) if response.parsed else AssistantIntent.model_validate_json(response.text or "{}")
        return fallback if intent.action == "chat" and fallback.action != "chat" else intent
    except Exception as exc:
        logger.warning("Action intent extraction failed (%s); using safe explicit-phrase fallback", type(exc).__name__)
        return fallback


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(settings.timezone))
    return parsed.astimezone(ZoneInfo(settings.timezone))


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def execute_approval_action(approval: models.Approval) -> str:
    """Execute only supported actions, after the approval row was explicitly approved."""
    if approval.action != "calendar.create_event":
        return "Approval recorded; no external action executor is configured for this action."
    try:
        event_data = json.loads(approval.details)
        result = create_calendar_event(approval.id, event_data)
        return "Calendar event created after approval." + (f" Google Calendar event: {result['html_link']}" if result.get("html_link") else "")
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(422, "The approved calendar event details are invalid.") from exc


async def execute_explicit_chat_action(text: str, db: Session) -> str | None:
    """Perform local task creation, create a draft, or queue an event for approval."""
    intent = await _interpret(text)
    if intent.action == "chat":
        return None

    if intent.action == "task":
        title = intent.title.strip()
        if not title:
            return "What should I call the task? For example: ‘Add a task to review my notes tomorrow.’"
        reminder = None
        if intent.reminder_at.strip():
            try:
                reminder = _parse_datetime(intent.reminder_at)
            except (ValueError, TypeError):
                return "I couldn't parse the reminder time. Please include a clear time, such as ‘today at 8:20 PM’."
            now_local = datetime.now(ZoneInfo(settings.timezone))
            if reminder <= now_local:
                return "That reminder time has already passed. Please give me a future time."
        task = models.Task(
            title=title[:240], description=intent.details[:4000], priority=intent.priority,
            category="Personal", due_date=intent.due_date or None,
            estimated_minutes=intent.estimated_minutes,
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        if reminder:
            if not (settings.telegram_bot_token and settings.telegram_user_id):
                return f"Added task ‘{task.title}’, but I couldn't schedule its reminder because Telegram isn't configured."
            db.add(models.TaskReminder(task_id=task.id, message=task.title, scheduled_at=_as_utc(reminder)))
            db.commit()
        due = f" It’s due {task.due_date}." if task.due_date else ""
        reminder_text = f" I’ll remind you on Telegram at {reminder.strftime('%b %d, %Y %I:%M %p')} ({settings.timezone}) to handle it; I won’t send the email." if reminder else ""
        return f"Added task ‘{task.title}’.{due}{reminder_text} You can see it in Tasks."

    if intent.action == "calendar_event":
        if not intent.title.strip():
            return "I can prepare that event, but I need its title."
        if intent.all_day:
            try:
                event_day = date.fromisoformat(intent.event_date or intent.start_datetime[:10])
            except ValueError:
                return "I couldn't understand the event date. Please give me a clear date, such as ‘December 8’."
            if event_day < datetime.now(ZoneInfo(settings.timezone)).date():
                return "That event date has already passed. Please give me a future date."
            details = {
                "summary": intent.title.strip()[:200], "description": intent.details[:4000],
                "date": event_day.isoformat(), "timezone": settings.timezone, "all_day": True,
            }
            approval = models.Approval(
                action="calendar.create_event", target=details["summary"],
                details=json.dumps(details, ensure_ascii=False), status="pending",
            )
            db.add(approval)
            db.commit()
            db.refresh(approval)
            return f"I queued the all-day event ‘{details['summary']}’ for {event_day.strftime('%b %d, %Y')} ({settings.timezone}). Review it in Approvals; it won't be added until you approve it."
        if not intent.start_datetime.strip():
            return "I can prepare that event, but I need its title and start date/time. Please include both, and optionally the duration or end time."
        try:
            start = _parse_datetime(intent.start_datetime)
            end = _parse_datetime(intent.end_datetime) if intent.end_datetime else start + timedelta(minutes=60)
        except (ValueError, TypeError):
            return "I couldn't understand the event date/time. Please give a clear date and start time, such as ‘November 6 at 3 PM’."
        now = datetime.now(ZoneInfo(settings.timezone))
        if start <= now:
            return "That start time is in the past. Please give me a future date and time."
        if end <= start or end - start > timedelta(hours=24):
            return "That event duration doesn't look valid. Please give an end time after the start, within 24 hours."
        details = {
            "summary": intent.title.strip()[:200], "description": intent.details[:4000],
            "start": start.isoformat(), "end": end.isoformat(), "timezone": settings.timezone,
        }
        approval = models.Approval(
            action="calendar.create_event", target=details["summary"],
            details=json.dumps(details, ensure_ascii=False), status="pending",
        )
        db.add(approval)
        db.commit()
        db.refresh(approval)
        return f"I prepared ‘{details['summary']}’ for {start.strftime('%b %d, %Y at %I:%M %p')} ({settings.timezone}). It’s in Approvals; I’ll add it to Calendar only after you approve it."

    if intent.action == "email_draft":
        recipient = intent.recipient.strip()
        subject = intent.subject.strip()
        body = intent.draft_body.strip()
        if not recipient or "@" not in recipient:
            return "Who should receive the draft? Please include their email address. I won’t send it."
        if not subject:
            return "What subject should I use for the email draft? I won’t send it."
        if not body:
            return "What should the email say? Tell me the key points and I’ll prepare a draft."
        try:
            draft_id = create_gmail_draft(recipient=recipient, subject=subject, body=body)
        except HTTPException as exc:
            return exc.detail
        return f"I saved an email draft to {recipient} with subject ‘{subject}’. It is in Gmail Drafts and has not been sent. Draft reference: {draft_id}."

    return None
